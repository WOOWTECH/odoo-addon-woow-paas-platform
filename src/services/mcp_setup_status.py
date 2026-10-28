# -*- coding: utf-8 -*-
"""MCP 服務「還沒設定連線」的即時狀態（woow-mcp-server 管理頁 ``GET /healthz`` 的 ``mcp``／``backend`` 欄位）。

2026-10-27 負責人要求處理：PaaS 新開的 Nextcloud MCP 在管理頁存好 Nextcloud 連線之前，MCP 一律回
HTTP 503（子行程缺 ``NEXTCLOUD_MCP_BASE_URL``／``USERNAME``／``APP_PASSWORD`` 以 exit 2 結束），
平台 ``state`` 卻是 ``running``、/woow 顯示「運作中」，租戶看不出要去哪裡設定（第三輪 staging 實測）。
pod 在、管理頁在、``/healthz`` 也是 200——平台的部署狀態沒有錯，缺的是「MCP 子行程有沒有在提供工具」
這一格，而它只有管理頁自己知道。

woow-mcp-server（mcp-admin-core）從 2026-10-27 那一版起在 ``/healthz`` 多回一個 ``mcp`` 欄位：

* ``running``        —— MCP 子行程在跑、正在提供工具；
* ``not_configured`` —— 還沒設定連線（管理頁 → Connection 還沒存）；
* ``not_running``    —— 子行程沒有在跑（例如憑證錯誤後停止重試），原因在管理頁的 Logs。

第五輪（負責人 10-27 決定一起做）起，宣告了後端連線欄位的產品（odoo、litellm、hermes、n8n 等，以 woow-mcp-server
``LaunchPolicy.backend_connection`` 為準）另回 ``backend``＝``configured``｜``missing``：
這幾個產品沒有後端連線也照樣起得來、列得出工具（odoo 41、litellm 40、hermes 11、n8n 文件模式 7），
``mcp`` 是 ``running``，但用到後端的工具會失敗。其他產品與舊映像沒有這個欄位。

舊映像兩個欄位都沒有（只回 ``{"status":"ok"}``）。本模組把一次 ``GET /healthz`` 變成
``McpSetupStatus(state, backend)``：``state`` 是上面三個值或 ``unknown``，``backend`` 是
``configured``／``missing`` 或 ``unknown``（沒有欄位、值不認得）。**讀不到、逾時、舊映像、形狀不對一律
``unknown``，從不 raise**：這是服務頁上的附帶資訊，``unknown`` 的處置是「什麼都不顯示」，不能讓它把
服務頁打壞，也不能讓一台健康的服務冒出錯誤。

## 安全邊界（不是 SSRF 入口）

- 只查平台自己算出的服務根位址（``CloudService._admin_page_public_url()``＝``_public_base_url()``＝
  ``https://<subdomain>.<paas_domain>``，subdomain 由平台產生），**永遠不接受呼叫端給的網址**；
  ``healthz_url`` 再擋一次形狀：只收 ``https://<一個 DNS label>.<paas_domain>``（第五輪 D3-B：限定在平台
  的 ``paas_domain`` 底下，``*.svc.cluster.local``、``metadata.google.internal``、別的網域都不查），不收
  port／帳密／路徑／query。
- 不跟隨轉址（``http.client`` 本來就不跟：服務開了 Cloudflare Access 時 ``/healthz`` 會被 302 到登入頁，
  那就是 ``unknown``）、不帶任何 cookie／憑證、body 最多讀 ``_BODY_MAX_BYTES``、只解析 ``mcp`` 與
  ``backend`` 兩個欄位，其他欄位一概不讀、不轉給前端。TLS 照常驗憑證與主機名（certifi，與 requests 相同）。
- 設自己的 User-Agent：Cloudflare 會擋部分預設 UA（Gitea 前面的 error 1010 就是），不能賭租戶網域的設定。

## 為什麼走公開網址、不走叢集內

平台 Odoo 的 egress（staging ``paas-odoo-platform-stg-web-cron-egress``：公網放行、私網段排除；正式站
沒有 egress policy）走得到 Cloudflare；租戶 namespace 的 ingress 則只收 Cloudflare tunnel
（``paas-cloudflare-system``）、同 namespace 與帶 reach 標籤的工作區 namespace（operator baseline ＋
mcp-server chart 的 default-deny），平台 namespace 不在其中，叢集內直連 ``svc-…-mcp-server:8080`` 會被擋。
2026-10-27 從 staging 平台 pod 以同樣的 UA／逾時／不跟隨轉址實測：正式站 MCP 服務的公開 ``/healthz``
0.1–1.0 秒回 200（staging 當時沒有任何 running 的 MCP 服務；只讀、不建服務）。兩個平台 pod 都沒有
``HTTP(S)_PROXY`` 環境變數（第五輪唯讀確認），所以不經 requests 也是同一條路。

## 截止時間（第五輪 D3-B：慢速逐 byte 的回應拖不住探測）

一條卡住的請求就是一個 Odoo worker 卡住（正式站 prefork ``workers=6``），所以有兩層：

1. 呼叫端（``probe``）只等 ``_DEADLINE_SECONDS``（含排隊），逾時回 ``unknown``——第四輪就有。
2. **探測本身也有總時限**（第五輪）。第四輪的 ``requests`` 讀取逾時只管「單次 socket read」：每 1.5 秒滴
   1 byte 的 ``/healthz`` 實測把 executor 執行緒拖住 24 秒，4 條全被佔住時別台健康的服務也只拿到
   ``unknown``。現在 ``_fetch`` 改用 ``http.client``：連上之後由看門狗（``_Watchdog``）在總時限到時
   ``shutdown`` socket，卡在讀 header 或 body 的那一步立刻結束，結果一律 ``unknown``；每一步的 socket
   逾時也不超過剩下的時間。TLS 握手由 CPython 以 socket 逾時當整段握手的截止時間。
   **涵蓋不到的只有 DNS**（``getaddrinfo`` 沒有 timeout，見 ``dns_probe`` 的說明）：DNS 卡住時執行緒照樣
   被佔住，但呼叫端仍只等 ``_DEADLINE_SECONDS``，而且下面的防重複探測讓同一個網址最多只佔一條。
3. **同一網址同時只探測一次**（第五輪）：已經有探測在跑時，後到的請求等同一個 future（各自仍只等
   ``_DEADLINE_SECONDS``），不再送出新的。探測完成時（即使呼叫端已經不等了）結果照樣寫進快取，下一次
   輪詢直接用。

## 為什麼用系統 DNS（不像 ``route_probe`` 走 DoH）

``route_probe`` 改用 DoH 是因為 SA route **改名**後叢集 resolver 會把「查無此名」快取住。這裡的
名字是服務開通時就定好的，而平台只在 ``state == 'running'`` 時查：服務要過 #648 的 DNS 閘才會進
running，那一關用的就是同一個 resolver。沒過關、帶著 ``public_url_dns_unverified`` 放行的服務，這裡多半
查不到 ⇒ ``unknown`` ⇒ 不顯示，Public URL 旁邊本來就有 DNS 的軟提示。

## 快取（第五輪 D3-B：存好連線後切回分頁不再多掛約 30 秒）

結果在行程內快取，**期限依結果而定**（``_ttl_for``）：

* 要租戶動手的結果（``not_configured``、``not_running``、``running``＋``backend=missing``）只留
  ``_ACTIONABLE_CACHE_TTL_SECONDS``（2 秒）：這幾個值會因為租戶在管理頁按下 Save 而改變，第四輪一律 10 秒，
  實測「存好連線、馬上切回分頁」拿到快取裡的舊值，提示多掛 31 秒（要等下一輪 30 秒輪詢）。2 秒仍足以讓
  同時打開的幾個分頁、同一個分頁的 focus＋visibilitychange 共用一次探測。
* 其他（``running``、``unknown``）留 ``_CACHE_TTL_SECONDS``（10 秒）：讀不到的那台不會讓每一次輪詢都付滿
  截止時間；健康的服務也不必每次都出站。

前端另外在分頁切回來（visibilitychange）與視窗重新取得焦點（並排的兩個視窗不會觸發 visibilitychange）
時立刻重問，提示顯示中也改成每 10 秒問一次（剛存檔就切回來、落在子行程重啟空窗時不必等 30 秒），見
``static/src/paas/services/mcp_setup_status.js``。快取每個 worker 行程一份。
"""
import http.client
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from urllib.parse import urlsplit

