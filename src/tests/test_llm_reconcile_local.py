"""Real ledger/scanner tests; no global sweep.

Payments and reversals use actual wallet/accounting services. Only the external
durable sinks are replaced, since their independent transactions cannot observe
TransactionCase fixtures. Every worker invocation supplies owned order IDs and
uses the same SQL/locking core as cron. No production config or cursor is changed.
The deadline regression separately commits only its marker-owned fixture and
scoped cursors, then reads through fresh connections to prove turn durability.
"""
import logging
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError, errors

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import llm_common
from ..models import llm_reconcile_local as local
from ..models.llm_common import LlmCronTick, _llm_internal
from ..models.llm_credit_order import ORDER_MODEL, ORDER_TABLE, _credit_internal
from ..models.llm_event import LlmAlert, LlmIncident, _env_on


@tagged('post_install', '-at_install', 'llm_reconcile_local')
class TestLlmReconcileLocal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Scanner = cls.env[local.CURSOR_MODEL].sudo()
        cls.Order = cls.env[ORDER_MODEL].sudo()
        cls.partner = cls.env['res.partner'].create({'name': 'Owned local reconciliation'})
        cls.other = cls.env['res.partner'].create({'name': 'Owned mismatch partner'})
        cls.account = _llm_internal(cls.env['woow_paas_platform.llm_account']).create({
            'partner_id': cls.partner.id})
        _llm_internal(cls.account).write({'state': 'active', 'newapi_user_id': 121204})
        cls.operator = new_test_user(
            cls.env, login='reconcile_%s' % uuid.uuid4().hex,
            email='reconcile-fixture@example.com', groups='base.group_user,base.group_system')
        cls.operator._llm_set_named_human_eligible(True)
        company = cls.env.company.sudo()
        bank = cls.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1)
        if not bank:
            bank = cls.env['account.journal'].sudo().create({
                'name': 'Reconciliation Bank', 'type': 'bank', 'code': 'RC12',
                'company_id': company.id})
        company.sh_paid_subscription_journal = bank
        if not cls.env['account.journal'].sudo().search([
                ('company_id', '=', company.id), ('type', '=', 'sale')], limit=1):
            cls.env['account.journal'].sudo().create({
                'name': 'Reconciliation Sales', 'type': 'sale', 'code': 'RS12',
                'company_id': company.id})
        cls.partner.add_wallet_points(5000, description='Owned reconciliation fixture')

    def setUp(self):
        super().setUp()
        self.now = datetime(2031, 6, 1, 15, 30)
        self._patch(local, 'llm_unconfigured', return_value=False)
        self.incident = self._patch(
            type(self.env['woow_paas_platform.llm_event']), '_llm_incident', return_value=101)
        self.alert = self._patch(
            type(self.env['woow_paas_platform.llm_event']), '_llm_alert', return_value=102)
        self._patch(type(self.account), '_llm_client',
                    side_effect=AssertionError('Local scanner must not create a client'))
        self._patch(type(self.env['mail.mail']), 'send',
                    side_effect=AssertionError('Local tests must never send mail'))
        self._patch(type(self.env['woow_paas_platform.llm_runtime_state']), '_llm_read',
                    return_value={'dr_mode': True})
        self.env.cr.execute('SHOW statement_timeout')
        self.statement_timeout = self.env.cr.fetchone()[0]
        self.env.cr.execute('SHOW lock_timeout')
        self.lock_timeout = self.env.cr.fetchone()[0]

    def _patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _restore_timeouts(self):
        self.env.cr.execute('SET LOCAL statement_timeout = %s', (self.statement_timeout,))
        self.env.cr.execute('SET LOCAL lock_timeout = %s', (self.lock_timeout,))

    def _tick(self, items=local.SCAN_ROWS, seconds=30):
        return LlmCronTick(seconds, items, monotonic=lambda: 0)

    def _manual(self, **overrides):
        values = {
            'account_id': self.account.id, 'partner_id': self.partner.id,
            'newapi_user_id': self.account.newapi_user_id, 'source': 'manual',
            'points': 100, 'quota': 1562500, 'client_request_id': str(uuid.uuid4()),
            'reason': 'Owned adjustment reason, never in an alert',
            'requested_by_uid': self.operator.id,
        }
        values.update(overrides)
        order = _credit_internal(self.Order).create(values)
        self._raw(order, create_date=self.now)
        return order

    def _pay(self):
        order = self.Order._llm_create_paid_order(self.account, 100, str(uuid.uuid4()))
        self._raw(order, create_date=self.now)
        return order

    def _raw(self, record, **values):
        """Only owned fixture corruption; never drop/alter production constraints."""
        self.env.flush_all()
        assignments = ', '.join('"%s" = %%s' % name for name in values)
        self.env.cr.execute(
            'UPDATE ' + record._table + ' SET ' + assignments + ' WHERE id = %s',
            [*values.values(), record.id])
        record.invalidate_recordset()

    def _state(self, order, state):
        if state in ('credited', 'credit_lost', 'unknown'):
            _credit_internal(order)._transition('sending')
            _credit_internal(order)._transition('unknown' if state == 'unknown' else 'credited')
            if state == 'credit_lost':
                _credit_internal(order)._transition(state)
        elif state != 'paid':
            _credit_internal(order)._transition(state)
        order.flush_recordset()
        return order

    def _row(self, order, task='i4'):
        self.env.flush_all()
        try:
            return self.Scanner._next_order(self._tick(), task, 0, order.id, [order.id])
        finally:
            self._restore_timeouts()

    def _run(self, orders, *, items=local.SCAN_ROWS, now=None, tick=None):
        self.env.flush_all()
        try:
            return self.Scanner._process_in_env(
                tick or self._tick(items), order_ids=orders.ids, now=now or self.now)
        finally:
            self._restore_timeouts()

    def _cursor(self, orders, task):
        key = local._task_keys(sorted(orders.ids))[0 if task == 'i1' else 1]
        self.Scanner.invalidate_model()
        return self.Scanner.search([('task_key', '=', key)])

    def _void(self):
        order = self._pay()
        order._llm_void_wallet()
        self.env.flush_all()
        return order

    def _recharge(self, origin):
        # The production recharge does remote evidence BEFORE these local
        # accounting steps. Here only its local ledger shape is under test.
        order = self._manual(
            source='recharge', recharge_of_order_id=origin.id, state='credited')
        invoice = origin.invoice_id.copy({
            'woow_llm_credit_order_id': order.id,
            'woow_invoice_issue_state': 'not_required', 'woow_skip_upstream_hook': True})
        floor = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner.id)], order='id desc', limit=1).id
        self.assertTrue(invoice._pay_from_wallet())
        debit = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner.id), ('id', '>', floor),
            ('transaction_type', '=', 'deduct')])
        self.assertEqual(len(debit), 1)
        _credit_internal(order).write({
            'invoice_id': invoice.id, 'wallet_transaction_id': debit.id})
        return order

    def test_real_paid_and_voided_documents_are_not_mismatches(self):
        paid, voided = self._pay(), self._void()
        for order in (paid, voided):
            self.assertIsNone(local._i4_code(self._row(order)))
        self.assertEqual(voided.invoice_id.payment_state, 'paid')
        self.assertEqual(voided.void_credit_note_id.payment_state, 'paid')
        self.assertEqual(voided.void_credit_note_id.reversed_entry_id, voided.invoice_id)

    def test_manual_cancelled_dr_and_recharge_valid_sources(self):
        manual = self._manual()
        cancelled = self._state(self._manual(), 'cancelled')
        lost = self._state(self._pay(), 'credit_lost')
        # A lost original does NOT require an already-created DR replacement.
        self.assertIsNone(local._i4_code(self._row(lost)))
        dr = self._manual(source='dr', dr_origin_order_id=lost.id)
        recharge = self._recharge(self._void())
        for order in (manual, cancelled, dr, recharge):
            with self.subTest(source=order.source, state=order.state):
                self.assertIsNone(local._i4_code(self._row(order)))

    def test_user_all_nonvoid_states_keep_original_settled_ledger(self):
        for state in ('paid', 'sending', 'unknown', 'needs_review', 'void_failed',
                      'credited', 'credit_lost'):
            with self.subTest(state=state), self.env.cr.savepoint():
                order = self._state(self._pay(), state)
                self.assertIsNone(local._i4_code(self._row(order)))
                # Release inflight uniqueness before the next owned fixture.
                if state in ('sending', 'unknown', 'needs_review'):
                    _credit_internal(order)._transition('credited')

    def test_wallet_debit_amount_type_partner_and_missing_link(self):
        order = self._pay()
        debit = order.wallet_transaction_id
        for changes in ({'amount': -99}, {'transaction_type': 'refund'},
                        {'partner_id': self.other.id}):
            with self.subTest(changes=changes):
                before = {key: debit[key].id if key == 'partner_id' else debit[key]
                          for key in changes}
                self._raw(debit, **changes)
                self.assertEqual(local._i4_code(self._row(order)), local.I4_DEBIT)
                self._raw(debit, **before)
        self._raw(order, wallet_transaction_id=None)
        self.assertEqual(local._i4_code(self._row(order)), local.I4_DEBIT)

    def test_invoice_amount_tax_partner_backlink_and_settlement(self):
        order = self._pay()
        invoice = order.invoice_id
        for changes in (
                {'amount_total': 99}, {'amount_tax': 1}, {'amount_residual': 1},
                {'partner_id': self.other.id}, {'woow_llm_credit_order_id': None},
                {'state': 'draft'}, {'payment_state': 'not_paid'}):
            with self.subTest(changes=changes):
                before = {key: invoice[key].id if key in (
                    'partner_id', 'woow_llm_credit_order_id') else invoice[key]
                          for key in changes}
                self._raw(invoice, **changes)
                self.assertEqual(local._i4_code(self._row(order)), local.I4_INVOICE)
                self._raw(invoice, **before)

    def test_zero_total_tax_does_not_hide_line_tax(self):
        order = self._pay()
        tax = self.env['account.tax'].create({
            'name': 'Owned zero tax', 'amount': 0, 'type_tax_use': 'sale',
            'company_id': self.env.company.id})
        line = order.invoice_id.invoice_line_ids.filtered(
            lambda value: value.display_type == 'product')
        relation = line._fields['tax_ids']
        self.env.cr.execute(
            f'INSERT INTO {relation.relation} ({relation.column1}, {relation.column2}) '
            'VALUES (%s, %s)', (line.id, tax.id))
        self.assertEqual(local._i4_code(self._row(order)), local.I4_INVOICE)

    def test_refund_and_credit_note_corruption(self):
        order = self._void()
        self._raw(order.void_wallet_transaction_id, amount=99)
        self.assertEqual(local._i4_code(self._row(order)), local.I4_REFUND)
        self._raw(order.void_wallet_transaction_id, amount=100)
        note = order.void_credit_note_id
        for changes in ({'amount_total': 99}, {'amount_tax': 1},
                        {'reversed_entry_id': None}, {'partner_id': self.other.id},
                        {'woow_llm_credit_order_id': None}, {'payment_state': 'not_paid'}):
            with self.subTest(changes=changes):
                before = {key: note[key].id if key in (
                    'partner_id', 'woow_llm_credit_order_id', 'reversed_entry_id') else note[key]
                          for key in changes}
                self._raw(note, **changes)
                self.assertEqual(local._i4_code(self._row(order)), local.I4_NOTE)
                self._raw(note, **before)

    def test_cancelled_and_internal_orders_detect_unlinked_vouchers(self):
        cancelled = self._state(self._manual(), 'cancelled')
        paid = self._pay()
        # Corruption may only be on the accounting backlink. Order's reversal
        # fields remain empty (and their existing DB checks remain enabled).
        note = paid.invoice_id.copy({
            'move_type': 'out_refund', 'woow_llm_credit_order_id': cancelled.id})
        self.assertFalse(cancelled.void_credit_note_id)
        self.assertEqual(local._i4_code(self._row(cancelled)), local.I4_CANCELLED)
        manual = self._manual()
        self._raw(note, woow_llm_credit_order_id=manual.id)
        self.assertEqual(local._i4_code(self._row(manual)), local.I4_INTERNAL)
        self._raw(note, woow_llm_credit_order_id=paid.id)
        self.assertEqual(local._i4_code(self._row(paid)), local.I4_REVERSAL)

    def test_dr_reason_and_origin_are_checked_from_database(self):
        lost = self._state(self._pay(), 'credit_lost')
        dr = self._manual(source='dr', dr_origin_order_id=lost.id)
        self._raw(dr, reason='   ')
        self.assertEqual(local._i4_code(self._row(dr)), local.I4_REASON)
        self._raw(dr, reason='Restored owned adjustment reason')
        self._raw(lost, state='credited')
        self.assertEqual(local._i4_code(self._row(dr)), local.I4_DR)
        self._raw(lost, state='credit_lost')
        spare = self._manual()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
            self._raw(spare, source='dr', dr_origin_order_id=lost.id)
        row = self._row(dr)
        self.assertEqual(row['dr_count'], 1)
        self.assertEqual(local._i4_code(dict(row, dr_count=2)), local.I4_DR)

    def test_order_partner_and_cross_account_origins(self):
        lost = self._state(self._pay(), 'credit_lost')
        dr = self._manual(source='dr', dr_origin_order_id=lost.id)
        other_account = _llm_internal(self.env['woow_paas_platform.llm_account']).create({
            'partner_id': self.other.id})
        self._raw(lost, account_id=other_account.id)
        self.assertEqual(local._i4_code(self._row(dr)), local.I4_DR)
        self._raw(dr, partner_id=self.other.id)
        self.assertEqual(local._i4_code(self._row(dr)), local.I4_PARTNER)

    def test_recharge_origin_state_and_uniqueness_backstop(self):
        origin = self._void()
        recharge = self._recharge(origin)
        self._raw(origin, state='paid')
        self.assertEqual(local._i4_code(self._row(recharge)), local.I4_RECHARGE)
        self._raw(origin, state='voided')
        # SQL checks/partial unique indexes are reused, never disabled for tests.
        spare = self._pay()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
            self._raw(spare, source='recharge', recharge_of_order_id=origin.id)
        row = self._row(recharge)
        self.assertEqual(row['recharge_count'], 1)
        self.assertIsNone(local._i4_code(row))
        # Pure defense against legacy corruption unreachable through today's
        # unique index; the preceding assertion exercises the actual SQL count.
        self.assertEqual(local._i4_code(dict(row, recharge_count=2)), local.I4_RECHARGE)
        other_account = _llm_internal(self.env['woow_paas_platform.llm_account']).create({
            'partner_id': self.other.id})
        self._raw(origin, account_id=other_account.id)
        self.assertEqual(local._i4_code(self._row(recharge)), local.I4_RECHARGE)

    def test_i1_age_lease_attempt_boundaries_and_terminal_exclusion(self):
        order = self._manual()
        for age, attempts, expected in (
                (900, 3, None), (901, 3, local.I1_PAID), (1, 4, local.I1_ATTEMPTS)):
            self._raw(order, create_date=self.now - timedelta(seconds=age), attempt=attempts)
            self.assertEqual(local._i1_code(self._row(order, 'i1'), self.now), expected)
        self._state(order, 'sending')
        for lease, expected in (
                (None, local.I1_LEASE), (self.now, local.I1_LEASE),
                (self.now + timedelta(seconds=1), None)):
            self._raw(order, lease_until=lease, attempt=3)
            self.assertEqual(local._i1_code(self._row(order, 'i1'), self.now), expected)
        _credit_internal(order)._transition('credited', attempt=6)
        self.assertIsNone(self._row(order, 'i1'))
        self.assertIsNone(local._i1_code(self._row(order), self.now))

    def test_i1_review_states_and_all_terminal_attempts(self):
        for state, expected in (
                ('unknown', local.I1_UNKNOWN), ('needs_review', local.I1_REVIEW),
                ('void_failed', local.I1_VOID), ('credited', None), ('credit_lost', None)):
            order = self._state(self._pay(), state)
            self._raw(order, attempt=6)
            row = self._row(order, 'i1')
            if expected:
                self.assertEqual(local._i1_code(row, self.now), expected)
            else:
                self.assertIsNone(row)
                self.assertIsNone(local._i1_code(self._row(order), self.now))
            if state in ('unknown', 'needs_review'):
                _credit_internal(order)._transition('credited')
        for order in (self._state(self._manual(), 'cancelled'), self._void()):
            self._raw(order, attempt=6)
            self.assertIsNone(self._row(order, 'i1'))
            self.assertIsNone(local._i1_code(self._row(order), self.now))

    def test_i1_terminal_history_does_not_consume_scan_budget(self):
        history = self._state(self._manual(), 'cancelled')
        history |= self._state(self._manual(), 'credited')
        history |= self._state(self._manual(), 'credit_lost')
        pending = self._manual()
        orders = history | pending
        tick = self._tick(items=1)
        self._run(orders, tick=tick)
        self.assertEqual(tick.done, 1)
        self.assertEqual(self._cursor(orders, 'i1').after_id, pending.id)
        self.incident.assert_not_called()

    def test_scoped_keyset_progress_and_fair_turns_survive_cache_invalidation(self):
        orders = self._manual() | self._manual() | self._manual()
        self.assertTrue(self._run(orders, items=1))
        self.assertEqual(self._cursor(orders, 'i1').after_id, orders[0].id)
        self.assertFalse(self._cursor(orders, 'i4').started_at)
        self.env.invalidate_all()
        self.assertTrue(self._run(orders, items=1))
        self.assertEqual(self._cursor(orders, 'i4').after_id, orders[0].id)
        for _ in range(8):
            if not self._run(orders, items=1):
                break
        self.assertTrue(self._cursor(orders, 'i1').finished_at)
        self.assertTrue(self._cursor(orders, 'i4').finished_at)
        self.assertEqual(self._cursor(orders, 'i1').after_id, max(orders.ids))
        self.incident.assert_not_called()
        self.alert.assert_not_called()

    def test_unfinished_period_is_completed_before_next_hour_and_taipei_day(self):
        orders = self._manual() | self._manual()
        self._run(orders, items=1)
        first = self._cursor(orders, 'i1').period_key
        later = self.now + timedelta(hours=1)  # Taipei midnight crossed.
        self._run(orders, items=1, now=later)  # I4 starts the new Taipei day.
        self._run(orders, items=1, now=later)  # I1 continues its OLD period.
        self.assertEqual(self._cursor(orders, 'i1').period_key, first)
        self.assertEqual(self._cursor(orders, 'i1').after_id, orders[-1].id)
        self.assertEqual(self._cursor(orders, 'i4').period_key, '20310602')
        for _ in range(10):
            self._run(orders, now=later)
        self.assertEqual(self._cursor(orders, 'i1').period_key, '2031060116')

    def test_failed_sink_does_not_advance_and_cannot_starve_other_task(self):
        order = self._manual()
        self._raw(order, create_date=self.now - timedelta(minutes=16))
        self.alert.return_value = False
        self.assertTrue(self._run(order))
        self.assertEqual(self._cursor(order, 'i1').after_id, 0)
        self.assertFalse(self._cursor(order, 'i1').finished_at)
        self.assertEqual(self.incident.call_count, 1)
        self.assertTrue(self._run(order))  # Healthy I4 gets its turn.
        self.assertTrue(self._cursor(order, 'i4').finished_at)
        self.alert.return_value = 102
        self.assertTrue(self._run(order))
        self.assertEqual(self._cursor(order, 'i1').after_id, order.id)
        self.assertEqual(self.incident.call_count, 2)
        self.assertEqual(self.alert.call_count, 2)

    def test_i4_worker_reports_corruption_without_repairing_any_ledger(self):
        order = self._pay()
        self._raw(order.wallet_transaction_id, amount=-99)
        before = self._row(order)
        self._run(order)  # I1 is healthy.
        self.incident.assert_not_called()
        self._run(order)  # Actual SQL I4 scan and actual notification adapter.
        self.assertEqual(self.alert.call_args.kwargs['error_code'], local.I4_DEBIT)
        self.assertEqual(self.incident.call_args.kwargs['order_id'], order.id)
        self.assertEqual(self._row(order), before)
        self.assertEqual(order.state, 'paid')
        self.assertEqual(order.wallet_transaction_id.amount, -99)

    def test_incident_false_or_exception_never_accepts_row(self):
        for failure in (False, RuntimeError('private reason must not be logged')):
            with self.subTest(failure=type(failure).__name__):
                order = self._manual()
                self._raw(order, create_date=self.now - timedelta(minutes=16))
                self.incident.reset_mock()
                self.incident.return_value = False
                self.incident.side_effect = failure if isinstance(failure, Exception) else None
                if isinstance(failure, Exception):
                    with self.assertLogs(local.__name__, level=logging.ERROR) as logs:
                        self.assertTrue(self._run(order))
                    self.assertNotIn(str(failure), '\n'.join(logs.output))
                else:
                    self.assertTrue(self._run(order))
                self.assertEqual(self._cursor(order, 'i1').after_id, 0)
        self.alert.assert_not_called()

    def test_one_finding_per_tick_and_safe_payload_registry(self):
        orders = self._manual() | self._manual()
        for order in orders:
            self._raw(order, create_date=self.now - timedelta(minutes=16))
        self._run(orders)
        self.incident.assert_called_once()
        self.alert.assert_called_once()
        self.assertEqual(self.incident.call_args.args, (LlmIncident.RECONCILE_MISMATCH,))
        self.assertEqual(self.alert.call_args.args, (LlmAlert.RECONCILE_MISMATCH,))
        payload = self.alert.call_args.kwargs
        self.assertEqual(payload['deadline'], 30 - local.SINK_TAIL_RESERVE)
        self.assertEqual(self.incident.call_args.kwargs['deadline'], payload['deadline'])
        self.assertEqual(payload['target_id'], orders[0].id)
        self.assertEqual(payload['numbers'], {'age_seconds': 960, 'attempts': 0, 'points': 100})
        self.assertNotIn(orders[0].reason, repr(payload))
        self.assertLessEqual(local.RECONCILE_CODES, llm_common.ERROR_CODES)
        self.assertLessEqual(set(payload['numbers']), llm_common.NUMBER_KEYS)

    def test_budget_exhaustion_and_sink_reserve_keep_row_pending(self):
        order = self._manual()
        self._raw(order, create_date=self.now - timedelta(minutes=16))
        tick = self._tick(seconds=local.SINK_RESERVE)
        self.assertTrue(self._run(order, tick=tick))
        self.assertFalse(self._cursor(order, 'i1'))
        self.incident.assert_not_called()
        tick = self._tick()

        def slow_incident(*args, **kwargs):
            tick.deadline = local.ALERT_RESERVE - 1
            return 101

        self.incident.side_effect = slow_incident
        self.assertTrue(self._run(order, tick=tick))
        self.assertEqual(self._cursor(order, 'i1').after_id, 0)
        self.alert.assert_not_called()
        self.assertGreater(tick.delay, 0)

    def test_empty_and_foreign_scopes_do_not_touch_global_cursors(self):
        owned, foreign = self._manual(), self._manual()
        global_rows = self.Scanner.search([('task_key', 'in', ['i1', 'i4'])])
        before = global_rows.read()
        self.assertFalse(self._run(self.Order.browse()))
        self.assertFalse(self.Scanner._process_batch(self._tick(), order_ids=[]))
        self._run(owned)
        # Foreign becomes bad after an earlier tick/precheck. Actual SQL must
        # still apply the scope on the next task, even with globally mocked sinks.
        self._raw(foreign, create_date=self.now - timedelta(hours=1))
        self._run(owned)
        self.incident.assert_not_called()
        self.assertEqual(global_rows.read(), before)
        self.assertEqual(self._cursor(owned, 'i1').upper_id, owned.id)
        self.assertEqual(self._cursor(owned, 'i4').upper_id, owned.id)

    def test_frozen_upper_bound_excludes_newer_rows_at_actual_sql_selection(self):
        first = self._manual()
        upper = first.id
        later = self._manual()
        self.env.flush_all()
        try:
            row = self.Scanner._next_order(self._tick(), 'i1', 0, upper, [first.id, later.id])
            self.assertEqual(row['id'], first.id)
            self.assertIsNone(self.Scanner._next_order(
                self._tick(), 'i1', first.id, upper, [first.id, later.id]))
            self.assertIsNone(self.Scanner._next_order(
                self._tick(), 'i4', 0, later.id, []))
        finally:
            self._restore_timeouts()

    def test_cursor_rpc_sudo_and_forged_context_cannot_write(self):
        order = self._manual()
        self._run(order)
        cursor = self._cursor(order, 'i1')
        for value in (True, 1, 'internal', {'enabled': True}):
            model = self.Scanner.with_context(
                woow_llm_internal_write=value, module_uninstall=True,
                default_task='i1', default_task_key='i1')
            with self.assertRaises(AccessError):
                model.create({})
            with self.assertRaises(AccessError):
                cursor.with_env(model.env).write({'after_id': order.id})
            with self.assertRaises(AccessError):
                cursor.with_env(model.env).unlink()

    def test_real_unconfigured_guard_makes_cron_and_worker_noop(self):
        order = self._manual()
        count = self.Scanner.search_count([])
        config = {'proxy_state': 'missing'}
        with patch.object(local, 'llm_unconfigured', llm_common.llm_unconfigured), \
                patch.object(local, 'llm_config', return_value=config), \
                patch.object(llm_common, 'llm_config', return_value=config):
            self.assertFalse(self.Scanner._cron_llm_reconcile_local())
            self.assertFalse(self.Scanner._process_batch(self._tick(), order_ids=order.ids))
            self.assertFalse(self._run(order))
        self.assertEqual(self.Scanner.search_count([]), count)
        self.incident.assert_not_called()
        self.alert.assert_not_called()


