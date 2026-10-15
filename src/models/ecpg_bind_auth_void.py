"""ECPay 站內付 2.0 綁卡驗證授權的取消帳本（V4 上線缺口）。

**病**：綁卡時 ECPay 會對卡片做一筆真的授權（平台送 ``TotalAmount=5``）。官方
GetTokenbyBindingCard 文件：「此金額為實際的交易授權金額，特店必須於綁定信用卡
成功後將此交易取消授權」。vendored ``payment_ecpay_ecpg`` 收到綁卡結果只建
``payment.token``，**從來不取消**那筆授權 ⇒ 每綁一張卡，持卡人帳單就多一筆
NT$5（每日 20:15 自動關帳後就變成真的請款）。

**這個帳本**：每個綁卡成功的 ``MerchantTradeNo`` 一列（unique ⇒ 一次綁卡只處理
一次，不論 ReturnURL／OrderResultURL／CreateBindCard 同步回應來幾次），由
``woow.wallet.topup._cron_cleanup_pending_topups``（每 10 分鐘）當作獨立 failure
domain 呼叫 ``_cron_process_due`` 去打 DoAction：

* 先送 ``N``（放棄＝取消授權）；成功即結案。
* ``N`` 不成功 ⇒ 用 CreditDetail/QueryTrade 看生命週期再決定：
  已取消／未授權 → ``not_needed``；「要關帳」（To be captured）→ 先 ``E`` 再 ``N``；
  已關帳（Captured）→ ``R`` 全額退刷；其他（含查不到）→ 退避後重試。
* 重試耗盡 → ``failed`` ＋ critical log，需人工到 ECPay 後台處理。

🔴 **失敗絕不影響綁卡**：入帳本是 best-effort（呼叫端包 savepoint），DoAction 只在
cron 打，綁卡回應路徑上零外呼。
🔴 **安全閘**：只處理 ``BIND`` 開頭、金額 1..``BIND_AUTH_MAX_AMOUNT`` 的列——
這個帳本會送 ``R``（退刷），絕不能被任何真正的儲值交易誤入。
🔴 **PCI**：只存 MTN／TradeNo／金額／代碼；不存 BindCardID、卡號片段、
``ResultData`` 或 ``RtnMsg`` 全文。
⚠ ECPay 官方：DoAction **測試環境無法使用**（沒有真實授權）——stage 上一律走到
重試分支，屬預期。
"""
import logging
from datetime import timedelta

import psycopg2
import requests

from odoo import api, fields, models
from odoo.tools import mute_logger

from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import (
    CreditTradeLookupState,
    ECPGError,
    ECPGQueryForbiddenError,
)
from odoo.addons.payment_ecpay_ecpg.sdk.utils import redact_host

from ..services.ecpg_v4 import EcpgV4Client

_logger = logging.getLogger(__name__)

# 綁卡驗證金額上限（平台送 5）。超過這個數字的列一律拒絕處理——安全閘。
BIND_AUTH_MAX_AMOUNT = 50
BIND_TRADE_PREFIX = 'BIND'

# 重試退避（分鐘），超出表長後固定用最後一格；總次數上限 _MAX_ATTEMPTS。
# 涵蓋「20:15 自動關帳 → 隔天 06:00 後才能 E」這段空窗。
_BACKOFF_MINUTES = (10, 30, 60, 180, 360, 720)
_MAX_ATTEMPTS = 15
_BATCH_SIZE = 20

# 'Cancelled'（雙 L）是 2026-10-15 stage 實測 N 成功後 CreditDetail 回的值；
# vendored ``assess_for_credit`` 只認單 L 的 'Canceled'，這裡兩種都收。
_RELEASED_TOP = frozenset({
    'Canceled', 'Cancelled', 'Unauthorized', 'Operation canceled'})
_RELEASED_CLOSE = frozenset({'Cancelled', 'Operation canceled'})


