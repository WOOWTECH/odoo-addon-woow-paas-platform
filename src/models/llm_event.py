"""AI 模型的稽核事件、節流、告警出口與 incident helper（DESIGN §2.6、§5.9；N6、N13、N24；票 LLM-02b）。

多張票共用的基礎都在這裡，其他票只呼叫、不各自實作：

1. **``llm_event``**（DESIGN §2.6）：system-only 的稽核表。所有群組 ACL 只有讀（system 與
   ``account.group_account_manager``）；寫入一律經本檔的 service method、以**獨立 cursor**
   直接 INSERT 並 commit（呼叫端的交易之後 rollback 也留得住）。ORM 的 create／write 另要
   ``llm_common._llm_internal`` 的把手（sudo 也擋），unlink 只在模組卸載時允許；保留 400 天，
   由 :meth:`LlmEvent._cron_llm_event_cleanup` 分批刪。
2. **節流**（N6）：:meth:`LlmEvent._llm_throttle_reserve` 以 ``llm_event`` 近一小時的筆數計（平台
   沒有通用限流層，``controllers/oauth2.py:429-438`` 是唯一先例）。「數＋記一筆」在同一個獨立
   交易裡、先拿 per-(帳戶, 動作) 的 advisory lock，並發的兩個請求不會一起擠過上限。被擋下的那一次
   **不記**（否則一直重試會把視窗一直往後推）。結果由 :meth:`LlmEvent._llm_event_finish` 補上。
3. **告警出口**（N13、N24）：:meth:`LlmEvent._llm_alert` 寫 ``llm_event(severity=alert)``＋寄信到
   ``llm_alert_email``；選配 ``llm_alert_webhook_url``；同一個告警（代碼＋帳戶＋對象＋錯誤碼）
   30 分鐘內只寄一次（之後只把 ``occurrences`` 加 1）。**介面只收 id、錯誤碼、數字**：告警代碼
   是封閉的 :class:`LlmAlert`、對象 model 是封閉清單、錯誤碼必須是小寫 snake、數字必須是
   int／float——其他型別（含任何自由文字，例如金鑰的 ``key_label``）一律
   :class:`LlmAlertInputError`（``TypeError`` 的子類），例外訊息只有參數名、不帶值。
4. **incident helper**：:meth:`LlmEvent._llm_incident` 以 ``woow.billing.incident._log``（它自己就是
   獨立 cursor、fail-soft）開帳務群組待辦；snapshot 只有 kind（封閉的 :class:`LlmIncident`）、
   partner／來源列的 id、錯誤碼與點數，**不用** ``detail`` 這個自由文字欄（N24）。

「未設定」（N27）時告警與 incident 一律不寫——cron 本來就第一行 return，這裡是第二道：例行發版
把程式帶上 prod 之後，任何路徑都不會對負責人或帳務群組製造東西。
"""
import enum
import hashlib
import json
import logging
import math
import re
import traceback
from datetime import datetime, timedelta, timezone

import psycopg2
import requests
from markupsafe import Markup, escape
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from ..services import llm_redact
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    ALERT_DEDUPE_MINUTES,
    ERR_LLM_RATE_LIMITED,
    ERR_LLM_UNAVAILABLE,
    LLM_EVENT_RETENTION_DAYS,
    THROTTLE_LIMITS,
    THROTTLE_WINDOW_SECONDS,
    LlmError,
    _is_llm_internal,
    _without_llm_internal,
    alert_targets,
    llm_unconfigured,
    run_llm_cron,
)

_logger = logging.getLogger(__name__)

EVENT_MODEL = 'woow_paas_platform.llm_event'
EVENT_TABLE = 'woow_paas_platform_llm_event'
ACCOUNT_TABLE = 'woow_paas_platform_llm_account'
CLEANUP_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_event_cleanup'
_LOCK_TIMEOUT = '5s'
#: 清理 cron：每批刪幾筆、一個 tick 最多幾批（N28）。
CLEANUP_BATCH = 5000
CLEANUP_BATCHES_PER_TICK = 20
#: webhook 是選配的旁路：短逾時、不重試、不跟 redirect；失敗只記在事件上。
WEBHOOK_TIMEOUT = (2, 3)

