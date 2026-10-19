"""LLM-08 Task 5: real commits and independent cursors, fake upstream only.

Run only in an isolated sandbox/CI database. Pause this fixture's credit cron
(restored on cleanup); settings, runtime/circuit, alerts and HTTP are patched in
this process. Claims are scoped to fixture accounts and cleanup uses exact ids.
Accounting/payment is covered by TestLlmCreditPayment; user orders here exercise
only the worker ledger and the deliberately unfinished Task 6 void seam. This
suite pins the original worker stub; Task 6 tests must use real paid invoices
and exercise the extension separately after integration.
"""
import os
import time
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import MagicMock, patch

from psycopg2.errors import UniqueViolation

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models import llm_common, llm_credit_order as worker_module
from ..models.llm_account import ACCOUNT_TABLE, balance_stamp
from ..models.llm_common import LlmError, _llm_internal
from ..models.llm_credit_order import (
    BALANCE_CRON_XMLID, CREDIT_CRON_XMLID, CREDIT_VERIFY_CRON_XMLID,
    ORDER_MODEL, ORDER_TABLE, _credit_internal,
)
from ..models.llm_event import EVENT_TABLE
from ..services import newapi_client as nac
from .llm_fake_newapi import FakeNewApi, SimulatedCrash
from .llm_test_support import (
    ACCOUNT, RUNTIME, TEST_CONFIG, CommittedLlmFixture, LlmClientAdapter, env_for, llm_env,
)


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditWorkerCommitted(TransactionCase):
    """Actual cron/claim/write transactions; no mocked commit or remote API."""

    def setUp(self):
        super().setUp()
        self.order_ids = []
        self.requester_ids = []
        self.fake = FakeNewApi()
        self.runtime = {'guard_tripped': False, 'dr_mode': True}
        self._patch(patch.dict(os.environ, llm_env()))
        self.client = LlmClientAdapter(self, self.fake, full_factory=False)
        self.fixture = CommittedLlmFixture(self)
        self.fixture.cron_id = self.env.ref(CREDIT_CRON_XMLID).id
        self.fixture.extra_cron_ids = [self.env.ref(CREDIT_VERIFY_CRON_XMLID).id]
        self.fixture.start()
        # Register after base purge, so orders disappear before their FK targets.
        self.addCleanup(self._purge_orders)
        self._patch(patch.object(worker_module, 'llm_config', return_value=dict(TEST_CONFIG)))
        self._patch(patch.object(type(self.env[RUNTIME]), '_llm_read',
                                side_effect=lambda: dict(self.runtime)))
        self.alert = self._patch(patch.object(type(self.env['woow_paas_platform.llm_event']),
                                             '_llm_alert', return_value=False))
        self._patch(patch.object(nac.NewApiClient, '_request',
                                side_effect=AssertionError('real HTTP forbidden in worker tests')))
        # Successful credit schedules a balance refresh; record that request
        # without waking any live scheduler or leaving unrelated trigger rows.
        self.balance_trigger = MagicMock()
        cron_cls = type(self.env['ir.cron'])
        trigger = cron_cls._trigger
        balance_cron_id = self.env.ref(BALANCE_CRON_XMLID).id

        def scoped_trigger(records, *args, **kwargs):
            if records.ids == [balance_cron_id]:
                return self.balance_trigger(*args, **kwargs)
            return trigger(records, *args, **kwargs)
        self._patch(patch.object(cron_cls, '_trigger', scoped_trigger))
        cls = type(self.env[ORDER_MODEL])
        # Keep these ledger-only fixtures out of Task 6 accounting after its
        # _inherit extension is installed. Exercise the actual Task 5 stub.
        self._patch(patch.object(cls, '_llm_worker_void_pending',
                                worker_module.LlmCreditOrder._llm_worker_void_pending))
        claim, due = cls._llm_claim_credit, cls._llm_credit_has_due
        fixture = self.fixture
        self._patch(patch.object(cls, '_llm_claim_credit',
                                lambda records, **kwargs: claim(records, account_ids=fixture.own_ids(), **kwargs)))
        self._patch(patch.object(cls, '_llm_credit_has_due',
                                lambda records: due(records, account_ids=fixture.own_ids())))
        self.account_id, self.user_id = self._account()

    def _patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _account(self):
        partner_id = self.fixture.partner('LLM worker %s' % uuid.uuid4().hex[:8])
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = _llm_internal(env[ACCOUNT]).create({'partner_id': partner_id})
            self.fixture.account_ids.append(account.id)
            username = nac.newapi_username(TEST_CONFIG['prefix'], account.id)
            user_id = self.fake.add_user(username, quota=2 ** 40,
                                         used_quota=2 ** 40, request_count=2 ** 40)
            _llm_internal(account).write({'state': 'active', 'newapi_user_id': user_id,
                                          'newapi_username': username})
        return account.id, user_id

    def _order(self, account_id=None, source='user', reserve=None):
        if reserve is None:
            reserve = source == 'user'
        account_id = account_id or self.account_id
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env[ACCOUNT].browse(account_id)
            values = {'account_id': account.id, 'partner_id': account.partner_id.id,
                      'newapi_user_id': account.newapi_user_id, 'source': source,
                      'points': 100, 'quota': 1562500, 'client_request_id': uuid.uuid4().hex}
            if source == 'manual':
                requester = env['res.users'].with_context(no_reset_password=True).create({
                    'name': 'Worker QA', 'login': 'worker_qa_%s' % uuid.uuid4().hex,
                    'email': 'worker-qa@example.invalid',
                    'groups_id': [(6, 0, [env.ref('base.group_user').id])],
                })
                self.requester_ids.append(requester.id)
                self.fixture.partner_ids.append(requester.partner_id.id)
                values.update(requested_by_uid=requester.id, reason='Worker cancellation QA')
            elif source == 'dr':
                origin = _credit_internal(env[ORDER_MODEL]).create(values | {
                    'source': 'user', 'client_request_id': uuid.uuid4().hex})
                self.order_ids.append(origin.id)
                cr.execute("UPDATE %s SET state = 'credit_lost' WHERE id = %%s" % ORDER_TABLE,
                           (origin.id,))
                origin.invalidate_recordset()
                values.update(dr_origin_order_id=origin.id, reason='Worker recovery QA')
            order = _credit_internal(env[ORDER_MODEL]).create(values)
            self.order_ids.append(order.id)
            if reserve:
                event_id = env['woow_paas_platform.llm_event']._llm_throttle_reserve(
                    account, 'convert', uid=env.uid, credit_t1_pending=True)
                _credit_internal(order).write({'convert_event_id': event_id})
                # T1 stores only the audit reference. Its RR cursor cannot
                # update the independently inserted event; first RC claim binds
                # target ownership and clears credit_t1_pending atomically.

        return order.id

    def _purge_orders(self):
        with self.registry.cursor() as cr:
            if self.order_ids:
                cr.execute("DELETE FROM %s WHERE id = ANY(%%s) AND source IN ('dr','recharge')"
                           % ORDER_TABLE, (self.order_ids,))
                cr.execute('DELETE FROM %s WHERE id = ANY(%%s)' % ORDER_TABLE, (self.order_ids,))
            if self.requester_ids:
                env_for(self.registry, cr)['res.users'].browse(self.requester_ids).unlink()

    def _row(self, order_id):
        with self.registry.cursor() as cr:
            cr.execute('SELECT * FROM %s WHERE id = %%s' % ORDER_TABLE, (order_id,))
            return cr.dictfetchone()

    def _events(self):
        with self.registry.cursor() as cr:
            cr.execute("SELECT id, result, target_model, target_id, credit_t1_pending FROM %s WHERE action = 'convert' "
                       'AND account_id = ANY(%%s) ORDER BY id' % EVENT_TABLE,
                       (self.fixture.own_ids(),))
            return cr.dictfetchall()

    def _claim(self):
        with self.registry.cursor() as cr:
            return env_for(self.registry, cr)[ORDER_MODEL]._llm_claim_credit()

    def _send(self, claim):
        with self.registry.cursor() as cr:
            env_for(self.registry, cr)[ORDER_MODEL]._llm_send_claimed(
                claim, time.monotonic() + 30)

    def _run(self):
        start = time.monotonic()
        with self.registry.cursor() as cr:
            env_for(self.registry, cr)[ORDER_MODEL]._cron_llm_credit()
        self.assertLess(time.monotonic() - start, 30)

    def _verify(self):
        start = time.monotonic()
        with self.registry.cursor() as cr:
            env_for(self.registry, cr)[ORDER_MODEL]._cron_llm_credit_verify()
        self.assertLess(time.monotonic() - start, 30)

    def _update_order(self, order_id, **values):
        columns = sorted(values)
        with self.registry.cursor() as cr:
            cr.execute('UPDATE %s SET %s WHERE id = %%s' % (
                ORDER_TABLE, ', '.join('%s = %%s' % key for key in columns)),
                [values[key] for key in columns] + [order_id])

    def _due(self, order_id):
        self._update_order(order_id, next_attempt_at=fields.Datetime.now() - timedelta(minutes=1))

    def _seed_events(self, count, *, account_id=None, result='not_executed', order_id=None):
        """Historical throttle receipts, owned by fixture accounts only."""
        account_id = account_id or self.account_id
        stamp = fields.Datetime.now() - timedelta(minutes=5)
        ids = []
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            partner_id = env[ACCOUNT].browse(account_id).partner_id.id
            for _i in range(count):
                cr.execute(
                    'INSERT INTO %s (account_id, partner_id, action, result, severity, occurrences, '
                    'target_model, target_id, credit_t1_pending, '
                    'create_uid, create_date, write_uid, write_date) '
                    "VALUES (%%s, %%s, 'convert', %%s, 'info', 1, %%s, %%s, false, %%s, %%s, %%s, %%s) "
                    'RETURNING id' % EVENT_TABLE,
                    (account_id, partner_id, result, ORDER_MODEL if order_id else None, order_id,
                     env.uid, stamp, env.uid, stamp))
                ids.append(cr.fetchone()[0])
        return ids

    def _retry_order(self, account_id=None):
        order_id = self._order(account_id)
        event_id = self._row(order_id)['convert_event_id']
        # Model an earlier, certainly unsent attempt using its real T1 event;
        # do not replace a user order's reservation with an artificial receipt.
        with self.registry.cursor() as cr:
            cr.execute('UPDATE %s SET result = %%s, target_model = %%s, target_id = %%s, '
                       'credit_t1_pending = false WHERE id = %%s' % EVENT_TABLE,
                       ('not_executed', ORDER_MODEL, order_id, event_id))
        self._update_order(order_id, attempt=1)
        return order_id

    def test_retry_slot_30_then_31_deferred_without_spending_attempt(self):
        order_id = self._retry_order()
        self._seed_events(28)
        claim = self._claim()
        self.assertEqual(claim['attempt'], 2)
        self.fake.fail_before['ap_add_quota'] = nac.NotExecuted('connect')
        self._send(claim)
        self._due(order_id)
        before = self._row(order_id)
        self.assertEqual(len(self._events()), 30)
        self.assertIsNone(self._claim())
        after = self._row(order_id)
        self.assertEqual(after['state'], 'paid')
        self.assertEqual(after['attempt'], 2)
        self.assertGreater(after['next_attempt_at'], fields.Datetime.now())
        self.assertEqual(after['last_error_code'], 'llm_rate_limited')
        for key in ('sent_at', 'lease_until', 'convert_event_id', 'quota_before', 'used_quota_before',
                    'request_count_before', 'before_sampled_at', 'before_snapshot_present'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(len(self._events()), 30)
        self.assertFalse(self.fake.quota_additions)

    def test_throttled_backlog_does_not_block_other_account(self):
        first = self._retry_order()
        second = self._retry_order()
        self._seed_events(28)
        other_account, _uid = self._account()
        other_order = self._order(other_account, reserve=True)
        claim = self._claim()
        self.assertEqual(claim['id'], other_order)
        self.assertEqual(self._row(first)['attempt'], 1)
        self.assertGreater(self._row(first)['next_attempt_at'], fields.Datetime.now())
        self.assertEqual(self._row(second)['attempt'], 1)
        self.assertIsNone(self._row(second)['next_attempt_at'], 'skip the entire limited account')
        self.assertFalse(self.fake.calls)

    def test_first_t1_unbound_reserved_event_is_bound_at_cap_without_recount(self):
        self.assertEqual(self.env[ORDER_MODEL]._fields['convert_event_id'].type, 'integer')
        with self.registry.cursor() as cr:
            cr.execute("SELECT 1 FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f' "
                       'AND conname = %s', (ORDER_TABLE, ORDER_TABLE + '_convert_event_id_fkey'))
            self.assertFalse(cr.fetchone())
        order_id = self._order(reserve=True)
        event_id = self._row(order_id)['convert_event_id']
        with self.registry.cursor() as cr:
            # Root T1 leaves its FK committed; its RR cursor cannot see the
            # independently inserted event. First RC worker binds it forever.
            cr.execute('UPDATE %s SET target_model = NULL, target_id = NULL, '
                       'credit_t1_pending = true WHERE id = %%s' % EVENT_TABLE, (event_id,))
        self._seed_events(29)
        claim = self._claim()
        self.assertEqual(claim['event_id'], event_id)
        self.assertEqual(claim['attempt'], 1)
        self.assertEqual(len(self._events()), 30)
        event = next(e for e in self._events() if e['id'] == event_id)
        self.assertEqual((event['target_model'], event['target_id']), (ORDER_MODEL, order_id))
        self.assertIs(event['credit_t1_pending'], False)

    def test_ten_failed_calls_limit_retry_but_not_executed_does_not(self):
        order_id = self._retry_order()
        self._seed_events(10, result='failed')
        self.assertIsNone(self._claim())
        self.assertEqual(self._row(order_id)['attempt'], 1)
        self.assertGreater(self._row(order_id)['next_attempt_at'], fields.Datetime.now())
        self.assertEqual(len(self._events()), 11)
        self.assertFalse(self.fake.calls)

    def test_parallel_retry_and_t1_share_the_final_hourly_slot(self):
        order_id = self._retry_order()
        self._seed_events(28)
        barrier = Barrier(2)

        def worker():
            barrier.wait(timeout=10)
            return self._claim()

        def t1():
            barrier.wait(timeout=10)
            try:
                with self.registry.cursor() as cr:
                    env = env_for(self.registry, cr)
                    return env['woow_paas_platform.llm_event']._llm_throttle_reserve(
                        env[ACCOUNT].browse(self.account_id), 'convert')
            except LlmError as exc:
                self.assertEqual(exc.code, 'llm_rate_limited')
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker), executor.submit(t1)]
            worker_claim, t1_event = [future.result(timeout=15) for future in futures]
        self.assertEqual(int(bool(worker_claim)) + int(bool(t1_event)), 1)
        self.assertEqual(len(self._events()), 30)
        row = self._row(order_id)
        if worker_claim:
            self.assertEqual((row['state'], row['attempt']), ('sending', 2))
        else:
            self.assertEqual((row['state'], row['attempt']), ('paid', 1))
            self.assertGreater(row['next_attempt_at'], fields.Datetime.now())
        self.assertFalse(self.fake.calls)

    def test_parallel_worker_retries_never_create_event_31(self):
        first, second = self._retry_order(), self._retry_order()
        self._seed_events(27)
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            return self._claim()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim) for _ in range(2)]
            claims = [future.result(timeout=15) for future in futures]
        self.assertEqual(len([value for value in claims if value]), 1)
        self.assertEqual(len(self._events()), 30)
        rows = [self._row(order_id) for order_id in (first, second)]
        self.assertEqual(sorted((row['state'], row['attempt']) for row in rows),
                         [('paid', 1), ('sending', 2)])

    def test_missing_or_zero_user_reservation_is_review_without_new_event(self):
        for missing in (True, False):
            account_id, _uid = self._account()
            order_id = self._order(account_id)
            event_id = self._row(order_id)['convert_event_id']
            if missing:
                with self.registry.cursor() as cr:
                    cr.execute('DELETE FROM %s WHERE id = %%s' % EVENT_TABLE, (event_id,))
            else:
                self._update_order(order_id, convert_event_id=0)
            count = len(self._events())
            self._run()
            row = self._row(order_id)
            self.assertEqual((row['state'], row['attempt']), ('needs_review', 0))
            self.assertEqual(row['last_error_code'], worker_module.CREDIT_RESERVATION_INVALID)
            self.assertEqual(row['convert_event_id'], event_id if missing else 0)
            self.assertEqual(len(self._events()), count)
        self.assertFalse(self.fake.calls)

    def test_first_terminal_user_reservations_are_review_not_replaced(self):
        for result in ('not_executed', 'failed', 'ok', 'unknown'):
            account_id, _uid = self._account()
            order_id = self._order(account_id)
            event_id = self._row(order_id)['convert_event_id']
            with self.registry.cursor() as cr:
                cr.execute('UPDATE %s SET result = %%s WHERE id = %%s' % EVENT_TABLE,
                           (result, event_id))
            count = len(self._events())
            self._run()
            row = self._row(order_id)
            self.assertEqual((row['state'], row['attempt']), ('needs_review', 0))
            self.assertEqual(row['convert_event_id'], event_id)
            self.assertEqual(len(self._events()), count)
            event = next(e for e in self._events() if e['id'] == event_id)
            self.assertEqual(event['result'], result)
            self.assertIsNone(event['target_id'], 'an invalid reservation is never rebound')
        self.assertFalse(self.fake.calls)

    def test_paid_retry_with_unsafe_prior_or_missing_owner_is_review(self):
        for result, owner_present in (('started', True), ('failed', True), ('ok', True),
                                      ('unknown', True), ('not_executed', False)):
            account_id, _uid = self._account()
            order_id = self._retry_order(account_id)
            event_id = self._row(order_id)['convert_event_id']
            with self.registry.cursor() as cr:
                cr.execute('UPDATE %s SET result = %%s, target_model = %%s, target_id = %%s '
                           'WHERE id = %%s' % EVENT_TABLE,
                           (result, ORDER_MODEL if owner_present else None,
                            order_id if owner_present else None, event_id))
            count = len(self._events())
            self._run()
            row = self._row(order_id)
            self.assertEqual((row['state'], row['attempt']), ('needs_review', 1))
            self.assertEqual(row['convert_event_id'], event_id)
            self.assertEqual(len(self._events()), count)
            self.assertEqual(next(e for e in self._events() if e['id'] == event_id)['result'], result)
        self.assertFalse(self.fake.calls)

    def test_mismatched_permanent_target_is_review_and_not_overwritten(self):
        order_id = self._order()
        event_id = self._row(order_id)['convert_event_id']
        with self.registry.cursor() as cr:
            cr.execute('UPDATE %s SET target_model = %%s, target_id = %%s WHERE id = %%s' % EVENT_TABLE,
                       (ORDER_MODEL, order_id + 1000000, event_id))
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'needs_review')
        event = next(e for e in self._events() if e['id'] == event_id)
        self.assertEqual(event['target_id'], order_id + 1000000)
        self.assertEqual(event['result'], 'started')
        self.assertFalse(self.fake.calls)

    def test_bad_reservation_commits_review_before_claiming_other_account(self):
        first = self._order()
        self._update_order(first, convert_event_id=0)
        other, _uid = self._account()
        second = self._order(other)
        self.alert.side_effect = lambda *a, **kw: self.assertEqual(self._row(first)['state'], 'needs_review')
        claim = self._claim()
        self.assertEqual(claim['id'], second)
        self.assertEqual(self._row(first)['attempt'], 0)
        self.alert.assert_called_once()
        self.assertFalse(self.fake.calls)

    def test_cron_commits_claim_and_int8_snapshot_before_post(self):
        order_id = self._order(reserve=True)
        original_event = self._row(order_id)['convert_event_id']

        def before_post(user_id, quota):
            row = self._row(order_id)  # another real connection sees the commit
            self.assertEqual((row['state'], row['attempt']), ('sending', 1))
            self.assertTrue(row['sent_at'])
            self.assertEqual(row['lease_until'] - row['sent_at'], timedelta(minutes=5))
            self.assertTrue(row['before_snapshot_present'])
            self.assertEqual([row[k] for k in ('quota_before', 'used_quota_before',
                                               'request_count_before')], [2 ** 40] * 3)
            self.assertEqual(row['convert_event_id'], original_event)
        self.fake.before_quota_add = before_post
        self._run()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'credited')
        self.assertEqual(row['credit_evidence'], 'response')
        self.assertTrue(row['newapi_request_id'])
        self.assertTrue(row['credited_at'])
        self.assertIsNone(row['lease_until'])
        self.assertEqual(self._events(), [{'id': original_event, 'result': 'ok',
                                          'target_model': ORDER_MODEL, 'target_id': order_id,
                                          'credit_t1_pending': False}])
        self.balance_trigger.assert_called_once()
        self.assertEqual(len(self.fake.quota_additions), 1)
        self.assertEqual([name for name, _ip in self.fake.calls], ['ap_get_user', 'ap_add_quota'])
        self.assertEqual(set(self.client.roles_used()), {nac.ROLE_CRON})

    def test_unknown_after_upstream_commit_does_not_resend(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'unknown')
        self.assertEqual(self._events()[0]['result'], 'unknown')
        self.assertEqual(len(self.fake.quota_additions), 1)

    def test_verifier_uses_complete_type_one_log_to_credit_unknown_order(self):
        order_id = self._order()
        receipt = 'receipt-123'
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout', request_id=receipt)
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'unknown')
        self.assertEqual(self._row(order_id)['newapi_request_id'], receipt)
        # The response request id is the strongest receipt.  A configured
        # fallback admin id must not invalidate it after an admin rotation.
        self.fake.logs[-1]['request_id'] = receipt
        self.fake.logs[-1]['other']['admin_info']['admin_id'] += 1
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'credited')
        self.assertEqual(row['credit_evidence'], 'log_request_id')
        self.assertTrue(row['newapi_request_id'])
        self.assertEqual(self._events()[0]['result'], 'ok')
        self.assertEqual([name for name, _ip in self.fake.calls],
                         ['ap_get_user', 'ap_add_quota', 'ap_log'])

    def test_verifier_persists_log_page_before_finding_later_proof(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        original = deepcopy(self.fake.logs[-1])
        # Logs are newest-first by (created_at, id). One hundred valid but
        # nonmatching type-1 rows force a durable second-page scan.
        for index in range(100):
            decoy = deepcopy(original)
            decoy['id'] = original['id'] + index + 1
            decoy['request_id'] = 'decoy-%s' % index
            decoy['other']['op']['params']['requested_quota'] = original['quota'] + index + 1
            decoy['other']['op']['params']['to'] += index + 1
            self.fake.logs.append(decoy)
        self._verify()
        first = self._row(order_id)
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(first['verify_log_page'], 2)
        self.assertTrue(first['verify_log_end_at'])
        self.assertIsNone(first['verify_token'])
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'credited')
        self.assertEqual(row['credit_evidence'], 'log_request_id')

    def test_verifier_log_read_failure_stays_unknown_and_releases_claim(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self.fake.fail_before['ap_log'] = nac.NotExecuted('connect')
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(row['last_error_code'], worker_module.CREDIT_EVIDENCE_UNAVAILABLE)
        self.assertIsNone(row['verify_token'])
        self.assertEqual(self._events()[0]['result'], 'unknown')

    def test_verifier_invalid_log_cursor_requires_review_without_log_call(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self._update_order(order_id, verify_log_page=1000000,
                           verify_log_end_at=fields.Datetime.now())
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'needs_review')
        self.assertEqual(row['last_error_code'], worker_module.CREDIT_EVIDENCE_INVALID)
        self.assertEqual(self._events()[0]['result'], 'unknown')
        self.assertNotIn('ap_log', [name for name, _ip in self.fake.calls])

    def test_verifier_without_precise_correlation_requires_review_before_log_read(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self._update_order(order_id, before_snapshot_present=False, before_sampled_at=None,
                           quota_before=None, used_quota_before=None, request_count_before=None,
                           newapi_request_id=None)
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'needs_review')
        self.assertEqual(row['last_error_code'], worker_module.CREDIT_EVIDENCE_MISSING_SNAPSHOT)
        self.assertEqual(self._events()[0]['result'], 'unknown')
        self.assertNotIn('ap_log', [name for name, _ip in self.fake.calls])

    def test_verifier_stale_prepost_snapshot_requires_review_before_log_read(self):
        order_id = self._order()
        self.fake.fail_before['ap_add_quota'] = nac.Unknown('transport')
        self._run()
        row = self._row(order_id)
        self._update_order(order_id, before_sampled_at=row['sent_at'] - timedelta(seconds=1))
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'needs_review')
        self.assertEqual(row['last_error_code'], worker_module.CREDIT_EVIDENCE_MISSING_SNAPSHOT)
        self.assertEqual(self._events()[0]['result'], 'unknown')
        self.assertNotIn('ap_log', [name for name, _ip in self.fake.calls])

    def test_verifier_missing_reservation_requires_review(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self._update_order(order_id, convert_event_id=0)
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'needs_review')
        self.assertEqual(row['last_error_code'], worker_module.CREDIT_EVIDENCE_INVALID)
        self.assertEqual(self._events()[0]['result'], 'unknown')

    def test_verifier_quiescent_delta_credits_after_five_minutes(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self.fake.logs.clear()  # Exercise the bounded no-log fallback only.
        self._verify()
        first = self._row(order_id)
        self.assertEqual(first['state'], 'unknown')
        self.assertTrue(first['verify_first_sample_present'])
        self._update_order(order_id, verify_first_sample_at=
                           fields.Datetime.now() - timedelta(minutes=5, seconds=1))
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'credited')
        self.assertEqual(row['credit_evidence'], 'quiescent_delta')
        self.assertEqual(self._events()[0]['result'], 'ok')

    def test_verifier_quiescent_noop_requeues_only_with_a_safe_event(self):
        order_id = self._order()
        # The worker took and persisted its before snapshot, but this request
        # definitely did not reach new-api. It still becomes unknown because a
        # generic transport result is never safe to resend without verification.
        self.fake.fail_before['ap_add_quota'] = nac.Unknown('transport')
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'unknown')
        self._verify()
        self._update_order(order_id, verify_first_sample_at=
                           fields.Datetime.now() - timedelta(minutes=5, seconds=1))
        self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'paid')
        self.assertEqual(row['credit_evidence'], 'quiescent_noop')
        self.assertEqual(self._events()[0]['result'], 'not_executed')
        self.assertIsNotNone(row['next_attempt_at'])

    def test_retry_after_quiescent_noop_clears_old_request_receipt(self):
        order_id = self._order()
        receipt = 'old-unexecuted-receipt'
        # Even if a transport layer exposed a request-like identifier, the
        # quiescent proof below establishes that this attempt did not execute.
        # Its receipt must not become evidence for the next, new POST.
        self.fake.fail_before['ap_add_quota'] = nac.Unknown('transport', request_id=receipt)
        self._run()
        self.assertEqual(self._row(order_id)['newapi_request_id'], receipt)
        self._verify()
        self._update_order(order_id, verify_first_sample_at=
                           fields.Datetime.now() - timedelta(minutes=5, seconds=1))
        self._verify()
        self.assertEqual(self._row(order_id)['state'], 'paid')

        def before_retry_post(_user_id, _quota):
            row = self._row(order_id)
            self.assertEqual(row['state'], 'sending')
            self.assertIsNone(row['newapi_request_id'])

        self.fake.before_quota_add = before_retry_post
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'credited')

    def test_verifier_mismatch_requires_human_review_without_releasing_account(self):
        order_id = self._order()
        self.fake.fail_before['ap_add_quota'] = nac.Unknown('transport')
        self._run()
        self._verify()
        self._update_order(order_id, verify_first_sample_at=
                           fields.Datetime.now() - timedelta(minutes=5, seconds=1))
        self.fake.users[self.user_id]['used_quota'] += 1
        event_model = type(self.env['woow_paas_platform.llm_event'])
        with patch.object(event_model, '_llm_incident', return_value=False) as incident:
            self._verify()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'needs_review')
        self.assertEqual(row['last_error_code'], worker_module.CREDIT_EVIDENCE_MISMATCH)
        self.assertEqual(self._events()[0]['result'], 'unknown')
        self.assertIsNone(row['verify_token'])
        incident.assert_called_once()
        self.alert.assert_called()

    def test_verifier_claim_is_exclusive_and_fences_the_late_worker(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            claim = orders._llm_claim_credit_verification()
            self.assertEqual(claim['id'], order_id)
            self.assertIsNone(orders._llm_claim_credit_verification())
            worker_claim = {
                'id': order_id, 'account_id': claim['account_id'], 'attempt': claim['attempt'],
                'event_id': claim['event_id'],
            }
            self.assertFalse(orders._llm_worker_update(worker_claim, {'last_error_code': 'llm_credit_unknown'}))
        row = self._row(order_id)
        self.assertEqual(row['verify_token'], claim['token'])
        self.assertEqual(row['state'], 'unknown')

    def test_crash_after_claim_is_durable_and_never_reclaimed(self):
        order_id = self._order()
        self.fake.crash_before['ap_get_user'] = SimulatedCrash
        try:
            self._run()
        except SimulatedCrash:
            pass
        else:
            self.fail('expected simulated process death after claim')
        row = self._row(order_id)
        self.assertEqual((row['state'], row['attempt']), ('sending', 1))
        self.assertFalse(row['before_snapshot_present'])
        self.assertEqual(self._events()[0]['result'], 'started')
        self._update_order(order_id, lease_until=fields.Datetime.now() - timedelta(minutes=1))
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'sending')
        self.assertFalse(self.fake.quota_additions)

    def test_crash_after_post_leaves_evidence_for_verifier(self):
        order_id = self._order()
        self.fake.crash_after['ap_add_quota'] = SimulatedCrash
        try:
            self._run()
        except SimulatedCrash:
            pass
        else:
            self.fail('expected simulated process death after upstream commit')
        self._run()
        row = self._row(order_id)
        self.assertEqual(row['state'], 'sending')
        self.assertTrue(row['before_snapshot_present'])
        self.assertEqual(len(self.fake.quota_additions), 1)

    def test_skip_locked_progresses_to_another_account(self):
        first = self._order()
        other, _uid = self._account()
        second = self._order(other)
        with self.registry.cursor() as locked:
            locked.execute('SELECT id FROM %s WHERE id = %%s FOR UPDATE' % ORDER_TABLE, (first,))
            claim = self._claim()
            self.assertEqual(claim['id'], second)
        self.assertEqual(self._row(first)['state'], 'paid')

    def test_same_account_inflight_and_parallel_claims(self):
        first, second = self._order(), self._order()
        barrier = Barrier(2)

        def claim():
            barrier.wait(timeout=10)
            return self._claim()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim) for _ in range(2)]
            claims = [f.result(timeout=15) for f in futures]
        self.assertEqual(len([c for c in claims if c]), 1)
        rows = [self._row(order_id) for order_id in (first, second)]
        self.assertEqual(sorted(r['state'] for r in rows), ['paid', 'sending'])
        # Independently prove the durable partial unique backstop.
        paid = next(r for r in rows if r['state'] == 'paid')
        try:
            with self.registry.cursor() as cr:
                cr.execute("UPDATE %s SET state = 'sending' WHERE id = %%s" % ORDER_TABLE,
                           (paid['id'],), log_exceptions=False)
        except UniqueViolation:
            pass
        else:
            self.fail('account inflight index did not reject a second sending order')

    def test_retry_has_fresh_reservation_and_keeps_old_ownership(self):
        order_id = self._order(reserve=True)
        original_event = self._row(order_id)['convert_event_id']
        self.fake.fail_before['ap_add_quota'] = nac.NotExecuted('connect')
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'paid')
        self._due(order_id)
        self._run()
        events = self._events()
        self.assertEqual([e['result'] for e in events], ['not_executed', 'ok'])
        self.assertEqual(events[0]['id'], original_event)
        self.assertNotEqual(self._row(order_id)['convert_event_id'], original_event)
        self.assertEqual({(e['target_model'], e['target_id']) for e in events}, {(ORDER_MODEL, order_id)})
        self.assertTrue(all(e['credit_t1_pending'] is False for e in events))
        self.assertEqual(len(self.fake.quota_additions), 1)

    def test_six_not_executed_attempts_then_review(self):
        order_id = self._order()
        for attempt, minutes in enumerate((1, 2, 5, 15, 60, None), 1):
            self.fake.fail_before['ap_add_quota'] = nac.NotExecuted('busy')
            self._run()
            row = self._row(order_id)
            self.assertEqual(row['attempt'], attempt)
            if minutes:
                self.assertEqual(row['state'], 'paid')
                self.assertLess(abs((row['next_attempt_at'] - fields.Datetime.now()).total_seconds()
                                    - minutes * 60), 3)
                self._due(order_id)
            else:
                self.assertEqual(row['state'], 'needs_review')
        self._run()
        self.assertEqual(self._row(order_id)['attempt'], 6)
        self.assertEqual(len(self._events()), 6)
        self.assertFalse(self.fake.quota_additions)

    def test_guard_and_n27_precede_circuit_probe_and_claim(self):
        order_id = self._order()
        before_events = self._events()
        self.runtime['guard_tripped'] = True
        with patch.object(llm_common, 'circuit_allows', side_effect=AssertionError('probe forbidden')):
            self._run()
        self.runtime['guard_tripped'] = False
        with patch.object(worker_module, 'llm_config', return_value=dict(TEST_CONFIG, proxy_state='empty')):
            with patch.object(llm_common, 'circuit_allows', side_effect=AssertionError('probe forbidden')):
                self._run()
        self.assertEqual(self._row(order_id)['attempt'], 0)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self._events(), before_events)
        self.alert.assert_not_called()

    def test_verifier_token_blocks_snapshot_and_all_terminal_writes(self):
        order_id = self._order()
        claim = self._claim()
        self._update_order(order_id, verify_token='verifier-owner',
                           verify_lease_until=fields.Datetime.now() + timedelta(minutes=5))
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            self.assertFalse(orders._llm_worker_update(claim, {'before_snapshot_present': False}))
            for result in (nac.Ok(request_id='late-ok'), nac.Unknown(), nac.NotExecuted(),
                           nac.Definite(proxy_error='denied')):
                self.assertFalse(orders._llm_classify_credit(claim, result))
        self._send(claim)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self._row(order_id)['state'], 'sending')
        self.assertEqual(self._events()[0]['result'], 'started')

    def test_expired_worker_lease_and_stale_attempt_do_not_write(self):
        order_id = self._order()
        claim = self._claim()
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            for stale in ({'attempt': 2}, {'account_id': self.account_id + 1000000},
                          {'event_id': claim['event_id'] + 1000000}):
                self.assertFalse(orders._llm_classify_credit(claim | stale, nac.Ok(request_id='stale')))
        self._update_order(order_id, lease_until=fields.Datetime.now() - timedelta(minutes=1))
        self._send(claim)
        with self.registry.cursor() as cr:
            self.assertFalse(env_for(self.registry, cr)[ORDER_MODEL]._llm_classify_credit(
                claim, nac.Ok(request_id='expired')))
        self.assertFalse(self.fake.calls)
        self.assertEqual(self._events()[0]['result'], 'started')

    def test_verifier_takes_over_during_post_without_event_overwrite(self):
        order_id = self._order()

        def takeover(*args):
            self._update_order(order_id, verify_token='new-owner',
                               verify_lease_until=fields.Datetime.now() + timedelta(minutes=5))
        self.fake.after_quota_add = takeover
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'sending')
        self.assertEqual(self._events()[0]['result'], 'started')
        self.assertEqual(len(self.fake.quota_additions), 1)

    def test_known_rejection_safe_void_seam_for_each_source(self):
        for source in ('user', 'manual', 'dr'):
            for key in worker_module._QUOTA_REJECTIONS:
                account_id, _uid = self._account()
                order_id = self._order(account_id, source=source)
                self.fake.fail_before['ap_add_quota'] = nac.Definite(message_key=key)
                self._run()
                row = self._row(order_id)
                self.assertEqual(row['state'], 'needs_review' if source == 'user' else 'cancelled')
                self.assertIsNone(row['void_wallet_transaction_id'])
                self.assertIsNone(row['void_credit_note_id'])
                if source == 'user':
                    self.assertEqual(row['last_error_code'], worker_module.CREDIT_VOID_PENDING)
                event = next(e for e in self._events() if e['id'] == row['convert_event_id'])
                self.assertEqual(event['result'], 'failed')
        self.assertFalse(self.fake.quota_additions)

    def test_closed_zero_outbound_suspended_and_snapshot_mismatch(self):
        for source in ('user', 'manual', 'dr'):
            account_id, _uid = self._account()
            order_id = self._order(account_id, source=source)
            with self.registry.cursor() as cr:
                cr.execute("UPDATE %s SET state = 'closed' WHERE id = %%s" % ACCOUNT_TABLE, (account_id,))
            self._run()
            self.assertEqual(self._row(order_id)['state'], 'needs_review' if source == 'user' else 'cancelled')
        self.assertFalse(self.fake.calls)
        order_id = self._order()
        with self.registry.cursor() as cr:
            cr.execute("UPDATE %s SET state = 'suspended' WHERE id = %%s" % ACCOUNT_TABLE, (self.account_id,))
        with patch.object(worker_module, 'llm_config', return_value=dict(TEST_CONFIG, orders_enabled=False)):
            self._run()
        self.assertEqual(self._row(order_id)['state'], 'credited')
        other, _uid = self._account()
        mismatched = self._order(other)
        self._update_order(mismatched, newapi_user_id=999999)
        before = len(self.fake.calls)
        self._run()
        self.assertEqual(self._row(mismatched)['last_error_code'], worker_module.CREDIT_SNAPSHOT_MISMATCH)
        self.assertEqual(len(self.fake.calls), before)

    def test_balance_cache_retains_asof_and_rejects_stale_refresh(self):
        order_id = self._order()
        old_stamp = balance_stamp() - timedelta(minutes=1)
        with self.registry.cursor() as cr:
            cr.execute('UPDATE %s SET quota_cached = 7, balance_synced_at = %%s WHERE id = %%s'
                       % ACCOUNT_TABLE, (old_stamp, self.account_id))
        read_at = balance_stamp()
        self._run()
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env[ACCOUNT].browse(self.account_id)
            self.assertEqual(account.quota_cached, 7, 'before snapshot is not a new cache value')
            self.assertEqual(account.balance_synced_at, old_stamp)
            cr.execute('SELECT balance_invalidated_at FROM %s WHERE id = %%s' % ACCOUNT_TABLE,
                       (self.account_id,))
            barrier = cr.fetchone()[0]
            self.assertGreaterEqual(barrier, read_at)
            username = account.newapi_username
        balances = [(username, self.user_id, 2 ** 40, 2 ** 40, 2 ** 40)]
        with self.registry.cursor() as cr:
            account_model = env_for(self.registry, cr)[ACCOUNT]
            self.assertEqual(account_model._llm_store_balances(balances, read_at), 0)
            self.assertEqual(account_model._llm_store_balances(balances, barrier), 0)
            fresh = [(username, self.user_id, 2 ** 40 + 1562500, 2 ** 40, 2 ** 40)]
            self.assertEqual(account_model._llm_store_balances(fresh, balance_stamp()), 1)
        with self.registry.cursor() as cr:
            cr.execute('SELECT quota_cached, balance_invalidated_at FROM %s WHERE id = %%s'
                       % ACCOUNT_TABLE, (self.account_id,))
            self.assertEqual(cr.fetchone(), (2 ** 40 + 1562500, None))
        self.assertEqual(self._row(order_id)['state'], 'credited')

    def test_lost_event_ownership_rolls_back_order_and_balance(self):
        order_id = self._order()
        claim = self._claim()
        with self.registry.cursor() as cr:
            cr.execute('UPDATE %s SET target_id = %%s WHERE id = %%s' % EVENT_TABLE,
                       (order_id + 1000000, claim['event_id']))
        try:
            with self.registry.cursor() as cr:
                env_for(self.registry, cr)[ORDER_MODEL]._llm_classify_credit(
                    claim, nac.Ok(request_id='cannot-commit'))
        except ValueError:
            pass
        else:
            self.fail('ownership mismatch must abort the entire result transaction')
        self.assertEqual(self._row(order_id)['state'], 'sending')
        self.assertEqual(self._events()[0]['result'], 'started')
