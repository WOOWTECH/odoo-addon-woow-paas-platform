"""AI 模型分頁（LLM）的共用常數、設定讀取與欄位型別（DESIGN §2.2、§2.11、§3.4）。

本檔集中三件事，讓 model、controller、cron 都讀同一份判準：

1. **設定**（DESIGN §2.11）：全部是「模組常數＋``get_param()``」寫法、**不 seed 預設值**
   （缺值時由這裡的常數提供，避免每次發版的 ``-u`` 重設），而且全部登記在
   ``hooks._CONFIG_PARAMETERS``（``test_llm_tracer`` 有一支登記測試補 repo-lint 的盲區）。
2. **未設定狀態**（N27）：``newapi_proxy_url`` 空或是黑洞值、或（``show_llm`` 關、pilot
   名單空、``llm_canary_partner_id`` 空）⇒ 所有 LLM cron 第一行就 return（不外呼、不寫
   incident、不告警），``/api/llm/*`` 回 ``llm_disabled``。例行發版會在 LLM-17 之前就把表與
   ``active=True`` 的 cron 帶上 prod，這個狀態保證它們在 prod 什麼都不做。
3. **錯誤碼**（DESIGN §3.4）：常數名 ``ERR_*``、值小寫 snake；前端 ``llm_errors.js``
   依 code 組句，後端 ``error`` 只是 fallback。
4. **cron 框架**（N27、N28；LLM-02b）：:data:`LLM_CRONS` 是全部 LLM cron 的登記表（測試以它
   跑「未設定零外呼」與 noupdate 凍結契約）；:func:`run_llm_cron` 是每支 LLM cron 的唯一入口：
   未設定 ⇒ 不呼叫工作函式；有設定 ⇒ 給一個 :class:`LlmCronTick`（牆鐘秒數＋件數上限），
   工作函式回「還有剩」就 ``_trigger()`` 同一支 cron 接著跑。
"""
import logging
import re
import time
import traceback

from odoo import _, fields
from odoo.exceptions import ValidationError

from ..services import llm_redact
from ..services import newapi_client as nac

_logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  設定 key（DESIGN §2.11）                                                     #
# --------------------------------------------------------------------------- #
PARAM_SHOW_LLM = 'woow_paas_platform.show_llm'
PARAM_PILOT_PARTNER_IDS = 'woow_paas_platform.llm_pilot_partner_ids'
PARAM_PROXY_URL = nac.PARAM_PROXY_URL
PARAM_USERNAME_PREFIX = 'woow_paas_platform.newapi_username_prefix'
PARAM_IP_BLOCK = 'woow_paas_platform.llm_synthetic_ip_block'
PARAM_CANARY_PARTNER_ID = 'woow_paas_platform.llm_canary_partner_id'
#: N13：告警信的收件人（負責人；逗號分隔最多 5 個）。沒設＝告警只寫 ``llm_event``、不寄信。
PARAM_ALERT_EMAIL = 'woow_paas_platform.llm_alert_email'
#: N13（選配）：告警 webhook，只收 ``https://``；沒設＝不送。
PARAM_ALERT_WEBHOOK_URL = 'woow_paas_platform.llm_alert_webhook_url'

#: AI 模型分頁的全部 key；``hooks._CONFIG_PARAMETERS`` 必須涵蓋（登記測試守）。
LLM_CONFIG_PARAMETERS = (
    PARAM_SHOW_LLM,
    PARAM_PILOT_PARTNER_IDS,
    PARAM_PROXY_URL,
    PARAM_USERNAME_PREFIX,
    PARAM_IP_BLOCK,
    PARAM_CANARY_PARTNER_ID,
    PARAM_ALERT_EMAIL,
    PARAM_ALERT_WEBHOOK_URL,
)

