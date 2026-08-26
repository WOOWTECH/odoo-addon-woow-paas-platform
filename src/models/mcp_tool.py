from odoo import _, fields, models
from odoo.exceptions import AccessError


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

    def write(self, vals):
        # server_id 是 tool 的 scope-bearing pointer：tool 繼承 server 的 scope，
        # 可見性／載入範圍完全由 server_id 決定。非 admin 不可 re-point 既有 tool
        # 到「別的 server」—— 那會把 tool 定義偷渡到目標 server 的 scope，造成
        # 跨租戶注入：
        #   • 目標 = system-scope server → 載進每個租戶（平台級 forgery，繞過
        #     mcp_tool_rule_system_readonly perm_create=False）；
        #   • 目標 = 他人 workspace 的 user-scope server → 注入該租戶（victim 的
        #     AI assistant 會載入此 tool）。
        # ir.rule 用 pre-write row（tool 仍在自己 server 上）判定 write rule →
        # 擋不住 re-point。合法流程從不 re-point tool（action_sync_tools 寫
        # server_id=self.id 為 no-op；無 controller/wizard 改 server_id），故對
        # 非 admin 一律擋「server_id 實際變更」。與 mcp_server.write() guard 同源
        # （sibling model parity，issue #98 / F-SDS-1）。
        # pattern: docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md
        if 'server_id' in vals and not self.env.su \
                and not self.env.user.has_group('base.group_system'):
            for tool in self:
                if vals['server_id'] != tool.server_id.id:
                    raise AccessError(
                        _("Only administrators can re-point an MCP tool to a "
                          "different server.")
                    )
        return super().write(vals)
