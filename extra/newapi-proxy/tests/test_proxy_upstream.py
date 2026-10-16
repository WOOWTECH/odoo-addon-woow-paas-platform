"""Error classification connect / reset / timeout, total deadline, no retries,
in-flight caps, body deadline, admin port (health, readiness, metrics)."""

import asyncio
import json
import logging
import time

import httpx
import pytest

from newapi_proxy import app as app_module
from newapi_proxy.metrics import Metrics, format_value
from newapi_proxy.upstream import classify
from proxy_harness import NOW, TENANT_IP, USER_PAT, Harness, asgi_call, ok, presend

LEAKY = "nap_LeakedInExceptionText0000111122223333444455"


def raiser(exc_type, *args):
    def handler(request):
        raise exc_type(f"boom {LEAKY} Authorization: Bearer {LEAKY}", request=request)

    return handler


@pytest.mark.parametrize(
    "exc, status, kind",
    [
        (httpx.ConnectError, 502, "connect"),
        (httpx.ConnectTimeout, 502, "connect"),
        (httpx.PoolTimeout, 502, "connect"),
        (httpx.ReadTimeout, 504, "timeout"),
        (httpx.WriteTimeout, 504, "timeout"),
        (httpx.RemoteProtocolError, 502, "reset"),
        (httpx.ReadError, 502, "reset"),
        (httpx.WriteError, 502, "reset"),
        (httpx.LocalProtocolError, 502, "reset"),
    ],
)
def test_transport_errors_classified_without_text(caplog, exc, status, kind):
    caplog.set_level(logging.DEBUG)
    h = Harness()
    h.upstream.on("GET", "/api/token/5", raiser(exc))
    res = h.user("GET", "/api/token/5")
    assert res.status == status
    assert res.proxy_error == kind
    assert LEAKY.encode() not in res.body
    assert b"boom" not in res.body
    assert LEAKY not in caplog.text
    assert "boom" not in caplog.text
    # never retried
    assert len(h.upstream.requests) == 1


def test_classify_unknown_exception_is_reset():
    assert classify(RuntimeError("x")) == "reset"


def test_errors_in_the_body_stream_are_reset_not_connect():
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"succ'
            raise httpx.ReadError("connection reset by peer")

    h = Harness()
    h.upstream.on("GET", "/api/token/5", lambda req: httpx.Response(200, stream=BrokenStream()))
    res = h.user("GET", "/api/token/5")
    assert res.status == 502
    assert res.proxy_error == "reset"


def test_oversized_upstream_response_is_reset():
    h = Harness()
    assert h.settings.max_response_bytes == 1024 * 1024
    h.proxy.upstream.max_response_bytes = 1024
    h.upstream.on("GET", "/api/token/5", httpx.Response(200, content=b"x" * 4096))
    res = h.user("GET", "/api/token/5")
    assert res.proxy_error == "reset"


def test_upstream_4xx_5xx_are_passed_through_not_retried():
    h = Harness()
    h.upstream.on("POST", "/api/user/login", httpx.Response(500, content=b""))
    res = h.user("POST", "/api/user/login", auth=None, body={"username": "wsab0000000042", "password": "P" * 40})
    assert res.status == 500
    assert res.proxy_error is None
    assert len(h.upstream.requests) == 1


# --- total deadline (X-Woow-Read-Timeout bounds the whole request) ---------------------


def realtime_harness(**env):
    return Harness(monotonic=time.monotonic, **env)


def test_trickling_response_cannot_outlive_the_read_timeout():
    """httpx read timeouts are per socket read: a body trickled in slowly used
    to keep the call alive far past X-Woow-Read-Timeout."""

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                await asyncio.sleep(0.2)
                yield b" "

    h = realtime_harness()
    h.upstream.on("GET", "/api/token/5", lambda req: httpx.Response(200, stream=Trickle()))
    started = time.monotonic()
    res = h.user("GET", "/api/token/5", headers=[("X-Woow-Read-Timeout", "1")])
    elapsed = time.monotonic() - started
    assert res.status == 504
    assert res.proxy_error == "timeout"  # the request went out: outcome unknown
    assert elapsed < 1.5


def test_stuck_before_sending_is_connect_not_timeout():
    async def stuck(request):
        await asyncio.sleep(10)

    h = realtime_harness()
    h.upstream.on("GET", "/api/token/5", presend(stuck))
    started = time.monotonic()
    res = h.user("GET", "/api/token/5", headers=[("X-Woow-Read-Timeout", "1")])
    assert time.monotonic() - started < 1.5
    assert res.status == 502
    assert res.proxy_error == "connect"  # nothing went out: safe to re-send
    assert h.upstream.requests == []


