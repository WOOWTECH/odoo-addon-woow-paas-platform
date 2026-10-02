# -*- coding: utf-8 -*-
"""Security Access route 的對外健康探測（#1234 第一步）。

回答使用者最常問的那一句「我設好了，為什麼打不開？」。SA 詳情頁原本只給一個
tunnel 層級的「Connector 已連線」，六條 route 長得一模一樣；而「打不開」有好幾種
原因、處理方式完全不同（見 #1234 的表）。這裡從**公開網路**對每條 route 的
hostname 發一個 GET，把回應分類成使用者能據以行動的狀態。

## 安全邊界

- 只探 ``route.hostname``——由平台以 ``{subdomain_prefix}.{paas_domain}`` 組出
  （prefix 過 ``_subdomain_prefix_error`` 格式閘），**永遠不接受呼叫端給的網址**
  ⇒ 不是 SSRF 入口。本模組自己再擋一次（``_is_probeable``）。
- 不跟隨轉址（``allow_redirects=False``）、不帶任何 cookie／憑證、body 最多讀
  ``_BODY_PEEK_BYTES`` 只為了找 Cloudflare 的錯誤碼。

## 為什麼自己包 timeout 與 executor

與 ``dns_probe`` 同一個理由：一條請求卡住就是一個 worker 卡住（prod 是 prefork
``workers=6``，#1159 Q12 複核）。這裡把每個 hostname 丟進 module-level executor，
整批共用一個總截止時間；逾時的那幾條回 ``timeout``，不拖住整頁。executor 的
fork 安全處理照抄 ``dns_probe._executor``（那邊的 docstring 有實測紀錄）。

## 為什麼不走叢集 DNS（2026-09-29 正式站實測後改）

改名或新增 route 之後，叢集內的 resolver 會把「查無此名」快取住——Cloudflare
zone 的 SOA 否定快取最長 30 分鐘。實測：改名後外部已經 200，本探針卻一直回
``dns_missing``。所以先用 Cloudflare 的 DNS-over-HTTPS 問（這些 hostname 都在
Cloudflare 託管的 zone，答案即時），拿到 edge IP 後**直接連那個 IP、以 SNI 帶
hostname**（憑證照常驗 hostname）。DoH 不可用時才退回系統 DNS。

## 反向代理的預設頁

route 常指到使用者自己的反向代理（Nginx Proxy Manager 等）。代理沒有這個
hostname 的站台時會回自己的預設頁、HTTP 200——整條路都通，但使用者的服務並沒有
被打到。實測改名後正是這樣（NPM 回「Default Site」）。所以 2xx 也讀 body 的
``<title>``，認得的預設頁歸為 ``proxy_no_site``。

## 快取

結果在行程內快取 ``_CACHE_TTL_SECONDS``。詳情頁開著、使用者連按「重新檢查」、
或多人同時看同一個 SA，都不會把同一個 hostname 打爆；``force=True`` 可略過。
"""
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait as _wait
from datetime import datetime, timezone
from urllib.parse import urlparse

import certifi
import requests
import urllib3

_logger = logging.getLogger(__name__)

#: 單一請求的讀取逾時（秒）。連線逾時另計在 ``_CONNECT_TIMEOUT``。
_READ_TIMEOUT = 5.0
_CONNECT_TIMEOUT = 3.0
#: 整批探測的總截止時間（秒）：executor 排隊＋請求本身。超過的回 ``timeout``。
_BATCH_DEADLINE = 8.0
#: 只讀 body 的前幾 KB：Cloudflare 的錯誤碼（``error code: 1033`` 或 HTML 頁裡
#: 的 ``errorCode: 1033``）都在前段。不整頁下載，避免探到大檔案。
_BODY_PEEK_BYTES = 16 * 1024
_CACHE_TTL_SECONDS = 30.0
_MAX_WORKERS = 6
_USER_AGENT = 'WoowPaaS-RouteProbe/1.0 (+https://aiot.woowtech.io)'

#: 狀態 → 是否算「健康」。前端依此上色；語意與使用者該做什麼見 ``STATUS_HINTS``。
STATUSES = (
    'ok',                 # 請求穿過 Cloudflare 與 tunnel、origin 有回應（< 500）
    'access_login',       # 有保護：Cloudflare Access 登入頁（正常）
    'access_missing',     # route 設為保護，但實際沒有登入頁擋在前面
    'access_unexpected',  # route 設為公開，Cloudflare 上卻有 Access 擋著（#1239）
    'tunnel_down',        # Cloudflare 1033：tunnel 沒有任何 connector 連著
    'not_routed',         # 名字還沒指到這條 tunnel（DNS／ingress 尚未生效）
    'dns_missing',        # 公開 DNS 查不到這個名字
    'origin_error',       # 請求有到使用者機器，但反向代理或後端回 5xx
    'proxy_no_site',      # 請求有到使用者機器，但反向代理回的是自己的預設頁
    'timeout',            # 在截止時間內沒回應
    'unknown',            # 其他無法歸類的失敗
)
HEALTHY = frozenset({'ok', 'access_login'})

