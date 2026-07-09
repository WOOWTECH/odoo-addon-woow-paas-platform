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


class TestSmartHomeToDictMcp(TransactionCase):
    """§7 準則 2（model 層）＋準則 3：to_dict() 依 plan 推導 mcp 字串。"""

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Subscription = self.env['sh.subscription.subscription']

        self.user = self.env.ref('base.user_admin')
        self.workspace = self.Workspace.create({
            'name': 'MCP ToDict WS',
            'owner_id': self.user.id,
        })

        self.plan_mcp = self.Plan.create({
            'name': 'SH MCP Plan',
            'sh_plan_price': 400.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'mcp_integration': 'ha_mcp_tools',
        })
        self.plan_plain = self.Plan.create({
            'name': 'SH Plain Plan',
            'sh_plan_price': 150.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.product = {}
        for plan, price in ((self.plan_mcp, 400.0), (self.plan_plain, 150.0)):
            self.product[plan.id] = self.env['product.template'].sudo().create({
                'name': f'{plan.name} Product',
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': price,
            })

    def _home_bound_to(self, plan):
        """最輕 fixture（leader decision 3）：直建 subscription、不 confirm，
        避免耦合 invoice/wallet/journal；getter 只需 subscription_id→plan。"""
        home = self.SmartHome.create({
            'name': f'HA for {plan.name}',
            'workspace_id': self.workspace.id,
        })
        sub = self.Subscription.sudo().create({
            'sh_partner_id': self.user.partner_id.id,
            'product_id': self.product[plan.id].product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
            'sh_source': 'manual',
        })
        home.sudo().subscription_id = sub.id
        return home

    def test_to_dict_mcp_present_when_plan_grants(self):
        """準則 2：綁 ha_mcp_tools plan → to_dict()['mcp'] == 'ha_mcp_tools'。"""
        home = self._home_bound_to(self.plan_mcp)
        self.assertEqual(home.to_dict()['mcp'], 'ha_mcp_tools')

    def test_to_dict_mcp_empty_when_plan_plain(self):
        """準則 3：綁無旗標 plan → mcp == ''。"""
        home = self._home_bound_to(self.plan_plain)
        self.assertEqual(home.to_dict()['mcp'], '')

    def test_to_dict_mcp_empty_when_no_subscription(self):
        """準則 3：無訂閱 → mcp == ''（且不拋錯）。"""
        home = self.SmartHome.create({
            'name': 'HA no sub',
            'workspace_id': self.workspace.id,
        })
        self.assertEqual(home.to_dict()['mcp'], '')

    def test_get_mcp_integration_helper_direct(self):
        """getter 直測：綁 MCP plan → 'ha_mcp_tools'。"""
        home = self._home_bound_to(self.plan_mcp)
        self.assertEqual(home._get_mcp_integration(), 'ha_mcp_tools')

    def test_to_dict_mcp_readable_by_restricted_owner(self):
        """【MUST-FIX / 真正驗證 sudo fix】受限 owner（只有 base.group_user、
        無 sh_subscription manager group）讀 to_dict()：pre-fix 讀 subscription_id
        觸發 AccessError（紅），post-fix self.sudo() 讀穿（綠）。

        關鍵：① owner groups 硬重設（防 sandbox 模板灌 admin 讓測試 no-op）；
        ② invalidate_all() 清 admin-context cache，逼 owner-env 重過 ACL。"""
        owner = self.env['res.users'].create({
            'name': 'SH MCP Restricted Owner',
            'login': 'sh_mcp_restricted_owner',
            'email': 'sh_mcp_restricted_owner@example.test',
        })
        owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, owner.groups_id)

        ws = self.Workspace.create({
            'name': 'MCP Restricted WS',
            'owner_id': owner.id,
        })
        home = self.SmartHome.create({
            'name': 'HA restricted owner',
            'workspace_id': ws.id,
        })
        sub = self.Subscription.sudo().create({
            'sh_partner_id': owner.partner_id.id,
            'product_id': self.product[self.plan_mcp.id].product_variant_id.id,
            'sh_subscription_plan_id': self.plan_mcp.id,
            'sh_source': 'manual',
        })
        home.sudo().subscription_id = sub.id
        self.assertTrue(home.subscription_id)

        owner_home = home.with_user(owner)
        # 直呼 getter，fresh cache：不經 to_dict()——後者的 sibling
        # _get_subscription_info() 會先 self.sudo() 讀 subscription_id 而 prime
        # cache，讓 getter 的非 sudo 讀命中暖 cache、遮掉 ACL 檢查。要真正驗到
        # getter 自身的 self.sudo()，必須 invalidate 後直呼 getter。
        self.env.invalidate_all()
        self.assertEqual(owner_home._get_mcp_integration(), 'ha_mcp_tools')
        # 附帶（wire 級）：整個 to_dict() 對受限 owner 也不拋、mcp 正確。
        self.env.invalidate_all()
        self.assertEqual(owner_home.to_dict()['mcp'], 'ha_mcp_tools')
