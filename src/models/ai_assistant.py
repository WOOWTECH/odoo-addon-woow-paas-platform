from odoo import fields, models


class AiAssistant(models.Model):
    _inherit = 'ai.assistant'

    mcp_server_ids = fields.Many2many(
        'woow_paas_platform.mcp_server',
        string='MCP Servers',
        domain=[('scope', '=', 'system'), ('active', '=', True)],
    )
    mcp_tool_disabled_ids = fields.Many2many(
        'woow_paas_platform.mcp_tool',
        string='Disabled MCP Tools',
        help='Tools in this list will not be available to the AI assistant',
    )

    def get_enabled_mcp_tools(self):
        """Return mcp_tool records that are enabled for this assistant.

        Returns all active tools from linked MCP servers, minus explicitly
        disabled tools.
        """
        self.ensure_one()
        if not self.mcp_server_ids:
            return self.env['woow_paas_platform.mcp_tool']
        all_tools = self.mcp_server_ids.mapped('tool_ids').filtered('active')
        return all_tools - self.mcp_tool_disabled_ids
