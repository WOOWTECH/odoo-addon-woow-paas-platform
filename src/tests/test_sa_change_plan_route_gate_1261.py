"""#1261：Security Access 換方案的 route 數降級 gate。

2026-09-29 production 瀏覽器 E2E 實撞：SA 在「3 組」方案（``sa_max_routes=3``）下
開了 3 條 route，降級到「1 組」（``sa_max_routes=1``）**成功** ⇒ 記錄永久停在
「3 / 1 routes」。route 配額守衛（``_check_route_quota``）是 route 寫入時才跑的
constraint，換方案不寫 route ⇒ 從來沒被叫醒。#773 的席位 gate 只管 Access 席數。

## 判別式設計（沿用 #773 ``test_change_plan_seat_gate_773`` 的原則）

1. **拒絕必須斷言「副作用都沒發生」**：舊訂閱仍 in_progress、方案沒換、錢包沒動、
   route 一條都沒被刪。只看 ``success is False`` 會被「gate 插在退點之後、整包回捲」
   或「擋住且順手刪 route」滿足。「錢包沒動」要有鑑別力，前提是這次換方案**真的會
   退點**：fixture 因此照 SH 的 ``test_change_plan_protection_gate_1261`` 補一筆
   ``done`` 的 ``deduct``、方案 ``sh_trial=False``，並在拒絕那條先斷言
   ``_compute_unused_refund() > 0``（#324：沒成功扣款就退 0，錢包斷言恆綠）。
2. **拒絕必須是 route gate 擋的**：斷言 marker ＋ ``data.current_routes`` /
   ``target_routes``。fixture 刻意**沒有任何 access policy** ⇒ #773 席位 gate
   （用量 0）一定放行，紅綠不會被它混淆。
3. **「應放行」一律斷言到具體結果**（``success`` ＋ 新方案真的掛上），不是斷言
   「沒有 route marker」——後者被任何提前 return 滿足。
4. **0 ＝一條都不准，不是 unlimited**：``sa_max_routes`` 的 0 語意與相鄰
   ``sa_max_access_policies`` 刻意相反；有人「順手對齊」時 zero 那兩條會紅。
5. **順序（gate 在第一個寫入之前）用 AST 釘**：理由同 #773
   ``TestSeatGateIsWiredBeforeFirstWrite``——端到端看不出「先退點再整包回捲」。
"""
import ast
import pathlib
from unittest.mock import MagicMock, patch
from .publication_receipt_fixture import routes_receipt

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)

from .test_security_access_subscription_flow import (
    MOCK_GET_CLIENT,
    MOCK_GET_CLIENT_MODEL,
    _patch_request,
)

# gate 兩則訊息各自的 marker（⛔ 皆非 route 寫入守衛
# 「Route quota exceeded」的子字串／超字串，兩者才分得開）
ROUTE_GATE_MARKER = 'Delete routes until at most'
ROUTE_GATE_MARKER_ZERO = 'Delete every route before changing'
ROUTE_GUARD_MARKER = 'Route quota exceeded'


