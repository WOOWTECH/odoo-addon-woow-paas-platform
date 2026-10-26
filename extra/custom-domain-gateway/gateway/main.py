"""Fail-closed custom-domain HTTP gateway for service-owned public Odoo."""
from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket


DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}
FORWARDING_HEADERS = {
    "forwarded", "x-real-ip", "cf-connecting-ip", "true-client-ip",
    "x-client-ip", "x-cluster-client-ip", "x-original-host",
}


@dataclass(frozen=True)
class Route:
    service: str
    namespace: str
    port: int

    @property
    def upstream(self) -> str:
        return f"http://{self.service}.{self.namespace}.svc.cluster.local:{self.port}"


class Settings:
    def __init__(self) -> None:
        self.routes_file = Path(os.getenv("ROUTES_FILE", "/config/routes.json"))
        self.namespace_prefix = os.getenv("PAAS_NAMESPACE_PREFIX", "")
        self.route_check_token = os.getenv("ROUTE_CHECK_TOKEN", "")
        self.max_request_bytes = int(os.getenv("MAX_REQUEST_BYTES", str(8 * 1024 * 1024)))
        self.max_response_bytes = int(os.getenv("MAX_RESPONSE_BYTES", str(16 * 1024 * 1024)))
        self.connect_timeout = float(os.getenv("UPSTREAM_CONNECT_TIMEOUT_SECONDS", "3"))
        self.read_timeout = float(os.getenv("UPSTREAM_READ_TIMEOUT_SECONDS", "30"))


