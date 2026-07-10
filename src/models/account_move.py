import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    def create_ecpay_invoice(self):
        """Short-circuit when invoice is PaaS-flagged and not forced.

        Why: upstream `ecpay_invoice_tw` calls `create_ecpay_invoice()` inline
        from its `action_post()` hook (when company.auto_invoice='automatic'),
        raising UserError on ECPay failure — which would roll back the entire
        `_pay_from_wallet` transaction (including the wallet deduction).

        PaaS-created invoices set `woow_skip_upstream_hook=True` so this
        method returns early when called from the upstream hook. Our async
        `ecpay_invoice_cron` opens the back door via
        `with_context(woow_force_issue=True)` so it can still issue the invoice.

        Thread-safe: pure inheritance + context (no class-level monkey-patch).
        """
        self.ensure_one()
        if (self.woow_skip_upstream_hook
                and not self.env.context.get('woow_force_issue')):
            _logger.debug(
                "[INV] create_ecpay_invoice short-circuit invoice=%s "
                "(skip flag set, no force ctx)",
                self.id,
            )
            return
        return super().create_ecpay_invoice()

    def _pay_from_wallet(self, subscription=None):
        """Settle this invoice using wallet points.

        Deducts `amount_total` (for draft) or `amount_residual` (for posted)
        from `partner_id.wallet_balance`, posts the invoice if still in draft,
        creates an `account.payment`, and reconciles the receivable lines.

        Caller is expected to have already verified balance sufficiency; if the
        wallet can't cover the amount when this method runs we raise UserError
        rather than silently partial-deducting (partner.deduct_wallet_points
        would otherwise return only what it could take).

        :param subscription: optional ``sh.subscription.subscription`` record
            used for the wallet.transaction description / audit link.
        :returns: True on success.
        :raises UserError: insufficient wallet balance, wallet deduction
            failure, or missing payment journal.
        """
        self.ensure_one()
        # #324：手動付款端點 api_invoice_pay 呼叫時不帶 subscription。若這是
        # 訂閱週期發票（account.move.sh_subscription_id 有值），從發票回推
        # subscription，讓 deduct_wallet_points 把 subscription_id stamp 到該筆
        # done deduct 上——否則退點 gate _has_paid_current_cycle 找不到 stamped
        # deduct，會把「手動付掉整個週期」的訂閱誤判為未付、刪服務退 0。
        if subscription is None and self.sh_subscription_id:
            subscription = self.sh_subscription_id
        partner = self.partner_id.sudo()
        amount = self.amount_total if self.state == 'draft' else self.amount_residual
        _logger.info(
            "[INV] _pay_from_wallet entry invoice=%s name=%s state=%s partner=%s amount=%s balance=%s sub=%s",
            self.id, self.name, self.state, partner.id, amount,
            partner.wallet_balance,
            subscription.id if subscription else None,
        )
        if amount <= 0:
            _logger.info(
                "[INV] _pay_from_wallet nothing_to_pay invoice=%s", self.id,
            )
            raise UserError(_("Nothing to pay for invoice %s.", self.name or ""))
        if partner.wallet_balance < amount:
            _logger.info(
                "[INV] _pay_from_wallet insufficient invoice=%s need=%s have=%s",
                self.id, amount, partner.wallet_balance,
            )
            raise UserError(_("Insufficient wallet balance."))

        # Post first so the sequence assigns self.name before the wallet
        # transaction records it in its description.
        if self.state == 'draft':
            _logger.info("[INV] _pay_from_wallet posting draft invoice=%s", self.id)
            self.action_post()

        description = (
            f"Subscription invoice: {self.name}" if subscription
            else f"Invoice payment: {self.name}"
        )
        deducted = partner.deduct_wallet_points(
            amount, description, subscription=subscription,
        )
        # Drop the partner.wallet_balance cache so any caller that re-reads it
        # in the same request (controllers building responses, the renewal cron
        # processing the next subscription) gets the post-deduction value.
        partner.invalidate_recordset(['wallet_balance'])
        _logger.info(
            "[INV] _pay_from_wallet deducted invoice=%s requested=%s got=%s",
            self.id, amount, deducted,
        )
        if deducted <= 0:
            raise UserError(_("Wallet deduction failed."))

        company = self.company_id.sudo()
        journal = company.sh_paid_subscription_journal
        if not journal:
            journal = self.env['account.journal'].sudo().search(
                [
                    ('company_id', '=', company.id),
                    ('type', 'in', ('bank', 'cash')),
                ],
                limit=1,
            )
        if not journal:
            _logger.warning(
                "[INV] _pay_from_wallet no journal invoice=%s company=%s",
                self.id, company.id,
            )
            raise UserError(_("No payment journal configured for company %s.", company.name))

        payment_method_line = journal.inbound_payment_method_line_ids[:1]
        payment = self.env['account.payment'].sudo().create({
            'journal_id': journal.id,
            'amount': amount,
            'payment_type': 'inbound',
            'payment_method_line_id': payment_method_line.id if payment_method_line else False,
            'partner_type': 'customer',
            'partner_id': partner.id,
        })
        payment.action_post()
        _logger.info(
            "[INV] _pay_from_wallet payment created invoice=%s payment=%s journal=%s",
            self.id, payment.id, journal.id,
        )

        receivable_lines = self.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
        )
        payment_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
        )
        if receivable_lines and payment_lines:
            (receivable_lines + payment_lines).reconcile()
            _logger.info(
                "[INV] _pay_from_wallet reconciled invoice=%s inv_lines=%s pay_lines=%s",
                self.id, len(receivable_lines), len(payment_lines),
            )
        else:
            _logger.warning(
                "[INV] _pay_from_wallet reconcile skipped invoice=%s inv_lines=%s pay_lines=%s",
                self.id, len(receivable_lines), len(payment_lines),
            )

        self.invalidate_recordset(['payment_state', 'amount_residual'])
        _logger.info(
            "[INV] _pay_from_wallet done invoice=%s name=%s amount=%s partner=%s",
            self.id, self.name, amount, partner.id,
        )
        return True
