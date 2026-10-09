"""#1599：訂閱回到 in_progress 時，請付款後復權的 cron 立即復權它底下停權中的雲端服務。

平台程式沒有任何地方直接寫 ``state = 'in_progress'``；會走到這裡的是上游的
``sh_subscription_confirm()``（後台確認續約子訂閱、換方案／開通時的新訂閱）與後台人工把
訂閱改回 in_progress。付款入帳那一半在 ``account.move._pay_from_wallet``。兩邊都只呼叫
``subscription_lifecycle_cron._request_paid_resume``：寫旗標＋排一次 cron，不呼叫 operator
（仍在付款／確認的交易內）；真正能不能復權（例如發票付清了沒）由 cron 在 commit 之後判斷。

#1610：「付清才復權」的人工出口 ``woow_resume_payment_confirmed``。線下付款、人工確認、
發票還沒在系統沖帳的客戶，由帳務管理員（``account.group_account_manager``）在訂閱上勾選，
勾了之後不看發票也照樣復權（判斷在 ``_paid_resume_block_reason``）。

* 只有帳務管理員設得了（欄位 ``groups``＋``write`` 守衛；平台內部的 sudo 路徑例外）。
* 審計：欄位 ``tracking``（誰、何時、前後值）＋勾選當下另記一則 chatter，列出當時未付清的
  發票。
* 範圍是「這一段 in_progress」：訂閱離開 in_progress（dunning 取消、到期、結束、續約）時自動
  清掉並記在 chatter——下次再回到 in_progress 要重新確認，不會變成永久免付款。
* 勾選的交易內只寫旗標並請付款後復權的 cron 立即處理 Cloud Service（同 #1599，不呼叫
  operator）；Security Access／Smart Home 由每日生命週期 cron 復權。
"""
from odoo import _, fields, models
from odoo.exceptions import AccessError

_MANUAL_RESUME_FIELD = 'woow_resume_payment_confirmed'
_BILLING_ADMIN_GROUP = 'account.group_account_manager'


class ShSubscriptionResumeTrigger(models.Model):
    _inherit = 'sh.subscription.subscription'

    woow_resume_payment_confirmed = fields.Boolean(
        string='Allow Resume (Payment Confirmed Manually)',
        copy=False,
        tracking=True,
        groups=_BILLING_ADMIN_GROUP,
        help='Tick after confirming the payment outside the system (for example a bank '
             'transfer that has not been reconciled yet). Suspended services on this '
             'subscription then resume even though its invoices are not paid in the '
             'system. Cleared automatically when the subscription leaves "In Progress". '
             'Only billing administrators can change it.',
    )

    def _check_manual_resume_rights(self):
        """#1610：只有帳務管理員（或平台內部的 sudo 路徑）能改人工出口。

        欄位的 ``groups`` 已經擋掉非帳務管理員的 ORM 寫入；這裡再明確擋一次，讓判準不依賴
        Odoo 欄位權限的實作細節，也給出看得懂的訊息。
        """
        if self.env.su or self.env.user.has_group(_BILLING_ADMIN_GROUP):
            return
        raise AccessError(_(
            'Only billing administrators can allow a suspended service to resume '
            'without a paid invoice.'))

    def write(self, vals):
        reactivated = self.browse()
        if vals.get('state') == 'in_progress':
            reactivated = self.filtered(lambda sub: sub.state != 'in_progress')
        leaving = self.browse()
        if vals.get('state') and vals['state'] != 'in_progress':
            leaving = self.filtered(lambda sub: sub.state == 'in_progress')
        confirming = self.browse()
        if _MANUAL_RESUME_FIELD in vals:
            self._check_manual_resume_rights()
            if vals[_MANUAL_RESUME_FIELD]:
                confirming = self.sudo().filtered(
                    lambda sub: not sub[_MANUAL_RESUME_FIELD]).with_env(self.env)
        res = super().write(vals)
        lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron'].sudo()
        for sub in confirming:
            unpaid = lifecycle._unpaid_subscription_invoices(sub)
            if unpaid:
                body = _(
                    'Payment confirmed manually by a billing administrator: suspended '
                    'services on this subscription may resume while it stays in progress. '
                    'Unpaid invoices at this moment: %(invoices)s.',
                    invoices=lifecycle._invoice_list_text(unpaid),
                )
            else:
                body = _(
                    'Payment confirmed manually by a billing administrator: suspended '
                    'services on this subscription may resume while it stays in progress. '
                    'There are no unpaid invoices at this moment.')
            sub.sudo().message_post(body=body)
        stale = leaving.sudo().filtered(_MANUAL_RESUME_FIELD)
        if stale:
            stale.write({_MANUAL_RESUME_FIELD: False})
            for sub in stale:
                sub.message_post(body=_(
                    '"Allow Resume (Payment Confirmed Manually)" was cleared because the '
                    'subscription is no longer in progress (%(state)s). Confirm the payment '
                    'again if it returns to in progress without being paid.',
                    state=sub.state,
                ))
        if reactivated:
            lifecycle._request_paid_resume(reactivated, source='subscription_in_progress')
        if confirming:
            lifecycle._request_paid_resume(confirming, source='manual_payment_confirmation')
        return res
