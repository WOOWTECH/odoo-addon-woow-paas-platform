"""MCP URL 不能是 new-api／newapi-proxy 的跳板（DESIGN §5.2 T3，〔REV〕13；票 LLM-02）。

租戶可以自己填 MCP server 的 URL 與 ``headers_json``，而 Odoo 自己（同步工具、測試連線、AI 助理）會
去連它。Odoo pod 是代理 NetworkPolicy 放行的那一批：URL 指到代理（或 new-api）＝讓 PaaS 替租戶打代理。
代理要 HMAC 簽章、靜態標頭重放不了（N23），這裡是第二道：

* **名字**：主機名的某一段是 new-api 的 namespace（``new-api``、``new-api-stg``），後面接 ``svc``／
  ``pod`` 或就是最後一段（``newapi-proxy.new-api-stg``、``….svc``、``….svc.cluster.local``、尾端加點）⇒ 拒絕。
* **位址**：URL 的主機（字面 IP、或 DNS 解析的**所有**結果；IPv4-mapped IPv6 先還原）落在代理或 new-api 的
  Service 位址（解析 :data:`PROTECTED_HOSTS`）⇒ 拒絕；不是叢集內名稱（``*.svc.cluster.local``）卻落在叢集的
  pod／service 網段（k3s 預設 :data:`CLUSTER_NETWORKS`，含 pod IP 與「解析到 10.43.x.x 的外部網域」）⇒ 拒絕。
  sidecar 的 MCP（``<release>-mcp.<租戶 ns>.svc.cluster.local``）照常可以用。
* **什麼時候查**：存檔時（model constraint 與 ``controllers/paas.py:_validate_mcp_url`` 共用
  :func:`jump_reason`——two-layer lockstep），**連線時再查一次**（每一個 HTTP 請求送出前，
  :func:`guarded_http_client_factory` 的 request hook；防 DNS rebinding），而且不跟 redirect
  （跨主機的 3xx 直接當成錯誤）。

解析不到（NXDOMAIN、逾時 :data:`RESOLVE_TIMEOUT_SECONDS`）在存檔時放行（連不到的 URL 本來就沒用），連線時
同一個檢查會再跑一次。殘留風險：連線時的檢查與 httpcore 自己的解析之間仍有極短的空窗（同一個名字兩次解析
得到不同答案）；這時代理的 HMAC 是最後一道。

本檔不 import odoo（``tests/test_llm_services.py`` 直接載入）。
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

#: new-api 與代理所在的 namespace（prod、stg）。
PROTECTED_NAMESPACES = frozenset({'new-api', 'new-api-stg'})
#: 這些名字解析出來的位址（Service ClusterIP）一律不准當 MCP 的目標。
PROTECTED_HOSTS = tuple(
    '%s.%s.svc.cluster.local' % (service, namespace)
    for namespace in sorted(PROTECTED_NAMESPACES)
    for service in ('newapi-proxy', 'newapi'))
#: k3s 預設的 pod 與 service 網段：不是叢集內名稱卻解析到這裡 ⇒ 拒絕。
CLUSTER_NETWORKS = (ipaddress.ip_network('10.42.0.0/16'), ipaddress.ip_network('10.43.0.0/16'))
CLUSTER_SUFFIX = '.svc.cluster.local'
#: 單次 DNS 解析最多等這麼久（存檔是在 HTTP 請求裡，不能卡住 worker）。
RESOLVE_TIMEOUT_SECONDS = 3
#: 受保護位址的快取（每個行程；Service ClusterIP 換了最多晚這麼久生效）。
PROTECTED_CACHE_SECONDS = 60

REASON_NAMESPACE = 'newapi_namespace'
REASON_ADDRESS = 'newapi_address'
REASON_CLUSTER = 'cluster_address'
REASON_REDIRECT = 'redirect'

_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='mcp-dns')
_cache_lock = threading.Lock()
_protected_cache = {'until': 0.0, 'ips': frozenset()}


class McpUrlRejected(Exception):
    """連線時的檢查不過（``reason`` 是 :data:`REASON_*` 之一；訊息不帶 URL）。"""

    def __init__(self, reason):
        super().__init__('mcp url rejected: %s' % reason)
        self.reason = reason


def _getaddrinfo(host):
    return {info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}


def resolve(host):
    """``host`` 的所有位址（字串）；解析不到或逾時 ⇒ 空集合。"""
    future = _EXECUTOR.submit(_getaddrinfo, host)
    try:
        return future.result(timeout=RESOLVE_TIMEOUT_SECONDS)
    except (concurrent.futures.TimeoutError, OSError, UnicodeError, ValueError):
        return set()


def _ip(value):
    try:
        address = ipaddress.ip_address(value.split('%', 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def normalize_host(url):
    """URL → 小寫、去掉尾端點的主機名（``None``＝解析不了）。"""
    try:
        host = urlparse(url or '').hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.strip().lower().rstrip('.') or None


def protected_ips(resolver=None):
    """代理與 new-api 的 Service 位址（快取 :data:`PROTECTED_CACHE_SECONDS` 秒；測試傳 ``resolver``）。"""
    if resolver is not None:
        return frozenset(ip for host in PROTECTED_HOSTS for ip in map(_ip, resolver(host)) if ip)
    now = time.monotonic()
    with _cache_lock:
        if now < _protected_cache['until']:
            return _protected_cache['ips']
    ips = frozenset(ip for host in PROTECTED_HOSTS for ip in map(_ip, resolve(host)) if ip)
    with _cache_lock:
        _protected_cache.update(until=now + PROTECTED_CACHE_SECONDS, ips=ips)
    return ips


def names_protected_namespace(host):
    labels = host.split('.')
    for index, label in enumerate(labels):
        if label not in PROTECTED_NAMESPACES:
            continue
        following = labels[index + 1] if index + 1 < len(labels) else None
        if following in (None, 'svc', 'pod'):
            return True
    return False


def jump_reason(url, resolver=None):
    """``url`` 是不是代理／new-api 的跳板 ⇒ :data:`REASON_*`；不是 ⇒ ``None``。

    ``resolver``：``host → {位址字串}``（測試用；預設 :func:`resolve`）。
    """
    host = normalize_host(url)
    if host is None:
        return None
    if names_protected_namespace(host):
        return REASON_NAMESPACE
    literal = _ip(host)
    if literal is not None:
        addresses = {literal}
    else:
        addresses = {ip for ip in map(_ip, (resolver or resolve)(host)) if ip}
    if not addresses:
        return None
    if addresses & protected_ips(resolver):
        return REASON_ADDRESS
    if not host.endswith(CLUSTER_SUFFIX) and any(
            address.version == 4 and any(address in net for net in CLUSTER_NETWORKS)
            for address in addresses):
        return REASON_CLUSTER
    return None


def guarded_http_client_factory(base_factory=None, resolver=None):
    """給 ``langchain_mcp_adapters`` 的 ``httpx_client_factory``：每個請求送出前再查一次、不跟跨主機 redirect。

    ``base_factory``：mcp 自己的 ``create_mcp_http_client``（沒給就從 mcp 拿；版本不同時它回的可能是
    ``httpx`` 或 ``httpx2`` 的 client——兩者的 ``event_hooks``／``follow_redirects`` 介面相同）。
    """
    def factory(headers=None, timeout=None, auth=None):
        create = base_factory
        if create is None:
            from mcp.shared._httpx_utils import create_mcp_http_client as create
        client = create(headers=headers, timeout=timeout, auth=auth)
        client.follow_redirects = False

        async def check_request(request):
            reason = jump_reason(str(request.url), resolver)
            if reason:
                raise McpUrlRejected(reason)

        async def check_response(response):
            if 300 <= response.status_code < 400:
                location = response.headers.get('location') or ''
                target = response.request.url.join(location) if location else None
                if target is None or normalize_host(str(target)) != normalize_host(
                        str(response.request.url)):
                    raise McpUrlRejected(REASON_REDIRECT)

        hooks = dict(client.event_hooks)
        hooks['request'] = list(hooks.get('request', [])) + [check_request]
        hooks['response'] = list(hooks.get('response', [])) + [check_response]
        client.event_hooks = hooks
        return client
    return factory
