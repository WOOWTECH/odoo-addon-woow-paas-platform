"""I1/I4 local, observational reconciliation (LLM-12b Task 2).

The only writes here are sweep progress and the existing incident/alert sinks.
Global sweeps use keys ``i1``/``i4``. Explicit private ``order_ids`` scopes use
separate, deterministic keys; they never claim or advance the global cursors.
All ledger observations for one order come from one SQL statement, including
accounting links. No ORM cache, order/account row locks, repair, or HTTP.

One tick claims one task, reads at most SCAN_ROWS orders, and delivers at most
one incident/alert pair. Durable turn counters alternate unfinished tasks even
when a sink keeps failing. Both independent sink transactions share an earlier
deadline, leaving time to commit the turn even when notification times out.
SINK_RESERVE also reserves their normal lock/cleanup allowances.
"""
import hashlib
import logging
from datetime import timedelta

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    _is_llm_internal, _without_llm_internal, llm_config, llm_unconfigured,
    run_llm_cron,
)
from .llm_credit_order import ORDER_MODEL, ORDER_TABLE
from .llm_event import LlmAlert, LlmIncident, _env_on

_logger = logging.getLogger(__name__)
CURSOR_MODEL = 'woow_paas_platform.llm_reconcile_local_cursor'
CURSOR_TABLE = 'woow_paas_platform_llm_reconcile_local_cursor'
CRON_XMLID = 'woow_paas_platform.ir_cron_llm_reconcile_local'
SCAN_ROWS = 200
SINK_RESERVE = 25.0
ALERT_RESERVE = 16.0
FINISH_RESERVE = 0.5
# Successful notification has two tail statements (progress + more), each
# capped at .5s by _execute. Add its .5s finish floor and 1s for timeout setup,
# commit and bounded cron continuation. A failed sink still commits its turn.
SINK_TAIL_RESERVE = 2.5
CONTINUE_DELAY = 60

# Main registers these closed codes in llm_common.ERROR_CODES. Reasons,
# descriptions, invoice names, and upstream evidence never enter a finding.
I1_PAID = 'llm_i1_paid_overdue'
I1_LEASE = 'llm_i1_sending_lease'
I1_UNKNOWN = 'llm_i1_unknown'
I1_REVIEW = 'llm_i1_needs_review'
I1_VOID = 'llm_i1_void_failed'
I1_ATTEMPTS = 'llm_i1_attempt_limit'
I4_PARTNER = 'llm_i4_order_partner'
I4_DEBIT = 'llm_i4_wallet_debit'
I4_INVOICE = 'llm_i4_invoice'
I4_REFUND = 'llm_i4_wallet_refund'
I4_NOTE = 'llm_i4_credit_note'
I4_INTERNAL = 'llm_i4_internal_links'
I4_REASON = 'llm_i4_missing_reason'
I4_CANCELLED = 'llm_i4_cancelled_refund'
I4_REVERSAL = 'llm_i4_unexpected_reversal'
I4_DR = 'llm_i4_dr_origin'
I4_RECHARGE = 'llm_i4_recharge_origin'
RECONCILE_CODES = frozenset({
    I1_PAID, I1_LEASE, I1_UNKNOWN, I1_REVIEW, I1_VOID, I1_ATTEMPTS,
    I4_PARTNER, I4_DEBIT, I4_INVOICE, I4_REFUND, I4_NOTE, I4_INTERNAL,
    I4_REASON, I4_CANCELLED, I4_REVERSAL, I4_DR, I4_RECHARGE,
})
_UNFINISHED = frozenset({
    'paid', 'sending', 'unknown', 'needs_review', 'void_failed',
})


def _scope_ids(order_ids):
    if order_ids is None:
        return None
    if not isinstance(order_ids, (list, tuple)) or any(
            type(value) is not int or value <= 0 for value in order_ids):
        raise ValidationError(_('Explicit reconciliation scope must contain order IDs.'))
    return sorted(set(order_ids))


def _task_keys(order_ids):
    """Pure private scope identity: a caller cannot supply another cursor key."""
    if order_ids is None:
        return ('i1', 'i4')
    digest = hashlib.sha256(','.join(map(str, order_ids)).encode('ascii')).hexdigest()
    return ('i1:' + digest, 'i4:' + digest)


def _periods(now):
    return {
        'i1': now.strftime('%Y%m%d%H'),
        'i4': (now + timedelta(hours=8)).strftime('%Y%m%d'),
    }