ACTION_SELECTION = [
    ('activate', 'Activate'),
    ('provision_step', 'Provisioning step'),
    ('key_create', 'Key created'),
    ('key_reveal', 'Key revealed'),
    ('key_update', 'Key updated'),
    ('key_status', 'Key status changed'),
    ('key_delete', 'Key deleted'),
    ('convert', 'Credit conversion'),
    ('order_transition', 'Order transition'),
    ('manual_credit', 'Manual credit'),
    ('suspend', 'Suspend'),
    ('resume', 'Resume'),
    ('close', 'Close'),
    ('reopen', 'Reopen'),
    ('up_resign', 'User PAT re-signed'),
    ('up_revoke', 'User PAT revoked'),
    ('guard_trip', 'Guard tripped'),
    ('config_change', 'Configuration changed'),
    ('not_me_report', 'Reported as not me'),
    ('reauth', 'Re-authenticated'),
    ('known_discrepancy', 'Known discrepancy'),
    ('alert', 'Alert'),
]
ACTIONS = frozenset(key for key, _label in ACTION_SELECTION)
SEVERITY_SELECTION = [('info', 'Info'), ('warning', 'Warning'), ('alert', 'Alert')]
SEVERITIES = frozenset(key for key, _label in SEVERITY_SELECTION)

#: ``target_model`` 的封閉清單（N24：告警裡的「對象」只能是 PaaS 自己的列）。
TARGET_MODELS = frozenset({
    'woow_paas_platform.llm_account',
    'woow_paas_platform.llm_api_key',
    'woow_paas_platform.llm_credit_order',
    'woow_paas_platform.llm_terms',
    'woow_paas_platform.llm_runtime_state',
    'woow.billing.incident',
    'ir.config_parameter',
})

#: 錯誤碼與結果碼：小寫 snake（DESIGN §3.4 的 ``ERR_*``、開戶的 ``last_error_code``…）。
CODE_RE = re.compile(r'[a-z][a-z0-9_]{0,63}', re.ASCII)
#: ``numbers``／``details`` 的 key。
_KEY_RE = re.compile(r'[a-z][a-z0-9_]{0,31}', re.ASCII)
_MAX_NUMBERS = 12
_INT8_MAX = 2 ** 63 - 1


class LlmAlert(enum.Enum):
    """告警代碼（封閉清單）。新增一種告警＝在這裡加一個成員（值是小寫 snake）。

    值會出現在信件主旨與 webhook；它是程式常數，不是資料。
    """

    #: 驗證告警出口用（odoo shell 觸發；LLM-02b AC2）。
    TEST = 'alert_test'
    #: 開戶放棄（``state=error``，第 6 次失敗或不可重試的錯誤）；錯誤碼＝``last_error_code``。
    PROVISION_FAILED = 'provision_failed'
    #: 密文被拒（綁定不符、解不開、內容不合）——有人動過 DB 或加密金鑰不見了（DESIGN §2.9）。
    SECRET_REJECTED = 'secret_rejected'


class LlmIncident(enum.Enum):
    """``woow.billing.incident.kind`` 裡屬於 AI 模型的那幾種（DESIGN §2.8、§2.9、§2.10）。"""

    CREDIT_UNKNOWN = 'llm_credit_unknown'
    RECONCILE_MISMATCH = 'llm_reconcile_mismatch'
    EXTERNAL_CREDIT = 'llm_external_credit'
    VOID_FAILED = 'llm_void_failed'
    NOT_ME = 'llm_not_me'
    SECRET_MISMATCH = 'llm_secret_mismatch'
    ACCOUNT_ERROR = 'llm_account_error'


class LlmAlertInputError(TypeError):
    """告警／incident／事件 helper 收到不允許的參數。訊息只有參數名，**不帶值**（值可能是租戶的字）。"""

    def __init__(self, field):
        super().__init__('llm event input rejected: %s' % field)
        self.field = field


