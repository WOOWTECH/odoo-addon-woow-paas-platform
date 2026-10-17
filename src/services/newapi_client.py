"""PaaS → newapi-proxy → new-api 的 HTTP client（AI 模型分頁，DESIGN §1.4.2、§3.4、§3.5）。

線上契約的權威在 ``extra/newapi-proxy/README.md``〈線上契約〉；本檔是它在 Odoo 端的實作：

* **HMAC 簽章**（N23）：每個請求帶 ``X-Woow-Proxy-Kid``／``-Ts``／``-Nonce``／``-Sig``，
  簽章字串與 ``extra/newapi-proxy/newapi_proxy/signing.py`` 逐位元一致（``tests/
  test_llm_services.py`` 拿代理的參考實作驗章）。**每個請求、每次重送都產生新的 nonce**。
* **身分**：``ap``（代理注入 AP）、``user``（帶 UP 或開戶時的 JWT，必帶租戶合成 IP）、
  ``anon``。``op``／``bg`` 在 LLM-02。
* **金鑰**：``WOOW_NEWAPI_PROXY_AUTH_WEB``（web 程式路徑）與 ``WOOW_NEWAPI_PROXY_AUTH_CRON``
  （cron）。值一律 strip 後驗證格式；缺或不合 ⇒ :class:`NewApiConfigError`（訊息只有原因
  碼）。web 路徑沒有 WEB 金鑰就 fail closed，不退回 CRON。WEB 金鑰的 client 只能叫
  :data:`WEB_METHODS`（代理 routetable 裡 ``web=True`` 的路由）；叫其他方法在送出前就丟
  :class:`NewApiInputError`——web 路徑誤用 cron 路由會在本機就紅，不必等代理 403。
  ``tests/test_llm_services.py`` 的契約測試逐一比對每個方法的路由、身分、query 與
  ``web`` 旗標和代理的 ``routetable.py``。
* **結果分類**（§3.4）：每次呼叫回傳 :class:`Ok`／:class:`Definite`／:class:`NotExecuted`／
  :class:`Unknown` 其中之一。傳輸錯誤只有「socket 根本沒連上」（``ConnectTimeout``、cause
  是 ``NewConnectionError``／DNS 解析失敗）才算 NotExecuted；request 已寫出才斷線、
  ``ReadTimeout``、``ChunkedEncodingError`` 一律 Unknown——**不照抄**
  ``services/paas_operator.py`` 整包 ``except ConnectionError``。
* **不用連線池**：每次呼叫開新的 ``requests.Session``、``Connection: close``、
  ``Retry(total=0)``、``trust_env=False``、不跟 redirect——連線池裡過期的 socket 不會把寫入
  變成「結果未知」，也不會讀到 env 裡的 proxy 設定。
* **例外**：requests／urllib3 的例外**不往外拋、不寫進 log**，只轉成分類；本檔自己拋的例外
  一律 ``raise … from None``、``args`` 只有原因碼。requests 遇到含換行的標頭值丟
  ``InvalidHeader``，訊息帶完整的標頭值（簽章金鑰、UP）——所以所有標頭值在送出前就先驗形狀。
* **log**：一次呼叫一行，只有 method、路徑樣板、身分、status、分類、耗時、request id；
  經 :func:`llm_redact.redact`。不記任何 body 與標頭值。

本檔不 import odoo（只有 :func:`get_newapi_client` 收一個 env 讀設定），所以
``tests/test_llm_services.py`` 可以在沒有 Odoo 的環境直接載入。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import NewConnectionError
from urllib3.util.retry import Retry

from . import llm_redact

_logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  設定                                                                         #
# --------------------------------------------------------------------------- #

ENV_AUTH_WEB = 'WOOW_NEWAPI_PROXY_AUTH_WEB'
ENV_AUTH_CRON = 'WOOW_NEWAPI_PROXY_AUTH_CRON'
ROLE_WEB = 'web'
ROLE_CRON = 'cron'
_ROLE_ENV = {ROLE_WEB: ENV_AUTH_WEB, ROLE_CRON: ENV_AUTH_CRON}

#: DESIGN §2.11：``newapi_proxy_url`` 只接受這三個值；黑洞值（neutralize）視同未設定。
PROXY_URL_PROD = 'http://newapi-proxy.new-api.svc.cluster.local:8080'
PROXY_URL_STG = 'http://newapi-proxy.new-api-stg.svc.cluster.local:8080'
PROXY_URL_BLACKHOLE = 'http://127.0.0.1:9'
ALLOWED_PROXY_URLS = frozenset({PROXY_URL_PROD, PROXY_URL_STG, PROXY_URL_BLACKHOLE})
USABLE_PROXY_URLS = frozenset({PROXY_URL_PROD, PROXY_URL_STG})

# --------------------------------------------------------------------------- #
#  逾時階梯（DESIGN §3.5；前端 > 平台 > 代理 > new-api）                            #
# --------------------------------------------------------------------------- #
#: 互動唯讀、同步（``/api/llm/usage/recent`` 在 LLM-02 改 SWR 之前、``usage/logs``）。
TIMEOUT_INTERACTIVE_READ = (2, 3)
#: 互動寫入（update、extend、status、delete；LLM-07）。
TIMEOUT_INTERACTIVE_WRITE = (3, 8)
#: 建金鑰的每一次呼叫；三次呼叫合計受 ``KEY_CREATE_BUDGET_SECONDS`` 截斷。
TIMEOUT_KEY_CREATE = (3, 8)
KEY_CREATE_BUDGET_SECONDS = 20
#: cron 開戶 P1–P9；一個 tick 合計 ``PROVISION_TICK_SECONDS``（N28）。
TIMEOUT_PROVISION = (5, 15)
PROVISION_TICK_SECONDS = 60
#: P8（撤 session）是清理步驟：tick 預算用完時另給這麼多秒讓它還能送出。
SESSION_REVOKE_GRACE_SECONDS = 5
#: cron 讀取（對帳、計數、SWR 背景刷新、價目）。
TIMEOUT_CRON_READ = (5, 20)
#: 剩下的預算不夠一次呼叫時就不送（送了也只會變成 Unknown）。
MIN_CALL_SECONDS = 2
#: ``X-Woow-Read-Timeout``＝本次 read timeout − 這個餘裕，夾在代理接受的 1–``PROXY_READ_TIMEOUT_MAX``
#: 秒（代理 ``app.py`` ``_READ_TIMEOUT_RE``）：代理的總期限一定比 Odoo 的 read timeout 早到，
#: 分類由代理給出、不會變成 Odoo 先逾時的 Unknown（DESIGN §1.4.2-7、§3.5）。
#: repo-lint ``newapi-timeout-ladder`` 守這幾個數字之間的大小關係。
PROXY_READ_TIMEOUT_MARGIN = 1
PROXY_READ_TIMEOUT_MAX = 30

# --------------------------------------------------------------------------- #
#  合成 IP 與使用者名稱（DESIGN §1.4.2-4、N14）                                   #
# --------------------------------------------------------------------------- #
SYNTHETIC_BASE = ipaddress.IPv4Address('198.18.0.0')
TENANT_LAST = ipaddress.IPv4Address('198.19.255.255')
TENANT_OFFSET = 256
BLOCK_SIZE = 4096
MAX_BLOCK = 31
#: block 0（prod）可容納的最大帳號 id：198.18.0.0 + 256 + 130815 = 198.19.255.255。
PROD_MAX_ACCOUNT_ID = 130815
PROD_USERNAME_PREFIX = 'wp'
USERNAME_PREFIX_RE = re.compile(r'w[a-z]{1,3}', re.ASCII)
USERNAME_RE = re.compile(r'w[a-z]{1,3}[0-9]{10}', re.ASCII)

#: UP 的 scope（排序後；DESIGN §1.3.1）與代理 body 政策的 UP_SCOPES 一致。
UP_SCOPES = ('api_key:read', 'api_key:reveal', 'api_key:write', 'profile:read', 'usage:read')
UP_LIFETIME_SECONDS = 90 * 86400
#: 兩個自架 Qwen（DESIGN §1.3.3；代理 ``PROXY_FREE_MODELS`` 的預設值）。
FREE_TRIAL_MODELS = ('qwen3-4b-aicoder', 'qwen3-4b-cluster1')
FREE_TRIAL_LIFETIME_SECONDS = 90 * 86400

#: new-api 回的英文原句（``message_key``）——依它分類，不受語系影響。
MSG_LOGIN_FAILED = 'Username or password is incorrect, or user has been banned'

#: WEB 金鑰（Odoo web 程式路徑）能叫的 client 方法＝代理 routetable 裡 ``web=True`` 的路由
#: （DESIGN §1.4.2-1）。其餘方法（開戶、PAT、session、ap）只給 CRON 金鑰。
WEB_METHODS = frozenset({
    'user_self', 'user_create_token', 'user_list_tokens', 'user_get_token',
    'user_reveal_token', 'user_log_self',
})

# --------------------------------------------------------------------------- #
#  格式（與代理 ``app.py`` 的 regex 一致；送出前就先驗，避免 InvalidHeader 帶出值）  #
# --------------------------------------------------------------------------- #
_KID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}', re.ASCII)
_SECRET_RE = re.compile(r'[A-Za-z0-9_-]{32,256}', re.ASCII)
_JWT = r'[A-Za-z0-9_-]{4,2048}\.[A-Za-z0-9_-]{4,4096}\.[A-Za-z0-9_-]{4,1024}'
JWT_RE = re.compile(_JWT, re.ASCII)
UP_RE = re.compile(r'nap_[A-Za-z0-9]{20,128}', re.ASCII)
SID_RE = re.compile(r'[A-Za-z0-9_-]{1,128}', re.ASCII)
_QUERY_VALUE_RE = re.compile(r'[A-Za-z0-9_.:/-]{1,256}', re.ASCII)
_QUERY_KEY_RE = re.compile(r'[a-z_]{1,32}', re.ASCII)
_REQUEST_ID_RE = re.compile(r'[A-Za-z0-9._-]{1,128}', re.ASCII)
_RETRY_AFTER_RE = re.compile(r'[0-9]{1,6}', re.ASCII)
_CODE_RE = re.compile(r'[A-Z][A-Z0-9_]{0,63}', re.ASCII)
_PROXY_REASON_RE = re.compile(r'[a-z0-9_]{1,64}', re.ASCII)
_BASE_URL_RE = re.compile(r'http://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?', re.ASCII)
INT32_MAX = 2_147_483_647

#: 代理錯誤 class → 分類（README〈回應與錯誤〉）。
_PROXY_NOT_EXECUTED = frozenset({'connect', 'busy', 'budget', 'body_timeout', 'config'})
_PROXY_UNKNOWN = frozenset({'reset', 'timeout', 'internal'})
#: 代理判定 PaaS 送錯了（簽章、白名單、body 政策）：確定沒執行，而且要開 circuit（LLM-02）。
_PROXY_BUG = frozenset({'auth', 'policy', 'bad_request', 'too_large'})


# --------------------------------------------------------------------------- #
#  例外（一律 ``raise … from None``；args 只有原因碼）                              #
# --------------------------------------------------------------------------- #
class NewApiError(Exception):
    code = 'newapi_error'

    def __init__(self, code=None):
        if code:
            self.code = code
        super().__init__(self.code)


class NewApiConfigError(NewApiError):
    """設定或 env 缺／不合（沒有送出任何東西）。"""

    code = 'newapi_config'


class NewApiInputError(NewApiError):
    """呼叫端給的值不合契約（程式錯誤；沒有送出任何東西）。"""

    code = 'newapi_input'


class NewApiTransportError(NewApiError):
    """client 自己的未預期錯誤（不是 requests 的傳輸錯誤——那些轉成分類）。"""

    code = 'newapi_transport'


# --------------------------------------------------------------------------- #
#  結果分類                                                                     #
# --------------------------------------------------------------------------- #
class Result:
    """一次呼叫的結果。``repr`` 不含任何 body，可以安全地出現在 log 與 traceback。"""

    __slots__ = ('status', 'request_id', 'retry_after')
    kind = 'result'
    ok = False

    def __init__(self, status=None, request_id=None, retry_after=None):
        self.status = status
        self.request_id = request_id
        self.retry_after = retry_after

    def label(self):
        return self.kind

    def __repr__(self):
        return '<%s %s status=%s>' % (type(self).__name__, self.label(), self.status)


class Ok(Result):
    __slots__ = ('data', 'message')
    kind = 'ok'
    ok = True

    def __init__(self, data=None, message=None, **kw):
        super().__init__(**kw)
        self.data = data
        self.message = message


class Definite(Result):
    """確定沒執行（new-api 明確拒絕、代理明確拒絕）。

    * ``code``：new-api 的穩定錯誤碼（``AUTH_UNAUTHORIZED``…），優先於 ``message_key``。
    * ``message_key``：new-api 的英文原句。
    * ``proxy_error``／``reason``：代理自己的拒絕（``auth``／``policy``／``denied``…）。
    * ``precheck``：代理轉回預檢的 new-api 回答時的 ``reveal``／``manage``。
    * ``no_message_key``：HTTP 200 ``success:false`` 但沒有 ``message_key``——只有唯讀呼叫會
      歸到這裡（寫入呼叫是 Unknown）。
    """

    __slots__ = ('code', 'message_key', 'proxy_error', 'reason', 'precheck')
    kind = 'definite'

    def __init__(self, code=None, message_key=None, proxy_error=None, reason=None,
                 precheck=None, **kw):
        super().__init__(**kw)
        self.code = code
        self.message_key = message_key
        self.proxy_error = proxy_error
        self.reason = reason
        self.precheck = precheck

    @property
    def circuit(self):
        """代理認定 PaaS 送錯（簽章、白名單、body 政策）——DESIGN §3.6 會開 circuit。"""
        return self.proxy_error in _PROXY_BUG

    def label(self):
        return self.code or (self.proxy_error and 'proxy_%s' % self.proxy_error) or (
            'message_key' if self.message_key else self.reason or 'definite')


class NotExecuted(Result):
    """確定沒送到 new-api（或 new-api 在 handler 之前就擋下），可以安全重送。"""

    __slots__ = ('reason',)
    kind = 'not_executed'

    def __init__(self, reason='not_executed', **kw):
        super().__init__(**kw)
        self.reason = reason

    def label(self):
        return self.reason


class Unknown(Result):
    """送出了但不知道結果；寫入不重送。"""

    __slots__ = ('reason',)
    kind = 'unknown'

    def __init__(self, reason='unknown', **kw):
        super().__init__(**kw)
        self.reason = reason

    def label(self):
        return self.reason


# --------------------------------------------------------------------------- #
#  純函式                                                                       #
# --------------------------------------------------------------------------- #
def parse_signing_key(raw):
    """``kid:secret[,kid:secret]`` → 第一把 ``(kid, secret_bytes)``；整串都要合格式。"""
    if raw is None or not str(raw).strip():
        raise NewApiConfigError('signing_key_missing') from None
    entries = [part.strip() for part in str(raw).strip().split(',')]
    parsed = []
    for entry in entries:
        kid, sep, secret = entry.partition(':')
        if not sep or not _KID_RE.fullmatch(kid) or not _SECRET_RE.fullmatch(secret):
            raise NewApiConfigError('signing_key_invalid') from None
        parsed.append((kid, secret.encode('ascii')))
    return parsed[0]


def tenant_ip(account_id, block):
    """租戶合成 IP：``198.18.0.0 + 256 + block × 4096 + account_id``；超出就 fail closed。"""
    if type(account_id) is not int or type(block) is not int:
        raise NewApiInputError('synthetic_ip') from None
    if not 0 <= block <= MAX_BLOCK or account_id < 1:
        raise NewApiInputError('synthetic_ip') from None
    if block > 0 and account_id >= BLOCK_SIZE:
        raise NewApiInputError('synthetic_ip') from None
    offset = TENANT_OFFSET + block * BLOCK_SIZE + account_id
    if SYNTHETIC_BASE + offset > TENANT_LAST:
        raise NewApiInputError('synthetic_ip') from None
    return str(SYNTHETIC_BASE + offset)


def newapi_username(prefix, account_id):
    """``<prefix>%010d``（固定寬度讓 admin search 的 ``LIKE %kw%`` 等於完全比對）。"""
    if not isinstance(prefix, str) or not USERNAME_PREFIX_RE.fullmatch(prefix):
        raise NewApiInputError('username_prefix') from None
    if type(account_id) is not int or not 1 <= account_id <= 9_999_999_999:
        raise NewApiInputError('username_id') from None
    return '%s%010d' % (prefix, account_id)


def check_prefix_block(prefix, block):
    """prod 固定 ``wp``＋block 0；stg 沙盒是其他前綴＋block 1–31（N14）。"""
    if not isinstance(prefix, str) or not USERNAME_PREFIX_RE.fullmatch(prefix):
        raise NewApiConfigError('username_prefix') from None
    if type(block) is not int or not 0 <= block <= MAX_BLOCK:
        raise NewApiConfigError('ip_block') from None
    if (prefix == PROD_USERNAME_PREFIX) != (block == 0):
        raise NewApiConfigError('prefix_block_mismatch') from None


def up_name(account_id, now=None):
    """UP 的名稱：``woow-paas-up-<id>-<YYYYMMDDHHMM>``（UTC；代理 ``UP_NAME_RE``）。"""
    now = now if now is not None else time.time()
    stamp = datetime.fromtimestamp(int(now), tz=timezone.utc).strftime('%Y%m%d%H%M')
    return 'woow-paas-up-%d-%s' % (account_id, stamp)


def free_trial_token_body(name, expired_time):
    """免費試用金鑰的 ``POST /api/token/`` body（DESIGN §1.3.3；欄位一個不少）。

    ``remain_quota: 1`` 是保險絲：免費請求不碰 token 額度；萬一 ``GroupRatio.free`` 被改成
    大於 0，token 預扣會先失敗、錢包不會被扣。
    """
    if not isinstance(name, str) or not re.fullmatch(r'wp_[0-9]{1,12}_[a-z0-9]{4}', name):
        raise NewApiInputError('token_name') from None
    if type(expired_time) is not int or expired_time <= 0:
        raise NewApiInputError('token_expired_time') from None
    return {
        'name': name,
        'expired_time': expired_time,
        'remain_quota': 1,
        'unlimited_quota': False,
        'model_limits_enabled': True,
        'model_limits': ','.join(FREE_TRIAL_MODELS),
        'allow_ips': '',
        'group': 'free',
        'cross_group_retry': False,
        'allow_user_quota_query': False,
    }


def canonical_query(pairs):
    return '&'.join('%s=%s' % (k, v) for k, v in sorted(pairs))


def string_to_sign(method, path, query, body, ts, nonce, kid, identity, client_ip):
    """與 ``extra/newapi-proxy/newapi_proxy/signing.py`` 的 ``string_to_sign`` 相同。"""
    return '\n'.join([
        method, path, query, hashlib.sha256(body).hexdigest(), ts, nonce, kid, identity,
        client_ip,
    ]).encode('ascii')


def sign(secret, message):
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _iter_causes(exc):
    """走訪例外鏈（``args``、``reason``、``__cause__``、``__context__``），不碰訊息文字。"""
    seen = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen or not isinstance(cur, BaseException):
            continue
        seen.add(id(cur))
        yield cur
        stack.extend(arg for arg in getattr(cur, 'args', ()) if isinstance(arg, BaseException))
        stack.append(getattr(cur, 'reason', None))
        stack.append(cur.__cause__)
        stack.append(cur.__context__)


def classify_transport_error(exc):
    """requests 的例外 → 分類（§3.4）。只有 socket 根本沒連上才是 NotExecuted。"""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return NotExecuted('connect_timeout')
    if isinstance(exc, (requests.exceptions.InvalidHeader, requests.exceptions.InvalidURL,
                        requests.exceptions.MissingSchema, requests.exceptions.InvalidSchema,
                        requests.exceptions.URLRequired)):
        # 在 prepare 階段就被擋下，沒有送出。
        return NotExecuted('request_invalid')
    if isinstance(exc, requests.exceptions.ConnectionError):
        for cause in _iter_causes(exc):
            # urllib3 2.x 的 NameResolutionError 是 NewConnectionError 的子類別。
            if isinstance(cause, NewConnectionError):
                return NotExecuted('connect')
        return Unknown('transport')
    return Unknown('transport')


def _parse_retry_after(value):
    if value and _RETRY_AFTER_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def classify_response(status, headers, body, write):
    """HTTP 回應 → 分類（DESIGN §3.4；代理錯誤見 README〈回應與錯誤〉）。

    ``headers`` 是大小寫不敏感的 mapping；``body`` 是 bytes。
    """
    request_id = headers.get('X-Oneapi-Request-Id')
    request_id = request_id if request_id and _REQUEST_ID_RE.fullmatch(request_id) else None
    retry_after = _parse_retry_after(headers.get('Retry-After'))
    common = {'status': status, 'request_id': request_id, 'retry_after': retry_after}

    proxy_error = headers.get('X-Woow-Proxy-Error')
    if proxy_error:
        proxy_error = proxy_error.strip()
        reason = None
        try:
            parsed = json.loads(body.decode('utf-8')) if body else None
            if isinstance(parsed, dict) and isinstance(parsed.get('reason'), str) \
                    and _PROXY_REASON_RE.fullmatch(parsed['reason']):
                reason = parsed['reason']
        except (ValueError, UnicodeDecodeError):
            reason = None
        if proxy_error in _PROXY_NOT_EXECUTED:
            return NotExecuted('proxy_%s' % proxy_error, **common)
        if proxy_error in _PROXY_UNKNOWN:
            return Unknown('proxy_%s' % proxy_error, **common)
        if proxy_error in _PROXY_BUG or proxy_error == 'denied':
            return Definite(proxy_error=proxy_error, reason=reason, **common)
        return Unknown('proxy_unrecognised', **common)

    precheck = headers.get('X-Woow-Proxy-Precheck')
    precheck = precheck.strip() if precheck and precheck.strip() in ('reveal', 'manage') else None

    if not body or not body.strip():
        if status == 429:
            return NotExecuted('rate_limited', **common)
        if status >= 500:
            # 限流器連不到 Valkey：在 handler 之前就失敗（〔PROV〕§1.1）。
            return NotExecuted('upstream_unavailable', **common)
        return Unknown('empty_body', **common)
    try:
        parsed = json.loads(body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return Unknown('non_json', **common)
    if not isinstance(parsed, dict):
        return Unknown('non_json', **common)

    code = parsed.get('code')
    if isinstance(code, str) and code:
        code = code if _CODE_RE.fullmatch(code) else 'UNRECOGNISED'
        return Definite(code=code, precheck=precheck, **common)
    error = parsed.get('error')
    if isinstance(error, dict) and error.get('type') == 'new_api_panic':
        return Unknown('new_api_panic', **common)
    success = parsed.get('success')
    if status == 200 and success is True:
        message = parsed.get('message')
        return Ok(data=parsed.get('data'), message=message if isinstance(message, str) else None,
                  **common)
    if success is False:
        message_key = parsed.get('message_key')
        if isinstance(message_key, str) and message_key:
            return Definite(message_key=message_key, precheck=precheck, **common)
        if write:
            # DB 錯誤這類沒有 message_key 的 success:false——寫入可能已 commit。
            return Unknown('no_message_key', **common)
        return Definite(reason='no_message_key', precheck=precheck, **common)
    return Unknown('unrecognised_body', **common)


# --------------------------------------------------------------------------- #
#  client                                                                     #
# --------------------------------------------------------------------------- #
class NewApiClient:
    """一個 client＝一組簽章金鑰（WEB 或 CRON）＋一個代理位址。

    每個方法都標明唯讀／寫入與 timeout 類別；``deadline``（``time.monotonic()`` 的絕對值）
    給了就以剩餘預算截斷 timeout，剩不到 ``MIN_CALL_SECONDS`` 就不送（NotExecuted）。
    """

    def __init__(self, base_url, kid, secret, role, *, clock=time.time,
                 monotonic=time.monotonic, session_factory=None):
        # 白名單在工廠（``get_newapi_client``）與 ``ir.config_parameter`` 守衛；這裡只擋
        # 形狀，讓單元測試能指向 loopback。
        if not isinstance(base_url, str) or not _BASE_URL_RE.fullmatch(base_url):
            raise NewApiConfigError('proxy_url') from None
        if role not in _ROLE_ENV:
            raise NewApiConfigError('role') from None
        if not isinstance(kid, str) or not _KID_RE.fullmatch(kid):
            raise NewApiConfigError('signing_key_invalid') from None
        if not isinstance(secret, bytes) or not _SECRET_RE.fullmatch(secret.decode('ascii', 'replace')):
            raise NewApiConfigError('signing_key_invalid') from None
        self.base_url = base_url
        self.role = role
        self._kid = kid
        self._secret = secret
        self._clock = clock
        self._monotonic = monotonic
        self._session_factory = session_factory or self._new_session

    def __repr__(self):
        return '<NewApiClient role=%s>' % self.role

    # ---- transport ------------------------------------------------------ #
    @staticmethod
    def _new_session():
        session = requests.Session()
        session.trust_env = False
        retry = Retry(total=0, connect=0, read=0, redirect=0, status=0, other=0,
                      raise_on_redirect=False, raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def _timeout(self, timeout, deadline):
        connect, read = timeout
        if deadline is None:
            return connect, read
        remaining = deadline - self._monotonic()
        if remaining < MIN_CALL_SECONDS:
            return None
        return min(connect, remaining), min(read, remaining)

    @staticmethod
    def _path(template, path_params):
        out = template
        for name, value in (path_params or {}).items():
            if name == 'sid':
                if not isinstance(value, str) or not SID_RE.fullmatch(value):
                    raise NewApiInputError('path_param') from None
                text = value
            else:
                if type(value) is not int or not 1 <= value <= INT32_MAX:
                    raise NewApiInputError('path_param') from None
                text = str(value)
            out = out.replace('{%s}' % name, text)
        if '{' in out:
            raise NewApiInputError('path_param') from None
        return out

    @staticmethod
    def _query(query):
        pairs = []
        for key, value in (query or {}).items():
            if value is None:
                continue
            value = str(value)
            if not _QUERY_KEY_RE.fullmatch(key) or not _QUERY_VALUE_RE.fullmatch(value):
                raise NewApiInputError('query') from None
            pairs.append((key, value))
        return canonical_query(pairs)

    def _request(self, method, template, *, identity, write, timeout, deadline=None,
                 path_params=None, query=None, body=None, client_ip='', bearer=None,
                 proof=None, web=False):
        """送一個簽過章的請求並分類。requests 的例外不會離開本方法。

        ``web``：這條路由在代理那邊是否允許 WEB 金鑰（routetable 的 ``web``）。WEB client
        叫 ``web=False`` 的路由 ⇒ 送出前就丟 :class:`NewApiInputError`。
        """
        if self.role == ROLE_WEB and not web:
            raise NewApiInputError('web_key_scope') from None
        path = self._path(template, path_params)
        query_string = self._query(query)
        if identity == 'user':
            if not isinstance(client_ip, str) or not client_ip:
                raise NewApiInputError('client_ip') from None
            try:
                parsed_ip = ipaddress.IPv4Address(client_ip)
            except ValueError:
                raise NewApiInputError('client_ip') from None
            if not SYNTHETIC_BASE + TENANT_OFFSET <= parsed_ip <= TENANT_LAST:
                raise NewApiInputError('client_ip') from None
        elif client_ip:
            raise NewApiInputError('client_ip') from None
        if bearer is not None and not (isinstance(bearer, str)
                                       and (UP_RE.fullmatch(bearer) or JWT_RE.fullmatch(bearer))):
            raise NewApiInputError('bearer_shape') from None
        if proof is not None and not (isinstance(proof, str) and JWT_RE.fullmatch(proof)):
            raise NewApiInputError('proof_shape') from None
        payload = b''
        if body is not None:
            payload = json.dumps(body, separators=(',', ':'), ensure_ascii=True,
                                 allow_nan=False).encode('ascii')

        effective = self._timeout(timeout, deadline)
        if effective is None:
            result = NotExecuted('deadline')
            self._log(method, template, identity, result, 0)
            return result
        connect, read = effective
        ts = str(int(self._clock()))
        nonce = secrets.token_urlsafe(16)
        signature = sign(self._secret, string_to_sign(
            method, path, query_string, payload, ts, nonce, self._kid, identity, client_ip))
        headers = {
            'X-Woow-Proxy-Kid': self._kid,
            'X-Woow-Proxy-Ts': ts,
            'X-Woow-Proxy-Nonce': nonce,
            'X-Woow-Proxy-Sig': signature,
            'X-Woow-Identity': identity,
            'X-Woow-Read-Timeout': str(max(1, min(PROXY_READ_TIMEOUT_MAX,
                                                  int(read) - PROXY_READ_TIMEOUT_MARGIN))),
            'Accept': 'application/json',
            'Connection': 'close',
        }
        if client_ip:
            headers['X-Woow-Client-IP'] = client_ip
        if payload:
            headers['Content-Type'] = 'application/json'
        if bearer is not None:
            headers['Authorization'] = 'Bearer %s' % bearer
        if proof is not None:
            headers['X-Security-Proof'] = proof
        url = self.base_url + path + ('?' + query_string if query_string else '')

        started = self._monotonic()
        try:
            session = self._session_factory()
            try:
                response = session.request(
                    method, url, data=payload or None, headers=headers,
                    timeout=(connect, read), allow_redirects=False)
                content = response.content
                status = response.status_code
                response_headers = response.headers
            finally:
                session.close()
        except requests.exceptions.RequestException as exc:
            result = classify_transport_error(exc)
        except Exception:  # noqa: BLE001 - nothing from the stack may carry header values out
            result = Unknown('client_internal')
            self._log(method, template, identity, result, self._monotonic() - started)
            raise NewApiTransportError('client_internal') from None
        else:
            result = classify_response(status, response_headers, content, write)
        self._log(method, template, identity, result, self._monotonic() - started)
        return result

    def _log(self, method, template, identity, result, elapsed):
        try:
            _logger.info(llm_redact.redact(
                '[LLM] newapi %s %s identity=%s role=%s status=%s result=%s/%s ms=%d rid=%s' % (
                    method, template, identity, self.role, result.status, result.kind,
                    result.label(), int(elapsed * 1000), result.request_id or '-')))
        except Exception:  # noqa: BLE001 - logging must never break a call
            pass

    # ---- anon --------------------------------------------------------- #
    def status(self, *, timeout=TIMEOUT_CRON_READ, deadline=None):
        return self._request('GET', '/api/status', identity='anon', write=False,
                             timeout=timeout, deadline=deadline)

    # ---- ap（服務帳號；只給 CRON 金鑰） ------------------------------------ #
    def ap_create_user(self, username, password, display_name, *,
                       timeout=TIMEOUT_PROVISION, deadline=None):
        body = {'username': username, 'password': password, 'display_name': display_name,
                'role': 1}
        return self._request('POST', '/api/user/', identity='ap', write=True,
                             timeout=timeout, deadline=deadline, body=body)

    def ap_search_user(self, username, *, timeout=TIMEOUT_PROVISION, deadline=None):
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise NewApiInputError('username') from None
        return self._request('GET', '/api/user/search', identity='ap', write=False,
                             timeout=timeout, deadline=deadline,
                             query={'keyword': username, 'p': 1, 'page_size': 10})

    def ap_get_user(self, user_id, *, timeout=TIMEOUT_PROVISION, deadline=None):
        return self._request('GET', '/api/user/{id}', identity='ap', write=False,
                             timeout=timeout, deadline=deadline, path_params={'id': user_id})

    # ---- user：session 與 PAT 管理（只給 CRON 金鑰） ------------------------ #
    def user_login(self, client_ip, username, password, *, timeout=TIMEOUT_PROVISION,
                   deadline=None):
        return self._request('POST', '/api/user/login', identity='user', write=True,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             body={'username': username, 'password': password})

    def user_list_access_tokens(self, client_ip, jwt, *, timeout=TIMEOUT_PROVISION,
                                deadline=None):
        return self._request('GET', '/api/user/access_tokens', identity='user', write=False,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=jwt)

    def user_verify_generate(self, client_ip, jwt, password, expires_at, *,
                             timeout=TIMEOUT_PROVISION, deadline=None):
        body = {'method': 'password', 'scope': 'access_token.generate',
                'context': {'scopes': list(UP_SCOPES), 'expires_at': expires_at},
                'password': password}
        return self._request('POST', '/api/verify', identity='user', write=True,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=jwt, body=body)

    def user_create_access_token(self, client_ip, jwt, proof, name, expires_at, *,
                                 timeout=TIMEOUT_PROVISION, deadline=None):
        body = {'name': name, 'scopes': list(UP_SCOPES), 'expires_at': expires_at}
        return self._request('POST', '/api/user/access_tokens', identity='user', write=True,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=jwt, proof=proof, body=body)

    def user_delete_session(self, client_ip, jwt, sid, *, timeout=TIMEOUT_PROVISION,
                            deadline=None):
        return self._request('DELETE', '/api/user/sessions/{sid}', identity='user', write=True,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=jwt, path_params={'sid': sid})

    # ---- user：以 UP 呼叫（WEB 金鑰也能用） --------------------------------- #
    def user_self(self, client_ip, up, *, timeout=TIMEOUT_CRON_READ, deadline=None):
        return self._request('GET', '/api/user/self', identity='user', write=False,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=up, web=True)

    def user_create_token(self, client_ip, up, body, *, timeout=TIMEOUT_KEY_CREATE,
                          deadline=None):
        return self._request('POST', '/api/token/', identity='user', write=True,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=up, body=body, web=True)

    def user_list_tokens(self, client_ip, up, page=1, page_size=10, *,
                         timeout=TIMEOUT_KEY_CREATE, deadline=None):
        return self._request('GET', '/api/token/', identity='user', write=False,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=up, query={'p': page, 'page_size': page_size}, web=True)

    def user_get_token(self, client_ip, up, token_id, *, timeout=TIMEOUT_KEY_CREATE,
                       deadline=None):
        return self._request('GET', '/api/token/{id}', identity='user', write=False,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=up, path_params={'id': token_id}, web=True)

    def user_reveal_token(self, client_ip, up, token_id, *, timeout=TIMEOUT_KEY_CREATE,
                          deadline=None):
        # 唯讀（〔PROV〕§6.2）：重送安全，只是再吃 1 格 CT。回應帶明文金鑰——本檔不 log body。
        return self._request('POST', '/api/token/{id}/key', identity='user', write=False,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=up, path_params={'id': token_id}, web=True)

    def user_log_self(self, client_ip, up, query, *, timeout=TIMEOUT_INTERACTIVE_READ,
                      deadline=None):
        return self._request('GET', '/api/log/self', identity='user', write=False,
                             timeout=timeout, deadline=deadline, client_ip=client_ip,
                             bearer=up, query=query, web=True)


# --------------------------------------------------------------------------- #
#  Odoo 端的工廠                                                                #
# --------------------------------------------------------------------------- #
PARAM_PROXY_URL = 'woow_paas_platform.newapi_proxy_url'


def proxy_url_state(raw):
    """``newapi_proxy_url`` 的值 → ``('usable', url)``／``('unconfigured', None)``／``('invalid', None)``。

    寫入時已由 ``ir.config_parameter`` 守衛擋過；這裡是讀取時再驗一次（SQL 直改也擋得住）。
    """
    value = (raw or '').strip()
    if not value or value == PROXY_URL_BLACKHOLE:
        return 'unconfigured', None
    if value in USABLE_PROXY_URLS:
        return 'usable', value
    return 'invalid', None


def signing_key_for_role(role, environ=None):
    """``role`` 的簽章金鑰：WEB 只讀 ``WOOW_NEWAPI_PROXY_AUTH_WEB``、CRON 只讀 ``…_CRON``，不互相退回。

    回 ``(kid, secret_bytes)``；缺或不合 ⇒ :class:`NewApiConfigError`（只有原因碼）。
    """
    if role not in _ROLE_ENV:
        raise NewApiConfigError('role') from None
    environ = os.environ if environ is None else environ
    return parse_signing_key(environ.get(_ROLE_ENV[role]))


def get_newapi_client(env, role, environ=None):
    """回傳設定好的 :class:`NewApiClient`；沒設定或 env 金鑰不合 ⇒ :class:`NewApiConfigError`。

    ``role`` 決定用哪一組簽章金鑰：web 程式路徑一律 ``ROLE_WEB``、cron 一律 ``ROLE_CRON``。
    """
    if role not in _ROLE_ENV:
        raise NewApiConfigError('role') from None
    raw = env['ir.config_parameter'].sudo().get_param(PARAM_PROXY_URL)
    state, url = proxy_url_state(raw)
    if state != 'usable':
        raise NewApiConfigError('proxy_url_%s' % state) from None
    kid, secret = signing_key_for_role(role, environ)
    return NewApiClient(url, kid, secret, role)
