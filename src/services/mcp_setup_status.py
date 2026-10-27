# -*- coding: utf-8 -*-
"""MCP 服務「還沒設定連線」的即時狀態（woow-mcp-server 管理頁 ``GET /healthz`` 的 ``mcp`` 欄位）。

2026-10-27 負責人要求處理：PaaS 新開的 Nextcloud MCP 在管理頁存好 Nextcloud 連線之前，MCP 一律回
HTTP 503（子行程缺 ``NEXTCLOUD_MCP_BASE_URL``／``USERNAME``／``APP_PASSWORD`` 以 exit 2 結束），
平台 ``state`` 卻是 ``running``、/woow 顯示「運作中」，租戶看不出要去哪裡設定（第三輪 staging 實測）。
pod 在、管理頁在、``/healthz`` 也是 200——平台的部署狀態沒有錯，缺的是「MCP 子行程有沒有在提供工具」
這一格，而它只有管理頁自己知道。

woow-mcp-server（mcp-admin-core）從 2026-10-27 那一版起在 ``/healthz`` 多回一個 ``mcp`` 欄位：

* ``running``        —— MCP 子行程在跑、正在提供工具；
* ``not_configured`` —— 還沒設定連線（管理頁 → Connection 還沒存）；
* ``not_running``    —— 子行程沒有在跑（例如憑證錯誤後停止重試），原因在管理頁的 Logs。

舊映像沒有這個欄位（只回 ``{"status":"ok"}``）。本模組把一次 ``GET /healthz`` 變成上面三個值或
``unknown``——**讀不到、逾時、舊映像、形狀不對一律 ``unknown``，從不 raise**：這是服務頁上的附帶
資訊，``unknown`` 的處置是「什麼都不顯示」，不能讓它把服務頁打壞，也不能讓一台健康的服務冒出錯誤。

## 安全邊界（不是 SSRF 入口）

- 只查平台自己算出的服務根位址（``CloudService._admin_page_public_url()``＝``_public_base_url()``＝
  ``https://<subdomain>.<paas_domain>``，subdomain 由平台產生），**永遠不接受呼叫端給的網址**；
  ``healthz_url`` 再擋一次形狀（只收 ``https://<hostname>``，不收 port／帳密／路徑／query）。
- 不跟隨轉址（``allow_redirects=False``：服務開了 Cloudflare Access 時 ``/healthz`` 會被 302 到登入頁，
  那就是 ``unknown``）、不帶任何 cookie／憑證、body 最多讀 ``_BODY_MAX_BYTES``、只解析 ``mcp`` 一個欄位，
  其他欄位一概不讀、不轉給前端。
- 設自己的 User-Agent：Cloudflare 會擋部分預設 UA（Gitea 前面的 error 1010 就是），不能賭租戶網域的設定。

## 為什麼走公開網址、不走叢集內

平台 Odoo 的 egress（staging ``paas-odoo-platform-stg-web-cron-egress``：公網放行、私網段排除；正式站
沒有 egress policy）走得到 Cloudflare；租戶 namespace 的 ingress 則只收 Cloudflare tunnel
（``paas-cloudflare-system``）、同 namespace 與帶 reach 標籤的工作區 namespace（operator baseline ＋
mcp-server chart 的 default-deny），平台 namespace 不在其中，叢集內直連 ``svc-…-mcp-server:8080`` 會被擋。
2026-10-27 從 staging 平台 pod 以同樣的 UA／逾時／不跟隨轉址實測：正式站 MCP 服務的公開 ``/healthz``
0.1–1.0 秒回 200（staging 當時沒有任何 running 的 MCP 服務；只讀、不建服務）。

## 為什麼自己包總截止時間

``requests`` 的 timeout 不涵蓋 DNS 解析（``getaddrinfo`` 沒有 timeout，見 ``dns_probe`` 的說明），而一條
卡住的請求就是一個 Odoo worker 卡住（正式站 prefork ``workers=6``）。所以查詢丟進 module-level
executor，只等 ``_DEADLINE_SECONDS``，逾時回 ``unknown``；卡住的那條不取消（取消不了），留給它自己結束，
pool 大小是它的封頂。fork 安全處理同 ``dns_probe``／``route_probe``。

## 為什麼用系統 DNS（不像 ``route_probe`` 走 DoH）

``route_probe`` 改用 DoH 是因為 SA route **改名**後叢集 resolver 會把新名字的「查無此名」快取住。這裡的
名字是服務開通時就定好的，而平台只在 ``state == 'running'`` 時查：服務要過 #648 的 DNS 閘才會進
running，那一關用的就是同一個 resolver。沒過關、帶著 ``public_url_dns_unverified`` 放行的服務，這裡多半
查不到 ⇒ ``unknown`` ⇒ 不顯示，Public URL 旁邊本來就有 DNS 的軟提示。

## 快取

結果在行程內快取 ``_CACHE_TTL_SECONDS``（含 ``unknown``：讀不到的那台不會在窗口內讓每一次輪詢都付滿
截止時間）。服務頁每 30 秒輪詢一次，多個分頁／成員同時看同一台服務都共用同一份結果。
"""
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from urllib.parse import urlsplit

import requests

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

HEALTHZ_PATH = '/healthz'

