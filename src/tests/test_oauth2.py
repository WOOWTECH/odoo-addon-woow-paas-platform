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
        self.assertIn(
            'https://my.home-assistant.io/redirect/oauth',
            client.redirect_uris,
        )


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
