"""Cron: warn users before subscription renewal if wallet balance is low.

TODO(phase-1 follow-up): add integration test that seeds an
sh.subscription.subscription with sh_date_of_next_payment within
WARNING_WINDOW_DAYS and asserts dedup via woow_last_low_balance_warned_for.
"""
import logging
from datetime import timedelta

from odoo import fields, models
from odoo.tools.misc import formatLang

_logger = logging.getLogger(__name__)


class LowBalanceWarningCron(models.AbstractModel):
    _name = 'woow_paas_platform.low_balance_warning_cron'
    _description = 'Pre-renewal Low Balance Warning'

    WARNING_WINDOW_DAYS = 3

    def _cron_warn_upcoming_renewal_low_balance(self):
        """Scan subscriptions renewing within WARNING_WINDOW_DAYS.

        For each subscription whose partner's wallet balance is below
        the plan price, send a single warning email per renewal cycle.
        Dedup via `sh.subscription.subscription.woow_last_low_balance_warned_for`.
        """
        today = fields.Date.today()
        cutoff = today + timedelta(days=self.WARNING_WINDOW_DAYS)

        Subscription = self.env['sh.subscription.subscription'].sudo()
        subs = Subscription.search([
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '>=', today),
            ('sh_date_of_next_payment', '<=', cutoff),
        ])
        _logger.info(
            "[LOW_BAL] pre-renewal check today=%s cutoff=%s subs_in_window=%s",
            today, cutoff, len(subs),
        )
        for sub in subs:
            try:
                with self.env.cr.savepoint():
                    self._warn_if_needed(sub)
            except Exception:
                _logger.exception(
                    "[LOW_BAL] warning failed for subscription %s", sub.id,
                )

    def _warn_if_needed(self, subscription):
        partner = subscription.sh_partner_id
        if not partner or not partner.email:
            return
        price = subscription.sh_plan_price or 0.0
        if price <= 0:
            return
        _logger.info(
            "[LOW_BAL] eval sub=%s partner=%s price=%s balance=%s next=%s",
            subscription.id, partner.id, price, partner.wallet_balance,
            subscription.sh_date_of_next_payment,
        )
        if partner.wallet_balance >= price:
            return

        # Dedup: only warn once per renewal cycle.
        next_payment = subscription.sh_date_of_next_payment
        if not next_payment:
            return
        last_warned = subscription.woow_last_low_balance_warned_for
        if last_warned and last_warned == next_payment:
            return

        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "mail_template_low_balance not found; skip pre-renewal warning"
            )
            return

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        ctx = {
            'invoice_name': '(upcoming renewal on %s)' % next_payment,
            'amount_text': formatLang(
                self.env, price,
                currency_obj=partner.company_id.currency_id,
            ),
            'balance_text': formatLang(
                self.env, partner.wallet_balance,
                currency_obj=partner.company_id.currency_id,
            ),
            'topup_url': '%s/woow#/billing/topup' % base_url.rstrip('/'),
        }
        template.with_context(**ctx).send_mail(
            partner.id,
            force_send=False,
            email_layout_xmlid='mail.mail_notification_light',
        )
        # Persist dedup marker
        subscription.write({'woow_last_low_balance_warned_for': next_payment})
        _logger.info(
            "[LOW_BAL] warning queued partner=%s sub=%s renewal=%s price=%.2f balance=%.2f",
            partner.id, subscription.id, next_payment, price, partner.wallet_balance,
        )
