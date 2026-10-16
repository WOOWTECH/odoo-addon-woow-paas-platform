"""The request pipeline (DESIGN §1.4.2).

Order of checks, each failing closed:

1. raw path / query canonical form ............................ 400
2. body size (signature covers the body) ...................... 413
3. HMAC: kid, ts ±30 s, signature, replay within 60 s ......... 401
4. identity, synthetic client IP .............................. 400
5. key class (WEB → user interactive routes only), break-glass  403
6. route whitelist, query keys and values ..................... 403 / 400
7. Authorization / X-Security-Proof handling .................. 400 / 403
8. JSON body + body policy .................................... 400 / 403
9. in-flight cap per identity ................................. 503 busy
10. reveal / add_quota pre-checks, add_quota hourly budget .... 403 / 429
11. one upstream call, no retry: connect 502 | reset 502 | timeout 504

Upstream responses pass through with only Content-Type, X-Oneapi-Request-Id
and Retry-After kept (Set-Cookie and everything else dropped). Proxy-made
errors carry ``X-Woow-Proxy-Error: <class>``.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from . import __version__, canonical, logs, policy, routetable, signing
from .limits import HourlyBudget, InflightLimiter, ReplayCache
from .metrics import Metrics
from .settings import IDENTITIES, SYSTEM_IPS, Settings
from .upstream import CONNECT, Upstream, UpstreamError

ERROR_HEADER = "X-Woow-Proxy-Error"

_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_TS_RE = re.compile(r"[0-9]{1,12}", re.ASCII)
_SIG_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_IDENTITY_FORMAT = re.compile(r"[a-z]{1,8}", re.ASCII)
_CLIENT_IP_FORMAT = re.compile(r"[0-9.]{0,15}", re.ASCII)
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4_RE = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}", re.ASCII)
_JWT = r"[A-Za-z0-9_-]{4,2048}\.[A-Za-z0-9_-]{4,4096}\.[A-Za-z0-9_-]{4,1024}"
_BEARER_RE = re.compile(rf"Bearer (?:nap_[A-Za-z0-9]{{20,128}}|{_JWT})", re.ASCII)
_PROOF_RE = re.compile(_JWT, re.ASCII)
_READ_TIMEOUT_RE = re.compile(r"[1-9]|[12][0-9]|30", re.ASCII)
_CONTENT_LENGTH_RE = re.compile(r"[0-9]{1,12}", re.ASCII)
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}", re.ASCII)
_RETRY_AFTER_RE = re.compile(r"[0-9]{1,6}", re.ASCII)
_CONTENT_TYPE_RE = re.compile(r"[A-Za-z0-9!#$&^_.+/=; -]{1,128}", re.ASCII)

#: Minimum read time left before the main upstream call is attempted.
MIN_SEND_BUDGET = 0.5
PRECHECK_READ_TIMEOUT = 3.0


class ProxyError(Exception):
    """A response the proxy itself produces. ``reason`` is a stable code."""

    STATUS = {
        "bad_request": 400,
        "auth": 401,
        "policy": 403,
        "too_large": 413,
        "budget": 429,
        "internal": 500,
        "connect": 502,
        "reset": 502,
        "config": 503,
        "busy": 503,
        "timeout": 504,
    }

    def __init__(self, kind: str, reason: str, headers: dict[str, str] | None = None):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.status = self.STATUS[kind]
        self.headers = headers or {}


@dataclass
class Ctx:
    method: str = "-"
    route: str = "-"
    identity: str = "-"
    kid: str = "-"
    key_class: str = "-"
    ip: str = "-"
    request_id: str = "-"
    proxy_error: str | None = None
    reason: str | None = None
    upstream_status: int | None = None
    started: float = field(default_factory=time.monotonic)


def _single(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) > 1:
        raise ProxyError("bad_request", "duplicate_header")
    return values[0] if values else None


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def parse_json_object(body: bytes) -> dict:
    """Strict JSON: UTF-8, no duplicate keys, no NaN/Infinity, top-level object."""
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProxyError("bad_request", "body_json") from None
    if not isinstance(value, dict):
        raise ProxyError("bad_request", "body_not_object")
    return value


def _json_or_none(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _days_left(expires_at: int | None, now: int) -> int | None:
    if expires_at is None:
        return None
    return (expires_at - now) // 86400


class Proxy:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.clock = clock
        self.monotonic = monotonic
        self.upstream = Upstream(
            settings.upstream_url,
            settings.connect_timeout,
            settings.max_response_bytes,
            transport=transport,
        )
        self.replay = ReplayCache(signing.REPLAY_TTL_SECONDS, settings.replay_cache_max)
        self.inflight = InflightLimiter(dict(settings.inflight))
        self.budget = HourlyBudget(settings.add_quota_hourly_cap)
        self.metrics = Metrics()
        self.metrics.gauge(
            "newapi_proxy_inflight",
            lambda: {(("identity", k),): v for k, v in self.inflight.current.items()},
        )
        self.metrics.gauge(
            "newapi_proxy_add_quota_budget_used",
            lambda: {(): self.budget.used(self.monotonic())},
        )

    # --- helpers ---------------------------------------------------------------

    def _now(self) -> int:
        return int(self.clock())

    def break_glass_active(self, now: int) -> bool:
        until = self.settings.break_glass_until
        return until is not None and now < until

    def _base_headers(self, ip: str, write: bool) -> dict[str, str]:
        headers = {
            "X-Forwarded-For": ip,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": f"woow-newapi-proxy/{__version__}",
        }
        if write:
            headers["Connection"] = "close"
        return headers

    def _tenant_ip(self, raw: str) -> str:
        if not raw:
            raise ProxyError("bad_request", "client_ip_missing")
        if not _IPV4_RE.fullmatch(raw):
            raise ProxyError("bad_request", "client_ip_format")
        value = int(ipaddress.IPv4Address(raw))
        if not self.settings.tenant_ip_first <= value <= self.settings.tenant_ip_last:
            raise ProxyError("bad_request", "client_ip_range")
        return raw

    def _is_service_token(self, token: str) -> bool:
        for own in (self.settings.ap_token, self.settings.op_token):
            if own and hmac.compare_digest(own.encode(), token.encode()):
                return True
        return False

    def _remaining(self, deadline: float) -> float:
        return deadline - self.monotonic()

    # --- ASGI endpoint -----------------------------------------------------------

    async def endpoint(self, request: Request) -> Response:
        ctx = Ctx(method=request.method)
        raw_path = request.scope.get("raw_path")
        try:
            if raw_path in (b"/healthz", b"/readyz", b"/metrics"):
                response = await self._health(request, raw_path)
            else:
                response = await self._proxy(request, ctx)
        except ProxyError as err:
            response = self._error(err, ctx)
        except ClientDisconnect:
            response = self._error(ProxyError("bad_request", "client_disconnect"), ctx)
        except Exception as exc:  # noqa: BLE001 - last resort, type name only
            logs.event("internal_error", logging.ERROR, exc_type=type(exc).__name__, route=ctx.route)
            response = self._error(ProxyError("internal", "internal"), ctx)
        if raw_path not in (b"/healthz", b"/readyz", b"/metrics"):
            self._log_access(ctx, response.status_code)
        return response

    def _error(self, err: ProxyError, ctx: Ctx) -> Response:
        ctx.proxy_error = err.kind
        ctx.reason = err.reason
        self.metrics.inc("newapi_proxy_rejections_total", kind=err.kind, reason=err.reason)
        # 401 says nothing more than "auth" (the precise reason is logged).
        reason = "auth" if err.kind == "auth" else err.reason
        body = json.dumps(
            {
                "success": False,
                "message": f"newapi-proxy: {err.kind}",
                "proxy_error": err.kind,
                "reason": reason,
            },
            separators=(",", ":"),
        ).encode()
        headers = {ERROR_HEADER: err.kind, "Cache-Control": "no-store", **err.headers}
        return Response(body, status_code=err.status, headers=headers, media_type="application/json")

    def _log_access(self, ctx: Ctx, status: int) -> None:
        self.metrics.inc(
            "newapi_proxy_requests_total",
            identity=ctx.identity,
            route=ctx.route,
            code=f"{status // 100}xx",
        )
        logs.access(
            method=ctx.method,
            route=ctx.route,
            status=status,
            upstream_status=ctx.upstream_status,
            ms=round((time.monotonic() - ctx.started) * 1000, 1),
            request_id=ctx.request_id,
            ip=ctx.ip,
            identity=ctx.identity,
            kid=ctx.kid,
            key_class=ctx.key_class,
            proxy_error=ctx.proxy_error,
            reason=ctx.reason,
        )

    # --- health ----------------------------------------------------------------

    async def _health(self, request: Request, raw_path: bytes) -> Response:
        if request.method != "GET" or request.scope.get("query_string"):
            raise ProxyError("policy", "health_method")
        if raw_path == b"/healthz":
            return Response(b'{"ok":true}', media_type="application/json")
        if raw_path == b"/metrics":
            return Response(self.metrics.render(), media_type="text/plain; version=0.0.4")
        return await self._readyz()

    async def _readyz(self) -> Response:
        now = self._now()
        upstream_state = "ok"
        try:
            resp = await self.upstream.send(
                "GET",
                "/api/status",
                self._base_headers(SYSTEM_IPS["anon"], write=False),
                b"",
                read_timeout=PRECHECK_READ_TIMEOUT,
                write=False,
            )
            data = _json_or_none(resp.body)
            ready = resp.status == 200 and isinstance(data, dict) and data.get("success") is True
            if not ready:
                upstream_state = f"http_{resp.status}"
        except UpstreamError as err:
            ready = False
            upstream_state = err.kind
        body = {
            "ready": ready,
            "upstream": upstream_state,
            "ap_configured": self.settings.ap_token is not None,
            "op_configured": self.settings.op_token is not None,
            "ap_days_left": _days_left(self.settings.ap_expires_at, now),
            "op_days_left": _days_left(self.settings.op_expires_at, now),
            "break_glass": self.break_glass_active(now),
            "version": __version__,
        }
        return Response(
            json.dumps(body, separators=(",", ":")).encode(),
            status_code=200 if ready else 503,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    # --- proxy -----------------------------------------------------------------

    async def _read_body(self, request: Request) -> bytes:
        limit = self.settings.max_body_bytes
        length = _single(request, "content-length")
        if length is not None:
            if not _CONTENT_LENGTH_RE.fullmatch(length):
                raise ProxyError("bad_request", "content_length")
            if int(length) > limit:
                raise ProxyError("too_large", "body_too_large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise ProxyError("too_large", "body_too_large")
        return bytes(body)

    async def _proxy(self, request: Request, ctx: Ctx) -> Response:
        settings = self.settings
        method = request.method
        try:
            path = canonical.check_raw_path(request.scope.get("raw_path"))
            pairs = canonical.parse_query(request.scope.get("query_string", b""))
        except canonical.CanonicalError as err:
            raise ProxyError("bad_request", err.reason) from None
        query = signing.canonical_query(pairs)

        kid = _single(request, signing.KID_HEADER)
        ts = _single(request, signing.TS_HEADER)
        sig = _single(request, signing.SIG_HEADER)
        identity = _single(request, signing.IDENTITY_HEADER)
        client_ip = _single(request, signing.CLIENT_IP_HEADER) or ""
        if not (kid and ts and sig and identity):
            raise ProxyError("auth", "auth_missing")
        if not _IDENTITY_FORMAT.fullmatch(identity) or not _CLIENT_IP_FORMAT.fullmatch(client_ip):
            raise ProxyError("bad_request", "identity_format")
        key = settings.key_for(kid) if _KID_RE.fullmatch(kid) else None
        if key is None:
            raise ProxyError("auth", "auth_kid")
        key_class, secret = key
        ctx.kid, ctx.key_class = kid, key_class
        now = self._now()
        if not _TS_RE.fullmatch(ts):
            raise ProxyError("auth", "auth_ts")
        if abs(now - int(ts)) > signing.MAX_SKEW_SECONDS:
            raise ProxyError("auth", "auth_ts_skew")
        if not _SIG_RE.fullmatch(sig):
            raise ProxyError("auth", "auth_sig_format")

        body = await self._read_body(request)
        message = signing.string_to_sign(method, path, query, body, ts, identity, client_ip)
        if not signing.signature_matches(secret, message, sig):
            raise ProxyError("auth", "auth_sig")
        replay = self.replay.add(sig, self.monotonic())
        if replay == "replay":
            raise ProxyError("auth", "auth_replay")
        if replay == "full":
            raise ProxyError("busy", "replay_cache_full")

        # identity and synthetic source address
        if identity not in IDENTITIES:
            raise ProxyError("bad_request", "identity_unknown")
        ctx.identity = identity
        if identity == "user":
            ip = self._tenant_ip(client_ip)
        else:
            if client_ip:
                raise ProxyError("bad_request", "client_ip_not_allowed")
            ip = SYSTEM_IPS[identity]
        ctx.ip = ip

        if key_class == "web" and identity != "user":
            raise ProxyError("policy", "key_scope")
        if identity == "bg":
            active = self.break_glass_active(now)
            self.metrics.inc("newapi_proxy_break_glass_total", result="active" if active else "off")
            logs.audit("bg", kid=kid, ip=ip, method=method, path=path, active=active)
            if not active:
                raise ProxyError("policy", "break_glass_off")

        matched = routetable.match(method, path)
        if matched is None or identity not in matched.route.identities:
            raise ProxyError("policy", "route")
        route = matched.route
        ctx.route = route.name
        if key_class == "web" and not route.web:
            raise ProxyError("policy", "key_scope")

        for name, value in pairs:
            spec = route.query.get(name)
            if spec is None:
                raise ProxyError("bad_request", "query_key")
            pattern = settings.username_re if spec == routetable.USERNAME else spec
            if not pattern.fullmatch(value):
                raise ProxyError("bad_request", "query_value")

        # credentials
        authorization = _single(request, "authorization")
        if identity == "user":
            if route.user_auth == "forbidden":
                if authorization is not None:
                    raise ProxyError("bad_request", "authorization_not_allowed")
                upstream_auth = None
            else:
                if authorization is None:
                    raise ProxyError("bad_request", "authorization_missing")
                if not _BEARER_RE.fullmatch(authorization):
                    raise ProxyError("bad_request", "authorization_format")
                if self._is_service_token(authorization[len("Bearer ") :]):
                    raise ProxyError("policy", "service_token")
                upstream_auth = authorization
        else:
            if authorization is not None:
                raise ProxyError("bad_request", "authorization_not_allowed")
            if identity in ("ap", "bg"):
                if not settings.ap_token:
                    raise ProxyError("config", "ap_token_missing")
                upstream_auth = f"Bearer {settings.ap_token}"
            elif identity == "op":
                if not settings.op_token:
                    raise ProxyError("config", "op_token_missing")
                upstream_auth = f"Bearer {settings.op_token}"
            else:
                upstream_auth = None

        proof = _single(request, "x-security-proof")
        if route.proof:
            if proof is None or not _PROOF_RE.fullmatch(proof):
                raise ProxyError("bad_request", "proof_missing")
        elif proof is not None:
            raise ProxyError("bad_request", "proof_not_allowed")

        read_timeout_raw = _single(request, signing.READ_TIMEOUT_HEADER)
        if read_timeout_raw is None:
            read_timeout = settings.default_read_timeout
        elif _READ_TIMEOUT_RE.fullmatch(read_timeout_raw):
            read_timeout = float(read_timeout_raw)
        else:
            raise ProxyError("bad_request", "read_timeout")

        # body policy
        validated: dict | None = None
        if route.body == "none":
            if body:
                raise ProxyError("bad_request", "body_not_allowed")
            upstream_body = b""
        else:
            obj = parse_json_object(body)
            try:
                validated = route.body_policy(obj, policy.PolicyContext(settings, now))
            except policy.PolicyDenied as err:
                # e.g. an add_quota "override" attempt: worth an audit line.
                self._audit(route, {"kid": kid, "ip": ip}, result="denied", reason=err.reason)
                raise ProxyError("policy", err.reason) from None
            upstream_body = json.dumps(validated, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(
                "ascii"
            )

        if not self.inflight.try_acquire(identity):
            raise ProxyError("busy", "inflight")
        try:
            return await self._forward(
                ctx, matched, method, query, ip, upstream_auth, proof, upstream_body, validated, read_timeout, now
            )
        finally:
            self.inflight.release(identity)

    async def _forward(
        self,
        ctx: Ctx,
        matched: routetable.Match,
        method: str,
        query: str,
        ip: str,
        upstream_auth: str | None,
        proof: str | None,
        upstream_body: bytes,
        validated: dict | None,
        read_timeout: float,
        now: int,
    ) -> Response:
        route = matched.route
        deadline = self.monotonic() + read_timeout
        write = method != "GET"
        headers = self._base_headers(ip, write)
        if upstream_auth:
            headers["Authorization"] = upstream_auth
        if proof:
            headers["X-Security-Proof"] = proof
        if upstream_body:
            headers["Content-Type"] = "application/json"
        url = matched.upstream_path() + (f"?{query}" if query else "")

        reservation = None
        audit_fields: dict[str, Any] = {"kid": ctx.kid, "ip": ip}
        if route.special == "reveal":
            token_id = int(matched.params["id"])
            audit_fields["token_id"] = token_id
            await self._reveal_precheck(route, token_id, ip, upstream_auth, deadline, now, audit_fields)
        elif route.special == "manage":
            assert validated is not None
            audit_fields.update(target_user_id=validated["id"], value=validated["value"])
            reservation = await self._manage_precheck(route, validated, deadline, audit_fields)

        remaining = self._remaining(deadline)
        if remaining < MIN_SEND_BUDGET:
            if reservation is not None:
                self.budget.release(reservation)
            self._audit(route, audit_fields, result="not_sent", reason="deadline")
            raise ProxyError("connect", "deadline")
        try:
            resp = await self.upstream.send(method, url, headers, upstream_body, read_timeout=remaining, write=write)
        except UpstreamError as err:
            if reservation is not None and err.kind == CONNECT:
                self.budget.release(reservation)
            self.metrics.inc("newapi_proxy_upstream_errors_total", kind=err.kind, exc=err.exc_type)
            self._audit(route, audit_fields, result=err.kind)
            raise ProxyError(err.kind, err.detail or f"upstream_{err.kind}") from None
        ctx.upstream_status = resp.status
        response = self._relay(resp, ctx)
        self._audit(route, audit_fields, result="forwarded", status=resp.status, request_id=ctx.request_id)
        if route.special == "manage" and resp.status == 200:
            answer = _json_or_none(resp.body)
            if isinstance(answer, dict) and answer.get("success") is True:
                self.metrics.inc("newapi_proxy_add_quota_value_total", validated["value"])
        return response

    def _audit(self, route: routetable.Route, fields: dict, **outcome: Any) -> None:
        if route.audit is None:
            return
        metric = {"reveal": "newapi_proxy_reveal_total", "add_quota": "newapi_proxy_add_quota_total"}[route.audit]
        self.metrics.inc(metric, result=str(outcome.get("result")))
        logs.audit(route.audit, **fields, **outcome)

    async def _precheck_get(self, url: str, ip: str, auth: str, deadline: float, what: str) -> dict:
        """GET used to vet a guarded call. Any failure means it was not sent."""
        timeout = min(PRECHECK_READ_TIMEOUT, self._remaining(deadline))
        if timeout < MIN_SEND_BUDGET:
            raise ProxyError("connect", "deadline")
        headers = self._base_headers(ip, write=False)
        headers["Authorization"] = auth
        try:
            resp = await self.upstream.send("GET", url, headers, b"", read_timeout=timeout, write=False)
        except UpstreamError as err:
            self.metrics.inc("newapi_proxy_upstream_errors_total", kind=err.kind, exc=err.exc_type)
            # The guarded call itself was never sent.
            raise ProxyError("connect", f"{what}_precheck_{err.kind}") from None
        if resp.status != 200:
            extra = {}
            retry_after = resp.headers.get("retry-after")
            if retry_after and _RETRY_AFTER_RE.fullmatch(retry_after):
                extra["Retry-After"] = retry_after
            raise ProxyError("connect", f"{what}_precheck_http_{resp.status}", headers=extra)
        data = _json_or_none(resp.body)
        if not isinstance(data, dict):
            raise ProxyError("connect", f"{what}_precheck_body")
        return data

    async def _reveal_precheck(self, route, token_id, ip, auth, deadline, now, fields) -> None:
        """Reveal only wp_ keys created within the last 120 s (〔REV〕2)."""
        try:
            data = await self._precheck_get(f"/api/token/{token_id}", ip, auth, deadline, "reveal")
        except ProxyError as err:
            self._audit(route, fields, result="not_sent", reason=err.reason)
            raise
        if data.get("success") is not True:
            self._audit(route, fields, result="denied", reason="reveal_token_unknown")
            raise ProxyError("policy", "reveal_token_unknown")
        denial = policy.reveal_denial(data.get("data"), token_id, now)
        if denial is not None:
            self._audit(route, fields, result="denied", reason=denial)
            raise ProxyError("policy", denial)

    async def _manage_precheck(self, route, validated: dict, deadline: float, fields: dict):
        """Hourly budget, then the target must be a PaaS user (role 1, default)."""
        reservation = self.budget.reserve(validated["value"], self.monotonic())
        if reservation is None:
            self._audit(route, fields, result="denied", reason="hourly_cap")
            logs.event("add_quota_budget_exhausted", logging.WARNING, cap=self.budget.cap)
            raise ProxyError("budget", "hourly_cap")
        try:
            data = await self._precheck_get(
                f"/api/user/{validated['id']}",
                SYSTEM_IPS["ap"],
                f"Bearer {self.settings.ap_token}",
                deadline,
                "manage",
            )
        except ProxyError as err:
            self.budget.release(reservation)
            self._audit(route, fields, result="not_sent", reason=err.reason)
            raise
        ctx = policy.PolicyContext(self.settings, self._now())
        if data.get("success") is not True or not policy.manage_target_ok(data.get("data"), validated["id"], ctx):
            self.budget.release(reservation)
            self._audit(route, fields, result="denied", reason="manage_target")
            raise ProxyError("policy", "manage_target")
        return reservation

    def _relay(self, resp, ctx: Ctx) -> Response:
        headers = {"Cache-Control": "no-store"}
        content_type = resp.headers.get("content-type")
        if content_type and _CONTENT_TYPE_RE.fullmatch(content_type):
            headers["Content-Type"] = content_type
        request_id = resp.headers.get("x-oneapi-request-id")
        if request_id and _REQUEST_ID_RE.fullmatch(request_id):
            headers["X-Oneapi-Request-Id"] = request_id
            ctx.request_id = request_id
        retry_after = resp.headers.get("retry-after")
        if retry_after and _RETRY_AFTER_RE.fullmatch(retry_after):
            headers["Retry-After"] = retry_after
        return Response(resp.body, status_code=resp.status, headers=headers)


class ProxyApp:
    """Plain ASGI entry: every HTTP request goes to ``Proxy.endpoint``.

    Starlette's router is deliberately not used — it matches on the *decoded*
    path; the proxy decides everything from ``raw_path`` itself.
    """

    def __init__(self, proxy: Proxy):
        self.proxy = proxy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            response = await self.proxy.endpoint(Request(scope, receive))
            await response(scope, receive, send)
        elif scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.proxy.upstream.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        # websocket and anything else: refuse by not accepting.


def create_app(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProxyApp:
    return ProxyApp(Proxy(settings, transport=transport, clock=clock, monotonic=monotonic))
