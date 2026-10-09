"""#1599：訂閱回到 in_progress 時，請付款後復權的 cron 立即復權它底下停權中的雲端服務。

平台程式沒有任何地方直接寫 ``state = 'in_progress'``；會走到這裡的是上游的
``sh_subscription_confirm()``（後台確認續約子訂閱、換方案／開通時的新訂閱）與後台人工把
訂閱改回 in_progress。付款入帳那一半在 ``account.move._pay_from_wallet``。兩邊都只呼叫
``subscription_lifecycle_cron._request_paid_resume``：寫旗標＋排一次 cron，不呼叫 operator
（仍在付款／確認的交易內）；真正能不能復權（例如發票付清了沒）由 cron 在 commit 之後判斷。
"""
from odoo import models


class ShSubscriptionResumeTrigger(models.Model):
    _inherit = 'sh.subscription.subscription'

    def write(self, vals):
        reactivated = self.browse()
        if vals.get('state') == 'in_progress':
            reactivated = self.filtered(lambda sub: sub.state != 'in_progress')
        res = super().write(vals)
        if reactivated:
            lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron'].sudo()
            lifecycle._request_paid_resume(reactivated, source='subscription_in_progress')
        return res
