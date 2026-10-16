"""Test harness: a fake new-api upstream, a fake shared replay store and a raw ASGI caller.

Requests are driven straight into the ASGI app with a hand-built scope so the
tests control ``raw_path`` byte for byte (an HTTP client would normalise
``..`` or percent-escapes before the proxy ever saw them). The upstream is an
``httpx.MockTransport``: no socket is opened. The shared replay store is an
in-memory stand-in for Valkey that several harnesses ("replicas") can share.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import unquote

import httpx

from newapi_proxy import signing
from newapi_proxy.app import AdminApp, create_app
from newapi_proxy.replaystore import ReplayStoreUnavailable
from newapi_proxy.settings import load_settings

NOW = 1_800_000_000

# Fake key material. Distinct, long, and greppable in the log-redaction tests.
WEB_KID = "web-2026a"
WEB_SECRET = "WebSecretAaaa0000Bbbb1111Cccc2222Dddd3333"
WEB_NEXT_KID = "web-2026b"
WEB_NEXT_SECRET = "WebSecretNext4444Eeee5555Ffff6666Gggg7777"
CRON_KID = "cron-2026a"
CRON_SECRET = "CronSecretHhhh8888Iiii9999Jjjj0000Kkkk1111"
AP_TOKEN = "nap_" + "ApTokenLLLL2222MMMM3333NNNN4444OOOO5555PPP"
OP_TOKEN = "nap_" + "OpTokenQQQQ6666RRRR7777SSSS8888TTTT9999UUU"
USER_PAT = "nap_" + "UserPatVVVV0000WWWW1111XXXX2222YYYY3333ZZZ"
USER_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMiLCJ0dSI6ImFjY2VzcyJ9.c2lnbmF0dXJlLXZhbHVlLWZvci10ZXN0cw"
PROOF = "eyJhbGciOiJIUzI1NiJ9.eyJ0dSI6InByb29mIiwic2NwIjpbImFjY2Vzc190b2tlbi5nZW5lcmF0ZSJdfQ.cHJvb2Ytc2lnbmF0dXJl"
VALKEY_PASSWORD = "ValkeyProxyPw5555Vvvv6666Wwww7777"
TENANT_IP = "198.18.17.5"
USERNAME = "wsab0000000042"
PASSWORD = "Pw0123456789abcdefghijABCDEFGHIJklmnopqrstuvwxyz0123456789ABCDEF"
REVEALED_KEY = "RevealedKey48charsAbcdefghijklmnopqrstuvwxyz0123"


def base_env(**overrides: str) -> dict[str, str]:
    env = {
        "PROXY_UPSTREAM_URL": "http://newapi:3000",
        "PROXY_AUTH_WEB": f"{WEB_KID}:{WEB_SECRET},{WEB_NEXT_KID}:{WEB_NEXT_SECRET}",
        "PROXY_AUTH_CRON": f"{CRON_KID}:{CRON_SECRET}",
        "PROXY_AP_TOKEN": AP_TOKEN,
        "PROXY_OP_TOKEN": OP_TOKEN,
        "PROXY_USERNAME_REGEX": r"^ws[a-z]{2}\d{10}$",
        "PROXY_TENANT_IP_RANGE": "198.18.17.0-198.19.255.255",
        "PROXY_ADD_QUOTA_HOURLY_CAP_POINTS": "100000",
        "PROXY_REPLICAS": "2",
        "PROXY_VALKEY_ADDR": "newapi-valkey:6379",
        "PROXY_VALKEY_PASSWORD": VALKEY_PASSWORD,
        "PROXY_AP_EXPIRES_AT": str(NOW + 100 * 86400 + 3600),
        "PROXY_OP_EXPIRES_AT": "",
    }
    env.update(overrides)
    return env


KEYS = {
    "web": (WEB_KID, WEB_SECRET.encode()),
    "web_next": (WEB_NEXT_KID, WEB_NEXT_SECRET.encode()),
    "cron": (CRON_KID, CRON_SECRET.encode()),
}


def ok(data=None, request_id: str = "req-1", headers: dict | None = None) -> httpx.Response:
    hdrs = {"X-Oneapi-Request-Id": request_id, "Content-Type": "application/json; charset=utf-8"}
    hdrs.update(headers or {})
    return httpx.Response(
        200, json={"success": True, "message": "", "data": data if data is not None else {}}, headers=hdrs
    )


class FakeReplayStore:
    """In-memory stand-in for the Valkey replay record (SET NX EX semantics)."""

    def __init__(self):
        self.keys: dict[str, float] = {}
        self.now = 0.0
        self.fail: str | None = None  # a ReplayStoreUnavailable kind to raise
        self.ping_ok = True
        self.claims: list[tuple[str, int]] = []

    async def claim(self, key: str, ttl: int) -> bool:
        if self.fail:
            raise ReplayStoreUnavailable(self.fail)
        self.claims.append((key, ttl))
        expires = self.keys.get(key)
        if expires is not None and expires > self.now:
            return False
        self.keys[key] = self.now + ttl
        return True

    async def ping(self) -> bool:
        return self.ping_ok and not self.fail

    async def aclose(self) -> None:
        pass


def presend(handler):
    """Mark a fake-upstream handler as running *before* the request goes out
    (connect phase): it runs before the "send_request_headers" trace event."""
    handler.presend = True
    return handler


class FakeUpstream:
    """Programmable stand-in for new-api that records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}
        self.transport = httpx.MockTransport(self._handle)

    def on(self, method: str, path: str, reply) -> None:
        self.routes[(method, path)] = reply if callable(reply) else (lambda _req, r=reply: r)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        handler = self.routes.get((request.method, request.url.path))
        if handler is not None and getattr(handler, "presend", False):
            result = handler(request)
            return await result if inspect.isawaitable(result) else result
        # Like httpcore: headers start going out, then the server answers.
        trace = request.extensions.get("trace")
        if trace is not None:
            await trace("http11.send_request_headers.started", {"request": request})
        self.requests.append(request)
        if handler is None:
            return ok()
        result = handler(request)
        return await result if inspect.isawaitable(result) else result

    def calls(self, method: str | None = None, path: str | None = None) -> list[httpx.Request]:
        return [
            r for r in self.requests if (method is None or r.method == method) and (path is None or r.url.path == path)
        ]


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self):
        return json.loads(self.body)

    @property
    def proxy_error(self) -> str | None:
        return self.headers.get("x-woow-proxy-error")


