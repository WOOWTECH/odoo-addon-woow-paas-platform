"""teardown 之後 `tunnel_status` 被凍結在 `connected`（#833 順帶修的一格）。

這是 #833 那一族裡**最刺眼**的一格，而且與 operator 無關、純平台側：

平台**唯一百分之百確定「這條 tunnel 已經不存在」的場景，就是它自己刪的**——
`_cleanup_tunnel()` / `_cleanup_cloudflare()` 成功之後。但那兩支只清 identity
anchor，**不碰 `tunnel_status`**；而 `action_refresh_status()` 的第一行是
`if not self.tunnel_id: return` ⇒ `tunnel_id` 一旦被清掉，那個欄位就再也**沒有任何
路徑**能更新它 —— 它被凍結在刪除前的最後一個值，通常是 `connected`（綠燈）。

SA 詳情頁上因此會出現兩顆同源 badge 說不同的話：「Tunnel Information」整張卡掛
`t-if="state.sa.tunnel_id"`（tunnel_id 清掉就整卡消失），但 Status 卡裡的
「Connector」badge 沒有 `t-if`，會繼續渲染那顆凍結的綠燈。

⚠ 這一族的驗收紀律同 `test_tunnel_soft_delete_833.py`：陽性對照（**失敗**的
teardown 必須維持原狀、不得被標成 deleted）與判別式同等重要——否則「不再是
connected」可能只是把成功與失敗兩條路一起弄壞了。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)

from .smart_home_fixture import sh_model

SA_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)
SH_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.'
    'get_paas_operator_client'
)


class TestSecurityAccessStatusAfterTeardown(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SA teardown WS'})
        self.SA = self.env['woow_paas_platform.security_access']

    def _sa(self):
        sa = self.SA.create({
            'name': 'SA teardown',
            'workspace_id': self.workspace.id,
        })
        _internal(sa).write({
            'state': 'active',
            'tunnel_id': 'tun-teardown',
            'tunnel_name': 'paas-sa-teardown',
        })
        sa.write({'tunnel_status': 'connected'})
        # 前提斷言：沒有這個前提，下面的判別式會在「本來就不是 connected」上假綠。
        self.assertEqual(sa.tunnel_status, 'connected', '前提沒建立起來')
        return sa

    def test_successful_teardown_stops_reporting_connected(self):
        """🔴 判別式：tunnel 被我們自己刪掉之後，不得繼續顯示 connected。"""
        sa = self._sa()
        with patch(SA_CLIENT) as get_client:
            get_client.return_value = MagicMock()
            sa._cleanup_cloudflare()

        self.assertFalse(sa.tunnel_id, 'teardown 沒成功，判別式沒走到該走的分支')
        self.assertEqual(
            sa.tunnel_status, 'deleted',
            "tunnel 已經被我們自己刪掉了，畫面卻還停在刪除前的值 —— 而且因為 "
            "action_refresh_status() 開頭就 `if not self.tunnel_id: return`，"
            "這個值再也沒有任何路徑能更新它",
        )

    def test_failed_teardown_leaves_everything_alone(self):
        """陽性對照：teardown **失敗**時 tunnel 還活著 ⇒ 狀態不得被動。

        沒有這條，上面那支可能只是把 `deleted` 無條件寫死在 cleanup 開頭 ——
        那會把「還活著、待重試」謊報成「已刪除」，比原本的 bug 更糟。
        """
        sa = self._sa()
        with patch(SA_CLIENT) as get_client:
            client = MagicMock()
            client.delete_tunnel.side_effect = RuntimeError('cf down')
            get_client.return_value = client
            sa._cleanup_cloudflare()

        self.assertEqual(sa.tunnel_id, 'tun-teardown', 'tunnel_id 必須留給重試')
        self.assertEqual(
            sa.tunnel_status, 'connected',
            'teardown 失敗 ⇒ tunnel 還在 CF 上，不得謊報成已刪除',
        )


class TestSmartHomeStatusAfterTeardown(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'SH teardown WS'})
        self.SH = sh_model(self.env)

    def _sh(self):
        sh = self.SH.create({
            'name': 'SH teardown',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-sh-teardown',
            'tunnel_name': 'paas-sm-teardown',
            'state': 'active',
        })
        sh.write({'tunnel_status': 'connected'})
        self.assertEqual(sh.tunnel_id, 'tun-sh-teardown', '前提沒建立起來')
        self.assertEqual(sh.tunnel_status, 'connected', '前提沒建立起來')
        return sh

    def test_successful_teardown_stops_reporting_connected(self):
        """🔴 判別式（SmartHome 側）。"""
        sh = self._sh()
        with patch(SH_CLIENT) as get_client:
            get_client.return_value = MagicMock()
            sh._cleanup_tunnel()

        self.assertFalse(sh.tunnel_id, 'teardown 沒成功，判別式沒走到該走的分支')
        self.assertEqual(sh.tunnel_status, 'deleted')

    def test_failed_teardown_leaves_everything_alone(self):
        """陽性對照（SmartHome 側）。"""
        sh = self._sh()
        with patch(SH_CLIENT) as get_client:
            client = MagicMock()
            client.delete_tunnel.side_effect = RuntimeError('cf down')
            get_client.return_value = client
            sh._cleanup_tunnel()

        self.assertEqual(sh.tunnel_id, 'tun-sh-teardown')
        self.assertEqual(sh.tunnel_status, 'connected')
