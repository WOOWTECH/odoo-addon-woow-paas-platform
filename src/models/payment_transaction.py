import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


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
        _logger.info(
            "[PAY_TX] _set_done called tx_ids=%s refs=%s topup_ids=%s msg=%r",
            self.ids, self.mapped('reference'), self.mapped('woow_topup_id').ids,
            (state_message or '')[:200],
        )
        res = super()._set_done(state_message=state_message, **kwargs)
        for tx in self:
            if tx.woow_topup_id and tx.woow_topup_id.state in ('draft', 'pending'):
                _logger.info(
                    "[PAY_TX] _set_done → topup._on_payment_done tx=%s ref=%s topup=%s topup_state=%s",
                    tx.id, tx.reference, tx.woow_topup_id.id, tx.woow_topup_id.state,
                )
                tx.woow_topup_id.sudo()._on_payment_done(tx)
            elif tx.woow_topup_id:
                _logger.info(
                    "[PAY_TX] _set_done skip topup callback (state=%s) tx=%s topup=%s",
                    tx.woow_topup_id.state, tx.id, tx.woow_topup_id.id,
                )
        return res

    def _set_error(self, state_message=None, **kwargs):
        _logger.info(
            "[PAY_TX] _set_error called tx_ids=%s refs=%s topup_ids=%s msg=%r",
            self.ids, self.mapped('reference'), self.mapped('woow_topup_id').ids,
            (state_message or '')[:200],
        )
        res = super()._set_error(state_message=state_message, **kwargs)
        for tx in self:
            if tx.woow_topup_id and tx.woow_topup_id.state in ('draft', 'pending'):
                _logger.info(
                    "[PAY_TX] _set_error → topup._on_payment_failed tx=%s ref=%s topup=%s",
                    tx.id, tx.reference, tx.woow_topup_id.id,
                )
                tx.woow_topup_id.sudo()._on_payment_failed(tx)
            elif tx.woow_topup_id:
                _logger.info(
                    "[PAY_TX] _set_error skip topup callback (state=%s) tx=%s topup=%s",
                    tx.woow_topup_id.state, tx.id, tx.woow_topup_id.id,
                )
        return res
