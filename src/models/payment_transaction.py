import logging

from odoo import _, fields, models
from odoo.exceptions import ValidationError

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
        # 必須在 ``super()`` 前拒絕：vendored webhook 若 catch 我方例外後仍
        # commit，super 已寫入的 state 就會落地，違反 reconstructed row
        # callback lifecycle 封閉不變式。
        for tx in self:
            topup = tx.woow_topup_id.sudo()
            if topup and topup.repair_incident_ref:
                raise ValidationError(_(
                    'Reconstructed wallet top-ups require the query-gated '
                    'repair process.'
                ))
        _logger.info(
            "[PAY_TX] _set_done called tx_ids=%s refs=%s topup_ids=%s msg=%r",
            self.ids, self.mapped('reference'), self.mapped('woow_topup_id').ids,
            (state_message or '')[:200],
        )
        res = super()._set_done(state_message=state_message, **kwargs)
        for tx in self:
            topup = tx.woow_topup_id.sudo()
            if topup and topup.state in ('draft', 'pending'):
                _logger.info(
                    "[PAY_TX] _set_done → topup._on_payment_done tx=%s ref=%s topup=%s topup_state=%s",
                    tx.id, tx.reference, topup.id, topup.state,
                )
                topup._on_payment_done(tx)
            elif topup:
                _logger.info(
                    "[PAY_TX] _set_done skip topup callback (state=%s) tx=%s topup=%s",
                    topup.state, tx.id, topup.id,
                )
        return res

    def _set_error(self, state_message=None, **kwargs):
        # 與 _set_done 對稱：reconstructed row 的任何 provider callback
        # 都不得改 lifecycle；只有 query-gated repair capability 可收斂。
        for tx in self:
            topup = tx.woow_topup_id.sudo()
            if topup and topup.repair_incident_ref:
                raise ValidationError(_(
                    'Reconstructed wallet top-ups require the query-gated '
                    'repair process.'
                ))
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
