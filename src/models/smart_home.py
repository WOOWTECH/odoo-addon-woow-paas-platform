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

    def _generate_subdomain(self):
        """Generate a unique subdomain for this smart home."""
        workspace = self.workspace_id
        slug = workspace.slug if hasattr(workspace, 'slug') else workspace.name.lower().replace(' ', '-')
        return make_smarthome_subdomain(slug, self.reference_id, self.name)

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

            # Create tunnel via operator
            result = client.create_tunnel(
                name=subdomain,
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
        """Delete the smart home and its Cloudflare Tunnel."""
        self.ensure_one()
        client = self._get_operator_client()

        if self.tunnel_id:
            self.write({'state': 'deleting'})
            try:
                client.delete_tunnel(self.tunnel_id)
                _logger.info(
                    "Smart Home '%s' tunnel deleted. Tunnel ID: %s",
                    self.name, self.tunnel_id,
                )
            except PaaSOperatorError as e:
                _logger.warning(
                    "Failed to delete tunnel %s for Smart Home '%s': %s",
                    self.tunnel_id, self.name, e.message,
                )

        self.unlink()

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
        }
