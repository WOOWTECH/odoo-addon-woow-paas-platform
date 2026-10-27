"""#833（平台側）：軟刪除的 tunnel 必須與「還在、只是沒連上」可區分。

operator 端（`extra/paas-operator/`）在 #833 起會轉發 Cloudflare 的 `deleted_at`。
平台這一側要做的是**兩件事，缺一不可**：

1. `deleted_at` 為真 ⇒ `tunnel_status='deleted'`（新增的第四格）。
2. 🔴 `deleted_at` **falsy 一律落回既有判斷**。falsy 有兩種來源，兩種都**不是**
   訊號：
   * key 整個缺席 ＝ **舊版 operator** 還沒有這個欄位；
   * 值為 `null` ＝ CF 對**未刪除**的 tunnel 就是這樣回的。

   通則見 `docs/tracking/pitfalls/operator-route-none-conflates-disabled-and-failed.md`：
   「欄位不存在不是失敗訊號，只是這一版／這個設定沒有這個資訊」。把缺席當成
   「不確定 ⇒ 標成 deleted 或 error」會在升級當下讓每一列一起翻。

⚠ 這一族 bug 的驗收紀律：壞掉的樣子與正常的樣子一致 ⇒ 陽性對照（真的沒連上**仍然**
是 `disconnected`）與判別式同等重要，否則「不再回 disconnected」可能只是把兩種狀態
一起弄壞了。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)

# #524：SmartHome 的建立閘 ＋ 身分錨點閘。本檔建的是「已存在的 smart_home」這個
# **前提**（要測的是 refresh 的狀態推導），不是建立路徑本身 ⇒ 走既有的開閘 helper。
# ⛔ 不開錨點閘的話 `tunnel_id` 會被 `_strip_identity_anchors` **靜默**丟掉，
#    `action_refresh_status()` 第一行就 `if not self.tunnel_id: return` ⇒ 整支
#    測試會在一個「什麼都沒做」的前提上跑完並顯示綠。
from .smart_home_fixture import sh_model

SA_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)
SH_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.'
    'get_paas_operator_client'
)

DELETED_AT = '2026-08-22T06:44:52.396846Z'
LIVE_CONN = {
    'connector_id': 'conn-1',
    'type': 'cloudflared',
    'opened_at': '2026-08-01T01:00:00Z',
}


def _client(payload):
    c = MagicMock()
    c.health_check.return_value = {'instance_epoch': 'epoch-test'}
    c.get_tunnel_status.return_value = payload
    return c


class TestSecurityAccessSoftDelete(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.workspace = self.Workspace.create({'name': 'SA 833 WS'})
        self.SA = self.env['woow_paas_platform.security_access']

    def _sa(self, tunnel_id='tun-833'):
        sa = self.SA.create({
            'name': 'SA 833',
            'workspace_id': self.workspace.id,
        })
        _internal(sa).write({'state': 'active', 'tunnel_id': tunnel_id})
        return sa

    def _refresh(self, payload):
        sa = self._sa()
        with patch(SA_CLIENT) as get_client:
            get_client.return_value = _client(payload)
            sa.action_refresh_status()
        return sa

    # ---- 判別式 -----------------------------------------------------------

    def test_soft_deleted_is_not_reported_as_disconnected(self):
        """🔴 判別式：軟刪除必須與「沒連上」分得開。

        未修時 `deleted_at` 被完全忽略 ⇒ `connections` 空 ⇒ 寫 `disconnected`，
        與下面 `test_genuinely_disconnected_...` 的結果**逐字相同** ⇒ 本支紅。
        """
        sa = self._refresh({'connections': [], 'deleted_at': DELETED_AT})
        self.assertEqual(
            sa.tunnel_status, 'deleted',
            "#833：tunnel 已在 Cloudflare 上被刪除，卻與「還在、只是沒連上」"
            "寫成同一個值 —— 使用者與維運看到的字一模一樣，不會有人去查",
        )

    def test_soft_deleted_clears_the_stale_connector_bookkeeping(self):
        """已刪除 ⇒ 上一輪的 connector 數字不得留在畫面上。

        混合世代（`Deleted` ＋ `Edge Connections: 4`）本身就是另一種「看起來
        正常」。
        """
        sa = self._sa()
        with patch(SA_CLIENT) as get_client:
            get_client.return_value = _client({'connections': [LIVE_CONN]})
            sa.action_refresh_status()
        self.assertEqual(sa.edge_connection_count, 1, '前置條件沒建立起來')

        with patch(SA_CLIENT) as get_client:
            get_client.return_value = _client(
                {'connections': [], 'deleted_at': DELETED_AT})
            sa.action_refresh_status()

        self.assertEqual(sa.tunnel_status, 'deleted')
        self.assertEqual(sa.edge_connection_count, 0)
        self.assertFalse(sa.connector_id)
        self.assertFalse(sa.connector_type)

    # ---- 陽性對照 ---------------------------------------------------------

    def test_genuinely_disconnected_is_still_disconnected(self):
        """真的沒連上（無 `deleted_at`）⇒ **仍然** `disconnected`。

        沒有這條，上面那支「不再是 disconnected」可能只是把兩種狀態一起弄壞。
        """
        sa = self._refresh({'connections': []})
        self.assertEqual(sa.tunnel_status, 'disconnected')

    def test_connected_is_unaffected(self):
        sa = self._refresh({'connections': [LIVE_CONN]})
        self.assertEqual(sa.tunnel_status, 'connected')
        self.assertEqual(sa.edge_connection_count, 1)

    # ---- 舊 operator / CF null：缺席不是訊號 -------------------------------

    def test_absent_deleted_at_key_falls_back_to_the_old_branch(self):
        """🔴 舊 operator 對照組：回應**沒有** `deleted_at` 這個 key。

        必須落回既有判斷，不得被當成「不確定」而標成 deleted 或 error。
        """
        sa = self._refresh({'connections': []})
        self.assertEqual(sa.tunnel_status, 'disconnected')

        sa2 = self._sa('tun-833-b')
        with patch(SA_CLIENT) as get_client:
            get_client.return_value = _client({'connections': [LIVE_CONN]})
            sa2.action_refresh_status()
        self.assertEqual(sa2.tunnel_status, 'connected')

    def test_null_deleted_at_means_not_deleted(self):
        """CF 對未刪除的 tunnel 回的是 `deleted_at: null`（欄位存在）。"""
        sa = self._refresh({'connections': [], 'deleted_at': None})
        self.assertEqual(sa.tunnel_status, 'disconnected')

    def test_operator_error_is_still_error_not_deleted(self):
        """operator／CF 掛掉 ⇒ 仍然 `error`。

        ⛔ 這一格刻意**不**映射成 `deleted`：平台看得到的只有「HTTP 失敗」，看不到
        那個失敗是誰發的。把它標成 deleted 會讓「operator_url 設錯」從吵鬧且正確的
        `error` 變成安靜且錯誤的「已刪除」—— 那是 #833 的鏡像。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        sa = self._sa()
        with patch(SA_CLIENT) as get_client:
            c = MagicMock()
            c.health_check.return_value = {'instance_epoch': 'epoch-test'}
            c.get_tunnel_status.side_effect = PaaSOperatorError(
                'Tunnel tun-833 not found', status_code=404)
            get_client.return_value = c
            sa.action_refresh_status()
        self.assertEqual(sa.tunnel_status, 'error')