# Cloudflare 錯誤碼 → 狀態。1033＝Argo Tunnel error（沒有 connector）；
# 1016＝Origin DNS error、1001／1014＝DNS resolution／CNAME cross-user，都是
# 「這個名字沒指到一條活的 tunnel」。
_CF_CODE_STATUS = {
    1033: 'tunnel_down',
    1016: 'not_routed',
    1001: 'not_routed',
    1014: 'not_routed',
}
_CF_CODE_RE = re.compile(
    r'(?:error\s*code[:\s]*|errorCode:\s*)(\d{3,4})', re.IGNORECASE)
_TITLE_RE = re.compile(r'<title>\s*([^<]{0,200}?)\s*</title>', re.IGNORECASE)
#: 反向代理「沒有這個站台」時的預設頁標題（小寫比對）。只收**確定是預設頁**的
#: 字串——NPM 自己的管理介面標題是「Nginx Proxy Manager」，不能被誤判。
_PROXY_DEFAULT_TITLES = (
    'default site',                     # Nginx Proxy Manager 2.x 預設站台
    'welcome to nginx!',                # nginx 套件預設頁
    "congratulations! you've successfully started the nginx proxy manager.",
)
_DOH_URL = 'https://cloudflare-dns.com/dns-query'
_DOH_TIMEOUT = 3.0
_HOSTNAME_RE = re.compile(r'^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')

_cache = {}   # hostname -> (monotonic_ts, is_protected, result)

_EXECUTOR = None
_EXECUTOR_PID = None


def _reset_after_fork():
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
            max_workers=_MAX_WORKERS, thread_name_prefix='woow-routeprobe')
        _EXECUTOR_PID = pid
    return _EXECUTOR


def _is_probeable(hostname):
    return bool(hostname) and bool(_HOSTNAME_RE.match(hostname))


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _result(status, http_code=None, cf_code=None):
    return {
        'status': status,
        'healthy': status in HEALTHY,
        'http_code': http_code,
        'cf_error_code': cf_code,
        'checked_at': _now_iso(),
    }


def _cf_error_code(body_head):
    m = _CF_CODE_RE.search(body_head or '')
    return int(m.group(1)) if m else None


def classify(status_code, headers, body_head, is_protected):
    """純函式：一次 HTTP 回應 → ``_result``。不碰網路，測試直接餵。"""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    location = headers.get('location', '')
    from_cloudflare = headers.get('server', '').lower() == 'cloudflare'

    if 300 <= status_code < 400 and location:
        host = (urlparse(location).hostname or '').lower()
        if host.endswith('.cloudflareaccess.com'):
            return _result('access_login' if is_protected else 'access_unexpected',
                           status_code)

    cf_code = _cf_error_code(body_head) if (from_cloudflare and status_code >= 400) else None
    if cf_code in _CF_CODE_STATUS:
        return _result(_CF_CODE_STATUS[cf_code], status_code, cf_code)
    if status_code == 530:
        # 530 是 Cloudflare 專用的「origin 端 1xxx 錯誤」；沒抓到碼時最常見的是 tunnel。
        return _result('tunnel_down', status_code, cf_code)
    if status_code >= 500:
        return _result('origin_error', status_code, cf_code)
    if is_protected:
        # 設了保護卻直接拿到 origin 的回應 ⇒ 前面沒有登入頁。
        return _result('access_missing', status_code)
    if 200 <= status_code < 300:
        m = _TITLE_RE.search(body_head or '')
        if m and m.group(1).strip().lower() in _PROXY_DEFAULT_TITLES:
            return _result('proxy_no_site', status_code)
    return _result('ok', status_code)


class _DnsMissing(Exception):
    """權威 DNS 說這個名字沒有 A 記錄。"""