def _utcnow():
    return fields.Datetime.now()


def safe_code(value, fallback='llm_unknown_code'):
    """把呼叫端手上的錯誤碼收斂成 :data:`CODE_RE`（不合就換成 ``fallback``；截到 64 字元）。

    給「錯誤碼來自別處（new-api 的 code 轉小寫、``_ProvisionStop.code``）」的呼叫端用：告警
    helper 本身對不合格的錯誤碼是**拒絕**，不會幫你改。
    """
    if isinstance(value, str):
        text = value.strip().lower()[:64]
        if CODE_RE.fullmatch(text):
            return text
    return fallback


def _check_id(name, value):
    if value is None:
        return None
    if type(value) is not int or value <= 0 or value > _INT8_MAX:
        raise LlmAlertInputError(name)
    return value


def _check_code(name, value):
    if value is None:
        return None
    if not isinstance(value, str) or not CODE_RE.fullmatch(value):
        raise LlmAlertInputError(name)
    return value


def _check_target(target_model, target_id):
    if target_model is None and target_id is None:
        return None, None
    if not isinstance(target_model, str) or target_model not in TARGET_MODELS:
        raise LlmAlertInputError('target_model')
    if target_id is None:
        raise LlmAlertInputError('target_id')
    return target_model, _check_id('target_id', target_id)


def _check_numbers(numbers, name='numbers'):
    """``{snake: int|float}``（最多 12 個；float 必須是有限值）。"""
    if numbers is None:
        return {}
    if not isinstance(numbers, dict) or len(numbers) > _MAX_NUMBERS:
        raise LlmAlertInputError(name)
    out = {}
    for key, value in numbers.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise LlmAlertInputError(name)
        if type(value) is int:
            if abs(value) > _INT8_MAX:
                raise LlmAlertInputError(name)
        elif type(value) is float:
            if not math.isfinite(value):
                raise LlmAlertInputError(name)
        else:
            raise LlmAlertInputError(name)
        out[key] = value
    return out


def _check_details(details):
    """稽核事件的 ``details``：值只能是數字、布林、``None`` 或小寫 snake 的碼（白名單形狀）。"""
    if details is None:
        return {}
    if not isinstance(details, dict) or len(details) > _MAX_NUMBERS:
        raise LlmAlertInputError('details')
    out = {}
    for key, value in details.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise LlmAlertInputError('details')
        if value is None or type(value) is bool:
            out[key] = value
        elif type(value) in (int, float):
            out[key] = _check_numbers({key: value}, 'details')[key]
        elif isinstance(value, str) and CODE_RE.fullmatch(value):
            out[key] = value
        else:
            raise LlmAlertInputError('details')
    return out


def _dump(payload, field):
    """白名單 dict → 緊湊 JSON；遮罩後有任何變化（長得像秘密的 key 或值）就拒絕。"""
    if not payload:
        return None
    text = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    if llm_redact.redact(text) != text:
        raise LlmAlertInputError(field)
    return text