import certifi

_logger = logging.getLogger(__name__)

STATE_RUNNING = 'running'
STATE_NOT_CONFIGURED = 'not_configured'
STATE_NOT_RUNNING = 'not_running'
STATE_UNKNOWN = 'unknown'

#: ``/healthz`` 的 ``mcp`` 欄位認得的三個值（woow-mcp-server 的契約：字串、封閉集合）。
#: 其餘任何值（缺欄位、``null``、物件、大小寫不同、新增的值）一律 ``unknown``。
REPORTED_STATES = frozenset({STATE_RUNNING, STATE_NOT_CONFIGURED, STATE_NOT_RUNNING})
#: 端點回給前端的四個值（前端 ``utils/mcp_setup_status.js`` 的 ``MCP_SETUP_STATES`` 與此一一對應）。
STATES = REPORTED_STATES | {STATE_UNKNOWN}

BACKEND_CONFIGURED = 'configured'
BACKEND_MISSING = 'missing'
BACKEND_UNKNOWN = 'unknown'
#: ``/healthz`` 的 ``backend`` 欄位認得的兩個值（第五輪：宣告了後端連線欄位的產品才有）。
REPORTED_BACKENDS = frozenset({BACKEND_CONFIGURED, BACKEND_MISSING})
#: 端點回給前端的三個值（前端 ``MCP_BACKEND_STATES`` 與此一一對應）；沒有欄位＝``unknown``＝不提示。
BACKENDS = REPORTED_BACKENDS | {BACKEND_UNKNOWN}

