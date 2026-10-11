"""Tests for OAuth 2.0 Provider models and token lifecycle."""
import base64
import hashlib
import json
import secrets
from datetime import timedelta
from unittest.mock import MagicMock, patch

from werkzeug.wrappers import Response as WerkzeugResponse

from odoo import fields
from odoo.exceptions import ValidationError
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

    def _ha_client(self):
        """A public + PKCE client in ha_callback mode (mirrors the seeded HA client)."""
        return self.OAuthClient.create({
            'name': 'HA Callback Test',
            'client_type': 'public',
            'require_pkce': True,
            'redirect_uri_match_mode': 'ha_callback',
            'redirect_uris': '/auth/external/woow/callback',
        })

    def test_ha_callback_accepts_loopback_http(self):
        c = self._ha_client()
        self.assertTrue(c.check_redirect_uri('http://127.0.0.1:8123/auth/external/woow/callback'))
        self.assertTrue(c.check_redirect_uri('http://localhost:8123/auth/external/woow/callback'))
        self.assertTrue(c.check_redirect_uri('http://[::1]:8123/auth/external/woow/callback'))

    def test_ha_callback_accepts_private_and_local_http(self):
        c = self._ha_client()
        self.assertTrue(c.check_redirect_uri('http://192.168.1.50:8123/auth/external/woow/callback'))
        self.assertTrue(c.check_redirect_uri('http://10.0.0.5:8123/auth/external/woow/callback'))
        self.assertTrue(c.check_redirect_uri('http://homeassistant.local:8123/auth/external/woow/callback'))

    def test_ha_callback_accepts_public_https(self):
        c = self._ha_client()
        self.assertTrue(c.check_redirect_uri('https://abc123.ui.nabu.casa/auth/external/woow/callback'))
        self.assertTrue(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback'))

    def test_ha_callback_rejects_public_http(self):
        c = self._ha_client()
        self.assertFalse(c.check_redirect_uri('http://ha.mydomain.com/auth/external/woow/callback'))
        self.assertFalse(c.check_redirect_uri('http://attacker.example.com/auth/external/woow/callback'))

    def test_ha_callback_rejects_wrong_path(self):
        c = self._ha_client()
        self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/evil'))
        self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback/extra'))
        self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback/../evil'))

    def test_ha_callback_rejects_query_fragment_userinfo(self):
        c = self._ha_client()
        self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback?x=1'))
        self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback#frag'))
        self.assertFalse(c.check_redirect_uri('https://user:pass@ha.mydomain.com/auth/external/woow/callback'))

    def test_ha_callback_rejects_bad_scheme(self):
        c = self._ha_client()
        self.assertFalse(c.check_redirect_uri('ftp://ha.mydomain.com/auth/external/woow/callback'))
        self.assertFalse(c.check_redirect_uri('javascript:alert(1)//auth/external/woow/callback'))

    def test_ha_callback_rejects_parser_confusion(self):
        # urlsplit silently strips \t\r\n; fail closed on raw control chars /
        # surrounding whitespace so the validated host cannot diverge from the
        # stored/browser-seen literal (HIGH-1 from adversarial review).
        c = self._ha_client()
        self.assertFalse(c.check_redirect_uri('http://local\thost:8123/auth/external/woow/callback'))
        self.assertFalse(c.check_redirect_uri('http://127.0.0.1\n/auth/external/woow/callback'))
        self.assertFalse(c.check_redirect_uri(' http://127.0.0.1:8123/auth/external/woow/callback'))
        self.assertFalse(c.check_redirect_uri('https://exa\tmple.com/auth/external/woow/callback'))

    def test_ha_callback_local_host_classifier_edges(self):
        c = self._ha_client()
        # bare '.local' (empty label) is NOT local -> public -> http reject
        self.assertFalse(c.check_redirect_uri('http://.local/auth/external/woow/callback'))
        # IPv4-mapped IPv6 loopback unwraps and is allowed over http
        self.assertTrue(c.check_redirect_uri('http://[::ffff:127.0.0.1]/auth/external/woow/callback'))
        # uppercase host normalized; .local mDNS allowed over http
        self.assertTrue(c.check_redirect_uri('http://HomeAssistant.LOCAL:8123/auth/external/woow/callback'))

    def test_exact_mode_unaffected_by_relaxation(self):
        c = self.OAuthClient.create({
            'name': 'Exact Mode',
            'redirect_uris': 'https://example.com/cb',
        })
        self.assertEqual(c.redirect_uri_match_mode, 'exact')
        self.assertTrue(c.check_redirect_uri('https://example.com/cb'))
        self.assertFalse(c.check_redirect_uri('https://anything.com/auth/external/woow/callback'))

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


class TestPublicClientConstraints(TransactionCase):
    """Constraint tests for public OAuth clients (PKCE-only)."""

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']

    def _public_vals(self, **overrides):
        vals = {
            'name': 'Public Test',
            'redirect_uris': 'https://my.home-assistant.io/redirect/oauth',
            'client_type': 'public',
            'require_pkce': True,
            'grant_types': 'authorization_code,refresh_token',
        }
        vals.update(overrides)
        return vals

    def test_public_must_require_pkce(self):
        with self.assertRaises(ValidationError):
            self.OAuthClient.create(self._public_vals(require_pkce=False))

    def test_public_cannot_have_client_credentials_grant(self):
        with self.assertRaises(ValidationError):
            self.OAuthClient.create(self._public_vals(
                grant_types='authorization_code,refresh_token,client_credentials',
            ))

    def test_public_cannot_set_secret(self):
        client = self.OAuthClient.create(self._public_vals())
        with self.assertRaises(ValidationError):
            client.set_secret('should-fail')

    def test_public_cannot_regenerate_secret(self):
        client = self.OAuthClient.create(self._public_vals())
        with self.assertRaises(ValidationError):
            client.action_regenerate_secret()

    def test_public_cannot_acquire_secret_via_write(self):
        client = self.OAuthClient.create(self._public_vals())
        with self.assertRaises(ValidationError):
            client.write({'client_secret_hash': 'pretend-hash'})

    def test_ha_callback_mode_requires_pkce(self):
        """ha_callback relaxes host; without PKCE it would be a bare open-redirect."""
        with self.assertRaises(ValidationError):
            self.OAuthClient.create({
                'name': 'Bad HA Callback',
                'client_type': 'confidential',
                'require_pkce': False,
                'redirect_uri_match_mode': 'ha_callback',
                'redirect_uris': '/auth/external/woow/callback',
            })

    def test_ha_callback_mode_with_pkce_allowed(self):
        client = self.OAuthClient.create({
            'name': 'Good HA Callback',
            'client_type': 'public',
            'require_pkce': True,
            'redirect_uri_match_mode': 'ha_callback',
            'redirect_uris': '/auth/external/woow/callback',
        })
        self.assertEqual(client.redirect_uri_match_mode, 'ha_callback')

    def test_seeded_ha_smart_home_client(self):
        """The XML data record should be seeded on install with public+PKCE."""
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home',
            raise_if_not_found=False,
        )
        self.assertIsNotNone(client, 'HA smart home client not seeded')
        self.assertEqual(client.client_id, 'woow-ha-smart-home')
        self.assertEqual(client.client_type, 'public')
        self.assertTrue(client.require_pkce)
        self.assertFalse(client.client_secret_hash)
        self.assertEqual(client.redirect_uri_match_mode, 'ha_callback')
        self.assertEqual(client.redirect_uris, '/auth/external/woow/callback')
        # The relay value must be gone (route one 消除 my.home-assistant.io).
        self.assertNotIn('my.home-assistant.io', client.redirect_uris or '')


