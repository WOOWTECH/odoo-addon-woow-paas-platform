import logging
from datetime import datetime, timedelta, timezone

import requests

from odoo import _, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import str2bool

from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import ECPGError
from odoo.addons.payment_ecpay_ecpg.sdk.utils import redact_host

from ..services.ecpg_v4 import (
    EcpgV4Client,
    ecpay_consumer_name,
    ecpay_consumer_phone,
)

_logger = logging.getLogger(__name__)

_TAIPEI_TZ = timezone(timedelta(hours=8))
_MERCHANT_TRADE_NO_MAX = 20

# Kill switch：手動儲值的 CreatePaymentWithCardID 是否帶頂層 OrderResultURL
# （預設帶）。正式站若 ECPay 對「Need3D=0 卻帶 OrderResultURL」有意見，不用
# 發版就能關掉；關掉的代價是萬一回 ThreeDURL，使用者驗證完回不到 /woow。
ECPG_TOPUP_ORDER_RESULT_URL_PARAM = (
    'woow_paas_platform.ecpg_topup_order_result_url_enabled')


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

    # ------------------------------------------------------------------
    # ECPay 站內付 2.0 V4：儲值扣款的送出點（平台側接手 vendored 的同一段）
    # ------------------------------------------------------------------

    def _woow_is_ecpg_topup_charge(self):
        """這筆是不是「用 ECPG 綁定卡替 ``woow.wallet.topup`` 扣款」。"""
        self.ensure_one()
        return bool(
            self.provider_code == 'ECPay'
            and self.woow_topup_id
            and self.token_id
            and self.token_id.ecpg_bindcard_id
        )

    def _send_payment_request(self):
        """儲值扣款改走平台實作；其他交易一律交回 vendored／上游。

        vendored ``payment_ecpay_ecpg`` 的實作有兩個 V4 上線缺口，而它不在本
        repo、不能改：

        1. CreatePaymentWithCardID 沒有頂層 ``OrderResultURL`` ⇒ 一旦 ECPay
           回 ThreeDURL（強制 3D／風控），使用者驗證完沒有地方回來。
        2. ``ConsumerInfo.Phone`` 只去掉非數字（``+886912…`` 會送成
           ``886912…``）。

        ⇒ 只有 ``woow_topup_id`` 有值的 ECPG token 交易（＝平台所有刷卡：手動
        儲值與訂閱短缺的自動儲值）走 ``_woow_ecpg_send_topup_payment``；
        vendored 自己的 portal／``ecpay.subscription`` 等交易**完全不受影響**。
        """
        if len(self) == 1 and self._woow_is_ecpg_topup_charge():
            return self._woow_ecpg_send_topup_payment()
        return super()._send_payment_request()

    def _woow_ecpg_topup_order_result_url(self):
        """手動儲值的瀏覽器回跳網址；自動儲值（cron，沒有瀏覽器）回 ``''``。"""
        self.ensure_one()
        topup = self.woow_topup_id
        if topup.source != 'manual':
            return ''
        ICP = self.env['ir.config_parameter'].sudo()
        if not str2bool(ICP.get_param(ECPG_TOPUP_ORDER_RESULT_URL_PARAM, 'True'),
                        default=True):
            return ''
        # 與綁卡 OrderResultURL 同一個來源：web.base.url 是使用者 session 所在的
        # 網域（provider 若綁了官網 website，get_base_url() 會是別的網域，回跳後
        # 讀不到 session）。
        base = (ICP.get_param('web.base.url') or self.provider_id.get_base_url()
                or '').rstrip('/')
        return f'{base}/api/billing/topup-result/{topup.id}'

    def _woow_ecpg_send_topup_payment(self):
        """CreatePaymentWithCardID（V4）——語意逐項對齊 vendored 的同名流程。

        與 vendored 不同的只有：頂層 ``OrderResultURL``（手動儲值）、電話／姓名
        正規化（``ecpay_consumer_phone``／``ecpay_consumer_name``；不合格式整筆
        會被拒）、``MerchantTradeDate`` 用台北時間。錯誤分類、
        ``_set_error`` 訊息格式（``ECPG error: RtnCode=…``，``_classify_failure``
        與卡片拒絕閘都靠它）、ThreeDURL → ``_set_pending`` ＋ redirect、
        其餘 → ``_set_done`` 全部照 vendored。未預期例外照 vendored 慣例**不攔**
        （由呼叫端依情境處理，見 ``payment_token._charge``／``charge_via_token``）。
        """
        self.ensure_one()
        provider = self.provider_id
        partner = self.partner_id
        token = self.token_id

        merchant_trade_no = self._ecpg_build_merchant_trade_no(self.reference)
        if not merchant_trade_no:
            merchant_trade_no = (
                f"TX{self.id}{int(datetime.now().timestamp())}"
            )[:_MERCHANT_TRADE_NO_MAX]
        self.ecpg_merchant_trade_no = merchant_trade_no

        consumer_info = {
            'MerchantMemberID': token.ecpg_member_id or str(partner.id),
            'Email': partner.email or '',
            'Phone': ecpay_consumer_phone(partner.phone),
            'Name': ecpay_consumer_name(partner.name),
            'CountryCode': '158',
            'Address': '',
        }
        base = provider.get_base_url().rstrip('/')
        order_info = {
            'MerchantTradeDate': datetime.now(_TAIPEI_TZ).strftime(
                '%Y/%m/%d %H:%M:%S'),
            'MerchantTradeNo': merchant_trade_no,
            'TotalAmount': int(self.amount),
            'ReturnURL': f'{base}/payment/ecpg/pay_callback',
            'TradeDesc': (self.reference or 'Payment')[:200],
            'ItemName': (self.reference or 'Payment')[:200],
        }
        order_result_url = self._woow_ecpg_topup_order_result_url()
        client = EcpgV4Client(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )
        _logger.info(
            "[PAY_TX] ECPG V4 topup charge tx=%s ref=%s topup=%s source=%s "
            "amount=%s order_result_url=%s",
            self.id, self.reference, self.woow_topup_id.id,
            self.woow_topup_id.source, order_info['TotalAmount'],
            bool(order_result_url),
        )
        try:
            result = client.create_payment_with_card_id_v4(
                bind_card_id=token.ecpg_bindcard_id,
                consumer_info=consumer_info,
                order_info=order_info,
                custom_field=self.reference,
                order_result_url=order_result_url,
            )
        except ECPGError as e:
            _logger.exception("ECPG payment failed: %s", e)
            self._set_error(_("ECPG error: %s") % redact_host(str(e)))
            return None
        except requests.exceptions.RequestException as e:
            _logger.warning("ECPG charge network error: %s", e)
            self._set_error(_("付款服務暫時無法連線，請稍後再試。"))
            return None

        if result.get('RtnCode') != 1:
            self._set_error(_("ECPG RtnMsg: %s") % result.get('RtnMsg'))
            return None

        trade_no = (result.get('OrderInfo') or {}).get('TradeNo') or ''
        if trade_no:
            self.provider_reference = trade_no

        three_d_url = (result.get('ThreeDInfo') or {}).get('ThreeDURL') or ''
        if three_d_url:
            # 最終狀態交給 pay_callback（S2S ReturnURL）。
            self._set_pending()
            return {'redirect_url': three_d_url}

        self._set_done()
        return None