McpSetupStatus = namedtuple('McpSetupStatus', ('state', 'backend'))
UNKNOWN = McpSetupStatus(STATE_UNKNOWN, BACKEND_UNKNOWN)

HEALTHZ_PATH = '/healthz'

#: 一次查詢最多等這麼久（秒）：呼叫端（排隊＋探測）與探測本身（連上之後的看門狗）各算一次。
_DEADLINE_SECONDS = 3.0
_CONNECT_TIMEOUT = 3.0
_READ_TIMEOUT = 3.0
#: ``/healthz`` 回的是幾十 bytes 的 JSON；超過就不是我們要的東西（當 ``unknown``），也不整份下載。
_BODY_MAX_BYTES = 4096
#: 健康（``running``）與讀不到（``unknown``）的結果快取多久（秒）。
_CACHE_TTL_SECONDS = 10.0
#: 要租戶動手的結果快取多久（秒）——它們會因為租戶按下 Save 而改變，見檔頭「快取」。
_ACTIONABLE_CACHE_TTL_SECONDS = 2.0
_CACHE_MAX = 512
_MAX_WORKERS = 4
_USER_AGENT = 'WoowPaaS-McpSetupStatus/1.0 (+https://aiot.woowtech.io)'
_HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
_LABEL_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')

#: 快取計時用的時鐘（測試 patch 這一支就能控制「過了多久」，不影響截止時間與看門狗）。
_now = time.monotonic

_lock = threading.Lock()
_cache = {}      # healthz url -> (expires_at, McpSetupStatus)
_inflight = {}   # healthz url -> Future（同一網址同時只探測一次）

_EXECUTOR = None
_EXECUTOR_PID = None
_TLS_CONTEXT = None


