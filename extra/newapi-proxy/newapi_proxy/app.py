"""The request pipeline (DESIGN §1.4.2).

Order of checks on the proxy port, each failing closed:

1. raw path / query canonical form ............................ 400
2. signature headers present and well-formed, kid known, ts ±30 s  401
3. X-Woow-Read-Timeout: the whole request's deadline .......... 400
4. body: size and a 5 s read deadline (signature covers it) ... 413 / 408
5. HMAC signature ............................................. 401
6. replay: (kid, nonce) once — this replica, then Valkey ...... 401 / 503 busy
7. identity, synthetic client IP .............................. 400
8. key class (WEB → user interactive routes only), break-glass  403
9. route whitelist, query keys and values ..................... 403 / 400
10. Authorization / X-Security-Proof handling ................. 400 / 403
11. JSON body + body policy ................................... 400 / 403
12. in-flight cap per identity ................................ 503 busy
13. reveal / manage / bg group pre-checks, add_quota hourly budget  403 denied / 429
14. if configured: durable credit intent, shared AP admission .... 503 busy / 429
15. one upstream call, no retry: connect 502 | reset 502 | timeout 504

Break-glass (``bg``, CRON key only, ``PROXY_BREAK_GLASS_UNTIL`` in the future) has
two routes (§1.4.2-2): the free-tier group fallback ``PUT /api/user/`` (the proxy
reads the user and sends the full update with only ``group`` changed) and the
``mode: subtract`` claw-back on ``POST /api/user/manage``. Every accepted bg call
writes an audit line and a ``break_glass_used`` warning, and counts in
``newapi_proxy_break_glass_calls_total``.

Upstream responses pass through with only Content-Type, X-Oneapi-Request-Id
and Retry-After kept (Set-Cookie and everything else dropped); a route with a
``response`` filter (``GET /api/user/``) has its body rebuilt from an
allow-list first, and an answer of the wrong shape is ``500 internal``.
Proxy-made errors carry ``X-Woow-Proxy-Error: <class>``.

``/healthz``, ``/readyz`` and ``/metrics`` are served by ``AdminApp`` on a
separate listener (``PROXY_ADMIN_PORT``); on the proxy port they are just
unsigned requests (401).
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from . import __version__, canonical, logs, policy, responses, routetable, signing
from .controlstore import ControlStoreError, PostgresControlStore
from .limits import CachedCheck, HourlyBudget, InflightLimiter, ReplayCache
from .metrics import Metrics
from .replaystore import ReplayStoreUnavailable, ValkeyReplayStore
from .settings import IDENTITIES, READINESS_IP, SYSTEM_IPS, Settings
from .upstream import CONNECT, Upstream, UpstreamError, UpstreamResponse

ERROR_HEADER = "X-Woow-Proxy-Error"
#: Marks a relayed answer to a pre-check GET (the guarded call was not sent).
PRECHECK_HEADER = "X-Woow-Proxy-Precheck"

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

#: Minimum time left before an upstream call is attempted at all.
MIN_SEND_BUDGET = 0.5
#: Upper bound for one pre-check GET (reveal / add_quota target).
PRECHECK_BUDGET = 3.0
#: The request body must arrive within this many seconds of the headers.
BODY_TIMEOUT = 5.0
#: Readiness: one upstream /api/status (and Valkey PING) at a time, reused for
#: READY_OK_TTL seconds (READY_FAIL_TTL after a failure). Total < probe timeout.
READY_UPSTREAM_BUDGET = 2.5
READY_OK_TTL = 5.0
READY_FAIL_TTL = 1.0


class ProxyError(Exception):
    """A response the proxy itself produces. ``reason`` is a stable code."""

    STATUS = {
        "bad_request": 400,
        "auth": 401,
        "policy": 403,
        "denied": 403,
        "body_timeout": 408,
        "too_large": 413,
        "budget": 429,
        "internal": 500,
        "connect": 502,
        "reset": 502,
        "config": 503,
        "busy": 503,
        "attempt_unknown": 503,
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
    #: Proxy clock (monotonic) at arrival: the request's deadline counts from here.
    started: float
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
    #: Audit kind of this request: the route's (``reveal``／``add_quota``), or ``bg`` for
    #: every break-glass call; None = not audited.
    audit: str | None = None
    # Legacy/local ID; authenticated conversion metadata may replace it after policy.
    # The same ID joins sending/outcome/access even if upstream returns no ID.
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_requested: bool = False
    correlated: bool = False
    not_sent_confirmed: bool = False
    ap_priority: str = "other"
    t0: float = field(default_factory=time.monotonic)


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


def _nonempty_str(value: Any) -> bool:
    return type(value) is str and value != ""


def _int_field(obj: dict, key: str) -> int | None:
    value = obj.get(key)
    return value if type(value) is int else None  # bool excluded on purpose


def proves_not_executed(status: int, body: bytes) -> bool:
    """new-api answered without running the handler's write (DESIGN §3.4, I2 §4.3).

    429 (rate limit, before the handler), 5xx with an empty body (the limiter
    lost Valkey), any refusal carrying a stable ``code`` (auth / PAT / proof
    middleware), or HTTP 200 ``success:false`` with a ``message_key`` (the
    handler refused and rolled back). Everything else — no ``message_key``,
    ``new_api_panic``, non-JSON — is an unknown outcome.
    """
    if status == 429:
        return True
    if status >= 500 and body == b"":
        return True
    data = _json_or_none(body)
    if not isinstance(data, dict) or data.get("success") is True:
        return False
    if _nonempty_str(data.get("code")):
        return True
    return status == 200 and data.get("success") is False and _nonempty_str(data.get("message_key"))


def _days_left(expires_at: int | None, now: int) -> int | None:
    if expires_at is None:
        return None
    return (expires_at - now) // 86400


def _json_response(body: dict, status: int = 200) -> Response:
    return Response(
        json.dumps(body, separators=(",", ":")).encode(),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


class Proxy:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        replay_store: Any = None,
        control_store: Any = None,
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
        if replay_store is None and settings.valkey_host is not None:
            replay_store = ValkeyReplayStore(
                settings.valkey_host,
                settings.valkey_port,
                settings.valkey_user,
                settings.valkey_password or "",
            )
        #: Shared (all replicas) replay record; None only with one replica.
        self.shared_replay = replay_store
        self.control = control_store
        if self.control is None and settings.control_dsn:
            self.control = PostgresControlStore(settings.control_dsn, settings.control_environment)
        self.inflight = InflightLimiter(dict(settings.inflight))
        self.budget = HourlyBudget(settings.add_quota_hourly_cap)
        self.metrics = Metrics()
        self.readiness = CachedCheck(
            self._readiness_check,
            self.monotonic,
            is_ok=lambda result: result["ready"],
            ok_ttl=READY_OK_TTL,
            fail_ttl=READY_FAIL_TTL,
        )
        self.metrics.gauge(
            "newapi_proxy_inflight",
            lambda: {(("identity", k),): v for k, v in self.inflight.current.items()},
        )
        self.metrics.gauge(
            "newapi_proxy_add_quota_budget_used",
            lambda: {(): self.budget.used(self.monotonic())},
        )

    async def aclose(self) -> None:
        await self.upstream.aclose()
        if self.shared_replay is not None:
            await self.shared_replay.aclose()
        if self.control is not None:
            await self.control.aclose()

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

    async def _control_call(self, deadline, method, *args):
        # Includes connection, lock, query and COMMIT; never retry a lost reply.
        budget = min(0.5, self._remaining(deadline) - MIN_SEND_BUDGET)
        if budget <= 0:
            raise ControlStoreError("deadline")
        try:
            async with asyncio.timeout(budget):
                return await getattr(self.control, method)(*args)
        except TimeoutError:
            raise ControlStoreError("deadline") from None

    async def _ap_admit(self, ctx, deadline, auth):
        if self.control is None or not auth or auth != f"Bearer {self.settings.ap_token}":
            return
        try:
            retry = await self._control_call(deadline, "acquire", ctx.ap_priority)
        except ControlStoreError:
            raise ProxyError("busy", "control_store") from None
        if retry:
            raise ProxyError("budget", "ap_rate", {"Retry-After": str(retry)})

    async def _finish_intent(self, ctx, deadline, result, status=None, request_id=None):
        # HTTP has possibly executed: losing its audit outcome must not turn a
        # successful credit into NotExecuted or trigger an automatic resend.
        if request_id is not None and logs.Redactor(self.settings.secrets)(request_id) != request_id:
            request_id = None
        try:
            await self._control_call(deadline, "finish", ctx.attempt_id, result, status, request_id)
            if result == "not_sent":
                ctx.not_sent_confirmed = True
        except ControlStoreError:
            logs.event("audit_outcome_pending", logging.ERROR, attempt_id=ctx.attempt_id)
            self.metrics.inc("newapi_proxy_audit_pending_total")

    # --- proxy port --------------------------------------------------------------

    async def endpoint(self, request: Request) -> Response:
        ctx = Ctx(started=self.monotonic(), method=request.method)
        try:
            response = await self._proxy(request, ctx)
        except ProxyError as err:
            response = self._error(err, ctx)
        except ClientDisconnect:
            response = self._error(ProxyError("bad_request", "client_disconnect"), ctx)
        except Exception as exc:  # noqa: BLE001 - last resort, type name only
            logs.event("internal_error", logging.ERROR, exc_type=type(exc).__name__, route=ctx.route)
            response = self._error(ProxyError("internal", "internal"), ctx)
        self._log_access(ctx, response.status_code)
        return response

    def _error(self, err: ProxyError, ctx: Ctx) -> Response:
        # Failure of this delivery cannot prove the caller's durable attempt
        # was never executed by an earlier delivery on another replica.
        if ((ctx.correlated or ctx.correlation_requested) and not ctx.not_sent_confirmed
                and err.kind in {"busy", "budget", "config", "connect", "body_timeout"}):
            err = ProxyError("attempt_unknown", "begin_unconfirmed")
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
            attempt_id=ctx.attempt_id,
            method=ctx.method,
            route=ctx.route,
            status=status,
            upstream_status=ctx.upstream_status,
            ms=round((time.monotonic() - ctx.t0) * 1000, 1),
            request_id=ctx.request_id,
            ip=ctx.ip,
            identity=ctx.identity,
            kid=ctx.kid,
            key_class=ctx.key_class,
            proxy_error=ctx.proxy_error,
            reason=ctx.reason,
        )

    async def _read_body(self, request: Request) -> bytes:
        limit = self.settings.max_body_bytes
        length = _single(request, "content-length")
        if length is not None:
            if not _CONTENT_LENGTH_RE.fullmatch(length):
                raise ProxyError("bad_request", "content_length")
            if int(length) > limit:
                raise ProxyError("too_large", "body_too_large")
        body = bytearray()
        try:
            # Before the body is read only a known kid and a fresh ts have been
            # checked (neither is secret): a slow sender must not hold a slot.
            async with asyncio.timeout(BODY_TIMEOUT):
                async for chunk in request.stream():
                    body += chunk
                    if len(body) > limit:
                        raise ProxyError("too_large", "body_too_large")
        except TimeoutError:
            raise ProxyError("body_timeout", "body_timeout") from None
        return bytes(body)

    async def _claim_nonce(self, kid: str, nonce: str) -> None:
        """Each (kid, nonce) is accepted once, on whichever replica sees it first."""
        key = f"{kid}:{nonce}"
        local = self.replay.add(key, self.monotonic())
        if local == "replay":
            raise ProxyError("auth", "auth_replay")
        if local == "full":
            raise ProxyError("busy", "replay_cache_full")
        if self.shared_replay is None:
            return
        try:
            fresh = await self.shared_replay.claim(key, signing.REPLAY_TTL_SECONDS)
        except ReplayStoreUnavailable as err:
            # No shared record = no replay protection across replicas: refuse
            # (new-api itself is down when its Valkey is). Not sent: the client
            # re-sends with a new nonce.
            self.metrics.inc("newapi_proxy_replay_store_errors_total", kind=err.kind)
            raise ProxyError("busy", "replay_store") from None
        if not fresh:
            raise ProxyError("auth", "auth_replay")

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
        nonce = _single(request, signing.NONCE_HEADER)
        sig = _single(request, signing.SIG_HEADER)
        identity = _single(request, signing.IDENTITY_HEADER)
        client_ip = _single(request, signing.CLIENT_IP_HEADER) or ""
        if not (kid and ts and nonce and sig and identity):
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
        if not signing.NONCE_RE.fullmatch(nonce):
            raise ProxyError("auth", "auth_nonce_format")
        if not _SIG_RE.fullmatch(sig):
            raise ProxyError("auth", "auth_sig_format")

        read_timeout_raw = _single(request, signing.READ_TIMEOUT_HEADER)
        if read_timeout_raw is None:
            read_timeout = settings.default_read_timeout
        elif _READ_TIMEOUT_RE.fullmatch(read_timeout_raw):
            read_timeout = float(read_timeout_raw)
        else:
            raise ProxyError("bad_request", "read_timeout")
        # Everything from here on — body, replay record, pre-checks and the
        # upstream call — ends by the caller's read timeout.
        deadline = ctx.started + read_timeout

        body = await self._read_body(request)
        message = signing.string_to_sign(method, path, query, body, ts, nonce, kid, identity, client_ip)
        if not signing.signature_matches(secret, message, sig):
            raise ProxyError("auth", "auth_sig")
        # Only a conservative error hint, not an accepted audit identity.
        # A shared replay outage must not classify a prior delivery as unsent.
        if method == "POST" and path == "/api/user/manage" and identity == "ap" and key_class == "cron":
            hint = _json_or_none(body)
            ctx.correlation_requested = isinstance(hint, dict) and policy.CONVERSION_ATTEMPT_FIELD in hint
        await self._claim_nonce(kid, nonce)

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
            logs.audit("bg", attempt_id=ctx.attempt_id, kid=kid, ip=ip, method=method, path=path, active=active)
            if not active:
                raise ProxyError("policy", "break_glass_off")
            # Every use of break-glass is an alert (DESIGN §1.4.2-2): the owner opened it.
            logs.event("break_glass_used", logging.WARNING, kid=kid, method=method, path=path)

        matched = routetable.match(method, path)
        if matched is None or identity not in matched.route.identities:
            raise ProxyError("policy", "route")
        route = matched.route
        ctx.route = route.name
        ctx.audit = "bg" if identity == "bg" else route.audit
        if route.name in {"ap.user.manage", "ap.user.get"}:
            ctx.ap_priority = "conversion"
        elif route.name == "ap.user.list":
            ctx.ap_priority = "balance"
        elif route.name == "ap.log.list" and dict(pairs).get("group") == "free" and dict(pairs).get("type") == "2":
            ctx.ap_priority = "free_meter"
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

        # body policy
        validated: dict | None = None
        if route.body == "none":
            if body:
                raise ProxyError("bad_request", "body_not_allowed")
            upstream_body = b""
        else:
            obj = parse_json_object(body)
            pctx = policy.PolicyContext(settings, now, identity=identity, query=dict(pairs))
            try:
                validated = route.body_policy(obj, pctx)
            except policy.PolicyDenied as err:
                # e.g. an add_quota "override" attempt: worth an audit line that
                # says against whom and for how much (only exact int fields).
                fields: dict[str, Any] = {"kid": kid, "ip": ip}
                if ctx.audit is not None:
                    target, value = _int_field(obj, "id"), _int_field(obj, "value")
                    if target is not None:
                        fields["target_user_id"] = target
                    if value is not None:
                        fields["value"] = value
                self._audit(ctx, fields, result="denied", reason=err.reason)
                raise ProxyError("policy", err.reason) from None
            if (route.name == "ap.user.manage" and ctx.identity == "ap"
                    and policy.CONVERSION_ATTEMPT_FIELD in obj):
                # HMAC, CRON scope and the full body policy have already passed.
                # validated is rebuilt without metadata: never forward it upstream.
                ctx.attempt_id = obj[policy.CONVERSION_ATTEMPT_FIELD]
                ctx.correlated = True
                if self.control is None:
                    raise ProxyError("attempt_unknown", "begin_unconfirmed")
            upstream_body = json.dumps(validated, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(
                "ascii"
            )

        if not self.inflight.try_acquire(identity):
            raise ProxyError("busy", "inflight")
        try:
            if route.special == "control_read":
                return await self._read_control_audit(pairs, deadline)
            return await self._forward(
                ctx, matched, method, query, ip, upstream_auth, proof, upstream_body, validated, deadline
            )
        finally:
            self.inflight.release(identity)

    async def _read_control_audit(self, pairs, deadline):
        if self.control is None:
            raise ProxyError("config", "control_store_disabled")
        params = dict(pairs)
        if "after" not in params:
            raise ProxyError("bad_request", "query_key")
        after = int(params["after"])
        through = int(params["through"]) if "through" in params else None
        limit = int(params.get("limit", "100"))
        if (after > 9223372036854775807
                or (through is not None and (through < after or through > 9223372036854775807))):
            raise ProxyError("bad_request", "query_value")
        try:
            result = await self._control_call(deadline, "read", after, through, limit)
        except ControlStoreError:
            raise ProxyError("busy", "control_store") from None
        return _json_response({"success": True, "data": result})

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
        deadline: float,
    ) -> Response:
        route = matched.route
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
            relayed = await self._reveal_precheck(ctx, token_id, ip, upstream_auth, deadline, audit_fields)
            if relayed is not None:
                return relayed
        elif route.special == "manage" and ctx.identity == "bg":
            assert validated is not None
            audit_fields.update(op="manage_subtract", target_user_id=validated["id"], value=validated["value"])
            _user, relayed = await self._target_precheck(
                ctx, validated["id"], ip, deadline, audit_fields, policy.PAAS_USER_GROUPS
            )
            if relayed is not None:
                return relayed
        elif route.special == "manage":
            assert validated is not None
            audit_fields.update(target_user_id=validated["id"], value=validated["value"])
            reservation, relayed = await self._manage_precheck(ctx, validated, ip, deadline, audit_fields)
            if relayed is not None:
                if ctx.correlated:
                    raise ProxyError("attempt_unknown", "begin_unconfirmed")
                return relayed
        elif route.special == "user_group":
            assert validated is not None
            audit_fields.update(op="user_group", target_user_id=validated["id"], group=validated["group"])
            user, relayed = await self._target_precheck(
                ctx, validated["id"], ip, deadline, audit_fields, policy.PAAS_USER_GROUPS
            )
            if relayed is not None:
                return relayed
            update = policy.bg_user_group_update(
                user, validated, policy.PolicyContext(self.settings, self._now(), identity=ctx.identity)
            )
            if update is None:
                self._audit(ctx, audit_fields, result="denied", reason="manage_target")
                raise ProxyError("denied", "manage_target")
            # new-api overwrites every user field it is sent: send back what it just said,
            # with only the group changed.
            upstream_body = json.dumps(update, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode(
                "ascii"
            )

        intent = self.control is not None and ctx.audit == "add_quota"
        try:
            if intent:
                await self._control_call(deadline, "begin", ctx.attempt_id, validated["id"], validated["value"])
            await self._ap_admit(ctx, deadline, upstream_auth)
        except (ControlStoreError, ProxyError) as err:
            if reservation is not None:
                self.budget.release(reservation)
            self._audit(ctx, audit_fields, result="not_sent", reason="control_store")
            # If intent COMMIT was ambiguous, there is deliberately no attempt
            # to infer it was absent. The possibly stored intent stays pending.
            if intent and isinstance(err, ProxyError):
                await self._finish_intent(ctx, deadline, "not_sent")
            if isinstance(err, ProxyError):
                raise
            raise ProxyError("busy", "control_store") from None

        # Leave a bounded tail for recording the outcome within the same
        # incoming request deadline (no detached background audit writes).
        budget = self._remaining(deadline) - (0.6 if intent else 0)
        if budget < MIN_SEND_BUDGET:
            if reservation is not None:
                self.budget.release(reservation)
            self._audit(ctx, audit_fields, result="not_sent", reason="deadline")
            if intent:
                await self._finish_intent(ctx, deadline, "not_sent")
            raise ProxyError("connect", "deadline")
        # Written *before* the call: if this pod dies mid-call (OOM, SIGKILL,
        # shutdown timeout) the reveal / add_quota / bg call is still on record
        # as possibly executed — the case reconciliation needs evidence for.
        self._audit(ctx, audit_fields, result="sending")
        try:
            resp = await self.upstream.send(method, url, headers, upstream_body, budget=budget, write=write)
        except UpstreamError as err:
            if reservation is not None and err.kind == CONNECT:
                self.budget.release(reservation)
            self.metrics.inc("newapi_proxy_upstream_errors_total", kind=err.kind, exc=err.exc_type)
            self._audit(ctx, audit_fields, result=err.kind)
            if intent:
                await self._finish_intent(ctx, deadline, err.kind)
            raise ProxyError(err.kind, err.detail or f"upstream_{err.kind}") from None
        except asyncio.CancelledError:
            self._audit(ctx, audit_fields, result="cancelled")
            # Durable intent already exists. Avoid delaying cancellation with
            # a second database request; absent outcome is explicitly unknown.
            raise
        ctx.upstream_status = resp.status
        if route.response is not None:
            try:
                resp = UpstreamResponse(resp.status, resp.headers, route.response(resp.status, resp.body, self.settings))
            except responses.ResponseShapeError as err:
                # Fail closed: not one byte of an answer we cannot vet goes back.
                logs.event("response_shape", logging.WARNING, route=route.name, reason=err.reason, status=resp.status)
                raise ProxyError("internal", err.reason) from None
        response = self._relay(resp, ctx)
        outcome: dict[str, Any] = {"result": "forwarded", "status": resp.status, "request_id": ctx.request_id}
        if reservation is not None:
            if proves_not_executed(resp.status, resp.body):
                # new-api's answer shows nothing was credited: give it back.
                self.budget.release(reservation)
                outcome["budget"] = "released"
            else:
                outcome["budget"] = "kept"
            answer = _json_or_none(resp.body)
            if resp.status == 200 and isinstance(answer, dict) and answer.get("success") is True:
                self.metrics.inc("newapi_proxy_add_quota_value_total", validated["value"])
        self._audit(ctx, audit_fields, **outcome)
        if intent:
            await self._finish_intent(ctx, deadline, "forwarded", resp.status,
                                      None if ctx.request_id == "-" else ctx.request_id)
        return response

    _AUDIT_METRICS = {
        "reveal": "newapi_proxy_reveal_total",
        "add_quota": "newapi_proxy_add_quota_total",
        "bg": "newapi_proxy_break_glass_calls_total",
    }

    def _audit(self, ctx: Ctx, fields: dict, **outcome: Any) -> None:
        if ctx.audit is None:
            return
        if ctx.correlated and not ctx.not_sent_confirmed and outcome.get("result") == "not_sent":
            outcome["result"] = "uncertain"
        self.metrics.inc(self._AUDIT_METRICS[ctx.audit], result=str(outcome.get("result")))
        logs.audit(ctx.audit, attempt_id=ctx.attempt_id, **fields, **outcome)

    async def _precheck_get(
        self, ctx: Ctx, url: str, ip: str, auth: str, deadline: float, what: str
    ) -> tuple[dict | None, UpstreamResponse | None]:
        """GET that vets a guarded call; the guarded call is sent only on ``(data, None)``.

        * ``(data, None)``: HTTP 200 ``success:true`` — go on with ``data``.
        * ``(None, resp)``: new-api gave a definite refusal (a ``code``, or
          ``success:false`` with a ``message_key``, e.g. the UP / AP is
          revoked): relay it as the answer, so the client classifies it
          exactly as it would the guarded call's own refusal.
        * ``ProxyError("denied", "<what>_not_found")``: ``record not found``.
        * ``ProxyError("connect", …)``: transport failure, 429, 5xx without a
          ``code``, or an answer without a definite meaning (a DB error) —
          nothing was sent, safe to re-send.
        """
        budget = min(PRECHECK_BUDGET, self._remaining(deadline) - MIN_SEND_BUDGET)
        if budget < MIN_SEND_BUDGET:
            raise ProxyError("connect", "deadline")
        headers = self._base_headers(ip, write=False)
        headers["Authorization"] = auth
        await self._ap_admit(ctx, deadline, auth)
        budget = min(budget, self._remaining(deadline) - MIN_SEND_BUDGET)
        if budget < MIN_SEND_BUDGET:
            raise ProxyError("connect", "deadline")
        try:
            resp = await self.upstream.send("GET", url, headers, b"", budget=budget, write=False)
        except UpstreamError as err:
            self.metrics.inc("newapi_proxy_upstream_errors_total", kind=err.kind, exc=err.exc_type)
            raise ProxyError("connect", f"{what}_precheck_{err.kind}") from None
        data = _json_or_none(resp.body)
        refusal = isinstance(data, dict) and data.get("success") is not True
        has_code = refusal and _nonempty_str(data.get("code"))
        if resp.status == 429 or (resp.status >= 500 and not has_code):
            extra = {}
            retry_after = resp.headers.get("retry-after")
            if retry_after and _RETRY_AFTER_RE.fullmatch(retry_after):
                extra["Retry-After"] = retry_after
            raise ProxyError("connect", f"{what}_precheck_http_{resp.status}", headers=extra)
        if has_code or (
            resp.status == 200 and refusal and data.get("success") is False and _nonempty_str(data.get("message_key"))
        ):
            return None, resp
        if resp.status != 200:
            raise ProxyError("connect", f"{what}_precheck_http_{resp.status}")
        if not isinstance(data, dict):
            raise ProxyError("connect", f"{what}_precheck_body")
        if data.get("success") is True:
            return data, None
        if data.get("message") == "record not found":
            raise ProxyError("denied", f"{what}_not_found")
        raise ProxyError("connect", f"{what}_precheck_body")

    def _relay_precheck(self, ctx: Ctx, resp: UpstreamResponse, what: str) -> Response:
        ctx.upstream_status = resp.status
        return self._relay(resp, ctx, extra={PRECHECK_HEADER: what})

    async def _reveal_precheck(self, ctx, token_id, ip, auth, deadline, fields) -> Response | None:
        """Reveal only wp_ keys created within the last 120 s (〔REV〕2)."""
        try:
            data, relay = await self._precheck_get(ctx, f"/api/token/{token_id}", ip, auth, deadline, "reveal")
        except ProxyError as err:
            self._audit(ctx, fields, result="denied" if err.kind == "denied" else "not_sent", reason=err.reason)
            raise
        if relay is not None:
            self._audit(ctx, fields, result="precheck_relayed", status=relay.status)
            return self._relay_precheck(ctx, relay, "reveal")
        denial = policy.reveal_denial(data.get("data"), token_id, self._now())
        if denial is not None:
            self._audit(ctx, fields, result="denied", reason=denial)
            raise ProxyError("denied", denial)
        return None

    async def _target_precheck(
        self, ctx: Ctx, target_id: int, ip: str, deadline: float, fields: dict, groups: frozenset
    ) -> tuple[dict | None, Response | None]:
        """AP ``GET /api/user/{id}``: the target must be a PaaS user (prefix, role 1, ``groups``).

        Returns ``(user, None)`` to go on, or ``(None, relayed)`` when new-api's own
        refusal is relayed. Raises 403 ``denied`` (``manage_target``／``manage_not_found``)
        or 502 ``connect`` — the guarded call was not sent in either case.
        """
        try:
            data, relay = await self._precheck_get(
                ctx, f"/api/user/{target_id}", ip, f"Bearer {self.settings.ap_token}", deadline, "manage"
            )
        except ProxyError as err:
            self._audit(ctx, fields, result="denied" if err.kind == "denied" else "not_sent", reason=err.reason)
            raise
        if relay is not None:
            self._audit(ctx, fields, result="precheck_relayed", status=relay.status)
            return None, self._relay_precheck(ctx, relay, "manage")
        user = data.get("data")
        pctx = policy.PolicyContext(self.settings, self._now(), identity=ctx.identity)
        if not policy.manage_target_ok(user, target_id, pctx, groups=groups):
            self._audit(ctx, fields, result="denied", reason="manage_target")
            raise ProxyError("denied", "manage_target")
        return user, None

    async def _manage_precheck(self, ctx, validated: dict, ip: str, deadline: float, fields: dict):
        """Hourly budget, then the target must be a PaaS user (role 1, default)."""
        reservation = self.budget.reserve(validated["value"], self.monotonic())
        if reservation is None:
            self._audit(ctx, fields, result="denied", reason="hourly_cap")
            logs.event("add_quota_budget_exhausted", logging.WARNING, cap=self.budget.cap)
            raise ProxyError("budget", "hourly_cap")
        try:
            _user, relayed = await self._target_precheck(
                ctx, validated["id"], ip, deadline, fields, frozenset({"default"})
            )
        except ProxyError:
            self.budget.release(reservation)
            raise
        if relayed is not None:
            self.budget.release(reservation)
            return None, relayed
        return reservation, None

    def _relay(self, resp: UpstreamResponse, ctx: Ctx, extra: dict[str, str] | None = None) -> Response:
        headers = {"Cache-Control": "no-store", **(extra or {})}
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

    # --- admin port: health, readiness, metrics -------------------------------------

    async def admin(self, request: Request) -> Response:
        raw_path = request.scope.get("raw_path")
        if request.method != "GET" or request.scope.get("query_string"):
            return _json_response({"ok": False, "error": "method"}, 405)
        if raw_path == b"/healthz":
            return _json_response({"ok": True})
        if raw_path == b"/metrics":
            return Response(self.metrics.render(), media_type="text/plain; version=0.0.4")
        if raw_path == b"/readyz":
            return await self._readyz()
        return _json_response({"ok": False, "error": "not_found"}, 404)

    async def _check_upstream(self) -> str:
        try:
            resp = await self.upstream.send(
                "GET",
                "/api/status",
                self._base_headers(READINESS_IP, write=False),
                b"",
                budget=READY_UPSTREAM_BUDGET,
                write=False,
            )
        except UpstreamError as err:
            return err.kind
        if resp.status == 429:
            # new-api answered: reachable, only a rate-limit bucket is full.
            return "rate_limited"
        data = _json_or_none(resp.body)
        if resp.status == 200 and isinstance(data, dict) and data.get("success") is True:
            return "ok"
        return f"http_{resp.status}"

    async def _check_replay_store(self) -> str:
        if self.shared_replay is None:
            return "local"
        return "ok" if await self.shared_replay.ping() else "unavailable"

    async def _readiness_check(self) -> dict:
        async def control_health():
            if self.control is None:
                return "disabled"
            try:
                async with asyncio.timeout(0.5):
                    return "ok" if await self.control.ping() else "unavailable"
            except (ControlStoreError, TimeoutError):
                return "unavailable"
        upstream, store, control = await asyncio.gather(
            self._check_upstream(), self._check_replay_store(), control_health())
        ready = upstream in ("ok", "rate_limited") and store in ("ok", "local") and control != "unavailable"
        self.metrics.inc("newapi_proxy_readiness_checks_total", result="ready" if ready else "not_ready")
        return {"ready": ready, "upstream": upstream, "replay_store": store, "control_store": control}

    async def _readyz(self) -> Response:
        check = await self.readiness.get()
        now = self._now()
        body = {
            **check,
            "ap_configured": self.settings.ap_token is not None,
            "op_configured": self.settings.op_token is not None,
            "ap_days_left": _days_left(self.settings.ap_expires_at, now),
            "op_days_left": _days_left(self.settings.op_expires_at, now),
            "break_glass": self.break_glass_active(now),
            "version": __version__,
        }
        return _json_response(body, 200 if check["ready"] else 503)


class ProxyApp:
    """Plain ASGI entry for the proxy port: every HTTP request goes to ``Proxy.endpoint``.

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
                    await self.proxy.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        # websocket and anything else: refuse by not accepting.


class AdminApp:
    """ASGI entry for the admin port: /healthz, /readyz, /metrics only."""

    def __init__(self, proxy: Proxy):
        self.proxy = proxy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            response = await self.proxy.admin(Request(scope, receive))
            await response(scope, receive, send)
        elif scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return


def create_app(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    replay_store: Any = None,
    control_store: Any = None,
) -> ProxyApp:
    return ProxyApp(Proxy(settings, transport=transport, clock=clock, monotonic=monotonic,
                          replay_store=replay_store, control_store=control_store))