@tagged('post_install', '-at_install', 'llm_reconcile_local')
class TestLlmReconcileDeadline(TransactionCase):
    """Real elapsed SQL timeout and committed cursor progress, without HTTP."""

    @contextmanager
    def _owned_order(self):
        marker = 'llm_reconcile_deadline_' + uuid.uuid4().hex
        owned = None
        try:
            with self.registry._db.cursor() as cr:
                env = _env_on(self.registry, cr)
                partner = env['res.partner'].create({'name': marker})
                account = _llm_internal(env['woow_paas_platform.llm_account']).create({
                    'partner_id': partner.id, 'state': 'closed', 'newapi_user_id': partner.id})
                # No sendable order/account, voucher, key, or cron trigger.
                # I1 reports needs_review; I4 reports the deliberately absent
                # debit. This fixture needs no global config or named user.
                order = _credit_internal(env[ORDER_MODEL]).create({
                    'account_id': account.id, 'partner_id': partner.id,
                    'newapi_user_id': account.newapi_user_id, 'source': 'user',
                    'points': 1, 'quota': 15625, 'client_request_id': marker, 'reason': marker})
                _credit_internal(order)._transition('needs_review')
                env.flush_all()
                owned = (partner.id, account.id, order.id)
                keys = list(local._task_keys([order.id]))
                cr.execute('SELECT id FROM ' + local.CURSOR_TABLE + ' WHERE task_key = ANY(%s)',
                           (keys,))
                self.assertFalse(cr.fetchall())
                cr.commit()
            yield owned[2], keys
        finally:
            if owned:
                partner_id, account_id, order_id = owned
                with self.registry._db.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = '2s'")
                    cr.execute("SET LOCAL statement_timeout = '5s'")
                    cr.execute('SELECT name FROM res_partner WHERE id = %s', (partner_id,))
                    partner = cr.fetchone()
                    cr.execute(
                        'SELECT partner_id, state FROM woow_paas_platform_llm_account WHERE id = %s',
                        (account_id,))
                    account = cr.fetchone()
                    cr.execute(
                        'SELECT account_id, partner_id, source, state, client_request_id, reason, '
                        'invoice_id, wallet_transaction_id, void_credit_note_id, '
                        'void_wallet_transaction_id, incident_id FROM ' + ORDER_TABLE
                        + ' WHERE id = %s', (order_id,))
                    order = cr.fetchone()
                    if partner or account or order:
                        message = 'Owned deadline fixture retained: ' + marker
                        self.assertEqual(partner, (marker,), message)
                        self.assertEqual(account, (partner_id, 'closed'), message)
                        self.assertEqual(order, (
                            account_id, partner_id, 'user', 'needs_review', marker, marker,
                            None, None, None, None, None), message)
                        cr.execute(
                            'SELECT id FROM woow_paas_platform_llm_event '
                            'WHERE account_id = %s OR (target_model = %s AND target_id = %s)',
                            (account_id, ORDER_MODEL, order_id))
                        self.assertFalse(cr.fetchall(), message)
                        # Exact private keys only. Foreign FK references abort
                        # the entire cleanup transaction; never broaden deletes.
                        cr.execute('DELETE FROM ' + local.CURSOR_TABLE
                                   + ' WHERE task_key = ANY(%s)', (keys,))
                        cr.execute('DELETE FROM ' + ORDER_TABLE + ' WHERE id = %s', (order_id,))
                        cr.execute('DELETE FROM woow_paas_platform_llm_account WHERE id = %s',
                                   (account_id,))
                        _env_on(self.registry, cr)['res.partner'].browse(partner_id).unlink()
                        cr.commit()

    def _committed_progress(self, keys):
        with self.registry._db.cursor() as cr:
            cr.execute('SELECT task, turn, after_id, finished_at FROM ' + local.CURSOR_TABLE
                       + ' WHERE task_key = ANY(%s)', (keys,))
            return {row[0]: row[1:] for row in cr.fetchall()}

    def test_expired_sink_preserves_committed_turn_and_other_task_progress(self):
        # Main's shared helper is exercised, not a fake monotonic clock. Shrink
        # only sink admission thresholds; retain the real 2.5s scanner tail.
        from ..services.llm_sql_budget import deadline_cursor

        event_type = type(self.env['woow_paas_platform.llm_event'])
        with patch.object(local, 'llm_unconfigured', return_value=False), \
                patch.object(local, 'SINK_RESERVE', 0.25), \
                patch.object(local, 'ALERT_RESERVE', 0.1), \
                patch.object(self.registry, 'cursor', self.registry._db.cursor), \
                patch.object(event_type, '_llm_incident', return_value=101) as incident, \
                patch.object(event_type, '_llm_alert', return_value=102) as alert, \
                patch('requests.sessions.Session.request',
                      side_effect=AssertionError('Local reconciliation cannot make HTTP requests')):
            for blocked in (incident, alert):
                with self.subTest(sink='incident' if blocked is incident else 'alert'), \
                        self._owned_order() as (order_id, keys):
                    incident.reset_mock()
                    alert.reset_mock()
                    incident.side_effect = alert.side_effect = None
                    deadlines, expirations = [], []

                    def exhaust_deadline(*args, deadline, **kwargs):
                        deadlines.append(deadline)
                        try:
                            with deadline_cursor(self.registry._db.cursor, deadline) as cr:
                                cr.execute('SELECT pg_sleep(%s)',
                                           (max(0, deadline - time.monotonic()) + 1,))
                        except errors.QueryCanceled:
                            expirations.append(time.monotonic())
                            return False
                        return True  # Assertions below require a real SQL timeout.

                    blocked.side_effect = exhaust_deadline
                    tick = LlmCronTick(local.SINK_TAIL_RESERVE + 1.5, local.SCAN_ROWS)
                    scanner = self.env[local.CURSOR_MODEL]
                    self.assertTrue(scanner._process_batch(tick, order_ids=[order_id]))
                    self.assertEqual(len(expirations), 1, 'The real pg_sleep timeout must be reached')
                    # The helper reserves 50ms; this is actual elapsed time,
                    # not an immediately returned False or advanced fake clock.
                    self.assertGreaterEqual(expirations[0], deadlines[0] - 0.2)
                    first = self._committed_progress(keys)
                    self.assertEqual(set(first), {'i1', 'i4'},
                                     'Sink timeout must not roll back the sweep cursors')
                    self.assertGreater(first['i1'][0], 0)
                    self.assertEqual(first['i1'][1:], (0, None))
                    self.assertEqual(first['i4'], (0, 0, None))
                    self.assertEqual(deadlines, [tick.deadline - local.SINK_TAIL_RESERVE])
                    if blocked is incident:
                        alert.assert_not_called()
                    else:
                        self.assertEqual(incident.call_args.kwargs['deadline'], deadlines[0])
                    # A fresh worker must observe that committed turn and let
                    # I4 advance, while I1's unaccepted finding remains pending.
                    incident.side_effect = alert.side_effect = None
                    self.assertTrue(scanner._process_batch(
                        LlmCronTick(30, local.SCAN_ROWS), order_ids=[order_id]))
                    second = self._committed_progress(keys)
                    self.assertEqual(second['i1'], first['i1'])
                    self.assertGreater(second['i4'][0], first['i1'][0])
                    self.assertEqual(second['i4'][1], order_id)
                    self.assertEqual(alert.call_args.kwargs['error_code'], local.I4_DEBIT)