# --------------------------------------------------------------------------- #
#  常數（不可設定；DESIGN §2.11 末、§4.1）                                       #
# --------------------------------------------------------------------------- #
#: D5：1 點＝15,625 quota（R＝32 永不改）。
QUOTA_PER_POINT = 15625
#: 條款版本：LLM-04 之前先用常數；之後改由 ``llm_terms`` 的 published 版提供。
TOS_VERSION_CURRENT = 'draft-0'
#: D10：每帳號最多 20 把金鑰。
MAX_KEYS_PER_ACCOUNT = 20
#: D11：免費試用每天約 200 次（每日上限在 LLM-10）。
FREE_DAILY_LIMIT = 200
#: N6：單次轉換下限／上限（LLM-08）。
CONVERT_MIN_POINTS = 50
CONVERT_MAX_POINTS = 10000
#: DESIGN §1.3.1：重試退避（分鐘）；連續 6 次失敗 ⇒ ``state=error``。
PROVISION_BACKOFF_MINUTES = (1, 2, 5, 15, 60)
PROVISION_MAX_ATTEMPTS = 6
PROVISION_LEASE_MINUTES = 5
#: N6：每帳號每小時的次數上限（以 ``llm_event`` 近一小時的筆數計）。轉換在 LLM-08 用。
THROTTLE_WINDOW_SECONDS = 3600
THROTTLE_LIMITS = {
    'key_create': 10,
    'convert': 10,
}
#: N13：同一個告警（代碼＋帳戶＋對象＋錯誤碼）30 分鐘內只寄一次。
ALERT_DEDUPE_MINUTES = 30
#: DESIGN §2.6：``llm_event`` 保留 400 天。
LLM_EVENT_RETENTION_DAYS = 400

# --------------------------------------------------------------------------- #
#  錯誤碼（DESIGN §3.4）                                                        #
# --------------------------------------------------------------------------- #
ERR_LLM_DISABLED = 'llm_disabled'
ERR_LLM_NOT_ACTIVATED = 'llm_not_activated'
ERR_LLM_TOS_REQUIRED = 'llm_tos_required'
ERR_LLM_TOS_OUTDATED = 'llm_tos_outdated'
ERR_LLM_PROVISIONING = 'llm_provisioning'
ERR_LLM_SUSPENDED = 'llm_suspended'
ERR_LLM_CLOSED = 'llm_closed'
ERR_LLM_UNAVAILABLE = 'llm_unavailable'
ERR_LLM_RATE_LIMITED = 'llm_rate_limited'
ERR_LLM_KEY_LIMIT = 'llm_key_limit'
ERR_LLM_FREE_KEY_EXISTS = 'llm_free_key_exists'
ERR_LLM_KEY_ALREADY_SHOWN = 'llm_key_already_shown'
ERR_LLM_KEY_INVALID = 'llm_key_invalid'
#: 開戶失敗（``state=error``）的帳戶操作建金鑰時。DESIGN 的碼表沒有這一格：error 狀態在
#: §6.2 只有畫面，沒有 API 錯誤碼；沿用 ``llm_unavailable`` 會讓前端以為只是暫時連不上。
ERR_LLM_ACCOUNT_ERROR = 'llm_account_error'
#: handler 的 catch-all（程式錯誤）。固定訊息、不帶任何例外文字。
ERR_LLM_INTERNAL = 'llm_internal_error'
#: gate 的帳號拒絕（``paas._account_denial``：未登入或帳號停用）。那個 helper 回的 dict 沒有
#: ``code``；本分頁的回應一律帶小寫 snake code（DESIGN §3.1），所以在 controller 補上這一格。
ERR_LLM_AUTH_REQUIRED = 'llm_auth_required'


class LlmError(Exception):
    """service 層的業務錯誤：``code`` 是上面的 ``ERR_*``，``params`` 只放數字等安全參數。"""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


# --------------------------------------------------------------------------- #
#  內部寫入把手（DESIGN §2.1：所有寫入只經 service method；〔REV〕8）              #
# --------------------------------------------------------------------------- #
# LLM model 對所有群組都是 perm_create／write／unlink＝0，但 ``sudo()`` 繞得過 ACL。
# create／write override 再擋一層：只有帶著這個 context 值的呼叫能寫。判準是值的**物件
# 身分**（``is``）——``/web/dataset/call_kw`` 會把呼叫端送來的 context 整包換進 env，
# JSON 載不了物件身分，所以偽造不了（#522 實測過裸 truthy 版可以被偽造）。
# repo-lint ``internal-ctx-key-registry`` 登記這把（status enforced）。
class _LlmInternalWrite:
    """LLM service method 內部寫入的 marker type。"""

    __slots__ = ()

    def __repr__(self):
        return '<llm internal write>'


_LLM_INTERNAL_WRITE = _LlmInternalWrite()
_LLM_INTERNAL_CTX_KEY = 'woow_llm_internal_write'


def _llm_internal(records):
    """回傳帶著內部寫入把手的 ``records``（只在真正落地的那一次呼叫上掛）。"""
    return records.with_context(**{_LLM_INTERNAL_CTX_KEY: _LLM_INTERNAL_WRITE})


