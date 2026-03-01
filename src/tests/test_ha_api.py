"""Tests for HA Integration API endpoints.

Uses TransactionCase with direct function calls to test
the controller logic without requiring a running HTTP server.
"""
from datetime import timedelta
from unittest.mock import patch, MagicMock

from odoo import fields
from odoo.tests.common import TransactionCase


class TestHAApiBase(TransactionCase):
    """Base class for HA API tests with shared fixtures."""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.OAuthToken = self.env['woow_paas_platform.oauth_token']

        # Use self.env.user (superuser in TransactionCase) to match
        # Workspace.create() which sets owner_id = self.env.user
        self.user = self.env.user

        # Create workspace (auto-creates owner access for self.env.user)
        self.workspace = self.Workspace.create({'name': 'HA Test Workspace'})

        # Create smart home
        self.home = self.SmartHome.create({
            'name': 'Test HA',
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-test-123',
            'tunnel_token': 'tok-secret-abc',
            'subdomain': 'paas-sm-a1b2c3d4-e5f6g7h8',
            'tunnel_status': 'connected',
        })

        # Create OAuth client + valid token
        self.oauth_client = self.OAuthClient.create({
            'name': 'HA Test Client',
            'redirect_uris': 'http://localhost:8123/auth/callback',
        })
        self.token = self.OAuthToken.create({
            'access_token': 'valid-bearer-token-123',
            'refresh_token': 'refresh-token-456',
            'scope': 'smarthome:read smarthome:tunnel workspace:read',
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'user_id': self.user.id,
            'client_id': self.oauth_client.id,
        })


class TestHAApiAccessControl(TestHAApiBase):
    """Test access control for HA API."""

    def test_token_lookup_valid(self):
        """Test that a valid token can be found."""
        token = self.OAuthToken.search([
            ('access_token', '=', 'valid-bearer-token-123'),
            ('is_revoked', '=', False),
        ], limit=1)
        self.assertTrue(token.exists())
        self.assertTrue(token.is_access_token_valid())

    def test_token_scope_check(self):
        """Test scope checking on the token."""
        self.assertTrue(self.token.has_scope('smarthome:read'))
        self.assertTrue(self.token.has_scope('workspace:read'))
        self.assertTrue(self.token.has_scope('smarthome:tunnel'))
        self.assertFalse(self.token.has_scope('admin:write'))

    def test_revoked_token_invalid(self):
        """Test that revoked tokens are rejected."""
        self.token.revoke()
        self.assertFalse(self.token.is_access_token_valid())

    def test_expired_token_invalid(self):
        """Test that expired tokens are rejected."""
        self.token.write({
            'expires_at': fields.Datetime.now() - timedelta(hours=1),
        })
        self.assertFalse(self.token.is_access_token_valid())

    def test_workspace_access_check(self):
        """Test workspace access verification."""
        access = self.WorkspaceAccess.search([
            ('user_id', '=', self.user.id),
            ('workspace_id', '=', self.workspace.id),
            ('workspace_id.state', '=', 'active'),
        ], limit=1)
        self.assertTrue(access.exists())

    def test_no_workspace_access(self):
        """Test that user without access cannot see workspace."""
        other_user = self.env['res.users'].create({
            'name': 'No Access User',
            'login': 'no_access_test@example.com',
        })
        access = self.WorkspaceAccess.sudo().search([
            ('user_id', '=', other_user.id),
            ('workspace_id', '=', self.workspace.id),
        ], limit=1)
        self.assertFalse(access.exists())


