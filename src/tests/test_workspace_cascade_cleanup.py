"""F-ENV-5: workspace.unlink() 必須讓帶 Cloudflare tunnel 的子 record
（SecurityAccess / SmartHome）走各自的 Python unlink()（含 F-ENV-4 fail-loud
guard）做 teardown，而不是讓 DB 層 FK cascade 直接 SQL DELETE 繞過它、把計費中
的 tunnel 變成無 retry 錨點的永久孤兒。

根因 pitfall: cf-ns-orphan-no-account-gc。precedent: cloud-service-unlink-
override-cleanup（DB cascade 繞過 Python unlink，cleanup 須上移到 parent.unlink）。
CloudService（helm/ns）有同樣 cascade 暴露，但其 unlink 的 MCP cleanup 會與既有
鎖定的 test_delete_workspace_preserves_mcp_via_orphaned_project invariant 互動，
刻意不在本案 route（F-ENV-5 residual）。
"""
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


def _mock_sh():
    return patch(
        'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')


def _mock_sa():
    return patch(
        'odoo.addons.woow_paas_platform.models.security_access.get_paas_operator_client')


class TestWorkspaceCascadeCleanup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.SA = self.env['woow_paas_platform.security_access']

    def test_workspace_unlink_tears_down_tunneled_smarthome(self):
        """workspace.unlink 必須走 SmartHome.unlink → delete_tunnel 被呼叫（F-ENV-5）。

        修前：workspace 無 unlink override → DB cascade 直刪 SH row、繞過 Python
        unlink → delete_tunnel 從未被呼叫 → tunnel 孤兒（RED）。
        """
        ws = self.Workspace.create({'name': 'WS Cascade SH'})
        with _mock_sh() as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client
            sh = self.SmartHome.create({
                'name': 'sh', 'workspace_id': ws.id,
                'tunnel_id': 'tun-live-1', 'state': 'active',
            })
            sh_id = sh.id
            ws.unlink()
            mock_client.delete_tunnel.assert_called_once_with('tun-live-1')
        self.assertFalse(self.SmartHome.browse(sh_id).exists(),
                         'cleanup 成功後 SmartHome 應隨 workspace 一併刪除')
        self.assertFalse(self.Workspace.browse(ws.id).exists())

    def test_workspace_unlink_tears_down_tunneled_security_access(self):
        """workspace.unlink 必須走 SecurityAccess.unlink → delete_tunnel 被呼叫（F-ENV-5）。"""
        ws = self.Workspace.create({'name': 'WS Cascade SA'})
        with _mock_sa() as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client
            sa = self.SA.create({
                'name': 'sa', 'workspace_id': ws.id,
                'tunnel_id': 'tun-live-2',
            })
            sa_id = sa.id
            ws.unlink()
            mock_client.delete_tunnel.assert_called_once_with('tun-live-2')
        self.assertFalse(self.SA.browse(sa_id).exists(),
                         'cleanup 成功後 SecurityAccess 應隨 workspace 一併刪除')

    def test_workspace_unlink_fail_loud_keeps_tunneled_child(self):
        """child tunnel cleanup 失敗 → child unlink raise UserError → 穿透
        workspace.unlink → 整個刪除 rollback：workspace + child + tunnel 全保留（F-ENV-5）。

        修前：cascade 直刪、無 guard → 不 raise、workspace+SH 都消失、tunnel 孤兒（RED）。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        ws = self.Workspace.create({'name': 'WS FailLoud'})
        with _mock_sh() as mock_get:
            mock_client = MagicMock()
            mock_client.delete_tunnel.side_effect = PaaSOperatorError(message='boom')
            mock_get.return_value = mock_client
            sh = self.SmartHome.create({
                'name': 'sh', 'workspace_id': ws.id,
                'tunnel_id': 'tun-x', 'state': 'active',
            })
            sh_id = sh.id
            with self.assertRaises(UserError):
                ws.unlink()
        # fail-loud rollback：三者皆存活、tunnel_id 保留供 retry
        self.assertTrue(self.Workspace.browse(ws.id).exists(),
                        'cleanup 失敗 → workspace 刪除應 rollback、保留')
        kept = self.SmartHome.browse(sh_id)
        self.assertTrue(kept.exists(), 'child 必須保留供 retry')
        self.assertEqual(kept.tunnel_id, 'tun-x', 'tunnel_id 必須保留供 retry')

    def test_workspace_unlink_no_tunnel_clean_delete(self):
        """無 tunnel 的 child → workspace 正常刪光，不呼叫 delete_tunnel、無 raise（happy）。"""
        ws = self.Workspace.create({'name': 'WS NoTunnel'})
        sh = self.SmartHome.create({'name': 'sh', 'workspace_id': ws.id})
        sh_id = sh.id
        ws.unlink()
        self.assertFalse(self.SmartHome.browse(sh_id).exists())
        self.assertFalse(self.Workspace.browse(ws.id).exists())

    def test_workspace_unlink_tears_down_both_sa_and_smarthome(self):
        """單 workspace 同時帶 SA + SmartHome（皆 tunnel）→ 兩個 delete_tunnel 都被
        呼叫、兩 row 都刪、互不 skip（F-ENV-5）。"""
        ws = self.Workspace.create({'name': 'WS Both'})
        with _mock_sh() as mock_sh_get, _mock_sa() as mock_sa_get:
            sh_client = MagicMock()
            sa_client = MagicMock()
            mock_sh_get.return_value = sh_client
            mock_sa_get.return_value = sa_client
            sh = self.SmartHome.create({
                'name': 'sh', 'workspace_id': ws.id,
                'tunnel_id': 'tun-sh', 'state': 'active',
            })
            sa = self.SA.create({
                'name': 'sa', 'workspace_id': ws.id, 'tunnel_id': 'tun-sa',
            })
            sh_id, sa_id = sh.id, sa.id
            ws.unlink()
            sh_client.delete_tunnel.assert_called_once_with('tun-sh')
            sa_client.delete_tunnel.assert_called_once_with('tun-sa')
        self.assertFalse(self.SmartHome.browse(sh_id).exists())
        self.assertFalse(self.SA.browse(sa_id).exists())
        self.assertFalse(self.Workspace.browse(ws.id).exists())

    def test_workspace_unlink_batch_multiple_workspaces(self):
        """batch：一次 unlink 多個 workspace，各帶 tunneled SmartHome → 每個
        delete_tunnel 都被呼叫、全部刪除（驗 for workspace in self 迴圈正確）。"""
        ws1 = self.Workspace.create({'name': 'WS B1'})
        ws2 = self.Workspace.create({'name': 'WS B2'})
        with _mock_sh() as mock_get:
            client = MagicMock()
            mock_get.return_value = client
            sh1 = self.SmartHome.create({
                'name': 'sh1', 'workspace_id': ws1.id,
                'tunnel_id': 'tun-b1', 'state': 'active',
            })
            sh2 = self.SmartHome.create({
                'name': 'sh2', 'workspace_id': ws2.id,
                'tunnel_id': 'tun-b2', 'state': 'active',
            })
            sh1_id, sh2_id = sh1.id, sh2.id
            (ws1 | ws2).unlink()
            self.assertEqual(client.delete_tunnel.call_count, 2)
            client.delete_tunnel.assert_any_call('tun-b1')
            client.delete_tunnel.assert_any_call('tun-b2')
        self.assertFalse(self.SmartHome.browse(sh1_id).exists())
        self.assertFalse(self.SmartHome.browse(sh2_id).exists())
        self.assertFalse(self.Workspace.browse(ws1.id).exists())
        self.assertFalse(self.Workspace.browse(ws2.id).exists())