#: 一次查詢（排隊＋DNS＋連線＋讀取）最多等這麼久（秒）。
_DEADLINE_SECONDS = 3.0
_CONNECT_TIMEOUT = 3.0
_READ_TIMEOUT = 3.0
#: ``/healthz`` 回的是幾十 bytes 的 JSON；超過就不是我們要的東西（當 ``unknown``），也不整份下載。
_BODY_MAX_BYTES = 4096
_CACHE_TTL_SECONDS = 10.0
_CACHE_MAX = 512
_MAX_WORKERS = 4
_USER_AGENT = 'WoowPaaS-McpSetupStatus/1.0 (+https://aiot.woowtech.io)'
_HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')

_cache = {}   # healthz url -> (monotonic_ts, state)

_EXECUTOR = None
_EXECUTOR_PID = None


def _reset_after_fork():
    """fork 後子行程手上的 executor 是廢的（worker thread 不存在）——丟掉重建，快取也清掉。"""
    global _EXECUTOR, _EXECUTOR_PID
    _EXECUTOR = None
    _EXECUTOR_PID = None
    _cache.clear()


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


def healthz_url(base_url):
    """平台算出的服務根位址 → ``https://<hostname>/healthz``；形狀不對回 ``''``（不查）。

    只收 ``https://<hostname>`` 或結尾一個斜線：不收 port、帳密、路徑、query、fragment，hostname
    必須是一般 DNS 名稱（IP、``localhost``、底線都不收）。正常情況下輸入一定合格（``_public_base_url``
    組出來的就是這個形狀）；這一關是給「哪天有人把別的值傳進來」的。
    """
    if not isinstance(base_url, str) or not base_url:
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
    return 'https://%s%s' % (host, HEALTHZ_PATH)


def parse_healthz(status_code, body):
    """純函式：一次 ``/healthz`` 回應 → 四個狀態之一。不碰網路，測試直接餵。

    只有 HTTP 200、body 是 JSON 物件、``mcp`` 是認得的字串時才回那個值；其餘（舊映像沒有欄位、
    Cloudflare 的錯誤頁、轉址、截斷的 body…）一律 ``unknown``。
    """
    if status_code != 200 or not isinstance(body, (bytes, str)) or len(body) > _BODY_MAX_BYTES:
        return STATE_UNKNOWN
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):   # UnicodeDecodeError 是 ValueError 的子類
        return STATE_UNKNOWN
    if not isinstance(payload, dict):
        return STATE_UNKNOWN
    value = payload.get('mcp')
    if isinstance(value, str) and value in REPORTED_STATES:
        return value
    return STATE_UNKNOWN


def _fetch(url):
    """單一 GET；回 ``(status_code, body_bytes)``，body 最多讀 ``_BODY_MAX_BYTES + 1``。可能 raise。

    唯一的網路 IO 縫：測試 patch 這一支（module attribute），不必碰 ``requests``。
    """
    response = requests.get(
        url,
        headers={'User-Agent': _USER_AGENT, 'Accept': 'application/json'},
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        allow_redirects=False,
        stream=True,
    )
    try:
        body = b''
        for chunk in response.iter_content(chunk_size=1024):
            body += chunk
            if len(body) > _BODY_MAX_BYTES:
                break
    finally:
        response.close()
    return response.status_code, body


def _host_of(url):
    try:
        return urlsplit(url).hostname or '-'
    except ValueError:
        return '-'


def _probe_once(url):
    """在 executor 裡跑：任何失敗都是 ``unknown``（只記例外類型與主機名，不記內容）。"""
    try:
        status_code, body = _fetch(url)
    except Exception as exc:  # noqa: BLE001 —— 附帶資訊，任何失敗都只是「讀不到」
        _logger.info('MCP setup status unavailable for %s (%s)', _host_of(url), type(exc).__name__)
        return STATE_UNKNOWN
    return parse_healthz(status_code, body)


def _store(url, state):
    now = time.monotonic()
    if len(_cache) >= _CACHE_MAX:
        for key in [k for k, (ts, _s) in list(_cache.items()) if now - ts >= _CACHE_TTL_SECONDS]:
            _cache.pop(key, None)
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
    _cache[url] = (now, state)


def probe(base_url):
    """服務根位址 → ``running``／``not_configured``／``not_running``／``unknown``。永不 raise。

    ``base_url`` 必須是平台算出的 ``_admin_page_public_url()``；形狀不對直接 ``unknown``、不發請求。
    同一個網址 ``_CACHE_TTL_SECONDS`` 秒內共用上一次的結果（含 ``unknown``）。
    """
    url = healthz_url(base_url)
    if not url:
        return STATE_UNKNOWN
    cached = _cache.get(url)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    try:
        future = _executor().submit(_probe_once, url)
    except RuntimeError as exc:      # interpreter 收攤中
        _logger.info('MCP setup status could not be scheduled for %s: %s', _host_of(url), exc)
        return STATE_UNKNOWN
    try:
        state = future.result(timeout=_DEADLINE_SECONDS)
    except _FutureTimeout:
        # 還在跑的不取消（requests 取消不了），只是不再等它。
        _logger.info('MCP setup status timed out for %s', _host_of(url))
        state = STATE_UNKNOWN
    _store(url, state)
    return state