def _age(row, now):
    # create_date is immutable and applies to every state; write_date would
    # hide old stuck orders whenever an unrelated field is refreshed.
    return max(0, int((now - row['create_date']).total_seconds())) \
        if row['create_date'] else 0


def _i1_code(row, now):
    state = row['state']
    if state not in _UNFINISHED:
        return None
    if state == 'paid' and _age(row, now) > 15 * 60:
        return I1_PAID
    if state == 'sending' and (
            not row['lease_until'] or row['lease_until'] <= now):
        return I1_LEASE
    code = {'unknown': I1_UNKNOWN, 'needs_review': I1_REVIEW,
            'void_failed': I1_VOID}.get(state)
    return code or (I1_ATTEMPTS if row['attempt'] >= 4 else None)


def _wallet_ok(row, prefix, kind, amount):
    return (row[prefix + '_id'] is not None
            and row[prefix + '_partner_id'] == row['partner_id']
            and row[prefix + '_type'] == kind
            and row[prefix + '_amount'] == amount)


def _invoice_ok(row, prefix, kind):
    return (row[prefix + '_id'] is not None
            and row[prefix + '_partner_id'] == row['partner_id']
            and row[prefix + '_order_id'] == row['id']
            and row[prefix + '_type'] == kind
            and row[prefix + '_state'] == 'posted'
            and row[prefix + '_payment_state'] == 'paid'
            and row[prefix + '_residual'] == 0
            and row[prefix + '_total'] == row['points']
            and row[prefix + '_tax'] == 0
            and not row[prefix + '_has_taxes'])


def _i4_code(row):
    """First mismatch only; SQL still evaluates every relevant ledger fact."""
    source, state = row['source'], row['state']
    reversal = (row['void_credit_note_id'] or row['void_wallet_transaction_id']
                or row['linked_notes'])
    if state == 'cancelled' and reversal:
        return I4_CANCELLED
    if row['partner_id'] != row['account_partner_id']:
        return I4_PARTNER
    if source in ('manual', 'dr'):
        if (row['invoice_id'] or row['wallet_transaction_id']
                or reversal or row['linked_invoices']):
            return I4_INTERNAL
        if not row['has_reason']:
            return I4_REASON
        if source == 'dr' and (
                row['dr_account_id'] != row['account_id']
                or row['dr_partner_id'] != row['partner_id']
                or row['dr_state'] != 'credit_lost' or row['dr_count'] != 1
                or row['dr_points'] is None or row['points'] > row['dr_points']):
            return I4_DR
        return None
    # User void_failed still has an original settled debit/invoice; its failed
    # reversal must have rolled back. I1 separately reports the stuck state.
    if not _wallet_ok(row, 'debit', 'deduct', -row['points']):
        return I4_DEBIT
    if not _invoice_ok(row, 'invoice', 'out_invoice') or row['linked_invoices'] != 1:
        return I4_INVOICE
    if source == 'recharge' and (
            row['recharge_account_id'] != row['account_id']
            or row['recharge_partner_id'] != row['partner_id']
            or row['recharge_source'] != 'user' or row['recharge_state'] != 'voided'
            or row['recharge_count'] != 1 or row['recharge_points'] != row['points']):
        return I4_RECHARGE
    if state == 'voided':
        if not _wallet_ok(row, 'refund', 'refund', row['points']):
            return I4_REFUND
        # _llm_void_wallet independently settles both documents as PAID.
        # The original invoice does not become payment_state='reversed'.
        if (not _invoice_ok(row, 'note', 'out_refund')
                or row['note_reversed_id'] != row['invoice_id']
                or row['linked_notes'] != 1):
            return I4_NOTE
    elif reversal:
        return I4_REVERSAL
    return None


class _ScanDeadline(TimeoutError):
    pass


def _execute(cr, tick, query, params=()):
    """Every scanner SQL statement has a short, live remaining-time bound."""
    remaining_ms = int((tick.remaining() - FINISH_RESERVE) * 1000)
    if remaining_ms < 100:
        raise _ScanDeadline()
    # Reserve SET round trips too; never install PostgreSQL's unlimited zero.
    cr.execute('SET LOCAL lock_timeout = %s', ('100ms',))
    cr.execute('SET LOCAL statement_timeout = %s', ('%sms' % min(500, remaining_ms - 50),))
    if tick.remaining() <= FINISH_RESERVE:
        raise _ScanDeadline()
    cr.execute(query, params, log_exceptions=False)