def _is_llm_internal(env):
    """判準：只比對 context 值的物件身分。"""
    return env.context.get(_LLM_INTERNAL_CTX_KEY) is _LLM_INTERNAL_WRITE


def _without_llm_internal(records):
    """拿掉內部寫入把手：``create`` 回傳的記錄不帶著它（Odoo 的 ``create`` 回傳的記錄沿用
    呼叫時的 env，不拿掉的話把手會跟著回傳值流出去；要再寫就再掛一次 ``_llm_internal``）。"""
    if _LLM_INTERNAL_CTX_KEY not in records.env.context:
        return records
    context = {k: v for k, v in records.env.context.items() if k != _LLM_INTERNAL_CTX_KEY}
    return records.with_env(records.env(context=context))


# --------------------------------------------------------------------------- #
#  int8 的 quota 欄位（〔REV〕30）                                              #
# --------------------------------------------------------------------------- #
class LlmQuotaInteger(fields.Integer):
    """quota 單位的欄位一律用 PostgreSQL ``int8``。

    Odoo 18 的 ``fields.Integer`` 是 int4，上限 2,147,483,647 quota（約 137,439 點）；
    ``quota + used_quota`` 是累計值，月消費 NT$1 萬多的帳號約一年就會溢位，而 new-api 端是
    64 位元。點數計算一律用 Python int，不經過 float。

    只覆寫 ``_column_type``，其餘交給 Odoo 18 本身（``odoo/fields.py``，18.0-20260926 對過）：

    * ``Field.column_type`` 是 ``lazy_property``：非 company_dependent、非 translate 的欄位
      直接回 ``self._column_type`` ⇒ 這裡設 ``('int8', 'int8')`` 就是 int8（``Integer`` 自己
      也只是把 ``_column_type`` 設成 ``('int4', 'int4')``）。不另外蓋 ``column_type``：
      那會繞過 lazy_property 的 jsonb 分支與 ``Field.setup`` 結尾的 ``reset_all``。
    * ``MetaField.__init__`` 只在 ``type`` 還沒登記時才寫 ``MetaField.by_type[type]``
      ⇒ 本子類別（``type='integer'``）不會取代 ``fields.Integer`` 成為手動欄位與
      ``ir.model.fields`` 用的預設實作，不需要也不應該去改 ``by_type``。

    ``test_llm_tracer.TestLlmQuotaInteger`` 在沙盒驗：欄位真的是 ``bigint``、寫入 2^40 讀回
    相等、``by_type['integer']`` 仍是 ``fields.Integer``。
    """

    _column_type = ('int8', 'int8')


# --------------------------------------------------------------------------- #
#  設定讀取                                                                     #
# --------------------------------------------------------------------------- #
_PILOT_RE = re.compile(r'[0-9]{1,12}')
#: 名單有值但格式不合：不能因此變成「名單空＝全部開放」，改成誰都不在名單內。
_PILOT_INVALID = frozenset({-1})


def _truthy(value):
    return (value or '').strip().lower() in ('true', '1')


def _parse_pilot(raw):
    text = (raw or '').strip()
    if not text:
        return frozenset()
    ids = set()
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        if not _PILOT_RE.fullmatch(part):
            _logger.warning('[LLM] %s is malformed; nobody is treated as a pilot partner',
                            PARAM_PILOT_PARTNER_IDS)
            return _PILOT_INVALID
        ids.add(int(part))
    return frozenset(ids)


def _parse_block(raw):
    text = (raw or '').strip()
    if not text:
        return 0
    if not re.fullmatch(r'[0-9]{1,2}', text):
        return None
    value = int(text)
    return value if 0 <= value <= nac.MAX_BLOCK else None


def llm_config(env):
    """一次讀完 LLM 設定（ir.config_parameter 有 ormcache，便宜）。"""
    icp = env['ir.config_parameter'].sudo()
    proxy_state, proxy_url = nac.proxy_url_state(icp.get_param(PARAM_PROXY_URL))
    if proxy_state == 'invalid':
        _logger.warning('[LLM] %s holds a value outside the allow-list; treated as unconfigured',
                        PARAM_PROXY_URL)
    prefix = (icp.get_param(PARAM_USERNAME_PREFIX) or nac.PROD_USERNAME_PREFIX).strip()
    block = _parse_block(icp.get_param(PARAM_IP_BLOCK))
    identity_ok = True
    try:
        nac.check_prefix_block(prefix, block)
    except nac.NewApiConfigError:
        identity_ok = False
    canary = (icp.get_param(PARAM_CANARY_PARTNER_ID) or '').strip()
    return {
        'proxy_state': proxy_state,
        'proxy_url': proxy_url,
        'show': _truthy(icp.get_param(PARAM_SHOW_LLM)),
        'pilot': _parse_pilot(icp.get_param(PARAM_PILOT_PARTNER_IDS)),
        'canary': canary,
        'prefix': prefix,
        'block': block,
        'identity_ok': identity_ok,
    }


