from odoo import fields, models


class CloudServiceSecret(models.Model):
    _name = 'woow_paas_platform.cloud_service_secret'
    _description = 'Cloud Service Secret'
    _order = 'updated_at desc, id desc'

    service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        required=True,
        ondelete='cascade',
        index=True,
        help='Owning cloud service',
    )
    admin_password = fields.Char(
        string='MCP Admin Password',
        required=True,
        help='One-time plaintext admin password for MCP admin GUI',
    )
    updated_at = fields.Datetime(
        string='Updated At',
        required=True,
        default=fields.Datetime.now,
    )

    _sql_constraints = [
        (
            'unique_cloud_service_secret_service',
            'UNIQUE(service_id)',
            'Each cloud service can have only one secret record.',
        ),
    ]

    def write(self, vals):
        vals = dict(vals)
        vals.setdefault('updated_at', fields.Datetime.now())
        return super().write(vals)
