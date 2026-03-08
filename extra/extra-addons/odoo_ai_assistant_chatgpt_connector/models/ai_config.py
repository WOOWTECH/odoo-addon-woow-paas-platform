from openai import OpenAI
from odoo import fields, models, _
from odoo.exceptions import UserError


class AIConfig(models.Model):
    _inherit = 'ai.config'

    type = fields.Selection(
        selection_add=[('chatgpt', 'ChatGPT')],
        ondelete={'chatgpt': 'cascade'}
    )

    def _get_default_model(self):
        """Return the default model for ChatGPT configurations."""
        if self.type == 'chatgpt':
            return 'gpt-4o-mini'
        return super()._get_default_model()

    def _get_chatgpt_client(self):
        """Create and return an OpenAI client instance.

        Validates that the API key is configured and has the correct format
        before creating the client.

        Returns:
            OpenAI: Configured OpenAI client instance

        Raises:
            UserError: If API key is not configured or has invalid format
        """
        if not self.api_key:
            raise UserError(_("ChatGPT API key is not configured"))
        if not self.api_key.startswith('sk-'):
            raise UserError(_("Invalid ChatGPT API key format. Keys should start with 'sk-'"))
        return OpenAI(api_key=self.api_key)
