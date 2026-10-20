"""商用驗收 AI-HMCP-002-4：0 席方案的服務能經 access-config 端點開 ``is_protected``，結果把 MCP client 鎖在門外。

重現（prod 2026-10-16，QA uid 205 的 Hermes MCP 服務 249，方案「Hermes MCP 伺服器 T1」，``ac_max_access_seats=0``）：

- ``access-policies create``（email 白名單）被席位尺擋下：「allows 0 Access seat(s)」。
- 但 ``access-config {is_protected: true, login_email_otp: true}`` **成功**（一條 policy 都沒有）。
- 開啟後，匿名請求與帶有效 ``/private_<token>/mcp`` 的 MCP 請求全部被 Cloudflare Access 302 導去登入頁。
  0 席方案加不了任何可登入的 email，MCP client 也沒有 service token 可用，整個 MCP 對外不可用。

0 席方案在前端本來就沒有 Access 頁籤（``ac_max_access_seats > 0`` 才顯示，#746 拍板 6／#521 留言 9960 §4：
「0 席＝一席都沒有，要用就加購」），換到 0 席時也會被 #1261 的 gate 擋（仍受保護 ⇒ 不准換）。
這個端點是唯一能做出「0 席＋受保護」的路，所以在端點擋住「開啟」方向：

- 0 席方案：關 → 開，拒絕，``data.code == 'access_seats_required'``，什麼都不寫、不打 operator。
- 有席位的方案、沒有訂閱（grandfathered）：照舊放行。
- 關閉方向永遠放行（#1261 的復原通道）；已經受保護時重送 True（其他欄位一起存）不擋。

判別式：「擋住」一律連同「沒寫入、沒打 operator」一起斷言；「放行」斷言到 ``is_protected`` 真的變了。
"""
from unittest.mock import patch

from odoo.tests import tagged

from ..models.cloud_service import _cs_skip_access_sync
from .test_cs_access_endpoints_745 import MOCK_GET_CLIENT, _CsAccessEndpointFixture, _sync_result

SEAT_REQUIRED_CODE = 'access_seats_required'


@tagged('post_install', '-at_install')
class TestCsZeroSeatProtectionEnable(_CsAccessEndpointFixture):
    """CS：0 席方案不能經 access-config 開啟保護。"""

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan'].sudo()
        self.plan_zero = self._plan('HMCP0024 T1', 0)
        self.plan_seat = self._plan('HMCP0024 T1 +1access', 1)

    def _plan(self, name, seats):
        plan = self.Plan.create({
            'name': name, 'sh_plan_price': 100.0,
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1, 'sh_trial': False,
            'sa_max_access_policies': 10, 'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': seats,
        })
        # `_create_and_confirm_subscription` 以 product.template 反查 plan
        self.env['product.template'].sudo().create({
            'name': name + ' P', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': 100.0,
        })
        return plan

    def _subscribe(self, plan):
        self.svc.sudo()._create_and_confirm_subscription(self.alice.partner_id, plan)
        self.assertEqual(self.svc.sudo().subscription_id.sh_subscription_plan_id, plan, 'fixture 前提')

    def test_zero_seat_plan_cannot_turn_protection_on(self):
        self._subscribe(self.plan_zero)
        self.assertFalse(self.svc.is_protected, '前提：保護關著')
        self.authenticate(self.alice_login, self.alice_login)
        client = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=client):
            result = self._access_config({'is_protected': True, 'login_email_otp': True})
        self.assertFalse(result.get('success'), '0 席方案開保護必須被擋（AI-HMCP-002-4 的洞），回 %r' % (result,))
        self.assertEqual((result.get('data') or {}).get('code'), SEAT_REQUIRED_CODE, result)
        self.assertIn('Access seat', result.get('error') or '', result)
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected, '被擋的請求不能寫入 is_protected')
        self.assertFalse(self.svc.login_email_otp, '被擋的請求連同其他欄位都不寫')
        client.sync_access_app.assert_not_called()

    def test_seated_plan_can_turn_protection_on(self):
        """陽性對照：有席位的方案照舊開得起來。"""
        self._subscribe(self.plan_seat)
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            result = self._access_config({'is_protected': True})
        self._assert_ok(result, '有席位的方案開保護')
        self.svc.invalidate_recordset()
        self.assertTrue(self.svc.is_protected)

    def test_unsubscribed_service_is_unchanged(self):
        """陽性對照：沒有訂閱（grandfathered）照舊放行，與席位尺的 grandfather 同款。"""
        self.assertFalse(self.svc.sudo().subscription_id, '前提：沒有訂閱')
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            result = self._access_config({'is_protected': True})
        self._assert_ok(result, '沒有訂閱的服務開保護')
        self.svc.invalidate_recordset()
        self.assertTrue(self.svc.is_protected)

    def test_zero_seat_plan_can_still_turn_protection_off(self):
        """#1261 復原通道：已卡在「0 席＋受保護」的服務，關閉方向一定放行；重送 True 也不擋。"""
        self._subscribe(self.plan_zero)
        _cs_skip_access_sync(self.svc.sudo()).write({'is_protected': True})  # 只搬 DB 狀態（舊的卡住記錄）
        self.authenticate(self.alice_login, self.alice_login)
        with patch(MOCK_GET_CLIENT, return_value=self._mock()):
            again = self._access_config({'is_protected': True, 'session_duration': '8h'})
        self.assertNotEqual((again.get('data') or {}).get('code'), SEAT_REQUIRED_CODE,
                            '已受保護時重送 True 不是「開啟」，不能被這條擋：%r' % (again,))
        # 關閉方向的 operator 回執：app 拆掉（app_id=None、沒有 policy）
        with patch(MOCK_GET_CLIENT, return_value=self._mock(result=_sync_result(app_id=None))):
            result = self._access_config({'is_protected': False})
        self._assert_ok(result, '0 席方案關保護')
        self.svc.invalidate_recordset()
        self.assertFalse(self.svc.is_protected)
