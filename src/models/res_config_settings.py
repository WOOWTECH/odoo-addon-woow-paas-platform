from odoo import api, fields, models


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

    def set_values(self):
        """Override to sync default provider to agents without a provider."""
        provider_before = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_ai_provider_id',
        )
        super().set_values()
        provider_after = self.woow_ai_provider_id
        if provider_after and str(provider_after.id) != str(provider_before or ''):
            # Assign the new default provider to agents that have no provider set
            agents_without_provider = self.env['woow_paas_platform.ai_agent'].sudo().search([
                ('provider_id', '=', False),
            ])
            if agents_without_provider:
                agents_without_provider.write({'provider_id': provider_after.id})

    def action_test_ai_connection(self):
        """Test connection to the configured AI provider."""
        self.ensure_one()
        provider = self.woow_ai_provider_id
        if not provider:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'AI Connection Test',
                    'message': 'No AI provider configured. Please select a provider first.',
                    'type': 'warning',
                    'sticky': False,
                },
            }

        from .ai_client import AIClient, AIClientError

        try:
            client = AIClient(
                api_base_url=provider.api_base_url,
                api_key=provider.api_key,
                model_name=provider.model_name,
                max_tokens=50,
                temperature=0.1,
            )
            messages = client.build_messages(
                system_prompt='',
                history=[],
                user_message="Say 'Connection successful' in exactly two words.",
            )
            response = client.chat_completion(messages)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'AI Connection Test',
                    'message': f'Connection successful! Response: {response[:100]}',
                    'type': 'success',
                    'sticky': False,
                },
            }
        except AIClientError as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'AI Connection Test Failed',
                    'message': f'Connection failed: {e.message}',
                    'type': 'danger',
                    'sticky': True,
                },
            }
