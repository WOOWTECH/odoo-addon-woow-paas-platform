import ipaddress
import secrets
from urllib.parse import urlsplit

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from werkzeug.security import check_password_hash, generate_password_hash


class OAuthClient(models.Model):
    _name = 'woow_paas_platform.oauth_client'
    _description = 'OAuth 2.0 Client Application'
    _order = 'create_date desc'

    # Cross-repo contract constant: the fixed callback path the HA custom
    # component registers as its own view. Mirrors WOOW_AUTH_CALLBACK_PATH in
    # the HA component's const.py. Changing this is a cross-repo breaking
    # change requiring lock-step release of both repos.
    HA_CALLBACK_PATH = '/auth/external/woow/callback'

    name = fields.Char(
        string='Application Name',
        required=True,
        help='Display name of the OAuth client application',
    )
    client_id = fields.Char(
        string='Client ID',
        required=True,
        readonly=True,
        index=True,
        default=lambda self: secrets.token_urlsafe(32),
        help='Public identifier for the client application. Readonly because '
             'integrations hardcode this value (e.g. Home Assistant pins '
             'woow-ha-smart-home in const.py) — rotating it breaks them.',
    )
    client_secret_hash = fields.Char(
        string='Client Secret (hashed)',
        help='Hashed client secret. The plain secret is only shown once at creation. '
             'Must be empty for public clients.',
    )
    client_type = fields.Selection(
        selection=[
            ('confidential', 'Confidential'),
            ('public', 'Public'),
        ],
        string='Client Type',
        default='confidential',
        required=True,
        help='Confidential clients store a client_secret (server-to-server, traditional web apps). '
             'Public clients (native apps, SPAs, HA integrations) cannot keep a secret '
             'and must use PKCE for the authorization_code grant.',
    )
    require_pkce = fields.Boolean(
        string='Require PKCE',
        default=False,
        help='If enabled, the authorization endpoint rejects requests without a '
             'code_challenge, and only S256 method is accepted. '
             'Public clients always require PKCE.',
    )
    redirect_uris = fields.Text(
        string='Redirect URIs',
        required=True,
        help='Allowed redirect URIs, one per line. '
             'Must match exactly during authorization (exact mode). '
             'In ha_callback mode this field is a display/record placeholder '
             'and does not participate in host matching.',
    )
    redirect_uri_match_mode = fields.Selection(
        selection=[
            ('exact', 'Exact Match'),
            ('ha_callback', 'HA Callback Pattern'),
        ],
        string='Redirect URI Match Mode',
        default='exact',
        required=True,
        help="exact: redirect_uri must appear verbatim in 'Redirect URIs' "
             "(RFC 6749 strict matching; the default for all clients). "
             "ha_callback: host-flexible matching for the Home Assistant "
             "custom component — scheme in {http,https}, path locked to "
             "HA_CALLBACK_PATH, host unconstrained (http only for "
             "loopback/private/.local hosts). Relies on PKCE to bound the "
             "open-redirect surface; require_pkce must be enabled.",
    )
    scopes = fields.Char(
        string='Allowed Scopes',
        default='smarthome:read smarthome:tunnel workspace:read',
        help='Space-separated list of scopes this client can request',
    )
    grant_types = fields.Char(
        string='Grant Types',
        default='authorization_code,refresh_token',
        help='Comma-separated list of allowed grant types: '
             'authorization_code, refresh_token, client_credentials',
    )
    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this client is allowed to request tokens',
    )

    _sql_constraints = [
        (
            'unique_client_id',
            'UNIQUE(client_id)',
            'Client ID must be unique.',
        ),
    ]

    @api.constrains('client_type', 'client_secret_hash', 'require_pkce', 'grant_types')
    def _check_client_type_consistency(self) -> None:
        """Enforce invariants for public clients (RFC 8252 / RFC 7636 hardening)."""
        for record in self:
            if record.client_type != 'public':
                continue
            if record.client_secret_hash:
                raise ValidationError(
                    _("Public clients must not have a client_secret. "
                      "Public clients rely on PKCE for authorization-code security.")
                )
            if not record.require_pkce:
                raise ValidationError(
                    _("Public clients must have 'Require PKCE' enabled.")
                )
            grant_set = {
                g.strip()
                for g in (record.grant_types or '').split(',')
                if g.strip()
            }
            if 'client_credentials' in grant_set:
                raise ValidationError(
                    _("Public clients cannot use the client_credentials grant type. "
                      "Use a confidential client for server-to-server access.")
                )

    @api.constrains('redirect_uri_match_mode', 'require_pkce')
    def _check_ha_callback_requires_pkce(self) -> None:
        """ha_callback mode relaxes the host check and relies on PKCE to bound
        the open-redirect surface, so PKCE must be enabled. Not keyed on
        client_id on purpose: any client opted into ha_callback inherits the
        same safety floor."""
        for record in self:
            if record.redirect_uri_match_mode == 'ha_callback' and not record.require_pkce:
                raise ValidationError(
                    _("redirect_uri_match_mode='ha_callback' relaxes the "
                      "redirect_uri host check and depends on PKCE to bound the "
                      "open-redirect surface. Enable 'Require PKCE'.")
                )

    def set_secret(self, plain_secret: str) -> None:
        """Hash and store a client secret. Rejects public clients."""
        self.ensure_one()
        if self.client_type == 'public':
            raise ValidationError(
                _("Cannot set a client_secret on a public client.")
            )
        self.client_secret_hash = generate_password_hash(plain_secret)

    def verify_secret(self, plain_secret: str) -> bool:
        """Verify a plain-text secret against the stored hash.

        Args:
            plain_secret: The plain-text secret to verify.

        Returns:
            True if the secret matches, False otherwise.
        """
        self.ensure_one()
        if not self.client_secret_hash:
            return False
        return check_password_hash(self.client_secret_hash, plain_secret)

    def check_redirect_uri(self, uri: str) -> bool:
        """Check if a redirect URI is acceptable for this client.

        exact mode: verbatim membership in redirect_uris.
        ha_callback mode: host-flexible pattern (see _check_ha_callback_uri).

        Security invariant: this method is the ONLY redirect_uri whitelist
        gate. The token endpoint does a redirect_uri *consistency* check
        against the stored auth_code value (oauth2.py:486), not a whitelist
        check — so relaxing here propagates to both endpoints automatically.
        Do NOT normalize/rewrite redirect_uri between authorize and token, or
        that equality check will break.
        """
        self.ensure_one()
        if self.redirect_uri_match_mode == 'ha_callback':
            return self._check_ha_callback_uri(uri)
        if not self.redirect_uris:
            return False
        registered = [
            u.strip() for u in self.redirect_uris.strip().splitlines() if u.strip()
        ]
        return uri in registered

    def _check_ha_callback_uri(self, uri: str) -> bool:
        """Host-flexible redirect_uri check for the HA custom component.

        Accepts any host as long as: scheme in {http,https}; path is exactly
        HA_CALLBACK_PATH; no query/fragment/userinfo; and http is used only
        for loopback / RFC1918 private / link-local / .local hosts (public
        hosts must use https, RFC 8252). The relaxed host surface is bounded
        by PKCE + single-use codes (see this client's require_pkce constraint).
        """
        if not uri:
            return False
        # urlsplit silently strips \t\r\n before parsing, which would let the
        # validated host diverge from the stored / browser-seen literal (the
        # token-endpoint equality check at oauth2.py:486 trusts that literal).
        # Fail closed on raw control chars / surrounding whitespace BEFORE
        # urlsplit — defends against the parser-confusion class.
        if uri != uri.strip() or any(c in uri for c in '\t\r\n'):
            return False
        try:
            parts = urlsplit(uri)
        except ValueError:
            return False
        if parts.scheme not in ('http', 'https'):
            return False
        if parts.path != self.HA_CALLBACK_PATH:
            return False
        if parts.query or parts.fragment:
            return False
        if parts.username or parts.password:
            return False
        try:
            host = parts.hostname
        except ValueError:
            return False
        if not host:
            return False
        if parts.scheme == 'http' and not self._is_local_host(host):
            return False
        return True

    @staticmethod
    def _is_local_host(host: str) -> bool:
        """True for loopback / RFC1918 private / link-local / .local hosts.

        Decides whether plain http is acceptable. Public hostnames (incl.
        Nabu Casa *.ui.nabu.casa) return False -> https required.

        Notes:
        - mDNS: requires a non-empty label before '.local' (bare '.local'
          rejected); '.localhost' subdomains accepted (loopback convention).
        - IPv4-mapped IPv6 (::ffff:x.x.x.x) is unwrapped so it classifies like
          the embedded IPv4 (predictable, no http-downgrade surprise).
        - ipaddress.is_private also covers reserved/doc ranges (TEST-NET,
          class E); allowing http there is harmless (not attacker-routable),
          so we keep the classifier simple rather than over-engineer it.
        """
        h = (host or '').lower().rstrip('.')  # normalize FQDN trailing dot
        if not h:
            return False
        if h == 'localhost' or h.endswith('.localhost'):
            return True
        if h != '.local' and h.endswith('.local'):
            return True
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            return False  # a real domain name -> public
        mapped = getattr(ip, 'ipv4_mapped', None)
        if mapped is not None:
            ip = mapped  # treat ::ffff:x.x.x.x like the IPv4 it embeds
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def check_grant_type(self, grant_type: str) -> bool:
        """Check if a grant type is allowed for this client.

        Args:
            grant_type: The grant type to check.

        Returns:
            True if allowed, False otherwise.
        """
        self.ensure_one()
        if not self.grant_types:
            return False
        allowed = [g.strip() for g in self.grant_types.split(',') if g.strip()]
        return grant_type in allowed

    def get_allowed_scopes(self) -> set[str]:
        """Return the set of scopes this client is allowed to request."""
        self.ensure_one()
        if not self.scopes:
            return set()
        return {s.strip() for s in self.scopes.split() if s.strip()}

    @api.model
    def _ensure_ha_smart_home_sa_scopes(self) -> None:
        """Idempotently grant Security Access scopes to the HA public client.

        ``oauth_clients.xml`` is ``noupdate="1"`` so editing the seed XML
        never reaches existing databases. This method merges the Security
        Access scopes into the seeded HA client and is invoked from two
        places that together cover both install and upgrade:

        - ``hooks.py:post_init_hook`` — fresh install path.
        - ``data/oauth_scope_updates.xml`` ``<function>`` — runs on every
          ``-u`` (upgrade path; noupdate-independent).

        Missing client scopes break OAuth token refresh for the HA
        component (G-H3), so this must run idempotently on upgrades.
        """
        required = ('security-access:read', 'security-access:tunnel')
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home',
            raise_if_not_found=False,
        )
        if not client:
            return
        granted = set((client.scopes or '').split())
        missing = [s for s in required if s not in granted]
        if missing:
            client.scopes = ' '.join(sorted(granted | set(missing)))

    @api.model
    def _ensure_ha_app_provision_scope(self) -> None:
        """冪等把 ha:provision 併進 seed 的 HA App public client（#336）。

        比照 _ensure_ha_smart_home_sa_scopes：oauth_clients.xml 是 noupdate=1，
        改 seed XML 到不了既有 DB。本 method 從 post_init（新裝）與
        data/oauth_ha_provision_scope_update.xml <function>（每次 -u）兩處呼叫，
        覆蓋新裝與升級兩條路徑。缺 client 時 no-op（fail-safe）。
        """
        required = ('ha:provision',)
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_app',
            raise_if_not_found=False,
        )
        if not client:
            return
        granted = set((client.scopes or '').split())
        missing = [s for s in required if s not in granted]
        if missing:
            client.scopes = ' '.join(sorted(granted | set(missing)))

    @api.model
    def _ensure_ha_smart_home_redirect_mode(self) -> None:
        """Idempotently migrate the seeded HA client to host-flexible callback
        matching. ``oauth_clients.xml`` is ``noupdate="1"`` (sticky), so the
        seed change to redirect_uri_match_mode / redirect_uris never reaches
        existing databases. This healer is wired into both:

        - ``hooks.py:post_init_hook`` — fresh install path.
        - ``data/oauth_redirect_mode_update.xml`` ``<function>`` — every -u.

        Removes the legacy ``my.home-assistant.io`` value (route one drops the
        relay entirely; not a superset). require_pkce is already True on this
        seeded client, so the ha_callback PKCE invariant holds.

        NB: in ha_callback mode ``redirect_uris`` does NOT participate in host
        matching (validation uses HA_CALLBACK_PATH + scheme/host class), so
        overwriting it to the path constant is intentional and loses nothing.
        To allow extra hosts, change _is_local_host — not redirect_uris.
        """
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home',
            raise_if_not_found=False,
        )
        if not client:
            return
        target = self.HA_CALLBACK_PATH
        if (client.redirect_uri_match_mode != 'ha_callback'
                or client.redirect_uris != target):
            client.write({
                'redirect_uri_match_mode': 'ha_callback',
                'redirect_uris': target,
            })

    @api.model
    def _ensure_ha_smart_home_device_grant(self) -> None:
        """Idempotently add the RFC 8628 device_code grant to the seeded HA
        client (hybrid: browser uses authorization_code, app uses device_code).
        oauth_clients.xml is noupdate=1 so the seed change never reaches
        existing DBs; this healer covers the upgrade path. Wired into
        post_init_hook + data/oauth_device_grant_update.xml <function>."""
        device_grant = 'urn:ietf:params:oauth:grant-type:device_code'
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home',
            raise_if_not_found=False,
        )
        if not client:
            return
        grants = [g.strip() for g in (client.grant_types or '').split(',') if g.strip()]
        if device_grant not in grants:
            grants.append(device_grant)
            client.grant_types = ','.join(grants)

    @api.model
    def generate_client_credentials(self) -> dict[str, str]:
        """Generate a new client_id and client_secret pair.

        Returns:
            dict with 'client_id' and 'client_secret' (plain text).
        """
        return {
            'client_id': secrets.token_urlsafe(32),
            'client_secret': secrets.token_urlsafe(48),
        }

    def action_regenerate_secret(self) -> dict:
        """Regenerate the client secret and surface it once. Rejects public clients."""
        self.ensure_one()
        if self.client_type == 'public':
            raise ValidationError(
                _("Public clients have no secret to regenerate.")
            )
        new_secret = secrets.token_urlsafe(48)
        self.set_secret(new_secret)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'New Client Secret Generated',
                'message': f'New secret (copy now, it will not be shown again): {new_secret}',
                'type': 'warning',
                'sticky': True,
            },
        }
