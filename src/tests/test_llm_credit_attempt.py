"""Slice A real transactions; fake upstream, isolated exclusive sandbox only.

These tests are prepared, not a record of a sandbox run. Inherited fixture
pauses only its crons, scopes claims to owned accounts, and purges exact IDs.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from psycopg2.errors import CheckViolation, UniqueViolation

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from ..models import llm_credit_attempt as ledger_module, llm_credit_order as worker_module
from ..models.llm_credit_attempt import ATTEMPT_MODEL, ATTEMPT_TABLE
from ..models.llm_credit_order import ORDER_MODEL
from ..services import newapi_client as nac
from .llm_test_support import env_for
from .test_llm_credit_worker import TestLlmCreditWorkerCommitted


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditAttemptCommitted(TestLlmCreditWorkerCommitted):
    """Own methods only; reuse committed setup/helpers, not inherited tests."""

    def _attempts(self, order_id):
        with self.registry.cursor() as cr:
            cr.execute('SELECT * FROM ' + ATTEMPT_TABLE + ' WHERE order_id = %s ORDER BY attempt_no',
                       (order_id,))
            return cr.dictfetchall()

    def _entry(self, claim):
        with self.registry.cursor() as cr:
            return env_for(self.registry, cr)[ORDER_MODEL]._llm_dispatch_claim(claim)

    def test_claim_commits_identity_and_exact_snapshots(self):
        order_id = self._order()
        claim = self._claim()
        row, = self._attempts(order_id)  # Different connection sees commit.
        self.assertEqual(str(uuid.UUID(row['attempt_uuid'], version=4)), claim['attempt_uuid'])
        self.assertEqual(uuid.UUID(claim['attempt_uuid']).version, 4)
        self.assertEqual((row['order_id'], row['account_id'], row['attempt_no'],
                          row['target_user_id'], row['quota'], row['reservation_event_id']),
                         (order_id, self.account_id, 1, self.user_id, 1562500, claim['event_id']))
        self.assertFalse(row['dispatch_claimed_at'])
        self.assertEqual(self._row(order_id)['state'], 'sending')
        self.assertEqual(self._events()[0]['target_id'], order_id)
        self.assertFalse(self.fake.calls)

    def test_issue_failure_rolls_back_order_event_and_ledger(self):
        order_id = self._order()
        before_order, before_events = self._row(order_id), self._events()
        original_execute = ledger_module._issue_cr

        def insert_then_fail(*args):
            original_execute(*args)
            raise RuntimeError('synthetic failure after ledger insert')
        with patch.object(worker_module, '_issue_cr', side_effect=insert_then_fail):
            with self.assertRaises(RuntimeError):
                self._claim()
        self.assertEqual(self._row(order_id), before_order)
        self.assertEqual(self._events(), before_events)
        self.assertEqual(self._attempts(order_id), [])
        self.assertFalse(self.fake.calls)

    def test_n6_denial_spends_no_identity_or_attempt(self):
        order_id = self._retry_order()
        self._seed_events(29)
        before = self._row(order_id)
        events = self._events()
        self.assertIsNone(self._claim())
        after = self._row(order_id)
        for key in ('attempt', 'state', 'sent_at', 'convert_event_id', 'lease_until'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self._events(), events)
        self.assertEqual(self._attempts(order_id), [])
        self.assertFalse(self.fake.calls)

    def test_parallel_dispatch_cas_has_one_winner(self):
        self._order()
        claim = self._claim()
        barrier = Barrier(2)

        def contender():
            barrier.wait(timeout=10)
            return self._entry(claim)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            results = [f.result(timeout=15) for f in futures]
        self.assertEqual(sorted(results), [False, True])
        self.assertTrue(self._attempts(claim['id'])[0]['dispatch_claimed_at'])
        self.assertFalse(self.fake.calls)

    def test_restart_after_cas_cannot_send_or_clear_latch(self):
        self._order()
        claim = self._claim()
        self.assertTrue(self._entry(claim))
        receipt = self._attempts(claim['id'])[0]['dispatch_claimed_at']
        self._send(dict(claim))  # Fresh connection, simulates restart before HTTP.
        self.assertFalse(self.fake.calls)
        self.assertEqual(self._row(claim['id'])['state'], 'sending')
        self.assertEqual(self._attempts(claim['id'])[0]['dispatch_claimed_at'], receipt)

    def test_uuid_owner_and_snapshot_mismatch_fail_closed(self):
        order_id = self._order()
        claim = self._claim()
        bad = dict(claim, attempt_uuid=str(uuid.uuid4()))
        self.assertFalse(self._entry(bad))
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            self.assertIsNone(orders._llm_claimed_row(bad))
            self.assertFalse(orders._llm_worker_update(bad, {'state': 'credited'}))
        ledger_before = self._attempts(order_id)
        # Keep the fixed-rate CHECK valid while diverging from the issued snapshot.
        self._update_order(order_id, points=101, quota=1578125)
        self.assertEqual(self._attempts(order_id), ledger_before)
        self.assertFalse(self._entry(claim))
        self._send(claim)
        self.assertFalse(self.fake.calls)
        self.assertEqual(self._row(order_id)['state'], 'sending')

    def test_verifier_takeover_fences_late_worker_and_bad_uuid(self):
        order_id = self._order()
        claim = self._claim()
        self.assertTrue(self._entry(claim))
        self._update_order(order_id, lease_until=fields.Datetime.now() - timedelta(seconds=1))
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            verifier = orders._llm_claim_credit_verification()
            self.assertEqual(verifier['id'], order_id)
            self.assertEqual(verifier['attempt_uuid'], claim['attempt_uuid'])
            self.assertFalse(orders._llm_worker_update(claim, {'state': 'credited'}))
            bad = dict(verifier, attempt_uuid=str(uuid.uuid4()))
            self.assertIsNone(orders._llm_verify_claimed_row(bad))
            self.assertFalse(orders._llm_verify_update(bad, {'state': 'paid'}))
            wrong_event = dict(verifier, event_id=verifier['event_id'] + 1)
            self.assertIsNone(orders._llm_verify_claimed_row(wrong_event))
            self.assertFalse(orders._llm_verify_update(wrong_event, {'state': 'paid'}))
            self.assertIsNotNone(orders._llm_verify_claimed_row(verifier))
        self.assertFalse(self._entry(claim))
        self.assertTrue(self._attempts(order_id)[0]['dispatch_claimed_at'])

    def test_safe_retry_keeps_both_identities_and_rejects_late_uuid(self):
        order_id = self._order()
        first = self._claim()
        self.fake.fail_before['ap_add_quota'] = nac.NotExecuted('connect')
        self._send(first)
        self._due(order_id)
        second = self._claim()
        self.assertNotEqual(first['attempt_uuid'], second['attempt_uuid'])
        rows = self._attempts(order_id)
        self.assertEqual([r['attempt_uuid'] for r in rows],
                         [first['attempt_uuid'], second['attempt_uuid']])
        self.assertEqual([r['attempt_no'] for r in rows], [1, 2])
        self.assertFalse(self._entry(dict(second, attempt_uuid=first['attempt_uuid'])))
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            self.assertFalse(orders._llm_worker_update(first, {'state': 'credited'}))

    def test_sql_unique_uuid_order_attempt_and_canonical_shape(self):
        order_id = self._order()
        self._claim()
        columns = ('order_id,account_id,attempt_no,attempt_uuid,target_user_id,quota,'
                   'reservation_event_id')
        with self.registry.cursor() as cr:
            for attempt_expr, uuid_expr, params, error in (
                    ('attempt_no+1', 'attempt_uuid', [], UniqueViolation),
                    ('attempt_no', '%s', [str(uuid.uuid4())], UniqueViolation),
                    ('attempt_no+1', '%s', ['EAF6854D-98A8-4807-A69C-05A7AC656536'], CheckViolation)):
                with self.assertRaises(error), cr.savepoint(flush=False):
                    cr.execute('INSERT INTO ' + ATTEMPT_TABLE + ' (' + columns + ') '
                               'SELECT order_id,account_id,' + attempt_expr + ',' + uuid_expr + ', '
                               'target_user_id,quota,reservation_event_id FROM ' + ATTEMPT_TABLE +
                               ' WHERE order_id = %s', params + [order_id])
        self.assertEqual(len(self._attempts(order_id)), 1)

    def test_orm_mutation_denied_even_sudo_and_public_cannot_read(self):
        order_id = self._order()
        self._claim()
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            attempts = env[ATTEMPT_MODEL].sudo().search([('order_id', '=', order_id)])
            self.assertEqual(len(attempts), 1)
            for mutation in (lambda: attempts.write({'quota': 1}), attempts.unlink,
                             lambda: env[ATTEMPT_MODEL].sudo().create({})):
                with self.assertRaises(AccessError):
                    mutation()
            settings = attempts.with_user(env.ref('base.user_admin'))
            self.assertEqual(settings.read(['attempt_uuid'])[0]['attempt_uuid'], attempts.attempt_uuid)
            portal_user = env['res.users'].with_context(no_reset_password=True).create({
                'name': 'Attempt ACL QA', 'login': 'attempt_acl_' + uuid.uuid4().hex,
                'groups_id': [(6, 0, [env.ref('base.group_portal').id])],
            })
            self.requester_ids.append(portal_user.id)
            for user in (env.ref('base.public_user'), portal_user):
                with self.assertRaises(AccessError):
                    attempts.with_user(user).read(['attempt_uuid'])
            with self.assertRaises(AccessError):
                ledger_module._issue_cr(cr, {}, 1, object())


    def _invalid_reservation_verifier(self):
        order_id = self._order()
        self.fake.fail_next['ap_add_quota'] = nac.Unknown('timeout')
        self._run()
        self.assertEqual(self._row(order_id)['state'], 'unknown')
        self._update_order(order_id, convert_event_id=0)
        with self.registry.cursor() as cr:
            claim = env_for(self.registry, cr)[ORDER_MODEL]._llm_claim_credit_verification()
        self.assertEqual(claim['id'], order_id)
        self.assertTrue(claim['attempt_uuid'])
        return order_id, claim

    def test_invalid_reservation_review_rejects_wrong_owners_and_financial_updates(self):
        order_id, claim = self._invalid_reservation_verifier()
        before, ledger, events = self._row(order_id), self._attempts(order_id), self._events()
        calls = list(self.fake.calls)
        bad_claims = [dict(claim, **{key: value}) for key, value in (
            ('id', order_id + 1), ('account_id', self.account_id + 1),
            ('attempt', claim['attempt'] + 1), ('state', 'paid'), ('token', 'stale'),
            ('event_id', None), ('event_id', -1), ('event_id', False),
            ('event_id', '0'), ('event_id', 1), ('attempt_uuid', None),
            ('attempt_uuid', str(uuid.uuid4())),
        )]
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            for bad in bad_claims:
                with self.subTest(claim_keys=[k for k in claim if claim[k] != bad.get(k)]):
                    self.assertFalse(orders._llm_verify_invalid_reservation_review(
                        bad, worker_module.CREDIT_EVIDENCE_INVALID))
            self.assertFalse(orders._llm_verify_invalid_reservation_review(
                claim, worker_module.CREDIT_EVIDENCE_CONFLICT))
            for state in ('credited', 'paid', 'needs_review'):
                self.assertFalse(orders._llm_verify_update(claim, {'state': state}))
            self.assertFalse(orders._llm_verify_finish(claim, 'paid'))
        self.assertEqual(self._row(order_id), before)
        self.assertEqual(self._attempts(order_id), ledger)
        self.assertEqual(self._events(), events)
        self.assertEqual(self.fake.calls, calls)
        # All denied calls left the genuine verifier's quarantine authority intact.
        with self.registry.cursor() as cr:
            self.assertTrue(env_for(self.registry, cr)[ORDER_MODEL]._llm_verify_review(
                claim, worker_module.CREDIT_EVIDENCE_INVALID))
        reviewed = self._row(order_id)
        self.assertEqual(reviewed['state'], 'needs_review')
        self.assertEqual(reviewed['last_error_code'], worker_module.CREDIT_EVIDENCE_INVALID)
        self.assertFalse(reviewed['verify_token'])
        self.assertFalse(reviewed['verify_lease_until'])
        self.assertEqual(reviewed['convert_event_id'], 0)
        self.assertEqual(self._attempts(order_id), ledger)
        self.assertEqual(self._events(), events)
        self.assertEqual(self.fake.calls, calls)

    def test_invalid_reservation_review_rejects_snapshot_drift_and_expired_lease(self):
        order_id, claim = self._invalid_reservation_verifier()
        ledger, events, calls = self._attempts(order_id), self._events(), list(self.fake.calls)
        for changed, restored in (
                ({'newapi_user_id': self.user_id + 1}, {'newapi_user_id': self.user_id}),
                ({'points': 101, 'quota': 1578125}, {'points': 100, 'quota': 1562500})):
            self._update_order(order_id, **changed)
            before = self._row(order_id)
            with self.registry.cursor() as cr:
                self.assertFalse(env_for(self.registry, cr)[ORDER_MODEL]._llm_verify_review(
                    claim, worker_module.CREDIT_EVIDENCE_INVALID))
            self.assertEqual(self._row(order_id), before)
            self._update_order(order_id, **restored)
        self._update_order(order_id, verify_lease_until=fields.Datetime.now() - timedelta(seconds=1))
        before = self._row(order_id)
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            self.assertFalse(orders._llm_verify_invalid_reservation_review(
                claim, worker_module.CREDIT_EVIDENCE_INVALID))
        self.assertEqual(self._row(order_id), before)
        with self.registry.cursor() as cr:
            takeover = env_for(self.registry, cr)[ORDER_MODEL]._llm_claim_credit_verification()
        self.assertEqual(takeover['id'], order_id)
        self.assertNotEqual(takeover['token'], claim['token'])
        with self.registry.cursor() as cr:
            orders = env_for(self.registry, cr)[ORDER_MODEL]
            self.assertFalse(orders._llm_verify_invalid_reservation_review(
                claim, worker_module.CREDIT_EVIDENCE_INVALID))
            self.assertTrue(orders._llm_verify_review(takeover, worker_module.CREDIT_EVIDENCE_INVALID))
        self.assertEqual(self._row(order_id)['state'], 'needs_review')
        self.assertEqual(self._attempts(order_id), ledger)
        self.assertEqual(self._events(), events)
        self.assertEqual(self.fake.calls, calls)

    def test_invalid_reservation_review_cas_has_one_winner_and_keeps_dispatch_latch(self):
        order_id, claim = self._invalid_reservation_verifier()
        ledger, events, calls = self._attempts(order_id), self._events(), list(self.fake.calls)
        self.assertTrue(ledger[0]['dispatch_claimed_at'])
        barrier = Barrier(2)

        def contender():
            barrier.wait(timeout=10)
            with self.registry.cursor() as cr:
                return env_for(self.registry, cr)[ORDER_MODEL]._llm_verify_invalid_reservation_review(
                    claim, worker_module.CREDIT_EVIDENCE_INVALID)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self._row(order_id)['state'], 'needs_review')
        self.assertEqual(self._attempts(order_id), ledger)
        self.assertEqual(self._events(), events)
        self.assertEqual(self.fake.calls, calls)
        with self.registry.cursor() as cr:
            self.assertFalse(env_for(self.registry, cr)[ORDER_MODEL]._llm_verify_update(
                claim, {'state': 'credited'}))
