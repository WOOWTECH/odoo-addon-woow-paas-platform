import uuid

from odoo import fields, models


class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _description = 'Cloud Service Instance'
    _order = 'create_date desc'

    _sql_constraints = [
        (
            'unique_subdomain',
            'UNIQUE(subdomain)',
            'Subdomain must be unique across all services.',
        ),
        (
            'unique_reference_id',
            'UNIQUE(reference_id)',
            'Reference ID must be unique.',
        ),
    ]

    # Relationships
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        help='Workspace this service belongs to',
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_app_template',
        string='Application Template',
        required=True,
        ondelete='restrict',
        help='The application template used to create this service',
    )

    # Identity
    name = fields.Char(
        string='Service Name',
        required=True,
        help='User-defined name for this service instance',
    )
    reference_id = fields.Char(
        string='Reference ID',
        index=True,
        readonly=True,
        default=lambda self: str(uuid.uuid4()),
        help='Immutable unique identifier for this service',
    )
    deployment_id = fields.Char(
        string='Deployment ID',
        help='External deployment system identifier',
    )

    # State
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('deploying', 'Deploying'),
            ('running', 'Running'),
            ('error', 'Error'),
            ('upgrading', 'Upgrading'),
            ('deleting', 'Deleting'),
        ],
        string='State',
        default='pending',
        help='Current state of the service deployment',
    )
    error_message = fields.Text(
        string='Error Message',
        help='Error details if deployment failed',
    )

    # Network
    subdomain = fields.Char(
        string='Subdomain',
        help='Subdomain for accessing this service (e.g., myapp.paas.woow.tw)',
    )
    custom_domain = fields.Char(
        string='Custom Domain',
        help='Custom domain configured by user',
    )
    internal_port = fields.Integer(
        string='Internal Port',
        help='Internal port where the service is listening',
    )

    # Helm Release Info
    helm_release_name = fields.Char(
        string='Helm Release Name',
        help='Name of the Helm release in Kubernetes',
    )
    helm_namespace = fields.Char(
        string='Helm Namespace',
        help='Kubernetes namespace where the service is deployed',
    )
    helm_values = fields.Text(
        string='Helm Values',
        help='JSON object with Helm values used for deployment',
    )
    helm_revision = fields.Integer(
        string='Helm Revision',
        default=1,
        help='Current Helm release revision number',
    )
    helm_chart_version = fields.Char(
        string='Chart Version',
        help='Version of the Helm chart currently deployed',
    )

    # Resources
    allocated_vcpu = fields.Integer(
        string='Allocated vCPU',
        help='Number of vCPU cores allocated to this service',
    )
    allocated_ram_gb = fields.Float(
        string='Allocated RAM (GB)',
        help='RAM in GB allocated to this service',
    )
    allocated_storage_gb = fields.Integer(
        string='Allocated Storage (GB)',
        help='Storage in GB allocated to this service',
    )

    # Timestamps
    deployed_at = fields.Datetime(
        string='Deployed At',
        help='Timestamp when service was first deployed',
    )
    last_upgraded_at = fields.Datetime(
        string='Last Upgraded At',
        help='Timestamp of the most recent upgrade',
    )
