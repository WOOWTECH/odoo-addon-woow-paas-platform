import base64
import logging
import math
import uuid

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.tools.misc import formatLang

# Return codes from _attempt_auto_topup.
TOPUP_OK = 'ok'
TOPUP_NO_SHORTFALL = 'no_shortfall'
TOPUP_NO_TOKEN = 'no_token'
TOPUP_PROVIDER_NOT_ENABLED = 'provider_not_enabled'
TOPUP_CURRENCY_NOT_CONFIGURED = 'currency_not_configured'
TOPUP_3DS_REQUIRED = '3ds_required'
TOPUP_SDK_ERROR = 'sdk_error'
TOPUP_WALLET_CREDIT_FAILED = 'wallet_credit_failed'

_logger = logging.getLogger(__name__)


class ShSubscriptionFix(models.Model):
    """Fix upstream sh_subscription bug in _sh_create_auto_invoice.

    The original method uses `self._generate_invoice_vals()` inside a loop
    over `subscription` records, but since it's an @api.model method, `self`
    is an empty recordset — producing invoice vals with all NULL fields.

    This override replaces `self` with `subscription` so each invoice
    references the correct partner, journal, and product.
    """
    _inherit = 'sh.subscription.subscription'

    def _compute_unused_refund(self):
        """Pro-rata refund for the unused portion of the current paid cycle.

        Returns 0 when the customer has not yet paid for the current cycle
        (today < sh_start_date — still in trial, or sub not yet started).
        Without this guard the formula
        ``price * (end - today) / (end - start)`` overshoots when today is
        before start, producing refund > price (BUG-5).
        """
        self.ensure_one()
        if not self.sh_end_date or not self.sh_start_date:
            return 0
        today = fields.Date.today()
        if today < self.sh_start_date:
            return 0
        total_days = (self.sh_end_date - self.sh_start_date).days or 1
        remaining_days = max(0, (self.sh_end_date - today).days)
        return round(self.sh_plan_price * remaining_days / total_days, 2)

    @api.model
    def _sh_create_auto_invoice(self):
        """Daily auto-invoice cron with T19 dunning semantics.

        Behaviour:
        - Filter is ``next_payment <= today`` so a sub whose previous tick
          left ``next_payment`` un-advanced (failed payment) keeps being
          retried until either resolution or auto-cancel.
        - Existing unpaid invoice for the sub is reused; we never stack a
          second invoice on top while the first is outstanding.
        - ``next_payment`` only advances on successful payment (Decision 1).
        - Counter increments on each failed retry; resets on success.
        - At ``threshold`` consecutive failures the sub is auto-cancelled
          and a one-shot cancellation email is queued (Decision 2/4). The
          existing ``subscription_lifecycle_cron`` then suspends K8s
          resources.
        """
        today = fields.Date.today()
        Subscription = self.env['sh.subscription.subscription']
        # Decision 1 follow-through: ``<= today`` (not ``==``) so a stuck
        # next_payment continues to be processed daily.
        subscriptions = Subscription.sudo().search([
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '<=', today),
        ])
        threshold = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.max_consecutive_payment_failures', '6'))
        _logger.info(
            "[SUB_FIX] _sh_create_auto_invoice scan due_or_overdue=%s threshold=%s",
            len(subscriptions), threshold,
        )
        for subscription in subscriptions:
            self._process_subscription_billing(subscription, threshold)

    def _process_subscription_billing(self, subscription, threshold):
        """Per-subscription billing for one cron tick. Isolated for testability."""
        today = fields.Date.today()
        partner = subscription.sh_partner_id
        _logger.info(
            "[SUB_FIX] auto_invoice start sub=%s partner=%s price=%s "
            "balance=%s failures=%s",
            subscription.id, partner.id, subscription.sh_plan_price,
            partner.wallet_balance,
            subscription.woow_consecutive_payment_failures,
        )
        try:
            with self.env.cr.savepoint():
                invoice, is_new = self._get_or_create_cycle_invoice(subscription)
                paid = self._try_settle_invoice(subscription, invoice)

                if paid:
                    self._on_payment_success(subscription, invoice, today)
                    if is_new:
                        self._send_invoice_pdf(subscription, invoice)
                else:
                    self._on_payment_failure(
                        subscription, invoice, today, threshold,
                    )
        except Exception:
            _logger.exception(
                'Auto-invoice failed for subscription %s; continuing',
                subscription.name,
            )

    def _get_or_create_cycle_invoice(self, subscription):
        """Return (invoice, is_new). Reuse the outstanding invoice if any."""
        existing = self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', subscription.id),
            ('payment_state', 'not in', ('paid', 'in_payment')),
            ('state', '!=', 'cancel'),
            ('move_type', '=', 'out_invoice'),
        ], order='invoice_date desc, id desc', limit=1)
        if existing:
            _logger.info(
                "[SUB_FIX] auto_invoice reuse sub=%s invoice=%s amount=%s",
                subscription.id, existing.id, existing.amount_total,
            )
            return existing, False

        invoice_vals = subscription._generate_invoice_vals()
        invoice = self.env['account.move'].sudo().create(invoice_vals)
        subscription.compute_view_invoice()
        _logger.info(
            "[SUB_FIX] auto_invoice created sub=%s invoice=%s amount=%s",
            subscription.id, invoice.id, invoice.amount_total,
        )
        return invoice, True

    def _try_settle_invoice(self, subscription, invoice):
        """Attempt wallet payment, then auto-topup. Returns True on success."""
        partner = subscription.sh_partner_id
        if partner.wallet_balance >= invoice.amount_total:
            subscription._handle_invoice_creation(invoice)
            return True

        shortfall = invoice.amount_total - partner.wallet_balance
        topup_ok, topup_msg = self._attempt_auto_topup(partner, shortfall)
        if topup_ok:
            _logger.info(
                'Auto-topup covered shortfall=%.2f for subscription %s; '
                'proceeding with invoice %s',
                shortfall, subscription.name, invoice.name,
            )
            subscription._handle_invoice_creation(invoice)
            return True

        _logger.warning(
            "Insufficient wallet balance for subscription %s and "
            "auto-topup failed (%s): balance=%.2f, required=%.2f — "
            "invoice %s left unpaid",
            subscription.name, topup_msg, partner.wallet_balance,
            invoice.amount_total, invoice.name,
        )
        return False

    def _on_payment_success(self, subscription, invoice, today):
        """Advance next_payment, reset dunning counter."""
        unit_deltas = {
            'day': relativedelta(days=subscription.sh_recurrency),
            'week': relativedelta(weeks=subscription.sh_recurrency),
            'month': relativedelta(months=subscription.sh_recurrency),
            'year': relativedelta(years=subscription.sh_recurrency),
        }
        delta = unit_deltas.get(subscription.sh_unit)
        vals = {}
        if delta:
            vals['sh_date_of_next_payment'] = today + delta
        if subscription.woow_consecutive_payment_failures:
            vals['woow_consecutive_payment_failures'] = 0
        if vals:
            subscription.write(vals)
        _logger.info(
            "[SUB_FIX] auto_invoice paid sub=%s invoice=%s next_payment=%s",
            subscription.id, invoice.name,
            vals.get('sh_date_of_next_payment',
                     subscription.sh_date_of_next_payment),
        )

    def _on_payment_failure(self, subscription, invoice, today, threshold):
        """Increment counter, possibly auto-cancel + notify."""
        new_count = subscription.woow_consecutive_payment_failures + 1
        subscription.write({
            'woow_consecutive_payment_failures': new_count,
            'woow_last_payment_failure_at': fields.Datetime.now(),
        })

        # Existing per-cycle low-balance warning, deduped on the original
        # next_payment date so retries on the same cycle don't re-spam.
        partner = subscription.sh_partner_id
        warn_key = subscription.sh_date_of_next_payment or today
        if subscription.woow_last_low_balance_warned_for != warn_key:
            self._notify_low_balance(partner, invoice)
            subscription.write({'woow_last_low_balance_warned_for': warn_key})

        # Threshold check (Decision 4: hard-cancel; Decision 3: 0 = disabled).
        if threshold and new_count >= threshold:
            _logger.warning(
                "[SUB_FIX] auto_cancel sub=%s after %s consecutive failures",
                subscription.id, new_count,
            )
            subscription.write({'state': 'cancel'})
            if not subscription.woow_cancellation_notified:
                self._notify_subscription_cancelled(subscription, invoice)
                subscription.write({'woow_cancellation_notified': True})
        else:
            _logger.info(
                "[SUB_FIX] auto_invoice failure recorded sub=%s failures=%s/%s",
                subscription.id, new_count, threshold or '∞',
            )

    def _send_invoice_pdf(self, subscription, invoice):
        """Render + email invoice PDF. First-creation only, never on retry."""
        try:
            pdf_content, _content_type = self.env[
                'ir.actions.report'
            ].sudo()._render_qweb_pdf('account.account_invoices', invoice.id)
            attachment = self.env['ir.attachment'].sudo().create({
                'name': 'Subscription Invoice',
                'type': 'binary',
                'datas': base64.b64encode(pdf_content),
                'mimetype': 'application/pdf',
                'res_id': invoice.id,
                'res_model': 'account.move',
            })
            subscription._sh_send_subscription_email(attachment)
            _logger.info(
                "Auto-invoice created for subscription %s (amount=%s, next=%s)",
                subscription.name, invoice.amount_total,
                subscription.sh_date_of_next_payment,
            )
        except Exception:
            _logger.warning(
                "Failed to generate/send invoice PDF for subscription %s",
                subscription.name, exc_info=True,
            )

    @api.model
    def _notify_low_balance(self, partner, invoice):
        """Send a low-wallet-balance email to the partner.

        Called when an auto-invoice cannot be paid because wallet balance
        is insufficient.
        """
        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("mail_template_low_balance not found; skip notification")
            return

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        amount_text = formatLang(
            self.env, invoice.amount_total, currency_obj=invoice.currency_id,
        )
        balance_text = formatLang(
            self.env, partner.wallet_balance,
            currency_obj=partner.company_id.currency_id,
        )
        ctx = {
            'invoice_name': invoice.name or '',
            'amount_text': amount_text,
            'balance_text': balance_text,
            'topup_url': '%s/woow#/billing/topup' % base_url.rstrip('/'),
        }
        try:
            template.with_context(**ctx).send_mail(
                partner.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_light',
            )
            _logger.info("Low-balance notification queued for partner %s", partner.id)
        except Exception:
            _logger.exception(
                "Failed to queue low-balance notification for partner %s",
                partner.id,
            )

    @api.model
    def _notify_subscription_cancelled(self, subscription, invoice):
        """Send a one-shot "subscription auto-cancelled" email to the partner.

        Called once when ``woow_consecutive_payment_failures`` crosses the
        configured threshold and the sub flips to ``state='cancel'``. Dedup
        is the caller's responsibility via ``woow_cancellation_notified``.
        """
        template = self.env.ref(
            'woow_paas_platform.mail_template_subscription_cancelled',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "mail_template_subscription_cancelled not found; skip notification"
            )
            return

        partner = subscription.sh_partner_id
        cleanup_months = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.cleanup_after_months', '6'))
        amount_text = formatLang(
            self.env, invoice.amount_total, currency_obj=invoice.currency_id,
        )
        ctx = {
            'subscription_name': subscription.name or '',
            'plan_name': (
                subscription.sh_subscription_plan_id.name
                if subscription.sh_subscription_plan_id else ''
            ),
            'invoice_name': invoice.name or '',
            'amount_text': amount_text,
            'failures': subscription.woow_consecutive_payment_failures,
            'preservation_months': cleanup_months,
        }
        try:
            template.with_context(**ctx).send_mail(
                partner.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_light',
            )
            _logger.info(
                "[SUB_FIX] cancellation email queued partner=%s sub=%s",
                partner.id, subscription.id,
            )
        except Exception:
            _logger.exception(
                "Failed to queue cancellation email for partner %s sub=%s",
                partner.id, subscription.id,
            )

    def _handle_invoice_creation(self, invoice_id):
        """Override upstream: when wallet can cover, post + pay from wallet.

        Upstream's two modes (``sh_invoice_generated='post'`` posts only,
        ``='paid'`` posts + creates a dummy inbound payment) both leave the
        wallet untouched. For our platform the wallet IS the payment source,
        so we bypass both modes and call ``account.move._pay_from_wallet``
        when the balance can cover — decrementing the wallet, creating a
        real ``account.payment``, and reconciling.

        Defensive ``super()`` fallback handles the (should-not-happen) case
        where the caller didn't pre-check balance; the normal cron path via
        ``_sh_create_auto_invoice`` guarantees sufficiency before this runs.
        """
        partner = self.sh_partner_id
        amount = invoice_id.amount_total or 0
        _logger.info(
            "[SUB_FIX] _handle_invoice_creation sub=%s invoice=%s amount=%s partner=%s balance=%s",
            self.id, invoice_id.id, amount,
            partner.id if partner else None,
            partner.wallet_balance if partner else None,
        )
        if partner and amount > 0 and partner.wallet_balance >= amount:
            _logger.info(
                "[SUB_FIX] _handle_invoice_creation pay from wallet sub=%s invoice=%s",
                self.id, invoice_id.id,
            )
            invoice_id._pay_from_wallet(subscription=self)
            return
        _logger.info(
            "[SUB_FIX] _handle_invoice_creation fallback super sub=%s invoice=%s",
            self.id, invoice_id.id,
        )
        return super()._handle_invoice_creation(invoice_id)

    @api.model
    def _attempt_auto_topup(self, partner, shortfall):
        """Charge the partner's default bound card to top up the wallet.

        Args:
            partner: res.partner record
            shortfall: float amount needed (TWD)

        Returns:
            (True, 'ok')           — charge succeeded, wallet credited
            (False, 'no_shortfall')— shortfall <= 0
            (False, 'no_token')    — partner has no usable default ECPG token
            (False, 'provider_not_enabled') — token's provider does not have enable_ecpg
            (False, 'currency_not_configured') — neither provider nor TWD available
            (False, '3ds_required')— ECPG returned ThreeDURL; cron cannot complete 3DS
            (False, '<state>')     — payment.transaction ended in a non-done state
            (False, 'sdk_error')   — unexpected exception from _send_payment_request
            (False, 'wallet_credit_failed') — ECPay charged but wallet credit failed;
                                              MANUAL RECONCILIATION REQUIRED
        """
        _logger.info(
            "[AUTO_TOPUP] _attempt_auto_topup partner=%s shortfall=%s",
            partner.id, shortfall,
        )
        if shortfall <= 0:
            return False, TOPUP_NO_SHORTFALL

        # ECPay requires TotalAmount as a positive integer TWD. Ceil so the wallet
        # covers the invoice even if the shortfall has a fractional TWD component.
        charge_amount = int(math.ceil(shortfall))

        # Default-token preference: Odoo Enterprise's account_payment adds a
        # company-dependent `property_payment_token_id` on res.partner. On
        # community installs that field is absent, so we fall back to the most
        # recently created active ECPG-bound token for this partner. The
        # set-default UI is a no-op without the Enterprise field — that is a
        # known limitation tracked separately for Phase 2c.
        token = self.env['payment.token'].sudo()
        if 'property_payment_token_id' in partner._fields:
            token = partner.with_company(
                partner.company_id or self.env.company
            ).sudo().property_payment_token_id
        if not token or not token.active or not token.ecpg_bindcard_id:
            token = self.env['payment.token'].sudo().search(
                [
                    ('partner_id', '=', partner.id),
                    ('active', '=', True),
                    ('ecpg_bindcard_id', '!=', False),
                ],
                order='id desc',
                limit=1,
            )

        if not token or not token.active or not token.ecpg_bindcard_id:
            _logger.info(
                "[AUTO_TOPUP] no usable token partner=%s", partner.id,
            )
            return False, TOPUP_NO_TOKEN

        provider = token.provider_id
        if not provider or not getattr(provider, 'enable_ecpg', False):
            _logger.info(
                "[AUTO_TOPUP] provider not ECPG partner=%s token=%s provider=%s",
                partner.id, token.id, provider.code if provider else None,
            )
            return False, TOPUP_PROVIDER_NOT_ENABLED

        currency = provider.main_currency_id or self.env.ref(
            'base.TWD', raise_if_not_found=False,
        )
        if not currency:
            return False, TOPUP_CURRENCY_NOT_CONFIGURED

        # 20-char limit: put the unique portion first (uuid) so ecpay's
        # _ecpg_build_merchant_trade_no truncation preserves uniqueness.
        # Matches upstream's BIND{uuid16} pattern.
        reference = f'TOPUP{uuid.uuid4().hex[:15].upper()}'
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': token.payment_method_id.id,
            'reference': reference,
            'amount': float(charge_amount),
            'currency_id': currency.id,
            'partner_id': partner.id,
            'token_id': token.id,
            'operation': 'offline',
        })
        _logger.info(
            "[AUTO_TOPUP] tx created partner=%s tx=%s ref=%s amount=%s token=%s",
            partner.id, tx.id, reference, charge_amount, token.id,
        )

        try:
            result = tx._send_payment_request()
        except Exception as e:
            _logger.exception(
                '[AUTO_TOPUP] send_payment_request failed partner=%s tx=%s: %s',
                partner.id, tx.id, e,
            )
            return False, TOPUP_SDK_ERROR

        # 3DS redirect — cron cannot drive it. Treat as failure.
        if isinstance(result, dict) and result.get('redirect_url'):
            _logger.info(
                '[AUTO_TOPUP] 3DS required partner=%s tx=%s; aborting',
                partner.id, tx.id,
            )
            return False, TOPUP_3DS_REQUIRED

        if tx.state != 'done':
            _logger.warning(
                '[AUTO_TOPUP] tx not done partner=%s tx=%s state=%s',
                partner.id, tx.id, tx.state,
            )
            return False, tx.state

        # ECPay has already charged the card. A subsequent failure during
        # add_wallet_points must NOT propagate — otherwise the outer cron
        # transaction rolls back, the payment.transaction row disappears, and
        # the next cron tick will charge the card AGAIN.
        #
        # We catch any wallet-credit failure, log it as a critical operational
        # event (manual reconciliation required), and return a dedicated
        # failure code so the caller does not retry this run.
        try:
            partner.sudo().add_wallet_points(
                float(charge_amount),
                description='Auto top-up for subscription renewal',
                payment_transaction=tx,
            )
        except Exception:
            _logger.exception(
                'CRITICAL: auto-topup charged at ECPay (tx=%s, charge_amount=%.2f) '
                'but wallet credit failed; manual reconciliation required '
                '(partner=%s)',
                tx.reference, charge_amount, partner.id,
            )
            return False, TOPUP_WALLET_CREDIT_FAILED

        # Invalidate the non-stored compute so the caller's subsequent
        # `_handle_invoice_creation` (which re-reads partner.wallet_balance to
        # decide pay-from-wallet vs fallback) sees the post-credit balance.
        # Without this, the cron charges the user's card AND leaves the
        # invoice unpaid because it reads the pre-credit cached balance.
        partner.invalidate_recordset(['wallet_balance'])
        _logger.info(
            '[AUTO_TOPUP] succeeded partner=%s charge_amount=%.2f tx=%s new_balance=%s',
            partner.id, charge_amount, tx.reference, partner.wallet_balance,
        )
        return True, TOPUP_OK
