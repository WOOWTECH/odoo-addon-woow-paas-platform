"""Committed T1/payment, worker claim and void against real accounting rows.

Only configuration and notification delivery are isolated. Every order,
payment, wallet transaction and refund crosses a real PostgreSQL commit.
"""
import sys
import threading
import time
import traceback
import uuid
from unittest import TestCase
from unittest.mock import patch

from psycopg2 import errorcodes
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.llm_common import ERR_INSUFFICIENT_BALANCE, LlmError, _llm_internal
from ..models.llm_credit_order import (
    CREDIT_CRON_XMLID,
    ORDER_MODEL,
    ORDER_TABLE,
)
from ..services import newapi_client as nac
from .llm_test_support import CommittedLlmFixture, env_for


def _thread_stacks(threads):
    """Return useful worker state if a real-transaction race does not finish."""
    frames = sys._current_frames()
    return {
        thread.name: ''.join(traceback.format_stack(frames[thread.ident]))
        for thread in threads
        if thread.ident in frames
    }


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditAccountingCommitted(TransactionCase):
    """Use independent registry cursors, including during assertions and cleanup."""

    def setUp(self):
        super().setUp()
        self.fixture = CommittedLlmFixture(self).start()
        self._tag = uuid.uuid4().hex
        self._owned_journals = []
        cron_patch = patch.object(type(self.env['ir.cron']), '_trigger',
                                  lambda records, *args, **kwargs: False)
        cron_patch.start()
        self.addCleanup(cron_patch.stop)
        upstream_patch = patch.object(
            nac.NewApiClient, '_request',
            side_effect=AssertionError('real upstream HTTP forbidden in committed accounting tests'))
        upstream_patch.start()
        self.addCleanup(upstream_patch.stop)
        # Notification delivery is outside the accounting contract, and this
        # base predates Task 6's needs_human incident policy integration.
        incident_patch = patch.object(
            type(self.env['woow_paas_platform.llm_event']), '_llm_incident',
            return_value=False)
        incident_patch.start()
        self.addCleanup(incident_patch.stop)
        guard_patch = patch.object(type(self.env['woow_paas_platform.llm_account']),
                                   '_llm_guard_tripped', return_value=False)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        self._pause_credit_cron()
        self.addCleanup(self._cleanup_committed)
        self.partner_id = self.fixture.partner('LLM committed credit ' + self._tag[:8])
        self.account_id = self.fixture.activate(self.partner_id)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env['woow_paas_platform.llm_account'].browse(self.account_id)
            _llm_internal(account).write({'state': 'active', 'newapi_user_id': 480810})
            company = env.company.sudo()
            Journal = env['account.journal'].sudo()
            for kind in ('bank', 'sale'):
                if not Journal.search([('company_id', '=', company.id),
                                       ('type', '=', kind)], limit=1):
                    journal = Journal.create({
                        'name': f'LLM committed {kind} {self._tag[:8]}',
                        'type': kind, 'code': (self._tag[:4] + kind[0]).upper(),
                        'company_id': company.id,
                    })
                    self._owned_journals.append(journal.id)

    def _pause_credit_cron(self):
        self.credit_cron_id = self.env.ref(CREDIT_CRON_XMLID).id
        with self.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', ('75s',))
            cr.execute('SELECT active FROM ir_cron WHERE id = %s FOR NO KEY UPDATE',
                       (self.credit_cron_id,))
            self._credit_cron_active = cr.fetchone()[0]
            cr.execute('UPDATE ir_cron SET active = false WHERE id = %s',
                       (self.credit_cron_id,))

    def _cleanup_committed(self):
        try:
            self._purge_accounting()
        except Exception:
            # The payment unlink may fail after a real T1 commit. Its cursor
            # rolls back, so a fresh transaction must make every remaining
            # fixture outbox row ineligible before waking the live cron.
            self._quarantine_fixture_orders()
            self._assert_no_worker_orders()
            self._restore_credit_cron()
            raise  # Preserve the original cleanup failure for CI.
        self._assert_no_worker_orders()
        # _trigger is a no-op in this process; we create no credit trigger and
        # never delete another process's trigger rows.
        self._restore_credit_cron()

    def _quarantine_fixture_orders(self):
        if not self.fixture.partner_ids:
            return
        with self.registry.cursor() as cr:
            # The partial inflight index allows only one needs_review row per
            # account. Park every worker-eligible test order in a terminal,
            # nonclaimable state first; all changes commit together. A user
            # order can then be promoted to needs_review for manual inspection.
            cr.execute(
                'UPDATE ' + ORDER_TABLE + ' SET state = CASE WHEN source = %s '
                'THEN %s ELSE %s END, lease_until = NULL, '
                'next_attempt_at = NULL, verify_token = NULL, verify_lease_until = NULL, '
                'write_uid = %s, write_date = NOW() '
                "WHERE partner_id = ANY(%s) AND source IN ('user', 'manual', 'dr') "
                "AND state IN ('paid', 'sending', 'unknown') "
                'RETURNING id, account_id, source',
                ('user', 'void_failed', 'cancelled', SUPERUSER_ID,
                 list(self.fixture.partner_ids)))
            first_user_order = {}
            for order_id, account_id, source in cr.fetchall():
                if source == 'user':
                    first_user_order[account_id] = min(
                        order_id, first_user_order.get(account_id, order_id))
            for account_id, order_id in first_user_order.items():
                cr.execute('SELECT 1 FROM ' + ORDER_TABLE +
                           " WHERE account_id = %s AND state = 'needs_review' LIMIT 1",
                           (account_id,))
                if not cr.fetchone():
                    cr.execute('UPDATE ' + ORDER_TABLE + ' SET state = %s '
                               'WHERE id = %s AND state = %s',
                               ('needs_review', order_id, 'void_failed'))
                    if cr.rowcount != 1:
                        raise AssertionError('fixture quarantine lost its review order')

    def _assert_no_worker_orders(self):
        if not self.fixture.partner_ids:
            return
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT id FROM ' + ORDER_TABLE +
                " WHERE partner_id = ANY(%s) AND source IN ('user','manual','dr') "
                "AND state IN ('paid', 'sending', 'unknown') "
                'LIMIT 1', (list(self.fixture.partner_ids),))
            if cr.fetchone():
                raise AssertionError('fixture still has a worker-eligible credit order')

    def _restore_credit_cron(self):
        with self.registry.cursor() as cr:
            cr.execute('UPDATE ir_cron SET active = %s WHERE id = %s',
                       (self._credit_cron_active, self.credit_cron_id))

    def _purge_accounting(self):
        """Remove only this partner's accounting, before fixture deletes its account."""
        partner_ids = self.fixture.partner_ids
        if not partner_ids:
            return
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            Order = env[ORDER_MODEL].sudo()
            orders = Order.search([('partner_id', 'in', partner_ids)])
            order_ids = orders.ids
            if order_ids:
                cr.execute('UPDATE ' + ORDER_TABLE + ' SET invoice_id = NULL, '
                           'wallet_transaction_id = NULL, void_credit_note_id = NULL, '
                           'void_wallet_transaction_id = NULL WHERE id = ANY(%s)',
                           (order_ids,))
            Payment = env['account.payment'].sudo()
            payments = Payment.search([('partner_id', 'in', partner_ids)])
            # account.payment.unlink() removes move_id before the payment record.
            # A paid payment with an outstanding account is invalid during that
            # short interval, so make the test-owned payments drafts first.
            payments.filtered(lambda payment: payment.state not in ('draft', 'canceled')).write({
                'state': 'draft',
            })
            payments.unlink()  # Native unlink drafts and removes payment moves.
            invoices = env['account.move'].sudo().search([
                ('partner_id', 'in', partner_ids),
                ('move_type', 'in', ('out_invoice', 'out_refund')),
            ])
            for invoice in invoices.filtered(lambda move: move.state == 'posted'):
                invoice.button_draft()  # Removes residual reconciliation.
            invoices.unlink()
            env['wallet.transaction'].sudo().search([
                ('partner_id', 'in', partner_ids),
            ]).unlink()
            env['loyalty.card'].sudo().search([
                ('partner_id', 'in', partner_ids),
            ]).unlink()
            if order_ids:
                cr.execute('DELETE FROM woow_paas_platform_llm_credit_attempt '
                           'WHERE order_id = ANY(%s)', (order_ids,))
                cr.execute('DELETE FROM ' + ORDER_TABLE + ' WHERE id = ANY(%s)',
                           (order_ids,))
            if self._owned_journals:
                env['account.journal'].sudo().browse(self._owned_journals).unlink()

    def _fund(self, points):
        with self.registry.cursor() as cr:
            partner = env_for(self.registry, cr)['res.partner'].browse(self.partner_id)
            partner.add_wallet_points(points, description='LLM committed fixture')
        self.assertEqual(self._snapshot()['balance'], points)

    def _convert(self, env, request_id, points=100):
        return env[ORDER_MODEL]._llm_convert(
            env['res.partner'].browse(self.partner_id), points, request_id,
            True, True, env.user, None, '203.0.113.8')

    def _committed_convert(self, request_id, points=100):
        with self.registry.cursor() as cr:
            return self._convert(env_for(self.registry, cr), request_id, points)

    def _snapshot(self):
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            partner = env['res.partner'].browse(self.partner_id)
            partner.invalidate_recordset(['wallet_balance'])
            return {
                'balance': partner.wallet_balance,
                'orders': env[ORDER_MODEL].sudo().search([
                    ('partner_id', '=', self.partner_id)]).ids,
                'payments': env['account.payment'].sudo().search([
                    ('partner_id', '=', self.partner_id)]).ids,
                'moves': env['account.move'].sudo().search([
                    ('partner_id', '=', self.partner_id),
                    ('move_type', 'in', ('out_invoice', 'out_refund')),
                ]).ids,
                'wallet': env['wallet.transaction'].sudo().search([
                    ('partner_id', '=', self.partner_id)]).ids,
            }

    def _claim_for_void(self, order_id):
        """Run Task 5's independent claim and verify its committed ownership."""
        claim = self.env[ORDER_MODEL]._llm_claim_credit(account_ids=[self.account_id])
        self.assertIsInstance(claim, dict)
        self.assertEqual((claim['id'], claim['account_id'], claim['attempt']),
                         (order_id, self.account_id, 1))
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            order = env[ORDER_MODEL].sudo().browse(order_id)
            event = env['woow_paas_platform.llm_event'].sudo().browse(claim['event_id'])
            self.assertEqual((order.state, order.attempt, order.convert_event_id),
                             ('sending', 1, claim['event_id']))
            self.assertTrue(order.lease_until)
            self.assertEqual((event.result, event.target_model, event.target_id,
                              event.credit_t1_pending),
                             ('started', ORDER_MODEL, order_id, False))
        return claim

    def _race(self, ids):
        barrier = threading.Barrier(2, timeout=15)
        outcomes = [None, None]
        conflicts = []
        # Construct both real pool connections and their Environments on the
        # main test thread. Doing Environment setup inside workers can instead
        # exercise Odoo's registry-loading lock, not PostgreSQL concurrency.
        cursors = [self.registry._db.cursor() for _request_id in ids]
        environments = [env_for(self.registry, cr) for cr in cursors]

        def run(index):
            cr = cursors[index]
            env = environments[index]
            try:
                for attempt in range(1, 6):
                    try:
                        if attempt == 1:
                            # Force both RR snapshots to predate either commit.
                            cr.execute('SELECT id FROM ' + ORDER_TABLE + ' LIMIT 1')
                            barrier.wait()
                        result = self._convert(env, ids[index])
                        cr.commit()
                    except SerializationFailure as exc:
                        if exc.pgcode != errorcodes.SERIALIZATION_FAILURE:
                            raise AssertionError('unexpected serialization SQLSTATE') from exc
                        conflicts.append(index)
                        cr.rollback()
                        if attempt == 5:
                            raise
                    except LlmError as exc:
                        cr.rollback()
                        outcomes[index] = ('business', exc.code)
                        return
                    else:
                        outcomes[index] = ('ok', result)
                        return
                raise AssertionError('serialization retry loop exhausted')
            except Exception as exc:  # noqa: BLE001 - surface thread failures in the test
                outcomes[index] = ('error', exc)
            finally:
                if not cr.closed:
                    cr.close()

        threads = [threading.Thread(target=run, args=(index,), daemon=True,
                                    name='llm-credit-race-%d' % index)
                   for index in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        finally:
            barrier.abort()
            for thread in threads:
                thread.join(2)
        alive = [thread for thread in threads if thread.is_alive()]
        self.assertFalse(alive, 'credit conversion race workers did not finish: %r'
                         % _thread_stacks(alive))
        for status, value in outcomes:
            if status == 'error':
                raise value
        self.assertEqual(len(conflicts), 1,
                         'both workers must cross a real 40001 retry boundary')
        return outcomes

    def test_t1_commit_payment_and_same_uuid_replay(self):
        self._fund(150)
        request_id = str(uuid.uuid4())
        first = self._committed_convert(request_id)
        after_first = self._snapshot()
        again = self._committed_convert(request_id)
        self.assertEqual(again['order_id'], first['order_id'])
        self.assertEqual(self._snapshot(), after_first)
        self.assertEqual(after_first['balance'], 50)
        self.assertEqual(len(after_first['orders']), 1)
        self.assertEqual(len(after_first['payments']), 1)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            order = env[ORDER_MODEL].browse(first['order_id'])
            self.assertEqual((order.invoice_id.state, order.invoice_id.payment_state),
                             ('posted', 'paid'))
            self.assertEqual((order.invoice_id.amount_total, order.invoice_id.amount_tax,
                              order.invoice_id.amount_residual), (100, 0, 0))
            self.assertEqual((order.wallet_transaction_id.transaction_type,
                              order.wallet_transaction_id.amount), ('deduct', -100))
            self.assertTrue(order.convert_event_id)

    def test_two_connections_same_uuid_retrying_debits_once(self):
        self._fund(150)
        request_id = str(uuid.uuid4())
        outcomes = self._race([request_id, request_id])
        self.assertEqual([status for status, _ in outcomes], ['ok', 'ok'])
        self.assertEqual(outcomes[0][1]['order_id'], outcomes[1][1]['order_id'])
        snapshot = self._snapshot()
        self.assertEqual((snapshot['balance'], len(snapshot['orders']),
                          len(snapshot['payments'])), (50, 1, 1))

    def test_two_connections_different_uuid_cannot_overdraw(self):
        self._fund(150)
        outcomes = self._race([str(uuid.uuid4()), str(uuid.uuid4())])
        self.assertEqual(sorted(status for status, _ in outcomes), ['business', 'ok'])
        self.assertEqual([value for status, value in outcomes if status == 'business'],
                         [ERR_INSUFFICIENT_BALANCE])
        snapshot = self._snapshot()
        self.assertEqual((snapshot['balance'], len(snapshot['orders']),
                          len(snapshot['payments'])), (50, 1, 1))
        self.assertGreaterEqual(snapshot['balance'], 0)

    def test_committed_t1_void_once_and_fully_settled(self):
        self._fund(150)
        order_id = self._committed_convert(str(uuid.uuid4()))['order_id']
        claim = self._claim_for_void(order_id)
        before = self._snapshot()
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            self.assertTrue(env[ORDER_MODEL]._llm_worker_void_pending(claim))
        after = self._snapshot()
        self.assertEqual(after['balance'], 150)
        self.assertEqual(len(after['wallet']) - len(before['wallet']), 1)
        self.assertEqual(len(after['payments']) - len(before['payments']), 1)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            order = env[ORDER_MODEL].browse(order_id)
            note = order.void_credit_note_id
            refund = order.void_wallet_transaction_id
            self.assertEqual(order.state, 'voided')
            self.assertEqual((refund.transaction_type, refund.amount), ('refund', 100))
            self.assertEqual((note.move_type, note.state, note.payment_state,
                              note.amount_total, note.amount_tax, note.amount_residual),
                             ('out_refund', 'posted', 'paid', 100, 0, 0))
            self.assertEqual(note.reversed_entry_id, order.invoice_id)
            self.assertEqual(note.woow_llm_credit_order_id, order)
            self.assertFalse(env[ORDER_MODEL]._llm_worker_void_pending(claim))
        self.assertEqual(self._snapshot(), after)

    def test_failed_payment_cleanup_quarantines_before_restoring_credit_cron(self):
        self._fund(250)
        order_ids = [self._committed_convert(str(uuid.uuid4()))['order_id']
                     for _index in range(2)]
        self._claim_for_void(order_ids[0])  # One sending and one paid outbox row.
        PaymentClass = type(self.env['account.payment'])
        with patch.object(PaymentClass, 'unlink',
                          side_effect=UserError('injected payment unlink failure')) as blocked, \
                TestCase.assertRaises(self, UserError) as raised:
            self._cleanup_committed()
        blocked.assert_called_once()
        self.assertEqual(str(raised.exception), 'injected payment unlink failure')
        with self.registry.cursor() as cr:
            cr.execute('SELECT active FROM ir_cron WHERE id = %s', (self.credit_cron_id,))
            self.assertEqual(cr.fetchone()[0], self._credit_cron_active)
            cr.execute('SELECT state, lease_until, next_attempt_at, verify_token '
                       'FROM ' + ORDER_TABLE + ' WHERE id = ANY(%s)', (order_ids,))
            quarantined = cr.fetchall()
            self.assertEqual(sorted(state for state, *_rest in quarantined),
                             ['needs_review', 'void_failed'])
            self.assertTrue(all(row[1:] == (None, None, None) for row in quarantined))
            cr.execute('SELECT count(*) FROM ' + ORDER_TABLE +
                       " WHERE partner_id = %s AND state IN ('paid', 'sending', 'unknown')",
                       (self.partner_id,))
            self.assertEqual(cr.fetchone()[0], 0)
        # The same real worker claim that normally takes this order now has
        # no eligible row, even though its payment survived the failed purge.
        self.assertEqual(len(self._snapshot()['payments']), 2)
        self.assertIsNone(self.env[ORDER_MODEL]._llm_claim_credit(
            account_ids=[self.account_id]))

    def test_two_workers_claim_once_and_stale_void_cannot_refund(self):
        self._fund(150)
        order_id = self._committed_convert(str(uuid.uuid4()))['order_id']
        lock_held = threading.Event()
        release_first = threading.Event()
        results = [None, None]
        OrderClass = type(self.env[ORDER_MODEL])
        original = OrderClass._llm_claim_event_cr

        def hold_after_order_update(cr, row):
            # _llm_claim_event_cr runs after the UPDATE ... FOR UPDATE OF o
            # returned the row, but before the claim transaction commits.
            lock_held.set()
            if not release_first.wait(30):
                raise TimeoutError('first claim was not released')
            return original(cr, row)

        def claim(index):
            try:
                with self.registry.cursor() as cr:
                    env = env_for(self.registry, cr)
                    results[index] = env[ORDER_MODEL]._llm_claim_credit(
                        account_ids=[self.account_id])
            except Exception as exc:  # noqa: BLE001 - report thread failures on test thread
                results[index] = exc

        first = threading.Thread(target=claim, args=(0,), daemon=True)
        second = threading.Thread(target=claim, args=(1,), daemon=True)
        with patch.object(OrderClass, '_llm_claim_event_cr',
                          staticmethod(hold_after_order_update)):
            first.start()
            try:
                self.assertTrue(lock_held.wait(10), results)
                with self.registry.cursor() as cr:
                    cr.execute('SELECT state, attempt FROM ' + ORDER_TABLE +
                               ' WHERE id = %s', (order_id,))
                    self.assertEqual(cr.fetchone(), ('paid', 0),
                                     'the first claim must still be uncommitted')
                # SKIP LOCKED must finish before the worker's 5s lock_timeout.
                # The extra 0.5s on elapsed allows for CI scheduling overhead.
                skip_locked_join_seconds = 2.0
                second_started = time.monotonic()
                second.start()
                second.join(skip_locked_join_seconds)
                elapsed = time.monotonic() - second_started
                self.assertFalse(second.is_alive(),
                                 f'the second claim waited {elapsed:.2f}s instead of '
                                 'skipping the locked order')
                self.assertLess(elapsed, skip_locked_join_seconds + 0.5)
                self.assertIsNone(results[1], results)
            finally:
                release_first.set()
                first.join(30)
                if second.ident is not None:
                    second.join(30)
        self.assertFalse(first.is_alive() or second.is_alive(), results)
        for result in results:
            if isinstance(result, Exception):
                raise result
        claims = [result for result in results if result]
        self.assertEqual(len(claims), 1, results)
        owned = claims[0]
        self.assertEqual((owned['id'], owned['account_id'], owned['attempt']),
                         (order_id, self.account_id, 1))
        before = self._snapshot()
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            Order = env[ORDER_MODEL]
            self.assertFalse(Order._llm_worker_void_pending(
                dict(owned, attempt=owned['attempt'] + 1)))
            self.assertFalse(Order._llm_worker_void_pending(
                dict(owned, event_id=owned['event_id'] + 1000000)))
        self.assertEqual(self._snapshot(), before)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            self.assertTrue(env[ORDER_MODEL]._llm_worker_void_pending(owned))
        after = self._snapshot()
        self.assertEqual((after['balance'], len(after['orders']),
                          len(after['payments']) - len(before['payments']),
                          len(after['wallet']) - len(before['wallet'])),
                         (150, 1, 1, 1))
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            self.assertFalse(env[ORDER_MODEL]._llm_worker_void_pending(owned))
        self.assertEqual(self._snapshot(), after)

    def test_failure_after_real_settlement_rolls_back_then_commits_void_failed(self):
        self._fund(150)
        order_id = self._committed_convert(str(uuid.uuid4()))['order_id']
        claim = self._claim_for_void(order_id)
        before = self._snapshot()
        Sub = type(self.env['sh.subscription.subscription'])
        original = Sub._woow_settle_refund_credit_note
        settled = []

        def fail_after_settle(records, *args, **kwargs):
            payment = original(records, *args, **kwargs)
            self.assertTrue(payment, 'The injection must follow a real settlement')
            settled.append(payment.id)
            raise UserError('injected after real settlement')

        with patch.object(Sub, '_woow_settle_refund_credit_note', fail_after_settle), \
                self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            self.assertFalse(env[ORDER_MODEL]._llm_worker_void_pending(claim))
        self.assertEqual(len(settled), 1)
        self.assertEqual(self._snapshot(), before)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            order = env[ORDER_MODEL].browse(order_id)
            self.assertEqual(order.state, 'void_failed')
            self.assertFalse(order.void_credit_note_id)
            self.assertFalse(order.void_wallet_transaction_id)
            event = env['woow_paas_platform.llm_event'].browse(claim['event_id'])
            self.assertEqual(event.result, 'not_executed')
