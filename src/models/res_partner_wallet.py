from odoo import _, models
from odoo.exceptions import UserError


class ResPartnerWallet(models.Model):
    """退點（refund）進錢包的 API。

    upstream ``add_wallet_points`` 把 ``transaction_type`` 硬編成 ``'topup'``
    且無型別參數，導致換 plan／刪服務的退點在對帳報表上與加值混在一起無法
    分辨。此 method 建立 ``transaction_type='refund'`` 的交易，並可帶
    ``subscription_id`` 供對帳追溯是哪張訂閱退的點。加點邏輯與 upstream
    ``add_wallet_points`` 一致（loyalty.card.points += amount）。
    """
    _inherit = 'res.partner'

    def refund_wallet_points(self, amount, description=None, subscription=None):
        self.ensure_one()
        if amount <= 0:
            raise UserError(_('Refund amount must be greater than 0'))
        loyalty_card = self.get_wallet_loyalty_card()
        if not loyalty_card:
            raise UserError(_('Unable to get wallet loyalty card'))
        loyalty_card.points += amount
        self.env['wallet.transaction'].create({
            'partner_id': self.id,
            'transaction_type': 'refund',
            'amount': amount,
            'description': description or _('Points refund'),
            'subscription_id': subscription.id if subscription else False,
            'balance_after': loyalty_card.points,
        })
        return True
