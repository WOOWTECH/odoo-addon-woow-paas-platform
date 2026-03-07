"""Tests for OAuth 2.0 Provider models and token lifecycle."""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestOAuthClient(TransactionCase):
    """Test cases for OAuth Client model."""

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']

    def test_create_client(self):
        """Test creating an OAuth client generates client_id."""
        client = self.OAuthClient.create({
            'name': 'Test App',
            'redirect_uris': 'http://localhost:8123/auth/callback',
        })
        self.assertTrue(client.client_id)
        self.assertEqual(client.name, 'Test App')
        self.assertTrue(client.is_active)

    def test_client_id_unique(self):
        """Test client_id uniqueness constraint."""
        client1 = self.OAuthClient.create({
            'name': 'App 1',
            'redirect_uris': 'http://localhost/cb',
        })
        with self.assertRaises(Exception):
            self.OAuthClient.create({
                'name': 'App 2',
                'client_id': client1.client_id,
                'redirect_uris': 'http://localhost/cb2',
            })

    def test_set_and_verify_secret(self):
        """Test setting and verifying client secret."""
        client = self.OAuthClient.create({
            'name': 'Secret Test',
            'redirect_uris': 'http://localhost/cb',
        })
        client.set_secret('my-secret-123')
        self.assertTrue(client.verify_secret('my-secret-123'))
        self.assertFalse(client.verify_secret('wrong-secret'))

    def test_check_redirect_uri(self):
        """Test redirect URI validation."""
        client = self.OAuthClient.create({
            'name': 'URI Test',
            'redirect_uris': 'http://localhost:8123/auth/callback\nhttps://example.com/cb',
        })
        self.assertTrue(client.check_redirect_uri('http://localhost:8123/auth/callback'))
        self.assertTrue(client.check_redirect_uri('https://example.com/cb'))
        self.assertFalse(client.check_redirect_uri('https://evil.com/cb'))

    def test_default_scopes(self):
        """Test default scopes include smart home scopes."""
        client = self.OAuthClient.create({
            'name': 'Scope Test',
            'redirect_uris': 'http://localhost/cb',
        })
        self.assertIn('smarthome:read', client.scopes)
        self.assertIn('workspace:read', client.scopes)
        self.assertIn('smarthome:tunnel', client.scopes)

    def test_default_grant_types(self):
        """Test default grant types."""
        client = self.OAuthClient.create({
            'name': 'Grant Test',
            'redirect_uris': 'http://localhost/cb',
        })
        self.assertIn('authorization_code', client.grant_types)
        self.assertIn('refresh_token', client.grant_types)


class TestOAuthToken(TransactionCase):
    """Test cases for OAuth Token model."""

    def setUp(self):
        super().setUp()
        self.OAuthToken = self.env['woow_paas_platform.oauth_token']
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']

        self.client = self.OAuthClient.create({
            'name': 'Token Test Client',
            'redirect_uris': 'http://localhost/cb',
        })
        self.user = self.env.ref('base.user_admin')

    def _create_token(self, **overrides):
        """Helper to create a test token."""
        vals = {
            'access_token': 'test-access-token-123',
            'refresh_token': 'test-refresh-token-456',
            'scope': 'smarthome:read workspace:read',
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'user_id': self.user.id,
            'client_id': self.client.id,
        }
        vals.update(overrides)
        return self.OAuthToken.create(vals)

    def test_create_token(self):
        """Test creating an OAuth token."""
        token = self._create_token()
        self.assertEqual(token.access_token, 'test-access-token-123')
        self.assertEqual(token.token_type, 'bearer')
        self.assertFalse(token.is_revoked)

    def test_access_token_valid(self):
        """Test valid access token check."""
        token = self._create_token()
        self.assertTrue(token.is_access_token_valid())

    def test_access_token_expired(self):
        """Test expired access token."""
        token = self._create_token(
            expires_at=fields.Datetime.now() - timedelta(hours=1),
        )
        self.assertFalse(token.is_access_token_valid())

    def test_access_token_revoked(self):
        """Test revoked access token."""
        token = self._create_token()
        token.revoke()
        self.assertTrue(token.is_revoked)
        self.assertFalse(token.is_access_token_valid())

    def test_refresh_token_valid(self):
        """Test valid refresh token check."""
        token = self._create_token()
        self.assertTrue(token.is_refresh_token_valid())

    def test_refresh_token_expired(self):
        """Test expired refresh token."""
        token = self._create_token(
            refresh_expires_at=fields.Datetime.now() - timedelta(days=1),
        )
        self.assertFalse(token.is_refresh_token_valid())

    def test_has_scope(self):
        """Test scope checking."""
        token = self._create_token(scope='smarthome:read workspace:read smarthome:tunnel')
        self.assertTrue(token.has_scope('smarthome:read'))
        self.assertTrue(token.has_scope('workspace:read'))
        self.assertTrue(token.has_scope('smarthome:tunnel'))
        self.assertFalse(token.has_scope('admin:write'))

    def test_has_all_scopes(self):
        """Test multiple scope checking."""
        token = self._create_token(scope='smarthome:read workspace:read')
        self.assertTrue(token.has_all_scopes(['smarthome:read', 'workspace:read']))
        self.assertFalse(token.has_all_scopes(['smarthome:read', 'smarthome:tunnel']))

    def test_revoke_token(self):
        """Test token revocation."""
        token = self._create_token()
        self.assertFalse(token.is_revoked)
        token.revoke()
        self.assertTrue(token.is_revoked)
        self.assertFalse(token.is_access_token_valid())
        self.assertFalse(token.is_refresh_token_valid())


class TestOAuthCode(TransactionCase):
    """Test cases for OAuth Authorization Code model."""

    def setUp(self):
        super().setUp()
        self.OAuthCode = self.env['woow_paas_platform.oauth_code']
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']

        self.client = self.OAuthClient.create({
            'name': 'Code Test Client',
            'redirect_uris': 'http://localhost/cb',
        })
        self.user = self.env.ref('base.user_admin')

    def test_create_code(self):
        """Test creating an authorization code."""
        code = self.OAuthCode.create({
            'code': 'auth-code-123',
            'client_id': self.client.id,
            'user_id': self.user.id,
            'redirect_uri': 'http://localhost/cb',
            'scope': 'smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
        })
        self.assertEqual(code.code, 'auth-code-123')
        self.assertFalse(code.is_used)

    def test_code_valid(self):
        """Test valid code check."""
        code = self.OAuthCode.create({
            'code': 'valid-code',
            'client_id': self.client.id,
            'user_id': self.user.id,
            'redirect_uri': 'http://localhost/cb',
            'scope': 'smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
        })
        self.assertTrue(code.is_valid())

    def test_code_expired(self):
        """Test expired code."""
        code = self.OAuthCode.create({
            'code': 'expired-code',
            'client_id': self.client.id,
            'user_id': self.user.id,
            'redirect_uri': 'http://localhost/cb',
            'scope': 'smarthome:read',
            'expires_at': fields.Datetime.now() - timedelta(minutes=1),
        })
        self.assertFalse(code.is_valid())

    def test_code_used(self):
        """Test used code rejected."""
        code = self.OAuthCode.create({
            'code': 'used-code',
            'client_id': self.client.id,
            'user_id': self.user.id,
            'redirect_uri': 'http://localhost/cb',
            'scope': 'smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
        })
        code.mark_used()
        self.assertTrue(code.is_used)
        self.assertFalse(code.is_valid())
