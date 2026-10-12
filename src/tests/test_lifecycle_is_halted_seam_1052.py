# -*- coding: utf-8 -*-
"""#1052：SmartHome／SecurityAccess 的 ``_do_resume`` guard 必須問 mixin 的 ``_is_halted()``。

背景
----
``subscription_lifecycle_mixin._is_halted()`` 是 lifecycle cron 決定「要不要復權」的共用
seam（``subscription_lifecycle_cron.py`` 對三個 model 一律先問它再呼叫 ``_do_resume``；
CloudService 為 #482/#497 override 成 ``state == 'suspended' or bool(pre_suspend_state)``）。
SmartHome／SecurityAccess 的 ``_do_resume`` 卻各自內聯了 ``if self.state != 'suspended':
return``——今天兩者字面相等（mixin 預設就是那一句，docstring 明寫「對它們是零行為變化」），
但形狀是「cron 問 seam、hook 再用 literal 問第二次」：哪天 SM／SA 也要 override
``_is_halted``，cron 會呼叫 resume 而 hook 靜默 no-op——正是 #482「租戶付了款也回不來」
的形狀。

#1052 的 reachability trace 找到真的路徑會讓 SM／SA 的 state 在停權中被寫離
``'suspended'``：兩個 model 的 ``_do_cleanup`` 拆 tunnel 失敗都寫 ``error``（cron branch 4
對 cancelled 訂閱先 suspend 再 cleanup）；``smart_home.py`` ``action_provision`` docstring
自陳「``/api/smart-homes`` 的 ``action='provision'`` 沒有 state 閘」（對**從未 provision**
的 suspended SM 可達；已有 ``tunnel_id`` 的會被 stored-id guard 擋）。本 PR **不**替
SM／SA 加 override（那是行為變更、要另案裁決），只把 guard 接回 seam，讓將來的 override
是一行而不是兩處內聯再改。

RED 預測（在 origin/develop 上跑本檔）
----------------------------------------
* ``test_resume_proceeds_when_seam_says_halted``：``state='error'``、``_is_halted`` 被
  patch 成 True ⇒ 內聯 guard ``state != 'suspended'`` 提前 return ⇒ operator／sync 沒被
  呼叫、state 仍是 ``'error'`` ⇒ **紅**。修法後 guard 改問 ``_is_halted()`` ⇒ 綠。
* ``test_resume_stops_when_seam_says_not_halted``：``state='suspended'``、``_is_halted``
  被 patch 成 False ⇒ 內聯 guard 放行（literal 看到 suspended）⇒ 有外呼 ⇒ **紅**。
  修法後 seam 說不是 halted ⇒ 不外呼 ⇒ 綠。
兩條方向相反：只有第一條時，一個「無條件放行」的壞修法也會綠；只有第二條時，
「無條件早退」也會綠。兩條同時綠才證明 guard 的判準來源真的是 ``_is_halted()``。

⚠ 只 patch model 自己的方法（``_get_operator_client``／``_sync_routes_to_operator``／
``_sync_all_routes_access``），不碰 ``get_paas_operator_client`` 的兩個 namespace
（docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）——本測試守的是 guard
的判準來源，不是 wire；也不用 ``assertRaises`` 區塊包 DB 斷言
（docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md）。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _sa_internal,
)
from .smart_home_fixture import open_gates, sh_model
from .publication_receipt_fixture import install_publication_receipts


@tagged('post_install', '-at_install')
class TestSmartHomeResumeConsultsSeam(TransactionCase):
    """SmartHome._do_resume 的 guard 判準來源必須是 mixin._is_halted()。"""

    def setUp(self):
        super().setUp()
        workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'Seam SM WS'})
        # #524 建立閘＋#522 身分錨點閘：fixture 慣例，見 smart_home_fixture.py。
        self.sm = sh_model(self.env).create({
            'name': 'Seam Home',
            'workspace_id': workspace.id,
        })
        open_gates(self.sm).write({'tunnel_id': 'tun-seam-sm'})
        self.client = install_publication_receipts(MagicMock())

    def _resume(self, state, halted):
        self.sm.write({'state': state, 'pre_suspend_state': 'active'})
        with patch.object(type(self.sm), '_get_operator_client',
                          return_value=self.client), \
                patch.object(type(self.sm), '_is_halted', return_value=halted):
            self.sm._do_resume()

    def test_resume_proceeds_when_seam_says_halted(self):
        """seam 說 halted ⇒ 即使 state 不是 'suspended' 也要復權（#482 形狀）。"""
        self._resume(state='error', halted=True)
        self.assertTrue(
            self.client.configure_tunnel.called,
            'guard 沒有問 _is_halted()：state=error 時內聯 literal 提前 return，'
            'tunnel 沒被還原（RED 預測就是這一格）')
        self.assertEqual(self.sm.state, 'active')
        self.assertFalse(self.sm.pre_suspend_state)

    def test_resume_stops_when_seam_says_not_halted(self):
        """seam 說不是 halted ⇒ 即使 state 恰好是 'suspended' 也不得外呼。"""
        self._resume(state='suspended', halted=False)
        self.assertFalse(
            self.client.configure_tunnel.called,
            'guard 沒有問 _is_halted()：state=suspended 時內聯 literal 放行，'
            '外呼發生了（RED 預測就是這一格）')
        self.assertEqual(self.sm.state, 'suspended')


@tagged('post_install', '-at_install')
class TestSecurityAccessResumeConsultsSeam(TransactionCase):
    """SecurityAccess._do_resume 的 guard 判準來源必須是 mixin._is_halted()。"""

    def setUp(self):
        super().setUp()
        workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'Seam SA WS'})
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Seam Gateway',
            'workspace_id': workspace.id,
        })
        # tunnel_id 是 #522 身分錨點，只能經 sentinel 開閘寫入。有 tunnel 才會走
        # 到 sync 那條分支（沒 tunnel 的分支只把 state 還原成 pending）。
        _sa_internal(self.sa).write({'state': 'active',
                                     'tunnel_id': 'tun-seam-sa'})

    def _resume(self, state, halted):
        self.sa.write({'state': state, 'pre_suspend_state': 'active'})
        with patch.object(type(self.sa), '_sync_routes_to_operator') as sync_routes, \
                patch.object(type(self.sa), '_sync_all_routes_access') as sync_access, \
                patch.object(type(self.sa), '_is_halted', return_value=halted):
            self.sa._do_resume()
        return sync_routes, sync_access

    def test_resume_proceeds_when_seam_says_halted(self):
        """seam 說 halted ⇒ 即使 state 不是 'suspended' 也要復權（#482 形狀）。"""
        sync_routes, sync_access = self._resume(state='error', halted=True)
        self.assertTrue(
            sync_routes.called,
            'guard 沒有問 _is_halted()：state=error 時內聯 literal 提前 return，'
            'route 沒被重推（RED 預測就是這一格）')
        self.assertTrue(sync_access.called)
        self.assertEqual(self.sa.state, 'active')
        self.assertFalse(self.sa.pre_suspend_state)

    def test_resume_stops_when_seam_says_not_halted(self):
        """seam 說不是 halted ⇒ 即使 state 恰好是 'suspended' 也不得外呼。"""
        sync_routes, sync_access = self._resume(state='suspended', halted=False)
        self.assertFalse(
            sync_routes.called,
            'guard 沒有問 _is_halted()：state=suspended 時內聯 literal 放行，'
            '外呼發生了（RED 預測就是這一格）')
        self.assertFalse(sync_access.called)
        self.assertEqual(self.sa.state, 'suspended')
