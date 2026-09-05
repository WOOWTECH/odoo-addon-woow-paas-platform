import logging

from odoo import models

_logger = logging.getLogger(__name__)


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
            'payment_method_id': self.payment_method_id.id,
            'partner_id': self.partner_id.id,
            'amount': amount,
            'currency_id': currency_id,
            'reference': reference,
            'token_id': self.id,
            'operation': 'online_token',
        }
        if purpose_field and purpose_id and purpose_field in Tx._fields:
            vals[purpose_field] = purpose_id

        _logger.info(
            "[PAY_TOKEN] _charge start token=%s partner=%s amount=%s ref=%s purpose=%s:%s provider=%s",
            self.id, self.partner_id.id, amount, reference,
            purpose_field, purpose_id, self.provider_id.code,
        )
        tx = Tx.create(vals)
        _logger.info(
            "[PAY_TOKEN] tx created token=%s tx=%s ref=%s initial_state=%s",
            self.id, tx.id, reference, tx.state,
        )

        # #864 PR-F：外呼前凍結所有 durable incident 所需純 scalar。except
        # 區塊不可 ORM read：若外層已 aborted，任何 lazy field access 都會再炸。
        manual_snapshot = None
        if purpose_field == 'woow_topup_id' and purpose_id:
            topup = self.env['woow.wallet.topup'].sudo().browse(purpose_id).exists()
            if topup:
                self.env.cr.execute('SELECT pg_current_xact_id()::text')
                origin_xid = self.env.cr.fetchone()[0]
                manual_snapshot = {
                    'external_ref': reference,
                    'kind': 'charged_not_credited',
                    'partner_ref': topup.partner_id.id or None,
                    'payment_token_ref': self.id,
                    'amount': float(amount),
                    'credits': topup.credits,
                    'bonus': topup.bonus,
                    'currency': topup.currency_id.name or None,
                    'failure_code': 'wallet_credit_failed',
                    'source_model': topup._name,
                    'source_ref': topup.id,
                    'origin_xid': origin_xid,
                    'topup_plan_ref': topup.plan_id.id or None,
                    'topup_source': topup.source,
                    'detail': 'ECPay 已確認扣款但 manual top-up 入帳 callback 失敗。',
                }
        try:
            send_result = tx._send_payment_request()
        except Exception as exc:
            _logger.exception(
                "[PAY_TOKEN] _send_payment_request exception token=%s tx=%s",
                self.id, tx.id,
            )
            # 只讀 Odoo Environment cache，不觸發 SQL。cache 沒有 ``done``
            # 就 fail-closed 留在 sdk_error，不猜測 provider 是否已扣款。
            cached_tx_state = tx.env.cache.get(
                tx, tx._fields['state'], None)
            if manual_snapshot and cached_tx_state == 'done':
                self.env['woow.billing.incident']._log_charged_not_credited(
                    manual_snapshot)
                # Controller catch 後可嘗試寫 local marker；跨 seam 只帶 Python
                # marker，不帶任何 recordset。
                setattr(exc, '_woow_charged_not_credited', True)
            raise

        redirect_url = None
        if isinstance(send_result, dict):
            redirect_url = send_result.get('redirect_url')

        _logger.info(
            "[PAY_TOKEN] _charge done token=%s tx=%s final_state=%s has_redirect=%s send_result_type=%s",
            self.id, tx.id, tx.state, bool(redirect_url),
            type(send_result).__name__,
        )
        return {'tx': tx, 'redirect_url': redirect_url, 'state': tx.state}
