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

    # Namespace Prefix
    woow_paas_namespace_prefix = fields.Char(
        string='Namespace Prefix',
        config_parameter='woow_paas_platform.namespace_prefix',
        default='paas-ws-',
        help='Prefix for Kubernetes namespaces created by PaaS (e.g., paas-ws- or paas-ws-stg-)',
    )

    # AI Configuration (using ai_base_gt models)
    woow_ai_config_id = fields.Many2one(
        'ai.config',
        string='Default AI Configuration',
        config_parameter='woow_paas_platform.default_ai_config_id',
        help='Default AI configuration used for AI assistant features',
    )
    woow_ai_assistant_id = fields.Many2one(
        'ai.assistant',
        string='Default AI Assistant',
        config_parameter='woow_paas_platform.default_ai_assistant_id',
        help='Default AI assistant for automatic replies',
    )

    # Subscription Lifecycle Settings
    woow_subscription_grace_days = fields.Integer(
        string='Subscription Grace Period (Days)',
        config_parameter='woow_paas_platform.grace_period_days',
        default=7,
        help='Days after subscription expiry before service suspension',
    )
    woow_subscription_cleanup_months = fields.Integer(
        string='Auto Cleanup After (Months)',
        config_parameter='woow_paas_platform.cleanup_after_months',
        default=6,
        help='Months after cancellation before permanent service removal',
    )

    def action_test_ai_connection(self):
        """Test connection to the configured AI assistant."""
        self.ensure_one()
        assistant = self.woow_ai_assistant_id
        if not assistant:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'AI Connection Test',
                    'message': 'No AI assistant configured. Please select an assistant first.',
                    'type': 'warning',
                    'sticky': False,
                },
            }

        from .ai_client import AIClient, AIClientError

        try:
            client = AIClient.from_assistant(assistant)
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
