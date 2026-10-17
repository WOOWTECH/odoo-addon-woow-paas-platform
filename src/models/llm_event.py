"""AI 模型的稽核事件、節流、告警出口與 incident helper（DESIGN §2.6、§5.9；N6、N13、N24；票 LLM-02b）。

多張票共用的基礎都在這裡，其他票只呼叫、不各自實作：

1. **``llm_event``**（DESIGN §2.6）：system-only 的稽核表。所有群組 ACL 只有讀（system 與
   ``account.group_account_manager``）；寫入一律經本檔的 service method、以**獨立 cursor**
   直接 INSERT 並 commit（呼叫端的交易之後 rollback 也留得住）。ORM 的 create／write 另要
   ``llm_common._llm_internal`` 的把手（sudo 也擋），unlink 只在模組卸載時允許；保留 400 天，
   由 :meth:`LlmEvent._cron_llm_event_cleanup` 分批刪（每 10 分鐘的冪等 cron，N28）。

   ⚠ **前提：``account_id`` 必須已經 commit**。獨立 cursor 看不到呼叫端交易裡還沒 commit 的列
   （``account_id`` 是外鍵，partner 也從帳戶補），所以同一個請求剛建的帳戶（例如
   ``_llm_activate`` 之後的 ``activate`` 稽核）要用 :meth:`LlmEvent._llm_record_after_commit`：
   參數當下就驗，等呼叫端 commit 之後才寫。
2. **節流**（N6）：:meth:`LlmEvent._llm_throttle_reserve` 以 ``llm_event`` 近一小時的筆數計（平台
   沒有通用限流層，``controllers/oauth2.py:429-438`` 是唯一先例）。「數＋記一筆」在同一個獨立
   交易裡、先拿 per-(帳戶, 動作) 的 advisory lock，而且那個交易是 **READ COMMITTED**：Odoo 的
   cursor 預設 REPEATABLE READ，snapshot 在第一個查詢（＝拿鎖那一句、等鎖之前）就定了，等到鎖
   的那一方會用「對方 commit 之前」的 snapshot 去數（審查 LLM-02b finding 11 在 postgres 重現過：
   兩條各數到 9、各插一筆 ⇒ 11）。被擋下的那一次**不記**（否則一直重試會把視窗一直往後推）。
   結果由 :meth:`LlmEvent._llm_event_finish` 補上。``THROTTLE_LIMITS`` 裡的動作只能經這裡寫：
   :meth:`LlmEvent._llm_record` 拒收，否則另記一筆稽核就會被算兩次。
3. **告警出口**（N13、N24）：:meth:`LlmEvent._llm_alert` 寫 ``llm_event(severity=alert)``＋同一個交易裡
   排一封信到 ``llm_alert_email``；選配的 ``llm_alert_webhook_url`` **不在呼叫端送**（HTTP 請求裡最多
   卡 5 秒，N26）：標 ``webhook_state=pending``、``_trigger()`` :meth:`LlmEvent._cron_llm_alert_delivery`。
   同一個告警（代碼＋帳戶＋對象＋錯誤碼）30 分鐘內只寄一次（之後只把 ``occurrences`` 加 1）——但只有
   **送達的**才算數：上一次信件建不出來（``email_state=failed``）或 webhook 失敗，下一次觸發就在同一列
   重送並更新狀態。查重＋INSERT＋排信在同一個 READ COMMITTED 交易、先拿 per-告警的 advisory lock。
   **介面只收 id、錯誤碼、數字**：告警代碼是封閉的 :class:`LlmAlert`、對象 model 是封閉清單、錯誤碼
   與結果碼是封閉登記表（``llm_common.ERROR_CODES``／``RESULT_CODES``）、數字必須是 int／float——
   其他型別或值（含任何自由文字，例如金鑰的 ``key_label``，連長得像 snake 的指令也算）一律
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
from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    ALERT_DEDUPE_MINUTES,
    ERR_LLM_RATE_LIMITED,
    ERR_LLM_UNAVAILABLE,
    ERROR_CODES,
    LLM_EVENT_RETENTION_DAYS,
    RESULT_CODES,
    THROTTLE_LIMITS,
    THROTTLE_WINDOW_SECONDS,
    UNKNOWN_CODE,
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
DELIVERY_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_alert_delivery'
MAIL_CRON_XMLID = 'mail.ir_cron_mail_scheduler_action'
_LOCK_TIMEOUT = '5s'
#: 清理 cron：每批刪幾筆、一個 tick 最多幾批（N28）。
CLEANUP_BATCH = 5000
CLEANUP_BATCHES_PER_TICK = 20
#: webhook 是選配的旁路：短逾時、不重試、不跟 redirect；只在 delivery cron 裡送，結果記在事件上。
WEBHOOK_TIMEOUT = (2, 3)
#: delivery cron 一個 tick 最多送幾則。
DELIVERY_PER_TICK = 5
#: 「送達」的狀態：去重只把這些算成已經通知過（``pending``＝webhook 已排進 delivery cron）。
EMAIL_DELIVERED = ('queued', 'no_recipient')
WEBHOOK_DELIVERED = ('pending', 'sent', 'not_configured')

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

#: ``numbers``／``details`` 的 key（程式裡的字面值；值才是要擋的東西）。
_KEY_RE = re.compile(r'[a-z][a-z0-9_]{0,31}', re.ASCII)
_MAX_NUMBERS = 12
_INT8_MAX = 2 ** 63 - 1
#: ``details`` 的字串值：錯誤碼或結果碼（封閉登記表）。
DETAIL_CODES = ERROR_CODES | RESULT_CODES


class LlmAlert(enum.Enum):
    """告警代碼（封閉清單）。新增一種告警＝在這裡加一個成員（值是小寫 snake）。

    值會出現在信件主旨與 webhook；它是程式常數，不是資料。
    """

    #: 驗證告警出口用（odoo shell 觸發；LLM-02b AC2）。
    TEST = 'alert_test'
    #: 開戶放棄（``state=error``，第 6 次失敗或不可重試的錯誤）；錯誤碼＝``last_error_code``。
    PROVISION_FAILED = 'provision_failed'
    #: 密文被拒（綁定不符、解不開、內容不合）——有人動過 DB 或加密金鑰換掉了（DESIGN §2.9）。
    SECRET_REJECTED = 'secret_rejected'
    #: LLM cron 的工作函式當掉（``llm_common.run_llm_cron`` 收掉例外之後）；錯誤碼＝該 cron 的固定碼。
    CRON_FAILED = 'cron_failed'
    #: 設定類的 fail closed：加密金鑰、代理簽章金鑰、前綴／block（``llm_common.CONFIG_ERROR_CODES``）。
    CONFIG_UNAVAILABLE = 'config_unavailable'


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


def safe_code(value, fallback=UNKNOWN_CODE):
    """呼叫端手上的錯誤碼 → 登記表裡的碼（``llm_common.ERROR_CODES``）；不在表裡一律 ``fallback``。

    給「錯誤碼來自別處（``LlmError.code``、``_ProvisionStop.code``、``LlmCryptoError.code``）」的呼叫端
    用。**不正規化**（不 strip、不轉小寫）：任意字串不會因為「整理一下」就變成合法的碼。告警 helper
    本身對登記表外的碼是**拒絕**。
    """
    if isinstance(value, str) and value in ERROR_CODES:
        return value
    return fallback


def _check_id(name, value):
    """正整數 id；上限是 int4（``target_id``、外鍵都是 int4——超過要在寫任何東西之前就拒絕）。"""
    if value is None:
        return None
    if type(value) is not int or value <= 0 or value > nac.INT32_MAX:
        raise LlmAlertInputError(name)
    return value


def _check_code(name, value, allowed=ERROR_CODES):
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
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
    """稽核事件的 ``details``：值只能是數字、布林、``None`` 或登記表裡的碼（:data:`DETAIL_CODES`）。"""
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
        elif isinstance(value, str) and value in DETAIL_CODES:
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


def alert_dedupe_key(alert, account_id=None, target_model=None, target_id=None, error_code=None):
    """同一個告警的識別（代碼＋帳戶＋對象＋錯誤碼）；參數已經驗過。"""
    return hashlib.sha256('|'.join((
        alert.value, str(account_id or ''), target_model or '', str(target_id or ''),
        error_code or '')).encode('ascii')).hexdigest()[:40]


def _begin(env, cr, read_committed=False):
    """獨立交易的開頭：``lock_timeout``；``read_committed`` ⇒ 這個交易改成 READ COMMITTED。

    「拿鎖之後再數」要看得到對方剛 commit 的列：REPEATABLE READ 的 snapshot 在拿鎖那一句（等鎖
    之前）就定了。``SET TRANSACTION`` 必須在第一個查詢之前（``SET LOCAL`` 不算查詢）。test mode
    （HttpCase）的獨立 cursor 是同一條測試交易的 savepoint，不能改隔離等級——那裡也只有一條連線，
    沒有並發可言；兩條真連線的並發由 ``TestLlmConcurrency``（TransactionCase）驗。
    """
    cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
    if read_committed and not env.registry.in_test_mode():
        cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')


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
    webhook_state = fields.Char(readonly=True, help='pending / sent / failed / not_configured')

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
        帳戶在這條連線上看不到（呼叫端還沒 commit）⇒ ``LlmAlertInputError('account_id')``，什麼都沒寫。
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
            _begin(self.env, cr)
            return self._llm_insert_event_cr(cr, values, _utcnow())

    @api.model
    def _llm_record_values(self, action, *, account_id=None, partner_id=None, uid=None,
                           real_ip=None, target_model=None, target_id=None, result='ok',
                           error_code=None, severity='info', details=None):
        """驗 :meth:`_llm_record` 的參數、回傳要 INSERT 的值（不寫任何東西）。"""
        if action not in ACTIONS or action == 'alert':
            raise LlmAlertInputError('action')
        if action in THROTTLE_LIMITS:
            # 節流以這些動作的列數計：只能經 _llm_throttle_reserve／_llm_event_finish 寫，
            # 否則另記一筆稽核就會被算兩次（審查 LLM-02b finding 5）。
            raise LlmAlertInputError('action')
        if severity not in SEVERITIES or severity == 'alert':
            raise LlmAlertInputError('severity')
        target_model, target_id = _check_target(target_model, target_id)
        return {
            'action': action,
            'account_id': _check_id('account_id', account_id),
            'partner_id': _check_id('partner_id', partner_id),
            'uid': _check_id('uid', uid),
            'real_ip': self._llm_ip(real_ip),
            'target_model': target_model,
            'target_id': target_id,
            'result': _check_code('result', result, RESULT_CODES),
            'error_code': _check_code('error_code', error_code),
            'severity': severity,
            'details_json': _dump(_check_details(details), 'details'),
        }

    @api.model
    def _llm_record(self, action, **kwargs):
        """寫一筆稽核事件（獨立交易；呼叫端之後 rollback 也留得住）。回傳 id。

        * ``details`` 只收數字、布林、``None``、登記表裡的碼（白名單形狀，見 :func:`_check_details`）。
        * ``result``：:data:`llm_common.RESULT_CODES`；``error_code``：:data:`llm_common.ERROR_CODES`。
        * 告警請用 :meth:`_llm_alert`（``action='alert'`` 不能從這裡寫）；``THROTTLE_LIMITS`` 的動作
          （``key_create``、``convert``）只能經 :meth:`_llm_throttle_reserve` 寫。
        * ⚠ ``account_id`` 必須已經 commit（見模組 docstring 第 1 點）；同一個交易剛建的帳戶用
          :meth:`_llm_record_after_commit`。
        """
        return self._llm_insert_event(self._llm_record_values(action, **kwargs))

    @api.model
    def _llm_record_after_commit(self, action, **kwargs):
        """同 :meth:`_llm_record`，但等呼叫端的交易 commit 之後才寫（帳戶是這個交易剛建的）。

        參數**現在**就驗（不合格立刻 :class:`LlmAlertInputError`）；commit 之後由
        ``cr.postcommit`` 以獨立 cursor 寫；呼叫端 rollback ⇒ 不寫。寫入失敗只記 log（交易已經
        commit，沒有呼叫端可以回報）。不回傳事件 id。
        """
        values = self._llm_record_values(action, **kwargs)
        registry = self.env.registry
        insert = self._llm_insert_event_cr

        def _write():
            try:
                with registry.cursor() as cr:
                    cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                    insert(cr, values, _utcnow())
            except Exception:  # noqa: BLE001 - the caller's transaction is already committed
                _logger.error('[LLM] %s event after commit not recorded: %s', values['action'],
                              llm_redact.redact(traceback.format_exc()))

        self.env.cr.postcommit.add(_write)

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
            result = _check_code('result', result, RESULT_CODES)
            error_code = _check_code('error_code', error_code)
            target_model, target_id = _check_target(target_model, target_id)
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr)
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
        以 :meth:`_llm_event_finish` 補結果。並發：advisory lock＋READ COMMITTED（模組 docstring 第 2 點）。
        """
        if action not in THROTTLE_LIMITS:
            raise LlmAlertInputError('action')
        account.ensure_one()
        uid = _check_id('uid', uid)
        real_ip = self._llm_ip(real_ip)
        limit = THROTTLE_LIMITS[action]
        now = _utcnow()
        since = now - timedelta(seconds=THROTTLE_WINDOW_SECONDS)
        retry_after = None
        try:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
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
                        (action, account.id, account.partner_id.id, uid, real_ip, 'started',
                         'info', SUPERUSER_ID, now, SUPERUSER_ID, now))
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

        只收：``alert``（:class:`LlmAlert`）、``account_id``／``target_id``（正整數，≤ int4）、
        ``target_model``（:data:`TARGET_MODELS`）、``error_code``（:data:`llm_common.ERROR_CODES`）、
        ``numbers``（``{snake: int|float}``）。其他一律 :class:`LlmAlertInputError`——在寫任何東西之前。

        * 事件、信件（``mail.mail``）與 delivery cron 的觸發在**同一個**獨立交易（READ COMMITTED、
          per-告警 advisory lock）：並發的兩個同樣告警只會有一列、一封信。
        * webhook 不在這裡送（HTTP 請求裡不能卡 5 秒，N26）：``webhook_state=pending``，由
          :meth:`_cron_llm_alert_delivery` 送。
        * 30 分鐘內同一個告警：已送達（:data:`EMAIL_DELIVERED`／:data:`WEBHOOK_DELIVERED`）⇒ 只累加
          ``occurrences``；上次失敗的那一路（信件建不出來、webhook 失敗）在同一列重送並更新狀態。
        * ⚠ ``account_id`` 必須已經 commit（模組 docstring 第 1 點）。DB 錯誤（例如 lock_timeout）往外丟，
          呼叫端自己決定（``llm_common`` 的 report helper 一律收掉、只記 log）。

        回傳事件 id（重複的回第一筆的 id）；未設定（N27）回 ``False``、什麼都不寫。
        """
        if not isinstance(alert, LlmAlert):
            raise LlmAlertInputError('alert')
        account_id = _check_id('account_id', account_id)
        target_model, target_id = _check_target(target_model, target_id)
        error_code = _check_code('error_code', error_code)
        numbers = _check_numbers(numbers)
        details_json = _dump({'numbers': numbers} if numbers else None, 'numbers')
        if llm_unconfigured(self.env):
            _logger.info('[LLM] alert %s suppressed: AI models are not configured (N27)',
                         alert.value)
            return False
        dedupe_key = alert_dedupe_key(alert, account_id, target_model, target_id, error_code)
        now = _utcnow()
        targets = alert_targets(self.env)
        payload = {
            'source': 'woow-paas',
            'database': self.env.cr.dbname,
            'event_id': None,
            'alert': alert.value,
            'account_id': account_id,
            'target_model': target_model,
            'target_id': target_id,
            'error_code': error_code,
            'numbers': numbers,
            'at': now.replace(tzinfo=timezone.utc).isoformat(),
        }
        repeated = False
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr, read_committed=True)
            cr.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                       ('woow_llm_alert:%s' % dedupe_key,))
            cr.execute(
                'SELECT id, email_state, webhook_state, create_date FROM %s '
                'WHERE dedupe_key = %%s AND severity = %%s AND create_date > %%s '
                'ORDER BY id DESC LIMIT 1' % EVENT_TABLE,
                (dedupe_key, 'alert', now - timedelta(minutes=ALERT_DEDUPE_MINUTES)))
            row = cr.fetchone()
            if row:
                repeated = True
                event_id, email_state, webhook_state, created = row
                payload.update(event_id=event_id,
                               at=created.replace(tzinfo=timezone.utc).isoformat())
                if email_state not in EMAIL_DELIVERED:
                    email_state = self._llm_alert_queue_email(cr, targets['emails'], payload)
                if webhook_state not in WEBHOOK_DELIVERED:
                    webhook_state = self._llm_alert_queue_webhook(cr, targets['webhook'])
                cr.execute(
                    'UPDATE %s SET occurrences = occurrences + 1, last_seen_at = %%s, '
                    'email_state = %%s, webhook_state = %%s, write_uid = %%s, write_date = %%s '
                    'WHERE id = %%s' % EVENT_TABLE,
                    (now, email_state, webhook_state, SUPERUSER_ID, now, event_id))
            else:
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
                    'email_state': 'failed',
                    'webhook_state': 'failed',
                }, now)
                payload['event_id'] = event_id
                email_state = self._llm_alert_queue_email(cr, targets['emails'], payload)
                webhook_state = self._llm_alert_queue_webhook(cr, targets['webhook'])
                cr.execute('UPDATE %s SET email_state = %%s, webhook_state = %%s WHERE id = %%s'
                           % EVENT_TABLE, (email_state, webhook_state, event_id))
        if repeated:
            _logger.info('[LLM] alert %s event=%s repeated within %s minutes (email=%s webhook=%s)',
                         alert.value, event_id, ALERT_DEDUPE_MINUTES, email_state, webhook_state)
        else:
            _logger.warning('[LLM] alert %s event=%s account=%s error=%s (email=%s webhook=%s)',
                            alert.value, event_id, account_id or '-', error_code or '-',
                            email_state, webhook_state)
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
    def _llm_alert_queue_email(self, cr, emails, payload):
        """在告警的交易（``cr``）裡建 ``mail.mail``＋觸發寄信 cron；回 ``queued``／``no_recipient``／``failed``。

        包在 savepoint 裡：信件建不出來不影響告警那一列（狀態記 ``failed``，下一次觸發重送）。
        """
        if not emails:
            return 'no_recipient'
        try:
            subject, lines, note = self._llm_alert_lines(payload)
            rows = Markup('').join(
                Markup('<tr><th style="text-align:left;padding-right:12px">%s</th>'
                       '<td><code>%s</code></td></tr>') % (escape(label), escape(str(value)))
                for label, value in lines)
            body = Markup('<table>%s</table><p>%s</p>') % (rows, escape(note))
            env = api.Environment(cr, SUPERUSER_ID, {})
            with cr.savepoint():
                env['mail.mail'].sudo().create({
                    'subject': subject,
                    'email_to': ','.join(emails),
                    'body_html': body,
                    'auto_delete': False,
                })
                cron = env.ref(MAIL_CRON_XMLID, raise_if_not_found=False)
                if cron:
                    cron._trigger()
            return 'queued'
        except Exception:  # noqa: BLE001 - the alert row itself must still be written
            _logger.error('[LLM] alert event=%s: email not queued: %s', payload['event_id'],
                          llm_redact.redact(traceback.format_exc()))
            return 'failed'

    @api.model
    def _llm_alert_queue_webhook(self, cr, url):
        """有 webhook ⇒ ``pending``＋在告警的交易裡 ``_trigger()`` delivery cron；沒有 ⇒ ``not_configured``。"""
        if not url:
            return 'not_configured'
        env = api.Environment(cr, SUPERUSER_ID, {})
        cron = env.ref(DELIVERY_CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron._trigger()
        return 'pending'

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
    #  webhook delivery cron（N13、N26；LLM-02b）                          #
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_llm_alert_delivery(self):
        """送 ``webhook_state=pending`` 的告警（每 tick ≤ :data:`DELIVERY_PER_TICK` 則、≤ 30 秒）。

        ⚠ **凍結契約**（同其他 LLM cron）：``data/llm_cron.xml`` 是 noupdate，本方法的名字與所在 model
        不能改（``llm_common.LLM_CRONS``）。告警 helper 每次排 webhook 都 ``_trigger()`` 它；排程的
        10 分鐘只是保險。同一支 cron 同時只會跑一份（``ir.cron`` 的列鎖），送出與記狀態之間行程死掉
        ＝下一次再送一次（至少一次）。
        """
        return run_llm_cron(self.env, DELIVERY_CRON_XMLID, self._llm_alert_delivery_tick,
                            items=DELIVERY_PER_TICK)

    @api.model
    def _llm_alert_delivery_tick(self, config, tick):
        with self.env.registry.cursor() as cr:
            _begin(self.env, cr)
            cr.execute(
                'SELECT id, alert_code, account_id, target_model, target_id, error_code, '
                'details_json, create_date FROM %s WHERE severity = %%s AND webhook_state = %%s '
                'ORDER BY id LIMIT %%s' % EVENT_TABLE, ('alert', 'pending', tick.items + 1))
            rows = cr.dictfetchall()
        if not rows:
            return False
        url = alert_targets(self.env)['webhook']
        for row in rows:
            if not tick.can_take(sum(WEBHOOK_TIMEOUT)):
                return True
            tick.took()
            state = self._llm_alert_webhook(url, self._llm_alert_payload(row)) if url \
                else 'not_configured'
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr)
                cr.execute('UPDATE %s SET webhook_state = %%s, write_uid = %%s, write_date = %%s '
                           'WHERE id = %%s AND webhook_state = %%s' % EVENT_TABLE,
                           (state, SUPERUSER_ID, _utcnow(), row['id'], 'pending'))
        return False

    @api.model
    def _llm_alert_payload(self, row):
        """webhook 的 body：全部取自事件那一列（寫入時已經驗過的 id、碼、數字）。"""
        numbers = {}
        if row['details_json']:
            try:
                numbers = json.loads(row['details_json']).get('numbers') or {}
            except (ValueError, AttributeError):
                numbers = {}
        return {
            'source': 'woow-paas',
            'database': self.env.cr.dbname,
            'event_id': row['id'],
            'alert': row['alert_code'],
            'account_id': row['account_id'],
            'target_model': row['target_model'],
            'target_id': row['target_id'],
            'error_code': row['error_code'],
            'numbers': numbers,
            'at': row['create_date'].replace(tzinfo=timezone.utc).isoformat(),
        }

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
        * ⚠ ``account_id`` 必須已經 commit（模組 docstring 第 1 點）。

        回傳 incident id；被拒（未設定、DB 錯誤、``_log`` 的 fail-soft）回 ``False``。
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
            try:
                with self.env.registry.cursor() as cr:
                    _begin(self.env, cr)
                    cr.execute('SELECT partner_id FROM %s WHERE id = %%s' % ACCOUNT_TABLE,
                               (account_id,))
                    row = cr.fetchone()
            except psycopg2.Error as exc:
                _logger.error('[LLM] incident %s not recorded: partner lookup failed (pgcode=%s)',
                              kind.value, getattr(exc, 'pgcode', None))
                return False
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

        每 10 分鐘跑一次的冪等 cron（N28：每日性質的工作不設成每天一次）：多數時候 ``create_date``
        索引上一筆都撈不到，幾乎不花成本。
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
                _begin(self.env, cr)
                # 依 create_date 由舊到新（走 create_date 索引；沒有過期列時立刻是空的）。
                cr.execute(
                    'DELETE FROM {t} WHERE id IN (SELECT id FROM {t} WHERE create_date < %s '
                    'ORDER BY create_date LIMIT %s)'.format(t=EVENT_TABLE), (cutoff, CLEANUP_BATCH))
                count = cr.rowcount
            tick.took()
            deleted += count
            if count < CLEANUP_BATCH:
                if deleted:
                    _logger.info('[LLM] event cleanup: %s events older than %s days deleted',
                                 deleted, LLM_EVENT_RETENTION_DAYS)
                return False
        return True
