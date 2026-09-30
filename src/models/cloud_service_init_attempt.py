"""#1257：post-deploy init 的 attempt 記錄、lease 長度與失敗分類。

設計權威是 ``docs/decisions/refactor/post-deploy-init-execution-model.md``（#1257
resolution Q2／Q3／Q8／Q11／Q32／Q39）。本檔只放三樣東西：

* ``cloud_service_init_attempt``：每送出一次 init 就一筆，先記結果、再由服務端 CAS
  套用（Q2）；租戶看不到（ACL 只給 ``base.group_system``）。
* lease 長度：由 client 預算常數直接推導，不開 config_parameter（Q32）。
* ``classify_init_result``：把 operator 回應或例外分成五類。分類看的是「遠端副作用
  是否可能已發生」，**不看** ``success`` 布林（Q3）。
"""
import logging
import re

import requests
from urllib3.exceptions import NewConnectionError

from odoo import api, fields, models

from ..services.paas_operator import (
    INIT_N8N_CONNECT_TIMEOUT,
    INIT_N8N_READ_TIMEOUT,
    INIT_ODOO_CONNECT_TIMEOUT,
    INIT_ODOO_READ_TIMEOUT,
    PaaSOperatorAPIError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------- #
#  Q32：lease 長度                                                            #
# ------------------------------------------------------------------------- #
#
# 下界＝client 預算（connect＋read），**直接引用同一組常數**：lease 一定要比
# 「client 還可能在等回應」的最長時間長，否則 lease 到期、背景重送時，上一次的
# request 可能還活著。read 另由 repo-lint init-*-budget-ladder 守住「> operator
# 子步 envelope」，所以 lease 也自動大於 envelope。
# 上界＝承載它的 runtime 的 cron 硬上限（prod `limit_time_real_cron=1800`，見
# woow-paas/paas-app-bundle-cd#154；遷移後 platform profile ≥1800，見
# woow-paas/woow-paas-charts#81）。餘裕 180s 讓兩者都成立：Odoo 1200、n8n 900。
_INIT_LEASE_MARGIN_SECONDS = 180
INIT_LEASE_SECONDS = {
    'odoo': INIT_ODOO_CONNECT_TIMEOUT + INIT_ODOO_READ_TIMEOUT + _INIT_LEASE_MARGIN_SECONDS,
    'n8n': INIT_N8N_CONNECT_TIMEOUT + INIT_N8N_READ_TIMEOUT + _INIT_LEASE_MARGIN_SECONDS,
}
# #1159 Q33：init cron 每 tick 判斷「還放不放得下下一筆」用的單次 attempt 最壞耗時＝
# client 預算（connect＋read），同一組常數：Odoo 1020、n8n 720。
INIT_ATTEMPT_WORST_SECONDS = {
    'odoo': INIT_ODOO_CONNECT_TIMEOUT + INIT_ODOO_READ_TIMEOUT,
    'n8n': INIT_N8N_CONNECT_TIMEOUT + INIT_N8N_READ_TIMEOUT,
}
# Q15：migration 替既有 `initializing` 列寫入的合成 lease。舊 process 可能還有在途
# 的 remote exec（舊 code 的 read 是 900s），取 Odoo 的 1200 一律套用。
LEGACY_LEASE_SECONDS = 1200

# ------------------------------------------------------------------------- #
#  Q3／Q11：失敗分類                                                          #
# ------------------------------------------------------------------------- #

OUTCOME_SUCCESS = 'success'
OUTCOME_FAILED = 'failed'
OUTCOME_PARTIAL = 'partial'
OUTCOME_UNKNOWN = 'unknown'
OUTCOME_NOT_EXECUTED = 'not_executed'
OUTCOME_LEGACY = 'legacy'

# operator `schemas.InitErrorKind` 的值域（#1263）。
_WIRE_KINDS = (OUTCOME_NOT_EXECUTED, OUTCOME_FAILED, OUTCOME_PARTIAL, OUTCOME_UNKNOWN)

_ERROR_MAX_CHARS = 2000
# attempt 記錄不得留下可用憑證：JWT（n8n API key 的形狀）、Bearer token、以及
# key=value／key: "value"／URL-encoded（`%3D`）形式的密鑰；引號內含空白也整段遮。
_JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}')
_BEARER_RE = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+')
_SECRET_KV_RE = re.compile(
    r'(?i)(password|passwd|api[_-]?key|token|secret)'
    r'(["\']?\s*(?::|=|%3D)\s*)'
    r'("[^"]*"|\'[^\']*\'|[^\s"\',}&]+)')