class _DeadlineExceeded(Exception):
    """探測的總時限到了（看門狗切斷連線，或連上時已經沒有剩下的時間）。"""


def _reset_after_fork():
    """fork 後子行程手上的 executor 是廢的（worker thread 不存在）——丟掉重建；快取、進行中的探測
    與鎖也重建（fork 當下若有別的執行緒拿著鎖，子行程裡那把鎖永遠解不開）。"""
    global _EXECUTOR, _EXECUTOR_PID, _lock, _TLS_CONTEXT
    _EXECUTOR = None
    _EXECUTOR_PID = None
    _TLS_CONTEXT = None
    _lock = threading.Lock()
    _cache.clear()
    _inflight.clear()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _executor():
    """本行程自己的 executor；fork 後重建。理由見 ``dns_probe._executor``。"""
    global _EXECUTOR, _EXECUTOR_PID
    pid = os.getpid()
    if _EXECUTOR is None or _EXECUTOR_PID != pid:
        _EXECUTOR = ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix='woow-mcpstatus')
        _EXECUTOR_PID = pid
    return _EXECUTOR


def healthz_url(base_url, paas_domain):
    """平台算出的服務根位址 → ``https://<hostname>/healthz``；形狀不對回 ``''``（不查）。

    只收 ``https://<一個 DNS label>.<paas_domain>`` 或結尾一個斜線（＝``_public_base_url`` 組出來的
    形狀：subdomain 是 ``make_service_subdomain`` 產生的單一 label）：不收 port、帳密、路徑、query、
    fragment；hostname 必須是一般 DNS 名稱（IP、``localhost``、底線都不收），而且**必須在平台的
    ``paas_domain`` 底下**（第五輪，縱深防禦：``*.svc.cluster.local``、``metadata.google.internal``、
    別的網域、``paas_domain`` 本身都不查）。``paas_domain`` 自己不是合法 DNS 名稱時一律不查。
    正常情況下輸入一定合格；這一關是給「哪天有人把別的值傳進來」的。
    """
    if not isinstance(base_url, str) or not base_url:
        return ''
    if not isinstance(paas_domain, str):
        return ''
    domain = paas_domain.strip().lower().rstrip('.')
    if not domain or not _HOSTNAME_RE.match(domain):
        return ''
    try:
        parts = urlsplit(base_url.strip())
        port = parts.port
    except ValueError:
        return ''
    host = parts.hostname or ''
    if (parts.scheme != 'https' or port is not None or parts.username or parts.password
            or parts.path not in ('', '/') or parts.query or parts.fragment
            or not _HOSTNAME_RE.match(host)):
        return ''
    suffix = '.' + domain
    if not host.endswith(suffix) or not _LABEL_RE.match(host[:-len(suffix)]):
        return ''
    return 'https://%s%s' % (host, HEALTHZ_PATH)


def parse_healthz(status_code, body):
    """純函式：一次 ``/healthz`` 回應 → ``McpSetupStatus``。不碰網路，測試直接餵。

    只有 HTTP 200、body 是 JSON 物件、``mcp`` 是認得的字串時才回那個值；其餘（舊映像沒有欄位、
    Cloudflare 的錯誤頁、轉址、截斷的 body…）一律 ``UNKNOWN``。``backend`` 只在 ``mcp`` 認得時才讀，
    值不認得（或沒有這個欄位）是 ``unknown``。
    """
    if status_code != 200 or not isinstance(body, (bytes, str)) or len(body) > _BODY_MAX_BYTES:
        return UNKNOWN
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):   # UnicodeDecodeError 是 ValueError 的子類
        return UNKNOWN
    if not isinstance(payload, dict):
        return UNKNOWN
    state = payload.get('mcp')
    if not (isinstance(state, str) and state in REPORTED_STATES):
        return UNKNOWN
    backend = payload.get('backend')
    if not (isinstance(backend, str) and backend in REPORTED_BACKENDS):
        backend = BACKEND_UNKNOWN
    return McpSetupStatus(state, backend)