class LlmReconcileLocalCursor(models.Model):
    _name = 'woow_paas_platform.llm_reconcile_local_cursor'
    _description = 'LLM local reconciliation sweep cursor'
    _rec_name = 'task_key'
    _order = 'task_key'

    task_key = fields.Char(required=True, readonly=True)
    task = fields.Selection([('i1', 'I1'), ('i4', 'I4')], required=True, readonly=True)
    period_key = fields.Char(readonly=True)
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)
    after_id = fields.Integer(default=0, readonly=True)
    upper_id = fields.Integer(default=0, readonly=True)
    turn = fields.Integer(default=0, readonly=True)

    _sql_constraints = [
        ('task_key_unique', 'UNIQUE(task_key)', 'A local reconciliation cursor already exists.'),
        ('progress_valid', 'CHECK(after_id >= 0 AND upper_id >= after_id AND turn >= 0)',
         'Invalid local reconciliation progress.'),
        ('task_key_valid',
         "CHECK(task IN ('i1','i4') AND (task_key = task OR "
         "task_key ~ ('^' || task || ':[0-9a-f]{64}$')))",
         'Invalid local reconciliation scope.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('Reconciliation cursors are managed by the scanner.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('Reconciliation cursors are managed by the scanner.'))
        return super().write(vals)

    def unlink(self):
        if not _is_odoo_module_uninstall(self.env):
            raise AccessError(_('Reconciliation cursors cannot be deleted.'))
        return super().unlink()

    @api.model
    def _cron_llm_reconcile_local(self):
        config = llm_config(self.env)
        if llm_unconfigured(self.env, config):
            return False
        return run_llm_cron(
            self.env, CRON_XMLID, lambda _config, tick: self._process_batch(tick),
            config=config, outbound=False, items=SCAN_ROWS)

    @api.model
    def _process_batch(self, tick, order_ids=None):
        """Independent durable transaction; explicit scopes stay independent."""
        ids = _scope_ids(order_ids)
        if ids == [] or llm_unconfigured(self.env):
            return False
        tick.delay = CONTINUE_DELAY
        try:
            with self.env.registry.cursor() as cr:
                if not self.env.registry.in_test_mode():
                    cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                env = _env_on(self.env.registry, cr)
                result = env[CURSOR_MODEL]._process_in_env(tick, order_ids=ids)
                cr.commit()
                return result
        except (_ScanDeadline, psycopg2.errors.LockNotAvailable,
                psycopg2.errors.QueryCanceled, psycopg2.errors.SerializationFailure):
            # Cursor progress rolls back. No swallowed result is called success.
            tick.more = True
            return True

    @api.model
    def _process_in_env(self, tick, order_ids=None, *, now=None):
        """Actual worker core, also used by rollback-only TransactionCase tests.

        Callers own this transaction; production uses _process_batch. A scoped
        run uses the same SQL/locking/progress code as cron, but separate keys.
        """
        ids = _scope_ids(order_ids)
        if ids == [] or llm_unconfigured(self.env):
            return False
        tick.delay = CONTINUE_DELAY
        if not tick.can_take(SINK_RESERVE + 1):
            return True
        now = now or fields.Datetime.now()
        periods, keys = _periods(now), _task_keys(ids)
        cr = self.env.cr
        for task, key in zip(('i1', 'i4'), keys):
            _execute(cr, tick, f"""
                INSERT INTO {CURSOR_TABLE}
                    (task_key, task, after_id, upper_id, turn,
                     create_uid, write_uid, create_date, write_date)
                VALUES (%s, %s, 0, 0, 0, %s, %s, %s, %s)
                ON CONFLICT (task_key) DO NOTHING
            """, (key, task, SUPERUSER_ID, SUPERUSER_ID, now, now))
        _execute(cr, tick, f"""
            SELECT * FROM {CURSOR_TABLE}
            WHERE task_key = ANY(%s)
              AND (finished_at IS NULL
                   OR period_key <> CASE task WHEN 'i1' THEN %s ELSE %s END)
            ORDER BY turn, task_key
            LIMIT 1 FOR UPDATE SKIP LOCKED
        """, (list(keys), periods['i1'], periods['i4']))
        cursor = cr.dictfetchone()
        if not cursor:
            # A concurrent worker may own a due cursor; the normal ten-minute
            # cron will revisit it. No immediate trigger/spin for lock contention.
            return False
        # Only cursor rows are locked. Never lock accounts/orders before sinks.
        _execute(cr, tick, f"""
            UPDATE {CURSOR_TABLE}
            SET turn = (SELECT max(turn) + 1 FROM {CURSOR_TABLE}
                        WHERE task_key = ANY(%s)), write_date = %s, write_uid = %s
            WHERE id = %s
        """, (list(keys), now, SUPERUSER_ID, cursor['id']))
        if not cursor['started_at'] or cursor['finished_at']:
            scope_sql = 'WHERE id = ANY(%s)' if ids is not None else ''
            _execute(cr, tick, f'SELECT COALESCE(max(id), 0) FROM {ORDER_TABLE} {scope_sql}',
                     (ids,) if ids is not None else ())
            cursor.update(after_id=0, upper_id=cr.fetchone()[0],
                          period_key=periods[cursor['task']])
            _execute(cr, tick, f"""
                UPDATE {CURSOR_TABLE} SET after_id = 0, upper_id = %s,
                    period_key = %s, started_at = %s, finished_at = NULL WHERE id = %s
            """, (cursor['upper_id'], cursor['period_key'], now, cursor['id']))
        scanned = 0
        while scanned < SCAN_ROWS and tick.can_take(SINK_RESERVE + 1):
            row = self._next_order(
                tick, cursor['task'], cursor['after_id'], cursor['upper_id'], ids)
            if not row:
                self._save_progress(tick, cursor, now, finished=True)
                break
            tick.took()
            scanned += 1
            code = _i1_code(row, now) if cursor['task'] == 'i1' else _i4_code(row)
            if code and not self._notify(row, code, now, tick):
                # Preserve after_id strictly before the unaccepted finding.
                break
            cursor['after_id'] = row['id']
            self._save_progress(tick, cursor, now)
            if code:
                # Hard helper-call bound: one incident + one alert per tick.
                break
        _execute(cr, tick, f"""
            SELECT EXISTS(SELECT 1 FROM {CURSOR_TABLE}
                WHERE task_key = ANY(%s) AND (finished_at IS NULL
                  OR period_key <> CASE task WHEN 'i1' THEN %s ELSE %s END))
        """, (list(keys), periods['i1'], periods['i4']))
        more = cr.fetchone()[0]
        tick.more = tick.more or more
        self.invalidate_model()
        return more

    @api.model
    def _save_progress(self, tick, cursor, now, *, finished=False):
        _execute(self.env.cr, tick, f"""
            UPDATE {CURSOR_TABLE}
            SET after_id = %s, finished_at = %s, write_date = %s WHERE id = %s
        """, (cursor['after_id'], now if finished else None, now, cursor['id']))

    @api.model
    def _next_order(self, tick, task, after_id, upper_id, order_ids):
        """Select ONE row at the actual scoped keyset boundary, never OFFSET."""
        scope_sql = 'AND o.id = ANY(%s)' if order_ids is not None else ''
        params = (after_id, upper_id) + ((order_ids,) if order_ids is not None else ())
        state_sql = 'AND o.state = ANY(%s)' if task == 'i1' else ''
        if task == 'i1':
            params += (sorted(_UNFINISHED),)
        base = f"""
            SELECT o.id, o.account_id, o.partner_id, o.source, o.state, o.points,
                   o.create_date, o.lease_until, o.attempt
            FROM {ORDER_TABLE} o
            WHERE o.id > %s AND o.id <= %s {scope_sql} {state_sql}
            ORDER BY o.id LIMIT 1
        """
        if task == 'i1':
            _execute(self.env.cr, tick, base, params)
            return self.env.cr.dictfetchone()
        # Field metadata supplies the Odoo-owned relation/table identifiers.
        tax = self.env['account.move.line']._fields['tax_ids']
        tax_checks = ', '.join(
            f"""EXISTS(SELECT 1 FROM account_move_line line
                 JOIN {tax.relation} rel ON rel.{tax.column1} = line.id
                 WHERE line.move_id = {alias}.id) AS {prefix}_has_taxes"""
            for alias, prefix in (('i', 'invoice'), ('n', 'note')))
        invoices = ', '.join(
            f"""{alias}.id AS {prefix}_id, {alias}.partner_id AS {prefix}_partner_id,
                {alias}.woow_llm_credit_order_id AS {prefix}_order_id,
                {alias}.move_type AS {prefix}_type, {alias}.state AS {prefix}_state,
                {alias}.payment_state AS {prefix}_payment_state,
                {alias}.amount_residual AS {prefix}_residual,
                {alias}.amount_total AS {prefix}_total, {alias}.amount_tax AS {prefix}_tax"""
            for alias, prefix in (('i', 'invoice'), ('n', 'note')))
        _execute(self.env.cr, tick, f"""
            WITH candidate AS ({base})
            SELECT c.*, o.invoice_id, o.wallet_transaction_id,
                o.void_credit_note_id, o.void_wallet_transaction_id,
                NULLIF(btrim(o.reason), '') IS NOT NULL AS has_reason,
                a.partner_id AS account_partner_id,
                w.id AS debit_id, w.partner_id AS debit_partner_id,
                w.transaction_type AS debit_type, w.amount AS debit_amount,
                r.id AS refund_id, r.partner_id AS refund_partner_id,
                r.transaction_type AS refund_type, r.amount AS refund_amount,
                {invoices}, {tax_checks}, n.reversed_entry_id AS note_reversed_id,
                d.account_id AS dr_account_id, d.state AS dr_state,
                d.partner_id AS dr_partner_id, d.points AS dr_points,
                q.account_id AS recharge_account_id, q.state AS recharge_state,
                q.partner_id AS recharge_partner_id, q.points AS recharge_points,
                q.source AS recharge_source,
                (SELECT count(*) FROM (SELECT id FROM {ORDER_TABLE}
                    WHERE source = 'dr' AND dr_origin_order_id = o.dr_origin_order_id
                    LIMIT 2) siblings) AS dr_count,
                (SELECT count(*) FROM (SELECT id FROM {ORDER_TABLE}
                    WHERE source = 'recharge'
                      AND recharge_of_order_id = o.recharge_of_order_id
                    LIMIT 2) siblings) AS recharge_count,
                (SELECT count(*) FROM (SELECT id FROM account_move
                    WHERE woow_llm_credit_order_id = o.id AND move_type = 'out_invoice'
                    LIMIT 2) documents) AS linked_invoices,
                (SELECT count(*) FROM (SELECT id FROM account_move
                    WHERE woow_llm_credit_order_id = o.id AND move_type = 'out_refund'
                    LIMIT 2) documents) AS linked_notes
            FROM candidate c JOIN {ORDER_TABLE} o ON o.id = c.id
            LEFT JOIN woow_paas_platform_llm_account a ON a.id = o.account_id
            LEFT JOIN wallet_transaction w ON w.id = o.wallet_transaction_id
            LEFT JOIN wallet_transaction r ON r.id = o.void_wallet_transaction_id
            LEFT JOIN account_move i ON i.id = o.invoice_id
            LEFT JOIN account_move n ON n.id = o.void_credit_note_id
            LEFT JOIN {ORDER_TABLE} d ON d.id = o.dr_origin_order_id
            LEFT JOIN {ORDER_TABLE} q ON q.id = o.recharge_of_order_id
        """, params)
        return self.env.cr.dictfetchone()

    @api.model
    def _notify(self, row, code, now, tick):
        remaining = tick.remaining()
        if remaining < SINK_RESERVE or remaining <= SINK_TAIL_RESERVE:
            return False
        sink_deadline = tick.deadline - SINK_TAIL_RESERVE
        event = self.env['woow_paas_platform.llm_event']
        try:
            incident = event._llm_incident(
                LlmIncident.RECONCILE_MISMATCH, account_id=row['account_id'],
                order_id=row['id'], error_code=code, points=row['points'],
                deadline=sink_deadline)
            if not incident or tick.remaining() - SINK_TAIL_RESERVE < ALERT_RESERVE:
                return False
            return bool(event._llm_alert(
                LlmAlert.RECONCILE_MISMATCH, account_id=row['account_id'],
                target_model=ORDER_MODEL, target_id=row['id'], error_code=code,
                numbers={'age_seconds': _age(row, now), 'attempts': row['attempt'],
                         'points': row['points']}, deadline=sink_deadline))
        except Exception:  # noqa: BLE001 - retry the same row; never log tenant text
            _logger.error('[LLM] local reconciliation sink failed order=%s code=%s',
                          row['id'], code)
            return False
