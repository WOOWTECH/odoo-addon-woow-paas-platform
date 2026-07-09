import logging
import uuid

from odoo import fields, models
from odoo.exceptions import UserError

from ..services.naming import make_smarthome_subdomain

from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)


class SmartHome(models.Model):
    _name = 'woow_paas_platform.smart_home'
    _inherit = ['woow_paas_platform.subscription_lifecycle_mixin']
    _description = 'Smart Home Instance'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'unique_subdomain',
            'UNIQUE(subdomain)',
            'Subdomain must be unique across all smart homes.',
        ),
    ]

    # Identity
    name = fields.Char(
        string='Name',
        required=True,
        help='User-defined name for this smart home',
    )
    reference_id = fields.Char(
        string='Reference ID',
        required=True,
        copy=False,
        index=True,
        default=lambda self: str(uuid.uuid4()),
        help='Unique reference ID for hash-based naming',
    )
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this smart home belongs to',
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

    # Store original service_url for resume after suspend
    original_service_url = fields.Char(
        help='Original service URL before suspension, used for resume',
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
    tunnel_route = fields.Char(string='Tunnel Route URL')
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

    # Config
    ha_port = fields.Integer(
        string='HA Port',
        default=8123,
        help='Home Assistant service port',
    )
    subdomain = fields.Char(
        string='Subdomain',
        help='Assigned subdomain for this smart home tunnel',
    )

    # Timestamps
    deployed_at = fields.Datetime(string='Deployed At')

    def _get_operator_client(self):
        """Get PaaS Operator client instance."""
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(
                "PaaS Operator is not configured. "
                "Please set Operator URL and API Key in Settings → Woow PaaS."
            )
        return client

    def _naming_slug(self):
        """Workspace slug used as the stable hash seed for Cloudflare naming."""
        workspace = self.workspace_id
        return workspace.slug if hasattr(workspace, 'slug') else workspace.name.lower().replace(' ', '-')

    def _generate_subdomain(self):
        """Generate the public subdomain (= user-facing hostname base).

        Deliberately env-token-FREE: the subdomain is the external URL and is
        immutable, so prod/stg are told apart by per-env ``paas_domain``, not by
        mutating the hostname (DP2). The env token rides the internal tunnel
        name instead — see ``_generate_tunnel_name``.
        """
        return make_smarthome_subdomain(self._naming_slug(), self.reference_id, self.name)

    def _generate_tunnel_name(self):
        """Generate the Cloudflare *tunnel display name*, carrying the deploy-env token.

        A SmartHome owns a dedicated Cloudflare tunnel on the prod/stg-shared
        account; with no env token in its name — and, unlike CloudService, no
        shared-tunnel CNAME ``content`` clue — it cannot self-identify its
        environment (the orphan-attribution gap from cf-ns-orphan-no-account-gc).
        We stamp the env onto the *internal* tunnel name (e.g. ``paas-stg-sm-…``)
        while the public subdomain stays clean, mirroring SecurityAccess where
        the token rides the internal ``canonical_name`` rather than the route's
        user-facing ``subdomain_prefix``. The operator keeps the tunnel name and
        the DNS hostname independent (DNS is derived from the hostname, never the
        name — see operator tunnels.py), so the user-facing URL is unchanged.
        Teardown reads the stored ``tunnel_id``, never a recomputed name, so the
        token is reverse-lookup-safe (external-resource-env-token-naming 守則 3/4).
        """
        env = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.deploy_env', 'prod')
        return make_smarthome_subdomain(
            self._naming_slug(), self.reference_id, self.name, env=env)

    def _get_paas_domain(self):
        """Get PaaS domain from system settings."""
        return self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.paas_domain', 'woowtech.io'
        )

    def action_provision(self):
        """Provision the smart home by creating a dedicated Cloudflare Tunnel."""
        self.ensure_one()
        client = self._get_operator_client()

        self.write({'state': 'provisioning', 'error_message': False})

        try:
            # Generate subdomain
            subdomain = self._generate_subdomain()
            domain = self._get_paas_domain()
            hostname = f"{subdomain}.{domain}"

            # Create tunnel via operator. The tunnel *name* carries the deploy-env
            # token (internal traceability on the shared CF account); the
            # *hostname* stays env-clean so the user-facing URL is unchanged.
            result = client.create_tunnel(
                name=self._generate_tunnel_name(),
                hostname=hostname,
                service_url=f"http://localhost:{self.ha_port}",
            )

            self.write({
                'state': 'active',
                'tunnel_id': result.get('tunnel_id'),
                'tunnel_token': result.get('tunnel_token'),
                'tunnel_name': result.get('tunnel_name'),
                'tunnel_route': f"https://{hostname}",
                'subdomain': subdomain,
                'deployed_at': fields.Datetime.now(),
            })

            _logger.info(
                "Smart Home '%s' provisioned successfully. Tunnel ID: %s",
                self.name, result.get('tunnel_id'),
            )

        except PaaSOperatorConnectionError as e:
            self.write({
                'state': 'error',
                'error_message': f"Cannot connect to PaaS Operator: {e.message}",
            })
            _logger.error("Smart Home provision connection error: %s", e.message)
        except PaaSOperatorError as e:
            self.write({
                'state': 'error',
                'error_message': f"Tunnel creation failed: {e.message}",
            })
            _logger.error("Smart Home provision error: %s", e.message)

    def action_delete(self):
        """Delete the smart home and its Cloudflare Tunnel.

        Teardown + fail-loud orphan guard are delegated to unlink(): if the
        tunnel cannot be deleted, the record is kept and a UserError surfaces,
        so a billed tunnel is never left orphaned in Cloudflare with no DB row
        to drive a retry (F-ENV-4).
        """
        self.ensure_one()
        if self.tunnel_id:
            self.write({'state': 'deleting'})
        self.unlink()

    def _cleanup_tunnel(self):
        """Best-effort idempotent Cloudflare tunnel teardown.

        Clears tunnel_id on success and KEEPS it on failure, so unlink() / a
        retry can detect a still-live tunnel and avoid orphaning it (symmetric
        with SecurityAccess._cleanup_cloudflare).
        """
        self.ensure_one()
        if not self.tunnel_id:
            return
        try:
            client = self._get_operator_client()
            client.delete_tunnel(self.tunnel_id)
            _logger.info(
                "[SH] tunnel deleted sh=%s tunnel=%s", self.id, self.tunnel_id,
            )
            self.tunnel_id = False
        except Exception as e:
            _logger.warning(
                "[SH] tunnel cleanup failed sh=%s: %s — kept for retry",
                self.id, e,
            )

    def unlink(self):
        """Direct-unlink guard: the Cloudflare tunnel must never outlive the
        record.

        Without this, a transient delete_tunnel failure would drop the DB row
        while a billed, token-still-valid tunnel lives on in Cloudflare with no
        DB reference left to drive a retry — a permanent orphan
        (cf-ns-orphan-no-account-gc: SmartHome's unconditional unlink was the
        source of 3 such orphan tunnels). Mirrors SecurityAccess.unlink: attempt
        cleanup, then fail-loud if the tunnel is still live so both the DB row
        and the CF tunnel survive for the user / cron to retry.
        """
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        # 捕獲在 fail-loud 之前、結算在 super().unlink() 之後：tunnel fail-loud
        # raise 時不結算，全交易 rollback。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        for record in self:
            if record.tunnel_id:
                record._cleanup_tunnel()
            if record.tunnel_id:
                raise UserError(
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. The record was kept so the deletion can be "
                    "retried. Please try again shortly."
                )
        res = super().unlink()
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Smart Home deleted')
        return res

    def action_refresh_status(self):
        """Refresh tunnel status from the PaaS Operator."""
        self.ensure_one()
        if not self.tunnel_id:
            return

        client = self._get_operator_client()

        try:
            result = client.get_tunnel_status(self.tunnel_id)

            # Parse connections
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
                "Failed to refresh tunnel status for Smart Home '%s': %s",
                self.name, e.message,
            )

    def to_dict(self):
        """Serialize smart home data for API responses."""
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'workspace_id': self.workspace_id.id,
            'state': self.state,
            'tunnel_id': self.tunnel_id or '',
            'tunnel_name': self.tunnel_name or '',
            'tunnel_route': self.tunnel_route or '',
            'tunnel_status': self.tunnel_status or 'disconnected',
            'tunnel_uptime': self.tunnel_uptime or '',
            'connector_id': self.connector_id or '',
            'connector_type': self.connector_type or '',
            'ha_port': self.ha_port,
            'subdomain': self.subdomain or '',
            'error_message': self.error_message or '',
            'deployed_at': self.deployed_at.isoformat() if self.deployed_at else '',
            'created_at': self.create_date.isoformat() if self.create_date else '',
            'subscription': self._get_subscription_info(),
        }

    # ------------------------------------------------------------------ #
    #  Subscription lifecycle: suspend / resume / cleanup                #
    # ------------------------------------------------------------------ #

    def _do_suspend(self):
        """Update tunnel ingress to return 503."""
        self.ensure_one()
        _logger.info(
            "[SH] _do_suspend sh=%s name=%s state=%s sub=%s",
            self.id, self.name, self.state,
            self.subscription_id.id if self.subscription_id else None,
        )
        if self.state == 'suspended':
            return

        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                hostname = f"{self.subdomain}.{self._get_paas_domain()}"
                client.configure_tunnel(
                    self.tunnel_id,
                    hostname=hostname,
                    service_url="http_status:503",
                )
                _logger.info(
                    "[SH] _do_suspend tunnel 503 sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_suspend tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error'})
                return

        self.write({
            'original_service_url': f"http://localhost:{self.ha_port}",
            'pre_suspend_state': self.state if self.state in ('active', 'error') else 'active',
            'state': 'suspended',
        })
        _logger.info("[SH] _do_suspend done sh=%s new_state=suspended", self.id)

    def _do_resume(self):
        """Restore tunnel ingress to original service URL."""
        self.ensure_one()
        _logger.info(
            "[SH] _do_resume sh=%s name=%s state=%s pre_suspend=%s",
            self.id, self.name, self.state, self.pre_suspend_state,
        )
        if self.state != 'suspended':
            return

        service_url = self.original_service_url or f"http://localhost:{self.ha_port}"
        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                hostname = f"{self.subdomain}.{self._get_paas_domain()}"
                client.configure_tunnel(
                    self.tunnel_id,
                    hostname=hostname,
                    service_url=service_url,
                )
                _logger.info(
                    "[SH] _do_resume tunnel restored sh=%s tunnel=%s",
                    self.id, self.tunnel_id,
                )
            except Exception as e:
                _logger.warning(
                    "[SH] _do_resume tunnel failed sh=%s: %s",
                    self.id, e,
                )
                self.write({'state': 'error'})
                return

        restore_state = self.pre_suspend_state or 'active'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })
        _logger.info(
            "[SH] _do_resume done sh=%s new_state=%s",
            self.id, restore_state,
        )

    def _do_cleanup(self):
        """Delete tunnel permanently (cron path).

        Mirrors SecurityAccess._do_cleanup fail-loud contract: the cron calls
        _do_cleanup (not unlink), so this path is NOT covered by the unlink
        guard and must not silently swallow a delete_tunnel failure then write
        'deleting' (a false success signal — the subscription is already
        cancelled so the cron never revisits, leaving a billed orphan tunnel).
        _cleanup_tunnel keeps tunnel_id on failure; if it is still set, land in
        'error' so the next cron pass retries (F-ENV-4 M1).
        """
        self.ensure_one()
        _logger.info(
            "[SH] _do_cleanup sh=%s name=%s tunnel=%s",
            self.id, self.name, self.tunnel_id,
        )
        self._cleanup_tunnel()
        if self.tunnel_id:
            self.write({
                'state': 'error',
                'error_message': (
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. Will retry on the next cron pass."
                ),
            })
            _logger.warning(
                "[SH] _do_cleanup incomplete sh=%s tunnel=%s (state=error)",
                self.id, self.tunnel_id,
            )
            return
        self.write({'state': 'deleting'})