class EcpgBindAuthVoid(models.Model):
    _name = 'woow_paas_platform.ecpg_bind_auth_void'
    _description = 'ECPG Bind Authorization Void'
    _order = 'id desc'
    _rec_name = 'merchant_trade_no'

    merchant_trade_no = fields.Char(required=True, readonly=True, index=True)
    trade_no = fields.Char(required=True, readonly=True)
    amount = fields.Integer(required=True, readonly=True)
    provider_id = fields.Many2one(
        'payment.provider', required=True, readonly=True, ondelete='restrict')
    partner_id = fields.Many2one(
        'res.partner', readonly=True, index=True, ondelete='set null')
    source = fields.Selection([
        ('return_url', 'ReturnURL (server-to-server)'),
        ('order_result', 'OrderResultURL (browser)'),
        ('create_bind', 'CreateBindCard response (no 3-D)'),
    ], required=True, readonly=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Voided / Refunded'),
        ('not_needed', 'Already released'),
        ('failed', 'Gave up (manual action needed)'),
    ], default='pending', required=True, index=True, readonly=True)
    action_trail = fields.Char(
        readonly=True,
        help='Actions sent to ECPay with their RtnCode, oldest first '
             '(e.g. "N:10200050 Q:To be captured E:1 N:1").')
    attempt_count = fields.Integer(default=0, readonly=True)
    next_attempt_at = fields.Datetime(index=True, readonly=True)
    last_attempt_at = fields.Datetime(readonly=True)
    last_error = fields.Char(readonly=True)
    done_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('merchant_trade_no_uniq', 'UNIQUE(merchant_trade_no)',
         'Each bind trade is voided at most once.'),
    ]

    # ------------------------------------------------------------------
    # 入帳本（綁卡回應路徑上呼叫；零外呼）
    # ------------------------------------------------------------------

    @api.model
    def _enqueue_from_bind_data(self, provider, data, source, partner=None):
        """綁卡成功的解密資料 → 一列 pending（冪等）。回傳該列或空集。

        ``data`` 是 ECPay 解密後的 Data（ReturnURL／OrderResultURL 與
        CreateBindCard 無 3D 回應同形）：只用 ``RtnCode`` 與 ``OrderInfo``
        的 ``MerchantTradeNo``／``TradeNo``／``TradeAmt``。BindCardID 為空
        （卡在通知前就被刪掉）**仍然要取消授權**——授權與卡片存廢無關。
        """
        if not data or data.get('RtnCode') != 1:
            return self.browse()
        order = data.get('OrderInfo') or {}
        mtn = order.get('MerchantTradeNo') or ''
        trade_no = order.get('TradeNo') or ''
        amount = order.get('TradeAmt')
        if amount in (None, ''):
            amount = (data.get('CardInfo') or {}).get('Amount')
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            amount = 0
        if not (mtn and trade_no and amount > 0):
            _logger.warning(
                '[BIND_VOID] bind succeeded but OrderInfo lacks MTN/TradeNo/'
                'amount (mtn=%s has_trade_no=%s amount=%s source=%s); the '
                'authorization cannot be voided automatically',
                mtn or None, bool(trade_no), amount, source,
            )
            return self.browse()
        existing = self.sudo().search([('merchant_trade_no', '=', mtn)], limit=1)
        if existing:
            return existing
        vals = {
            'merchant_trade_no': mtn,
            'trade_no': trade_no,
            'amount': amount,
            'provider_id': provider.id,
            'partner_id': partner.id if partner else False,
            'source': source,
            'state': 'pending',
            'next_attempt_at': fields.Datetime.now(),
        }
        try:
            with self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
                row = self.sudo().create(vals)
        except psycopg2.IntegrityError:
            # 另一條回呼路徑（S2S 與瀏覽器常常同時到）先寫了。
            return self.sudo().search([('merchant_trade_no', '=', mtn)], limit=1)
        _logger.info(
            '[BIND_VOID] enqueued mtn=%s amount=%s source=%s row=%s',
            mtn, amount, source, row.id,
        )
        return row

    # ------------------------------------------------------------------
    # cron（掛在 _cron_cleanup_pending_topups，每 10 分鐘）
    # ------------------------------------------------------------------

    @api.model
    def _cron_process_due(self, limit=_BATCH_SIZE):
        """處理到期的 pending 列；每列一顆 savepoint，一列出事不拖累其他列。"""
        now = fields.Datetime.now()
        rows = self.sudo().search([
            ('state', '=', 'pending'),
            '|', ('next_attempt_at', '=', False), ('next_attempt_at', '<=', now),
        ], order='next_attempt_at, id', limit=limit)
        summary = {}
        for row in rows:
            try:
                with self.env.cr.savepoint():
                    outcome = row._process_one()
            except psycopg2.OperationalError:
                raise
            except Exception:
                _logger.exception('[BIND_VOID] processing crashed row=%s', row.id)
                outcome = 'crashed'
            summary[outcome] = summary.get(outcome, 0) + 1
        if rows:
            _logger.info('[BIND_VOID] cron processed=%s summary=%s',
                         len(rows), summary)
        return summary

    def _guard_ok(self):
        self.ensure_one()
        provider = self.provider_id
        return bool(
            (self.merchant_trade_no or '').startswith(BIND_TRADE_PREFIX)
            and 0 < self.amount <= BIND_AUTH_MAX_AMOUNT
            and self.trade_no
            and provider.code == 'ECPay'
            and provider.MerchantID and provider.HashKey and provider.HashIV
        )

    def _client(self):
        provider = self.provider_id
        return EcpgV4Client(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )

    def _send_action(self, client, action, trail):
        """送一個 DoAction；回 ``(ok, error_text)``，所有外呼例外都收斂在這裡。"""
        try:
            res = client.do_action(
                self.merchant_trade_no, self.trade_no, action, self.amount)
        except (ECPGError, requests.exceptions.RequestException, ValueError) as e:
            trail.append('%s:exc' % action)
            return False, '%s %s' % (action, redact_host(str(e))[:200])
        code = res.get('RtnCode')
        trail.append('%s:%s' % (action, code))
        if code == 1:
            return True, ''
        msg = res.get('RtnMsg')
        msg = redact_host(msg)[:120] if isinstance(msg, str) else ''
        return False, '%s RtnCode=%s %s' % (action, code, msg)

    def _process_one(self):
        """對一列做一輪：回 ``done``／``not_needed``／``retry``／``failed``。"""
        self.ensure_one()
        if self.state != 'pending':
            return self.state
        trail = []
        if not self._guard_ok():
            return self._finish(
                'failed', trail,
                error='guard: not a bind authorization this ledger may touch')
        client = self._client()

        ok, err = self._send_action(client, 'N', trail)
        if ok:
            return self._finish('done', trail)

        # N 沒成功：看 ECPay 上這筆現在是什麼狀態再決定。
        Circuit = self.env['woow_paas_platform.ecpg_query_circuit'].sudo()
        merchant_id = self.provider_id.MerchantID
        try:
            blocked_until = Circuit._blocked_until(merchant_id)
        except Exception:
            blocked_until = None
        if blocked_until and blocked_until > fields.Datetime.now():
            trail.append('Q:circuit_open')
            return self._retry(trail, err + ' | query circuit open')
        try:
            lookup = client.query_credit_trade(self.merchant_trade_no)
        except ECPGQueryForbiddenError as e:
            trail.append('Q:403')
            try:
                Circuit._record_forbidden(
                    merchant_id, self.provider_id.id, e.retry_after_seconds)
            except Exception:
                _logger.exception('[BIND_VOID] cannot persist shared 403 circuit')
            return self._retry(trail, err + ' | query 403')
        except (ECPGError, requests.exceptions.RequestException, ValueError) as e:
            trail.append('Q:exc')
            return self._retry(
                trail, err + ' | query ' + redact_host(str(e))[:120])

        if lookup.state is CreditTradeLookupState.NOT_FOUND:
            trail.append('Q:not_found')
            return self._retry(trail, err + ' | query not found')
        detail = lookup.detail
        status = detail.status
        trail.append('Q:%s' % status)
        if (status in _RELEASED_TOP
                or any(r.status in _RELEASED_CLOSE for r in detail.close_data)):
            return self._finish('not_needed', trail)
        if status == 'To be captured':
            ok, err2 = self._send_action(client, 'E', trail)
            if not ok:
                return self._retry(trail, err2)
            ok, err3 = self._send_action(client, 'N', trail)
            if ok:
                return self._finish('done', trail)
            return self._retry(trail, err3)
        if status == 'Captured':
            if detail.amount != self.amount:
                return self._finish(
                    'failed', trail,
                    error='captured amount %s != bind amount %s' % (
                        detail.amount, self.amount))
            ok, err4 = self._send_action(client, 'R', trail)
            if ok:
                return self._finish('done', trail)
            return self._retry(trail, err4)
        # Authorized（N 卻失敗）或未知狀態：等下一輪。
        return self._retry(trail, err)

    def _append_trail(self, trail):
        joined = ' '.join(filter(None, [self.action_trail or '', ' '.join(trail)]))
        return joined[-500:]

    def _finish(self, state, trail, error=''):
        now = fields.Datetime.now()
        vals = {
            'state': state,
            'action_trail': self._append_trail(trail),
            'attempt_count': self.attempt_count + 1,
            'last_attempt_at': now,
            'next_attempt_at': False,
            'last_error': error[:300] or False,
        }
        if state in ('done', 'not_needed'):
            vals['done_at'] = now
        self.write(vals)
        log = _logger.critical if state == 'failed' else _logger.info
        log('[BIND_VOID] mtn=%s -> %s trail=%s%s', self.merchant_trade_no,
            state, vals['action_trail'],
            (' error=%s' % error) if error else '')
        return state

    def _retry(self, trail, error):
        attempts = self.attempt_count + 1
        if attempts >= _MAX_ATTEMPTS:
            return self._finish(
                'failed', trail,
                error=('gave up after %s attempts: %s' % (attempts, error)))
        now = fields.Datetime.now()
        minutes = _BACKOFF_MINUTES[min(attempts - 1, len(_BACKOFF_MINUTES) - 1)]
        self.write({
            'attempt_count': attempts,
            'action_trail': self._append_trail(trail),
            'last_attempt_at': now,
            'next_attempt_at': now + timedelta(minutes=minutes),
            'last_error': (error or '')[:300] or False,
        })
        _logger.warning(
            '[BIND_VOID] mtn=%s not voided yet (attempt %s/%s, next in %s min): %s',
            self.merchant_trade_no, attempts, _MAX_ATTEMPTS, minutes, error,
        )
        return 'retry'

    def action_retry_now(self):
        """後台按鈕：把 failed／pending 列重新排進下一輪 cron。"""
        self.check_access('write')
        for row in self.sudo():
            if row.state in ('pending', 'failed'):
                row.write({
                    'state': 'pending',
                    'next_attempt_at': fields.Datetime.now(),
                    'attempt_count': 0,
                })
        return True