class RouteStore:
    """mtime-cached route file; any read/parse/validation failure empties the table."""

    def __init__(self, path: Path, namespace_prefix: str) -> None:
        self.path = path
        self.namespace_prefix = namespace_prefix
        self._stamp: tuple[int, int] | None = None
        self._routes: dict[str, Route] = {}

    def load(self) -> dict[str, Route]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == self._stamp:
                return self._routes
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            routes = self._parse(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            # Do not retain a last-known-good route after a broken ConfigMap update.
            self._routes = {}
            self._stamp = None
            return self._routes
        self._routes = routes
        self._stamp = stamp
        return self._routes

    def _parse(self, raw: object) -> dict[str, Route]:
        if not self.namespace_prefix or not isinstance(raw, dict):
            raise ValueError("invalid gateway configuration")
        routes: dict[str, Route] = {}
        for hostname, item in raw.items():
            if (
                not isinstance(hostname, str)
                or hostname != hostname.lower()
                or HOSTNAME.fullmatch(hostname) is None
                or not isinstance(item, dict)
                or set(item) != {"service", "namespace", "port"}
            ):
                raise ValueError("invalid route")
            service = item["service"]
            namespace = item["namespace"]
            port = item["port"]
            if (
                not isinstance(service, str)
                or DNS_LABEL.fullmatch(service) is None
                or not isinstance(namespace, str)
                or DNS_LABEL.fullmatch(namespace) is None
                or not namespace.startswith(self.namespace_prefix)
                or (self.namespace_prefix == "paas-ws-"
                    and namespace.startswith("paas-ws-stg-"))
                or type(port) is not int
                or port != 80
            ):
                raise ValueError("invalid route target")
            routes[hostname] = Route(service=service, namespace=namespace, port=port)
        return routes


settings = Settings()
route_store = RouteStore(settings.routes_file, settings.namespace_prefix)
app = FastAPI(title="Custom Domain Gateway", docs_url=None, redoc_url=None)


def _request_host(request: Request) -> str:
    values = [value.decode("latin-1") for key, value in request.scope["headers"] if key.lower() == b"host"]
    if len(values) != 1:
        raise HTTPException(status_code=400, detail="Malformed Host header")
    host = values[0]
    if host != host.lower() or HOSTNAME.fullmatch(host) is None:
        raise HTTPException(status_code=400, detail="Malformed Host header")
    return host


def _connection_tokens(headers: httpx.Headers) -> set[str]:
    tokens: set[str] = set()
    for value in headers.get_list("connection"):
        tokens.update(part.strip().lower() for part in value.split(",") if part.strip())
    return tokens


def _upstream_request_headers(request: Request, host: str) -> list[tuple[str, str]]:
    connection_tokens = _connection_tokens(httpx.Headers(request.headers.raw))
    headers: list[tuple[str, str]] = []
    for key_raw, value_raw in request.headers.raw:
        key = key_raw.decode("latin-1").lower()
        if (
            key in HOP_BY_HOP
            or key in connection_tokens
            or key in FORWARDING_HEADERS
            or key.startswith("x-forwarded-")
            or key in {"host", "content-length"}
        ):
            continue
        headers.append((key, value_raw.decode("latin-1")))
    headers.extend((("host", host), ("x-forwarded-proto", "https")))
    return headers


def _downstream_response_headers(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
    connection_tokens = _connection_tokens(headers)
    # httpx.aiter_bytes() decodes gzip/br/deflate before the body reaches the
    # browser. Do not forward metadata that describes the *encoded* bytes.
    decoded = "content-encoding" in headers
    representation_headers = {"content-encoding", "etag", "content-md5", "digest", "content-range"}
    return [
        (key, value)
        for key, value in headers.raw
        if key.decode("latin-1").lower() not in HOP_BY_HOP
        and key.decode("latin-1").lower() not in connection_tokens
        and key.decode("latin-1").lower() != "content-length"
        and not (decoded and key.decode("latin-1").lower() in representation_headers)
    ]


async def _bounded_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > settings.max_request_bytes:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_request_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")
    return bytes(body)


async def _send_upstream(
    method: str, url: str, headers: list[tuple[str, str]], body: bytes,
) -> httpx.Response:
    timeout = httpx.Timeout(
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.read_timeout,
        pool=settings.connect_timeout,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 trust_env=False) as client:
        return await client.request(method, url, headers=headers, content=body)


async def _bounded_response(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length")
    if length:
        try:
            if int(length) < 0 or int(length) > settings.max_response_bytes:
                raise HTTPException(status_code=502, detail="Upstream response too large")
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="Invalid upstream Content-Length") from exc
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > settings.max_response_bytes:
            raise HTTPException(status_code=502, detail="Upstream response too large")
    return bytes(data)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/_internal/route")
async def internal_route(request: Request, hostname: str = Query(...)) -> dict[str, object]:
    authorization = request.headers.get("authorization", "")
    expected = f"Bearer {settings.route_check_token}"
    if not settings.route_check_token or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if hostname != hostname.lower() or HOSTNAME.fullmatch(hostname) is None:
        raise HTTPException(status_code=422, detail="Invalid hostname")
    return {"hostname": hostname, "loaded": hostname in route_store.load()}


@app.websocket("/{path:path}")
async def websocket_not_supported(websocket: WebSocket, path: str) -> None:
    """WebSocket proxying is deliberately unsupported in this HTTP-only slice."""
    await websocket.close(code=1008, reason="WebSocket proxying is not supported")


@app.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(request: Request, path: str) -> Response:
    host = _request_host(request)
    route = route_store.load().get(host)
    if route is None:
        raise HTTPException(status_code=404, detail="Unknown host")
    body = await _bounded_body(request)
    # ASGI's decoded `path` can turn %2F into '/', changing the tenant's URL.
    # Use the original percent-encoded path, with the destination host fixed by
    # the verified route table rather than by any incoming URL/header.
    raw_path = request.scope.get("raw_path", b"/").decode("ascii")
    query = request.url.query
    url = f"{route.upstream}{raw_path}" + (f"?{query}" if query else "")
    try:
        upstream = await _send_upstream(
            request.method,
            url,
            _upstream_request_headers(request, host),
            body,
        )
        content = await _bounded_response(upstream)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Upstream timeout") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Upstream unavailable") from exc
    downstream = Response(
        content=b"" if request.method == "HEAD" else content,
        status_code=upstream.status_code,
    )
    # Preserve repeated fields such as Set-Cookie; a dict would comma-join them
    # and break Odoo sessions. Starlette already supplied the correct bounded
    # Content-Length, so only append the filtered upstream fields.
    downstream.raw_headers.extend(_downstream_response_headers(upstream.headers))
    return downstream
