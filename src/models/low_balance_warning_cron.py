"""Cron: warn users before subscription renewal if wallet balance is low."""
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
        the plan price, send a single **delivered** warning email per renewal
        cycle. Dedup 判準與蓋章共用 ``sh.subscription.subscription`` 上的
        ``_low_balance_warning_needs_send`` / ``_stamp_low_balance_warning``
        ——本 cron 與 dunning 的 ``_on_payment_failure`` 是同一個 marker 的兩個
        寫入者，語意必須綁在一起（#389）。
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

        # Dedup: 每個續費週期只提醒一次——但「提醒過」的判準是**那封信還活著**，
        # 不是「我呼叫過通知函式」。
        #
        # 這裡是 ``woow_last_low_balance_warned_for`` 的**第二個**寫入者（另一個
        # 是 dunning 的 ``_on_payment_failure``），兩邊寫的是同一個欄位、同一個
        # key（``sh_date_of_next_payment``）。所以判準與蓋章一律走
        # ``sh.subscription.subscription`` 上的共用 helper——只改其中一個寫入者
        # 等於沒改：#389 的 prod 時序正是「D-3 這裡排信＋寫 marker（舊版不記
        # 載體）→ SMTP 掛 → D 日 dunning 讀到『有 marker 查無載體』判成已送達
        # → 整個週期靜音」。
        next_payment = subscription.sh_date_of_next_payment
        if not next_payment:
            return
        Subscription = self.env['sh.subscription.subscription']
        if not Subscription._low_balance_warning_needs_send(
                subscription, next_payment):
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
        mail = Subscription._queue_low_balance_mail(partner, ctx)
        if not mail:
            # 沒排進佇列（無模板／send_mail 丟例外）就不蓋 marker——明天這支
            # cron 還在視窗內就會再試一次。
            return
        Subscription._stamp_low_balance_warning(subscription, next_payment, mail)
        _logger.info(
            "[LOW_BAL] warning queued partner=%s sub=%s renewal=%s price=%.2f "
            "balance=%.2f mail=%s",
            partner.id, subscription.id, next_payment, price,
            partner.wallet_balance, mail.id,
        )
