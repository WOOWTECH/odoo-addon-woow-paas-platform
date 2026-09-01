"""#864 PR-G — `pay_callback` 的 inbox override：只收 facts，⛔ 不動對外回應。

**病**：3DS callback 可能在原 request 的交易 commit **之前**抵達。那一刻
``payment.transaction`` 這一列還不存在 ⇒ vendored ``pay_callback`` 查無 tx ⇒
回 ``1|OK``（ECPay 協定的「收到了，別再重送」）⇒ 錢永不入帳、零告警，≤60 分
後還會被 cleanup cron 標成 ``cancelled``（看起來像使用者自己放棄）。

**這支 override 做的唯一一件事**：在把控制權交回父類之前，把 provider 送來的
事實（``MerchantTradeNo`` / ``RtnCode`` / ``TradeNo``）用獨立交易寫進
``woow.billing.incident``；之後 ``woow.wallet.topup._cron_replay_callback_inbox``
拿這些事實去補入帳。

🔴 **⛔ 不改對外回應**：``return super().pay_callback(**kw)`` 是本 method 的
最後一個 statement，status／body 逐字元照父類。「查無 tx 改成可重送」是
**PR-H** 的射程，而且我們對 ECPay 的重送政策零量測證據（只有 addon 註解）
⇒ G 的收斂完全不押它。回應零漂移由
``src/tests/test_ecpg_callback_inbox_864.py`` 對每一個分支直接量測釘住。

🔴 **裸 ``@route()``**：Odoo 18 的 leaf-controller 合成會沿用父類的 URL／
``type``／``auth``／``methods``；⛔ 不要重寫那些參數（``type`` 寫錯會被
``_check_and_complete_route_definition`` 警告，且等於偷偷改協定）。

🔴 **PCI**：只存 ``RtnCode``／``TradeNo``；``detail`` 寫死通用句子。
⛔ 不存 ``ResultData`` 密文（2026-08-29 裁決②：會沾到 ``Card4No`` 尾碼、
把 PCI-DSS 4.0 的 12 個月保存義務攬進平台 DB），⛔ 不存 ``RtnMsg`` 原文
（可能夾帶 PAN-like／HTML ⇒ ``_validate_log_snapshot`` 會**整筆**拒收，
結果是「越危險的 callback 越沒有紀錄」）。
"""

import logging

from odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback import (
    ECPGCallback,
)
from odoo.http import request, route

_logger = logging.getLogger(__name__)

# 寫死的通用句子：⛔ 不放 provider 原始訊息（見模組 docstring 的 PCI 段）。
# ⚠ 這個字串本身也要過 `_validate_log_snapshot` 的敏感字掃描——`detail` 是
# free-text 欄位，連「我們不存 R-e-s-u-l-t-D-a-t-a」這種**說明句**都會命中
# `_SENSITIVE_MARKER_RE` 而讓整筆 snapshot 被 fail-soft 拒收（實測踩過）。
# 要講「不存什麼」請寫在程式碼註解裡，⛔ 不要寫進落庫的值。
_CALLBACK_DETAIL = (
    'ECPay pay_callback 事實已登錄；代碼與交易編號見同列欄位，'
    'provider 原始訊息與加密內容一律不落庫。'
)


class WoowECPGCallbackInbox(ECPGCallback):
    """把 pay_callback 的事實落進 billing incident inbox（#864 PR-G）。"""

    @route()
    def pay_callback(self, **kw):
        # 先落 facts、再交回父類：父類若拋例外，事實仍留得下來（獨立交易）。
        self._woow_record_callback_evidence(kw)
        return super().pay_callback(**kw)

    def _woow_record_callback_evidence(self, post_params):
        """Best-effort：把 callback 事實寫進 inbox，⛔ 永不影響回應。

        父類的 ``_parse_result_data`` / ``_decrypt_envelope`` 都是無副作用的
        ``staticmethod``，所以這裡重跑一次是安全的（代價只是失敗分支的
        warning log 會出現兩次——那是 log 不是回應）。⛔ 不自己抄一份解析／
        解密規則：抄了就會與 vendored addon 漂開。
        """
        try:
            envelope = self._parse_result_data(post_params)
            if envelope is None:
                return
            provider = request.env['payment.provider'].sudo().search([
                ('code', '=', 'ECPay'),
                ('enable_ecpg', '=', True),
                ('MerchantID', '=', envelope.get('MerchantID')),
            ], limit=1)
            if not provider:
                return
            data = self._decrypt_envelope(envelope, provider)
            if data is None:
                return
            order = data.get('OrderInfo') or {}
            merchant_trade_no = order.get('MerchantTradeNo')
            if not merchant_trade_no:
                return
            rtn_code = data.get('RtnCode')
            snapshot = {
                'external_ref': merchant_trade_no,
                # 這個 kind 只在「完全沒有預登記」時才會真的落地。key 已被
                # 佔用時，同一個 statement 只更新 evidence 欄位、**保留該列
                # 原本的 kind**（`intent`，或已經被升級成
                # `charged_not_credited` 的那一列）——裁決 G1／Q3。
                # ⚠ 有了 fail-closed 預登記之後，本平台發起的付款一定先有
                # intent 列 ⇒ 走到「真孤兒」這條的只剩「不是我們發起的 MTN」。
                'kind': 'callback_tx_missing',
                # RtnCode 是 int，rtn_code 欄位是 Char ⇒ 不轉 str 會被型別
                # 檢查整筆拒收，而且是靜默的（fail-soft 回 False）。
                'rtn_code': None if rtn_code is None else str(rtn_code),
                'trade_no': order.get('TradeNo') or None,
                'detail': _CALLBACK_DETAIL,
            }
            incident_id = request.env['woow.billing.incident'].sudo() \
                ._log_callback_evidence(snapshot)
            if not incident_id:
                _logger.critical(
                    '[ECPG_INBOX] callback evidence was NOT recorded; a '
                    'top-up whose transaction is not visible yet can no '
                    'longer be replayed from the inbox',
                )
        except Exception:
            # ⛔ inbox 是附加保險，絕不能反過來改變 provider 看到的回應。
            _logger.critical(
                '[ECPG_INBOX] recording callback evidence raised; the '
                'provider response is unaffected',
            )