def _doh_resolve(hostname):
    """Cloudflare DoH 查 A 記錄 → edge IP；NXDOMAIN／無 A → raise ``_DnsMissing``；
    DoH 本身不可用 → ``None``（呼叫端退回系統 DNS）。"""
    try:
        data = requests.get(
            _DOH_URL, params={'name': hostname, 'type': 'A'},
            headers={'Accept': 'application/dns-json'}, timeout=_DOH_TIMEOUT,
        ).json()
    except Exception as exc:             # noqa: BLE001 —— 退回系統 DNS
        _logger.info("route probe DoH unavailable for %s: %s", hostname, exc)
        return None
    ips = [a.get('data') for a in data.get('Answer') or [] if a.get('type') == 1]
    if ips:
        return ips[0]
    if data.get('Status') in (0, 3):     # NOERROR 無 A／NXDOMAIN：權威地說「沒有」
        raise _DnsMissing(hostname)
    return None


def _headers():
    return {'User-Agent': _USER_AGENT, 'Accept': 'text/html,*/*'}


def _fetch_via_ip(hostname, ip):
    """連 edge IP、SNI 與 Host 帶 hostname；憑證照常驗 hostname。"""
    pool = urllib3.HTTPSConnectionPool(
        ip, 443, server_hostname=hostname, assert_hostname=hostname,
        cert_reqs='CERT_REQUIRED', ca_certs=certifi.where(), retries=False,
        timeout=urllib3.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT))
    try:
        resp = pool.request('GET', '/', headers=dict(_headers(), Host=hostname),
                            redirect=False, preload_content=False)
        try:
            chunk = resp.read(_BODY_PEEK_BYTES)
        finally:
            resp.release_conn()
        return resp.status, dict(resp.headers), chunk.decode('utf-8', 'replace')
    finally:
        pool.close()


def _fetch(hostname):
    """單一 hostname 的 GET；回 ``(status_code, headers, body_head)`` 或 raise。"""
    ip = _doh_resolve(hostname)
    if ip:
        return _fetch_via_ip(hostname, ip)
    resp = requests.get(
        'https://%s/' % hostname,
        timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        allow_redirects=False,
        stream=True,
        headers=_headers(),
    )
    try:
        chunk = next(resp.iter_content(_BODY_PEEK_BYTES), b'')
    finally:
        resp.close()
    return resp.status_code, dict(resp.headers), chunk.decode('utf-8', 'replace')


def _probe_one(hostname, is_protected):
    try:
        status_code, headers, body_head = _fetch(hostname)
    except _DnsMissing:
        return _result('dns_missing')
    except urllib3.exceptions.NewConnectionError:
        # 連 edge IP 被拒／無路由。urllib3（1.26 與 2.x）把它定義成
        # ``ConnectTimeoutError`` 的子類，不先攔就會被下一行吃成 timeout
        # （前端顯示「服務沒有及時回應」）。與 requests 路徑對齊：非 DNS 的
        # 連線失敗一律 unknown。
        return _result('unknown')
    except (requests.exceptions.Timeout, urllib3.exceptions.TimeoutError):
        return _result('timeout')
    except requests.exceptions.ConnectionError as exc:
        text = str(exc)
        if 'Name or service not known' in text or 'NameResolutionError' in text \
                or 'nodename nor servname' in text or 'getaddrinfo failed' in text:
            return _result('dns_missing')
        return _result('unknown')
    except Exception as exc:             # noqa: BLE001 —— 探針不得炸掉呼叫端
        _logger.warning("route probe for %s raised %s: %s",
                        hostname, type(exc).__name__, exc)
        return _result('unknown')
    return classify(status_code, headers, body_head, is_protected)


def probe_routes(targets, force=False):
    """``targets``：``[(hostname, is_protected), ...]`` → ``{hostname: result}``。

    永不 raise。無法探測的 hostname（格式不符）直接回 ``unknown``。
    """
    results, pending = {}, {}
    now = time.monotonic()
    for hostname, is_protected in targets:
        if not _is_probeable(hostname):
            results[hostname] = _result('unknown')
            continue
        cached = _cache.get(hostname)
        if (not force and cached and now - cached[0] < _CACHE_TTL_SECONDS
                and cached[1] == bool(is_protected)):
            results[hostname] = cached[2]
            continue
        try:
            pending[_executor().submit(_probe_one, hostname, bool(is_protected))] = (
                hostname, bool(is_protected))
        except RuntimeError as exc:      # interpreter 收攤中
            _logger.warning("route probe could not be scheduled for %s: %s", hostname, exc)
            results[hostname] = _result('unknown')

    if pending:
        done, not_done = _wait(pending, timeout=_BATCH_DEADLINE)
        for future in done:
            hostname, is_protected = pending[future]
            result = future.result()
            results[hostname] = result
            _cache[hostname] = (time.monotonic(), is_protected, result)
        for future in not_done:
            # 還在跑的不取消（requests 取消不了），只是不再等它；也不寫快取。
            results[pending[future][0]] = _result('timeout')
    return results