class InitOutcome:
    """分類結果：outcome 之外帶上 operator 回報的步驟與（n8n）拿到的 api_key。

    ``error_kind`` 只放 operator **wire 上回報**的值（#1263）；平台自己推論出來的
    分類（connect 階段失敗、read timeout……）只在 ``outcome``，``error_kind`` 留空。
    """

    __slots__ = ('outcome', 'error_step', 'error_kind', 'message', 'api_key')

    def __init__(self, outcome, error_step=None, error_kind=None, message=None, api_key=None):
        self.outcome = outcome
        self.error_step = error_step
        self.error_kind = error_kind
        self.message = message
        self.api_key = api_key

    def __repr__(self):
        return 'InitOutcome(%s, step=%s, kind=%s)' % (self.outcome, self.error_step, self.error_kind)


def _is_connect_phase(exc):
    """``PaaSOperatorConnectionError`` 是否確定發生在 connect 階段（request 未送出）。

    client 的 ``_request`` 把 requests 的 ``ConnectionError`` 一律包成
    ``PaaSOperatorConnectionError``——但 requests 對「連線中途被切」
    （``ProtocolError``／``RemoteDisconnected``，request 可能已送達）**也**丟
    ``ConnectionError``。只有 ``ConnectTimeout`` 與 urllib3 的
    ``NewConnectionError``（連線被拒、DNS 失敗）能證明 request 沒送出去（Q11：
    平台端只有 connect 階段失敗算 not_executed）。原始例外由 ``raise`` 在
    ``except`` 區塊內自動掛在 ``__context__``。
    """
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(cause, requests.exceptions.ConnectionError) and cause.args:
        reason = getattr(cause.args[0], 'reason', cause.args[0])
        return isinstance(reason, NewConnectionError)
    return False


def _wire_kind(value):
    return value if value in _WIRE_KINDS else None


def classify_init_result(init_type, result=None, exc=None):
    """把一次 init 呼叫的結果分成 success／failed／partial／unknown／not_executed。

    - 例外：connect 階段 → not_executed；HTTP 錯誤帶結構化 ``detail.error_kind`` 時
      採用之（#1263 的 500 一律是 unknown）；其餘（read timeout、連線中途斷、JSON
      無效、非預期例外）→ unknown。
    - 回應：``success`` 為真 → success（n8n 另須帶 ``api_key``，否則 unknown）；
      n8n 帶 ``api_key`` 的失敗 → partial；其他失敗採用 ``error_kind``，**缺欄位的
      舊 operator 保守歸 unknown**（Q11）。
    """
    if exc is not None:
        message = getattr(exc, 'detail', None) or str(exc)
        if isinstance(exc, PaaSOperatorConnectionError):
            outcome = OUTCOME_NOT_EXECUTED if _is_connect_phase(exc) else OUTCOME_UNKNOWN
            return InitOutcome(outcome, message=str(message))
        if isinstance(exc, PaaSOperatorAPIError) and isinstance(exc.detail, dict):
            kind = _wire_kind(exc.detail.get('error_kind'))
            if kind == OUTCOME_PARTIAL:
                kind = OUTCOME_UNKNOWN  # 500 不會帶回 api_key，partial 無從成立
            return InitOutcome(
                kind or OUTCOME_UNKNOWN,
                error_step=exc.detail.get('error_step'),
                error_kind=kind,
                message=str(exc.detail.get('message') or exc.detail),
            )
        return InitOutcome(OUTCOME_UNKNOWN, message='%s: %s' % (type(exc).__name__, message))

    if not isinstance(result, dict):
        return InitOutcome(OUTCOME_UNKNOWN, message='non-JSON-object response: %r' % (result,))
    api_key = result.get('api_key') if init_type == 'n8n' else None
    step = result.get('error_step')
    kind = _wire_kind(result.get('error_kind'))
    if result.get('success') is True:
        if init_type == 'n8n' and not api_key:
            return InitOutcome(OUTCOME_UNKNOWN, error_step=step,
                               message='success response without api_key')
        return InitOutcome(OUTCOME_SUCCESS, api_key=api_key)
    message = result.get('error') or result.get('message') or 'Unknown error'
    if api_key:
        return InitOutcome(OUTCOME_PARTIAL, error_step=step, error_kind=kind,
                           message=message, api_key=api_key)
    if kind in (None, OUTCOME_PARTIAL):
        # 缺 error_kind（#1263 之前的 operator），或聲稱 partial 卻沒帶 key：無法證明
        # 遠端沒有副作用 ⇒ unknown。
        return InitOutcome(OUTCOME_UNKNOWN, error_step=step, error_kind=kind, message=message)
    return InitOutcome(kind, error_step=step, error_kind=kind, message=message)


