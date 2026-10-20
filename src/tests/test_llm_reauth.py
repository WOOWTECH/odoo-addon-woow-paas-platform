"""LLM-08/N25 password reauth, session isolation and durable attempt audit.

Read-only inspection of llmdev on 2026-10-18 confirmed installed
18.0-20250805 res.users._check_credentials(self, credential, env): password dict +
interactive=True returns {uid, auth_method='password'}; denial raises AccessDenied.
Base checks env.user, so the verifier must never run on the sudo account env.
The sandbox had auth_oauth uninstalled and 10 active/0 passwordless accounts;
staging/prod passwordless coverage remains a release gate (email OTP is not here).

HttpCase uses TestCursor for independent audit cursors. These tests check error
paths keep their reservations without assertRaises rolling the tested writes back.
TestLlmReauthCommitted uses real connections for rollback and concurrency.
"""
import copy
import json
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged

from ..models import llm_account as account_module
from ..models.llm_account import REAUTH_SESSION_KEY
from ..models.llm_common import LlmError, _llm_internal
from .llm_test_support import ACCOUNT, EVENT, CommittedLlmFixture, configure, env_for


class _Session(dict):
    def __init__(self, uid, sid=None):
        super().__init__()
        self.uid = uid
        self.sid = sid or uuid.uuid4().hex


