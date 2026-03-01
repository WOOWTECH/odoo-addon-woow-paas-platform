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
        self.assertTrue(subdomain.startswith('sh-'))
        self.assertGreater(len(subdomain), 5)

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
