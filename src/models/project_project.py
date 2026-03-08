from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        string='Workspace',
        ondelete='set null',
        index=True,
        help='PaaS Workspace this project belongs to',
    )