class _RouteGateFixture:
    """一個掛在 template 上、訂閱「3 組」方案的 SA，外加一組目標方案。

    刻意**不**繼承 ``TransactionCase``、本 class 內**不得**有 ``test_*``——理由同
    ``test_ac_access_seat_enforcement_521._SeatFixture``（loader 會把抽象基底也收去跑；
    mixin 裡的 test 一條都不會被執行）。

    SA 刻意停在預設 state、沒有 tunnel：route 的 create override 因此跳過 operator
    sync（同 ``test_route_quota_enforced_at_orm_backstop`` 的作法）；gate 只看 route
    數與目標 plan，與 SA 是否 active 無關。operator client 仍一律 mock，避免哪天
    create／換方案路徑多了一個 operator 呼叫就漏到真實 HTTP。
    """

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.owner = self.env['res.users'].create({
            'name': 'SA Route Gate Owner', 'login': 'sa_route_gate_1261',
            'email': 'sa_route_gate_1261@test.example',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA Route Gate WS', 'owner_id': self.owner.id,
        })
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.owner.id, 'role': 'owner'})
        # 方案名照 prod 的「N 組」；每支都帶 product（`_create_and_confirm_subscription`
        # 找不到 product 會 raise ⇒ 「應放行」那幾條會 ERROR 而不是紅得有意義）。
        self.plan_3 = self._plan('Gate 1261 3 routes', 300, 3)
        self.plan_1 = self._plan('Gate 1261 1 route', 100, 1)
        self.plan_5 = self._plan('Gate 1261 5 routes', 500, 5)
        self.plan_0 = self._plan('Gate 1261 0 routes', 50, 0)
        self.template = self.env[
            'woow_paas_platform.security_access_template'].create({
                'name': 'SA Route Gate Tmpl',
                'subscription_plan_ids': [(6, 0, [
                    self.plan_3.id, self.plan_1.id,
                    self.plan_5.id, self.plan_0.id])],
            })
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        self.mc = MagicMock()
        self.mc.sync_tunnel_routes.side_effect = routes_receipt
        self.mc.sync_access_app.return_value = {
            'app_id': 'app-1261', 'policy_ids': [], 'allowed_idps': []}
        self.sa = self.SecurityAccess.create({
            'name': 'Route Gate GW', 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.partner = self.workspace.owner_id.partner_id
        self.sa._create_and_confirm_subscription(self.partner, self.plan_3)
        self.old_sub = self.sa.sudo().subscription_id
        self.assertEqual(self.old_sub.state, 'in_progress')
        self.assertEqual(self.old_sub.sh_subscription_plan_id, self.plan_3)
        # 鑑別力前提（#1261 review）：新 owner 錢包是 0、付不出 300，確認訂閱時
        # 不會寫 done deduct ⇒ `_has_paid_current_cycle()` 為 False ⇒ 退點恆 0，
        # 「gate 擋在退點之前」的錢包斷言就證明不了任何事。照 SH 版 fixture
        # 補一筆本訂閱的成功扣款，讓換方案時真的會 `refund_wallet_points`。
        self.WalletTx = self.env['wallet.transaction']
        self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -self.old_sub.sh_plan_price,
            'subscription_id': self.old_sub.id, 'state': 'done',
        })

    def _plan(self, name, price, max_routes):
        plan = self.env['sh.subscription.plan'].create({
            'name': name, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': price, 'sh_no_of_billing_cycle': -1,
            # sh_trial=False：試用期內 `_prorata_unused_refund` 退 0（start 在
            # 未來），錢包斷言同樣失去鑑別力——與 SH 版 fixture 一致。
            'sh_trial': False,
            'sa_max_routes': max_routes, 'service_type': 'security_access',
        })
        self.env['product.product'].create({
            'name': name + ' Product', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
        })
        return plan

    def _add_routes(self, n):
        with patch(MOCK_GET_CLIENT, return_value=self.mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=self.mc):
            for i in range(n):
                self.Route.create({
                    'security_access_id': self.sa.id,
                    'subdomain_prefix': 'gate1261-r%d' % i,
                })
        self.assertEqual(len(self.sa.sudo().route_ids), n, '前置：route 數')

    def _change_plan(self, plan):
        self.env.invalidate_all()
        with patch(MOCK_GET_CLIENT, return_value=self.mc), \
                patch(MOCK_GET_CLIENT_MODEL, return_value=self.mc), \
                _patch_request(self.env(user=self.owner)):
            return self.controller.api_sa_change_plan(
                workspace_id=self.workspace.id, sa_id=self.sa.id,
                plan_id=plan.id)

    def _balance(self):
        self.partner.sudo().invalidate_recordset(['wallet_balance'])
        return self.partner.sudo().wallet_balance

    def _refund_count(self):
        return self.WalletTx.search_count([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])