class TestSmartHomeSoftDelete(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.workspace = self.Workspace.create({'name': 'SH 833 WS'})
        self.SH = sh_model(self.env)

    def _sh(self, tunnel_id='tun-sh-833'):
        sh = self.SH.create({
            'name': 'SH 833',
            'workspace_id': self.workspace.id,
            'tunnel_id': tunnel_id,
            'state': 'active',
        })
        # 前提斷言：`action_refresh_status()` 第一行是 `if not self.tunnel_id:
        # return` ⇒ 錨點沒落地的話整支測試會什麼都沒測到卻全綠。
        self.assertEqual(sh.tunnel_id, tunnel_id, '前提沒建立起來：tunnel_id 被剝掉了')
        return sh

    def _refresh(self, payload):
        sh = self._sh()
        with patch(SH_CLIENT) as get_client:
            get_client.return_value = _client(payload)
            sh.action_refresh_status()
        return sh

    def test_soft_deleted_is_not_reported_as_disconnected(self):
        """🔴 判別式（SmartHome 側）。"""
        sh = self._refresh({'connections': [], 'deleted_at': DELETED_AT})
        self.assertEqual(sh.tunnel_status, 'deleted')

    def test_genuinely_disconnected_is_still_disconnected(self):
        sh = self._refresh({'connections': []})
        self.assertEqual(sh.tunnel_status, 'disconnected')

    def test_connected_is_unaffected(self):
        sh = self._refresh({'connections': [LIVE_CONN]})
        self.assertEqual(sh.tunnel_status, 'connected')
        self.assertEqual(sh.connector_id, 'conn-1')

    def test_null_deleted_at_means_not_deleted(self):
        sh = self._refresh({'connections': [], 'deleted_at': None})
        self.assertEqual(sh.tunnel_status, 'disconnected')


class TestDeletedIsVisuallyDistinct(TransactionCase):
    """`deleted` 在 UI 上必須與 `disconnected` **不同色**。

    🔴 沒有這條，#833 會在 UI 層被原封不動地重做一次：`StatusBadge` 對未知 status
    的 fallback 是「label 用原字串、顏色用 `o_woow_status_pending`」—— 也就是跟
    `disconnected` **同一個顏色**。狀態值加了、但畫面上還是同一顆琥珀燈。
    """

    def _badge_src(self):
        from pathlib import Path

        from odoo.modules.module import get_module_path
        p = (Path(get_module_path('woow_paas_platform'))
             / 'static/src/paas/components/common/StatusBadge.js')
        return p.read_text(encoding='utf-8') if p.exists() else ''

    def test_status_badge_declares_deleted_with_its_own_colour(self):
        import re

        src = self._badge_src()
        self.assertTrue(src, 'StatusBadge.js not found —— 判別式壞了，不是真的沒有')

        def colour_of(key):
            m = re.search(
                key + r':\s*\{[^}]*?colorClass:\s*"([^"]+)"', src, re.S)
            return m.group(1) if m else None

        disconnected = colour_of('disconnected')
        deleted = colour_of('deleted')
        self.assertTrue(
            disconnected, "抓不到 disconnected 的 colorClass —— 探針壞了")
        self.assertTrue(
            deleted,
            "StatusBadge 沒有 `deleted` 條目 ⇒ 會落到 fallback（原字串＋琥珀），"
            "與 disconnected 同色，#833 在 UI 層被重做一次",
        )
        self.assertNotEqual(
            deleted, disconnected,
            "`deleted` 與 `disconnected` 同色 ⇒ 使用者看到的還是同一顆燈",
        )
