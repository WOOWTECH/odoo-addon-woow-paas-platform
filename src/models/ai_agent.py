from odoo import api, fields, models


class WoowAiAgent(models.Model):
    _name = 'woow_paas_platform.ai_agent'
    _description = 'AI Agent'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        help='Technical identifier for the agent (e.g., woowbot)',
    )
    agent_display_name = fields.Char(
        string='Display Name',
        help='User-facing name shown in chat (e.g., WoowBot)',
    )
    system_prompt = fields.Text(
        string='System Prompt',
        help='System prompt that defines the agent personality and behavior',
    )
    provider_id = fields.Many2one(
        'woow_paas_platform.ai_provider',
        string='AI Provider',
        ondelete='set null',
        help='AI provider used by this agent for generating responses',
    )
    avatar_color = fields.Char(
        string='Avatar Color',
        default='#875A7B',
        help='Hex color code for the agent avatar (e.g., #875A7B)',
    )
    is_default = fields.Boolean(
        string='Default Agent',
        default=False,
        help='Whether this agent is the default agent for new conversations',
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            if record.is_default:
                # Ensure only one default agent exists
                self.search([
                    ('is_default', '=', True),
                    ('id', '!=', record.id),
                ]).write({'is_default': False})
        return records

    def write(self, vals):
        result = super().write(vals)
        if vals.get('is_default'):
            # Ensure only one default agent exists
            self.search([
                ('is_default', '=', True),
                ('id', 'not in', self.ids),
            ]).write({'is_default': False})
        return result

    @api.model
    def get_default(self):
        """Return the default AI agent, or an empty recordset if none exists."""
        return self.sudo().search([('is_default', '=', True)], limit=1)