class TestSaRouteGateRuler(_RouteGateFixture, TransactionCase):
    """``_route_gate_denial`` 本身：比較子、目標 plan、0 的語意、不刪東西。"""

    allow_inherited_tests_method = True

    def test_boundary_is_strict_greater_than(self):
        """``routes == cap`` 放行、``routes == cap + 1`` 擋——與 ``_check_route_quota``
        的 ``count > quota`` 同一個比較子。"""
        self._add_routes(3)
        self.assertIsNone(self.sa._route_gate_denial(self.plan_3),
                          'route 數等於 cap 應放行（比較子必須是嚴格 >）')
        denial = self.sa._route_gate_denial(self.plan_1)
        self.assertIsNotNone(denial, '3 條 > cap 1 應被擋')
        self.assertEqual(denial['current_routes'], 3)
        self.assertEqual(denial['target_routes'], 1)
        self.assertIn(ROUTE_GATE_MARKER, denial['error'], denial)

    def test_gate_uses_target_plan_not_current_plan(self):
        """🔴 gate 必須拿**目標** plan 的 cap，⛔ 不是 SA 當下的（``_route_quota()``）。

        SA 現在掛 3 組、有 3 條（合規），目標 1 組。若 gate 讀當下方案會判合規、
        放行——那正是 prod 撞到的「3 / 1 routes」本身，而升級／合規放行的測試照樣全綠。
        """
        self._add_routes(3)
        self.assertEqual(self.sa._route_quota(), 3, '前置：當下方案 3 組')
        self.assertIsNotNone(self.sa._route_gate_denial(self.plan_1),
                             '拿目標方案（1）比對 route 數（3）必須擋')

    def test_zero_routes_is_none_allowed_not_unlimited(self):
        """``sa_max_routes == 0`` ＝一條都不准（欄位 help「0 = none allowed」）。

        ⚠ 相鄰 ``sa_max_access_policies`` 的 0 **是** unlimited（#743），刻意相反；
        有人「順手對齊」成 ``if not quota: return None`` 時本條會紅。
        """
        self._add_routes(1)
        denial = self.sa._route_gate_denial(self.plan_0)
        self.assertIsNotNone(denial, '0 必須擋，不得當成 unlimited')
        self.assertIn(ROUTE_GATE_MARKER_ZERO, denial['error'], denial)
        self.assertEqual(denial['target_routes'], 0)
        self.assertEqual(denial['current_routes'], 1)

    def test_zero_routes_plan_accepts_sa_without_routes(self):
        """0 條的 SA 換到 0 組方案應放行（陽性對照，避免「0 一律擋」也讓上一條綠）。"""
        self.assertFalse(self.sa.sudo().route_ids, '前置：沒有 route')
        self.assertIsNone(self.sa._route_gate_denial(self.plan_0))

    def test_gate_message_is_not_the_write_guard_message(self):
        """gate 與 route 寫入守衛的訊息互不包含 ⇒ 斷言分得出是誰擋的。"""
        self._add_routes(3)
        for plan in (self.plan_1, self.plan_0):
            msg = self.sa._route_gate_denial(plan)['error']
            self.assertNotIn(ROUTE_GUARD_MARKER, msg)

    def test_gate_denial_deletes_nothing(self):
        """🔴 gate 只回報，⛔ 不刪任何 route（同 #773 裁決：由租戶自己刪）。"""
        self._add_routes(3)
        before = self.sa.sudo().route_ids.ids
        self.assertIsNotNone(self.sa._route_gate_denial(self.plan_1))
        self.sa.invalidate_recordset(['route_ids'])
        self.assertEqual(self.sa.sudo().route_ids.ids, before,
                         'gate 不得刪除任何 route')


