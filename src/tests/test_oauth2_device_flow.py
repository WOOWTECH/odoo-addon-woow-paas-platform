"""Tests for OAuth 2.0 Device Authorization Grant (RFC 8628)."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, new_test_user

_CTRL = 'odoo.addons.woow_paas_platform.controllers.oauth2'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'


class TestOAuthDeviceCodeModel(TransactionCase):
    def setUp(self):
        super().setUp()
        self.DeviceCode = self.env['woow_paas_platform.oauth_device_code']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def test_create_for_client_generates_codes(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        self.assertTrue(dc.device_code)
        self.assertRegex(dc.user_code, r'^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$')
        self.assertEqual(dc.status, 'pending')
        self.assertEqual(dc.client_id, self.client)
        self.assertFalse(dc.user_id)
        self.assertGreater(dc.expires_at, fields.Datetime.now())

    def test_user_code_entropy_alphabet(self):
        code = self.DeviceCode._generate_user_code().replace('-', '')
        self.assertEqual(len(code), 8)
        for ch in code:
            self.assertIn(ch, 'BCDFGHJKLMNPQRSTVWXZ')

    def test_is_expired(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        self.assertFalse(dc.is_expired())
        dc.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertTrue(dc.is_expired())

    def test_register_poll_slow_down_and_cap(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        self.assertFalse(dc.register_poll())   # first poll OK
        self.assertTrue(dc.register_poll())    # immediate second -> too fast
        self.assertGreater(dc.interval, 5)     # interval bumped
        for _ in range(10):
            dc.register_poll()
        self.assertLessEqual(dc.interval, 30)  # capped

    def test_mark_approved_denied(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.mark_approved(self.env.user)
        self.assertEqual(dc.status, 'approved')
        self.assertEqual(dc.user_id, self.env.user)
        dc2 = self.DeviceCode.create_for_client(self.client, '')
        dc2.mark_denied()
        self.assertEqual(dc2.status, 'denied')

    def test_consume_race_safe_single_winner(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        dc.mark_approved(self.env.user)
        self.assertTrue(dc.consume())       # first poll wins -> issue
        self.assertFalse(dc.consume())      # replay loses -> NO double-issue
        self.assertEqual(dc.status, 'consumed')

    def test_cron_gc_expired_removes_old(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.expires_at = fields.Datetime.now() - timedelta(hours=2)
        self.DeviceCode._cron_gc_expired()
        self.assertFalse(dc.exists())


class TestDeviceCodeEndpoints(TransactionCase):
    """Mock `request` (cursor stays in the test transaction) and call the
    controller methods directly — deterministic coverage of the handler's five
    RFC 8628 §3.5 branches + device_authorization + /device approve/deny, which
    a flaky HttpCase round-trip cannot pin (adversarial-review finding B/C)."""

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.DeviceCode = self.env['woow_paas_platform.oauth_device_code']
        self.user = new_test_user(
            self.env,
            login='device_flow_active_subject',
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True},
        )
        self.assertTrue(self.user.active)
        self.assertTrue(self.user.share)
        self.assertFalse(self.user._is_public())
        self.client = self.OAuthClient.create({
            'name': 'Device Test Client',
            'client_id': 'test-device-client',
            'redirect_uris': 'https://example.com/cb',
            'client_type': 'public',
            'require_pkce': True,
            'scopes': 'smarthome:read smarthome:tunnel workspace:read',
            'grant_types': 'authorization_code,refresh_token,' + DEVICE_GRANT,
            'is_active': True,
        })
        self.assertTrue(self.client.is_active)

    def _ctrl(self):
        from odoo.addons.woow_paas_platform.controllers.oauth2 import OAuth2Controller
        return OAuth2Controller()

    def _req(self, session=None):
        from odoo import http
        m = MagicMock()
        # Direct controller calls must see the same active subject that approves
        # the device request, never TransactionCase's inactive ROOT1.
        m.env = self.env(user=self.user)
        m.session = session if session is not None else {}
        # render() must return a real Response — the route wrapper type-checks
        # the handler's return value (MagicMock would raise TypeError). We only
        # assert side effects (status / session) for the render paths.
        m.render.return_value = http.Response('ok')
        return m

    def _call(self, method_name, _session=None, **payload):
        req = self._req(_session)
        with patch(f'{_CTRL}.request', req):
            resp = getattr(self._ctrl(), method_name)(**payload)
        # JSON endpoints return http.Response with JSON body; render() returns
        # a MagicMock (we assert side effects for those, not the body).
        try:
            body = json.loads(resp.get_data(as_text=True))
            return resp.status_code, body, req
        except (AttributeError, TypeError, ValueError):
            return None, None, req

    def _poll(self, device_code, _session=None):
        return self._call(
            'token', _session=_session, grant_type=DEVICE_GRANT,
            device_code=device_code, client_id=self.client.client_id,
        )

    # ---- device_authorization endpoint ----
    def test_device_authorization_returns_rfc8628_fields(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://stg.example.com')
        status, body, _ = self._call(
            'device_authorization', client_id=self.client.client_id, scope='smarthome:read',
        )
        self.assertEqual(status, 200, body)
        self.assertIn('device_code', body)
        self.assertIn('user_code', body)
        self.assertEqual(body['verification_uri'], 'https://stg.example.com/device')
        self.assertIn(body['user_code'], body['verification_uri_complete'])
        self.assertEqual(body['interval'], 5)
        self.assertGreater(body['expires_in'], 0)

    def test_device_authorization_unknown_client(self):
        status, body, _ = self._call('device_authorization', client_id='nope')
        self.assertEqual(status, 401)
        self.assertEqual(body['error'], 'invalid_client')

    def test_device_authorization_empty_scope_resolves_default(self):
        self.env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://stg.example.com')
        status, body, _ = self._call('device_authorization', client_id=self.client.client_id)
        dc = self.DeviceCode.sudo().search([('device_code', '=', body['device_code'])])
        # consent will show exactly what the token carries (no empty-scope mismatch)
        self.assertTrue(dc.scope)
        self.assertIn('smarthome:read', dc.scope)

    # ---- token endpoint device_code grant: five branches ----
    def test_poll_unknown_device_code(self):
        _, body, _ = self._poll('nonexistent')
        self.assertEqual(body['error'], 'invalid_grant')

    def test_poll_pending_authorization_pending(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        _, body, _ = self._poll(dc.device_code)
        self.assertEqual(body['error'], 'authorization_pending')

    def test_poll_too_fast_slow_down(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        self._poll(dc.device_code)                 # first poll
        _, body, _ = self._poll(dc.device_code)    # immediate -> slow_down
        self.assertEqual(body['error'], 'slow_down')

    def test_poll_denied_access_denied(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.mark_denied()
        _, body, _ = self._poll(dc.device_code)    # terminal beats slow_down
        self.assertEqual(body['error'], 'access_denied')

    def test_poll_expired_expired_token(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        _, body, _ = self._poll(dc.device_code)
        self.assertEqual(body['error'], 'expired_token')

    def test_poll_approved_issues_token_then_replay_invalid(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        dc.mark_approved(self.user)
        status, body, _ = self._poll(dc.device_code)
        self.assertEqual(status, 200, body)
        self.assertIn('access_token', body)
        self.assertIn('refresh_token', body)
        self.assertIn('smarthome:read', body['scope'])
        # replay -> consumed -> invalid_grant (single-use, no double-issue)
        _, body2, _ = self._poll(dc.device_code)
        self.assertEqual(body2['error'], 'invalid_grant')

    # ---- /device verification page side effects ----
    def test_device_verify_approve_marks_approved(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        self._call('device_verify_post', user_code=dc.user_code, approve='1')
        dc.invalidate_recordset(['status', 'user_id'])
        self.assertEqual(dc.status, 'approved')
        self.assertEqual(dc.user_id, self.user)

    def test_device_verify_deny_marks_denied(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        self._call('device_verify_post', user_code=dc.user_code, deny='1')
        dc.invalidate_recordset(['status'])
        self.assertEqual(dc.status, 'denied')

    def test_device_result_title_matches_outcome(self):
        """拒絕頁的分頁標題曾寫死成「Device Authorized」（2026-10-01 正式站實測），
        與內文「Request Denied」相反。標題要跟著結果走。"""
        qweb = self.env['ir.qweb']
        denied = str(qweb._render('woow_paas_platform.device_result',
                                  {'approved': False, 'message': None}))
        self.assertIn('<title>Request Denied - Woow PaaS</title>', denied)
        self.assertNotIn('Device Authorized', denied)
        approved = str(qweb._render('woow_paas_platform.device_result',
                                    {'approved': True}))
        self.assertIn('<title>Device Connected - Woow PaaS</title>', approved)

    def test_device_verify_invalid_code_increments_session(self):
        session = {}
        self._call('device_verify_post', _session=session,
                   user_code='WRNG-CODE', verify='1')
        self.assertEqual(session.get('device_verify_fails'), 1)

    def test_device_verify_throttle_locks_out(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        session = {'device_verify_fails': 5}
        # even a VALID code is refused while locked out
        self._call('device_verify_post', _session=session,
                   user_code=dc.user_code, approve='1')
        dc.invalidate_recordset(['status'])
        self.assertEqual(dc.status, 'pending')  # not approved — throttled


class TestDeviceGrantHealer(TransactionCase):
    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def test_seed_has_device_grant(self):
        self.assertTrue(self.client.check_grant_type(DEVICE_GRANT))
        # hybrid: browser-flow grant still present alongside device_code
        self.assertTrue(self.client.check_grant_type('authorization_code'))

    def test_healer_adds_device_grant_to_legacy(self):
        self.client.grant_types = 'authorization_code,refresh_token'
        self.assertFalse(self.client.check_grant_type(DEVICE_GRANT))
        self.OAuthClient._ensure_ha_smart_home_device_grant()
        self.assertTrue(self.client.check_grant_type(DEVICE_GRANT))
        self.assertTrue(self.client.check_grant_type('authorization_code'))

    def test_healer_idempotent(self):
        self.OAuthClient._ensure_ha_smart_home_device_grant()
        self.OAuthClient._ensure_ha_smart_home_device_grant()
        grants = [g.strip() for g in self.client.grant_types.split(',')]
        self.assertEqual(grants.count(DEVICE_GRANT), 1)