def is_actionable(status):
    """這個結果是不是「要租戶動手」的（服務頁會畫提示）：not_configured、not_running、
    running＋backend=missing。前端 ``mcpSetupCondition`` 是同一個判斷（node 測試對照）。"""
    return (status.state in (STATE_NOT_CONFIGURED, STATE_NOT_RUNNING)
            or (status.state == STATE_RUNNING and status.backend == BACKEND_MISSING))


def _ttl_for(status):
    return _ACTIONABLE_CACHE_TTL_SECONDS if is_actionable(status) else _CACHE_TTL_SECONDS


def _tls_context():
    """本行程共用的 TLS 設定：驗憑證與主機名（certifi，與 requests／``route_probe`` 同一份信任根）。"""
    global _TLS_CONTEXT
    if _TLS_CONTEXT is None:
        context = ssl.create_default_context(cafile=certifi.where())
        context.set_alpn_protocols(['http/1.1'])
        _TLS_CONTEXT = context
    return _TLS_CONTEXT


def _connection(host):
    """對 ``host:443`` 的 HTTPS 連線物件（還沒連）。測試換成連本機的 ``HTTPConnection``。"""
    return http.client.HTTPSConnection(
        host, 443, timeout=_CONNECT_TIMEOUT, context=_tls_context())


class _Watchdog:
    """總時限到時 ``shutdown`` 連線的 socket，讓卡在讀 header／body 的那一步立刻結束。

    ``shutdown`` 叫的是 ``socket.socket`` 本身的方法（不經 ``SSLSocket.shutdown``，不動 SSL 物件），
    只讓阻塞中的 recv 拿到 EOF。與 ``disarm`` 共用一把鎖：解除之後絕不再碰那個 socket（關掉後同一個
    fd 號碼可能已經分給別的連線）。
    """

    def __init__(self, conn, seconds):
        self._conn = conn
        self._guard = threading.Lock()
        self._done = False
        self.fired = False
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self):
        with self._guard:
            if self._done:
                return
            self.fired = True
            sock = getattr(self._conn, 'sock', None)
            if sock is not None:
                try:
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)
                except OSError:
                    pass

    def disarm(self):
        with self._guard:
            self._done = True
        self._timer.cancel()


def _fetch(url):
    """單一 GET；回 ``(status_code, body_bytes)``，body 最多讀 ``_BODY_MAX_BYTES + 1``。可能 raise。

    唯一的網路 IO 縫：測試 patch 這一支（module attribute）就不必碰網路；``_connection`` 是更下一層的
    縫（測試換成本機的 HTTP 伺服器，驗真的 socket 行為：慢速逐 byte、送出的 header）。

    總時限 ``_DEADLINE_SECONDS`` 從進到這裡開始算：連線（DNS＋TCP＋TLS）之後若已經沒有時間就不送請求；
    送請求、讀 header、讀 body 由 ``_Watchdog`` 在時限到時切斷，切斷過的結果一律當失敗（不解析半截內容）。
    """
    deadline = time.monotonic() + _DEADLINE_SECONDS
    host = urlsplit(url).hostname
    conn = _connection(host)
    watchdog = None
    try:
        conn.timeout = min(_CONNECT_TIMEOUT, _DEADLINE_SECONDS)
        conn.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExceeded('connected after the deadline')
        conn.sock.settimeout(min(_READ_TIMEOUT, remaining))
        watchdog = _Watchdog(conn, remaining)
        conn.putrequest('GET', HEALTHZ_PATH, skip_host=True, skip_accept_encoding=True)
        conn.putheader('Host', host)
        conn.putheader('User-Agent', _USER_AGENT)
        conn.putheader('Accept', 'application/json')
        conn.putheader('Connection', 'close')
        conn.endheaders()
        response = conn.getresponse()
        body = response.read(_BODY_MAX_BYTES + 1)
        status_code = response.status
    except Exception:
        # 看門狗切斷後，http.client 報的是 RemoteDisconnected／IncompleteRead／SSL EOF 之類——
        # 對外一律說成「超過總時限」，log 才看得出是哪一種失敗。
        if watchdog is not None and watchdog.fired:
            raise _DeadlineExceeded('the response took longer than the deadline') from None
        raise
    finally:
        if watchdog is not None:
            watchdog.disarm()
        conn.close()
    if watchdog.fired:
        # 讀完了，但看門狗在讀完之前已經切過（半截內容不解析）。
        raise _DeadlineExceeded('the response took longer than the deadline')
    return status_code, body


