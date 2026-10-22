"""LLM credit conversion ledger (DESIGN §2.4, §2.10; LLM-08 Task 1).

Only private platform services may mutate orders, including in a sudo environment.
Capabilities use object identity, never JSON-serializable context flags. Creation
may precede invoice/payment links inside T1's savepoint; the payment service must
complete those links before committing. Payment stays local and atomic; only
the background worker calls new-api.
"""
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services import llm_credit_evidence as credit_evidence
from ..services import llm_redact
from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    CONVERT_MAX_POINTS, CONVERT_MIN_POINTS, ERR_INSUFFICIENT_BALANCE,
    ERR_LLM_INTERNAL, ERR_LLM_INVALID_REQUEST, ERR_LLM_NO_REFUND_ACK_REQUIRED,
    ERR_LLM_ORDER_NOT_FOUND, ERR_LLM_PENDING_REVIEW, ERR_LLM_POINTS_OUT_OF_RANGE,
    ERR_LLM_RESERVE_CONFIRM_REQUIRED, ERR_LLM_RATE_LIMITED, LlmError, LlmQuotaInteger, QUOTA_PER_POINT,
    _without_llm_internal, llm_config, llm_unconfigured, run_llm_cron,
)
from .llm_credit_attempt import ATTEMPT_TABLE, _ISSUE_CAPABILITY, _issue_cr
from .llm_account import ACCOUNT_TABLE, balance_stamp
from .llm_event import EVENT_TABLE, LlmAlert, LlmIncident, _begin, _iso, _throttle_reserve_cr, safe_code

_logger = logging.getLogger(__name__)

ORDER_MODEL = 'woow_paas_platform.llm_credit_order'
ORDER_TABLE = 'woow_paas_platform_llm_credit_order'
CREDIT_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_credit'
CREDIT_VERIFY_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_credit_verify'
BALANCE_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_balance_refresh'
CREDIT_LEASE = timedelta(minutes=5)
CREDIT_VERIFY_LEASE = timedelta(minutes=5)
CREDIT_BACKOFF_MINUTES = (1, 2, 5, 15, 60)
CREDIT_RETRY_LIMIT = 6
CREDIT_CLAIM_SCAN_LIMIT = 20
CREDIT_VERIFY_LOG_PAGE_SIZE = 100
CREDIT_CLAIM_LOCK_BUDGET = 5.0  # _begin lock_timeout, shared with the tick deadline
CREDIT_CLAIM_FINISH_BUDGET = 0.5  # Savepoint release/rollback and transaction commit/rollback.
CREDIT_CLAIM_CONFIG_BUDGET = 0.3  # Initial allowance for both SET LOCAL round trips.
CREDIT_VOID_PENDING = 'llm_credit_void_pending'
CREDIT_BUDGET = 'llm_credit_budget'
CREDIT_RESERVATION_INVALID = 'llm_credit_reservation_invalid'
CREDIT_SNAPSHOT_MISMATCH = 'llm_credit_snapshot_mismatch'
CREDIT_SNAPSHOT_INVALID = 'llm_credit_snapshot_invalid'
CREDIT_RESPONSE_INVALID = 'llm_credit_response_invalid'
CREDIT_CLIENT_UNAVAILABLE = 'llm_credit_client_unavailable'
CREDIT_DEFINITE_UNRECOGNISED = 'llm_credit_definite_unrecognised'
CREDIT_EVIDENCE_UNAVAILABLE = 'llm_credit_evidence_unavailable'
CREDIT_EVIDENCE_INVALID = 'llm_credit_evidence_invalid'
CREDIT_EVIDENCE_CONFLICT = 'llm_credit_evidence_conflict'
CREDIT_EVIDENCE_MISMATCH = 'llm_credit_evidence_mismatch'
CREDIT_EVIDENCE_MISSING_SNAPSHOT = 'llm_credit_evidence_missing_snapshot'
CREDIT_EVIDENCE_RETRY_LIMIT = 'llm_credit_evidence_retry_limit'
_QUOTA_REJECTIONS = frozenset({
    'Quota change amount cannot be zero', 'Invalid parameters',
    'No permission to update users of same or higher permission level',
    'User does not exist', 'Wallet quota limit exceeded',
})
_WRITE_KEY = 'woow_llm_credit_write'
_TRANSITION_KEY = 'woow_llm_credit_transition'
_WRITE_CAPABILITY = object()
_TRANSITION_CAPABILITY = object()
_IMMUTABLE = frozenset({
    'name', 'account_id', 'partner_id', 'newapi_user_id', 'source', 'points', 'quota',
    'client_request_id', 'rate_r_snapshot', 'q_snapshot', 'reason', 'requested_by_uid',
    'dr_origin_order_id', 'recharge_of_order_id',
})
_STATES = (
    'paid', 'sending', 'credited', 'unknown', 'needs_review', 'voided',
    'cancelled', 'void_failed', 'credit_lost',
)
# Source-specific terminal restrictions are additionally checked in _check_source.
_TRANSITIONS = {
    'paid': frozenset({'sending', 'voided', 'cancelled', 'void_failed', 'needs_review'}),
    'sending': frozenset({'paid', 'credited', 'unknown', 'needs_review', 'voided',
                          'cancelled', 'void_failed'}),
    'unknown': frozenset({'paid', 'credited', 'needs_review'}),
    'needs_review': frozenset({'paid', 'credited', 'voided', 'cancelled', 'void_failed'}),
    'void_failed': frozenset({'voided', 'needs_review'}),
    'credited': frozenset({'credit_lost'}),
    'voided': frozenset(), 'cancelled': frozenset(), 'credit_lost': frozenset(),
}
_REQUEST_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
_INT8_MAX = 2 ** 63 - 1


# Full local owner check. A missing/mismatched ledger never authorizes a worker.
# Verifier alone may keep handling pre-upgrade orders with no ledger history.
def _attempt_guard(order, *, legacy=False):
    owned = (
        'EXISTS (SELECT 1 FROM ' + ATTEMPT_TABLE + ' attempt_owner WHERE '
        'attempt_owner.order_id = {o}.id AND attempt_owner.account_id = {o}.account_id '
        'AND attempt_owner.attempt_no = {o}.attempt '
        'AND attempt_owner.reservation_event_id = {o}.convert_event_id '
        'AND attempt_owner.target_user_id = {o}.newapi_user_id '
        'AND attempt_owner.quota = {o}.quota AND attempt_owner.attempt_uuid = %s)'
    ).format(o=order)
    if legacy:
        return ('(' + owned + ' OR (%s IS NULL AND NOT EXISTS (SELECT 1 FROM '
                + ATTEMPT_TABLE + ' legacy_owner WHERE legacy_owner.order_id = '
                + order + '.id)))')
    return owned


class _CreditReservationInvalid(ValueError):
    """The ledger has no trusted reservation authorizing a new send."""


class _CreditClaimDeadline(TimeoutError):
    """Unwind the whole provisional claim when its shared budget is exhausted."""


class _CreditClaimCursor:
    """Bound every claim statement, including SQL inside the N6 helper.

    lock_timeout is per lock acquisition, so it alone cannot bound a statement
    that waits for several locks. statement_timeout also caps their total.
    Savepoint rollback uses the underlying cursor and must remain possible
    after the deadline. Callers without a deadline retain the existing policy.
    """
    def __init__(self, cr, deadline):
        self._cr, self._deadline = cr, deadline

    def __getattr__(self, name):
        return getattr(self._cr, name)

    def check(self):
        if self._deadline is None:
            return None
        milliseconds = int((self._deadline - time.monotonic() - CREDIT_CLAIM_FINISH_BUDGET) * 1000)
        if milliseconds < 1:
            # PostgreSQL interprets zero timeouts as unlimited.
            raise _CreditClaimDeadline()
        return milliseconds

    def execute(self, *args, **kwargs):
        remaining = self.check()
        if remaining is not None:
            self._set_timeouts(remaining)
        result = self._cr.execute(*args, **kwargs)
        self.check()
        return result

    def _set_timeouts(self, remaining):
        setup_ms = int(CREDIT_CLAIM_CONFIG_BUDGET * 1000)
        for _retry in range(3):
            statement_ms = remaining - setup_ms
            if statement_ms < 1:
                raise _CreditClaimDeadline()
            self._cr.execute('SET LOCAL lock_timeout = %s',
                             ('%sms' % min(int(CREDIT_CLAIM_LOCK_BUDGET * 1000), statement_ms),))
            self._cr.execute('SET LOCAL statement_timeout = %s', ('%sms' % statement_ms,))
            actual = self.check()
            if statement_ms <= actual:
                # Validate AFTER both settings: no business SQL can use a stale
                # timeout that consumes the reserved transaction finish budget.
                return
            # A slow SET pair used more than its allowance. Retry with its
            # observed cost plus 50ms slack; never spin indefinitely or execute
            # business SQL while its installed timeout exceeds the live budget.
            setup_ms = max(setup_ms, remaining - actual + 50)
            remaining = actual
        raise _CreditClaimDeadline()


def _credit_internal(records):
    """Grant a capability only to the immediate private service call."""
    return records.with_context(**{_WRITE_KEY: _WRITE_CAPABILITY})


def _without_capabilities(records):
    context = {k: v for k, v in records.env.context.items()
               if k not in (_WRITE_KEY, _TRANSITION_KEY)}
    return records.with_env(records.env(context=context))


def _request_id(value):
    if not value:
        return False
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value) \
            or llm_redact.redact(value) != value:
        raise ValidationError(_('Invalid upstream request identifier.'))
    return value


def _evidence(value):
    """Closed, numeric evidence schema: no upstream blobs or tenant free text."""
    if not value:
        return False
    if not isinstance(value, dict) or set(value) - {'log', 'before', 'first', 'second'}:
        raise ValidationError(_('Invalid credit evidence.'))
    clean = {}
    numbers = {'quota', 'used_quota', 'request_count', 'target_user_id', 'admin_id', 'from', 'to'}
    for section, sample in value.items():
        if not isinstance(sample, dict) or set(sample) - numbers - {'request_id', 'sampled_at'}:
            raise ValidationError(_('Invalid credit evidence.'))
        clean[section] = {}
        for key, item in sample.items():
            if key in numbers:
                if type(item) is not int or not -_INT8_MAX - 1 <= item <= _INT8_MAX:
                    raise ValidationError(_('Invalid credit evidence number.'))
                clean[section][key] = item
            elif key == 'request_id':
                clean[section][key] = _request_id(item)
            else:
                try:
                    stamp = fields.Datetime.to_datetime(item)
                    if not stamp:
                        raise ValueError()
                    clean[section][key] = fields.Datetime.to_string(stamp)
                except (TypeError, ValueError):
                    raise ValidationError(_('Invalid credit evidence timestamp.')) from None
    return json.loads(llm_redact.redact(json.dumps(clean)))