def llm_unconfigured(env, config=None):
    """N27：「未設定」⇒ 所有 LLM cron 直接 return。**所有 LLM cron 第一行就呼叫它。**"""
    config = config or llm_config(env)
    if config['proxy_state'] != 'usable':
        return True
    return not config['show'] and not config['pilot'] and not config['canary']


def llm_visible(env, partner, config=None):
    """這個 partner 看得到 AI 模型分頁嗎（後端 gate 與 ``woow_show_llm`` 共用）。"""
    config = config or llm_config(env)
    if llm_unconfigured(env, config) or not config['show']:
        return False
    if config['pilot']:
        return bool(partner) and partner.id in config['pilot']
    return True


def validate_llm_param(key, value):
    """``ir.config_parameter`` 寫入守衛（DESIGN §2.11，〔REV〕15）：不合法 ⇒ ValidationError。

    只管本票的 key；空值（刪除／清空）一律允許＝未設定。讀取時 ``llm_config`` 會再驗一次，
    所以 SQL 直改也只會落到「未設定」。
    """
    if key not in LLM_CONFIG_PARAMETERS:
        return
    text = (value or '').strip() if isinstance(value, str) or value is None else str(value)
    if not text:
        return
    if key == PARAM_PROXY_URL and text not in nac.ALLOWED_PROXY_URLS:
        raise ValidationError(_(
            'The AI models proxy URL must be one of the in-cluster proxy addresses.'))
    if key == PARAM_USERNAME_PREFIX and not nac.USERNAME_PREFIX_RE.fullmatch(text):
        raise ValidationError(_(
            'The new-api username prefix must be "w" followed by one to three lowercase letters.'))
    if key == PARAM_IP_BLOCK and _parse_block(text) is None:
        raise ValidationError(_('The synthetic IP block must be an integer from 0 to 31.'))
    if key == PARAM_PILOT_PARTNER_IDS and _parse_pilot(text) is _PILOT_INVALID:
        raise ValidationError(_('The pilot list must be comma-separated partner IDs.'))
    if key == PARAM_CANARY_PARTNER_ID and not _PILOT_RE.fullmatch(text):
        raise ValidationError(_('The canary partner must be a partner ID.'))
    if key == PARAM_ALERT_EMAIL and parse_alert_emails(text) is None:
        raise ValidationError(_(
            'The AI models alert email must be one to five comma-separated email addresses.'))
    if key == PARAM_ALERT_WEBHOOK_URL and not valid_alert_webhook_url(text):
        raise ValidationError(_(
            'The AI models alert webhook must be an https:// URL without credentials.'))


_EMAIL_RE = re.compile(
    r"[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+", re.ASCII)
_WEBHOOK_RE = re.compile(r'https://[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/%-]*)?',
                         re.ASCII)
_MAX_ALERT_EMAILS = 5


def parse_alert_emails(raw):
    """``llm_alert_email`` → 收件人 tuple；空值 → ``()``；格式不合 → ``None``（讀取端當成沒設）。"""
    text = (raw or '').strip()
    if not text:
        return ()
    parts = [part.strip() for part in text.split(',')]
    if not parts or len(parts) > _MAX_ALERT_EMAILS or not all(
            _EMAIL_RE.fullmatch(part) for part in parts):
        return None
    return tuple(parts)


def valid_alert_webhook_url(raw):
    """只收 ``https://host[:port][/path]``（不收帳密、query、fragment；≤ 512 字元）。"""
    text = (raw or '').strip()
    return bool(text) and len(text) <= 512 and bool(_WEBHOOK_RE.fullmatch(text))


