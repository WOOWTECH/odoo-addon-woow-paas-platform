from odoo import api, fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    cloud_service_id = fields.Many2one(
        'woow_paas_platform.cloud_service',
        string='Cloud Service',
        ondelete='set null',
        index=True,
        help='Cloud Service this support project is bound to',
    )
    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        string='Workspace',
        compute='_compute_workspace_id',
        store=True,
        help='Workspace derived from the linked Cloud Service',
    )

    @api.depends('cloud_service_id.workspace_id')
    def _compute_workspace_id(self):
        for project in self:
            project.workspace_id = project.cloud_service_id.workspace_id

    _sql_constraints = [
        (
            'unique_cloud_service',
            'UNIQUE(cloud_service_id)',
            'A Cloud Service can only have one Support Project.',
        ),
    ]
