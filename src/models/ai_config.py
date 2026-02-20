from odoo import fields, models


class AIConfig(models.Model):
    _inherit = 'ai.config'

    api_base_url = fields.Char(
        string='API Base URL',
        help='Custom API base URL (e.g. for Azure OpenAI or compatible endpoints). '
             'Leave empty to use the default OpenAI endpoint.',
    )