class TestHaRedirectModeHealer(TransactionCase):
    """Healer migrates the seeded HA client to ha_callback mode on -u
    (sticky-noupdate path: seed XML edits never reach existing DBs)."""

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def test_healer_flips_legacy_record_to_ha_callback(self):
        # Simulate a legacy DB row still in exact mode with the old value.
        self.client.write({
            'redirect_uri_match_mode': 'exact',
            'redirect_uris': 'https://my.home-assistant.io/redirect/oauth',
        })
        self.OAuthClient._ensure_ha_smart_home_redirect_mode()
        self.assertEqual(self.client.redirect_uri_match_mode, 'ha_callback')
        self.assertEqual(self.client.redirect_uris, self.OAuthClient.HA_CALLBACK_PATH)
        # Legacy my.home-assistant.io value is gone (not superset).
        self.assertNotIn('my.home-assistant.io', self.client.redirect_uris)

    def test_healer_is_idempotent(self):
        self.OAuthClient._ensure_ha_smart_home_redirect_mode()
        self.OAuthClient._ensure_ha_smart_home_redirect_mode()
        self.assertEqual(self.client.redirect_uri_match_mode, 'ha_callback')
        self.assertEqual(self.client.redirect_uris, self.OAuthClient.HA_CALLBACK_PATH)


