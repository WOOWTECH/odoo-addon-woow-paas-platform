"""True HTTP controls for OAuth subject/client validity (AC1011)."""
import base64
import hashlib
import secrets
import uuid
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, new_test_user, tagged


REDIRECT_URI = 'http://localhost:8123/auth/callback'


@tagged('post_install', '-at_install', 'oauth_subject_http_ac1011')
class TestOAuthSubjectHttpAC1011(HttpCase):
    """Exercise route dispatch, form parsing, token rows, and HA bearer auth."""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:10]
        self.portal = new_test_user(
            self.env,
            login=f'oauth_http_portal_{suffix}',
            password='oauth-http-sandbox-only',
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True},
        )
        self.assertTrue(self.portal.active)
        self.assertTrue(self.portal.share)
        self.assertFalse(self.portal._is_public())

        self.client = self.env['woow_paas_platform.oauth_client'].sudo().create({
            'name': f'OAuth HTTP subject client {suffix}',
            'client_id': f'oauth-http-subject-{suffix}',
            'client_type': 'public',
            'require_pkce': True,
            'redirect_uris': REDIRECT_URI,
            'scopes': 'workspace:read smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'OAuth HTTP workspace {suffix}',
            'owner_id': self.portal.id,
        })
        access = self.env['woow_paas_platform.workspace_access'].sudo().search([
            ('workspace_id', '=', self.workspace.id),
            ('user_id', '=', self.portal.id),
        ])
        self.assertEqual(access.role, 'owner')
        self.Token = self.env['woow_paas_platform.oauth_token'].sudo()
        self.Code = self.env['woow_paas_platform.oauth_code'].sudo()

    def _token(self, user=None, client=None):
        return self.Token.create({
            'access_token': secrets.token_urlsafe(36),
            'refresh_token': secrets.token_urlsafe(36),
            'scope': 'workspace:read smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'user_id': (user or self.portal).id,
            'client_id': (client or self.client).id,
        })

    def _ha_workspaces(self, access_token):
        return self.url_open(
            '/api/smarthome/workspaces',
            headers={'Authorization': f'Bearer {access_token}'},
        )

    def _refresh(self, refresh_token):
        return self.url_open('/oauth2/token', data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'client_id': self.client.client_id,
        })

    def _code(self, user):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode('ascii')).digest()
        ).rstrip(b'=').decode('ascii')
        code = self.Code.create({
            'code': secrets.token_urlsafe(32),
            'redirect_uri': REDIRECT_URI,
            'scope': 'workspace:read',
            'expires_at': fields.Datetime.now() + timedelta(minutes=10),
            'user_id': user.id,
            'client_id': self.client.id,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
        })
        return code, verifier

    def _exchange(self, code, verifier):
        return self.url_open('/oauth2/token', data={
            'grant_type': 'authorization_code',
            'code': code.code,
            'redirect_uri': REDIRECT_URI,
            'client_id': self.client.client_id,
            'code_verifier': verifier,
        })

    def _assert_no_tokens_in_error(self, response):
        body = response.json()
        self.assertNotIn('access_token', body)
        self.assertNotIn('refresh_token', body)
        return body

    def test_http_active_portal_refresh_and_ha_bearer_succeed(self):
        old = self._token()
        response = self._refresh(old.refresh_token)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotEqual(body['refresh_token'], old.refresh_token)
        self.env.invalidate_all()
        self.assertTrue(old.is_revoked)
        new = self.Token.search([('access_token', '=', body['access_token'])])
        self.assertEqual(new.user_id, self.portal)
        self.assertEqual(new.client_id, self.client)

        ha = self._ha_workspaces(body['access_token'])
        self.assertEqual(ha.status_code, 200, ha.text)
        self.assertIn(
            self.workspace.id,
            {workspace['id'] for workspace in ha.json()['workspaces']},
        )

    def test_http_disabled_subject_denies_bearer_and_refresh_without_new_row(self):
        token = self._token()
        token_count = self.Token.search_count([])
        self.portal.active = False
        self.env.flush_all()

        bearer = self._ha_workspaces(token.access_token)
        self.assertEqual(bearer.status_code, 401, bearer.text)
        self.assertEqual(self._assert_no_tokens_in_error(bearer)['error'], 'Unauthorized')
        refresh = self._refresh(token.refresh_token)
        self.assertEqual(refresh.status_code, 400, refresh.text)
        self.assertEqual(self._assert_no_tokens_in_error(refresh)['error'], 'invalid_grant')
        self.env.invalidate_all()
        self.assertEqual(self.Token.search_count([]), token_count)
        self.assertFalse(token.is_revoked)

        # Disable is a live gate, not durable revocation: re-enable restores an
        # otherwise unexpired, non-revoked bearer token under the current contract.
        self.portal.active = True
        self.env.flush_all()
        restored = self._ha_workspaces(token.access_token)
        self.assertEqual(restored.status_code, 200, restored.text)

    def test_http_public_subject_code_exchange_denied_without_token_row(self):
        code, verifier = self._code(self.env.ref('base.public_user'))
        token_count = self.Token.search_count([])
        response = self._exchange(code, verifier)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._assert_no_tokens_in_error(response)['error'], 'invalid_grant')
        self.env.invalidate_all()
        self.assertEqual(self.Token.search_count([]), token_count)

    def test_http_inactive_client_denies_bearer_and_refresh_without_new_row(self):
        token = self._token()
        token_count = self.Token.search_count([])
        self.client.is_active = False
        self.env.flush_all()

        bearer = self._ha_workspaces(token.access_token)
        self.assertEqual(bearer.status_code, 401, bearer.text)
        self.assertEqual(self._assert_no_tokens_in_error(bearer)['error'], 'Unauthorized')
        refresh = self._refresh(token.refresh_token)
        self.assertEqual(refresh.status_code, 401, refresh.text)
        self.assertEqual(self._assert_no_tokens_in_error(refresh)['error'], 'invalid_client')
        self.env.invalidate_all()
        self.assertEqual(self.Token.search_count([]), token_count)
        self.assertFalse(token.is_revoked)

    def test_http_active_pkce_exchange_issues_one_row_and_ha_bearer_works(self):
        code, verifier = self._code(self.portal)
        token_count = self.Token.search_count([])
        response = self._exchange(code, verifier)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn('access_token', body)
        self.assertIn('refresh_token', body)
        self.env.invalidate_all()
        self.assertEqual(self.Token.search_count([]), token_count + 1)
        issued = self.Token.search([('access_token', '=', body['access_token'])])
        self.assertEqual(issued.user_id, self.portal)
        self.assertTrue(code.is_used)

        ha = self._ha_workspaces(body['access_token'])
        self.assertEqual(ha.status_code, 200, ha.text)
