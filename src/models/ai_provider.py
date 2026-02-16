from odoo import fields, models


class WoowAiProvider(models.Model):
    _name = 'woow_paas_platform.ai_provider'
    _description = 'AI Provider'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Display name for this AI provider (e.g., OpenAI, Ollama)',
    )
    api_base_url = fields.Char(
        string='API Base URL',
        required=True,
        help='Base URL for the OpenAI-compatible API (e.g., https://api.openai.com/v1)',
    )
    api_key = fields.Char(
        string='API Key',
        required=True,
        help='API key for authenticating with the provider',
    )
    model_name = fields.Char(
        string='Model Name',
        required=True,
        help='Model identifier (e.g., gpt-4o, llama3)',
    )
    is_active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this provider is available for use',
    )
    max_tokens = fields.Integer(
        string='Max Tokens',
        default=4096,
        help='Maximum number of tokens for AI responses',
    )
    temperature = fields.Float(
        string='Temperature',
        default=0.7,
        help='Sampling temperature (0.0 = deterministic, 1.0 = creative)',
    )
