"""Real PostgreSQL transaction and row-lock evidence for the LLM canary.

Run only in the dedicated scratch workflow. Every fixture is synthetic, has no
email address, and is removed by its exact account/request identity. The
runtime singleton is snapshotted and restored around each test.
"""
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from ...models import llm_canary as canary_model
from ...models.llm_api_key import KEY_TABLE
from ...models.llm_common import LlmError, _llm_internal
from ...models.llm_event import EVENT_TABLE
from ...models.llm_free_usage import USAGE_TABLE
from ...models.llm_runtime_state import STATE_TABLE
from ...services import llm_canary as canary_policy
from ...services import newapi_client as nac

ACCOUNT = 'woow_paas_platform.llm_account'
KEY = 'woow_paas_platform.llm_api_key'
CANARY_COLUMNS = (
    'canary_generation', 'canary_started_at', 'canary_lease_until', 'canary_work',
    'canary_probe_result', 'canary_latency_ms', 'canary_failures',
    'last_canary_at', 'last_canary_result',
)
RUNTIME_SNAPSHOT_COLUMNS = CANARY_COLUMNS + ('circuit_failures',)
CANARY_ENV = 'WOOW_LLM_CANARY_PG_TEST'
CANARY_ENV_VALUE = 'isolated-v1'
TEST_PREFIX = 'wqa'
DAY = date(2036, 4, 5)
INITIAL_CANARY_STATE = {
    'canary_generation': 0,
    'canary_started_at': None,
    'canary_lease_until': None,
    'canary_work': None,
    'canary_probe_result': None,
    'canary_latency_ms': 0,
    'canary_failures': 0,
    'last_canary_at': None,
    'last_canary_result': None,
}


