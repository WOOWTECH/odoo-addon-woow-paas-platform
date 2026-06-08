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

    # Security Access
    woow_sa_default_service_url = fields.Char(
        string='SA Default Service URL',
        config_parameter='woow_paas_platform.sa_default_service_url',
        default='http://localhost:80',
        help='Default reverse-proxy target for new Security Access '
             'application routes. Users are expected to run a Host-header '
             'aware reverse proxy (e.g. NPM) at this address.',
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
    woow_default_agent_template_id = fields.Many2one(
        'woow_paas_platform.agent_template',
        string='Default Agent Template',
        config_parameter='woow_paas_platform.default_agent_template_id',
        domain="[('is_public', '=', True), ('state', '=', 'published'), ('project_ids', '=', False)]",
        help='Platform-wide fallback template used by any project that has no '
             'template of its own (agent-runtime pod chat). Only a public, '
             'published, project-unbound template qualifies — a project-bound '
             'template would leak that tenant MCP/skill bindings cluster-wide.',
    )
    woow_default_enable_agent_runtime = fields.Boolean(
        string='Auto-enable Agent Runtime for New Projects',
        config_parameter='woow_paas_platform.default_enable_agent_runtime',
        default=False,
        help='When ON, a project created from /woow automatically turns on '
             'its Agent Runtime Pod — but only if the platform is ready '
             '(PaaS Operator URL + API Key set and a valid Default Agent '
             'Template selected above). When the platform is not ready the '
             'flag stays off so a new project never lands in a guaranteed '
             'spawn-failure state. Users never set this per project; it is a '
             'platform-wide default decided here.',
    )

    # Subscription Billing
    woow_subscription_journal_id = fields.Many2one(
        'account.journal',
        related='company_id.sh_journal_id',
        readonly=False,
        string='Subscription Journal',
        help='Accounting journal used for subscription invoices. '
             'Must be a Sales type journal.',
    )

    # Credit Top-up
    woow_topup_payment_enabled = fields.Boolean(
        string='Enable Credit Top-up Payment',
        config_parameter='woow_paas_platform.topup_payment_enabled',
        default=False,
        help='Allow users to actually charge a payment method when buying '
             'top-up plans. Disable to block the checkout call while payment '
             'processing is still being configured.',
    )

    # Portal (/woow) Project Settings
    woow_portal_project_settings_editable = fields.Boolean(
        string='Portal Project Settings Editable',
        config_parameter='woow_paas_platform.portal_project_settings_editable',
        help='When ON, /woow portal owners/admins can edit project name/'
             'description, cloud-service binding and user-scope MCP servers. '
             'Default OFF: portal project settings are read-only and managed '
             'from the backend (2026-06-03 meeting, topic H).',
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