@tagged('post_install', '-at_install', 'llm_reauth')
class TestLlmReauth(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.password = 'Reauth-password-%s!' % suffix
        self.other_password = 'Other-password-%s!' % suffix
        self.alice = new_test_user(
            self.env, login='reauth_a_%s' % suffix, password=self.password,
            groups='base.group_portal', context={'no_reset_password': True})
        self.bob = new_test_user(
            self.env, login='reauth_b_%s' % suffix, password=self.other_password,
            groups='base.group_portal', context={'no_reset_password': True})
        configure(self.env)
        self.account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': self.alice.partner_id.id, 'state': 'active',
            'newapi_username': 'wqa%010d' % self.alice.id,
            'newapi_user_id': self.alice.id,
        })
        self.service = self.env[ACCOUNT].with_user(self.alice)
        self.account_as_alice = self.account.with_user(self.alice).sudo()
        self.session = _Session(self.alice.id)

    def _reauth(self, password=None, session=None):
        return self.service._llm_reauthenticate(
            self.alice.partner_id, self.password if password is None else password,
            self.session if session is None else session, '203.0.113.7')

    def _code(self, call):
        # No assertRaises savepoint: audit mutations must survive the exception.
        try:
            call()
        except LlmError as exc:
            return exc.code
        self.fail('Expected a fixed LlmError')

    def _events(self):
        return self.env[EVENT].sudo().search([
            ('account_id', '=', self.account.id), ('action', '=', 'reauth')], order='id')

    def _require(self, points=1001, session=None, account=None):
        (self.account_as_alice if account is None else account)._llm_require_reauth(
            self.session if session is None else session, points)

    def _rpc(self, params=None):
        body = self.url_open('/api/llm/account/reauth', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
            headers={'Content-Type': 'application/json'}).json()
        self.assertNotIn('error', body)
        return body['result']

    def test_correct_password_checks_current_user_without_sudo_and_returns_expiry(self):
        real = type(self.env['res.users'])._check_credentials
        seen = []

        def verifier(user, credential, env):
            seen.append((user.id, user.env.su, credential['type'], env))
            return real(user, credential, env)

        with patch.object(type(self.env['res.users']), '_check_credentials', verifier):
            result = self._reauth()
        self.assertEqual(seen, [(self.alice.id, False, 'password', {'interactive': True})])
        self.assertRegex(result['reauth_until'], r'\+00:00$')
        self._require()
        self.account.invalidate_recordset(['reauth_at'])
        self.assertTrue(self.account.reauth_at)
        self.assertEqual(self._events().result, 'ok')
        self.assertEqual(self._events().uid, self.alice)

    def test_wrong_and_other_users_passwords_are_rejected_and_audited(self):
        for password in ('wrong', self.other_password):
            self.assertEqual(self._code(lambda: self._reauth(password)), 'llm_reauth_required')
        self.assertEqual(self._events().mapped('result'), ['failed', 'failed'])
        self.assertNotIn(REAUTH_SESSION_KEY, self.session)
        self.assertFalse(self.account.reauth_at)

    def test_passwordless_and_malformed_credentials_fail_closed(self):
        self.env.flush_all()
        self.env.cr.execute('UPDATE res_users SET password = NULL WHERE id = %s',
                            (self.alice.id,))
        self.alice.invalidate_recordset()
        for password in ('anything', '', {}, 5):
            self.assertEqual(self._code(lambda: self._reauth(password)), 'llm_reauth_required')
        self.assertEqual(len(self._events()), 4)
        self.assertNotIn(REAUTH_SESSION_KEY, self.session)

    def test_verifier_exception_never_escapes_or_enters_logs_or_event(self):
        secret = 'raw-password-secret-that-must-not-leak'
        with patch.object(type(self.env['res.users']), '_check_credentials',
                          side_effect=RuntimeError(secret)), \
                patch.object(account_module._logger, 'error') as log:
            self.assertEqual(self._code(lambda: self._reauth(secret)), 'llm_reauth_required')
        log.assert_not_called()
        self.assertNotIn(secret, str(self._events().read()))
        self.assertNotIn(secret, str(self.session))
        self.assertEqual(self._events().error_code, 'llm_reauth_required')

    def test_five_attempts_include_failures_and_success_across_sessions(self):
        for index in range(4):
            other_session = _Session(self.alice.id)
            self.assertEqual(self._code(lambda: self._reauth('wrong', other_session)),
                             'llm_reauth_required')
        self._reauth()
        with patch.object(type(self.env['res.users']), '_check_credentials') as verifier:
            self.assertEqual(self._code(lambda: self._reauth()), 'llm_rate_limited')
        verifier.assert_not_called()
        self.assertEqual(len(self._events()), 5)
        self.assertEqual(self._events().mapped('result'), ['failed'] * 4 + ['ok'])
        self.assertNotIn(REAUTH_SESSION_KEY, self.session)

    def test_old_attempts_leave_the_rolling_hour(self):
        for _index in range(5):
            self.assertEqual(self._code(lambda: self._reauth('wrong')), 'llm_reauth_required')
        self.env.cr.execute(
            'UPDATE woow_paas_platform_llm_event SET create_date = %s WHERE id IN %s',
            (account_module._utcnow() - timedelta(hours=1, seconds=1), tuple(self._events().ids)))
        self._reauth()
        self._require()

    def test_only_strictly_over_1000_requires_reauth(self):
        self._require(1000)
        self.assertEqual(self._code(lambda: self._require(1001)), 'llm_reauth_required')
        self.assertEqual(self._code(lambda: self.account_as_alice._llm_require_reauth(
            self.session)), 'llm_reauth_required')

    def test_expired_future_and_malformed_grants_are_rejected(self):
        self._reauth()
        stamp = account_module.fields.Datetime.to_datetime(
            self.session[REAUTH_SESSION_KEY]['at'])
        for delta in (timedelta(seconds=600), timedelta(seconds=-1)):
            with patch.object(account_module, '_utcnow', return_value=stamp + delta):
                self.assertEqual(self._code(self._require), 'llm_reauth_required')
        for stamp in (None, 'invalid', {}, 5):
            self.session[REAUTH_SESSION_KEY]['at'] = stamp
            self.assertEqual(self._code(self._require), 'llm_reauth_required')

    def test_second_session_and_changed_sid_uid_account_do_not_inherit_grant(self):
        self._reauth()
        other = _Session(self.alice.id)
        self.assertEqual(self._code(lambda: self._require(session=other)), 'llm_reauth_required')
        other.update(copy.deepcopy(self.session))
        self.assertEqual(self._code(lambda: self._require(session=other)), 'llm_reauth_required')
        grant = copy.deepcopy(self.session[REAUTH_SESSION_KEY])
        for key, value in (('uid', self.bob.id), ('account_id', self.account.id + 1)):
            self.session[REAUTH_SESSION_KEY] = dict(grant, **{key: value})
            self.assertEqual(self._code(self._require), 'llm_reauth_required')
        self.session[REAUTH_SESSION_KEY] = grant
        self.session.uid = self.bob.id
        self.assertEqual(self._code(self._require), 'llm_reauth_required')
        self.assertTrue(self.account.reauth_at, 'account audit time never grants another session')

    def test_partner_and_session_user_cannot_be_supplied_for_another_user(self):
        self.assertEqual(self._code(lambda: self.service._llm_reauthenticate(
            self.bob.partner_id, self.other_password, self.session)), 'llm_reauth_required')
        self.assertEqual(self._code(lambda: self._reauth(session=_Session(self.bob.id))),
                         'llm_reauth_required')
        self.assertFalse(self._events())

    def test_audit_failure_never_grants_and_started_attempt_still_counts(self):
        with patch.object(type(self.env[EVENT]), '_llm_reauth_finish',
                          side_effect=LlmError('llm_unavailable')):
            self.assertEqual(self._code(self._reauth), 'llm_unavailable')
        self.assertNotIn(REAUTH_SESSION_KEY, self.session)
        self.assertEqual(self._events().result, 'started')

    def test_reauth_at_is_guarded_even_under_sudo(self):
        self.assertEqual(self._code(lambda: self._require()), 'llm_reauth_required')
        with self.assertRaises(AccessError):
            self.account.sudo().write({'reauth_at': account_module._utcnow()})

    def test_http_endpoint_anchors_owner_ignores_client_ids_and_safe_error(self):
        self.authenticate(self.alice.login, self.password)
        result = self._rpc({'password': self.password, 'uid': self.bob.id,
                            'partner_id': self.bob.partner_id.id, 'reauth': True})
        self.assertTrue(result['success'], result)
        self.assertEqual(set(result['data']), {'reauth_until'})
        self.assertEqual(self._events().uid, self.alice)
        denied = self._rpc({'password': self.other_password})
        self.assertFalse(denied['success'])
        self.assertEqual(denied['code'], 'llm_reauth_required')
        self.assertNotIn(self.other_password, str(denied))

    def test_http_endpoint_respects_existing_visibility_gate(self):
        self.authenticate(self.alice.login, self.password)
        self.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.show_llm', 'False')
        self.assertEqual(self._rpc({'password': self.password})['code'], 'llm_disabled')
        self.assertFalse(self._events())