def _webhook_session():
    session = requests.Session()
    session.trust_env = False
    retry = Retry(total=0, connect=0, read=0, redirect=0, status=0, other=0,
                  raise_on_redirect=False, raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount('https://', adapter)
    return session


class LlmEvent(models.Model):
    _name = 'woow_paas_platform.llm_event'
    _description = 'AI Models Audit Event'
    _order = 'id desc'
    _rec_name = 'action'

    account_id = fields.Many2one(
        'woow_paas_platform.llm_account', ondelete='restrict', index=True, readonly=True)
    partner_id = fields.Many2one('res.partner', ondelete='restrict', index=True, readonly=True)
    uid = fields.Many2one('res.users', string='Acting User', ondelete='set null', readonly=True)
    real_ip = fields.Char(readonly=True, help='The client IP PaaS itself observed.')
    action = fields.Selection(ACTION_SELECTION, required=True, readonly=True, index=True)
    target_model = fields.Char(readonly=True)
    target_id = fields.Integer(readonly=True)
    result = fields.Char(readonly=True, help='Result code (lower snake).')
    error_code = fields.Char(readonly=True)
    severity = fields.Selection(SEVERITY_SELECTION, required=True, default='info',
                                readonly=True, index=True)
    details_json = fields.Text(
        readonly=True,
        help='Whitelisted numbers and codes only; never keys, PATs, passwords or tenant text.')
    alert_code = fields.Char(readonly=True, index=True)
    dedupe_key = fields.Char(readonly=True, index=True, copy=False)
    occurrences = fields.Integer(readonly=True, default=1)
    last_seen_at = fields.Datetime(readonly=True)
    email_state = fields.Char(readonly=True, help='queued / no_recipient / failed')
    webhook_state = fields.Char(readonly=True, help='sent / failed / not_configured')

    def init(self):
        # 節流：每帳號、每動作近一小時的筆數（N6）。
        self.env.cr.execute(
            'CREATE INDEX IF NOT EXISTS woow_llm_event_throttle_idx '
            'ON %s (account_id, action, create_date)' % EVENT_TABLE)
        # 保留 400 天的清理（DESIGN §2.6）。
        self.env.cr.execute(
            'CREATE INDEX IF NOT EXISTS woow_llm_event_create_date_idx '
            'ON %s (create_date)' % EVENT_TABLE)

    # ------------------------------------------------------------------ #
    #  守衛（同其他 LLM model）                                             #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI model audit events can only be written by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI model audit events cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('AI model audit events cannot be deleted.'))

    # ------------------------------------------------------------------ #
    #  稽核事件                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _llm_insert_event_cr(cr, values, now):
        """在 ``cr``（獨立 cursor）裡 INSERT 一列、回傳 id。``values`` 已經驗過（欄位名是常數）。

        帳戶有值而 ``partner_id`` 沒給時，從帳戶補（同一個交易，不經主 cursor 的 snapshot）。
        """
        values = dict(values)
        values.setdefault('severity', 'info')
        values.setdefault('occurrences', 1)
        values.update(create_uid=SUPERUSER_ID, write_uid=SUPERUSER_ID,
                      create_date=now, write_date=now)
        if values.get('account_id') and not values.get('partner_id'):
            cr.execute('SELECT partner_id FROM %s WHERE id = %%s' % ACCOUNT_TABLE,
                       (values['account_id'],))
            row = cr.fetchone()
            if not row:
                raise LlmAlertInputError('account_id')
            values['partner_id'] = row[0]
        columns = sorted(values)
        cr.execute(
            'INSERT INTO %s (%s) VALUES (%s) RETURNING id' % (
                EVENT_TABLE, ', '.join(columns), ', '.join(['%s'] * len(columns))),
            [values[column] for column in columns])
        return cr.fetchone()[0]

    @api.model
    def _llm_insert_event(self, values):
        """以獨立 cursor INSERT 一列並 commit，回傳 id。"""
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            return self._llm_insert_event_cr(cr, values, _utcnow())

    @api.model
    def _llm_record(self, action, *, account_id=None, partner_id=None, uid=None, real_ip=None,
                    target_model=None, target_id=None, result='ok', error_code=None,
                    severity='info', details=None):
        """寫一筆稽核事件（獨立交易；呼叫端之後 rollback 也留得住）。回傳 id。

        ``details`` 只收數字、布林、``None``、小寫 snake 的碼（白名單形狀，見 :func:`_check_details`）。
        告警請用 :meth:`_llm_alert`（``action='alert'`` 不能從這裡寫）。
        """
        if action not in ACTIONS or action == 'alert':
            raise LlmAlertInputError('action')
        if severity not in SEVERITIES or severity == 'alert':
            raise LlmAlertInputError('severity')
        target_model, target_id = _check_target(target_model, target_id)
        values = {
            'action': action,
            'account_id': _check_id('account_id', account_id),
            'partner_id': _check_id('partner_id', partner_id),
            'uid': _check_id('uid', uid),
            'real_ip': self._llm_ip(real_ip),
            'target_model': target_model,
            'target_id': target_id,
            'result': _check_code('result', result),
            'error_code': _check_code('error_code', error_code),
            'severity': severity,
            'details_json': _dump(_check_details(details), 'details'),
        }
        return self._llm_insert_event(values)

    @staticmethod
    def _llm_ip(real_ip):
        if real_ip in (None, False, ''):
            return None
        if not isinstance(real_ip, str) or len(real_ip) > 45 \
                or not re.fullmatch(r'[0-9A-Fa-f:.]{2,45}', real_ip):
            raise LlmAlertInputError('real_ip')
        return real_ip

    @api.model
    def _llm_event_finish(self, event_id, result, *, error_code=None, target_model=None,
                          target_id=None):
        """補上一筆 ``result='started'`` 事件的結果（只改仍是 started 的那一列）。盡力而為：失敗只記 log。"""
        try:
            result = _check_code('result', result)
            error_code = _check_code('error_code', error_code)
            target_model, target_id = _check_target(target_model, target_id)
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute(
                    'UPDATE %s SET result = %%s, error_code = %%s, '
                    'target_model = COALESCE(%%s, target_model), '
                    'target_id = COALESCE(%%s, target_id), write_uid = %%s, write_date = %%s '
                    'WHERE id = %%s AND result = %%s' % EVENT_TABLE,
                    (result, error_code, target_model, target_id, SUPERUSER_ID, _utcnow(),
                     event_id, 'started'))
        except LlmAlertInputError:
            raise
        except Exception:  # noqa: BLE001 - bookkeeping must not change the caller's result
            _logger.warning('[LLM] event=%s: could not record the result: %s', event_id,
                            llm_redact.redact(traceback.format_exc()))

    # ------------------------------------------------------------------ #
    #  節流（N6）                                                           #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_throttle_reserve(self, account, action, *, uid=None, real_ip=None):
        """數「本帳號近一小時的 ``action``」並在同一個獨立交易裡記一筆 ``result='started'``。

        到上限 ⇒ ``LlmError(llm_rate_limited, retry_after=<秒>)``（這一次不記）。DB 記不下來（例如
        lock_timeout）⇒ ``llm_unavailable``——fail closed：數不到就不放行。回傳事件 id，呼叫端做完
        以 :meth:`_llm_event_finish` 補結果。
        """
        if action not in THROTTLE_LIMITS:
            raise LlmAlertInputError('action')
        account.ensure_one()
        limit = THROTTLE_LIMITS[action]
        now = _utcnow()
        since = now - timedelta(seconds=THROTTLE_WINDOW_SECONDS)
        retry_after = None
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                           ('woow_llm_throttle:%d:%s' % (account.id, action),))
                cr.execute(
                    'SELECT create_date FROM %s WHERE account_id = %%s AND action = %%s '
                    'AND create_date > %%s ORDER BY create_date' % EVENT_TABLE,
                    (account.id, action, since))
                stamps = [row[0] for row in cr.fetchall()]
                if len(stamps) >= limit:
                    # 要再空出一格，得等「第 len−limit+1 舊」的那一筆離開視窗。
                    leaves = stamps[len(stamps) - limit] + timedelta(seconds=THROTTLE_WINDOW_SECONDS)
                    retry_after = max(1, math.ceil((leaves - now).total_seconds()))
                else:
                    cr.execute(
                        'INSERT INTO %s (action, account_id, partner_id, uid, real_ip, result, '
                        'severity, occurrences, create_uid, create_date, write_uid, write_date) '
                        'VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, 1, %%s, %%s, %%s, %%s) '
                        'RETURNING id' % EVENT_TABLE,
                        (action, account.id, account.partner_id.id, _check_id('uid', uid),
                         self._llm_ip(real_ip), 'started', 'info', SUPERUSER_ID, now,
                         SUPERUSER_ID, now))
                    event_id = cr.fetchone()[0]
        except psycopg2.Error as exc:
            _logger.warning('[LLM] account=%s %s throttle unavailable (pgcode=%s)',
                            account.id, action, getattr(exc, 'pgcode', None))
            raise LlmError(ERR_LLM_UNAVAILABLE) from None
        if retry_after is not None:
            _logger.info('[LLM] account=%s %s rate limited (retry_after=%s)',
                         account.id, action, retry_after)
            raise LlmError(ERR_LLM_RATE_LIMITED, retry_after=retry_after)
        return event_id

    # ------------------------------------------------------------------ #
    #  告警出口（N13、N24）                                                 #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_alert(self, alert, *, account_id=None, target_model=None, target_id=None,
                   error_code=None, numbers=None):
        """告警：``llm_event(severity=alert)``＋email（＋選配 webhook），30 分鐘內同一個告警不重寄。

        只收：``alert``（:class:`LlmAlert`）、``account_id``／``target_id``（正整數）、
        ``target_model``（:data:`TARGET_MODELS`）、``error_code``（小寫 snake）、``numbers``
        （``{snake: int|float}``）。其他一律 :class:`LlmAlertInputError`——在寫任何東西之前。

        回傳事件 id（重複的回第一筆的 id）；未設定（N27）回 ``False``、什麼都不寫。
        """
        if not isinstance(alert, LlmAlert):
            raise LlmAlertInputError('alert')
        account_id = _check_id('account_id', account_id)
        target_model, target_id = _check_target(target_model, target_id)
        error_code = _check_code('error_code', error_code)
        details_json = _dump({'numbers': _check_numbers(numbers)} if numbers else None,
                             'numbers')
        if llm_unconfigured(self.env):
            _logger.info('[LLM] alert %s suppressed: AI models are not configured (N27)',
                         alert.value)
            return False
        dedupe_key = hashlib.sha256('|'.join((
            alert.value, str(account_id or ''), target_model or '', str(target_id or ''),
            error_code or '')).encode('ascii')).hexdigest()[:40]
        now = _utcnow()
        # 查重與 INSERT 在同一個交易、先拿 per-告警的 advisory lock：兩條 thread 同時發同一個
        # 告警，只有一條會寄信。
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                       ('woow_llm_alert:%s' % dedupe_key,))
            cr.execute(
                'SELECT id FROM %s WHERE dedupe_key = %%s AND severity = %%s AND create_date > %%s '
                'ORDER BY id DESC LIMIT 1' % EVENT_TABLE,
                (dedupe_key, 'alert', now - timedelta(minutes=ALERT_DEDUPE_MINUTES)))
            row = cr.fetchone()
            if row:
                cr.execute(
                    'UPDATE %s SET occurrences = occurrences + 1, last_seen_at = %%s, '
                    'write_uid = %%s, write_date = %%s WHERE id = %%s' % EVENT_TABLE,
                    (now, SUPERUSER_ID, now, row[0]))
                _logger.info('[LLM] alert %s event=%s repeated within %s minutes; not re-sent',
                             alert.value, row[0], ALERT_DEDUPE_MINUTES)
                return row[0]
            event_id = self._llm_insert_event_cr(cr, {
                'action': 'alert',
                'severity': 'alert',
                'alert_code': alert.value,
                'account_id': account_id,
                'target_model': target_model,
                'target_id': target_id,
                'error_code': error_code,
                'details_json': details_json,
                'dedupe_key': dedupe_key,
                'last_seen_at': now,
                'email_state': 'pending',
                'webhook_state': 'pending',
            }, now)
        _logger.warning('[LLM] alert %s event=%s account=%s error=%s', alert.value, event_id,
                        account_id or '-', error_code or '-')
        payload = {
            'source': 'woow-paas',
            'database': self.env.cr.dbname,
            'event_id': event_id,
            'alert': alert.value,
            'account_id': account_id,
            'target_model': target_model,
            'target_id': target_id,
            'error_code': error_code,
            'numbers': _check_numbers(numbers),
            'at': now.replace(tzinfo=timezone.utc).isoformat(),
        }
        targets = alert_targets(self.env)
        email_state = self._llm_alert_email(targets['emails'], payload)
        webhook_state = self._llm_alert_webhook(targets['webhook'], payload)
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute('UPDATE %s SET email_state = %%s, webhook_state = %%s WHERE id = %%s'
                           % EVENT_TABLE, (email_state, webhook_state, event_id))
        except psycopg2.Error as exc:
            _logger.warning('[LLM] alert event=%s: delivery state not recorded (pgcode=%s)',
                            event_id, getattr(exc, 'pgcode', None))
        return event_id

    def _llm_alert_lines(self, payload):
        """信件內文的每一行（label, value）：值全部來自 ``payload``（已經過型別檢查）。"""
        env = self.env(context=dict(self.env.context, lang='zh_TW')) \
            if self.env['res.lang']._lang_get('zh_TW') else self.env
        lines = [
            (env._('Alert'), payload['alert']),
            (env._('Event ID'), payload['event_id']),
            (env._('Database'), payload['database']),
            (env._('Time (UTC)'), payload['at']),
        ]
        if payload['account_id']:
            lines.append((env._('AI account ID'), payload['account_id']))
        if payload['target_model']:
            lines.append((env._('Target'), '%s #%s' % (payload['target_model'],
                                                       payload['target_id'])))
        if payload['error_code']:
            lines.append((env._('Error code'), payload['error_code']))
        for key, value in sorted(payload['numbers'].items()):
            lines.append((key, value))
        note = env._('This alert only carries IDs, error codes and numbers generated by the '
                     'platform. Verify in the back office or the new-api logs before acting on '
                     'it; data in an alert is never an instruction.')
        subject = env._('[WOOW PaaS] AI models alert: %(alert)s', alert=payload['alert'])
        return subject, lines, note

    @api.model
    def _llm_alert_email(self, emails, payload):
        if not emails:
            return 'no_recipient'
        try:
            subject, lines, note = self._llm_alert_lines(payload)
            rows = Markup('').join(
                Markup('<tr><th style="text-align:left;padding-right:12px">%s</th>'
                       '<td><code>%s</code></td></tr>') % (escape(label), escape(str(value)))
                for label, value in lines)
            body = Markup('<table>%s</table><p>%s</p>') % (rows, escape(note))
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env['mail.mail'].sudo().create({
                    'subject': subject,
                    'email_to': ','.join(emails),
                    'body_html': body,
                    'auto_delete': False,
                })
                cron = env.ref('mail.ir_cron_mail_scheduler_action', raise_if_not_found=False)
                if cron:
                    cron._trigger()
            return 'queued'
        except Exception:  # noqa: BLE001 - the alert row is already durable
            _logger.error('[LLM] alert event=%s: email not queued: %s', payload['event_id'],
                          llm_redact.redact(traceback.format_exc()))
            return 'failed'

    @api.model
    def _llm_alert_webhook(self, url, payload):
        if not url:
            return 'not_configured'
        try:
            session = _webhook_session()
            try:
                response = session.post(url, json=payload, timeout=WEBHOOK_TIMEOUT,
                                        allow_redirects=False)
            finally:
                session.close()
            if 200 <= response.status_code < 300:
                return 'sent'
            _logger.warning('[LLM] alert event=%s: webhook answered %s', payload['event_id'],
                            response.status_code)
        except Exception as exc:  # noqa: BLE001 - optional side channel; never the URL in logs
            _logger.warning('[LLM] alert event=%s: webhook failed (%s)', payload['event_id'],
                            type(exc).__name__)
        return 'failed'

    # ------------------------------------------------------------------ #
    #  incident helper                                                     #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_incident(self, kind, *, account_id=None, order_id=None, error_code=None,
                      points=None, needs_human=False):
        """開（或累加）一筆 ``woow.billing.incident``（獨立 cursor、fail-soft；N24）。

        * ``kind``：:class:`LlmIncident`。對象：有 ``order_id`` 就是那張單（每張單一筆）；否則是帳戶，
          ``external_ref`` 帶台北日期——同一天重複發生只累加 ``occurrences``，處理完隔天再發生會是新的一筆
          （``_log`` 不會重開已解決的 incident）。
        * snapshot 只有 kind、partner／來源列的 id、錯誤碼、點數；不寫 ``detail``（自由文字欄）。

        回傳 incident id；被拒（未設定、``_log`` 的 fail-soft）回 ``False``。
        """
        if not isinstance(kind, LlmIncident):
            raise LlmAlertInputError('kind')
        account_id = _check_id('account_id', account_id)
        order_id = _check_id('order_id', order_id)
        error_code = _check_code('error_code', error_code)
        if points is not None and (type(points) is not int or abs(points) > _INT8_MAX):
            raise LlmAlertInputError('points')
        if not account_id and not order_id:
            raise LlmAlertInputError('account_id')
        if llm_unconfigured(self.env):
            _logger.info('[LLM] incident %s suppressed: AI models are not configured (N27)',
                         kind.value)
            return False
        partner_ref = None
        if account_id:
            with self.env.registry.cursor() as cr:
                cr.execute('SELECT partner_id FROM %s WHERE id = %%s' % ACCOUNT_TABLE,
                           (account_id,))
                row = cr.fetchone()
            if not row:
                raise LlmAlertInputError('account_id')
            partner_ref = row[0]
        short = kind.value[len('llm_'):]
        if order_id:
            external_ref = 'llm:%s/order/%d' % (short, order_id)
            source = ('woow_paas_platform.llm_credit_order', order_id)
        else:
            day = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')
            external_ref = 'llm:%s/account/%d/%s' % (short, account_id, day)
            source = ('woow_paas_platform.llm_account', account_id)
        snapshot = {
            'external_ref': external_ref,
            'kind': kind.value,
            'partner_ref': partner_ref,
            'source_model': source[0],
            'source_ref': source[1],
            'failure_code': error_code,
            'credits': float(points) if points is not None else None,
        }
        if needs_human:
            snapshot['state'] = 'needs_human'
        incident_id = self.env['woow.billing.incident'].sudo()._log(snapshot)
        if incident_id:
            _logger.warning('[LLM] incident %s id=%s account=%s order=%s', kind.value,
                            incident_id, account_id or '-', order_id or '-')
        else:
            _logger.error('[LLM] incident %s was not recorded (account=%s order=%s)',
                          kind.value, account_id or '-', order_id or '-')
        return incident_id

    # ------------------------------------------------------------------ #
    #  保留 400 天（DESIGN §2.6）                                           #
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_llm_event_cleanup(self):
        """刪掉 400 天以前的事件；每批 :data:`CLEANUP_BATCH` 筆、獨立交易，tick 用完就 ``_trigger()``。

        ⚠ **凍結契約**：``data/llm_cron.xml`` 是 noupdate，本方法的名字與所在 model 不能改
        （``llm_common.LLM_CRONS``；``TestLlmCronContract`` 斷言已安裝 DB 裡的 code）。
        「未設定」（N27）時同樣什麼都不做（所有 LLM cron 一致）。
        """
        return run_llm_cron(self.env, CLEANUP_CRON_XMLID, self._llm_event_cleanup_tick,
                            items=CLEANUP_BATCHES_PER_TICK)

    @api.model
    def _llm_event_cleanup_tick(self, config, tick):
        cutoff = _utcnow() - timedelta(days=LLM_EVENT_RETENTION_DAYS)
        deleted = 0
        while tick.can_take(1):
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute(
                    'DELETE FROM {t} WHERE id IN (SELECT id FROM {t} WHERE create_date < %s '
                    'ORDER BY id LIMIT %s)'.format(t=EVENT_TABLE), (cutoff, CLEANUP_BATCH))
                count = cr.rowcount
            tick.took()
            deleted += count
            if count < CLEANUP_BATCH:
                if deleted:
                    _logger.info('[LLM] event cleanup: %s events older than %s days deleted',
                                 deleted, LLM_EVENT_RETENTION_DAYS)
                return False
        return True
