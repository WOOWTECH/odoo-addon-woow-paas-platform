"""Smart Home MCP（#270 ha_mcp_tools 方案）平台側測試。

§7 平台驗收準則 1-4：
  1. plan 有 mcp_integration 欄位、default ''、不覆寫 std 欄位（TestSmartHomeMcpPlanField）
  2. 綁 ha_mcp_tools plan 的 home → payload mcp='ha_mcp_tools'（to_dict + endpoint）
  3. 無訂閱 / plan mcp='' → payload mcp=''
  4. 既有 remote-access 行為不變、無新增 tunnel/route/token/port
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, HttpCase, tagged


class TestSmartHomeMcpPlanField(TransactionCase):
    """§7 準則 1：sh.subscription.plan 新增 add-only 的 mcp_integration Char
    欄位、default ''（比照 sh_subscription_plan_sa_quota）。"""

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']

    def test_mcp_integration_defaults_empty(self):
        plan = self.Plan.create({
            'name': 'MCP Field Test Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.assertEqual(plan.mcp_integration, '')

    def test_mcp_integration_is_char_and_round_trips(self):
        field = self.Plan._fields['mcp_integration']
        self.assertEqual(field.type, 'char')
        plan = self.Plan.create({
            'name': 'MCP Field Test Plan 2',
            'sh_plan_price': 400.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'mcp_integration': 'ha_mcp_tools',
        })
        self.assertEqual(plan.mcp_integration, 'ha_mcp_tools')
