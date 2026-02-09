from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # PaaS Operator Configuration
    woow_paas_operator_url = fields.Char(
        string='PaaS Operator URL',
        config_parameter='woow_paas_platform.operator_url',
        help='Base URL of the PaaS Operator service (e.g., http://paas-operator:8000)',
    )
    woow_paas_operator_api_key = fields.Char(
        string='PaaS Operator API Key',
        config_parameter='woow_paas_platform.operator_api_key',
        help='API key for authenticating with the PaaS Operator service',
    )

    # Domain Configuration
    woow_paas_domain = fields.Char(
        string='PaaS Domain',
        config_parameter='woow_paas_platform.paas_domain',
        default='woowtech.io',
        help='Base domain for deployed services (e.g., woowtech.io)',
    )

    # AI Provider Configuration
    woow_ai_provider_id = fields.Many2one(
        'woow_paas_platform.ai_provider',
        string='Default AI Provider',
        config_parameter='woow_paas_platform.default_ai_provider_id',
        help='Default AI provider used for AI assistant features',
    )