def test_slow_precheck_leaves_no_time_and_the_reveal_is_not_sent():
    async def slow_token(request):
        await asyncio.sleep(0.8)
        return ok({"id": 42, "name": "wp_7_ab12", "created_time": NOW})

    h = realtime_harness()
    h.upstream.on("GET", "/api/token/42", slow_token)
    started = time.monotonic()
    res = h.user("POST", "/api/token/42/key", headers=[("X-Woow-Read-Timeout", "1")])
    assert time.monotonic() - started < 1.5
    assert res.status == 502
    assert res.proxy_error == "connect"
    assert h.upstream.calls("POST") == []


def test_connect_timeout_never_exceeds_the_remaining_budget():
    seen = {}

    def capture(request):
        seen.update(request.extensions["timeout"])
        return ok()

    h = Harness()
    h.upstream.on("GET", "/api/token/5", capture)
    h.user("GET", "/api/token/5", headers=[("X-Woow-Read-Timeout", "1")])
    assert seen["connect"] <= 1 and seen["pool"] <= 1 and seen["read"] <= 1


# --- in-flight, body deadline -------------------------------------------------------


def test_inflight_cap_returns_busy():
    h = Harness(PROXY_INFLIGHT_USER="2")
    gate = asyncio.Event()
    started = []

    async def slow(request):
        started.append(request)
        await gate.wait()
        return ok()

    for n in (1, 2, 3):
        h.upstream.on("GET", f"/api/token/{n}", slow)

    async def scenario():
        reqs = []
        for n in range(3):
            headers = h.sign_headers("GET", f"/api/token/{n + 1}", identity="user", client_ip="198.18.17.9")
            headers.append(("Authorization", "Bearer nap_" + "u" * 43))
            reqs.append(asyncio.create_task(h.acall("GET", f"/api/token/{n + 1}".encode(), b"", headers)))
            await asyncio.sleep(0)
        while len(started) < 2:
            await asyncio.sleep(0)
        # third is refused at once, without touching upstream
        third = await asyncio.wait_for(reqs[2], timeout=2)
        gate.set()
        first, second = await asyncio.gather(reqs[0], reqs[1])
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert (first.status, second.status) == (200, 200)
    assert third.status == 503
    assert third.proxy_error == "busy"
    assert len(started) == 2
    assert h.proxy.inflight.current["user"] == 0


def test_slow_body_is_cut_off(monkeypatch):
    """Only a known kid and a fresh ts are checked before the body: a sender
    that never finishes must not hold the connection."""
    monkeypatch.setattr(app_module, "BODY_TIMEOUT", 0.2)
    h = realtime_harness()
    body = b'{"name":"wp_1_abcd"}'
    headers = h.sign_headers("POST", "/api/token/", body=body, identity="user", client_ip=TENANT_IP)
    headers += [("Authorization", f"Bearer {USER_PAT}"), ("Content-Length", str(len(body)))]

    async def dribble():
        await asyncio.sleep(0)
        if not hasattr(dribble, "sent"):
            dribble.sent = True
            return {"type": "http.request", "body": body[:3], "more_body": True}
        await asyncio.sleep(30)

    started = time.monotonic()
    res = h.call("POST", b"/api/token/", b"", headers, receive=dribble)
    assert time.monotonic() - started < 1
    assert res.status == 408
    assert res.proxy_error == "body_timeout"
    assert h.upstream.requests == []


# --- admin port -------------------------------------------------------------------


def test_healthz_does_not_touch_upstream():
    h = Harness()
    res = h.admin(b"/healthz")
    assert res.status == 200
    assert h.upstream.requests == []
    assert h.admin(b"/healthz", method="POST").status == 405
    assert h.admin(b"/healthz", query=b"x=1").status == 405
    assert h.admin(b"/%68ealthz").status == 404
    assert h.admin(b"/api/status").status == 404


def test_proxy_port_serves_no_unauthenticated_paths():
    h = Harness()
    for path in (b"/healthz", b"/readyz", b"/metrics"):
        res = h.call("GET", path)
        assert res.status == 401, path
    assert h.upstream.requests == []


def test_readyz_checks_upstream_and_reports_days_left():
    h = Harness(PROXY_OP_EXPIRES_AT=str(NOW + 13 * 86400 + 5))
    h.upstream.on("GET", "/api/status", ok({"quota_per_unit": 500000}))
    res = h.admin(b"/readyz")
    assert res.status == 200
    body = res.json
    assert body["ready"] is True and body["upstream"] == "ok" and body["replay_store"] == "ok"
    assert body["ap_days_left"] == 100
    assert body["op_days_left"] == 13
    assert body["break_glass"] is False
    req = h.upstream.requests[0]
    # its own synthetic IP: never anon's (198.18.0.2) GA rate-limit bucket
    assert req.headers["x-forwarded-for"] == "198.18.0.5"
    assert "authorization" not in req.headers
    # no token values anywhere in the answer
    assert b"nap_" not in res.body