def alert_targets(env):
    """告警出口的設定（讀取時再驗一次；SQL 直改成不合法的值＝當成沒設）。"""
    icp = env['ir.config_parameter'].sudo()
    emails = parse_alert_emails(icp.get_param(PARAM_ALERT_EMAIL))
    if emails is None:
        _logger.warning('[LLM] %s is malformed; alert emails are not sent', PARAM_ALERT_EMAIL)
        emails = ()
    webhook = (icp.get_param(PARAM_ALERT_WEBHOOK_URL) or '').strip()
    if webhook and not valid_alert_webhook_url(webhook):
        _logger.warning('[LLM] %s is malformed; the alert webhook is not called',
                        PARAM_ALERT_WEBHOOK_URL)
        webhook = ''
    return {'emails': emails, 'webhook': webhook or None}


# --------------------------------------------------------------------------- #
#  cron 框架（N27、N28；LLM-02b）                                               #
# --------------------------------------------------------------------------- #
#: 全部 LLM cron：xmlid → (model, 方法名)。``data/llm_cron.xml`` 是 noupdate，方法名與 model 是
#: 凍結契約（改名要同 PR 寫 migration 改 ``ir_act_server.code``）；``TestLlmCronContract`` 對已
#: 安裝 DB 逐支斷言，``TestLlmUnconfiguredZeroOutbound`` 逐支手動觸發。新增 LLM cron 一律登記在這。
LLM_CRONS = {
    'woow_paas_platform.ir_cron_llm_provision': (
        'woow_paas_platform.llm_account', '_cron_llm_provision'),
    'woow_paas_platform.ir_cron_llm_balance_refresh': (
        'woow_paas_platform.llm_account', '_cron_llm_balance_refresh'),
    'woow_paas_platform.ir_cron_llm_event_cleanup': (
        'woow_paas_platform.llm_event', '_cron_llm_event_cleanup'),
}


class LlmCronTick:
    """一個 cron tick 的預算（N28）：牆鐘 ``seconds`` 秒、最多 ``items`` 件。

    工作函式每做一件之前問 :meth:`can_take`（需要的秒數給 ``need_seconds``）；答否時
    ``more`` 會被設成 True，:func:`run_llm_cron` 就 ``_trigger()`` 下一個 tick。外呼一律帶
    ``deadline=tick.deadline``，讓 client 依剩餘預算截斷 timeout。
    """

    __slots__ = ('deadline', 'items', 'done', 'more', '_monotonic')

    def __init__(self, seconds, items, monotonic=time.monotonic):
        self._monotonic = monotonic
        self.deadline = monotonic() + seconds
        self.items = items
        self.done = 0
        self.more = False

    def remaining(self):
        return self.deadline - self._monotonic()

    def can_take(self, need_seconds=0):
        """還能再做一件嗎（件數沒滿、剩下的秒數 ≥ ``need_seconds``）；不能就標 ``more``。"""
        if self.done >= self.items or self.remaining() < need_seconds:
            self.more = True
            return False
        return True

    def took(self, count=1):
        self.done += count


def run_llm_cron(env, xmlid, work, *, seconds=nac.CRON_TICK_SECONDS, items, config=None):
    """每支 LLM cron 的唯一入口（N27、N28）。

    * ``config``：呼叫端已經讀好的 :func:`llm_config`（沒給就在這裡讀）。model 檔各自以自己
      import 的 ``llm_config`` 讀好再傳進來——committed 測試只在本行程 patch 那個名字、不 commit
      任何設定（``tests/llm_test_support.py``）。
    * **未設定 ⇒ 直接 return**：不呼叫 ``work``——不建 client、不外呼、不寫 incident、不告警。
    * ``work(config, tick)`` 回 truthy（或 ``tick.more``）⇒ ``_trigger()`` 同一支 cron。
    * ``work`` 丟的例外在這裡收掉：traceback 經 :func:`llm_redact.redact` 才進 log，主 cursor
      回到 savepoint（工作函式的寫入都走獨立 cursor，已 commit 的不受影響）。不往外丟是刻意的：
      Odoo 18 對連續失敗的 cron 會自動停用（``ir_cron._update_failure_count``），而 LLM cron
      被靜默停用＝對帳、刷新全停。下一個排程照常再跑。
    """
    config = config or llm_config(env)
    if llm_unconfigured(env, config):
        return False
    tick = LlmCronTick(seconds, items)
    try:
        with env.cr.savepoint():
            more = work(config, tick)
    except Exception:  # noqa: BLE001 - see docstring
        _logger.error('[LLM] cron %s crashed: %s', xmlid,
                      llm_redact.redact(traceback.format_exc()))
        return False
    more = bool(more) or tick.more
    if more:
        cron = env.ref(xmlid, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
    return more