@tagged('-standard', '-at_install', 'post_install', 'llm_canary_pg')
class TestLlmCanaryPostgres(BaseCase):
    """These tests require committed scratch-DB fixtures and real cursors."""

    def setUp(self):
        super().setUp()
        if os.environ.get(CANARY_ENV) != CANARY_ENV_VALUE or get_db_name() != 'test':
            self.fail('llm_canary_pg requires isolated-v1 and database name test')

        self.registry = Registry(get_db_name())
        # BaseCase does not provide TransactionCase's ``self.env``. Own the
        # outer transaction explicitly, as the existing real-PG tests do.
        self.outer = self.registry.cursor()
        self.outer_env = api.Environment(self.outer, odoo.SUPERUSER_ID, {})
        self.addCleanup(self._close_outer)
        self.account_id = None
        self.user_id = None
        self.partner_id = None
        self.fixture_tag = uuid.uuid4().hex
        self.forbidden_calls = []

        # This is a module-local policy input. Account identity continues to be
        # selected by the real runtime SQL against res_users and llm_account.
        self.environment = {
            'fingerprint': 'canary-pg-' + self.fixture_tag,
            'proxy_url': 'https://llm-canary-proxy.invalid',
            'public_base_url': 'https://canary.invalid/v1',
            'partner_id': None,
            'prefix': TEST_PREFIX,
            'block': 7,
            'valid': True,
        }
        env_patch = patch.object(canary_model, '_environment', self._environment)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        def forbidden(name):
            def reject(*_args, **_kwargs):
                self.forbidden_calls.append(name)
                raise AssertionError('llm_canary_pg forbids outbound calls and alerts')
            return reject

        account_class = type(self.outer_env[ACCOUNT])
        event_class = type(self.outer_env['woow_paas_platform.llm_event'])
        for target, name, label in (
                (canary_model.llm_canary_probe, 'probe', 'probe'),
                (account_class, '_llm_client', 'upstream_client'),
                (account_class, '_llm_up_token', 'upstream_token'),
                (event_class, '_llm_alert', 'alert')):
            blocker = patch.object(target, name, forbidden(label))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.addCleanup(self._assert_no_forbidden_calls)

        with self.registry.cursor() as cr:
            canary_model_model = api.Environment(cr, odoo.SUPERUSER_ID, {})[
                'woow_paas_platform.llm_runtime_state']
            canary_model_model._llm_ensure_row(cr)
            cr.execute(f"SELECT {', '.join(RUNTIME_SNAPSHOT_COLUMNS)} FROM {STATE_TABLE}")
            self.runtime_snapshot = cr.dictfetchone()
            self.addCleanup(self._cleanup_fixture)
            self._write_canary_state(cr, INITIAL_CANARY_STATE)
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            label = 'Synthetic canary PG ' + self.fixture_tag
            partner = env['res.partner'].create({'name': label})
            self.partner_id = partner.id
            user = env['res.users'].with_context(no_reset_password=True).create({
                'name': label,
                'login': 'llm-canary-pg-' + self.fixture_tag,
                'partner_id': partner.id,
                'groups_id': [(6, 0, [env.ref('base.group_user').id])],
            })
            self.user_id = user.id
            account = _llm_internal(env[ACCOUNT].sudo()).create({'partner_id': partner.id})
            self.account_id = account.id
            _llm_internal(account).write({
                'state': 'active',
                'newapi_username': nac.newapi_username(TEST_PREFIX, account.id),
            })
            self.environment['partner_id'] = partner.id
            cr.commit()

    def _close_outer(self):
        if getattr(self, 'outer', None) and not self.outer.closed:
            self.outer.rollback()
            self.outer.close()

    def _assert_no_forbidden_calls(self):
        self.assertEqual(self.forbidden_calls, [],
                         f'outbound/alert boundary was called: {self.forbidden_calls}')

    @staticmethod
    def _write_canary_state(cr, values):
        cr.execute(f'UPDATE {STATE_TABLE} SET ' + ', '.join(
            f'{column}=%s' for column in values),
            [json.dumps(values[column]) if column == 'canary_work'
             and values[column] is not None else values[column]
             for column in values])

    def _environment(self, _cr):
        return dict(self.environment)

    def _deadline(self):
        return time.monotonic() + 30

    def _claim(self):
        claimed = self.outer_env['woow_paas_platform.llm_runtime_state']._canary_claim(
            self._deadline())
        self.assertTrue(claimed)
        return claimed

    def _runtime(self, cr=None):
        if cr is not None:
            cr.execute(f"SELECT {', '.join(CANARY_COLUMNS)} FROM {STATE_TABLE}")
            return cr.dictfetchone()
        with self.registry.cursor() as verify:
            verify.execute(f"SELECT {', '.join(CANARY_COLUMNS)} FROM {STATE_TABLE}")
            return verify.dictfetchone()

    def _set_runtime(self, assignments, values):
        with self.registry.cursor() as cr:
            cr.execute(f"UPDATE {STATE_TABLE} SET " + ', '.join(
                f'{column}=%s' for column in assignments), values)

    def _create_key_mirror(self, request_id, generation, state, suffix):
        with self.registry.cursor() as cr:
            committed_env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            key = _llm_internal(committed_env[KEY].sudo()).create({
                'account_id': self.account_id,
                'kind': 'free_trial',
                'client_request_id': request_id,
                'canary_generation': generation,
                'newapi_name': 'synthetic_canary_' + suffix,
                'state': state,
                'model_allowlist': ','.join(nac.FREE_TRIAL_MODELS),
            })
            key_id = key.id
            cr.commit()
        return key_id

    def _cleanup_fixture(self):
        self._close_outer()
        with self.registry.cursor() as cr:
            if self.account_id:
                cr.execute(f'DELETE FROM {EVENT_TABLE} WHERE account_id=%s',
                           (self.account_id,))
                cr.execute(f'DELETE FROM {KEY_TABLE} WHERE account_id=%s',
                           (self.account_id,))
                cr.execute(f'DELETE FROM {USAGE_TABLE} WHERE account_id=%s',
                           (self.account_id,))
                cr.execute(f'DELETE FROM {ACCOUNT.replace(".", "_")} WHERE id=%s',
                           (self.account_id,))
            if self.user_id:
                cr.execute('DELETE FROM res_users WHERE id=%s', (self.user_id,))
            if self.partner_id:
                cr.execute('DELETE FROM res_partner WHERE id=%s', (self.partner_id,))
            self._write_canary_state(cr, self.runtime_snapshot)

    def test_claim_commit_survives_outer_transaction_rollback(self):
        partner = self.outer_env['res.partner'].browse(self.partner_id)
        original_name = partner.name
        partner.write({'name': original_name + ' rollback marker'})
        claim, work = self._claim()
        self.assertEqual(work['account_id'], self.account_id)
        self.assertEqual(work['partner_id'], self.partner_id)

        # The claim used deadline_cursor + a real Registry cursor. Rolling back
        # BaseCase's outer transaction cannot undo this committed generation.
        self.outer.rollback()
        with self.registry.cursor() as verify:
            row = self._runtime(verify)
            self.assertEqual(row['canary_generation'], claim.generation)
            self.assertEqual(row['canary_work']['request_id'], claim.request_id)
            verify.execute('SELECT name FROM res_partner WHERE id=%s', (self.partner_id,))
            self.assertEqual(verify.fetchone()[0], original_name)

    def test_second_cursor_holding_runtime_lock_causes_skip_locked(self):
        locker = self.registry.cursor()
        try:
            locker.execute(f'SELECT id FROM {STATE_TABLE} FOR UPDATE')
            self.assertIsNotNone(locker.fetchone())
            self.assertIsNone(self.outer_env['woow_paas_platform.llm_runtime_state']._canary_claim(
                self._deadline()))
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(self._runtime()['canary_generation'], 0)

    def test_creation_guard_does_not_hold_runtime_lock_during_client_callback(self):
        claim, _work = self._claim()
        key_id = self._create_key_mirror(
            claim.request_id, claim.generation, 'creating', 'creation_guard')
        self._set_runtime(['circuit_failures'], [3])

        deadline = time.monotonic() + 2
        started = time.monotonic()
        with self.outer_env[KEY]._llm_creation_guard(
                key_id, canary_claim=claim, deadline=deadline):
            self.outer_env['woow_paas_platform.llm_runtime_state']._llm_update(
                'circuit_failures = 0', ())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0, 'runtime callback UPDATE must not wait on the key guard')

        with self.registry.cursor() as verify:
            verify.execute(f'SELECT circuit_failures FROM {STATE_TABLE}')
            self.assertEqual(verify.fetchone()[0], 0)
            row = self._runtime(verify)
            self.assertEqual(row['canary_generation'], claim.generation)
            self.assertEqual(row['canary_work']['request_id'], claim.request_id)

    def test_expired_lease_reclaims_same_uuid_and_fences_old_claim(self):
        old_claim, old_work = self._claim()
        own_deleted_key = self._create_key_mirror(
            old_claim.request_id, old_claim.generation, 'deleted', 'owned')
        unrelated_request = str(uuid.uuid4())
        unrelated_key = self._create_key_mirror(
            unrelated_request, old_claim.generation, 'active', 'unrelated')

        expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        self._set_runtime(['canary_lease_until'], [expired_at])
        new_claim, new_work = self._claim()
        self.assertGreater(new_claim.generation, old_claim.generation)
        self.assertEqual(new_claim.request_id, old_claim.request_id)
        self.assertEqual(new_work['phase'], 'cleanup')

        state = self.outer_env['woow_paas_platform.llm_runtime_state']
        self.assertFalse(state._canary_save_probe(old_claim, 'ok', 1, self._deadline()))
        self.assertFalse(state._canary_finish(old_claim, True, None, self._deadline()))
        self.assertEqual(state._canary_cleanup(new_claim, new_work, self._deadline()), (True, None))

        with self.registry.cursor() as verify:
            verify.execute(f'SELECT state FROM {KEY_TABLE} WHERE id=%s', (own_deleted_key,))
            self.assertEqual(verify.fetchone()[0], 'deleted')
            verify.execute(f'SELECT state FROM {KEY_TABLE} WHERE id=%s', (unrelated_key,))
            self.assertEqual(verify.fetchone()[0], 'active')
            row = self._runtime(verify)
            self.assertEqual(row['canary_generation'], new_claim.generation)
            self.assertEqual(row['canary_work']['request_id'], old_work['request_id'])

    def test_canary_key_exemption_preserves_three_ordinary_reservations(self):
        claim, _work = self._claim()
        Usage = self.outer_env['woow_paas_platform.llm_free_usage']
        for _ in range(3):
            Usage._llm_reserve_key_cr(self.outer, self.account_id, DAY)
        Usage._llm_reserve_canary_key_cr(self.outer, self.account_id, DAY, claim)
        self.outer.execute(
            f'SELECT success_count, trial_keys_created FROM {USAGE_TABLE} '
            'WHERE account_id=%s AND day_tpe=%s',
            (self.account_id, DAY))
        self.assertEqual(self.outer.fetchone(), (0, 3))

        malformed = {'generation': claim.generation}
        wrong_account = canary_policy.CanaryClaim(
            claim.generation, claim.request_id, claim.account_id + 1,
            claim.partner_id, claim.fingerprint, claim._authority)
        for invalid in (malformed, wrong_account):
            with self.outer.savepoint(), self.assertRaises(LlmError):
                Usage._llm_reserve_canary_key_cr(
                    self.outer, self.account_id, DAY, invalid)
        self.outer.execute(
            f'SELECT success_count, trial_keys_created FROM {USAGE_TABLE} '
            'WHERE account_id=%s AND day_tpe=%s',
            (self.account_id, DAY))
        self.assertEqual(self.outer.fetchone(), (0, 3))

    def test_probe_finish_receipt_and_failure_count_commit_past_outer_rollback(self):
        self._set_runtime(['canary_failures'], [4])
        success_claim, _work = self._claim()
        state = self.outer_env['woow_paas_platform.llm_runtime_state']
        self.assertTrue(state._canary_save_probe(
            success_claim, 'ok', 8, self._deadline()))
        self.assertFalse(state._canary_finish(
            success_claim, True, None, self._deadline()))

        old_start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=31)
        self._set_runtime(['canary_started_at'], [old_start])
        failed_claim, _work = self._claim()
        self.assertTrue(state._canary_save_probe(
            failed_claim, 'probe_failed', 17, self._deadline()))
        self.assertFalse(state._canary_finish(
            failed_claim, True, None, self._deadline()))

        self.outer.rollback()
        with self.registry.cursor() as verify:
            row = self._runtime(verify)
            self.assertEqual(row['canary_failures'], 1)
            self.assertEqual(row['last_canary_result'], 'probe_failed')
            verify.execute(f'SELECT count(*) FROM {EVENT_TABLE} '
                           'WHERE account_id=%s AND action=%s',
                           (self.account_id, 'canary'))
            self.assertEqual(verify.fetchone()[0], 2)