class TestHAApiData(TestHAApiBase):
    """Test data retrieval for HA API endpoints."""

    def test_user_workspaces(self):
        """Test listing user's accessible workspaces."""
        access_records = self.WorkspaceAccess.search([
            ('user_id', '=', self.user.id),
            ('workspace_id.state', '=', 'active'),
        ])
        workspaces = access_records.mapped('workspace_id')
        self.assertIn(self.workspace, workspaces)

    def test_workspace_smart_homes(self):
        """Test listing smart homes in a workspace."""
        homes = self.SmartHome.search([
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertEqual(len(homes), 1)
        self.assertEqual(homes[0].name, 'Test HA')

    def test_smart_home_to_dict(self):
        """Test smart home serialization."""
        data = self.home.to_dict()
        self.assertEqual(data['id'], self.home.id)
        self.assertEqual(data['name'], 'Test HA')
        self.assertEqual(data['state'], 'active')
        self.assertEqual(data['tunnel_id'], 'tun-test-123')
        self.assertEqual(data['subdomain'], 'paas-sm-a1b2c3d4-e5f6g7h8')
        self.assertEqual(data['tunnel_status'], 'connected')

    def test_tunnel_token_data(self):
        """Test that tunnel token data is accessible."""
        self.assertEqual(self.home.tunnel_token, 'tok-secret-abc')
        self.assertEqual(self.home.tunnel_id, 'tun-test-123')

    def test_smart_home_domain(self):
        """Test domain retrieval for FQDN construction."""
        domain = self.home._get_paas_domain()
        self.assertTrue(domain)  # Should have a default

    def test_multiple_homes_in_workspace(self):
        """Test multiple smart homes in same workspace."""
        self.SmartHome.create({
            'name': 'Second HA',
            'workspace_id': self.workspace.id,
        })
        homes = self.SmartHome.search([
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertEqual(len(homes), 2)

    def test_home_not_visible_in_other_workspace(self):
        """Test smart home isolation between workspaces."""
        other_ws = self.Workspace.create({'name': 'Other WS'})
        homes = self.SmartHome.search([
            ('workspace_id', '=', other_ws.id),
        ])
        self.assertEqual(len(homes), 0)

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_refresh_status_updates_home(self, mock_get_client):
        """Test that refresh status updates the home record."""
        mock_client = MagicMock()
        mock_client.get_tunnel_status.return_value = {
            'connections': [{
                'connector_id': 'conn-new',
                'type': 'cloudflared',
                'opened_at': '2026-03-01T12:00:00Z',
            }],
        }
        mock_get_client.return_value = mock_client

        self.home.action_refresh_status()
        self.assertEqual(self.home.tunnel_status, 'connected')
        self.assertEqual(self.home.connector_id, 'conn-new')


class TestHAApiIntegration(TestHAApiBase):
    """Integration tests for the full Smart Home -> Tunnel Token flow."""

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_create_provision_get_token_flow(self, mock_get_client):
        """Test complete flow: create -> provision -> get token."""
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-flow-123',
            'tunnel_token': 'tok-flow-secret',
            'tunnel_name': 'smarthome-flow',
        }
        mock_get_client.return_value = mock_client

        # Step 1: Create Smart Home
        home = self.SmartHome.create({
            'name': 'Flow Test HA',
            'workspace_id': self.workspace.id,
        })
        self.assertEqual(home.state, 'pending')

        # Step 2: Provision
        home.action_provision()
        self.assertEqual(home.state, 'active')
        self.assertEqual(home.tunnel_id, 'tun-flow-123')
        self.assertEqual(home.tunnel_token, 'tok-flow-secret')
        self.assertTrue(home.subdomain)
        self.assertTrue(home.tunnel_route)

        # Step 3: Verify token data is available
        data = home.to_dict()
        self.assertEqual(data['state'], 'active')
        self.assertEqual(data['tunnel_id'], 'tun-flow-123')

        # Step 4: Token is accessible (for HA component)
        self.assertEqual(home.tunnel_token, 'tok-flow-secret')

        # Step 5: Verify workspace access control
        access = self.WorkspaceAccess.search([
            ('user_id', '=', self.user.id),
            ('workspace_id', '=', home.workspace_id.id),
        ], limit=1)
        self.assertTrue(access.exists())

    @patch('odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client')
    def test_create_provision_delete_flow(self, mock_get_client):
        """Test full lifecycle: create -> provision -> delete."""
        mock_client = MagicMock()
        mock_client.create_tunnel.return_value = {
            'tunnel_id': 'tun-lifecycle',
            'tunnel_token': 'tok-lifecycle',
            'tunnel_name': 'smarthome-lifecycle',
        }
        mock_get_client.return_value = mock_client

        # Create and provision
        home = self.SmartHome.create({
            'name': 'Lifecycle HA',
            'workspace_id': self.workspace.id,
        })
        home.action_provision()
        self.assertEqual(home.state, 'active')
        home_id = home.id

        # Delete
        home.action_delete()
        mock_client.delete_tunnel.assert_called_once_with('tun-lifecycle')
        self.assertFalse(self.SmartHome.browse(home_id).exists())