def mask_init_error(text, secrets=()):
    """截斷並遮罩要寫進 attempt 記錄的錯誤字串（Q39：不存 api_key 等憑證）。"""
    if not text:
        return False
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '***')
    text = _JWT_RE.sub('***', text)
    text = _BEARER_RE.sub(lambda m: m.group(1) + '***', text)
    text = _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + '***', text)
    if len(text) > _ERROR_MAX_CHARS:
        text = text[:_ERROR_MAX_CHARS] + '…[truncated]'
    return text


class CloudServiceInitAttempt(models.Model):
    _name = 'woow_paas_platform.cloud_service_init_attempt'
    _description = 'Cloud Service Post-deploy Init Attempt'
    _order = 'id desc'

    # Q39 保留：每服務最近 20 筆＋目前這一代的全部；超過 90 天者清掉（當代除外）。
    # 20／90 是保守值，沒有量測依據。
    _KEEP_PER_SERVICE = 20
    _KEEP_DAYS = 90

    service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        required=True,
        ondelete='cascade',
        index=True,
    )
    generation = fields.Integer(
        required=True,
        help='Init generation this attempt belongs to (failure quotas are per generation)',
    )
    sequence = fields.Integer(
        required=True,
        help='Per-service attempt ordinal; shown to the tenant as the support reference',
    )
    init_type = fields.Selection(
        selection=[('odoo', 'Odoo'), ('n8n', 'n8n')],
        required=True,
    )
    claimed_at = fields.Datetime(required=True, help='DB now() (UTC) when the lease was taken')
    lease_until = fields.Datetime(required=True, help='DB clock (UTC); see INIT_LEASE_SECONDS')
    finished_at = fields.Datetime(help='Empty while in flight')
    applied_at = fields.Datetime(
        help='已持久化結果套用到 service 計數／狀態的時間；使結果記錄 → 套用的 '
             'crash recovery 保持冪等。',
    )
    n8n_key_persisted = fields.Boolean(
        string='n8n Key Persisted',
        default=False,
        copy=False,
        help='本 attempt 的 n8n API key 已與此 marker 同交易寫入 service 受保護欄位；'
             '此欄僅記錄持久化證據，不存放 secret。',
    )
    outcome = fields.Selection(
        selection=[
            (OUTCOME_SUCCESS, 'Success'),
            (OUTCOME_FAILED, 'Failed'),
            (OUTCOME_PARTIAL, 'Partial'),
            (OUTCOME_UNKNOWN, 'Unknown'),
            (OUTCOME_NOT_EXECUTED, 'Not executed'),
            (OUTCOME_LEGACY, 'Legacy (pre-#1257 in-flight)'),
        ],
        help='Empty while in flight. legacy = synthetic attempt written by the '
             '#1257 migration for services already initializing; it never counts '
             'towards the unknown quota.',
    )
    error_step = fields.Char()
    error_kind = fields.Char()
    error = fields.Text(help='Truncated and masked operator error (never an API key)')

    @api.model
    def _cron_purge_init_attempts(self):
        """Q39 保留政策（每日）。當代的 attempt 一律保留：它們是額度計數的依據。"""
        cr = self.env.cr
        self.env.flush_all()  # raw SQL 讀不到 ORM 尚未 flush 的值
        cr.execute("""
            DELETE FROM woow_paas_platform_cloud_service_init_attempt a
             USING woow_paas_platform_cloud_service s
             WHERE s.id = a.service_id
               AND a.generation <> COALESCE(s.init_generation, 0)
               AND a.id IS DISTINCT FROM s.init_attempt_id
               AND (
                    a.claimed_at < (now() AT TIME ZONE 'UTC') - make_interval(days => %s)
                    OR a.id NOT IN (
                        SELECT b.id FROM woow_paas_platform_cloud_service_init_attempt b
                         WHERE b.service_id = a.service_id
                         ORDER BY b.id DESC LIMIT %s
                    )
               )
        """, (self._KEEP_DAYS, self._KEEP_PER_SERVICE))
        if cr.rowcount:
            _logger.info("[#1257] purged %d init attempt(s)", cr.rowcount)
        self.env.invalidate_all()
