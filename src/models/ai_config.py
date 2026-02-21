from odoo import api, fields, models


class AIConfig(models.Model):
    _inherit = 'ai.config'

    type = fields.Selection(
        selection_add=[('openai_compatible', 'OpenAI Compatible')],
        ondelete={'openai_compatible': 'cascade'},
    )
    api_base_url = fields.Char(
        string='API Base URL',
        help='Custom API base URL for OpenAI-compatible endpoints '
             '(e.g. https://api.openai.com/v1).',
    )

    def _get_default_model(self):
        if self.type == 'openai_compatible':
            return 'gpt-4o-mini'
        return super()._get_default_model()
