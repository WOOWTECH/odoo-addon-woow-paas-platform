import logging
from datetime import timedelta

from odoo import fields, models

_logger = logging.getLogger(__name__)

# 同一個 BindCardID 只能有一張 **active** token。partial（只看 active）是刻意的：
# 卡被封存（刪卡）後若 ECPay 端刪除同步失敗、使用者又綁同一張卡，ECPay 可能回
# 同一個 BindCardID（IsSameCard），vendored 冪等查詢只看 active ⇒ 會建一張新的
# active token；全表 unique 會讓這張卡永遠「綁成功卻看不到」。
ECPG_BINDCARD_UNIQUE_INDEX = 'payment_token_woow_ecpg_bindcard_active_uniq'

# 自動扣款被 ECPay 拒絕（或要求 3D）後，同一張卡在這段時間內不再自動授權。
# ECPay 官方：交易失敗後不要對同一個 BindCardID 反覆重新授權（連續失敗可能被
# 暫停授權）。拒絕後等待完整 7 天才恢復自動授權；使用者用這張卡手動儲值成功
# 會立即解除，新的拒絕則重新起算 7 天。
AUTO_CHARGE_DECLINE_BLOCK_DAYS = 7


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    woow_auto_charge_blocked_at = fields.Datetime(
        copy=False, readonly=True,
        help='Set when ECPay declined a charge on this card (or demanded 3-D '
             'Secure that a cron charge cannot complete). Automatic top-ups '
             'skip this card until a successful charge clears it or the '
             'block window passes.',
    )
    woow_auto_charge_block_code = fields.Char(copy=False, readonly=True)

    def init(self):
        """``ecpg_bindcard_id`` 的 partial unique index（active 限定）。

        既有資料若已有重複的 active BindCardID，**不建 index、不讓升級失敗**
        （只 log error）——上線當天不能因為歷史資料把整個 -u 卡死；重複列要
        人工處理後下次升級自動補建。
        """
        parent_init = getattr(super(), 'init', None)
        if parent_init:
            parent_init()
        cr = self.env.cr
        cr.execute(
            "SELECT 1 FROM pg_indexes WHERE indexname = %s",
            (ECPG_BINDCARD_UNIQUE_INDEX,))
        if cr.fetchone():
            return
        cr.execute("""
            SELECT ecpg_bindcard_id, COUNT(*) FROM payment_token
            WHERE ecpg_bindcard_id IS NOT NULL AND active
            GROUP BY ecpg_bindcard_id HAVING COUNT(*) > 1
        """)
        duplicates = cr.fetchall()
        if duplicates:
            _logger.error(
                '[PAY_TOKEN] %s active BindCardID value(s) are duplicated; '
                'unique index %s NOT created — archive the duplicates and '
                'upgrade again', len(duplicates), ECPG_BINDCARD_UNIQUE_INDEX,
            )
            return
        cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS %s ON payment_token '
            '(ecpg_bindcard_id) WHERE ecpg_bindcard_id IS NOT NULL AND active'
            % ECPG_BINDCARD_UNIQUE_INDEX
        )

    def _woow_auto_charge_blocked(self):
        """這張卡現在是否不可自動扣款（被拒絕後的冷卻期內）。"""
        self.ensure_one()
        blocked_at = self.woow_auto_charge_blocked_at
        if not blocked_at:
            return False
        cutoff = fields.Datetime.now() - timedelta(
            days=AUTO_CHARGE_DECLINE_BLOCK_DAYS)
        return blocked_at > cutoff

    def _woow_block_auto_charge(self, code):
        """記下「ECPay 拒絕了這張卡」；自動扣款在冷卻期內不再碰它。"""
        for token in self.sudo():
            token.write({
                'woow_auto_charge_blocked_at': fields.Datetime.now(),
                'woow_auto_charge_block_code': (code or 'declined')[:64],
            })
            _logger.warning(
                '[PAY_TOKEN] auto-charge blocked token=%s partner=%s code=%s '
                '(no automatic re-authorisation for %s days)',
                token.id, token.partner_id.id, code,
                AUTO_CHARGE_DECLINE_BLOCK_DAYS,
            )

    def _woow_clear_auto_charge_block(self):
        tokens = self.sudo().filtered('woow_auto_charge_blocked_at')
        if tokens:
            tokens.write({
                'woow_auto_charge_blocked_at': False,
                'woow_auto_charge_block_code': False,
            })
            _logger.info('[PAY_TOKEN] auto-charge block cleared tokens=%s',
                         tokens.ids)

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
