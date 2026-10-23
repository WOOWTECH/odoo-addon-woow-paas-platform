"""Rollback-scoped canary SQL/worker tests; no independent commit evidence.

The model's module-local deadline_cursor is routed to this HttpCase test
transaction through savepoints. _begin is bypassed only because a test cursor
cannot change its isolation level after queries. _environment and _now are
deterministic; claim, probe receipt, finish, cleanup SQL, and cleanup ORM
methods remain real. Test mode rolls every write back at class teardown.

These tests establish SQL predicates and state transitions on one transaction.
They do not establish lock contention between independent connections,
commit visibility, restart durability, or real upstream behavior.
"""
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.tests.common import new_test_user, tagged

from ..models import llm_canary as module
from ..models.llm_common import LlmError, _llm_internal
from ..models.llm_runtime_state import STATE_TABLE
from ..services import newapi_client as nac
from .llm_test_support import ACCOUNT, EVENT, KEY, TEST_PREFIX
from .test_llm_platform_foundation import _ConfiguredCase


@tagged('post_install', '-at_install', 'llm_canary')
class TestLlmCanary(_ConfiguredCase):

    def setUp(self):
        super().setUp()
        self.State = self.env['woow_paas_platform.llm_runtime_state'].sudo()
        self.Account = self.env[ACCOUNT].sudo()
        self.Keys = self.env[KEY].sudo()
        self.clock = datetime(2031, 6, 8, 12)  # noqa: DTZ001 - Odoo stores naive UTC.
        self.fingerprint = 'canary-test-environment-a'
        self._real_environment = module._environment
        self.account = self._account('Canary internal')
        self.State._llm_ensure_row(self.env.cr)
        self.env.cr.execute(
            f'UPDATE {STATE_TABLE} SET canary_generation=0, canary_started_at=NULL, '
            'canary_lease_until=NULL, canary_work=NULL, canary_probe_result=NULL, '
            'canary_latency_ms=0, canary_failures=0, last_canary_at=NULL, '
            'last_canary_result=NULL')

        @contextmanager
        def rollback_cursor(_factory, _deadline):
            # Do not create a registry cursor: in TransactionCase that would
            # commit independently and could not see this uncommitted fixture.
            with self.env.cr.savepoint(flush=False):
                yield self.env.cr

        for name, replacement in (
                ('deadline_cursor', rollback_cursor),
                ('_begin', lambda *args, **kwargs: None),
                ('_environment', self._environment),
                ('_now', lambda _cr: self.clock)):
            handle = patch.object(module, name, replacement)
            handle.start()
            self.addCleanup(handle.stop)

    def _account(self, name, *, groups='base.group_user'):
        user = new_test_user(
            self.env, login=f"canary_{name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}",
            groups=groups)
        # Build the account on the real res.users partner consumed by the
        # worker's account query (share=True for portal users).
        account = _llm_internal(self.env[ACCOUNT].sudo()).create({
            'partner_id': user.partner_id.id,
        })
        _llm_internal(account).write({
            'state': 'active',
            'newapi_username': nac.newapi_username(TEST_PREFIX, account.id),
        })
        self.env.flush_all()
        return account

    def _environment(self, _cr):
        return {
            'fingerprint': self.fingerprint,
            'proxy_url': nac.PROXY_URL_STG,
            'public_base_url': 'https://newapi-stg.woowtech.io/v1',
            'partner_id': self.account.partner_id.id,
            'prefix': TEST_PREFIX,
            'block': 7,
            'valid': True,
        }

    def deadline(self):
        return time.monotonic() + 30

    def _row(self):
        self.env.cr.execute(f'SELECT * FROM {STATE_TABLE}')
        return self.env.cr.dictfetchone()

    def _claim(self):
        claimed = self.State._canary_claim(self.deadline())
        self.assertTrue(claimed)
        return claimed

    def _mirror(self, claim, *, token_id=None, name=None, state='creating', request_id=None,
                account=None):
        account = account or self.account
        return _llm_internal(self.Keys).create({
            'account_id': account.id,
            'kind': 'free_trial',
            'client_request_id': request_id or claim.request_id,
            'state': state,
            'newapi_name': name or f'wp_{account.id}_test',
            'newapi_token_id': token_id,
            'canary_generation': claim.generation,
            'expires_at': self.clock + timedelta(minutes=10),
            'model_allowlist': ','.join(nac.FREE_TRIAL_MODELS),
        })

    def _key_state(self, key_id):
        self.env.cr.execute('SELECT state FROM woow_paas_platform_llm_api_key WHERE id=%s', (key_id,))
        return self.env.cr.fetchone()[0]

    def _cleanup_client(self, *, listed=None, deleted=None):
        if listed is None:
            listed = nac.Ok({'items': [], 'total': 0})
        if deleted is None:
            deleted = nac.Ok({})

        class Client:
            base_url = nac.PROXY_URL_STG

            def __init__(self):
                self.deleted_ids = []

            def user_list_tokens(_self, *args, **kwargs):
                return listed

            def user_delete_token(_self, token_ip, token, token_id, **kwargs):
                _self.deleted_ids.append(token_id)
                return deleted

        client = Client()
        AccountClass = type(self.Account)
        client_patch = patch.object(AccountClass, '_llm_client', return_value=client)
        token_patch = patch.object(type(self.account), '_llm_up_token', return_value='up-token')
        client_patch.start()
        token_patch.start()
        self.addCleanup(token_patch.stop)
        self.addCleanup(client_patch.stop)
        return client

    def test_inactive_cron_and_n27_make_no_external_call_or_alert(self):
        self.assertFalse(self.env.ref(module.CRON_XMLID).active)
        account_cls = type(self.Account)
        event_cls = type(self.env[EVENT])
        config = {'proxy_state': 'unset', 'canary': ''}
        with patch.object(module.common, 'llm_config', return_value=config), \
                patch.object(module.common, 'llm_unconfigured', return_value=True), \
                patch.object(account_cls, '_llm_client') as client, \
                patch.object(event_cls, '_llm_alert') as alert, \
                patch.object(type(self.State), '_canary_claim') as claim:
            self.assertFalse(self.State._cron_llm_canary())
        client.assert_not_called()
        alert.assert_not_called()
        claim.assert_not_called()

    def test_claim_excludes_live_owner_and_new_generation_fences_old_work(self):
        first, first_work = self._claim()
        self.assertEqual(first_work['phase'], 'creating')
        self.assertFalse(self.State._canary_claim(self.deadline()))

        self.clock += timedelta(seconds=46)
        second, second_work = self._claim()
        self.assertGreater(second.generation, first.generation)
        self.assertEqual(second.request_id, first.request_id)
        self.assertEqual(second_work['phase'], 'cleanup')
        self.assertFalse(self.State._canary_save_probe(first, 'ok', 5, self.deadline()))
        self.assertFalse(self.State._canary_finish(first, True, None, self.deadline()))
        self.assertEqual(self._row()['canary_generation'], second.generation)
        self.assertEqual(self._row()['canary_work']['phase'], 'cleanup')

    def test_claim_is_due_after_thirty_minutes(self):
        self.env.cr.execute(f'UPDATE {STATE_TABLE} SET canary_started_at=%s',
                            (self.clock - timedelta(minutes=30),))
        claim, work = self._claim()
        self.assertEqual(work['phase'], 'creating')
        self.assertEqual(work['account_id'], self.account.id)
        self.assertEqual(self._row()['canary_started_at'], self.clock)
        self.assertTrue(module.policy.authorized(claim))

    def test_cleanup_retries_count_one_failure_per_thirty_minute_sample(self):
        claim, _work = self._claim()
        self.assertTrue(self.State._canary_save_probe(claim, 'probe_failed', 17, self.deadline()))
        event_cls = type(self.env[EVENT])
        with patch.object(event_cls, '_llm_alert') as alert:
            self.assertTrue(self.State._canary_finish(
                claim, False, 'cleanup_pending', self.deadline()))
            self.assertEqual(self._row()['canary_failures'], 1)
            alert.assert_not_called()

            self.clock += timedelta(minutes=1)
            retry, _work = self._claim()
            self.assertTrue(self.State._canary_finish(
                retry, False, 'cleanup_pending', self.deadline()))
            self.assertEqual(self._row()['canary_failures'], 1)
            alert.assert_not_called()

            self.clock += timedelta(minutes=30)
            next_sample, _work = self._claim()
            self.assertTrue(self.State._canary_finish(
                next_sample, False, 'cleanup_pending', self.deadline()))
            self.assertEqual(self._row()['canary_failures'], 2)
            alert.assert_called_once()

    def test_successful_sample_clears_failure_streak(self):
        self.env.cr.execute(f'UPDATE {STATE_TABLE} SET canary_failures=3')
        claim, _work = self._claim()
        self.assertTrue(self.State._canary_save_probe(claim, 'ok', 9, self.deadline()))
        # No mirror means create was never attempted; cleanup's real SQL path
        # treats it as complete without asking the external client.
        with patch.object(type(self.Account), '_llm_client',
                          side_effect=AssertionError('no mirror must make no client')):
            self.assertEqual(self.State._canary_cleanup(claim, _work, self.deadline()), (True, None))
        self.assertFalse(self.State._canary_finish(claim, True, None, self.deadline()))
        self.assertEqual(self._row()['canary_failures'], 0)
        self.assertEqual(self._row()['last_canary_result'], 'ok')

    def test_bad_configuration_and_portal_only_identity_are_rejected(self):
        claim, _work = self._claim()
        original = self.fingerprint
        self.fingerprint = 'changed-fingerprint'
        with self.assertRaises(LlmError):
            self.State._canary_validate_claim_cr(self.env.cr, claim)
        self.fingerprint = original

    def test_real_environment_fingerprints_only_canary_identity_settings(self):
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param(module.common.PARAM_CANARY_PARTNER_ID, str(self.account.partner_id.id))
        icp.set_param(module.common.PARAM_PUBLIC_BASE_URL,
                      'https://newapi-stg.woowtech.io/v1')
        icp.set_param(module.common.PARAM_ALERT_EMAIL, 'first-alert@example.org')
        self.env.flush_all()
        baseline = self._real_environment(self.env.cr)

        icp.set_param(module.common.PARAM_ALERT_EMAIL, 'second-alert@example.org')
        self.env.flush_all()
        alert_changed = self._real_environment(self.env.cr)
        self.assertEqual(alert_changed['fingerprint'], baseline['fingerprint'])

        icp.set_param(module.common.PARAM_PROXY_URL, nac.PROXY_URL_PROD)
        self.env.flush_all()
        proxy_changed = self._real_environment(self.env.cr)
        self.assertNotEqual(proxy_changed['fingerprint'], baseline['fingerprint'])

    def test_invalid_canary_identity_records_two_failures_without_network(self):
        # Model a malformed value introduced outside the normal parameter validator,
        # as can happen after a direct SQL edit.
        self.env.cr.execute(
            'INSERT INTO ir_config_parameter '
            '(key, value, create_uid, write_uid, create_date, write_date) '
            "VALUES (%s, %s, 1, 1, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC') "
            'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, '
            'write_uid=EXCLUDED.write_uid, write_date=EXCLUDED.write_date',
            (module.common.PARAM_CANARY_PARTNER_ID, 'abc'))
        self.env.flush_all()
        actual = self._real_environment(self.env.cr)
        self.assertTrue(actual['enabled'])
        self.assertEqual(actual['partner_id'], 0)
        self.assertFalse(actual['valid'])

        # Isolate the enabled-but-invalid worker state: it must persist
        # account_unavailable samples while taking the cleanup-only path.
        environment = {
            **self._environment(self.env.cr),
            'enabled': True,
            'partner_id': 0,
            'valid': False,
        }
        event_cls = type(self.env[EVENT])
        with patch.object(module, '_environment', return_value=environment), \
                patch.object(type(self.Account), '_llm_client',
                             side_effect=AssertionError('invalid identity must not call network')), \
                patch.object(module.llm_canary_probe, 'probe',
                             side_effect=AssertionError('invalid identity must not probe')), \
                patch.object(event_cls, '_llm_alert') as alert:
            for sample in range(2):
                if sample:
                    self.clock += timedelta(minutes=30)
                tick = type('Tick', (), {'deadline': self.deadline(), 'delay': None})()
                self.assertFalse(self.State._canary_tick({}, tick))

        self.assertEqual(self._row()['canary_failures'], 2)
        self.assertEqual(self._row()['last_canary_result'], 'account_unavailable')
        events = self.env[EVENT].sudo().search([('action', '=', 'canary')], order='id')
        self.assertEqual(events.mapped('result'), ['failed', 'failed'])
        self.assertEqual([json.loads(event.details_json)['count'] for event in events], [1, 2])
        alert.assert_called_once()

    def test_portal_only_account_cannot_be_claimed_as_internal_canary(self):
        portal_account = self._account('Canary portal only', groups='base.group_portal')
        self.env.flush_all()
        self.env.cr.execute('SELECT id FROM res_users WHERE partner_id=%s AND active AND share',
                            (portal_account.partner_id.id,))
        self.assertTrue(self.env.cr.fetchone())
        self.assertIsNone(module._account(self.env.cr, portal_account.partner_id.id))
        self.assertEqual(module._account(self.env.cr, self.account.partner_id.id)['id'], self.account.id)
        # Exercise the real account-identity SQL through a claim whose configured
        # partner has only a portal user. It must become cleanup-only work.
        self.account = portal_account
        with patch.object(module, '_environment', return_value={
                **self._environment(self.env.cr), 'partner_id': portal_account.partner_id.id}):
            unavailable, work = self.State._canary_claim(self.deadline())
        self.assertTrue(unavailable)
        self.assertEqual(work['phase'], 'cleanup')
        self.assertEqual(work['account_id'], 0)

    def test_cleanup_selects_request_and_account_and_refuses_name_id_conflict(self):
        claim, work = self._claim()
        target = self._mirror(claim, token_id=31, name='wp_target', state='active')
        self._mirror(claim, token_id=99, name='wp_other_request', state='failed_reveal',
                     request_id=str(uuid.uuid4()))
        other_account = self._account('Canary other account')
        self._mirror(claim, token_id=100, name='wp_other_account', state='active', account=other_account)
        self.env.flush_all()
        self._cleanup_client(listed=nac.Ok({'items': [{'id': 31, 'name': target.newapi_name}],
                                            'total': 1}))
        cleaned, failure = self.State._canary_cleanup(claim, work, self.deadline())
        self.assertTrue(cleaned)
        self.assertIsNone(failure)
        self.assertEqual(self._key_state(target.id), 'deleted')
        self.assertEqual(self._key_state(self.Keys.search([('newapi_token_id', '=', 99)]).id),
                         'failed_reveal')
        self.assertEqual(self._key_state(self.Keys.search([('newapi_token_id', '=', 100)]).id), 'active')
        self.assertFalse(self.State._canary_finish(claim, True, None, self.deadline()))

        self.clock += timedelta(minutes=30)
        second, second_work = self._claim()
        conflicting = self._mirror(second, token_id=41, name='wp_conflict', state='active')
        self.env.flush_all()
        self._cleanup_client(listed=nac.Ok({
            'items': [{'id': 42, 'name': conflicting.newapi_name}], 'total': 1}))
        client = type(self.Account)._llm_client(self.Account, nac.ROLE_CRON)
        with patch.object(client, 'user_delete_token') as delete:
            cleaned, failure = self.State._canary_cleanup(second, second_work, self.deadline())
        self.assertFalse(cleaned)
        self.assertEqual(failure, 'cleanup_pending')
        delete.assert_not_called()
        self.assertEqual(self._key_state(conflicting.id), 'active')

    def test_cleanup_delete_unknown_keeps_mirror_pending(self):
        claim, work = self._claim()
        key = self._mirror(claim, token_id=55, name='wp_delete_unknown', state='active')
        self.env.flush_all()
        self._cleanup_client(
            listed=nac.Ok({'items': [{'id': 55, 'name': key.newapi_name}], 'total': 1}),
            deleted=nac.Unknown())
        cleaned, failure = self.State._canary_cleanup(claim, work, self.deadline())
        self.assertFalse(cleaned)
        self.assertEqual(failure, 'cleanup_pending')
        self.assertEqual(self._key_state(key.id), 'active')

    def test_cleanup_list_failure_never_deletes(self):
        claim, work = self._claim()
        key = self._mirror(claim, token_id=56, name='wp_list_failed', state='active')
        self.env.flush_all()
        client = self._cleanup_client(listed=nac.Unknown())

        cleaned, failure = self.State._canary_cleanup(claim, work, self.deadline())

        self.assertFalse(cleaned)
        self.assertEqual(failure, 'cleanup_pending')
        self.assertEqual(client.deleted_ids, [])
        self.assertEqual(self._key_state(key.id), 'active')

    def test_cleanup_incomplete_list_never_deletes(self):
        claim, work = self._claim()
        key = self._mirror(claim, token_id=57, name='wp_list_incomplete', state='active')
        self.env.flush_all()
        client = self._cleanup_client(listed=nac.Ok({'items': [], 'total': 1}))

        cleaned, failure = self.State._canary_cleanup(claim, work, self.deadline())

        self.assertFalse(cleaned)
        self.assertEqual(failure, 'cleanup_pending')
        self.assertEqual(client.deleted_ids, [])
        self.assertEqual(self._key_state(key.id), 'active')

    def test_missing_key_has_no_external_call_and_unknown_post_waits_until_expiry(self):
        claim, work = self._claim()
        with patch.object(type(self.Account), '_llm_client',
                          side_effect=AssertionError('missing mirror means no upstream call')):
            self.assertEqual(self.State._canary_cleanup(claim, work, self.deadline()), (True, None))
        self.assertFalse(self.State._canary_finish(claim, True, None, self.deadline()))

        self.clock += timedelta(minutes=30)
        retry, retry_work = self._claim()
        key = self._mirror(retry, token_id=None, name='wp_unknown_post', state='failed_reveal')
        self.env.flush_all()
        self._cleanup_client(listed=nac.Ok({'items': [], 'total': 0}))
        client = type(self.Account)._llm_client(self.Account, nac.ROLE_CRON)
        self.assertEqual(self.State._canary_cleanup(retry, retry_work, self.deadline()),
                         (False, 'cleanup_pending'))
        self.assertEqual(self._key_state(key.id), 'failed_reveal')

        self.clock = datetime.fromisoformat(retry_work['expires_at']) + timedelta(seconds=1)
        expired, expired_work = self._claim()
        with patch.object(client, 'user_list_tokens', return_value=nac.Ok({'items': [], 'total': 0})):
            cleaned, failure = self.State._canary_cleanup(expired, expired_work, self.deadline())
        self.assertTrue(cleaned)
        self.assertIsNone(failure)
        self.assertEqual(self._key_state(key.id), 'deleted')

    def test_real_worker_tick_runs_claim_probe_cleanup_and_finish(self):
        claim_result = {}

        def create(_keys, account, claim, _deadline):
            claim_result['claim'] = claim
            key = self._mirror(claim, token_id=71, name='wp_tick', state='active', account=account)
            self.env.flush_all()
            return {'key': 'one-time-secret', 'key_id': key.id}

        class Probe:
            code = 'ok'
            latency_ms = 23

        self._cleanup_client(listed=nac.Ok({'items': [{'id': 71, 'name': 'wp_tick'}], 'total': 1}))
        key_cls = type(self.Keys)
        with patch.object(key_cls, '_llm_create_canary_key', create), \
                patch.object(module.llm_canary_probe, 'probe', return_value=Probe()), \
                patch.object(type(self.env[EVENT]), '_llm_alert') as alert:
            tick = type('Tick', (), {'deadline': self.deadline(), 'delay': None})()
            self.assertFalse(self.State._canary_tick({}, tick))
        self.assertTrue(module.policy.authorized(claim_result['claim']))
        self.assertEqual(self._row()['canary_probe_result'], 'ok')
        self.assertEqual(self._row()['canary_work'], {})
        self.assertEqual(self._row()['last_canary_result'], 'ok')
        self.assertFalse(alert.called)

    def test_probe_failure_still_deletes_canary_key(self):
        claim_result = {}

        def create(_keys, account, claim, _deadline):
            claim_result['claim'] = claim
            key = self._mirror(claim, token_id=72, name='wp_probe_failed',
                               state='active', account=account)
            self.env.flush_all()
            claim_result['key_id'] = key.id
            return {'key': 'one-time-secret', 'key_id': key.id}

        class Probe:
            code = 'unavailable'
            latency_ms = 19

        client = self._cleanup_client(listed=nac.Ok({
            'items': [{'id': 72, 'name': 'wp_probe_failed'}], 'total': 1}))
        with patch.object(type(self.Keys), '_llm_create_canary_key', create), \
                patch.object(module.llm_canary_probe, 'probe', return_value=Probe()):
            tick = type('Tick', (), {'deadline': self.deadline(), 'delay': None})()
            self.assertFalse(self.State._canary_tick({}, tick))

        self.assertEqual(client.deleted_ids, [72])
        self.assertEqual(self._key_state(claim_result['key_id']), 'deleted')
        self.assertEqual(self._row()['canary_probe_result'], 'probe_failed')
        self.assertEqual(self._row()['last_canary_result'], 'probe_failed')