def _host_of(url):
    try:
        return urlsplit(url).hostname or '-'
    except ValueError:
        return '-'


def _probe_once(url):
    """在 executor 裡跑：任何失敗都是 ``UNKNOWN``（只記例外類型與主機名，不記內容）。"""
    try:
        status_code, body = _fetch(url)
    except Exception as exc:  # noqa: BLE001 —— 附帶資訊，任何失敗都只是「讀不到」
        _logger.info('MCP setup status unavailable for %s (%s)', _host_of(url), type(exc).__name__)
        return UNKNOWN
    return parse_healthz(status_code, body)


def _store(url, status):
    """寫快取（呼叫端已拿著 ``_lock``）。期限依結果而定，見 ``_ttl_for``。"""
    now = _now()
    if len(_cache) >= _CACHE_MAX:
        for key in [k for k, (expires, _s) in list(_cache.items()) if expires <= now]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
    _cache[url] = (now + _ttl_for(status), status)


def _finish(url, future):
    """探測完成（executor 執行緒裡跑）：寫快取、清掉進行中的紀錄。呼叫端已經不等了也照寫。"""
    try:
        status = future.result()
    except BaseException:  # noqa: BLE001 —— _probe_once 不會 raise；萬一被取消也只是「讀不到」
        status = UNKNOWN
    with _lock:
        _store(url, status)
        if _inflight.get(url) is future:
            del _inflight[url]


def probe(base_url, paas_domain):
    """服務根位址 → ``McpSetupStatus``（state＝running／not_configured／not_running／unknown）。永不 raise。

    ``base_url`` 必須是平台算出的 ``_admin_page_public_url()``、``paas_domain`` 是平台的
    ``woow_paas_platform.paas_domain``；形狀不對直接 ``UNKNOWN``、不發請求。快取期限見 ``_ttl_for``；
    同一網址已經有探測在跑時等同一個（不另外送）。
    """
    url = healthz_url(base_url, paas_domain)
    if not url:
        return UNKNOWN
    submitted = False
    with _lock:
        cached = _cache.get(url)
        if cached and _now() < cached[0]:
            return cached[1]
        future = _inflight.get(url)
        if future is None:
            try:
                future = _executor().submit(_probe_once, url)
            except RuntimeError as exc:      # interpreter 收攤中
                _logger.info('MCP setup status could not be scheduled for %s: %s', _host_of(url), exc)
                return UNKNOWN
            _inflight[url] = future
            submitted = True
    if submitted:
        # 完成時（不論呼叫端還等不等）寫快取、清掉進行中的紀錄。已經完成的 future 會在這裡同步
        # 呼叫 _finish，而 _finish 要拿 _lock——所以註冊放在鎖外面。
        future.add_done_callback(lambda done, url=url: _finish(url, done))
    try:
        return future.result(timeout=_DEADLINE_SECONDS)
    except _FutureTimeout:
        # 還在跑的不取消（DNS 取消不了），只是不再等它；它完成時 _finish 會寫進真正的結果。
        _logger.info('MCP setup status timed out for %s', _host_of(url))
        with _lock:
            if not future.done():
                _store(url, UNKNOWN)
        return UNKNOWN
    except Exception as exc:  # noqa: BLE001 —— 被取消（executor 收攤）之類：只是「讀不到」
        _logger.info('MCP setup status failed for %s (%s)', _host_of(url), type(exc).__name__)
        return UNKNOWN
