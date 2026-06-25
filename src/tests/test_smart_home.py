"""Tests for Smart Home model."""
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


class TestSmartHome(TransactionCase):
    """Test cases for woow_paas_platform.smart_home model."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.workspace = self.Workspace.create({'name': 'Test Workspace'})

    def test_create_smart_home(self):
        """Test creating a smart home with minimal fields."""
        home = self.SmartHome.create({
            'name': 'My HA',
            'workspace_id': self.workspace.id,
        })
        self.assertEqual(home.name, 'My HA')
        self.assertEqual(home.state, 'pending')
        self.assertEqual(home.ha_port, 8123)
        self.assertEqual(home.workspace_id.id, self.workspace.id)
        self.assertEqual(home.tunnel_status, 'disconnected')

    def test_create_smart_home_custom_port(self):
        """Test creating a smart home with custom HA port."""
        home = self.SmartHome.create({
            'name': 'Custom Port HA',
            'workspace_id': self.workspace.id,
            'ha_port': 9123,
        })
        self.assertEqual(home.ha_port, 9123)

    def test_state_default_pending(self):
        """Test that new smart homes start in pending state."""
        home = self.SmartHome.create({
            'name': 'Pending Home',
            'workspace_id': self.workspace.id,
        })
        self.assertEqual(home.state, 'pending')

    def test_generate_subdomain(self):
        """Test subdomain generation format."""
        home = self.SmartHome.create({
            'name': 'Subdomain Test',
            'workspace_id': self.workspace.id,
        })
        subdomain = home._generate_subdomain()
        self.assertTrue(subdomain.startswith('paas-sm-'))
        self.assertGreater(len(subdomain), 10)

    def test_subdomain_has_no_env_token_even_when_deploy_env_stg(self):
        """DP2 lock：SM subdomain = 對外 URL，決策上「不注」env token。

        即使 deploy_env=stg，SM `_generate_subdomain` 也不得帶 token（subdomain
        對外、env 由 per-env paas_domain 區分；naming helper 雖支援 env，但 SM
        呼叫端刻意不傳）。鎖住「CS/SM 不接線」的決策，防未來誤接。
        """
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.deploy_env', 'stg')
        home = self.SmartHome.create({
            'name': 'Stg No Token Home',
            'workspace_id': self.workspace.id,
        })
        subdomain = home._generate_subdomain()
        self.assertTrue(subdomain.startswith('paas-sm-'))
        self.assertNotIn('stg', subdomain)

    def test_to_dict(self):
        """Test serialization to dict."""
        home = self.SmartHome.create({
            'name': 'Dict Test',
            'workspace_id': self.workspace.id,
        })
        data = home.to_dict()
        self.assertEqual(data['name'], 'Dict Test')
        self.assertEqual(data['state'], 'pending')
        self.assertEqual(data['ha_port'], 8123)
        self.assertEqual(data['workspace_id'], self.workspace.id)
        self.assertEqual(data['tunnel_status'], 'disconnected')
        self.assertIn('id', data)
        self.assertIn('created_at', data)

    def test_unique_subdomain_constraint(self):
        """Test that subdomains must be unique."""
        self.SmartHome.create({
            'name': 'Home 1',
            'workspace_id': self.workspace.id,
            'subdomain': 'unique-test-123',
        })
        with self.assertRaises(Exception):
            self.SmartHome.create({
                'name': 'Home 2',
                'workspace_id': self.workspace.id,
                'subdomain': 'unique-test-123',
            })

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_provision_success(self, mock_get_client):
        """Test successful provisioning creates tunnel."""
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-abc123',
            'tunnel_token': 'tok-secret',
            'tunnel_name': 'smarthome-test',
        }
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Provision Test',
            'workspace_id': self.workspace.id,
        })
        home.action_provision()

        self.assertEqual(home.state, 'active')
        self.assertEqual(home.tunnel_id, 'tun-abc123')
        self.assertEqual(home.tunnel_token, 'tok-secret')
        self.assertTrue(home.subdomain)
        self.assertTrue(home.tunnel_route)
        self.assertTrue(home.deployed_at)

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_provision_connection_error(self, mock_get_client):
        """Test provisioning handles connection error."""
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorConnectionError
        mock_client = MagicMock()
        mock_client.create_tunnel.side_effect = PaaSOperatorConnectionError(
            message='Connection refused'
        )
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Error Test',
            'workspace_id': self.workspace.id,
        })
        home.action_provision()

        self.assertEqual(home.state, 'error')
        self.assertIn('Cannot connect', home.error_message)

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_provision_operator_error(self, mock_get_client):
        """Test provisioning handles operator API error."""
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        mock_client = MagicMock()
        mock_client.create_tunnel.side_effect = PaaSOperatorError(
            message='Tunnel name already exists'
        )
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'API Error Test',
            'workspace_id': self.workspace.id,
        })
        home.action_provision()

        self.assertEqual(home.state, 'error')
        self.assertIn('Tunnel creation failed', home.error_message)

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_delete_with_tunnel(self, mock_get_client):
        """Test deleting a smart home with an existing tunnel."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Delete Test',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-delete-me',
            'state': 'active',
        })
        home_id = home.id
        home.action_delete()

        mock_client.delete_tunnel.assert_called_once_with('tun-delete-me')
        self.assertFalse(self.SmartHome.browse(home_id).exists())

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_delete_without_tunnel(self, mock_get_client):
        """Test deleting a smart home without a tunnel."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'No Tunnel Delete',
            'workspace_id': self.workspace.id,
        })
        home_id = home.id
        home.action_delete()

        mock_client.delete_tunnel.assert_not_called()
        self.assertFalse(self.SmartHome.browse(home_id).exists())

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_action_delete_keeps_record_and_raises_when_tunnel_delete_fails(self, mock_get_client):
        """刪 tunnel 暫態失敗 → 不得無條件 unlink 製造孤兒（F-ENV-4 止血）。

        鏡像 SecurityAccess.unlink 的 fail-loud 契約：delete_tunnel 失敗時
        保留 DB row + tunnel_id 供 retry、並 raise UserError —— 而非靜默
        unlink、留下計費中且無 DB 參照可再發現的孤兒 tunnel（cf-ns-orphan
        事故 3 條孤兒 tunnel 的製造源）。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        mock_client = MagicMock()
        mock_client.delete_tunnel.side_effect = PaaSOperatorError(message='upstream 503')
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Orphan Risk Home',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-still-live',
            'state': 'active',
        })
        home_id = home.id

        with self.assertRaises(UserError):
            home.action_delete()

        kept = self.SmartHome.browse(home_id)
        self.assertTrue(kept.exists(), '刪 tunnel 失敗時 record 必須保留供 retry')
        self.assertEqual(kept.tunnel_id, 'tun-still-live', 'tunnel_id 必須保留供 retry')
        mock_client.delete_tunnel.assert_called_once_with('tun-still-live')

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_unlink_keeps_record_and_raises_when_tunnel_still_live(self, mock_get_client):
        """直接 unlink 也要 fail-loud：tunnel 還活著就不可刪 DB row（F-ENV-4）。

        與 SecurityAccess.unlink 對稱 —— 任何 unlink 路徑（含 cron、直接刪）
        都受同一道 guard 保護，tunnel 未成功 teardown 即 raise、保留供 retry。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        mock_client = MagicMock()
        mock_client.delete_tunnel.side_effect = PaaSOperatorError(message='upstream 503')
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Direct Unlink Home',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-still-live-2',
            'state': 'active',
        })
        home_id = home.id

        with self.assertRaises(UserError):
            home.unlink()

        kept = self.SmartHome.browse(home_id)
        self.assertTrue(kept.exists(), 'tunnel 還活著時 record 必須保留供 retry')
        self.assertEqual(kept.tunnel_id, 'tun-still-live-2', 'tunnel_id 必須保留供 retry')
        mock_client.delete_tunnel.assert_called_once_with('tun-still-live-2')

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_unlink_success_clears_tunnel_and_removes_record(self, mock_get_client):
        """正向控制：tunnel 刪成功時 unlink 正常清空 tunnel_id 並刪 record（F-ENV-4）。

        擋「_cleanup_tunnel 成功路徑被改壞」的回歸（純負向測試不足）。
        """
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Clean Unlink Home',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-clean',
            'state': 'active',
        })
        home_id = home.id

        home.unlink()

        mock_client.delete_tunnel.assert_called_once_with('tun-clean')
        self.assertFalse(
            self.SmartHome.browse(home_id).exists(),
            'tunnel 清乾淨後 record 應正常刪除',
        )

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_do_cleanup_keeps_tunnel_and_lands_error_on_failure(self, mock_get_client):
        """cron 路徑（_do_cleanup）刪 tunnel 失敗也要 fail-loud（F-ENV-4 M1）。

        cron（subscription_lifecycle_cron）走 _do_cleanup() 非 unlink()，unlink
        guard 不覆蓋此路徑。_do_cleanup 須鏡像 SecurityAccess._do_cleanup：刪失敗
        則保留 tunnel_id + 落 state='error'（非 'deleting' 假成功訊號），讓下一次
        cron retry，避免訂閱過期後孤兒從 cron 路徑永久外洩。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        mock_client = MagicMock()
        mock_client.delete_tunnel.side_effect = PaaSOperatorError(message='upstream 503')
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Cron Cleanup Home',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-cron-live',
            'state': 'active',
        })

        home._do_cleanup()

        self.assertEqual(home.state, 'error', '刪 tunnel 失敗 → state 應落 error 非 deleting')
        self.assertEqual(home.tunnel_id, 'tun-cron-live', 'tunnel_id 必須保留供 cron retry')

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_refresh_status_connected(self, mock_get_client):
        """Test refreshing status with connected tunnel."""
        mock_client = MagicMock()
        mock_client.get_tunnel_status.return_value = {
            'connections': [{
                'connector_id': 'conn-123',
                'type': 'cloudflared',
                'opened_at': '2026-03-01T00:00:00Z',
            }],
        }
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Refresh Test',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-refresh',
            'state': 'active',
        })
        home.action_refresh_status()

        self.assertEqual(home.tunnel_status, 'connected')
        self.assertEqual(home.connector_id, 'conn-123')
        self.assertEqual(home.connector_type, 'cloudflared')

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_refresh_status_disconnected(self, mock_get_client):
        """Test refreshing status with no connections."""
        mock_client = MagicMock()
        mock_client.get_tunnel_status.return_value = {
            'connections': [],
        }
        mock_get_client.return_value = mock_client

        home = self.SmartHome.create({
            'name': 'Disconnected Test',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'tun-disc',
            'state': 'active',
        })
        home.action_refresh_status()

        self.assertEqual(home.tunnel_status, 'disconnected')

    def test_no_operator_raises_error(self):
        """Test that missing operator config raises UserError."""
        home = self.SmartHome.create({
            'name': 'No Operator',
            'workspace_id': self.workspace.id,
        })
        # Ensure operator is not configured
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.operator_url', ''
        )
        with self.assertRaises(UserError):
            home._get_operator_client()

    def test_cascade_delete_workspace(self):
        """Test that deleting workspace cascades to smart homes."""
        home = self.SmartHome.create({
            'name': 'Cascade Test',
            'workspace_id': self.workspace.id,
        })
        home_id = home.id
        self.workspace.unlink()
        self.assertFalse(self.SmartHome.browse(home_id).exists())