@tagged('post_install', '-at_install', 'llm_reauth')
class TestLlmReauthCommitted(TransactionCase):
    """Durable reservations survive caller rollback; six workers get five slots."""

    def setUp(self):
        super().setUp()
        self.fixture = CommittedLlmFixture(self).start()
        self.account_id = self.fixture.activate(
            self.fixture.partner('reauth committed %s' % uuid.uuid4().hex[:8]))

    def _count(self):
        with self.registry.cursor() as cr:
            cr.execute("SELECT count(*) FROM woow_paas_platform_llm_event "
                       "WHERE account_id = %s AND action = 'reauth'", (self.account_id,))
            return cr.fetchone()[0]

    def test_failed_attempt_and_audit_survive_caller_rollback(self):
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            event_id = env[EVENT]._llm_reauth_reserve(
                env[ACCOUNT].browse(self.account_id), uid=env.uid)
            env[EVENT]._llm_reauth_finish(event_id, self.account_id, env.uid, False)
            cr.rollback()
        self.assertEqual(self._count(), 1)
        with self.registry.cursor() as cr:
            cr.execute('SELECT result, error_code FROM woow_paas_platform_llm_event '
                       'WHERE id = %s', (event_id,))
            self.assertEqual(cr.fetchone(), ('failed', 'llm_reauth_required'))

    def test_concurrent_sessions_share_five_durable_attempts(self):
        barrier = threading.Barrier(6, timeout=10)
        outcomes = []

        def reserve():
            try:
                with self.registry.cursor() as cr:
                    env = env_for(self.registry, cr)
                    barrier.wait()
                    env[EVENT]._llm_reauth_reserve(
                        env[ACCOUNT].browse(self.account_id), uid=env.uid)
                outcomes.append('reserved')
            except LlmError as exc:
                outcomes.append(exc.code)
            except Exception as exc:  # noqa: BLE001 - report worker failures to test thread
                outcomes.append(type(exc).__name__)

        threads = [threading.Thread(target=reserve, daemon=True) for _index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcomes), ['llm_rate_limited'] + ['reserved'] * 5)
        self.assertEqual(self._count(), 5)