class LlmCreditOrder(models.Model):
    _name = 'woow_paas_platform.llm_credit_order'
    _description = 'AI Credit Conversion Order'
    _order = 'id desc'

    name = fields.Char(required=True, readonly=True, copy=False, default='/')
    account_id = fields.Many2one('woow_paas_platform.llm_account', required=True,
                                 readonly=True, index=True, ondelete='restrict')
    partner_id = fields.Many2one('res.partner', required=True, readonly=True,
                                 index=True, ondelete='restrict')
    newapi_user_id = fields.Integer(required=True, readonly=True)
    source = fields.Selection(selection=[('user', 'User'), ('manual', 'Manual'),
                                         ('dr', 'Disaster Recovery'), ('recharge', 'Recharge')],
                              required=True, readonly=True)
    points = fields.Integer(required=True, readonly=True)
    quota = LlmQuotaInteger(required=True, readonly=True)
    rate_r_snapshot = fields.Integer(required=True, readonly=True, default=32)
    q_snapshot = fields.Integer(required=True, readonly=True, default=15625)
    client_request_id = fields.Char(required=True, readonly=True, copy=False)
    state = fields.Selection(selection=[(s, s.replace('_', ' ').title()) for s in _STATES],
                             required=True, readonly=True, default='paid', index=True, copy=False)
    invoice_id = fields.Many2one('account.move', readonly=True, ondelete='restrict', copy=False)
    wallet_transaction_id = fields.Many2one('wallet.transaction', readonly=True,
                                            ondelete='restrict', copy=False)
    void_credit_note_id = fields.Many2one('account.move', readonly=True,
                                         ondelete='restrict', copy=False)
    void_wallet_transaction_id = fields.Many2one('wallet.transaction', readonly=True,
                                                 ondelete='restrict', copy=False)
    voided_at = fields.Datetime(readonly=True, copy=False)
    attempt = fields.Integer(required=True, readonly=True, default=0, copy=False)
    next_attempt_at = fields.Datetime(readonly=True, index=True, copy=False)
    sent_at = fields.Datetime(readonly=True, copy=False)
    lease_until = fields.Datetime(readonly=True, copy=False)
    quota_before = LlmQuotaInteger(readonly=True, copy=False)
    used_quota_before = LlmQuotaInteger(readonly=True, copy=False)
    request_count_before = LlmQuotaInteger(readonly=True, copy=False)
    before_snapshot_present = fields.Boolean(readonly=True, default=False, copy=False)
    before_sampled_at = fields.Datetime(readonly=True, copy=False)
    # Separate verifier ownership survives ticks and invalidates stale worker results.
    verify_token = fields.Char(readonly=True, copy=False, groups='base.group_system')
    verify_lease_until = fields.Datetime(readonly=True, copy=False)
    verify_first_quota = LlmQuotaInteger(readonly=True, copy=False)
    verify_first_used_quota = LlmQuotaInteger(readonly=True, copy=False)
    verify_first_request_count = LlmQuotaInteger(readonly=True, copy=False)
    verify_first_sample_at = fields.Datetime(readonly=True, copy=False)
    verify_first_sample_present = fields.Boolean(readonly=True, default=False, copy=False)
    # A frozen log upper bound and page number make a large audit query restartable.
    # Page zero means no scan is in progress; the next verifier claim starts at page one.
    verify_log_page = fields.Integer(readonly=True, default=0, copy=False)
    verify_log_end_at = fields.Datetime(readonly=True, copy=False)
    newapi_request_id = fields.Char(readonly=True, copy=False, index=True)
    credited_at = fields.Datetime(readonly=True, copy=False)
    credit_evidence = fields.Selection(
        selection=[('response', 'Response'), ('log_request_id', 'Log Request ID'),
                   ('quiescent_delta', 'Quiescent Delta'),
                   ('quiescent_noop', 'Quiescent No-op'), ('manual', 'Manual')],
        readonly=True, copy=False)
    last_error_code = fields.Char(readonly=True, copy=False)
    evidence_json = fields.Json(readonly=True, copy=False)
    reason = fields.Text(readonly=True, copy=False)
    requested_by_uid = fields.Many2one('res.users', readonly=True, ondelete='restrict', copy=False)
    dr_origin_order_id = fields.Many2one(ORDER_MODEL, readonly=True, ondelete='restrict', copy=False)
    recharge_of_order_id = fields.Many2one(ORDER_MODEL, readonly=True, ondelete='restrict', copy=False)
    incident_id = fields.Many2one('woow.billing.incident', readonly=True,
                                  ondelete='restrict', copy=False)
    notified_state = fields.Selection(selection=[(s, s) for s in
                                                 ('unknown', 'credited', 'needs_review', 'voided',
                                                  'cancelled', 'void_failed', 'credit_lost')],
                                      readonly=True, copy=False)
    # Acceptance and terminal mail have separate durable lifecycles. LLM-11
    # consumes the former; notified_state remains terminal-only.
    acceptance_notice_pending = fields.Boolean(readonly=True, default=False, copy=False)
    acceptance_notice_retry_at = fields.Datetime(readonly=True, copy=False)
    state_notice_retry_at = fields.Datetime(readonly=True, copy=False)
    # The event belongs to this attempt, not the entire order lifetime. Worker
    # retries reserve a fresh event and retain prior audit rows unchanged.
    # Scalar audit reference, deliberately without a foreign key. The event
    # commits on another connection after T1's REPEATABLE READ snapshot:
    # even FK validation cannot see that new row. A relational field makes
    # real T1 commits fail while single-connection HttpCase still passes.
    convert_event_id = fields.Integer(readonly=True, index=True, copy=False,
                                      groups='base.group_system')

    _sql_constraints = [
        ('partner_client_unique', 'UNIQUE(partner_id, client_request_id)',
         'A conversion request may only be used once.'),
        ('points_range', 'CHECK(points BETWEEN 1 AND 10000)', 'Invalid conversion points.'),
        ('quota_exact', 'CHECK(quota = points::bigint * 15625::bigint)',
         'Quota must match the fixed conversion rate.'),
        ('fixed_rate', 'CHECK(rate_r_snapshot = 32 AND q_snapshot = 15625)',
         'The conversion rate is fixed.'),
        ('positive_newapi_id', 'CHECK(newapi_user_id > 0)', 'An upstream user is required.'),
        ('client_nonempty', "CHECK(char_length(btrim(client_request_id)) > 0)",
         'A request identifier is required.'),
        ('manual_requirements', "CHECK(source <> 'manual' OR (points <= 1000 AND reason IS NOT NULL "
         'AND char_length(btrim(reason)) >= 10 AND requested_by_uid IS NOT NULL))',
         'Manual credits require at most 1000 points, a reason and a named requester.'),
        ('origin_required', "CHECK((source = 'dr') = (dr_origin_order_id IS NOT NULL) "
         "AND (source = 'recharge') = (recharge_of_order_id IS NOT NULL))",
         'Recovery credits must link to their original order.'),
        ('attempt_range', 'CHECK(attempt BETWEEN 0 AND 6)', 'Invalid conversion attempt count.'),
        ('source_valid', "CHECK(source IN ('user','manual','dr','recharge'))", 'Invalid credit source.'),
        ('state_valid', "CHECK(state IN ('paid','sending','credited','unknown','needs_review',"
         "'voided','cancelled','void_failed','credit_lost'))", 'Invalid conversion state.'),
        ('source_links', "CHECK(source IN ('user','recharge') OR "
         '(invoice_id IS NULL AND wallet_transaction_id IS NULL))',
         'Non-wallet credits cannot link to wallet payments.'),
        ('void_source', "CHECK(source = 'user' OR (void_credit_note_id IS NULL "
         'AND void_wallet_transaction_id IS NULL AND voided_at IS NULL))',
         'Only user conversions may link to a reversal.'),
        ('source_state', "CHECK((state NOT IN ('voided','void_failed') OR source = 'user') "
         "AND (state <> 'cancelled' OR source IN ('manual','dr')) "
         "AND (source <> 'recharge' OR state IN ('paid','credited','credit_lost')))",
         'The conversion state is invalid for this source.'),
        ('before_presence', 'CHECK(NOT before_snapshot_present OR before_sampled_at IS NOT NULL)',
         'A before snapshot requires its sample time.'),
        ('verify_presence', 'CHECK(NOT verify_first_sample_present OR verify_first_sample_at IS NOT NULL)',
         'A verification sample requires its sample time.'),
        ('verify_lease', 'CHECK((verify_token IS NULL) = (verify_lease_until IS NULL))',
         'Verification ownership requires a token and lease.'),
        ('verify_log_page', 'CHECK(COALESCE(verify_log_page, 0) >= 0)',
         'A verification log page cannot be negative.'),
        ('verify_log_cursor', 'CHECK((COALESCE(verify_log_page, 0) = 0) = '
         '(verify_log_end_at IS NULL))', 'A verification log cursor requires its frozen end time.'),
    ]

    def init(self):
        # Upgrade the earlier development schema without retaining its FK.
        self.env.cr.execute('ALTER TABLE ' + ORDER_TABLE + ' DROP CONSTRAINT IF EXISTS '
                            'woow_paas_platform_llm_credit_order_convert_event_id_fkey')
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_credit_inflight_uniq ON '
            + ORDER_TABLE + " (account_id) WHERE state IN ('sending','unknown','needs_review')")
        for column, source in (('dr_origin_order_id', 'dr'), ('recharge_of_order_id', 'recharge')):
            self.env.cr.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_credit_%s_uniq ON %s (%s) '
                "WHERE source = '%s'" % (source, ORDER_TABLE, column, source))
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_credit_request_uniq ON '
            + ORDER_TABLE + " (newapi_request_id) WHERE newapi_request_id IS NOT NULL "
            "AND btrim(newapi_request_id) <> ''")

    @api.model
    def _llm_create_paid_order(self, account, points, client_request_id):
        """Create a user conversion and its fully settled voucher atomically.

        T1 owns eligibility, terms, replay lookup and account serialization;
        it must hold that lock before calling this private local-only seam.
        No commit, upstream call, cron trigger or throttle reservation belongs
        here. The returned order carries neither mutation capability.

        The ledger's initial state is paid. Its temporary unlinked row exists
        only inside this savepoint and cannot survive a failed payment; links
        are filled only after independently checking the actual wallet debit.
        """
        account.ensure_one()
        try:
            with self.env.cr.savepoint():
                if type(points) is not int or not 1 <= points <= 10000:
                    raise ValidationError(_('Invalid conversion points.'))
                account = account.with_env(self.env).sudo()
                partner = account.partner_id
                partner.invalidate_recordset(['wallet_balance'])
                if partner.wallet_balance < points:
                    error = LlmError(ERR_INSUFFICIENT_BALANCE)
                    error.not_executed = True
                    raise error
                order = _credit_internal(self.sudo()).create({
                    'account_id': account.id,
                    'partner_id': partner.id,
                    'newapi_user_id': account.newapi_user_id,
                    'source': 'user',
                    'points': points,
                    'quota': points * QUOTA_PER_POINT,
                    'rate_r_snapshot': 32,
                    'q_snapshot': QUOTA_PER_POINT,
                    'client_request_id': client_request_id,
                })
                product = self.env.ref('woow_paas_platform.product_llm_credit')
                company = self.env.company
                invoice = self.env['account.move'].sudo().with_context(
                    woow_force_issue=False).create({
                        'move_type': 'out_invoice',
                        'partner_id': partner.id,
                        'company_id': company.id,
                        'currency_id': company.currency_id.id,
                        'invoice_date': fields.Date.context_today(self),
                        'woow_llm_credit_order_id': order.id,
                        'woow_invoice_issue_state': 'not_required',
                        'woow_skip_upstream_hook': True,
                        'invoice_line_ids': [(0, 0, {
                            'product_id': product.id,
                            'name': product.display_name,
                            'quantity': points,
                            'price_unit': 1,
                            'discount': 0,
                            'tax_ids': [(5, 0, 0)],
                        })],
                    })
                Wallet = self.env['wallet.transaction'].sudo()
                floor = Wallet.search([], order='id desc', limit=1).id or 0
                if not invoice._pay_from_wallet():
                    raise UserError(_('Credit conversion payment failed.'))
                transactions = Wallet.search([
                    ('partner_id', '=', partner.id), ('id', '>', floor),
                    ('transaction_type', '=', 'deduct'),
                ])
                invoice.invalidate_recordset(['amount_tax', 'amount_total', 'payment_state'])
                if (len(transactions) != 1 or transactions.amount != -points
                        or invoice.amount_tax != 0 or invoice.amount_total != points
                        or invoice.payment_state != 'paid' or invoice.state != 'posted'
                        or invoice.invoice_line_ids.tax_ids
                        or not invoice.woow_skip_upstream_hook
                        or invoice.woow_invoice_issue_state != 'not_required'):
                    raise UserError(_('Credit conversion payment did not settle exactly.'))
                _credit_internal(order).write({
                    'invoice_id': invoice.id,
                    'wallet_transaction_id': transactions.id,
                })
            return order
        except UserError:
            # Catch outside the savepoint: even a late reconciliation/check
            # failure removes the voucher, payment, debit and order together.
            # OperationalError deliberately propagates to Odoo retrying().
            error = LlmError(ERR_LLM_INTERNAL)
            error.not_executed = True
            raise error from None

    def _llm_dto(self):
        """Only fields safe for the partner-scoped conversion history UI."""
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'created_at': _iso(self.create_date),
            'points': self.points,
            'state': self.state,
            'source': self.source,
            'note': None,
            'client_request_id': self.client_request_id,
            'poll_after_ms': 3000,
        }

    @api.model
    def _llm_orders_payload(self, partner, *, page=1, limit=20, client_request_id=None):
        """Read by caller partner even when its AI account has since closed."""
        if type(page) is not int or page < 1 or type(limit) is not int or not 1 <= limit <= 50:
            raise LlmError(ERR_LLM_INVALID_REQUEST)
        domain = [('partner_id', '=', partner.id)]
        if client_request_id is not None:
            domain.append(('client_request_id', '=', self._llm_client_request_id(client_request_id)))
        orders = self.sudo()
        total = orders.search_count(domain)
        rows = orders.search(domain, order='id desc', offset=(page - 1) * limit, limit=limit)
        return {'items': [row._llm_dto() for row in rows], 'total': total,
                'poll_after_ms': 3000}

    @api.model
    def _llm_order_payload(self, partner, order_id):
        if type(order_id) is not int or order_id < 1:
            raise LlmError(ERR_LLM_ORDER_NOT_FOUND)
        order = self.sudo().search([('id', '=', order_id), ('partner_id', '=', partner.id)], limit=1)
        if not order:
            raise LlmError(ERR_LLM_ORDER_NOT_FOUND)
        return order._llm_dto()

    @api.model
    def _llm_client_request_id(self, value):
        """A bounded opaque key; preserving bytes makes exact lookup unambiguous."""
        if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value) \
                or llm_redact.redact(value) != value:
            raise LlmError(ERR_LLM_INVALID_REQUEST)
        return value

    @api.model
    def _llm_credit_preview(self, partner, points):
        """Local-only preview; stale terms remain visible so the UI can offer acceptance."""
        self._llm_points(points)
        account = self.env['woow_paas_platform.llm_account']._llm_account_for_partner(partner)
        account._llm_require_active()
        owner = partner.sudo()
        owner.invalidate_recordset(['wallet_balance'])
        wallet = owner.wallet_balance
        balance_after = wallet - points
        needed = account._llm_reserve_needed(days=7)
        current = self.env['woow_paas_platform.llm_terms']._llm_current()
        quota_after = int(account.quota_cached or 0) + points * QUOTA_PER_POINT
        return {
            'wallet_balance': wallet,
            'balance_after': balance_after,
            'quota_after_points': quota_after // QUOTA_PER_POINT,
            'reserve': {'needed_7d': needed, 'short_by': max(needed - balance_after, 0)},
            'pending_review': account._llm_has_pending_review(),
            'tos': account._llm_tos_dto(current),
        }

    @api.model
    def _llm_points(self, points):
        if type(points) is not int or not CONVERT_MIN_POINTS <= points <= CONVERT_MAX_POINTS:
            raise LlmError(ERR_LLM_POINTS_OUT_OF_RANGE)
        return points

    @api.model
    def _llm_convert_preflight(self, account, partner, points, confirm_reserve,
                               accept_no_refund, session):
        """Check local state before terms and independent throttle reservation."""
        account._llm_require_active()
        self._llm_points(points)
        if accept_no_refund is not True:
            raise LlmError(ERR_LLM_NO_REFUND_ACK_REQUIRED)
        if account._llm_has_pending_review():
            raise LlmError(ERR_LLM_PENDING_REVIEW)
        account._llm_require_reauth(session, points)
        owner = partner.sudo()
        owner.invalidate_recordset(['wallet_balance'])
        balance_after = owner.wallet_balance - points
        if balance_after < 0:
            raise LlmError(ERR_INSUFFICIENT_BALANCE)
        needed = account._llm_reserve_needed(days=7)
        if balance_after < needed and confirm_reserve is not True:
            raise LlmError(ERR_LLM_RESERVE_CONFIRM_REQUIRED,
                           needed_7d=needed, short_by=needed - balance_after)
        # Foundation gate checks current published terms, orders_enabled and
        # runtime guard, all before the CT throttle reservation.
        account._llm_require_new_operation()

    @api.model
    def _llm_release_reservation_on_rollback(self, event_id):
        """Close an independently committed event if the enclosing request rolls back.

        The event helper opens and commits its own cursor. Odoo runs this hook
        after the original cursor's rollback, including retrying()'s 40001
        path; a successful commit clears the hook without running it.
        """
        Event = self.env['woow_paas_platform.llm_event']
        self.env.cr.postrollback.add(
            lambda: Event._llm_event_finish(event_id, 'not_executed'))

    @api.model
    def _llm_convert(self, partner, points, client_request_id, confirm_reserve,
                     accept_no_refund, user, session, real_ip):
        """T1: an idempotent local transaction with no upstream call."""
        client_request_id = self._llm_client_request_id(client_request_id)
        Order = self.sudo()
        domain = [('partner_id', '=', partner.id),
                  ('client_request_id', '=', client_request_id)]
        existing = Order.search(domain, limit=1)
        if existing:
            return {'order_id': existing.id, 'name': existing.name, 'state': existing.state}

        Account = self.env['woow_paas_platform.llm_account']
        account = Account._llm_account_for_partner(partner)
        if not account:
            # Historical orders are looked up above, before a closed account
            # can hide them. A new attempt gets the distinct closed error.
            account = Account.sudo().search([('partner_id', '=', partner.id),
                                             ('state', '=', 'closed')], limit=1)
        self._llm_convert_preflight(account, partner, points, confirm_reserve,
                                    accept_no_refund, session)

        Event = self.env['woow_paas_platform.llm_event']
        event_id = Event._llm_throttle_reserve(account, 'convert',
                                               uid=user.id, real_ip=real_ip,
                                               credit_t1_pending=True)
        # Savepoint success is not outer HTTP commit. A later rollback must
        # release this independent reservation, even after we return.
        self._llm_release_reservation_on_rollback(event_id)
        try:
            with self.env.cr.savepoint():
                # NO KEY UPDATE remains compatible with the independent
                # event cursor's FK KEY SHARE lock. The real UPDATE enforces
                # 40001/retrying() for distinct requests under REPEATABLE READ.
                account.flush_recordset()
                self.env.cr.execute('SELECT id FROM %s WHERE id = %%s FOR NO KEY UPDATE'
                                    % account._table, (account.id,))
                existing = Order.search(domain, limit=1)
                if not existing:
                    now = fields.Datetime.now()
                    self.env.cr.execute(
                        'UPDATE %s SET last_convert_request_at = %%s, '
                        'write_date = %%s, write_uid = %%s WHERE id = %%s' % account._table,
                        (now, now, user.id, account.id))
                    account.invalidate_recordset()
                    self._llm_convert_preflight(account, partner, points,
                                                confirm_reserve, accept_no_refund, session)
                    order = Order._llm_create_paid_order(account, points, client_request_id)
                    _credit_internal(order).write({
                        'convert_event_id': event_id,
                        'acceptance_notice_pending': True,
                    })
                    # The scalar reference is durable ownership before worker
                    # claim. This REPEATABLE READ cursor cannot see the event
                    # inserted after its snapshot by an independent cursor
                    # (even a foreign key check would reject it).
                    # Worker claim binds its permanent event target before
                    # this pointer can ever be replaced on a later attempt.
                    # The trigger row and paid order commit or roll back as one.
                    self.env.ref(CREDIT_CRON_XMLID).sudo()._trigger()
                    self.env.ref('woow_paas_platform.ir_cron_llm_notifications').sudo()._trigger()
                else:
                    order = existing
        except Exception:
            # The event was committed by an independent cursor. All T1 work
            # was inside the savepoint and is definitely not sent upstream.
            Event._llm_event_finish(event_id, 'not_executed')
            raise
        if existing:
            Event._llm_event_finish(event_id, 'not_executed')
        return {'order_id': order.id, 'name': order.name, 'state': order.state}

    @api.model
    def _cron_llm_credit(self):
        """N27/N28 gate first; paid orders continue when orders_enabled is off."""
        config = llm_config(self.env)
        if llm_unconfigured(self.env, config):
            return False
        # A half-open circuit probe is HTTP too: guard must precede the
        # shared cron runner, not just the work callback it invokes.
        if self.env['woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            return False
        return run_llm_cron(self.env, CREDIT_CRON_XMLID, self._llm_credit_tick,
                            config=config, items=1, outbound=True)

    @api.model
    def _llm_credit_tick(self, config, tick):
        # Recheck after a possible circuit probe and before claiming.
        if self.env['woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            return False
        if not tick.can_take(nac.MIN_CALL_SECONDS):
            return False
        claim = self._llm_claim_credit(deadline=tick.deadline)
        if not claim:
            return False
        tick.took()
        self._llm_send_claimed(claim, tick.deadline)
        # Only one potentially 20-second upstream write per tick. A paid tail
        # gets another cron trigger; the minute interval is the crash fallback.
        return self._llm_credit_has_due()

    # ------------------------------------------------------------------ #
    #  Unknown credit verification (LLM-08 Task 7)                       #
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_llm_credit_verify(self):
        """Converge an unknown/expired conversion without ever resending it.

        This is intentionally separate from ``_cron_llm_credit``: the latter
        owns non-idempotent POSTs, while this job only reads new-api and can
        move an in-flight order to a terminal state or a safely re-queued
        ``paid`` state after durable evidence. ``orders_enabled`` is a new-T1
        switch and must not strand an already debited order.
        """
        config = llm_config(self.env)
        if llm_unconfigured(self.env, config):
            return False
        if self.env['woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            return False
        return run_llm_cron(self.env, CREDIT_VERIFY_CRON_XMLID, self._llm_credit_verify_tick,
                            config=config, items=1, outbound=True)

    @api.model
    def _llm_credit_verify_tick(self, config, tick):
        if self.env['woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            return False
        if not tick.can_take(nac.MIN_CALL_SECONDS):
            return False
        claim = self._llm_claim_credit_verification()
        if not claim:
            return False
        tick.took()
        self._llm_verify_claimed(claim, config, tick.deadline)
        more = self._llm_credit_verify_has_due()
        # A retained page cursor or a five-minute quiet period must not spin
        # immediate triggers; the normal one-minute schedule is sufficient.
        if more:
            tick.delay = 60
        return more

    @api.model
    def _llm_claim_credit_verification(self):
        """Claim one unknown or expired-sending order in an independent cursor.

        The verifier does not clear the per-account in-flight state while it
        is reading. A late worker still has ``verify_token IS NULL`` in every
        guarded write, so this token is also the stale-worker fence.
        """
        token = secrets.token_urlsafe(24)
        now = fields.Datetime.now()
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute(
                'UPDATE {t} AS target SET verify_token = %s, verify_lease_until = %s, '
                'write_uid = %s, write_date = %s '
                'WHERE target.id = (SELECT o.id FROM {t} o '
                'WHERE (o.verify_token IS NULL OR o.verify_lease_until <= %s) '
                "AND (o.state = 'unknown' OR (o.state = 'sending' AND "
                '(o.lease_until IS NULL OR o.lease_until <= %s))) '
                'ORDER BY o.sent_at NULLS FIRST, o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1) '
                'RETURNING target.id, target.account_id, target.attempt, target.state, '
                'target.convert_event_id AS event_id, target.points, '
                '(SELECT ca.attempt_uuid FROM {ledger} ca WHERE ca.order_id = target.id '
                'AND ca.attempt_no = target.attempt) AS attempt_uuid'.format(
                    t=ORDER_TABLE, ledger=ATTEMPT_TABLE),
                (token, now + CREDIT_VERIFY_LEASE, SUPERUSER_ID, now, now, now),
                log_exceptions=False)
            row = cr.dictfetchone()
        if not row:
            return None
        row['token'] = token
        return row

    @api.model
    def _llm_credit_verify_has_due(self):
        now = fields.Datetime.now()
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT 1 FROM {t} o WHERE '
                '(o.verify_token IS NULL OR o.verify_lease_until <= %s) '
                "AND (o.state = 'unknown' OR (o.state = 'sending' AND "
                '(o.lease_until IS NULL OR o.lease_until <= %s))) LIMIT 1'.format(t=ORDER_TABLE),
                (now, now))
            return bool(cr.fetchone())

    @api.model
    def _llm_verify_claimed_row(self, claim):
        """Read the current row only while this verifier still owns it."""
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT o.id, o.account_id, o.newapi_user_id, o.points, o.quota, o.source, '
                'o.state, o.attempt, o.sent_at, o.lease_until, o.convert_event_id, '
                'o.before_snapshot_present, o.quota_before, o.used_quota_before, '
                'o.request_count_before, o.before_sampled_at, o.verify_first_sample_present, '
                'o.verify_first_quota, o.verify_first_used_quota, o.verify_first_request_count, '
                'o.verify_first_sample_at, o.verify_log_page, o.verify_log_end_at, '
                'o.newapi_request_id, a.newapi_username, a.newapi_user_id AS account_user_id '
                'FROM {t} o JOIN {a} a ON a.id = o.account_id '
                'WHERE o.id = %s AND o.account_id = %s AND o.attempt = %s AND o.state = %s '
                'AND o.verify_token = %s AND o.verify_lease_until > %s '
                'AND o.convert_event_id IS NOT DISTINCT FROM %s AND '.format(
                    t=ORDER_TABLE, a=ACCOUNT_TABLE) + _attempt_guard('o', legacy=True),
                (claim['id'], claim['account_id'], claim['attempt'], claim['state'],
                 claim['token'], fields.Datetime.now(), claim.get('event_id'), claim.get('attempt_uuid'),
                 claim.get('attempt_uuid')))
            row = cr.dictfetchone()
        return row

    @staticmethod
    def _llm_verify_epoch(value):
        try:
            stamp = fields.Datetime.to_datetime(value)
        except (TypeError, ValueError):
            return None
        if not stamp:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)
        return int(stamp.timestamp())

    @staticmethod
    def _llm_verify_snapshot(data, user_id):
        if not isinstance(data, dict) or type(data.get('id')) is not int or data['id'] != user_id:
            raise credit_evidence.EvidenceError('invalid_quota_snapshot')
        return credit_evidence.QuotaSnapshot(
            data.get('quota'), data.get('used_quota'), data.get('request_count'))

    @staticmethod
    def _llm_verify_snapshot_evidence(snapshot, sampled_at):
        return {
            'quota': snapshot.quota,
            'used_quota': snapshot.used_quota,
            'request_count': snapshot.request_count,
            'sampled_at': fields.Datetime.to_string(sampled_at),
        }

    @staticmethod
    def _llm_verify_log_evidence(proof):
        stamp = datetime.fromtimestamp(proof.created_at, tz=timezone.utc).replace(tzinfo=None)
        return {
            'request_id': proof.request_id,
            'target_user_id': proof.target_user_id,
            'admin_id': proof.admin_id,
            'quota': proof.quota,
            'from': proof.before,
            'to': proof.after,
            'sampled_at': fields.Datetime.to_string(stamp),
        }

    @staticmethod
    def _llm_verify_log_page(data, page):
        """Validate a complete new-api PageInfo response before using absence."""
        if not isinstance(data, dict):
            raise credit_evidence.EvidenceError('invalid_credit_log')
        response_page = data.get('page')
        response_size = data.get('page_size')
        total = data.get('total')
        items = data.get('items')
        if (type(response_page) is not int or response_page != page or
                type(response_size) is not int or response_size != CREDIT_VERIFY_LOG_PAGE_SIZE or
                type(total) is not int or total < 0 or not isinstance(items, list)):
            raise credit_evidence.EvidenceError('invalid_credit_log')
        offset = (page - 1) * CREDIT_VERIFY_LOG_PAGE_SIZE
        expected = min(CREDIT_VERIFY_LOG_PAGE_SIZE, max(total - offset, 0))
        if len(items) != expected:
            # A changed/truncated page is not evidence of an empty search.
            raise credit_evidence.EvidenceError('invalid_credit_log')
        return items, offset + len(items) < total

    @staticmethod
    def _llm_verify_before_snapshot(row, sent_at=None):
        if not row['before_snapshot_present']:
            return None
        try:
            stamp = fields.Datetime.to_datetime(row['before_sampled_at'])
            stamp_epoch = LlmCreditOrder._llm_verify_epoch(stamp)
            # The claim timestamp is committed before the pre-POST user read.
            # An older durable sample could only belong to a prior attempt and
            # must not become a fallback proof for this one.
            if (not stamp or stamp_epoch is None or
                    (sent_at is not None and stamp_epoch < sent_at)):
                raise credit_evidence.EvidenceError('invalid_quota_snapshot')
            return credit_evidence.QuotaSnapshot(
                row['quota_before'], row['used_quota_before'], row['request_count_before'])
        except (TypeError, ValueError, credit_evidence.EvidenceError):
            return None

    @staticmethod
    def _llm_verify_first_snapshot(row):
        if not row['verify_first_sample_present']:
            return None
        try:
            stamp = fields.Datetime.to_datetime(row['verify_first_sample_at'])
            if not stamp:
                raise credit_evidence.EvidenceError('invalid_quota_snapshot')
            return (credit_evidence.QuotaSnapshot(
                row['verify_first_quota'], row['verify_first_used_quota'],
                row['verify_first_request_count']), stamp)
        except (TypeError, ValueError, credit_evidence.EvidenceError):
            return None

    @api.model
    def _llm_verify_request_id_claimed(self, request_id, order_id):
        """Fast conflict check; the DB unique index remains the race backstop."""
        with self.env.registry.cursor() as cr:
            cr.execute('SELECT 1 FROM %s WHERE newapi_request_id = %%s AND id <> %%s LIMIT 1'
                       % ORDER_TABLE, (request_id, order_id))
            return bool(cr.fetchone())

    @api.model
    def _llm_verify_update(self, claim, values, *, terminal_event=None, strict_event=True):
        """Persist one verifier outcome and release its token under one guard."""
        allowed = {
            'state', 'next_attempt_at', 'lease_until', 'last_error_code',
            'verify_first_quota', 'verify_first_used_quota', 'verify_first_request_count',
            'verify_first_sample_at', 'verify_first_sample_present', 'verify_log_page',
            'verify_log_end_at', 'newapi_request_id', 'credit_evidence', 'credited_at',
            'evidence_json',
        }
        values = dict(values)
        if not values or set(values) - allowed:
            raise ValueError('invalid credit verifier write')
        if terminal_event not in (None, 'ok', 'unknown', 'not_executed'):
            raise ValueError('invalid credit verifier event result')
        if 'newapi_request_id' in values:
            values['newapi_request_id'] = _request_id(values['newapi_request_id'])
        if 'evidence_json' in values:
            values['evidence_json'] = json.dumps(_evidence(values['evidence_json']),
                                                 separators=(',', ':'), sort_keys=True)
        # A completed read must release ownership. A crash retains the lease
        # until another verifier can safely fence this token out.
        values['verify_token'] = None
        values['verify_lease_until'] = None
        columns = sorted(values)
        assignments = ', '.join('%s = %%s' % column for column in columns)
        now = fields.Datetime.now()
        params = [values[column] for column in columns]
        params.extend([SUPERUSER_ID, now, claim['id'], claim['account_id'], claim['attempt'],
                       claim['state'], claim['token'], now])
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute(
                'UPDATE %s SET %s, write_uid = %%s, write_date = %%s '
                'WHERE id = %%s AND account_id = %%s AND attempt = %%s AND state = %%s '
                'AND verify_token = %%s AND verify_lease_until > %%s '
                'AND convert_event_id IS NOT DISTINCT FROM %%s '
                'AND %s RETURNING account_id, newapi_user_id, quota'
                % (ORDER_TABLE, assignments, _attempt_guard(ORDER_TABLE, legacy=True)),
                params + [claim.get('event_id'), claim.get('attempt_uuid'), claim.get('attempt_uuid')],
                log_exceptions=False)
            updated = cr.dictfetchone()
            if not updated:
                return False
            if terminal_event:
                cr.execute(
                    'UPDATE %s SET result = %%s, error_code = %%s, write_uid = %%s, '
                    'write_date = %%s WHERE id = %%s AND action = %s '
                    "AND result IN ('started', 'unknown') AND target_model = %%s "
                    'AND target_id = %%s AND account_id = %%s' % (EVENT_TABLE, '%s'),
                    (terminal_event, values.get('last_error_code'), SUPERUSER_ID, now,
                     claim['event_id'], 'convert', ORDER_MODEL, claim['id'], claim['account_id']),
                    log_exceptions=False)
                if strict_event and cr.rowcount != 1:
                    raise ValueError('credit verifier reservation changed')
            if values.get('state') == 'credited':
                cr.execute('UPDATE %s SET balance_invalidated_at = %%s, write_uid = %%s, '
                           'write_date = %%s WHERE id = %%s AND newapi_user_id = %%s' % ACCOUNT_TABLE,
                           (balance_stamp(), SUPERUSER_ID, now, updated['account_id'],
                            updated['newapi_user_id']), log_exceptions=False)
        return True

    @api.model
    def _llm_verify_release(self, claim, *, error_code=None, **values):
        values['last_error_code'] = safe_code(error_code) if error_code else None
        return self._llm_verify_update(claim, values)

    @api.model
    def _llm_verify_finish(self, claim, state, error_code=None, *, terminal_event=None,
                           strict_event=True, evidence=None, request_id=None,
                           next_attempt_at=None, credit_evidence_kind=None):
        # A quiet balance / absent outcome cannot fence a paused dispatcher.
        # Until owner/epoch and stronger not-sent evidence are defined, a
        # durable attempt is never automatically reissued by the verifier.
        if state == 'paid' and claim.get('attempt_uuid'):
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_CONFLICT, evidence=evidence)
        values = {
            'state': state,
            'lease_until': None,
            'next_attempt_at': next_attempt_at,
            'last_error_code': safe_code(error_code) if error_code else None,
            'verify_log_page': 0,
            'verify_log_end_at': None,
        }
        if evidence is not None:
            values['evidence_json'] = evidence
        if request_id is not None:
            values['newapi_request_id'] = request_id
        if credit_evidence_kind is not None:
            values['credit_evidence'] = credit_evidence_kind
        if state == 'credited':
            values['credited_at'] = fields.Datetime.now()
        try:
            committed = self._llm_verify_update(claim, values, terminal_event=terminal_event,
                                                strict_event=strict_event)
        except psycopg2.errors.UniqueViolation:
            if state == 'needs_review':
                raise
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_CONFLICT, evidence=evidence)
        if not committed:
            return False
        if state == 'credited':
            self.env.ref(BALANCE_CRON_XMLID).sudo()._trigger()
        elif state == 'paid':
            self.env.ref(CREDIT_CRON_XMLID).sudo()._trigger()
        return True

    @api.model
    def _llm_verify_invalid_reservation_review(self, claim, error_code):
        """Quarantine an owned verifier with a broken event scalar, never settle it.

        The normal owner guard remains mandatory for credit/retry/completion.
        This separate CAS tolerates only the invalid order-event binding: the
        immutable ledger must still match UUID/order/account/attempt/target/quota.
        It cannot repair a reservation, mutate a ledger or authorize another send.
        """
        event_id = claim.get('event_id')
        if (error_code != CREDIT_EVIDENCE_INVALID or
                (event_id is not None and (type(event_id) is not int or event_id > 0)) or
                claim.get('state') not in ('unknown', 'sending')):
            return False
        now = fields.Datetime.now()
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute(
                "UPDATE {orders} AS o SET state = 'needs_review', last_error_code = %s, "
                'lease_until = NULL, next_attempt_at = NULL, verify_token = NULL, '
                'verify_lease_until = NULL, verify_log_page = 0, verify_log_end_at = NULL, '
                'write_uid = %s, write_date = %s '
                'WHERE o.id = %s AND o.account_id = %s AND o.attempt = %s AND o.state = %s '
                "AND o.state IN ('unknown', 'sending') "
                'AND o.verify_token = %s AND o.verify_lease_until > %s '
                'AND o.convert_event_id IS NOT DISTINCT FROM %s '
                'AND (o.convert_event_id IS NULL OR o.convert_event_id <= 0) '
                'AND EXISTS (SELECT 1 FROM {ledger} ca WHERE ca.order_id = o.id '
                'AND ca.account_id = o.account_id AND ca.attempt_no = o.attempt '
                'AND ca.target_user_id = o.newapi_user_id AND ca.quota = o.quota '
                'AND ca.attempt_uuid = %s AND ca.reservation_event_id > 0) RETURNING id'.format(
                    orders=ORDER_TABLE, ledger=ATTEMPT_TABLE),
                (CREDIT_EVIDENCE_INVALID, SUPERUSER_ID, now, claim['id'], claim['account_id'],
                 claim['attempt'], claim['state'], claim['token'], now, event_id,
                 claim.get('attempt_uuid')), log_exceptions=False)
            return cr.dictfetchone() is not None

    @api.model
    def _llm_verify_review(self, claim, error_code, *, evidence=None):
        """Mark the order for humans only after its verifier guard commits."""
        committed = self._llm_verify_finish(
            claim, 'needs_review', error_code,
            terminal_event='unknown' if claim.get('event_id') else None,
            strict_event=False, evidence=evidence)
        if not committed:
            committed = self._llm_verify_invalid_reservation_review(claim, error_code)
        if not committed:
            return False
        self._llm_credit_alert(claim, error_code)
        try:
            incident_id = self.env['woow_paas_platform.llm_event']._llm_incident(
                LlmIncident.CREDIT_UNKNOWN, account_id=claim['account_id'], order_id=claim['id'],
                error_code=error_code, points=claim.get('points'), needs_human=True)
        except Exception:  # noqa: BLE001 - the review state is already durable
            _logger.error('[LLM] credit verifier incident failed for order=%s', claim['id'])
            incident_id = False
        if incident_id:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute('UPDATE %s SET incident_id = %%s, write_uid = %%s, write_date = %%s '
                           "WHERE id = %%s AND state = 'needs_review' AND incident_id IS NULL" % ORDER_TABLE,
                           (incident_id, SUPERUSER_ID, fields.Datetime.now(), claim['id']),
                           log_exceptions=False)
        return True

    @api.model
    def _llm_verify_claimed(self, claim, config, deadline):
        if type(claim.get('event_id')) is not int or claim['event_id'] <= 0:
            # 每次 conversion 都必須有耐久的 throttle reservation；損壞的
            # legacy row 沒有它就不能安全收斂。
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        row = self._llm_verify_claimed_row(claim)
        if not row:
            return False
        sent_at = self._llm_verify_epoch(row['sent_at'])
        if sent_at is None:
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        username = row['newapi_username']
        if (not isinstance(username, str) or not nac.USERNAME_RE.fullmatch(username) or
                row['newapi_user_id'] != row['account_user_id']):
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        expected_request_id = row['newapi_request_id'] or None
        if expected_request_id is not None:
            try:
                expected_request_id = _request_id(expected_request_id)
            except ValidationError:
                return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        elif not row['before_snapshot_present']:
            # 沒有 response id 時，只能靠完整且受信任的操作內容關聯 audit
            # log，而該證據一定要有 pre-send 值。
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_MISSING_SNAPSHOT)
        before = self._llm_verify_before_snapshot(row, sent_at)
        if expected_request_id is None and before is None:
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_MISSING_SNAPSHOT)
        page = row['verify_log_page'] or 0
        # ``NewApiClient._paging`` only accepts 1..999999.  A malformed
        # durable cursor must not turn into an endlessly retried client-input
        # exception while the account stays in-flight.
        if page and (type(page) is not int or not 1 <= page <= 999999):
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        if page:
            end_at = row['verify_log_end_at']
            end_at_epoch = self._llm_verify_epoch(end_at)
            if end_at_epoch is None:
                return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        else:
            page, end_at = 1, fields.Datetime.now()
            end_at_epoch = self._llm_verify_epoch(end_at)
        if end_at_epoch is None or end_at_epoch < sent_at:
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        try:
            client = self.env['woow_paas_platform.llm_account']._llm_client(nac.ROLE_CRON)
        except nac.NewApiError:
            return self._llm_verify_release(claim, error_code=CREDIT_EVIDENCE_UNAVAILABLE)
        log_result = client.ap_log({
            'p': page,
            'page_size': CREDIT_VERIFY_LOG_PAGE_SIZE,
            'type': 1,
            'start_timestamp': sent_at,
            'end_timestamp': end_at_epoch,
            'username': username,
        }, timeout=nac.TIMEOUT_CRON_READ, deadline=deadline, priority=nac.AP_PRIORITY_OTHER)
        if not isinstance(log_result, nac.Ok):
            return self._llm_verify_release(claim, error_code=CREDIT_EVIDENCE_UNAVAILABLE)
        try:
            items, more_pages = self._llm_verify_log_page(log_result.data, page)
            for log in items:
                proof = credit_evidence.credit_log_proof(
                    log, target_user_id=row['newapi_user_id'], quota=row['quota'],
                    quota_before=before.quota if before is not None else None,
                    sent_at=sent_at, observed_at=end_at_epoch,
                    expected_request_id=expected_request_id,
                    service_admin_id=config.get('service_admin_id'))
                if proof is None:
                    continue
                if self._llm_verify_request_id_claimed(proof.request_id, claim['id']):
                    return self._llm_verify_review(claim, CREDIT_EVIDENCE_CONFLICT)
                return self._llm_verify_finish(
                    claim, 'credited', terminal_event='ok', request_id=proof.request_id,
                    credit_evidence_kind='log_request_id',
                    evidence={'log': self._llm_verify_log_evidence(proof)})
        except credit_evidence.EvidenceError:
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        if more_pages:
            return self._llm_verify_release(
                claim, verify_log_page=page + 1, verify_log_end_at=end_at)

        sample_result = client.ap_get_user(
            row['newapi_user_id'], timeout=nac.TIMEOUT_CRON_READ, deadline=deadline,
            priority=nac.AP_PRIORITY_OTHER)
        if not isinstance(sample_result, nac.Ok):
            return self._llm_verify_release(claim, error_code=CREDIT_EVIDENCE_UNAVAILABLE)
        sampled_at = fields.Datetime.now()
        try:
            sample = self._llm_verify_snapshot(sample_result.data, row['newapi_user_id'])
        except credit_evidence.EvidenceError:
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_INVALID)
        first = self._llm_verify_first_snapshot(row)
        if first is None:
            if before is None:
                return self._llm_verify_review(
                    claim, CREDIT_EVIDENCE_MISSING_SNAPSHOT,
                    evidence={'first': self._llm_verify_snapshot_evidence(sample, sampled_at)})
            return self._llm_verify_release(
                claim, verify_first_quota=sample.quota,
                verify_first_used_quota=sample.used_quota,
                verify_first_request_count=sample.request_count,
                verify_first_sample_at=sampled_at, verify_first_sample_present=True,
                verify_log_page=0, verify_log_end_at=None)
        first_sample, first_at = first
        first_epoch = self._llm_verify_epoch(first_at)
        if before is None or first_epoch is None:
            return self._llm_verify_review(claim, CREDIT_EVIDENCE_MISSING_SNAPSHOT)
        outcome = credit_evidence.quiescent_result(
            before, first_sample, sample, quota=row['quota'],
            elapsed_seconds=max(0, self._llm_verify_epoch(sampled_at) - first_epoch))
        evidence = {
            'before': self._llm_verify_snapshot_evidence(before, row['before_sampled_at']),
            'first': self._llm_verify_snapshot_evidence(first_sample, first_at),
            'second': self._llm_verify_snapshot_evidence(sample, sampled_at),
        }
        if outcome == 'wait':
            return self._llm_verify_release(claim, verify_log_page=0, verify_log_end_at=None)
        if outcome == 'credited':
            return self._llm_verify_finish(
                claim, 'credited', terminal_event='ok', evidence=evidence,
                credit_evidence_kind='quiescent_delta')
        if outcome == 'not_executed':
            if claim['attempt'] >= CREDIT_RETRY_LIMIT:
                return self._llm_verify_review(claim, CREDIT_EVIDENCE_RETRY_LIMIT, evidence=evidence)
            return self._llm_verify_finish(
                claim, 'paid', terminal_event='not_executed', evidence=evidence,
                credit_evidence_kind='quiescent_noop', next_attempt_at=fields.Datetime.now())
        return self._llm_verify_review(claim, CREDIT_EVIDENCE_MISMATCH, evidence=evidence)

    @api.model
    def _llm_claim_credit(self, account_ids=None, deadline=None):
        """Commit sending+attempt+lease and its N6 reservation before HTTP.

        A savepoint rolls back the entire provisional claim on N6 denial, so
        attempt/sent_at/snapshots are unchanged. Only next_attempt_at is then
        deferred; a bounded scan skips that account and can claim another.
        No expired sending row is reclaimed: Task 7 owns that decision.
        """
        scope = 'AND o.account_id = ANY(%s) ' if account_ids is not None else ''
        scope_params = [list(account_ids)] if account_ids is not None else []
        excluded = set()
        collisions = 0
        for scan in range(CREDIT_CLAIM_SCAN_LIMIT):
            # N28: repeated waits ending in N6 denial share one cron budget.
            # Leave room for one lock_timeout instead of starting another wait
            # with only a fraction of the tick left. No claim/attempt is spent.
            if deadline is not None and deadline - time.monotonic() < CREDIT_CLAIM_LOCK_BUDGET:
                return None
            review = None
            try:
                with self.env.registry.cursor() as cr:
                    _begin(self.env, cr, read_committed=True)
                    cr = _CreditClaimCursor(cr, deadline)
                    now = fields.Datetime.now()
                    try:
                        with cr.savepoint(flush=False):
                            cr.execute(
                                'UPDATE {t} AS target SET state = %s, attempt = target.attempt + 1, '
                                'lease_until = %s, sent_at = %s, next_attempt_at = NULL, '
                                'before_snapshot_present = false, before_sampled_at = NULL, '
                                'quota_before = NULL, used_quota_before = NULL, '
                                'request_count_before = NULL, last_error_code = NULL, '
                                'verify_first_sample_present = false, verify_first_sample_at = NULL, '
                                'verify_first_quota = NULL, verify_first_used_quota = NULL, '
                                'verify_first_request_count = NULL, verify_log_page = 0, '
                                'verify_log_end_at = NULL, newapi_request_id = NULL, '
                                'write_uid = %s, write_date = %s '
                                'WHERE target.id = (SELECT o.id FROM {t} o '
                                "WHERE o.state = 'paid' AND o.source IN ('user','manual','dr') "
                                "AND (o.source <> 'manual' OR o.manual_notice_accepted) "
                                'AND o.attempt < %s '
                                'AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= %s) '
                                'AND o.verify_token IS NULL {scope}'
                                'AND NOT (o.account_id = ANY(%s)) '
                                'AND NOT EXISTS (SELECT 1 FROM {t} busy WHERE '
                                'busy.account_id = o.account_id AND '
                                "busy.state IN ('sending','unknown','needs_review')) "
                                'ORDER BY o.id FOR UPDATE OF o SKIP LOCKED LIMIT 1) '
                                'RETURNING target.id, target.account_id, target.partner_id, '
                                'target.attempt, target.convert_event_id, target.source, '
                                'target.newapi_user_id, target.quota'.format(t=ORDER_TABLE, scope=scope),
                                ('sending', now + CREDIT_LEASE, now, SUPERUSER_ID, now,
                                 CREDIT_RETRY_LIMIT, now, *scope_params, list(excluded)),
                                log_exceptions=False)
                            row = cr.dictfetchone()
                            if not row:
                                return None
                            event_id = self._llm_claim_event_cr(cr, row)
                            attempt_uuid = _issue_cr(cr, row, event_id, _ISSUE_CAPABILITY)
                            cr.check()
                            return {'id': row['id'], 'account_id': row['account_id'],
                                    'attempt': row['attempt'], 'event_id': event_id,
                                    'attempt_uuid': attempt_uuid}
                    except _CreditReservationInvalid:
                        # The provisional claim has rolled back. Keep the
                        # inconsistent audit row untouched and stop for review.
                        cr.execute(
                            'UPDATE %s SET state = %%s, lease_until = NULL, next_attempt_at = NULL, '
                            'last_error_code = %%s, write_uid = %%s, write_date = %%s '
                            "WHERE id = %%s AND account_id = %%s AND state = 'paid' AND attempt = %%s "
                            'AND convert_event_id IS NOT DISTINCT FROM %%s AND verify_token IS NULL'
                            % ORDER_TABLE,
                            ('needs_review', CREDIT_RESERVATION_INVALID, SUPERUSER_ID, fields.Datetime.now(),
                             row['id'], row['account_id'], row['attempt'] - 1, row['convert_event_id']),
                            log_exceptions=False)
                        if cr.rowcount == 1:
                            review = row
                        excluded.add(row['account_id'])
                    except LlmError as exc:
                        if exc.code != ERR_LLM_RATE_LIMITED:
                            raise
                        retry_at = fields.Datetime.now() + timedelta(seconds=exc.params['retry_after'])
                        cr.execute(
                            'UPDATE %s SET next_attempt_at = %%s, last_error_code = %%s, '
                            'write_uid = %%s, write_date = %%s '
                            "WHERE id = %%s AND account_id = %%s AND state = 'paid' AND attempt = %%s "
                            'AND convert_event_id IS NOT DISTINCT FROM %%s AND verify_token IS NULL'
                            % ORDER_TABLE,
                            (retry_at, ERR_LLM_RATE_LIMITED, SUPERUSER_ID, fields.Datetime.now(),
                             row['id'], row['account_id'], row['attempt'] - 1, row['convert_event_id']),
                            log_exceptions=False)
                        excluded.add(row['account_id'])
                        # Normal cursor exit commits only the guarded deferral.
                    cr.check()
                if review:
                    self._llm_credit_alert(review, CREDIT_RESERVATION_INVALID)
            except _CreditClaimDeadline:
                # Outside both contexts: rollback sending/attempt/any new event.
                return None
            except psycopg2.errors.UniqueViolation:
                collisions += 1
                _logger.info('[LLM] credit claim account collision (scan=%s)', scan)
                if collisions >= 2:
                    return None
            except psycopg2.Error as exc:
                _logger.warning('[LLM] credit claim unavailable (pgcode=%s)',
                                getattr(exc, 'pgcode', None))
                return None
        return None

    @staticmethod
    def _llm_claim_event_cr(cr, row):
        event_id = row['convert_event_id']
        prior = None
        if event_id:
            cr.execute('SELECT result, action, account_id, uid, real_ip, target_model, target_id FROM %s '
                       'WHERE id = %%s FOR UPDATE' % EVENT_TABLE, (event_id,))
            prior = cr.dictfetchone()
            if prior is None:
                raise _CreditReservationInvalid('missing credit reservation')
        if prior and (prior['action'], prior['account_id']) != ('convert', row['account_id']):
            raise _CreditReservationInvalid('invalid credit reservation identity')
        owner = (prior['target_model'], prior['target_id']) if prior else (None, None)
        expected = (ORDER_MODEL, row['id'])
        if any(owner) and owner != expected:
            raise _CreditReservationInvalid('credit reservation belongs to another order')
        if row['attempt'] == 1:
            if prior:
                if prior['result'] != 'started':
                    raise _CreditReservationInvalid('first reservation is no longer started')
                # T1 commits only its scalar audit reference. This first RC
                # claim binds ownership before a later attempt can replace it.
                cr.execute('UPDATE %s SET target_model = %%s, target_id = %%s, credit_t1_pending = false '
                           'WHERE id = %%s' % EVENT_TABLE, (ORDER_MODEL, row['id'], event_id),
                           log_exceptions=False)
                return event_id
            if row['source'] not in ('manual', 'dr'):
                raise _CreditReservationInvalid('user conversion has no T1 reservation')
        elif not prior or prior['result'] != 'not_executed' or owner != expected:
            # Unknown/started/ok cannot authorize another non-idempotent POST.
            # A verifier may requeue only after settling the prior reservation
            # as not_executed with evidence, in the same transaction as paid.
            raise _CreditReservationInvalid('retry has no safe owned reservation')
        event_id = _throttle_reserve_cr(
            cr, row['account_id'], 'convert', partner_id=row['partner_id'],
            uid=prior['uid'] if prior else None, real_ip=prior['real_ip'] if prior else None,
            target_model=ORDER_MODEL, target_id=row['id'], credit_t1_pending=False)
        cr.execute('UPDATE %s SET convert_event_id = %%s WHERE id = %%s '
                   "AND state = 'sending' AND attempt = %%s" % ORDER_TABLE,
                   (event_id, row['id'], row['attempt']), log_exceptions=False)
        if cr.rowcount != 1:
            raise ValueError('credit claim lost')
        return event_id

    @api.model
    def _llm_credit_has_due(self, account_ids=None):
        scope = 'AND o.account_id = ANY(%s) ' if account_ids is not None else ''
        scope_params = [list(account_ids)] if account_ids is not None else []
        with self.env.registry.cursor() as cr:
            cr.execute("SELECT 1 FROM {t} o WHERE o.state = %s AND o.source IN ('user','manual','dr') "
                       "AND (o.source <> 'manual' OR o.manual_notice_accepted) "
                       'AND o.attempt < %s '
                       'AND (o.next_attempt_at IS NULL OR o.next_attempt_at <= %s) '
                       'AND o.verify_token IS NULL {scope}'
                       'AND NOT EXISTS (SELECT 1 FROM {t} busy WHERE '
                       'busy.account_id = o.account_id AND '
                       "busy.state IN ('sending','unknown','needs_review')) "
                       'LIMIT 1'.format(t=ORDER_TABLE, scope=scope),
                       ('paid', CREDIT_RETRY_LIMIT, fields.Datetime.now(), *scope_params))
            return bool(cr.fetchone())

    @api.model
    def _llm_claimed_row(self, claim):
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT o.id, o.account_id, o.newapi_user_id, o.quota, '
                'o.source, o.state, o.attempt, o.before_snapshot_present, '
                'a.state AS account_state, a.newapi_user_id AS account_user_id '
                'FROM %s o JOIN %s a ON a.id = o.account_id '
                "WHERE o.id = %%s AND o.state = 'sending' AND o.attempt = %%s "
                'AND o.account_id = %%s AND o.convert_event_id = %%s '
                'AND o.verify_token IS NULL AND o.lease_until > %%s AND '
                % (ORDER_TABLE, ACCOUNT_TABLE) + _attempt_guard('o'),
                (claim['id'], claim['attempt'], claim['account_id'], claim['event_id'],
                 fields.Datetime.now(), claim.get('attempt_uuid')))
            row = cr.dictfetchone()
        if not row or row['state'] != 'sending' or row['attempt'] != claim['attempt']:
            return None
        return row

    @api.model
    def _llm_dispatch_claim(self, claim):
        """Commit a one-way worker-entry CAS before any precheck/HTTP.

        Lock order first, as completion/verifier do. Crash after this commit
        consumes entry permanently; another invocation cannot retry this UUID.
        The timestamp is an entry receipt, never proof that HTTP was sent.
        """
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute(
                'SELECT o.id FROM ' + ORDER_TABLE + ' o WHERE o.id = %s '
                "AND o.state = 'sending' AND o.account_id = %s AND o.attempt = %s "
                'AND o.convert_event_id = %s AND o.verify_token IS NULL '
                'AND o.lease_until > %s AND ' + _attempt_guard('o') + ' FOR UPDATE OF o',
                (claim['id'], claim['account_id'], claim['attempt'], claim['event_id'],
                 fields.Datetime.now(), claim.get('attempt_uuid')))
            if not cr.fetchone():
                return False
            cr.execute(
                'UPDATE ' + ATTEMPT_TABLE + ' SET dispatch_claimed_at = %s '
                'WHERE order_id = %s AND attempt_no = %s AND attempt_uuid = %s '
                'AND dispatch_claimed_at IS NULL RETURNING id',
                (fields.Datetime.now(), claim['id'], claim['attempt'], claim.get('attempt_uuid')),
                log_exceptions=False)
            return bool(cr.fetchone())

    @api.model
    def _llm_send_claimed(self, claim, deadline):
        if not self._llm_dispatch_claim(claim):
            return
        row = self._llm_claimed_row(claim)
        if not row:
            return
        if row['account_state'] == 'closed':
            self._llm_worker_closed(claim, row)
            return
        if row['newapi_user_id'] != row['account_user_id']:
            self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                    CREDIT_SNAPSHOT_MISMATCH, alert=True)
            return
        if row['account_state'] not in ('active', 'suspended'):
            self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                    CREDIT_SNAPSHOT_MISMATCH, alert=True)
            return
        if self.env['woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            self._llm_worker_retry(claim, 'llm_guard')
            return
        try:
            client = self.env['woow_paas_platform.llm_account']._llm_client(nac.ROLE_CRON)
        except nac.NewApiError:
            self._llm_worker_retry(claim, CREDIT_CLIENT_UNAVAILABLE)
            return
        sample = client.ap_get_user(row['newapi_user_id'],
                                    timeout=nac.TIMEOUT_CRON_READ,
                                    deadline=deadline,
                                    priority=nac.AP_PRIORITY_CONVERSION)
        if not isinstance(sample, nac.Ok):
            # GET has no side effect, but policy/auth and target denials
            # require review just as they do on POST. The real client guard
            # already owns circuit accounting; do not count a result twice.
            if isinstance(sample, nac.Definite) and (
                    sample.proxy_error == 'denied' or
                    nac.circuit_effect('ap', sample)[0] == nac.CIRCUIT_TRIP):
                self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                        self._llm_result_code(sample), alert=True)
            else:
                self._llm_worker_retry(claim, self._llm_result_code(sample))
            return
        values = sample.data if isinstance(sample.data, dict) else {}
        before = tuple(values.get(key) for key in ('quota', 'used_quota', 'request_count'))
        if (type(values.get('id')) is not int or values.get('id') != row['newapi_user_id'] or
                not all(type(value) is int and -_INT8_MAX - 1 <= value <= _INT8_MAX
                        for value in before) or before[1] < 0 or before[2] < 0):
            self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                    CREDIT_SNAPSHOT_INVALID, alert=True)
            return
        sampled_at = fields.Datetime.now()
        if not self._llm_worker_update(claim, {
                'quota_before': before[0], 'used_quota_before': before[1],
                'request_count_before': before[2], 'before_sampled_at': sampled_at,
                'before_snapshot_present': True}):
            return
        # The guard or account may have changed while GET was in flight. A
        # stale snapshot cannot authorize the irreversible POST.
        if self.env['woow_paas_platform.llm_runtime_state']._llm_read()['guard_tripped']:
            self._llm_worker_retry(claim, 'llm_guard')
            return
        row = self._llm_claimed_row(claim)
        if not row:
            return
        if row['account_state'] == 'closed':
            self._llm_worker_closed(claim, row)
            return
        if row['newapi_user_id'] != row['account_user_id']:
            self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                    CREDIT_SNAPSHOT_MISMATCH, alert=True)
            return
        if row['account_state'] not in ('active', 'suspended'):
            self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                    CREDIT_SNAPSHOT_MISMATCH, alert=True)
            return
        if not row['before_snapshot_present']:
            # A verifier took over or snapshot persistence was rolled back.
            return
        result = client.ap_add_quota(row['newapi_user_id'], row['quota'],
                                     timeout=nac.TIMEOUT_CRON_WRITE,
                                     deadline=deadline,
                                     priority=nac.AP_PRIORITY_CONVERSION)
        self._llm_classify_credit(claim, result)

    @api.model
    def _llm_worker_closed(self, claim, row):
        if row['source'] in ('manual', 'dr'):
            self._llm_worker_finish(claim, 'cancelled', 'not_executed', None)
        else:
            # Task 6 replaces this seam with its atomic refund+credit-note
            # service. Until then, keep the paid wallet debit and require
            # review; never call it voided or pretend points were returned.
            self._llm_worker_void_pending(claim)

    @api.model
    def _llm_worker_void_pending(self, claim, *, event_result='not_executed'):
        """Task 6 integration seam; claim carries id/account_id/attempt/event_id.

        Caller has definite evidence no quota was added. Known upstream
        rejections use event_result=failed; closed-before-HTTP uses not_executed.
        Task 6 must keep the
        same id/account/event/sending/attempt/unowned-verifier/live-lease guard while atomically
        settling user wallet+credit note. Until integrated, user debit remains
        intact in review. Non-wallet orders can already be safely cancelled.
        """
        row = self._llm_claimed_row(claim)
        if not row:
            return False
        if row['source'] in ('manual', 'dr'):
            return self._llm_worker_finish(claim, 'cancelled', event_result, None)
        return self._llm_worker_finish(claim, 'needs_review', event_result,
                                       CREDIT_VOID_PENDING, alert=True)

    @api.model
    def _llm_classify_credit(self, claim, result):
        if isinstance(result, nac.Ok):
            try:
                request_id = _request_id(result.request_id)
            except ValidationError:
                request_id = None
            if request_id:
                return self._llm_worker_finish(claim, 'credited', 'ok', None,
                                               request_id=request_id)
            return self._llm_worker_finish(claim, 'unknown', 'unknown',
                                           CREDIT_RESPONSE_INVALID)
        if isinstance(result, nac.Unknown):
            try:
                request_id = _request_id(result.request_id)
            except ValidationError:
                request_id = None
            return self._llm_worker_finish(claim, 'unknown', 'unknown',
                                           self._llm_result_code(result), request_id=request_id)
        if isinstance(result, nac.NotExecuted):
            return self._llm_worker_retry(claim, self._llm_result_code(result),
                                          retry_after=result.retry_after)
        if isinstance(result, nac.Definite):
            if (result.proxy_error == 'denied' or
                    nac.circuit_effect('ap', result)[0] == nac.CIRCUIT_TRIP):
                return self._llm_worker_finish(claim, 'needs_review', 'failed',
                                               self._llm_result_code(result), alert=True)
            if result.message_key in _QUOTA_REJECTIONS:
                # Known quota rollback; Task 6 will call its atomic void/cancel
                # method here. Review is safe while that method is absent.
                return self._llm_worker_void_pending(claim, event_result='failed')
            if result.message_key:
                return self._llm_worker_finish(claim, 'needs_review', 'failed',
                                               CREDIT_DEFINITE_UNRECOGNISED, alert=True)
            if (result.status in (401, 403) and result.code in nac.KNOWN_NEWAPI_CODES) \
                    or (result.status == 500 and result.code == 'AUTH_INTERNAL_ERROR'):
                return self._llm_worker_retry(claim, self._llm_result_code(result),
                                              retry_after=result.retry_after)
            return self._llm_worker_finish(claim, 'needs_review', 'failed',
                                           CREDIT_DEFINITE_UNRECOGNISED, alert=True)
        # A future Result subclass cannot silently turn into a safe retry.
        return self._llm_worker_finish(claim, 'unknown', 'unknown',
                                       CREDIT_DEFINITE_UNRECOGNISED)

    @staticmethod
    def _llm_result_code(result):
        if isinstance(result, nac.Definite):
            if result.proxy_error:
                return safe_code('llm_proxy_%s' % result.proxy_error)
            if result.code:
                return nac.newapi_error_code(result.code)
        if isinstance(result, nac.NotExecuted):
            return CREDIT_BUDGET if result.reason == 'proxy_budget' else 'llm_credit_not_executed'
        if isinstance(result, nac.Unknown):
            return safe_code('llm_credit_unknown')
        return CREDIT_DEFINITE_UNRECOGNISED

    @api.model
    def _llm_worker_retry(self, claim, error_code, retry_after=None):
        attempt = claim['attempt']
        if attempt >= CREDIT_RETRY_LIMIT:
            return self._llm_worker_finish(claim, 'needs_review', 'not_executed',
                                           error_code, alert=True)
        seconds = CREDIT_BACKOFF_MINUTES[min(attempt - 1, 4)] * 60
        if type(retry_after) is int and 0 < retry_after <= 86400:
            seconds = max(seconds, retry_after)
        return self._llm_worker_finish(
            claim, 'paid', 'not_executed', error_code,
            next_attempt_at=fields.Datetime.now() + timedelta(seconds=seconds),
            alert=LlmAlert.CREDIT_BUDGET if error_code == CREDIT_BUDGET else False)

    @api.model
    def _llm_worker_update(self, claim, values, *, terminal_event=None):
        allowed = {'state', 'next_attempt_at', 'quota_before', 'used_quota_before',
                   'request_count_before', 'before_sampled_at', 'before_snapshot_present',
                   'last_error_code', 'newapi_request_id', 'credit_evidence',
                   'credited_at', 'lease_until'}
        if not values or set(values) - allowed:
            raise ValueError('invalid credit worker write')
        columns = sorted(values)
        assignments = ', '.join('%s = %%s' % column for column in columns)
        params = [values[column] for column in columns]
        params.extend([SUPERUSER_ID, fields.Datetime.now(), claim['id'], claim['attempt'],
                       claim['account_id'], claim['event_id'], fields.Datetime.now()])
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute(
                'UPDATE %s SET %s, write_uid = %%s, write_date = %%s '
                "WHERE id = %%s AND state = 'sending' AND attempt = %%s "
                'AND account_id = %%s AND convert_event_id = %%s '
                'AND verify_token IS NULL AND lease_until > %%s '
                'AND %s RETURNING account_id, newapi_user_id, quota'
                % (ORDER_TABLE, assignments, _attempt_guard(ORDER_TABLE)),
                params + [claim.get('attempt_uuid')], log_exceptions=False)
            updated = cr.dictfetchone()
            if not updated:
                return False
            if terminal_event:
                cr.execute('UPDATE %s SET result = %%s, error_code = %%s, '
                           'write_uid = %%s, write_date = %%s WHERE id = %%s '
                           "AND result = 'started' AND action = 'convert' "
                           'AND target_model = %%s AND target_id = %%s AND account_id = %%s' % EVENT_TABLE,
                           (terminal_event, values.get('last_error_code'), SUPERUSER_ID,
                            fields.Datetime.now(), claim['event_id'], ORDER_MODEL, claim['id'],
                            claim['account_id']),
                           log_exceptions=False)
                if cr.rowcount != 1:
                    raise ValueError('credit attempt reservation was not started')
            if values.get('state') == 'credited':
                cr.execute('UPDATE %s SET balance_invalidated_at = %%s, '
                           'write_uid = %%s, write_date = %%s WHERE id = %%s '
                           'AND newapi_user_id = %%s' % ACCOUNT_TABLE,
                           (balance_stamp(), SUPERUSER_ID, fields.Datetime.now(),
                            updated['account_id'], updated['newapi_user_id']),
                           log_exceptions=False)
            return True

    @api.model
    def _llm_worker_finish(self, claim, state, event_result, error_code,
                           *, request_id=None, next_attempt_at=None, alert=False):
        values = {'state': state, 'lease_until': None,
                  'last_error_code': safe_code(error_code) if error_code else None,
                  'next_attempt_at': next_attempt_at}
        if state == 'credited':
            values.update(newapi_request_id=request_id, credit_evidence='response',
                          credited_at=fields.Datetime.now())
        elif state == 'unknown' and request_id:
            # A timeout may still carry the proxy/new-api request id. Reserving
            # it lets Task 7 prove the matching audit row without trusting a
            # log's operator id; the DB unique index is the collision backstop.
            values['newapi_request_id'] = request_id
        try:
            committed = self._llm_worker_update(claim, values, terminal_event=event_result)
        except psycopg2.errors.UniqueViolation:
            if state == 'needs_review':
                raise
            return self._llm_worker_finish(claim, 'needs_review', 'unknown',
                                           CREDIT_EVIDENCE_CONFLICT, alert=True)
        if not committed:
            return False
        if state == 'credited':
            # The cache retains its actual earlier as-of time. Conversion
            # invalidation bars any GET begun before this commit from writing
            # over the new quota; refresh reads the authoritative balance.
            self.env.ref(BALANCE_CRON_XMLID).sudo()._trigger()
        if alert:
            self._llm_credit_alert(claim, values['last_error_code'],
                                   LlmAlert.CREDIT_REVIEW if alert is True else alert)
        return True

    @api.model
    def _llm_wallet_descriptions(self, partner, transactions):
        """Batch-map tenant wallet DTOs without rewriting accounting history.

        Callers pass only their owner-scoped page/activity rows. Orders have no
        portal ACL, so this private lookup uses sudo with the same owner anchor
        and a bounded transaction-id set; no order DTO or voucher id escapes.
        """
        if not transactions:
            return {}
        owned = transactions.filtered(lambda row: row.partner_id == partner)
        if not owned:
            return {}
        ids = set(owned.ids)
        orders = self.sudo().search([
            ('partner_id', '=', partner.id), '|',
            ('wallet_transaction_id', 'in', list(ids)),
            ('void_wallet_transaction_id', 'in', list(ids)),
        ])
        descriptions = {}
        for order in orders:
            if order.wallet_transaction_id.id in ids:
                descriptions[order.wallet_transaction_id.id] = _('AI 額度轉換 %s', order.name)
            if order.void_wallet_transaction_id.id in ids:
                descriptions[order.void_wallet_transaction_id.id] = _('AI 額度轉換未完成，點數退回')
        return descriptions

    @api.model
    def _llm_credit_alert(self, claim, error_code, alert=LlmAlert.CREDIT_REVIEW):
        """Alert only after its authoritative order transaction has committed."""
        try:
            self.env['woow_paas_platform.llm_event']._llm_alert(
                alert, account_id=claim['account_id'], target_model=ORDER_MODEL,
                target_id=claim['id'], error_code=error_code)
        except Exception:  # noqa: BLE001 - persisted state is authoritative
            _logger.warning('[LLM] credit order=%s alert failed', claim['id'])

    @api.model
    def _require_capability(self):
        if self.env.context.get(_WRITE_KEY) is not _WRITE_CAPABILITY:
            raise AccessError(_('Credit orders can only be changed by the platform.'))

    @api.model
    def _clean_values(self, values):
        values = dict(values)
        if 'evidence_json' in values:
            values['evidence_json'] = _evidence(values['evidence_json'])
        if 'newapi_request_id' in values:
            values['newapi_request_id'] = _request_id(values['newapi_request_id'])
        if 'last_error_code' in values:
            values['last_error_code'] = safe_code(values['last_error_code'])
        return values

    @api.model_create_multi
    def create(self, vals_list):
        self._require_capability()
        clean = []
        for values in vals_list:
            values = self._clean_values(values)
            state = values.get('state', 'paid')
            if state != 'paid' and not (values.get('source') == 'recharge' and state == 'credited'):
                raise ValidationError(_('A credit order must start paid.'))
            if values.get('source') == 'dr' and not self.env[
                    'woow_paas_platform.llm_runtime_state']._llm_read().get('dr_mode'):
                raise ValidationError(_('Recovery credits require disaster recovery mode.'))
            if values.get('name', '/') == '/':
                values['name'] = self.env['ir.sequence'].sudo().next_by_code(ORDER_MODEL)
                if not values['name']:
                    raise ValidationError(_('The credit order sequence is missing.'))
            clean.append(values)
        orders = super().create(clean)
        for order in orders:
            if order.source == 'manual' and order.account_id.state not in ('active', 'suspended'):
                raise ValidationError(_('Manual credits require an active or suspended account.'))
            # Eligibility is checked when accepting a new adjustment. Later
            # requester changes must not block settlement of an existing order;
            # requested_by_uid remains an immutable audit snapshot.
            requester = order.requested_by_uid
            if order.source == 'manual' and (not requester or requester.share or
                                            not requester.active or not requester.login or
                                            not requester.email or not requester.email.strip() or
                                            not requester.llm_named_human_eligible or
                                            requester in (self.env.ref('base.user_root'),
                                                          self.env.ref('base.user_admin'))):
                raise ValidationError(_('Manual credits require a provisioned named internal requester.'))
            if order.newapi_user_id != order.account_id.newapi_user_id:
                raise ValidationError(_('A credit order must snapshot its current upstream user.'))
        return _without_llm_internal(_without_capabilities(orders))

    def write(self, vals):
        self._require_capability()
        if set(vals) & _IMMUTABLE:
            raise AccessError(_('Credit order identity and conversion snapshots are immutable.'))
        if 'state' in vals and self.env.context.get(_TRANSITION_KEY) is not _TRANSITION_CAPABILITY:
            raise AccessError(_('Credit order states may only change through the state machine.'))
        return super().write(self._clean_values(vals))

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('Credit conversion orders cannot be deleted.'))

    def _transition(self, state, **values):
        """Private, serialized state API; caller must already hold the write capability.

        Evidence verification, payment and reversal are responsibilities of the
        calling service. Unknown results can never be blindly cancelled here.
        """
        self._require_capability()
        self.ensure_one()
        if 'state' in values:
            raise ValidationError(_('State must be supplied only once.'))
        self.flush_recordset()
        self.env.cr.execute('SELECT id FROM ' + ORDER_TABLE + ' WHERE id = %s FOR NO KEY UPDATE',
                            (self.id,))
        self.invalidate_recordset(['state', 'attempt'])
        allowed = state in _TRANSITIONS[self.state] or (
            self.source == 'recharge' and self.state == 'paid' and state == 'credited')
        if not allowed:
            raise ValidationError(_('Invalid credit order state transition.'))
        if not self._source_allows_state(self.source, state):
            raise ValidationError(_('The conversion state is invalid for this source.'))
        if state == 'paid' and max(self.attempt, values.get('attempt', self.attempt)) >= 6:
            raise ValidationError(_('Exhausted credit orders require manual review.'))
        if state == 'credit_lost' and not self.env[
                'woow_paas_platform.llm_runtime_state']._llm_read().get('dr_mode'):
            raise ValidationError(_('Lost credit may only be recorded in recovery mode.'))
        values['state'] = state
        values['state_notice_retry_at'] = False
        return self.with_context(**{_TRANSITION_KEY: _TRANSITION_CAPABILITY}).write(values)

    @api.model
    def _source_allows_state(self, source, state):
        if state in ('voided', 'void_failed') and source != 'user':
            return False
        if state == 'cancelled' and source not in ('manual', 'dr'):
            return False
        return source != 'recharge' or state in ('paid', 'credited', 'credit_lost')

    @api.constrains('account_id', 'partner_id', 'source', 'state', 'points', 'reason',
                    'requested_by_uid', 'dr_origin_order_id', 'recharge_of_order_id')
    def _check_source(self):
        for order in self:
            if not self._source_allows_state(order.source, order.state):
                raise ValidationError(_('The conversion state is invalid for this source.'))
            if order.partner_id != order.account_id.partner_id:
                raise ValidationError(_('The credit order must belong to its account partner.'))
            if order.source in ('manual', 'dr') and len((order.reason or '').strip()) < 10:
                raise ValidationError(_('A credit adjustment requires a reason of at least 10 characters.'))
            origin = order.dr_origin_order_id or order.recharge_of_order_id
            if origin and (origin == order or origin.account_id != order.account_id or
                           origin.partner_id != order.partner_id):
                raise ValidationError(_('A recovery order must use the same account as its origin.'))
            if order.source == 'dr' and (origin.state != 'credit_lost' or order.points > origin.points):
                raise ValidationError(_('Recovery credits cannot exceed a lost credit order.'))
            if order.source == 'recharge' and (origin.state != 'voided' or origin.source != 'user' or
                                              order.points != origin.points):
                raise ValidationError(_('Recharge must match a voided user conversion.'))
