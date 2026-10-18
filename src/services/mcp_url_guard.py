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
  :func:`jump_reason`——two-layer lockstep），**連線時再查一次**（每一條 TCP 連線建立前，
  :func:`guarded_http_client_factory` 的 network backend；檢查過的數字 IP 直接用來連線），而且不跟 redirect
  （所有 3xx 直接當成錯誤）。

解析不到（NXDOMAIN、逾時或解析資源已滿）在存檔時放行，連線時拒絕。DNS 執行中與排隊的工作合計有上限，
逾時不釋出仍在執行／排隊的額度。HTTP origin 保留原始主機名，TLS SNI、憑證主機名驗證與 Host 都不改寫。

本檔不 import odoo（``tests/test_llm_services.py`` 直接載入）。
"""
from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
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
REASON_DNS = 'dns_unavailable'

# libc DNS cannot be interrupted. Bound running + pending jobs even after callers time out.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='mcp-dns')
_RESOLVE_SLOTS = threading.BoundedSemaphore(8)
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
    slots = _RESOLVE_SLOTS
    if not slots.acquire(blocking=False):
        return set()
    try:
        future = _EXECUTOR.submit(_getaddrinfo, host)
    except RuntimeError:
        slots.release()
        return set()
    # Do not cancel queued futures: cancelling releases their callback but leaves the
    # work item in ThreadPoolExecutor's unbounded queue until a worker can drain it.
    future.add_done_callback(lambda done: slots.release())
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
    return _address_reason(host, addresses, resolver)


def _address_reason(host, addresses, resolver):
    if addresses & protected_ips(resolver):
        return REASON_ADDRESS
    if not host.endswith(CLUSTER_SUFFIX) and any(
            address.version == 4 and any(address in net for net in CLUSTER_NETWORKS)
            for address in addresses):
        return REASON_CLUSTER
    return None


def _connection_addresses(host, resolver):
    host = host.lower().rstrip('.')
    if names_protected_namespace(host):
        raise McpUrlRejected(REASON_NAMESPACE)
    literal = _ip(host)
    raw = {str(literal)} if literal else (resolver or resolve)(host)
    addresses = {_ip(value) for value in raw}
    if not addresses or None in addresses:
        raise McpUrlRejected(REASON_DNS)
    reason = _address_reason(host, addresses, resolver)
    if reason:
        raise McpUrlRejected(reason)
    return sorted(map(str, addresses))


def _http_modules():
    # MCP 2.x (including deployed 2.3.0) uses httpx2/httpcore2. Legacy MCP uses
    # httpx/httpcore. Keep these optional: the save-time guard needs only stdlib.
    try:
        import httpx2 as httpx
        import httpcore2 as httpcore
    except ImportError:
        import httpx
        import httpcore
    return httpx, httpcore


def _network_backend(httpcore):
    return httpcore.AnyIOBackend()


@contextmanager
def _transport_errors(httpx, httpcore):
    try:
        yield
    except (httpcore.TimeoutException, httpcore.NetworkError, httpcore.ProtocolError,
            httpcore.ProxyError, httpcore.UnsupportedProtocol) as exc:
        # Map the most specific public exception (also while consuming a stream).
        for cls in type(exc).__mro__:
            mapped = getattr(httpx, cls.__name__, None)
            if mapped is not None and issubclass(mapped, httpx.RequestError):
                raise mapped(str(exc)) from None
        raise


def _guarded_transport(httpx, httpcore, resolver):
    import anyio

    class NetworkBackend(httpcore.AsyncNetworkBackend):
        def __init__(self):
            self.backend = _network_backend(httpcore)

        async def connect_tcp(self, host, port, timeout=None, local_address=None,
                              socket_options=None):
            # httpcore keeps its original origin for HTTP Host and TLS verification;
            # only the socket destination changes. The lower backend gets a numeric
            # address, so there is no second hostname lookup to rebind.
            try:
                with anyio.fail_after(timeout) as scope:
                    addresses = await anyio.to_thread.run_sync(
                        _connection_addresses, host, resolver, abandon_on_cancel=True)
                    for address in addresses:
                        remaining = max(0, scope.deadline - anyio.current_time())
                        try:
                            return await self.backend.connect_tcp(
                                address, port, timeout=remaining, local_address=local_address,
                                socket_options=socket_options)
                        except (httpcore.ConnectError, httpcore.ConnectTimeout):
                            if address == addresses[-1]:
                                raise
            except TimeoutError:
                raise httpcore.ConnectTimeout('MCP connection timed out') from None

        async def sleep(self, seconds):
            await self.backend.sleep(seconds)

    class ResponseStream(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            with _transport_errors(httpx, httpcore):
                async for chunk in self.stream:
                    yield chunk

        async def aclose(self):
            await self.stream.aclose()

    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.pool = httpcore.AsyncConnectionPool(
                ssl_context=httpcore.default_ssl_context(),
                network_backend=NetworkBackend(), max_connections=100,
                max_keepalive_connections=20, keepalive_expiry=5)

        async def handle_async_request(self, request):
            # Avoid allowing a caller to override TLS hostname validation via an
            # extension. The configured URL is always the certificate identity.
            extensions = dict(request.extensions)
            extensions.pop('sni_hostname', None)
            core_request = httpcore.Request(
                method=request.method,
                url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                                 port=request.url.port, target=request.url.raw_path),
                headers=request.headers.raw, content=request.stream, extensions=extensions)
            with _transport_errors(httpx, httpcore):
                response = await self.pool.handle_async_request(core_request)
            return httpx.Response(response.status, headers=response.headers,
                                  stream=ResponseStream(response.stream),
                                  extensions=response.extensions)

        async def aclose(self):
            await self.pool.aclose()

    return Transport()


def guarded_http_client_factory(base_factory=None, resolver=None):
    """MCP client factory: validate and pin every new TCP connection, never follow redirects.

    ``base_factory`` is an AsyncClient constructor accepting ``transport`` (tests).
    Construct through public APIs rather than replacing an existing client's private
    pool. Disable environment proxies, which could resolve the hostname independently.
    """
    def factory(headers=None, timeout=None, auth=None):
        httpx, httpcore = _http_modules()

        async def check_response(response):
            if 300 <= response.status_code < 400:
                raise McpUrlRejected(REASON_REDIRECT)

        return (base_factory or httpx.AsyncClient)(
            headers=headers, timeout=timeout if timeout is not None else httpx.Timeout(30, read=300),
            auth=auth, transport=_guarded_transport(httpx, httpcore, resolver),
            trust_env=False, follow_redirects=False, event_hooks={'response': [check_response]})
    return factory
