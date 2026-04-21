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
