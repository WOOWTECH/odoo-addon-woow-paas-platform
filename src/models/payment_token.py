from odoo import models


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    def _charge(self, amount, reference, currency_id=None,
                purpose_field=None, purpose_id=None):
        """Charge this bound payment token and return transaction info.

        For ECPG tokens this ends up calling ``create_payment_with_card_id``,
        which — per current ECPay spec — always produces a 3DS challenge.
        Callers must redirect the browser to ``redirect_url`` when truthy.

        Args:
            amount: charge amount (int/float; caller decides rounding)
            reference: unique string for ``payment.transaction.reference``
            currency_id: override; defaults to current company currency
            purpose_field: Many2one field on ``payment.transaction`` pointing
                to the domain object this charge fulfills (e.g. ``woow_topup_id``)
            purpose_id: id for that Many2one

        Returns:
            dict with keys:
                tx           -- payment.transaction recordset
                redirect_url -- str | None
                state        -- 'pending' | 'done' | 'error' | 'draft'
        """
        self.ensure_one()
        Tx = self.env['payment.transaction'].sudo()
        currency_id = currency_id or self.env.company.currency_id.id

        vals = {
            'provider_id': self.provider_id.id,
            'partner_id': self.partner_id.id,
            'amount': amount,
            'currency_id': currency_id,
            'reference': reference,
            'token_id': self.id,
            'operation': 'online_token',
        }
        if purpose_field and purpose_id and purpose_field in Tx._fields:
            vals[purpose_field] = purpose_id

        tx = Tx.create(vals)
        send_result = tx._send_payment_request()

        redirect_url = None
        if isinstance(send_result, dict):
            redirect_url = send_result.get('redirect_url')

        return {'tx': tx, 'redirect_url': redirect_url, 'state': tx.state}
