import logging
import re
import uuid

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.naming import make_security_access_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)

# A subdomain prefix is a SINGLE DNS label: 3-63 chars of lowercase letters,
# digits and hyphens only — NO dots (a dot would split it into multiple labels)
# and no other symbols/uppercase/spaces; must not start or end with a hyphen.
_PREFIX_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')


class SecurityAccess(models.Model):
    _name = 'woow_paas_platform.security_access'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Security Access Instance'
    _order = 'create_date desc'

    # Identity
    name = fields.Char(
        string='Name',
        required=True,
        help='User-defined nickname for this security access (e.g. '
             'secure-gateway). The Cloudflare-facing identity is '
             'canonical_name, which never changes.',
    )
    reference_id = fields.Char(
        string='Reference ID',
        required=True,
        copy=False,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help='Unique reference ID for hash-based naming. Immutable.',
    )
    canonical_name = fields.Char(
        string='Canonical Name',
        readonly=True,
        copy=False,
        index=True,
        help='paas-sa-{ws_hash}-{res_hash}. Computed ONCE at create and '
             'stored — the naming hash salts on the (mutable) nickname, so '
             'recomputing would drift the subdomain and break routes. Used '
             'as tunnel name and stamped on every Cloudflare object for '
             'traceability.',
    )
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this security access belongs to',
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_template',
        string='Template',
        help='Catalog template this instance was created from',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('provisioning', 'Provisioning'),
            ('active', 'Active'),
            # D1: tunnel provisioned but has zero application routes — reachable
            # at nothing yet (catch-all 404). Distinct from 'active' so monitoring
            # can tell a routed tunnel from an empty one.
            ('active_no_route', 'Active (No Route)'),
            ('error', 'Error'),
            ('suspended', 'Suspended'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        required=True,
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if provisioning failed',
    )

    # Tunnel info
    tunnel_id = fields.Char(string='Tunnel ID')
    tunnel_token = fields.Char(
        string='Tunnel Token',
        groups='base.group_system',
    )
    tunnel_name = fields.Char(string='Tunnel Name')
    connector_id = fields.Char(string='Connector ID')
    connector_type = fields.Char(string='Connector Type')
    tunnel_status = fields.Selection(
        selection=[
            ('connected', 'Connected'),
            ('disconnected', 'Disconnected'),
            ('error', 'Error'),
        ],
        string='Tunnel Status',
        default='disconnected',
    )
    tunnel_uptime = fields.Char(
        string='Connector Opened At',
        help="Connector connection open time (Cloudflare opened_at ISO "
             "timestamp), NOT a pre-computed duration. Consumers derive the "
             "uptime as now() - opened_at.",
    )
    connector_version = fields.Char(
        string='Connector Version',
        help="cloudflared client version reported by the active connector "
             "(from the operator tunnel-status connections payload).",
    )
    edge_connection_count = fields.Integer(
        string='Edge Connections',
        default=0,
        help="Number of active edge connections the connector holds "
             "(len of the operator tunnel-status connections list).",
    )

    # NOTE: there is deliberately NO SA-level "primary_hostname" field, and no
    # auto-created primary route. A Security Access IS a bare Cloudflare Tunnel
    # with N user-managed application routes (each forwarded to the user's
    # reverse proxy); Cloudflare Tunnel has no primary/default hostname concept.
    # canonical_name is only the tunnel's CF identity (its name), never surfaced
    # as "the hostname"; a freshly-provisioned tunnel has zero routes and zero
    # DNS until the user adds a route.

    # Access Control（P2.5；下放到 route 子模型 per-route）
    #
    # P2.5 removed the SA-LEVEL service-wide Access fields (access_enabled /
    # login_google / login_email_otp / session_duration / access_app_id /
    # access_unresolved_idp_types / access_policy_ids). Access is now per-route:
    # each Application Route is its own Cloudflare Access application with its
    # own protection toggle (route.is_protected), login methods, session and
    # policy set. See SecurityAccessRoute below. The only SA-level Access helper
    # that survives is _teardown_route_access (tears down a deleted route's app).

    route_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_route',
        inverse_name='security_access_id',
        string='Application Routes',
    )
    route_count = fields.Integer(
        compute='_compute_route_count', string='Routes',
    )

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    # ------------------------------------------------------------------ #
    #  Computes / helpers                                                #
    # ------------------------------------------------------------------ #

    @api.depends('route_ids')
    def _compute_route_count(self):
        for record in self:
            record.route_count = len(record.route_ids)

    def _recompute_route_state(self):
        """D1: flip a provisioned tunnel between 'active' (has ≥1 route) and
        'active_no_route' (bare). Only touches those two states — never
        pending/provisioning/error/suspended/deleting. Called from the route
        sub-model's create()/unlink() so adding the first route activates the
        SA and deleting the last one drops it back to no-route."""
        for sa in self:
            if sa.state in ('active', 'active_no_route'):
                target = 'active' if sa.route_ids else 'active_no_route'
                if sa.state != target:
                    sa.state = target

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            )
        return client

    def _get_paas_domain(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io'
        )

    @api.model
    def _get_default_service_url(self):
        """後台可設定的預設反向代理 URL（spec §6）。"""
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.sa_default_service_url', 'http://localhost:80'
        )

    def _generate_canonical_name(self):
        self.ensure_one()
        workspace = self.workspace_id
        slug = (workspace.slug if hasattr(workspace, 'slug') and workspace.slug
                else workspace.name.lower().replace(' ', '-'))
        return make_security_access_subdomain(slug, self.reference_id, self.name)

    @api.model_create_multi
    def create(self, vals_list):
        # Identity anchors must not be attacker-injectable at create time.
        # The write() guard only covers writes; super().create() does not run
        # through write() nor pop these keys, so a workspace member calling ORM
        # create directly could squat a victim-namespace canonical_name
        # (decides tunnel name, primary subdomain_prefix, DNS comment stamp and
        # the public FQDN). Strip both keys from caller vals — for EVERYONE
        # incl. superuser, gated only by the same context escape hatch as the
        # write() guard (no env.su exemption, else su-env tests false-green).
        # canonical_name is then computed once below; reference_id falls back
        # to its uuid4 default. Closes the create+write immutable chain.
        if not self.env.context.get('sa_internal_identity_write'):
            for vals in vals_list:
                vals.pop('canonical_name', None)
                vals.pop('reference_id', None)
        records = super().create(vals_list)
        for record in records:
            if not record.canonical_name:
                # compute ONCE and store; never recompute (name is mutable).
                # The write() guard below blocks canonical_name writes for
                # everyone (incl. superuser) — this initial assignment is the
                # one legitimate internal write, opted in via the context
                # escape hatch (not an env.su exemption: see write() comment).
                record.with_context(
                    sa_internal_identity_write=True,
                ).write({
                    'canonical_name': record._generate_canonical_name(),
                })
        return records

    def write(self, vals):
        # canonical_name / reference_id are identity anchors — subdomain &
        # tunnel naming derive from them; drift would break routing. Hard
        # invariant for EVERYONE including superuser: an env.su exemption would
        # silently skip this guard in TransactionCase (su env) and any sudo()'d
        # path, giving false-green tests (same trap the route prefix guard
        # documents). The context key is the single deliberate escape hatch —
        # only the create-time initial assignment sets it.
        if not self.env.context.get('sa_internal_identity_write'):
            for forbidden in ('canonical_name', 'reference_id'):
                if forbidden in vals:
                    raise UserError(
                        "Field '%s' is immutable after creation." % forbidden
                    )
        # P2.5: the SA layer no longer carries any service-wide Access config —
        # Access is per-route now, so a route's create/write/unlink fires its
        # OWN _sync_access_to_operator. There is no SA-level access write trigger
        # any more (the route/policy sub-models own the savepoint + liveness
        # gate). A plain write here only persists SA identity/tunnel/state fields.
        result = super().write(vals)
        return result

    # ------------------------------------------------------------------ #
    #  Provision / status / delete                                       #
    # ------------------------------------------------------------------ #

    def action_provision(self):
        """Provision: create dedicated tunnel + primary route.

        Idempotency guard: this is a two-stage provision (create_tunnel, then
        a re-sync that carries http_host_header). If stage 2 fails the record
        lands in 'error' but the tunnel already exists in Cloudflare and
        tunnel_id is stored. A retry from the 'error' state must NOT call
        create_tunnel again — that would leave the first tunnel orphaned in CF
        (live token, billed, no DB reference). When tunnel_id is already set we
        only re-run the declarative route sync and restore the active state.

        tunnel_token ACL note: the write below sets tunnel_token, which is a
        ``groups='base.group_system'`` field. This method runs in the CALLER's
        context (the controller invokes it WITHOUT .sudo()), mirroring
        smart_home.action_provision exactly. Consequences:
        - the field-group write only succeeds for a system/admin caller; a
          plain base.group_user owner triggering provision would hit
          AccessError on the tunnel_token write. That is the intended
          fail-closed behaviour — the token must never be written (or read)
          by a non-system principal.
        - do NOT "fix" a portal AccessError here by wrapping the whole method
          in .sudo(): that would widen the token write/read surface to any
          workspace owner and defeat the groups= field mask. The defence is
          deliberately layered: groups= on the field + to_dict() omits the
          token + a dedicated sudo'd tunnel-token endpoint is the only read
          path. The with_user provision test locks this semantic.
        """
        self.ensure_one()
        client = self._get_operator_client()

        # Already has a live tunnel (e.g. retry after a stage-2 sync failure):
        # never recreate it, just finish the route sync.
        if self.tunnel_id:
            self.write({'state': 'provisioning', 'error_message': False})
            try:
                # 軸2: bare tunnel — no primary route to self-heal. Just re-run
                # the declarative route sync (zero routes -> empty sync, tunnel
                # keeps catch-all 404) + per-route Access for any user routes.
                self._sync_routes_to_operator()
                # P2.5: if any route was set is_protected (+ policies) on the SA
                # while it was still pending, the route Access sync never reached
                # the operator (no live tunnel then). The tunnel exists now, so
                # push every protected route's own Access app here — otherwise a
                # route marked protected stays silently inert (no CF Access app)
                # until some route access field is re-toggled.
                self._sync_all_routes_access()
                self.write({
                    'state': 'active' if self.route_ids else 'active_no_route',
                })
            except PaaSOperatorConnectionError as e:
                self.write({
                    'state': 'error',
                    'error_message':
                        f"Cannot connect to PaaS Operator: {e.message}",
                })
                _logger.error(
                    "[SA] provision re-sync connection error sa=%s: %s",
                    self.id, e.message,
                )
            except PaaSOperatorError as e:
                self.write({
                    'state': 'error',
                    'error_message': f"Route sync failed: {e.message}",
                })
                _logger.error(
                    "[SA] provision re-sync error sa=%s: %s",
                    self.id, e.message,
                )
            except AccessError:
                # A field-group (tunnel_token) AccessError is the deliberate
                # fail-closed signal for a non-system caller — it must propagate
                # to the caller, NOT be downgraded to a silent state='error'
                # (which would mask the security denial and report a benign
                # provisioning failure instead). Re-raise; see the tunnel_token
                # ACL note in the method docstring. Symmetric with the
                # first-provision branch below.
                raise
            except Exception as e:
                # Any non-operator failure (route rebuild ValidationError,
                # DB error, …) must also fail loud into 'error' — tunnel_id
                # is retained so the next retry can finish (symmetric with the
                # broad except in the first-provision branch below).
                self.write({
                    'state': 'error',
                    'error_message': f"Provision re-sync failed: {e}",
                })
                _logger.exception(
                    "[SA] provision re-sync unexpected error sa=%s", self.id,
                )
            return

        self.write({'state': 'provisioning', 'error_message': False})

        try:
            default_url = self._get_default_service_url()

            # 軸2: build a BARE tunnel — no hostname, no auto-created primary
            # route, no DNS. canonical_name is only the tunnel identity (its CF
            # name); application routes (and their DNS) are added on demand by
            # the user afterwards. create_tunnel with no hostname configures
            # the tunnel with a catch-all 404 ingress only.
            result = client.create_tunnel(
                name=self.canonical_name,
                service_url=default_url,
            )

            self.write({
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'deployed_at': fields.Datetime.now(),
            })

            # Push whatever routes already exist — a user may have added routes
            # on the still-pending SA via the configure-then-provision flow.
            # Zero routes -> empty declarative sync -> tunnel keeps the
            # catch-all 404 only (the operator accepts an empty route list).
            self._sync_routes_to_operator()

            # P2.5: push per-route Access too if any route was set protected
            # pre-provision. The configure-then-provision flow lets a user mark
            # a route protected (+ add policies) on a 'pending' SA (BFF gates on
            # subscription state, not provisioning state); those route syncs
            # never reached the operator (no live tunnel). Now that the tunnel
            # exists, converge each protected route's CF Access app.
            self._sync_all_routes_access()

            # D1: a tunnel with zero application routes is reachable at nothing
            # yet -> distinct 'active_no_route' state; once it has a route it is
            # 'active'.
            self.write({
                'state': 'active' if self.route_ids else 'active_no_route',
            })

            _logger.info(
                "[SA] provisioned sa=%s canonical=%s tunnel=%s",
                self.id, self.canonical_name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': f"Cannot connect to PaaS Operator: {e.message}",
            })
            _logger.error("[SA] provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': f"Tunnel creation failed: {e.message}",
            })
            _logger.error("[SA] provision error: %s", e.message)
        except AccessError:
            # The stage-2 tunnel_token write is a groups='base.group_system'
            # field write; for a non-system caller it raises AccessError. That
            # is the deliberate fail-closed defense (see the tunnel_token ACL
            # note in the docstring) — a non-system principal must never write
            # the token. Re-raise so the denial surfaces to the caller; do NOT
            # downgrade it to a silent state='error' (which would mask the
            # security boundary as an ordinary provisioning failure and let the
            # endpoint claim a benign error). The with_user provision test locks
            # this. (create_tunnel already ran here, but the controller never
            # drives provision as a bare portal user — system/admin context or a
            # narrowly-sudo'd path is the only legitimate caller.)
            raise
        except Exception as e:
            # Stage-2 (write / primary-route create / re-sync) can raise
            # non-operator exceptions: a ValidationError from
            # _check_prefix_format, a DB IntegrityError, or a UNIQUE clash on
            # the primary route. The narrow operator-only excepts above would
            # let those propagate while state was already flipped to 'active'
            # and tunnel_id stored — silent CF↔DB drift. Catch everything and
            # fail loud into 'error' (tunnel_id is retained, so the idempotent
            # retry-from-error path can finish without orphaning the tunnel),
            # mirroring _do_suspend / _do_resume / _do_cleanup's bare except.
            self.write({
                'state': 'error',
                'error_message': f"Provisioning failed: {e}",
            })
            _logger.exception("[SA] provision unexpected error sa=%s", self.id)

    def action_refresh_status(self):
        """Refresh tunnel connector status from the PaaS Operator."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        try:
            result = client.get_tunnel_status(self.tunnel_id)
            connections = result.get('connections', [])
            update_vals = {
                'tunnel_status': 'connected' if connections else 'disconnected',
                'edge_connection_count': len(connections),
            }
            if connections:
                conn = connections[0]
                update_vals['connector_id'] = conn.get('connector_id', '')
                update_vals['connector_type'] = conn.get('type', '')
                # cloudflared client version (key varies by operator/CF payload)
                update_vals['connector_version'] = (
                    conn.get('client_version')
                    or conn.get('version') or '')
                if conn.get('opened_at'):
                    update_vals['tunnel_uptime'] = conn['opened_at']
            else:
                update_vals['connector_version'] = ''
            self.write(update_vals)
        except PaaSOperatorError as e:
            self.write({
                'tunnel_status': 'error',
                'error_message': f"Status refresh failed: {e.message}",
            })
            _logger.warning(
                "[SA] status refresh failed sa=%s: %s", self.id, e.message,
            )

    def action_delete(self):
        """User-initiated delete: full Cloudflare teardown then unlink."""
        self.ensure_one()
        self.write({'state': 'deleting'})
        self._cleanup_cloudflare()
        self.unlink()

    def unlink(self):
        """Direct-unlink guard: Cloudflare resources must never outlive the
        record (cloud-service-unlink-override-cleanup pattern).

        Fail-loud if cleanup left a live tunnel. _cleanup_cloudflare swallows
        operator/CF errors and deliberately keeps tunnel_id for a retry; if we
        unconditionally unlinked we would drop the DB row while a billed,
        token-still-valid tunnel lives on in Cloudflare with no DB reference
        left to drive a retry — a permanent orphan. Raise instead so the
        controller surfaces a truthful failure and both the DB row and the CF
        tunnel survive for the user / cron _do_cleanup to retry. This aligns
        the direct-delete path with the fail-loud contract _do_cleanup already
        establishes for the cron path."""
        for record in self:
            if record.tunnel_id:
                record._cleanup_cloudflare()
            if record.tunnel_id:
                raise UserError(
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. The record was kept so the deletion can be "
                    "retried. Please try again shortly."
                )
        # 軸2: routes are freely deletable (no primary guard), so cascade-delete
        # them directly as part of tearing down the whole SA.
        self.route_ids.unlink()
        return super().unlink()

    # ------------------------------------------------------------------ #
    #  Route sync (declarative)                                          #
    # ------------------------------------------------------------------ #

    def _build_route_payload(self, override_service_url=None):
        """Desired-state payload for sync_tunnel_routes.

        Args:
            override_service_url: when set (e.g. 'http_status:503'),
                every route's service is replaced — used by suspend.
        """
        self.ensure_one()
        payload = []
        for route in self.route_ids.sorted(
                key=lambda r: (not r.is_primary, r.id)):
            payload.append({
                'subdomain': route.subdomain_prefix,
                'service_url': override_service_url or route.service_url,
                'http_host_header': (
                    route.http_host_header and not override_service_url
                ),
            })
        return payload

    def _sync_routes_to_operator(self, override_service_url=None):
        """Push the full desired route list to the operator (idempotent)."""
        self.ensure_one()
        if not self.tunnel_id:
            return
        client = self._get_operator_client()
        result = client.sync_tunnel_routes(
            self.tunnel_id,
            routes=self._build_route_payload(override_service_url),
            comment=self.canonical_name,
        )
        # store DNS record ids back on routes
        dns_by_sub = {
            r['subdomain']: r.get('dns_record_id')
            for r in result.get('routes', [])
        }
        for route in self.route_ids:
            rec_id = dns_by_sub.get(route.subdomain_prefix)
            if rec_id and route.dns_record_id != rec_id:
                route.dns_record_id = rec_id

    # ------------------------------------------------------------------ #
    #  Access sync (declarative; P2.5 per-route — see SecurityAccessRoute) #
    # ------------------------------------------------------------------ #

    def _sync_all_routes_access(self):
        """P2.5 convergence point: push every protected route's own Access app.

        Called once the tunnel is live (provision / resume) so per-route Access
        that was configured on a 'pending' SA — when no route sync could reach
        the operator — gets pushed, AND so a route disabled while suspended is
        torn down at resume. The route sync still requires a live tunnel; the
        sa_force_access_sync context bypasses only the state half of the gate
        because these convergence points run before the SA state is settled to
        'active'/'error'. Each route builds its own single-route payload, so an
        unprotected route with no stored app id is a cheap no-op."""
        self.ensure_one()
        for route in self.route_ids:
            route.with_context(sa_force_access_sync=True)._sync_access_to_operator()

    def _teardown_route_access(self, app_id, policy_ids, app_name, hostname):
        """Tear down ONE route's Cloudflare Access app (+ reusable policies)
        when the route row no longer exists (deleted) or during SA cleanup. The
        route may be gone, so the caller passes the captured ids; we build a
        minimal enabled=False payload here. Best-effort — a teardown failure is
        logged, not raised, so it does not abort the surrounding unlink/cleanup.
        Returns True on a clean teardown (so the caller may clear the stored
        app_id), False on failure (so a surviving route keeps its id and the SA
        lands re-entrant in 'error' instead of reporting a false clean)."""
        self.ensure_one()
        if not app_id and not policy_ids:
            return True
        try:
            client = self._get_operator_client()
            result = client.sync_access_app({
                'enabled': False,
                'canonical_name': self.canonical_name,
                'app_name': app_name or self.canonical_name,
                'primary_hostname': hostname or '',
                'protected_hostnames': [],
                'session_duration': '24h',
                'login_idp_types': [],
                'policies': [],
                'existing_app_id': app_id or None,
                'existing_policy_ids': policy_ids or [],
            })
            orphaned = result.get('orphaned_policy_ids') or []
            if orphaned:
                _logger.warning(
                    "[SA] route access teardown sa=%s app=%s left %d orphan "
                    "policy(ies): %s", self.id, app_id, len(orphaned),
                    ', '.join(orphaned))
            return True
        except Exception as e:
            _logger.warning(
                "[SA] route access teardown failed sa=%s app=%s: %s",
                self.id, app_id, e)
            return False

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """503-freeze every route's ingress; keep DNS / tunnel / token."""
        self.ensure_one()
        _logger.info(
            "[SA] _do_suspend sa=%s state=%s sub=%s",
            self.id, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        # 'deleting' is terminal-ish (cleanup already ran): never flip it back
        # to 'suspended' — that reverse transition makes a cleaned-up record
        # re-advertise as merely suspended and triggers needless re-cleanup
        # churn on every cron pass while the subscription stays cancelled.
        if self.state in ('suspended', 'deleting'):
            return
        if self.tunnel_id:
            try:
                self._sync_routes_to_operator(
                    override_service_url='http_status:503')
            except Exception as e:
                _logger.warning("[SA] _do_suspend failed sa=%s: %s", self.id, e)
                self.write({'state': 'error'})
                return
        self.write({
            'pre_suspend_state': (
                self.state if self.state in ('active', 'active_no_route', 'error') else 'active'
            ),
            'state': 'suspended',
        })
        _logger.info("[SA] _do_suspend done sa=%s", self.id)

    def _do_resume(self):
        """Restore every route's ingress to its stored service_url."""
        self.ensure_one()
        _logger.info(
            "[SA] _do_resume sa=%s state=%s pre_suspend=%s",
            self.id, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return
        if not self.tunnel_id:
            # Never successfully provisioned (suspended while pending/
            # provisioning/error before a tunnel existed). pre_suspend_state
            # was coerced to 'active' by _do_suspend's fallback, which would
            # mislead the UI into showing "running" with no tunnel. Restore to
            # 'pending' so the user is prompted to (re-)provision.
            self.write({'state': 'pending', 'pre_suspend_state': False})
            _logger.info(
                "[SA] _do_resume sa=%s no tunnel → restored to 'pending'",
                self.id,
            )
            return
        try:
            self._sync_routes_to_operator()
            # P2.5: re-assert per-route Access on resume too. Suspend only
            # 503-freezes route ingress (it does not tear Access apps down), but
            # resuming is also the convergence point for any per-route Access
            # config edited while suspended or set pre-provision — push every
            # route's own desired state so CF matches the live route set.
            #
            # Converge BOTH directions per route: _sync_access_to_operator is
            # idempotent. A route flipped is_protected→False while suspended (no
            # write trigger fired — state not in ('active','error')) still
            # carries a stored access_app_id, so its resume sync sends an
            # enabled=False teardown payload; a route still protected re-asserts
            # its app. A live tunnel is guaranteed here (the `not tunnel_id`
            # branch above returned), so push unconditionally.
            self._sync_all_routes_access()
        except Exception as e:
            _logger.warning("[SA] _do_resume failed sa=%s: %s", self.id, e)
            self.write({'state': 'error'})
            return
        restore_state = self.pre_suspend_state or 'active'
        resume_vals = {'pre_suspend_state': False}
        if restore_state == 'error':
            # The re-sync above just succeeded, so the tunnel/ingress is
            # healthy right now — restoring the stale pre-suspend 'error'
            # would leave a working service permanently flagged 'error' (cron
            # branch 2 only resumes 'suspended', so it never gets revisited)
            # with a misleading old error_message. Heal to 'active' instead.
            resume_vals['state'] = 'active'
            resume_vals['error_message'] = False
            restore_state = 'active'
        else:
            resume_vals['state'] = restore_state
        self.write(resume_vals)
        _logger.info("[SA] _do_resume done sa=%s state=%s",
                     self.id, restore_state)

    def _do_cleanup(self):
        """Cron-driven permanent removal of Cloudflare resources.

        Only mark 'deleting' once the tunnel is actually gone. If
        _cleanup_cloudflare failed it deliberately keeps tunnel_id (re-entrant
        retry); writing 'deleting' then would be a misleading success signal
        that hides a still-live CF tunnel and lets ops mistake "clean" for
        "clean failed". Land in 'error' + error_message instead, fail-loud and
        observable like _do_suspend / _do_resume."""
        self.ensure_one()
        _logger.info("[SA] _do_cleanup sa=%s tunnel=%s",
                     self.id, self.tunnel_id)
        self._cleanup_cloudflare()
        # P2.5: a lingering per-route Access app is a stale route.access_app_id.
        lingering_access = self.route_ids.filtered('access_app_id')
        if self.tunnel_id or lingering_access:
            # cleanup did not finish — either the tunnel is still live in
            # Cloudflare, OR one of the per-route Access teardowns failed while
            # the tunnel delete succeeded (ordering-dependent orphan: a stale
            # route.access_app_id means that route's CF Access app + reusable
            # policies still exist). Either way, writing 'deleting' would be a
            # misleading success signal that hides a lingering CF resource and
            # lets ops mistake "clean" for "clean failed". Land in 'error' +
            # error_message instead, fail-loud and re-entrant: the next cron
            # pass re-runs _cleanup_cloudflare, which retries whichever leg is
            # still outstanding.
            self.write({
                'state': 'error',
                'error_message': "Cloudflare cleanup failed; %s still live. "
                                 "Will retry on the next cron pass." % (
                                     'tunnel' if self.tunnel_id
                                     else 'Access app'),
            })
            _logger.warning(
                "[SA] _do_cleanup incomplete sa=%s tunnel=%s lingering_access=%s "
                "(state=error)",
                self.id, self.tunnel_id, lingering_access.ids,
            )
            return
        self.write({'state': 'deleting'})

    def _cleanup_cloudflare(self):
        """Idempotent CF teardown. delete_tunnel cascades: operator removes
        every DNS record pointing at the tunnel, then deletes the tunnel
        (token invalidated)."""
        self.ensure_one()
        access_routes = self.route_ids.filtered('access_app_id')
        if not self.tunnel_id and not access_routes:
            return
        # P2.5: tear down each route's own Cloudflare Access app (+ reusable
        # policies) first. Per-route now — loop every route that still carries an
        # access_app_id and push its own enabled=False teardown. Each is wrapped
        # (best-effort inside _route_teardown via _teardown_route_access) so one
        # Access teardown failure does NOT abort the tunnel delete below — the
        # tunnel is the billed resource and must always get its delete attempt.
        # The teardown only needs existing_app_id + existing_policy_ids, NOT the
        # tunnel, so we still attempt it even when tunnel_id is already gone — a
        # prior partial cleanup (tunnel deleted, an Access teardown failed) would
        # otherwise leave that route's Access app/policies as permanent orphans
        # with no retry path. A route's access_app_id is cleared only on a
        # successful teardown, so a failure keeps it set and _do_cleanup lands
        # the SA in 'error' (re-entrant) instead of reporting a false clean.
        for route in access_routes:
            pol_ids = [p.cf_policy_id for p in route.access_policy_ids
                       if p.cf_policy_id]
            ok = self._teardown_route_access(
                app_id=route.access_app_id,
                policy_ids=pol_ids,
                app_name=route.subdomain_prefix,
                hostname=route.hostname,
            )
            if ok:
                route.access_app_id = False
                for pol in route.access_policy_ids:
                    if pol.cf_policy_id:
                        pol.cf_policy_id = False
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            client.delete_tunnel(self.tunnel_id)
            _logger.info("[SA] tunnel deleted sa=%s tunnel=%s",
                         self.id, self.tunnel_id)
            self.write({'tunnel_id': False})
        except Exception as e:
            # cleanup is re-entrant: log and keep tunnel_id so a later
            # retry (cron or manual) can finish the job.
            _logger.warning("[SA] cleanup failed sa=%s: %s", self.id, e)

    # ------------------------------------------------------------------ #
    #  Serialization                                                      #
    # ------------------------------------------------------------------ #

    def to_dict(self):
        """Serialize for API responses."""
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'canonical_name': self.canonical_name or '',
            'workspace_id': self.workspace_id.id,
            'state': self.state,
            'tunnel_id': self.tunnel_id or '',
            'tunnel_name': self.tunnel_name or '',
            'tunnel_status': self.tunnel_status or 'disconnected',
            'tunnel_uptime': self.tunnel_uptime or '',
            'connector_id': self.connector_id or '',
            'connector_type': self.connector_type or '',
            'connector_version': self.connector_version or '',
            'edge_connection_count': self.edge_connection_count,
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            # P2.5: Access config is per-route now. Each route's to_dict()
            # carries its own is_protected / login methods / session / policies
            # / fail-open signal. The SA layer no longer exposes service-wide
            # Access fields.
            'routes': [r.to_dict() for r in self.route_ids],
            'subscription': self._get_subscription_info(),
        }


class SecurityAccessRoute(models.Model):
    _name = 'woow_paas_platform.security_access_route'
    _description = 'Security Access Application Route'
    _order = 'is_primary desc, id'

    _sql_constraints = [
        (
            'unique_subdomain_prefix',
            'UNIQUE(subdomain_prefix)',
            'Subdomain prefix must be unique across the platform.',
        ),
    ]

    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        required=True,
        ondelete='cascade',
        index=True,
    )
    subdomain_prefix = fields.Char(
        string='Subdomain Prefix',
        required=True,
        help='User-defined prefix; hostname = {prefix}.{paas_domain}. '
             'Flat platform-wide namespace, first come first served. '
             'Immutable after create (delete + recreate to rename).',
    )
    hostname = fields.Char(
        string='Hostname',
        compute='_compute_hostname',
    )
    service_url = fields.Char(
        string='Service URL',
        required=True,
        default=lambda self: self.env[
            'woow_paas_platform.security_access']._get_default_service_url(),
        help='Backend target. Default assumes the user runs a reverse '
             'proxy (NPM) on localhost:80 that splits by Host header.',
    )
    http_host_header = fields.Boolean(
        string='Send HTTP Host Header',
        default=True,
        help='Send the public hostname as the HTTP Host header to the '
             'origin so the user-side reverse proxy can route by Host.',
    )
    is_primary = fields.Boolean(
        string='Primary Route',
        default=False,
        help='Auto-created at provision (prefix = canonical name). '
             'Cannot be deleted.',
    )
    is_protected = fields.Boolean(
        string='Protected',
        default=False,
        help='Per-route Access protection toggle (P2.5). True = this route is '
             'its own Cloudflare Access application; False = publicly reachable '
             'through the tunnel with no Access gate.',
    )
    dns_record_id = fields.Char(string='DNS Record ID')

    # ------------------------------------------------------------------ #
    #  P2.5 per-route Access config (three pieces: policies / login /    #
    #  session) + CF Access app id, all downshifted from the SA layer.   #
    # ------------------------------------------------------------------ #
    session_duration = fields.Selection(
        selection=[
            ('30m', '30 minutes'), ('1h', '1 hour'), ('8h', '8 hours'),
            ('24h', '24 hours'), ('168h', '7 days'),
        ],
        string='Session Duration',
        default='24h',
        help='How long users stay authenticated before re-login (this route).',
    )
    login_google = fields.Boolean(string='Login: Google', default=False)
    login_email_otp = fields.Boolean(string='Login: Email OTP', default=False)
    access_app_id = fields.Char(
        string='CF Access App ID', copy=False,
        help='Cloudflare Access Application id for THIS route (set on sync).',
    )
    access_policy_ids = fields.One2many(
        comodel_name='woow_paas_platform.security_access_access_policy',
        inverse_name='route_id',
        string='Access Policies',
    )
    access_unresolved_idp_types = fields.Char(
        string='Unresolved Login Methods', copy=False,
        help='Requested login methods (e.g. google) with no matching '
             'Cloudflare account IdP at the last sync of THIS route. Non-empty '
             'means the login restriction silently widened — if it emptied the '
             'resolved set, the Access app falls back to ALL available IdPs. '
             'Surfaced so the fail-open widening is visible, not dropped.',
    )

    @api.depends('subdomain_prefix')
    def _compute_hostname(self):
        domain = self.env['woow_paas_platform.security_access'
                          ]._get_paas_domain()
        for route in self:
            route.hostname = (
                f"{route.subdomain_prefix}.{domain}"
                if route.subdomain_prefix else False
            )

    @api.model
    def _subdomain_prefix_error(self, prefix):
        """Single source of truth for subdomain-prefix validation.

        Shared by the create/write constraint (`_check_prefix_format`) AND the
        availability endpoint (`controllers/security_access._check_route_prefix`)
        so the two layers never diverge — previously availability only checked
        DB/Cloudflare collision and reported a bad-format prefix as "Available",
        then create rejected it. Returns a human-readable error message, or
        None when the prefix is valid.
        """
        prefix = prefix or ''
        if not _PREFIX_RE.match(prefix):
            return (
                "Subdomain must be 3-63 chars of lowercase letters, digits and "
                "hyphens only — no dots or other symbols, and must not start or "
                "end with a hyphen."
            )
        return None

    @api.constrains('subdomain_prefix', 'is_primary')
    def _check_prefix_format(self):
        for route in self:
            err = self._subdomain_prefix_error(route.subdomain_prefix)
            if err:
                raise ValidationError("%s: %r" % (err, route.subdomain_prefix or ''))

    @api.constrains('security_access_id')
    def _check_route_quota(self):
        """Plan quota: total route count <= plan.sa_max_routes.

        軸2: every application route counts (no more free auto primary route).
        Existing subscriptions are grandfathered in the .38 migration (sa_max_routes
        +1) so they don't lose a slot when their old primary route is downgraded.

        No subscription (admin/legacy) → unrestricted, mirroring
        subscription_lifecycle_mixin._is_subscription_active. The
        controller layer re-checks (two-layer enforcement).
        """
        for route in self:
            sa = route.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_routes
            count = len(sa.route_ids)
            if count > quota:
                raise ValidationError(
                    "Route quota exceeded: plan '%s' allows %d "
                    "route(s)." % (sub.sh_subscription_plan_id.name, quota)
                )

    @api.model_create_multi
    def create(self, vals_list):
        # Atomicity scope: the savepoint makes the Odoo side atomic — a
        # create+sync where sync raises rolls the DB row back so there is no
        # "ghost route" committed in the DB but absent in CF that a later sync
        # would silently push public, and the re-raise lets the controller
        # report a truthful failure. It does NOT make the operator side
        # atomic: sync_tunnel_routes rewrites ingress then reconciles DNS with
        # no compensating action, so a mid-reconcile failure can leave CF
        # partially mutated even though the DB row rolled back. That residual
        # CF↔DB divergence converges only via a later idempotent re-sync (the
        # endpoint is declarative full-state), not via this savepoint.
        with self.env.cr.savepoint():
            routes = super().create(vals_list)
            # one sync per affected SA (declarative full-state push).
            # action_provision sets sa_skip_route_sync because it issues its
            # own explicit, http_host_header-carrying re-sync right after — so
            # this override's sync would be a redundant extra operator call.
            #
            # Sync whenever a tunnel exists, including state=='error': a
            # stage-2 provision failure lands the SA in 'error' with a live
            # tunnel_id, yet the subscription stays active so the BFF still
            # lets owners CRUD routes. Skipping the push for 'error' (sync is
            # an idempotent full-state push) would silently diverge the DB
            # from Cloudflare until someone re-provisions, while the endpoint
            # reports success — so include 'error' here (and in write/unlink).
            if not self.env.context.get('sa_skip_route_sync'):
                for sa in routes.mapped('security_access_id'):
                    if sa.state in ('active', 'active_no_route', 'error') and sa.tunnel_id:
                        sa._sync_routes_to_operator()
            # P2.5: a newly-created is_protected route is its own Cloudflare
            # Access application — push its own Access state. The route sync
            # carries its own liveness gate (no-op without a live tunnel / when
            # not protected), so just call it on every fresh route; an
            # unprotected route's sync is a cheap no-op.
            if not self.env.context.get('sa_skip_route_sync'):
                for route in routes:
                    route._sync_access_to_operator()
        # D1: first route on a bare tunnel flips it active_no_route -> active.
        routes.mapped('security_access_id')._recompute_route_state()
        return routes

    def write(self, vals):
        # Hard invariant for everyone incl. superuser: hostname / DNS /
        # ingress all derive from the prefix, and no internal path
        # legitimately renames it (delete + recreate is the rename). The
        # context key is a deliberate escape hatch for future data-repair
        # migrations only — an `env.su` exemption would silently skip this
        # guard in TransactionCase (su env) and any sudo()'d controller path.
        if 'subdomain_prefix' in vals and not self.env.context.get(
                'sa_internal_prefix_write'):
            for route in self:
                if route.subdomain_prefix != vals['subdomain_prefix']:
                    raise UserError(
                        "Subdomain prefix is immutable. Delete the route "
                        "and create a new one to rename."
                    )
        # Atomicity scope (see create): the savepoint guarantees ONLY the
        # Odoo side — on a sync failure the DB write (service_url/header) rolls
        # back so the row never claims a value the operator never accepted.
        # It does NOT roll back the operator/Cloudflare side: sync_tunnel_routes
        # rewrites ingress then reconciles DNS with no compensating action, so a
        # mid-reconcile failure (multi-route create/delete) can leave CF
        # partially changed while the DB rolls back. That residual CF↔DB
        # divergence is converged only by a later idempotent re-sync (the
        # operator endpoint is declarative full-state), not by this savepoint.
        # For a pure service_url update (hostnames unchanged) reconcile is a
        # no-op, so there the lock-step is exact.
        with self.env.cr.savepoint():
            result = super().write(vals)
            if any(k in vals for k in ('service_url', 'http_host_header')):
                for sa in self.mapped('security_access_id'):
                    if sa.state in ('active', 'active_no_route', 'error') and sa.tunnel_id:
                        sa._sync_routes_to_operator()
            # P2.5: toggling is_protected, or changing this route's own login
            # methods / session duration, changes its own Cloudflare Access app
            # — re-push THIS route's Access state. is_protected=True builds/
            # updates the app; is_protected=False tears it down. The route sync
            # carries the liveness gate (no-op without a live tunnel), so the
            # write trigger just forwards. Done per route in self.
            #
            # sa_skip_route_sync: bulk row moves (migration / provision-time)
            # set this so the write only re-homes DB state and does NOT call the
            # operator — CF converges via the explicit re-sync those flows issue,
            # not per-write here. Symmetric with create()/policy create+write+
            # unlink, which already honour the key. WITHOUT this guard the P2.5
            # migration's login/session re-home (post-migrate.py writes
            # login_google/login_email_otp/session_duration under the key) would
            # fire a live per-route CF Access app build mid-upgrade on stg's
            # active+live-tunnel SAs, defeating the deferral to Phase 5 — and an
            # unreachable operator during the upgrade window would re-raise the
            # savepoint and abort the whole migration.
            if (not self.env.context.get('sa_skip_route_sync')
                    and any(k in vals for k in (
                        'is_protected', 'login_google',
                        'login_email_otp', 'session_duration'))):
                for route in self:
                    route._sync_access_to_operator()
        return result

    def unlink(self):
        # 軸2: no primary route, so any route is deletable (incl. the last one,
        # which drops the SA back to 'active_no_route' via _recompute_route_state
        # below). The old is_primary delete guard is gone.
        affected = self.mapped('security_access_id')
        # P2.5: a protected route is its OWN Cloudflare Access app. Deleting the
        # route must tear that app (+ its reusable policies) down, but
        # super().unlink() drops the row first, taking access_app_id and every
        # policy cf_policy_id with it — after that the operator has no id to
        # delete and the app/policies leak as permanent CF orphans. Capture each
        # doomed route's teardown ids BEFORE the unlink, then push an
        # enabled=False teardown sync carrying them.
        teardown_by_route = {}
        for route in self:
            if not route.is_protected:
                continue
            sa = route.security_access_id
            if not (sa and sa.state in ('active', 'active_no_route', 'error') and sa.tunnel_id):
                continue
            pol_ids = [p.cf_policy_id for p in route.access_policy_ids
                       if p.cf_policy_id]
            teardown_by_route[route.id] = {
                'sa': sa,
                'app_id': route.access_app_id or None,
                'policy_ids': pol_ids,
                'app_name': route.subdomain_prefix,
                'hostname': route.hostname,
            }
        # Atomicity scope — the most dangerous direction: super().unlink()
        # drops the DB row, then the sync removes it from CF. The savepoint
        # rolls the unlink back on sync failure so the row survives — without
        # it a failed sync would leave the route gone from the DB but still
        # serving publicly via DNS/ingress, with no DB row left to drive a
        # desired-state push (a permanent orphan). The guarantee is Odoo-side
        # only: a mid-reconcile operator failure can still leave CF partially
        # mutated (e.g. ingress rewritten but a DNS delete failed). Because the
        # DB row is preserved, the next idempotent re-sync re-asserts the full
        # desired state and converges CF — which is exactly why preserving the
        # row matters. The re-raised error reaches the controller as a truthful
        # failure.
        with self.env.cr.savepoint():
            result = super().unlink()
            for sa in affected:
                if (sa.exists() and sa.state in ('active', 'active_no_route', 'error')
                        and sa.tunnel_id):
                    sa._sync_routes_to_operator()
            # P2.5: tear down each deleted protected route's own Access app
            # using the ids captured before the unlink (the rows are gone now).
            #
            # Fail-loud on teardown failure, do NOT swallow it. _teardown_route_
            # access best-effort-catches and returns False (not raise), but here
            # the row is already gone via super().unlink(); ignoring the False
            # would leave the route's CF Access app + reusable policies as
            # permanent orphans with NO retry anchor (the deleted row can never
            # re-enter _cleanup_cloudflare's route_ids.filtered('access_app_id'))
            # and NO error signal. Instead, raise so the savepoint rolls the
            # unlink back: the row (+ its access_app_id) survives as the retry
            # anchor, mirroring the sibling route-sync leg and policy-unlink
            # resync, and consistent with _cleanup_cloudflare's `if ok:` honour
            # of the same True/False teardown contract ([[two-stage-remote-
            # provision-idempotency]]: every failure path keeps the stored id so
            # retry is safe).
            failed = []
            for info in teardown_by_route.values():
                sa = info['sa']
                if not sa.exists():
                    continue
                ok = sa._teardown_route_access(
                    app_id=info['app_id'],
                    policy_ids=info['policy_ids'],
                    app_name=info['app_name'],
                    hostname=info['hostname'],
                )
                if not ok:
                    failed.append(info['app_name'] or info['app_id'])
            if failed:
                raise UserError(
                    "Cloudflare Access teardown failed for route(s): %s. The "
                    "route was kept so the deletion can be retried; its Access "
                    "app is still live in Cloudflare." % ', '.join(
                        str(f) for f in failed)
                )
        # D1: deleting the last route drops the SA back to 'active_no_route'.
        affected.exists()._recompute_route_state()
        return result

    # ------------------------------------------------------------------ #
    #  P2.5 per-route Access sync (declarative; one CF Access app/route)  #
    # ------------------------------------------------------------------ #

    def _build_existing_policy_ids(self):
        """CF reusable-policy ids the operator must delete-then-recreate for
        THIS route. Normally the surviving rows' cf_policy_id; a policy unlink
        runs super().unlink() before the resync, so the just-deleted rows' cf
        ids are already gone from the DB — they would never enter this list and
        the operator (whose only delete path is this list) would leak them as
        permanent CF orphans. unlink() captures those doomed ids and passes them
        via context so they are reaped on the same resync."""
        self.ensure_one()
        ids = [p.cf_policy_id for p in self.access_policy_ids if p.cf_policy_id]
        for cf_id in self.env.context.get('sa_unlinked_cf_policy_ids') or []:
            if cf_id and cf_id not in ids:
                ids.append(cf_id)
        return ids

    def _build_access_payload(self):
        """Desired-state payload for the operator's /api/access/sync — scoped to
        THIS single route. Each protected route is its own Cloudflare Access
        application: domain = destinations = this route's hostname only."""
        self.ensure_one()
        sa = self.security_access_id
        login_idp_types = []
        if self.login_google:
            login_idp_types.append('google')
        if self.login_email_otp:
            login_idp_types.append('onetimepin')
        hostname = self.hostname or ''
        protected = [hostname] if (self.is_protected and hostname) else []
        return {
            'enabled': bool(self.is_protected),
            'canonical_name': sa.canonical_name,
            # Per-route app display name: distinct per route so multiple apps
            # under one tunnel never collide on a shared name.
            'app_name': self.subdomain_prefix or sa.canonical_name,
            'primary_hostname': hostname,
            'protected_hostnames': protected,
            'session_duration': self.session_duration or '24h',
            'login_idp_types': login_idp_types,
            'policies': [{
                'name': p.name, 'decision': p.decision,
                'rule_type': p.rule_type, 'rule_value': p.rule_value,
                'precedence': p.sequence,
            } for p in self.access_policy_ids.sorted(
                key=lambda x: (x.sequence, x.id))],
            'existing_app_id': self.access_app_id or None,
            'existing_policy_ids': self._build_existing_policy_ids(),
        }

    def _sync_access_to_operator(self):
        """Push THIS route's full desired Access state to the operator
        (idempotent). Liveness gate matches the route/SA sync: only fires once
        the SA has a live tunnel and is in ('active', 'active_no_route', 'error') — a stage-2
        provision failure leaves the SA in 'error' with a live tunnel while the
        subscription stays active, so an Access edit in that state must still
        converge CF, not silently diverge it. is_protected=False sends an
        enabled=False payload (teardown via existing_app_id), then clears the
        stored app id; with no app id it is a harmless no-op.

        The `sa_force_access_sync` context bypasses the STATE half of the gate
        (tunnel_id is still required): provision/resume are deliberate
        convergence points that run BEFORE the SA state is flipped back to
        'active' (resume) or while it is being restored, so the create/write
        liveness gate would otherwise skip the very push they exist to do —
        e.g. converging a route disabled while suspended (its stored app_id must
        be torn down at resume even though state is still 'suspended' here)."""
        self.ensure_one()
        sa = self.security_access_id
        forced = self.env.context.get('sa_force_access_sync')
        if not sa or not sa.tunnel_id:
            return
        if not forced and sa.state not in ('active', 'active_no_route', 'error'):
            return
        # Nothing to do for an unprotected route that was never an app.
        if not self.is_protected and not self.access_app_id:
            return
        client = sa._get_operator_client()
        result = client.sync_access_app(self._build_access_payload())
        # Surface old reusable policies the operator could not delete after
        # repointing the app — permanent Cloudflare orphans (cost/clutter) that
        # no later re-sync reconciles, so make them observable in the log.
        orphaned = result.get('orphaned_policy_ids') or []
        if orphaned:
            _logger.warning(
                "Security Access route %s (%s, sa=%s): operator left %d orphan "
                "CF Access policy(ies) after sync: %s", self.id,
                self.subdomain_prefix, sa.id, len(orphaned),
                ', '.join(orphaned))
        # write back CF ids onto this route
        self.access_app_id = result.get('app_id') or False
        # Persist the operator's fail-open login-widening signal (requested
        # login methods with no matching CF IdP). Surfaced via to_dict().
        unresolved = result.get('unresolved_idp_types') or []
        new_unresolved = ', '.join(unresolved) if unresolved else False
        if self.access_unresolved_idp_types != new_unresolved:
            self.access_unresolved_idp_types = new_unresolved
        # map returned policy ids back onto policy rows in (sequence,id) order
        returned = result.get('policy_ids') or []
        policies = self.access_policy_ids.sorted(
            key=lambda x: (x.sequence, x.id))
        for pol, cf_id in zip(policies, returned):
            if pol.cf_policy_id != cf_id:
                pol.cf_policy_id = cf_id
        if not result.get('app_id'):
            # torn down → clear stale cf ids on this route's policies
            for pol in self.access_policy_ids:
                if pol.cf_policy_id:
                    pol.cf_policy_id = False

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'subdomain_prefix': self.subdomain_prefix,
            'hostname': self.hostname or '',
            'service_url': self.service_url,
            'http_host_header': self.http_host_header,
            'is_protected': self.is_protected,
            # P2.5 per-route Access config
            'session_duration': self.session_duration,
            'login_google': self.login_google,
            'login_email_otp': self.login_email_otp,
            # Fail-open login-widening signal from this route's last sync.
            'access_unresolved_idp_types': self.access_unresolved_idp_types or '',
            'access_policies': [p.to_dict() for p in self.access_policy_ids],
        }