class TestSaChangePlanRouteGateEndpoint(_RouteGateFixture, TransactionCase):
    """``api_sa_change_plan`` 端到端：prod 重現情境被擋、合規與升級照常放行。"""

    allow_inherited_tests_method = True

    def test_downgrade_3_routes_to_1_route_plan_refused_before_side_effects(self):
        """prod 重現：3 條 route、3 組 → 1 組 ⇒ 結構化拒絕，且**什麼都沒發生**。"""
        self._add_routes(3)
        # 前提：這次換方案若放行**會**退點 ⇒ 下面的錢包／退點斷言才分得出
        # 「gate 在退點之前」與「gate 在退點之後（或沒 gate）」。
        self.assertGreater(self.old_sub._compute_unused_refund(), 0,
                           'fixture 前提：舊訂閱要有可退點數，否則錢包斷言恆綠')
        balance_before = self._balance()
        refunds_before = self._refund_count()
        routes_before = self.sa.sudo().route_ids.ids

        res = self._change_plan(self.plan_1)

        self.assertFalse(res['success'], res)
        self.assertIn(ROUTE_GATE_MARKER, res.get('error') or '',
                      '擋是擋了，但不是 route gate 擋的：%s' % res)
        self.assertEqual(res.get('data'),
                         {'current_routes': 3, 'target_routes': 1}, res)
        # 擋在副作用之前：舊訂閱沒關、方案沒換、錢包沒動、route 沒刪
        self.old_sub.invalidate_recordset(['state'])
        self.assertEqual(self.old_sub.state, 'in_progress',
                         'gate 應擋在關閉舊訂閱之前')
        self.sa.invalidate_recordset(['subscription_id', 'route_ids'])
        self.assertEqual(self.sa.sudo().subscription_id, self.old_sub,
                         '不得開出新訂閱')
        self.assertEqual(
            self.sa.sudo().subscription_id.sh_subscription_plan_id, self.plan_3)
        self.assertEqual(self._balance(), balance_before, 'gate 應擋在退點之前')
        self.assertEqual(self._refund_count(), refunds_before,
                         'gate 應擋在退點之前：不得寫出 refund 交易')
        self.assertEqual(self.sa.sudo().route_ids.ids, routes_before,
                         'gate ⛔ 不得刪除任何 route')

    def test_downgrade_to_zero_route_plan_refused(self):
        """有 route 時換到 0 組方案被擋（0 ＝不准，不是 unlimited）。"""
        self._add_routes(1)
        res = self._change_plan(self.plan_0)
        self.assertFalse(res['success'], res)
        self.assertIn(ROUTE_GATE_MARKER_ZERO, res.get('error') or '', res)
        self.assertEqual(res.get('data'),
                         {'current_routes': 1, 'target_routes': 0}, res)
        self.assertEqual(
            self.sa.sudo().subscription_id.sh_subscription_plan_id, self.plan_3)

    def test_downgrade_allowed_when_routes_fit_target(self):
        """route 數 ≤ 目標 cap 的降級照常成功（1 條、3 組 → 1 組）。

        ⚠ 沒有這條，「SA change-plan 一律拒絕」也會讓上面兩條綠。
        """
        self._add_routes(1)
        refunds_before = self._refund_count()
        res = self._change_plan(self.plan_1)
        self.assertTrue(res['success'], res.get('error'))
        self.sa.invalidate_recordset(['subscription_id'])
        self.assertEqual(
            self.sa.sudo().subscription_id.sh_subscription_plan_id, self.plan_1)
        self.assertEqual(len(self.sa.sudo().route_ids), 1, 'route 不得被動到')
        # 陽性對照（#1261 review）：放行路徑確實退點 ⇒ fixture 的退點前提是活的，
        # 拒絕那條的「沒有 refund」才有意義。
        self.assertGreater(res['data']['refund_amount'], 0, res)
        self.assertEqual(self._refund_count(), refunds_before + 1,
                         '放行的換方案應寫出一筆 refund 交易')

    def test_upgrade_passes_without_downgrade_classification(self):
        """升級天然通過（3 條、3 組 → 5 組）⇒ 實作不需要先判「是不是降級」。"""
        self._add_routes(3)
        res = self._change_plan(self.plan_5)
        self.assertTrue(res['success'], res.get('error'))
        self.sa.invalidate_recordset(['subscription_id'])
        self.assertEqual(
            self.sa.sudo().subscription_id.sh_subscription_plan_id, self.plan_5)


class TestSaRouteGateIsWiredBeforeFirstWrite(TransactionCase):
    """``api_sa_change_plan`` 呼叫 route gate，且呼叫點排在第一個寫入（退點）之前。

    靜態判準，理由同 #773 ``TestSeatGateIsWiredBeforeFirstWrite``：gate 就算被移到
    退點之後，只要交易整包回捲，端到端看到的仍是「被擋了、資料沒變」。
    """

    def _func_node(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        tree = ast.parse((root / 'controllers/security_access.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) \
                    and node.name == 'api_sa_change_plan':
                return node
        self.fail('controllers/security_access.py 找不到 api_sa_change_plan')

    @staticmethod
    def _first_line_calling(func_node, attr_name):
        lines = [n.lineno for n in ast.walk(func_node)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == attr_name]
        return min(lines) if lines else None

    def test_route_gate_precedes_refund(self):
        node = self._func_node()
        gate_line = self._first_line_calling(node, '_route_gate_denial')
        write_line = self._first_line_calling(node, 'refund_wallet_points')
        self.assertIsNotNone(gate_line, 'api_sa_change_plan 沒有呼叫 route gate')
        self.assertIsNotNone(write_line, '找不到 refund_wallet_points：錨點失效')
        self.assertLess(gate_line, write_line,
                        'route gate 必須排在第一個寫入（退點）之前')