def test_readyz_is_single_flight_and_cached():
    """Finding: 40 concurrent /readyz used to make 40 upstream calls, and ~360
    of them could fill a GA bucket and take every replica out of the Service."""
    h = Harness()
    gate = asyncio.Event()

    async def status(request):
        await gate.wait()
        return ok()

    h.upstream.on("GET", "/api/status", status)

    async def burst(n):
        tasks = [asyncio.create_task(asgi_call(h.admin_app, "GET", b"/readyz", b"", [], b"")) for _ in range(n)]
        await asyncio.sleep(0.05)
        gate.set()
        return await asyncio.gather(*tasks)

    answers = asyncio.run(burst(40))
    assert {r.status for r in answers} == {200}
    assert len(h.upstream.requests) == 1
    # cached for READY_OK_TTL seconds, then checked again
    assert h.admin(b"/readyz").status == 200
    assert len(h.upstream.requests) == 1
    h.mono += app_module.READY_OK_TTL + 0.1
    assert h.admin(b"/readyz").status == 200
    assert len(h.upstream.requests) == 2
    assert h.proxy.metrics.get("newapi_proxy_readiness_checks_total", result="ready") == 2


def test_readyz_failure_is_cached_briefly():
    h = Harness()
    h.upstream.on("GET", "/api/status", raiser(httpx.ConnectError))
    assert h.admin(b"/readyz").status == 503
    assert h.admin(b"/readyz").status == 503
    assert len(h.upstream.requests) == 1
    h.mono += app_module.READY_FAIL_TTL + 0.1
    h.upstream.on("GET", "/api/status", ok())
    assert h.admin(b"/readyz").status == 200


def test_readyz_treats_rate_limited_upstream_as_reachable():
    h = Harness()
    h.upstream.on("GET", "/api/status", httpx.Response(429, headers={"Retry-After": "60"}))
    res = h.admin(b"/readyz")
    assert res.status == 200
    assert res.json["upstream"] == "rate_limited"


@pytest.mark.parametrize(
    "reply, state",
    [
        (httpx.Response(500), "http_500"),
        (httpx.Response(200, json={"success": False}), "http_200"),
        (httpx.Response(200, content=b"<html>"), "http_200"),
    ],
)
def test_readyz_not_ready(reply, state):
    h = Harness()
    h.upstream.on("GET", "/api/status", reply)
    res = h.admin(b"/readyz")
    assert res.status == 503
    assert res.json["upstream"] == state


def test_readyz_connect_failure():
    h = Harness()
    h.upstream.on("GET", "/api/status", raiser(httpx.ConnectError))
    res = h.admin(b"/readyz")
    assert res.status == 503
    assert res.json["upstream"] == "connect"


def test_readyz_needs_the_shared_replay_store():
    h = Harness()
    h.upstream.on("GET", "/api/status", ok())
    h.replay_store.ping_ok = False
    res = h.admin(b"/readyz")
    assert res.status == 503
    assert res.json["replay_store"] == "unavailable"


def test_readyz_single_replica_is_local():
    h = Harness(PROXY_REPLICAS="1", PROXY_VALKEY_ADDR="", PROXY_VALKEY_PASSWORD="")
    h.upstream.on("GET", "/api/status", ok())
    assert h.admin(b"/readyz").json["replay_store"] == "local"


def test_metrics_exposed():
    h = Harness()
    h.signed("GET", "/api/status")
    h.signed("GET", "/v1/models", identity="anon")
    text = h.admin(b"/metrics").body.decode()
    assert 'newapi_proxy_requests_total{code="2xx",identity="anon",route="anon.status"} 1' in text
    assert 'newapi_proxy_rejections_total{kind="policy",reason="route"} 1' in text
    assert 'newapi_proxy_inflight{identity="user"} 0' in text


def test_metric_values_are_exact():
    """Quota sums reach 1e8+; ``:g`` printed 123456789 as 1.23457e+08."""
    m = Metrics()
    m.inc("newapi_proxy_add_quota_value_total", 123_456_789)
    m.inc("newapi_proxy_add_quota_value_total", 156_250_000)
    m.gauge("newapi_proxy_add_quota_budget_used", lambda: {(): 1_562_500_000})
    text = m.render()
    assert "newapi_proxy_add_quota_value_total 279706789\n" in text
    assert "newapi_proxy_add_quota_budget_used 1562500000\n" in text
    assert format_value(0.25) == "0.25"
    assert format_value(3.0) == "3"
    assert format_value(True) == "1"


def test_error_body_shape():
    h = Harness()
    res = h.signed("GET", "/api/channel/", identity="ap")
    assert json.loads(res.body) == {
        "success": False,
        "message": "newapi-proxy: policy",
        "proxy_error": "policy",
        "reason": "route",
    }
    assert res.headers["x-woow-proxy-error"] == "policy"
    assert res.headers["cache-control"] == "no-store"
