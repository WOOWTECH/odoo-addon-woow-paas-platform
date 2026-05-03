import base64
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.misc import formatLang

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
        # Carry the partner's invoice carrier preference into the move so the
        # ECPay issuing cron has everything it needs at post time.
        invoice_vals.update(
            subscription.sh_partner_id._woow_partner_invoice_vals()
        )
        # Path B: cycle invoice 由 wallet 扣款結算（儲值時已開過 ECPay 發票），
        # 不重複開發票。woow_skip_upstream_hook 隔離上游 inline hook
        # (即使 company.auto_invoice='automatic' 也不會被觸發開立)。
        # 詳見 docs/architecture/billing-philosophy.md Principle 3-5。
        invoice_vals['woow_invoice_issue_state'] = 'not_required'
        invoice_vals['woow_skip_upstream_hook'] = True
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
        """[DEPRECATED] Thin wrapper around wallet.topup.charge_via_token.

        Kept for backwards compatibility with existing tests and the existing
        ``_try_settle_invoice`` call site. New code should call wallet.topup
        directly via ``create_auto_topup()`` + ``charge_via_token()``.

        Returns ``(ok, msg)`` — same contract as before. The actual ECPay
        charge, wallet credit, and invoice creation are delegated to
        ``wallet.topup``.

        Return codes (preserved from original implementation):

        - ``'ok'``                          — charge succeeded, wallet credited
        - ``'no_shortfall'``                — shortfall <= 0
        - ``'no_token'``                    — no usable default ECPG token
        - ``'provider_not_enabled'``        — token's provider missing enable_ecpg
        - ``'currency_not_configured'``     — neither provider nor TWD available
        - ``'3ds_required'``                — ECPG returned ThreeDURL
        - ``'<state>'``                     — tx ended in non-done state
        - ``'sdk_error'``                   — exception from _send_payment_request
        - ``'wallet_credit_failed'``        — ECPay charged but wallet credit failed
        - ``'invalid_state'``               — topup state guard (concurrent caller)

        TODO: remove after one stable release once all callers migrated.
        Tracked in: ``docs/plans/2026-05-03-invoice-issue-fix-path-b.md``
        "Follow-up tickets" section.
        """
        _logger.info(
            '[AUTO_TOPUP] delegating to wallet.topup partner=%s shortfall=%s',
            partner.id, shortfall,
        )
        if shortfall <= 0:
            return False, 'no_shortfall'

        token = self._resolve_default_token(partner)
        if not token:
            return False, 'no_token'

        # base.TWD missing or other factory-time validation failure: degrade
        # to the documented `currency_not_configured` code so the contract
        # advertised in this docstring stays accurate.
        try:
            topup = self.env['woow.wallet.topup'].sudo().create_auto_topup(
                partner=partner, amount=shortfall,
            )
        except (ValidationError, ValueError):
            _logger.exception(
                '[AUTO_TOPUP] create_auto_topup failed partner=%s shortfall=%s',
                partner.id, shortfall,
            )
            return False, 'currency_not_configured'
        return topup.charge_via_token(token)

    def _resolve_default_token(self, partner):
        """Find the default ECPG-bound payment token for ``partner``.

        Phase 2c known limitation: community ed has no
        ``property_payment_token_id`` so we fall back to the most recently
        created active ECPG-bound token.

        Returns: ``payment.token`` record (or ``None`` if none found).
        """
        Token = self.env['payment.token'].sudo()
        token = Token
        if 'property_payment_token_id' in partner._fields:
            token = partner.with_company(
                partner.company_id or self.env.company,
            ).sudo().property_payment_token_id
        if not token or not token.active or not token.ecpg_bindcard_id:
            token = Token.search(
                [
                    ('partner_id', '=', partner.id),
                    ('active', '=', True),
                    ('ecpg_bindcard_id', '!=', False),
                ],
                order='id desc', limit=1,
            )
        return token if token and token.active and token.ecpg_bindcard_id else None
