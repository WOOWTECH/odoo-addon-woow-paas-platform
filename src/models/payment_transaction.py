from odoo import fields, models


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    woow_topup_id = fields.Many2one(
        'woow.wallet.topup',
        ondelete='set null',
        index=True,
        help="When set, this transaction is a wallet top-up. "
             "_set_done/_set_error will drive the linked topup state.",
    )

    def _set_done(self, state_message=None, **kwargs):
        res = super()._set_done(state_message=state_message, **kwargs)
        for tx in self:
            if tx.woow_topup_id and tx.woow_topup_id.state in ('draft', 'pending'):
                tx.woow_topup_id.sudo()._on_payment_done(tx)
        return res

    def _set_error(self, state_message=None, **kwargs):
        res = super()._set_error(state_message=state_message, **kwargs)
        for tx in self:
            if tx.woow_topup_id and tx.woow_topup_id.state in ('draft', 'pending'):
                tx.woow_topup_id.sudo()._on_payment_failed(tx)
        return res