async def asgi_call(app, method: str, raw_path: bytes, query: bytes, headers, body: bytes, receive=None) -> Result:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw_path.decode("latin-1")),
        "raw_path": raw_path,
        "query_string": query,
        "root_path": "",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "client": ("10.42.3.4", 40000),
        "server": ("newapi-proxy", 8080),
    }
    done = asyncio.Event()
    sent_body = False
    start: dict = {}
    chunks: list[bytes] = []

    async def default_receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                done.set()

    await app(scope, receive or default_receive, send)
    headers_out = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start.get("headers", [])}
    return Result(start["status"], headers_out, b"".join(chunks))


class Harness:
    def __init__(self, now: int = NOW, replay_store: FakeReplayStore | None = None, monotonic=None, **env_overrides):
        self.now = now
        self.mono = 10_000.0
        self.upstream = FakeUpstream()
        self.settings, self.warnings = load_settings(base_env(**env_overrides), now)
        if replay_store is None and self.settings.valkey_host is not None:
            replay_store = FakeReplayStore()
        self.replay_store = replay_store
        self.app = create_app(
            self.settings,
            transport=self.upstream.transport,
            clock=lambda: self.now,
            monotonic=monotonic or (lambda: self.mono),
            replay_store=replay_store,
        )
        self.proxy = self.app.proxy
        self.admin_app = AdminApp(self.proxy)
        #: Every header list sent through ``acall`` (signature values included).
        self.sent: list[list[tuple[str, str]]] = []

    # --- raw ASGI ----------------------------------------------------------------

    async def acall(
        self,
        method: str,
        raw_path: bytes,
        query: bytes = b"",
        headers: Iterable[tuple[str, str]] = (),
        body: bytes = b"",
        receive=None,
    ) -> Result:
        headers = list(headers)
        self.sent.append(headers)
        return await asgi_call(self.app, method, raw_path, query, headers, body, receive)

    def call(self, *args, **kwargs) -> Result:
        return asyncio.run(self.acall(*args, **kwargs))

    def admin(self, raw_path: bytes, method: str = "GET", query: bytes = b"") -> Result:
        return asyncio.run(asgi_call(self.admin_app, method, raw_path, query, [], b""))

    # --- signed requests ------------------------------------------------------------

    def sign_headers(
        self,
        method: str,
        path: str,
        query_pairs: Iterable[tuple[str, str]] = (),
        body: bytes = b"",
        identity: str = "anon",
        client_ip: str = "",
        key: str = "cron",
        ts: int | None = None,
        nonce: str | None = None,
    ) -> list[tuple[str, str]]:
        kid, secret = KEYS[key]
        hdrs = signing.signed_headers(
            kid,
            secret,
            method,
            path,
            list(query_pairs),
            body,
            identity,
            client_ip,
            self.now if ts is None else ts,
            nonce=nonce,
        )
        return list(hdrs.items())

    def signed(
        self,
        method: str,
        path: str,
        query_pairs: Iterable[tuple[str, str]] = (),
        body: bytes | dict | None = None,
        identity: str = "anon",
        client_ip: str | None = None,
        key: str = "cron",
        ts: int | None = None,
        headers: Iterable[tuple[str, str]] = (),
        raw_path: bytes | None = None,
        raw_query: bytes | None = None,
        nonce: str | None = None,
    ) -> Result:
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        body = body or b""
        if client_ip is None:
            client_ip = TENANT_IP if identity == "user" else ""
        pairs = list(query_pairs)
        hdrs = self.sign_headers(method, path, pairs, body, identity, client_ip, key, ts, nonce)
        query = raw_query if raw_query is not None else signing.canonical_query(pairs).encode()
        return self.call(method, raw_path or path.encode(), query, hdrs + list(headers), body)

    def user(self, method: str, path: str, auth: str | None = USER_PAT, **kwargs) -> Result:
        headers = list(kwargs.pop("headers", ()))
        if auth is not None:
            headers.append(("Authorization", f"Bearer {auth}"))
        return self.signed(method, path, identity="user", headers=headers, **kwargs)
