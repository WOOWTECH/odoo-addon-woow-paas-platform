"""ECPay 站內付 2.0（V4 綁卡）平台側補強：電話正規化＋vendored client 的兩個缺口。

vendored ``payment_ecpay_ecpg`` 不在本 repo（唯讀、跟著映像走），本檔只在平台側
**繼承**它的 ``ECPGClient``，加上兩個它沒有的呼叫：

* ``create_payment_with_card_id_v4``：與 vendored ``create_payment_with_card_id``
  同一個端點、同一個 payload，只多一個 **頂層** ``OrderResultURL``（官方文件
  CreatePaymentWithCardID：``OrderResultURL`` 是 Data 的頂層欄位，不在
  ``OrderInfo`` 裡）。
* ``do_action``：``https://ecpayment.ecpay.com.tw/1.0.0/Credit/DoAction``（信用卡
  請退款）。綁卡驗證那一筆 NT$5 是**真的授權**，官方要求綁卡成功後取消授權；
  vendored 完全沒有這支，所以由平台補（``woow_paas_platform.ecpg_bind_auth_void``）。

加解密、HTTP、TransCode／MerchantID 驗證全部沿用 vendored ``_post_decoded``／
``_post``——⛔ 不在平台側抄一份加密規則（抄了就會漂）。
"""
from __future__ import annotations

import re

from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import ECPGClient

# ECPay ConsumerInfo.Phone：平台只送台灣手機 09XXXXXXXX；其餘一律送空字串
# （Email 一定有帶，官方規則是 Email／Phone 擇一必填，所以 Phone 可以空）。
_TW_MOBILE_RE = re.compile(r'09\d{8}')


def ecpay_consumer_phone(raw):
    """把 ``res.partner.phone`` 正規化成 ECPay 可收的台灣手機號碼，否則回 ``''``。

    * ``+886 912-345-678`` / ``886912345678`` → ``0912345678``
    * ``+886 0912 345 678``（多寫了 0）→ ``0912345678``
    * ``0912-345-678`` → ``0912345678``
    * 市話、國外號碼、太短、空值 → ``''``

    寧可不送也不送錯：ECPay 對格式錯的 Phone 會整筆拒絕（綁卡／扣款都是），
    而空的 Phone 是合法的。
    """
    if not raw:
        return ''
    digits = ''.join(ch for ch in str(raw) if ch.isdigit())
    if digits.startswith('8860'):
        digits = digits[3:]
    elif digits.startswith('886'):
        digits = '0' + digits[3:]
    return digits if _TW_MOBILE_RE.fullmatch(digits) else ''


# ECPay ConsumerInfo.Name：官方只寫「限制字元」。2026-10-15 對 stage（3002607）
# 實測：英文字母、空白、中日韓統一表意文字（含擴充 A）與 . , ' - ( ) / 可過；
# 數字、& @ _ + ; # < " 、重音字母、全形英數／全形括號、emoji、擴充 B 以後
# 一律回 RtnCode 5100011「The parameter [Name] Incorrect format」——綁卡／扣款
# **整筆被拒**。空字串可過 ⇒ 不合規就送空（不嘗試刪字元：刪掉數字會把
# 「3M」變成「M」，等於送一個錯的名字）。
_ECPAY_NAME_RE = re.compile(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff .,'()/-]+")
_ECPAY_NAME_MAX = 50


def ecpay_consumer_name(raw):
    """``res.partner.name`` → ECPay 可收的 ConsumerInfo.Name，否則回 ``''``。"""
    if not raw:
        return ''
    name = ' '.join(str(raw).split())[:_ECPAY_NAME_MAX].strip()
    return name if _ECPAY_NAME_RE.fullmatch(name) else ''


class EcpgV4Client(ECPGClient):
    """vendored ``ECPGClient`` ＋ 平台需要、vendored 沒有的兩個呼叫。"""

    def create_payment_with_card_id_v4(
        self,
        bind_card_id: str,
        consumer_info: dict,
        order_info: dict,
        custom_field: str = '',
        order_result_url: str = '',
    ) -> dict:
        """CreatePaymentWithCardID；``order_result_url`` 非空時放進 Data 頂層。

        刻意**不送** ``Need3D``（官方預設 0＝不走 3D）——自動扣款（cron）沒有
        瀏覽器可以完成 3D。``OrderResultURL`` 只在呼叫端給了才帶：官方文件寫
        Need3D=1 時必填、Need3D=0 時「不需要」；手動儲值仍帶上，是因為特店
        若被 ECPay 設成強制 3D（或風控判定要 3D），回應仍可能帶 ThreeDURL，
        這時瀏覽器驗證完必須有地方回來。
        """
        payload = {
            'PlatformID': '',
            'MerchantID': self.merchant_id,
            'BindCardID': bind_card_id,
            'OrderInfo': order_info,
            'ConsumerInfo': consumer_info,
            'CustomField': custom_field,
        }
        if order_result_url:
            payload['OrderResultURL'] = order_result_url
        return self._post('/Merchant/CreatePaymentWithCardID', payload)

    def do_action(
        self,
        merchant_trade_no: str,
        trade_no: str,
        action: str,
        total_amount: int,
    ) -> dict:
        """信用卡請退款功能（DoAction）。回傳解密後的 Data，**不**替呼叫端判 RtnCode。

        ``action``：``N``＝放棄（已授權未關帳時取消授權）、``E``＝取消關帳
        （「要關帳」狀態要先 E 再 N）、``R``＝退刷（已關帳）。官方註明測試環境
        無法使用（沒有真實授權）——stage 上呼叫失敗是預期的，呼叫端要能承受。
        """
        if action not in ('N', 'E', 'R', 'C'):
            raise ValueError('DoAction action must be one of C/R/E/N')
        payload = {
            'PlatformID': '',
            'MerchantID': self.merchant_id,
            'MerchantTradeNo': merchant_trade_no,
            'TradeNo': trade_no,
            'Action': action,
            'TotalAmount': int(total_amount),
        }
        return self._post_decoded('ecpayment', '/1.0.0/Credit/DoAction', payload)
