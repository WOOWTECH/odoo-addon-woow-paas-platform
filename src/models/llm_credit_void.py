"""Atomic local reversal of a conversion proven not to have reached new-api.

Only the worker's current attempt can enter this service. Order ownership,
wallet credit, zero-tax credit note, settlement and event completion commit
together. It performs no HTTP and never reverses an uncertain upstream result.
"""
import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError

from .llm_common import ERR_LLM_INTERNAL
from .llm_credit_order import ORDER_MODEL, ORDER_TABLE, _attempt_guard, _credit_internal
from .llm_event import EVENT_TABLE, LlmIncident, _begin, _env_on

_logger = logging.getLogger(__name__)


class LlmCreditVoid(models.Model):
    _inherit = 'woow_paas_platform.llm_credit_order'

    _sql_constraints = [
        ('void_note_unique', 'UNIQUE(void_credit_note_id)',
         'A reversal credit note may settle only one conversion.'),
        ('void_wallet_unique', 'UNIQUE(void_wallet_transaction_id)',
         'A wallet reversal may settle only one conversion.'),
    ]

    @api.model
    def _llm_lock_void_claim(self, claim):
        """Refresh after taking the same attempt/lease guard as the worker."""
        if not isinstance(claim, dict) or any(
                type(claim.get(key)) is not int or claim[key] <= 0
                for key in ('id', 'account_id', 'attempt', 'event_id')):
            return self.browse()
        self.env.cr.execute(
            'SELECT id FROM ' + ORDER_TABLE + " WHERE id = %s AND state = 'sending' "
            'AND account_id = %s AND attempt = %s AND convert_event_id = %s '
            'AND verify_token IS NULL AND lease_until > %s AND '
            + _attempt_guard(ORDER_TABLE) + ' FOR NO KEY UPDATE',
            (claim['id'], claim['account_id'], claim['attempt'], claim['event_id'],
             fields.Datetime.now(), claim.get('attempt_uuid')))
        row = self.env.cr.fetchone()
        order = self.sudo().browse(row[0] if row else [])
        order.invalidate_recordset()
        return order

    def _llm_finish_void_event(self, claim, event_result, error_code=None):
        self.ensure_one()
        self.env.cr.execute(
            'UPDATE ' + EVENT_TABLE + ' SET result = %s, error_code = %s, '
            'write_uid = %s, write_date = %s WHERE id = %s '
            "AND action = 'convert' AND result = 'started' "
            'AND target_model = %s AND target_id = %s AND account_id = %s',
            (event_result, error_code, SUPERUSER_ID, fields.Datetime.now(),
             claim['event_id'], ORDER_MODEL, self.id, self.account_id.id))
        if self.env.cr.rowcount != 1:
            raise UserError(_('The credit attempt reservation is no longer owned.'))

    @api.model
    def _llm_worker_void_pending(self, claim, *, event_result='not_executed'):
        """Consume the worker's proof of non-execution; stale claims do nothing."""
        # A quota rejection reached new-api and still consumes N6's attempt
        # budget. Only local/closed-account cancellation is not_executed.
        if event_result not in ('not_executed', 'failed'):
            raise ValueError('invalid credit void event result')
        try:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                env = _env_on(self.env.registry, cr)
                with cr.savepoint():
                    order = env[ORDER_MODEL]._llm_lock_void_claim(claim)
                    if not order or order.source not in ('user', 'manual', 'dr'):
                        return False
                    if order.source == 'user':
                        order._llm_void_wallet()
                    else:
                        _credit_internal(order)._transition(
                            'cancelled', lease_until=False, next_attempt_at=False,
                            last_error_code=False)
                    order._llm_finish_void_event(claim, event_result)
                return True
        except Exception as exc:  # rollback the complete independent transaction first
            _logger.warning('[LLM] credit void order=%s failed (type=%s)',
                            claim.get('id') if isinstance(claim, dict) else None,
                            type(exc).__name__)
        # No refund survives the failed transaction. Preserve a durable,
        # guarded failure in a fresh transaction before creating the incident.
        try:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                env = _env_on(self.env.registry, cr)
                order = env[ORDER_MODEL]._llm_lock_void_claim(claim)
                if not order or order.source != 'user':
                    return False
                _credit_internal(order)._transition(
                    'void_failed', lease_until=False, next_attempt_at=False,
                    last_error_code=ERR_LLM_INTERNAL)
                order._llm_finish_void_event(claim, event_result, ERR_LLM_INTERNAL)
                points = order.points
        except Exception as exc:
            _logger.warning('[LLM] credit void failure could not be recorded (type=%s)',
                            type(exc).__name__)
            return False
        incident_id = self.env['woow_paas_platform.llm_event']._llm_incident(
            LlmIncident.VOID_FAILED, account_id=claim['account_id'],
            order_id=claim['id'], points=points, error_code=ERR_LLM_INTERNAL,
            needs_human=True)
        if incident_id:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute(
                    'UPDATE ' + ORDER_TABLE + ' SET incident_id = %s '
                    "WHERE id = %s AND state = 'void_failed' AND attempt = %s "
                    'AND convert_event_id = %s AND incident_id IS NULL',
                    (incident_id, claim['id'], claim['attempt'], claim['event_id']))
        return False

    def _llm_void_wallet(self):
        """The caller holds the order lock and owns the encompassing savepoint."""
        self.ensure_one()
        invoice = self.invoice_id
        source = invoice.invoice_line_ids.filtered(lambda line: line.display_type == 'product')
        if (self.source != 'user' or self.void_credit_note_id or self.void_wallet_transaction_id
                or not invoice or invoice.woow_llm_credit_order_id != self
                or invoice.partner_id != self.partner_id or invoice.move_type != 'out_invoice'
                or invoice.state != 'posted' or invoice.payment_state != 'paid'
                or invoice.amount_total != self.points or invoice.amount_tax
                or len(source) != 1 or source.tax_ids
                or self.wallet_transaction_id.transaction_type != 'deduct'
                or self.wallet_transaction_id.partner_id != self.partner_id
                or self.wallet_transaction_id.amount != -self.points):
            raise UserError(_('The original credit conversion did not settle exactly.'))
        partner = self.partner_id
        card = partner.get_wallet_loyalty_card()
        if not card:
            raise UserError(_('The conversion wallet is missing.'))
        card.flush_recordset(['points'])
        self.env.cr.execute('SELECT id FROM loyalty_card WHERE id = %s FOR NO KEY UPDATE',
                            (card.id,))
        # An actual row version serializes stale REPEATABLE READ consumers too.
        self.env.cr.execute('UPDATE loyalty_card SET points = points WHERE id = %s', (card.id,))
        card.invalidate_recordset(['points'])
        before = card.points
        Wallet = self.env['wallet.transaction'].sudo()
        domain = [('partner_id', '=', partner.id)]
        floor = Wallet.search(domain, order='id desc', limit=1).id or 0
        if not partner.refund_wallet_points(
                self.points, _('AI credit conversion incomplete %(order)s', order=self.name)):
            raise UserError(_('The conversion wallet reversal failed.'))
        card.invalidate_recordset(['points'])
        refund = Wallet.search(domain + [('id', '>', floor), ('transaction_type', '=', 'refund')])
        if (len(refund) != 1 or refund.amount != self.points
                or card.points != before + self.points or refund.balance_after != card.points):
            raise UserError(_('The conversion wallet reversal did not settle exactly.'))
        credit_note = self.env['account.move'].with_company(invoice.company_id).with_context(
            woow_force_issue=False).create({
                'move_type': 'out_refund', 'partner_id': partner.id,
                'journal_id': invoice.journal_id.id, 'currency_id': invoice.currency_id.id,
                'invoice_date': fields.Date.context_today(self),
                'reversed_entry_id': invoice.id, 'invoice_origin': invoice.name,
                'woow_llm_credit_order_id': self.id,
                'woow_skip_upstream_hook': True, 'woow_invoice_issue_state': 'not_required',
                'invoice_line_ids': [(0, 0, {
                    'product_id': source.product_id.id, 'account_id': source.account_id.id,
                    'name': _('Incomplete AI credit conversion %(order)s', order=self.name),
                    'quantity': self.points, 'price_unit': 1, 'discount': 0,
                    'tax_ids': [(5, 0, 0)],
                })],
            })
        credit_note.action_post()
        # Reuse the accounting inner helper, not the subscription's fail-soft
        # wrapper. Explicit checks turn every False/partial result into rollback.
        payment = self.env['sh.subscription.subscription']._woow_settle_refund_credit_note(
            credit_note, self.points)
        credit_note.invalidate_recordset()
        if (not payment or payment.amount != self.points or payment.payment_type != 'outbound'
                or payment.partner_id != partner or payment.move_id.state != 'posted'
                or credit_note.state != 'posted' or credit_note.payment_state != 'paid'
                or credit_note.amount_residual != 0 or credit_note.amount_total != self.points
                or credit_note.amount_tax != 0 or credit_note.invoice_line_ids.tax_ids
                or not credit_note.woow_skip_upstream_hook
                or credit_note.woow_invoice_issue_state != 'not_required'):
            raise UserError(_('The conversion credit note did not settle exactly.'))
        _credit_internal(self)._transition(
            'voided', void_credit_note_id=credit_note.id, void_wallet_transaction_id=refund.id,
            voided_at=fields.Datetime.now(), lease_until=False, next_attempt_at=False,
            last_error_code=False)
