"""Real authentication, consent GET, CSRF POST, code and token scope boundaries."""
import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlparse

from odoo.tests.common import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install', 'oauth_scope_http')
class TestAuthorizeScopeHttp20261011(HttpCase):
    def setUp(self):
        super().setUp()
        self.owner = new_test_user(
            self.env, login='oauth_scope_http_owner_20261011',
            password='oauth-scope-sandbox-only', groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True},
        )
        self.client = self.env['woow_paas_platform.oauth_client'].create({
            'name': 'Sandbox HTTP consent scope regression',
            'client_id': 'oauth-scope-http-20261011',
            'client_type': 'public',
            'require_pkce': True,
            'redirect_uris': 'http://localhost:8123/auth/callback',
            'scopes': 'workspace:read smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        self.assertTrue(self.client.check_grant_type('authorization_code'))
        self.Code = self.env['woow_paas_platform.oauth_code']
        self.Token = self.env['woow_paas_platform.oauth_token']
        self.verifier = secrets.token_urlsafe(48)
        self.challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self.verifier.encode()).digest()
        ).rstrip(b'=').decode()
        self.authenticate(self.owner.login, 'oauth-scope-sandbox-only')

    def _consent(self, scope='workspace:read'):
        params = {
            'response_type': 'code', 'client_id': self.client.client_id,
            'redirect_uri': 'http://localhost:8123/auth/callback',
            'scope': scope, 'state': 'sandbox-consent-state',
            'code_challenge': self.challenge, 'code_challenge_method': 'S256',
        }
        page = self.url_open('/oauth2/authorize?' + urlencode(params))
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="approve"', page.text, 'Must reach the real consent form')
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        self.assertTrue(csrf, 'Consent form must provide a real session CSRF token')
        return dict(params, csrf_token=csrf.group(1), approve='1')

    def _decision(self, form):
        result = self.url_open('/oauth2/authorize', data=form, allow_redirects=False)
        self.env.invalidate_all()
        self.assertIn(result.status_code, (302, 303))
        location = urlparse(result.headers['Location'])
        self.assertEqual(location.scheme, 'http')
        self.assertEqual(location.netloc, 'localhost:8123')
        self.assertEqual(location.path, '/auth/callback')
        params = parse_qs(location.query)
        self.assertEqual(params.get('state'), ['sandbox-consent-state'])
        return params

    def _assert_no_issue(self):
        self.assertFalse(self.Code.search([('client_id', '=', self.client.id)]))
        self.assertFalse(self.Token.search([('client_id', '=', self.client.id)]))

    def _exchange_and_assert_scope(self, params, expected):
        self.assertNotIn('error', params)
        code = self.Code.search([('code', '=', params['code'][0])])
        self.assertEqual(code.scope, expected)
        self.assertEqual(code.user_id, self.owner)
        result = self.url_open('/oauth2/token', data={
            'grant_type': 'authorization_code', 'client_id': self.client.client_id,
            'redirect_uri': 'http://localhost:8123/auth/callback',
            'code': params['code'][0], 'code_verifier': self.verifier,
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['scope'], expected)
        self.env.invalidate_all()
        token = self.Token.search([('access_token', '=', result.json()['access_token'])])
        self.assertEqual(token.scope, expected)
        self.assertEqual(token.user_id, self.owner)

    def test_http_tampered_post_scope_is_rejected_without_code_or_token(self):
        form = self._consent()
        form['scope'] = 'workspace:read smarthome:tunnel'
        params = self._decision(form)
        self.assertEqual(params.get('error'), ['invalid_scope'])
        self.assertNotIn('code', params)
        self._assert_no_issue()

    def test_http_post_rechecks_allowlist_after_consent_get(self):
        form = self._consent('workspace:read smarthome:read')
        self.client.write({'scopes': 'workspace:read'})
        self.env.flush_all()
        params = self._decision(form)
        self.assertEqual(params.get('error'), ['invalid_scope'])
        self.assertNotIn('code', params)
        self._assert_no_issue()

    def test_http_valid_subset_stays_narrow_at_code_and_token(self):
        form = self._consent()
        form['scope'] = '  workspace:read\t '
        self._exchange_and_assert_scope(self._decision(form), 'workspace:read')

    def test_http_whitespace_scope_uses_current_allowed_defaults(self):
        form = self._consent('')
        form['scope'] = ' \t '
        self._exchange_and_assert_scope(
            self._decision(form), 'smarthome:read workspace:read')

    def test_http_denial_keeps_access_denied_contract(self):
        form = self._consent()
        form.pop('approve')
        form.update(deny='1', scope='smarthome:tunnel')
        params = self._decision(form)
        self.assertEqual(params.get('error'), ['access_denied'])
        self.assertNotIn('code', params)
        self._assert_no_issue()

    def test_http_invalid_csrf_still_rejects_valid_scope(self):
        form = self._consent()
        form['csrf_token'] = 'invalid-sandbox-csrf'
        result = self.url_open('/oauth2/authorize', data=form, allow_redirects=False)
        self.assertEqual(result.status_code, 400)
        self.env.invalidate_all()
        self._assert_no_issue()
