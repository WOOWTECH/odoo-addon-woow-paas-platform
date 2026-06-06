import logging
import re
import uuid

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..services.naming import make_security_access_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)

# 3-63 chars, lowercase alphanumeric + hyphen, no leading/trailing hyphen
_PREFIX_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$')
# Platform-internal naming conventions users must not squat on
_RESERVED_PREFIXES = ('paas-', 'svc-')


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
    tunnel_uptime = fields.Char(string='Uptime')

    primary_hostname = fields.Char(
        string='Primary Hostname',
        compute='_compute_primary_hostname',
        help='{canonical_name}.{paas_domain} — safe to compute: derives '
             'from the stored canonical_name, not from the nickname.',
    )

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

    @api.depends('canonical_name')
    def _compute_primary_hostname(self):
        domain = self._get_paas_domain()
        for record in self:
            record.primary_hostname = (
                f"{record.canonical_name}.{domain}"
                if record.canonical_name else False
            )

    def _compute_route_count(self):
        for record in self:
            record.route_count = len(record.route_ids)

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
        return super().write(vals)

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
                # Self-heal: a first provision can land state=active +
                # tunnel_id but die before/while creating the primary route
                # record (e.g. UNIQUE clash or a ValidationError). Without the
                # primary row _build_route_payload would push a tunnel that
                # serves only the catch-all 404. Rebuild it from the stored
                # canonical_name (the immutable primary subdomain) before
                # re-syncing so the retry path is genuinely self-curing rather
                # than freezing an "active but no primary route" state.
                if not self.route_ids.filtered('is_primary'):
                    self.env[
                        'woow_paas_platform.security_access_route'
                    ].with_context(sa_skip_route_sync=True).create({
                        'security_access_id': self.id,
                        'subdomain_prefix': self.canonical_name,
                        'service_url': self._get_default_service_url(),
                        'is_primary': True,
                    })
                self._sync_routes_to_operator()
                self.write({'state': 'active'})
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
            hostname = self.primary_hostname
            default_url = self._get_default_service_url()

            # create_tunnel already configures ingress (single rule +
            # catch-all) and creates the primary DNS record in one call.
            result = client.create_tunnel(
                name=self.canonical_name,
                hostname=hostname,
                service_url=default_url,
            )

            self.write({
                'state': 'active',
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'deployed_at': fields.Datetime.now(),
            })

            # Primary route record mirrors the ingress rule created above.
            # Skip the route-create override's sync (sa_skip_route_sync): it
            # would fire a first PUT /routes that lacks http_host_header, and
            # the explicit re-sync just below supersedes it — two operator
            # round-trips for one declarative result. Keep only the explicit
            # one (it carries the originRequest/http_host_header semantics).
            self.env['woow_paas_platform.security_access_route'].with_context(
                sa_skip_route_sync=True,
            ).create({
                'security_access_id': self.id,
                'subdomain_prefix': self.canonical_name,
                'service_url': default_url,
                'is_primary': True,
                'dns_record_id': result.get('dns_record_id'),
            })

            # Re-sync so the primary rule carries http_host_header
            # (create_tunnel's single-rule config doesn't set originRequest).
            self._sync_routes_to_operator()

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
            }
            if connections:
                conn = connections[0]
                update_vals['connector_id'] = conn.get('connector_id', '')
                update_vals['connector_type'] = conn.get('type', '')
                if conn.get('opened_at'):
                    update_vals['tunnel_uptime'] = conn['opened_at']
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
        record (cloud-service-unlink-override-cleanup pattern)."""
        for record in self:
            if record.tunnel_id:
                record._cleanup_cloudflare()
        # primary route's own unlink guard would block the cascade — force it
        # since the whole SA (and its routes) is being torn down.
        self.route_ids.with_context(sa_force_delete_primary=True).unlink()
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
                self.state if self.state in ('active', 'error') else 'active'
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
        if self.tunnel_id:
            try:
                self._sync_routes_to_operator()
            except Exception as e:
                _logger.warning("[SA] _do_resume failed sa=%s: %s", self.id, e)
                self.write({'state': 'error'})
                return
        restore_state = self.pre_suspend_state or 'active'
        self.write({'state': restore_state, 'pre_suspend_state': False})
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
        if self.tunnel_id:
            # cleanup did not finish — tunnel still live in Cloudflare
            self.write({
                'state': 'error',
                'error_message': "Cloudflare cleanup failed; tunnel still "
                                 "live. Will retry on the next cron pass.",
            })
            _logger.warning(
                "[SA] _do_cleanup incomplete sa=%s tunnel=%s (state=error)",
                self.id, self.tunnel_id,
            )
            return
        self.write({'state': 'deleting'})

    def _cleanup_cloudflare(self):
        """Idempotent CF teardown. delete_tunnel cascades: operator removes
        every DNS record pointing at the tunnel, then deletes the tunnel
        (token invalidated)."""
        self.ensure_one()
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
            'primary_hostname': self.primary_hostname or '',
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
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
        help='Per-route Access protection toggle. Wired to Cloudflare '
             'Access in P2 — stored but inert in P1.',
    )
    dns_record_id = fields.Char(string='DNS Record ID')

    @api.depends('subdomain_prefix')
    def _compute_hostname(self):
        domain = self.env['woow_paas_platform.security_access'
                          ]._get_paas_domain()
        for route in self:
            route.hostname = (
                f"{route.subdomain_prefix}.{domain}"
                if route.subdomain_prefix else False
            )

    @api.constrains('subdomain_prefix', 'is_primary')
    def _check_prefix_format(self):
        for route in self:
            prefix = route.subdomain_prefix or ''
            if not _PREFIX_RE.match(prefix):
                raise ValidationError(
                    "Subdomain prefix must be 3-63 chars of lowercase "
                    "letters, digits and hyphens, and must not start or "
                    "end with a hyphen: %r" % prefix
                )
            if not route.is_primary and prefix.startswith(_RESERVED_PREFIXES):
                raise ValidationError(
                    "Subdomain prefix must not start with a reserved "
                    "platform prefix (%s): %r"
                    % (', '.join(_RESERVED_PREFIXES), prefix)
                )

    @api.constrains('security_access_id')
    def _check_route_quota(self):
        """Plan quota: non-primary route count <= plan.sa_max_routes.

        No subscription (admin/legacy) → unrestricted, mirroring
        subscription_lifecycle_mixin._is_subscription_active. The
        controller layer re-checks (two-layer enforcement).
        """
        for route in self:
            if route.is_primary:
                continue
            sa = route.security_access_id
            sub = sa.sudo().subscription_id
            if not sub or not sub.sh_subscription_plan_id:
                continue
            quota = sub.sh_subscription_plan_id.sa_max_routes
            count = len(sa.route_ids.filtered(lambda r: not r.is_primary))
            if count > quota:
                raise ValidationError(
                    "Route quota exceeded: plan '%s' allows %d custom "
                    "route(s)." % (sub.sh_subscription_plan_id.name, quota)
                )

    @api.model_create_multi
    def create(self, vals_list):
        # Atomicity: the route row and its Cloudflare ingress must land (or
        # not land) together. A bare create+sync that swallows the sync error
        # at the controller leaves the row committed in the DB but absent in
        # CF — a "ghost route" that a later successful sync silently pushes
        # public. Wrap both in a savepoint and re-raise so the controller's
        # try/except reports a truthful failure with the DB row rolled back.
        with self.env.cr.savepoint():
            routes = super().create(vals_list)
            # one sync per affected SA (declarative full-state push).
            # action_provision sets sa_skip_route_sync because it issues its
            # own explicit, http_host_header-carrying re-sync right after — so
            # this override's sync would be a redundant extra operator call.
            if not self.env.context.get('sa_skip_route_sync'):
                for sa in routes.mapped('security_access_id'):
                    if sa.state == 'active' and sa.tunnel_id:
                        sa._sync_routes_to_operator()
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
        # Atomicity (see create): keep the DB service_url/header in lock-step
        # with the CF ingress. On sync failure roll the write back and surface
        # the error rather than committing a DB↔CF divergence.
        with self.env.cr.savepoint():
            result = super().write(vals)
            if any(k in vals for k in ('service_url', 'http_host_header')):
                for sa in self.mapped('security_access_id'):
                    if sa.state == 'active' and sa.tunnel_id:
                        sa._sync_routes_to_operator()
        return result

    def unlink(self):
        if any(r.is_primary for r in self) and not self.env.context.get(
                'sa_force_delete_primary'):
            raise UserError(
                "The primary route cannot be deleted. It is removed "
                "together with the Security Access instance."
            )
        affected = self.mapped('security_access_id')
        # Atomicity — the most dangerous direction: super().unlink() drops the
        # DB row, then the sync removes it from CF. If the sync fails without a
        # rollback, the route is gone from the DB but its DNS/ingress still
        # serves publicly, and no later sync can ever reach it (no DB row to
        # drive the desired-state push) — a permanent orphan. The savepoint
        # rolls the unlink back on sync failure so the row survives and the
        # public route is never left stranded; the re-raised error reaches the
        # controller as a truthful failure.
        with self.env.cr.savepoint():
            result = super().unlink()
            for sa in affected:
                if sa.exists() and sa.state == 'active' and sa.tunnel_id:
                    sa._sync_routes_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'subdomain_prefix': self.subdomain_prefix,
            'hostname': self.hostname or '',
            'service_url': self.service_url,
            'http_host_header': self.http_host_header,
            'is_primary': self.is_primary,
            'is_protected': self.is_protected,
        }
