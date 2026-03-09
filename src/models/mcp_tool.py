from odoo import fields, models


class McpTool(models.Model):
    _name = 'woow_paas_platform.mcp_tool'
    _description = 'MCP Tool'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        index=True,
    )
    description = fields.Text(
        string='Description',
    )
    input_schema = fields.Text(
        string='Input Schema (JSON)',
        help='JSON Schema describing the tool input parameters',
    )
    server_id = fields.Many2one(
        'woow_paas_platform.mcp_server',
        string='MCP Server',
        required=True,
        ondelete='cascade',
        index=True,
    )
    active = fields.Boolean(
        default=True,
    )