class TestPkceOnlyTokenFlow(TransactionCase):
    """PKCE-only token-endpoint tests. Mocks `request` so the controller's
    cursor stays in the test transaction (avoids HttpCase visibility gaps)."""

    REDIRECT_URI = 'https://my.home-assistant.io/redirect/oauth'

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.OAuthCode = self.env['woow_paas_platform.oauth_code']
        self.user = self.env.user

        self.public_client = self.OAuthClient.create({
            'name': 'PKCE Test Public Client',
            'client_id': 'test-public-client-pkce',
            'redirect_uris': self.REDIRECT_URI,
            'client_type': 'public',
            'require_pkce': True,
            'scopes': 'smarthome:read smarthome:tunnel workspace:read',
            'grant_types': 'authorization_code,refresh_token',
        })

    @staticmethod
    def _pkce_pair():
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode('ascii')).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
        return verifier, challenge

    def _create_auth_code(self, challenge, method='S256', client=None):
        client = client or self.public_client
        code_str = secrets.token_urlsafe(32)
        self.OAuthCode.create({
            'code': code_str,
            'redirect_uri': self.REDIRECT_URI,
            'scope': 'smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
            'user_id': self.user.id,
            'client_id': client.id,
            'code_challenge': challenge,
            'code_challenge_method': method,
        })
        return code_str

    def _call_token(self, **payload):
        """Invoke OAuth2Controller.token with a patched `request` global."""
        # Import inside call so patch path is consistent.
        from odoo.addons.woow_paas_platform.controllers.oauth2 import (
            OAuth2Controller,
        )
        request_mock = MagicMock()
        request_mock.env = self.env
        with patch(
            'odoo.addons.woow_paas_platform.controllers.oauth2.request',
            request_mock,
        ):
            response = OAuth2Controller().token(**payload)
        body = json.loads(response.get_data(as_text=True))
        return response.status_code, body

    def test_authorization_code_with_pkce_no_secret_succeeds(self):
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(status, 200, body)
        self.assertIn('access_token', body)
        self.assertIn('refresh_token', body)
        self.assertEqual(body['token_type'], 'bearer')
        self.assertIsInstance(body['expires_in'], int)
        self.assertIn('smarthome:read', body['scope'])

    def test_authorization_code_with_secret_on_public_rejected(self):
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            client_secret='should-not-be-sent',
            code_verifier=verifier,
        )
        self.assertEqual(status, 401)
        self.assertEqual(body['error'], 'invalid_client')

    def test_authorization_code_without_verifier_rejected(self):
        _, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_grant')

    def test_authorization_code_with_wrong_verifier_rejected(self):
        _, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            code_verifier='wrong-verifier',
        )
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_grant')

    def test_authorization_code_with_plain_method_rejected_for_public(self):
        # Defense-in-depth: even if a plain-method auth_code somehow exists
        # for a public client (bypassing the authorize endpoint), the token
        # endpoint must reject it.
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge, method='plain')
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_grant')

    def test_refresh_without_secret_succeeds_and_rotates(self):
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        _, first = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            code_verifier=verifier,
        )

        status, body = self._call_token(
            grant_type='refresh_token',
            refresh_token=first['refresh_token'],
            client_id=self.public_client.client_id,
        )
        self.assertEqual(status, 200, body)
        self.assertIn('access_token', body)
        self.assertIn('refresh_token', body)
        self.assertNotEqual(body['refresh_token'], first['refresh_token'])

        # Old refresh_token must be revoked
        status, body = self._call_token(
            grant_type='refresh_token',
            refresh_token=first['refresh_token'],
            client_id=self.public_client.client_id,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_grant')

    def test_refresh_with_secret_on_public_rejected(self):
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        _, first = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            code_verifier=verifier,
        )

        status, body = self._call_token(
            grant_type='refresh_token',
            refresh_token=first['refresh_token'],
            client_id=self.public_client.client_id,
            client_secret='should-not-be-here',
        )
        self.assertEqual(status, 401)
        self.assertEqual(body['error'], 'invalid_client')

    def test_confidential_client_still_requires_secret(self):
        """Regression: confidential client flow must remain unchanged."""
        confidential = self.OAuthClient.create({
            'name': 'Confidential Test',
            'client_id': 'test-confidential-client',
            'redirect_uris': self.REDIRECT_URI,
            'client_type': 'confidential',
            'scopes': 'smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        confidential.set_secret('the-secret')

        # Plain (non-PKCE) auth_code for this confidential client
        code_str = secrets.token_urlsafe(32)
        self.OAuthCode.create({
            'code': code_str,
            'redirect_uri': self.REDIRECT_URI,
            'scope': 'smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
            'user_id': self.user.id,
            'client_id': confidential.id,
        })

        # Without secret → rejected
        status, _ = self._call_token(
            grant_type='authorization_code',
            code=code_str,
            redirect_uri=self.REDIRECT_URI,
            client_id=confidential.client_id,
        )
        self.assertEqual(status, 401)

        # With correct secret → succeeds
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code_str,
            redirect_uri=self.REDIRECT_URI,
            client_id=confidential.client_id,
            client_secret='the-secret',
        )
        self.assertEqual(status, 200, body)
        self.assertIn('access_token', body)

    def test_token_redirect_uri_mismatch_returns_invalid_grant(self):
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri='https://attacker.example.com/callback',
            client_id=self.public_client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid_grant')

    def test_token_unknown_client_id_returns_invalid_client(self):
        status, body = self._call_token(
            grant_type='authorization_code',
            code='whatever',
            redirect_uri=self.REDIRECT_URI,
            client_id='nonexistent-client-id-xyz',
            code_verifier='whatever',
        )
        self.assertEqual(status, 401)
        self.assertEqual(body['error'], 'invalid_client')

    def test_inactive_public_client_cannot_authenticate(self):
        self.public_client.is_active = False
        verifier, challenge = self._pkce_pair()
        code = self._create_auth_code(challenge)
        status, body = self._call_token(
            grant_type='authorization_code',
            code=code,
            redirect_uri=self.REDIRECT_URI,
            client_id=self.public_client.client_id,
            code_verifier=verifier,
        )
        self.assertEqual(status, 401)
        self.assertEqual(body['error'], 'invalid_client')


class TestAuthorizeEndpointPkce(TransactionCase):
    """PKCE enforcement on /oauth2/authorize (GET + POST)."""

    REDIRECT_URI = 'https://my.home-assistant.io/redirect/oauth'

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.OAuthCode = self.env['woow_paas_platform.oauth_code']

        self.public_client = self.OAuthClient.create({
            'name': 'Authorize PKCE Test Client',
            'client_id': 'test-authorize-public-client',
            'redirect_uris': self.REDIRECT_URI,
            'client_type': 'public',
            'require_pkce': True,
            'scopes': 'smarthome:read smarthome:tunnel workspace:read',
            'grant_types': 'authorization_code,refresh_token',
        })

    def _call_authorize_get(self, **kwargs):
        from odoo.addons.woow_paas_platform.controllers.oauth2 import (
            OAuth2Controller,
        )
        captured = {}

        def fake_render(template, ctx):
            captured['template'] = template
            captured['ctx'] = ctx
            # The @route wrapper runs Response.load() on the return value, which
            # rejects a bare MagicMock (TypeError, http.py). Return a real
            # Response so the wrapper passes; assertions read `captured`, which
            # is populated above (F-ARC-1 / gitea #131).
            return WerkzeugResponse()

        request_mock = MagicMock()
        request_mock.env = self.env
        request_mock.render = fake_render
        with patch(
            'odoo.addons.woow_paas_platform.controllers.oauth2.request',
            request_mock,
        ):
            OAuth2Controller().authorize_get(**kwargs)
        return captured

    def _call_authorize_post(self, **kwargs):
        from odoo.addons.woow_paas_platform.controllers.oauth2 import (
            OAuth2Controller,
        )
        captured = {}

        def fake_render(template, ctx):
            captured['render_template'] = template
            captured['render_ctx'] = ctx
            # See _call_authorize_get: return a real Response so the @route
            # wrapper's Response.load() accepts it (F-ARC-1 / gitea #131).
            return WerkzeugResponse()

        def fake_redirect(url, local=False):
            captured['redirect_url'] = url
            return WerkzeugResponse(status=302)

        request_mock = MagicMock()
        request_mock.env = self.env
        request_mock.render = fake_render
        request_mock.redirect = fake_redirect
        with patch(
            'odoo.addons.woow_paas_platform.controllers.oauth2.request',
            request_mock,
        ), patch(
            'odoo.addons.woow_paas_platform.controllers.oauth2.http.request',
            request_mock,
        ):
            OAuth2Controller().authorize_post(**kwargs)
        return captured

    def test_authorize_get_without_challenge_renders_pkce_required(self):
        result = self._call_authorize_get(
            response_type='code',
            client_id=self.public_client.client_id,
            redirect_uri=self.REDIRECT_URI,
            scope='smarthome:read',
            state='xyz',
        )
        self.assertEqual(result['template'], 'woow_paas_platform.oauth2_error')
        self.assertEqual(result['ctx']['error_title'], 'PKCE Required')

    def test_authorize_get_with_plain_method_rejected(self):
        result = self._call_authorize_get(
            response_type='code',
            client_id=self.public_client.client_id,
            redirect_uri=self.REDIRECT_URI,
            scope='smarthome:read',
            state='xyz',
            code_challenge='dummy-challenge-string',
            code_challenge_method='plain',
        )
        self.assertEqual(result['template'], 'woow_paas_platform.oauth2_error')
        self.assertEqual(result['ctx']['error_title'], 'Invalid PKCE Method')

    def test_authorize_post_without_challenge_redirects_with_error(self):
        # Bypass GET: hand-crafted POST without code_challenge must not
        # produce an authorization code, even though the user "approved".
        result = self._call_authorize_post(
            client_id=self.public_client.client_id,
            redirect_uri=self.REDIRECT_URI,
            scope='smarthome:read',
            state='xyz-state',
            approve='1',
        )
        self.assertIn('redirect_url', result)
        self.assertIn('error=invalid_request', result['redirect_url'])
        self.assertIn('state=xyz-state', result['redirect_url'])
        codes = self.OAuthCode.search([
            ('client_id', '=', self.public_client.id),
        ])
        self.assertFalse(codes)

    def _approve_with_scope(self, scope):
        return self._call_authorize_post(
            client_id=self.public_client.client_id,
            redirect_uri=self.REDIRECT_URI,
            scope=scope,
            state='scope-state',
            approve='1',
            code_challenge='scope-test-challenge',
            code_challenge_method='S256',
        )

    def test_authorize_post_rejects_scope_outside_client_allowlist(self):
        """Direct POST must not bypass the scope check on authorize GET."""
        result = self._approve_with_scope('smarthome:read ha:provision')
        self.assertIn('error=invalid_scope', result['redirect_url'])
        self.assertIn('state=scope-state', result['redirect_url'])
        self.assertFalse(self.OAuthCode.search([
            ('client_id', '=', self.public_client.id),
        ]))

    def test_authorize_post_rechecks_current_client_scopes(self):
        """A permission removed after consent GET cannot be issued by POST."""
        self.public_client.write({'scopes': 'smarthome:read'})
        result = self._approve_with_scope('smarthome:tunnel')
        self.assertIn('error=invalid_scope', result['redirect_url'])
        self.assertFalse(self.OAuthCode.search([
            ('client_id', '=', self.public_client.id),
        ]))

    def test_authorize_post_preserves_requested_scope_subset(self):
        result = self._approve_with_scope('  workspace:read  smarthome:read  ')
        self.assertNotIn('error=', result['redirect_url'])
        codes = self.OAuthCode.search([
            ('client_id', '=', self.public_client.id),
        ])
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes.scope, 'workspace:read smarthome:read')

    def test_authorize_post_empty_scope_uses_allowed_defaults(self):
        result = self._approve_with_scope(' \t ')
        self.assertNotIn('error=', result['redirect_url'])
        codes = self.OAuthCode.search([
            ('client_id', '=', self.public_client.id),
        ])
        self.assertEqual(len(codes), 1)
        self.assertEqual(
            codes.scope, ' '.join(sorted(self.public_client.get_allowed_scopes())),
        )

    def test_authorize_post_denied_unallowed_scope_creates_no_code(self):
        result = self._call_authorize_post(
            client_id=self.public_client.client_id,
            redirect_uri=self.REDIRECT_URI,
            scope='ha:provision',
            state='scope-state',
            deny='1',
            code_challenge='scope-test-challenge',
            code_challenge_method='S256',
        )
        self.assertIn('error=access_denied', result['redirect_url'])
        self.assertFalse(self.OAuthCode.search([
            ('client_id', '=', self.public_client.id),
        ]))
