"""Error classification connect / reset / timeout, no retries, in-flight caps, health."""

import asyncio
import json
import logging

import httpx
import pytest

from newapi_proxy.upstream import classify
from proxy_harness import NOW, Harness, ok

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


def test_inflight_cap_returns_busy():
    h = Harness(PROXY_INFLIGHT_USER="2")
    gate = asyncio.Event()
    started = []

    async def slow(request):
        started.append(request)
        await gate.wait()
        return ok()

    for n in (1, 2, 3):
        h.upstream.on("GET", f"/api/token/{n}", slow)  # async handler: MockTransport awaits it

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


def test_healthz_does_not_touch_upstream():
    h = Harness()
    res = h.call("GET", b"/healthz")
    assert res.status == 200
    assert h.upstream.requests == []
    assert h.call("POST", b"/healthz").status == 403


def test_readyz_checks_upstream_and_reports_days_left():
    h = Harness(PROXY_OP_EXPIRES_AT=str(NOW + 13 * 86400 + 5))
    h.upstream.on("GET", "/api/status", ok({"quota_per_unit": 500000}))
    res = h.call("GET", b"/readyz")
    assert res.status == 200
    body = res.json
    assert body["ready"] is True and body["upstream"] == "ok"
    assert body["ap_days_left"] == 100
    assert body["op_days_left"] == 13
    assert body["break_glass"] is False
    req = h.upstream.requests[0]
    assert req.headers["x-forwarded-for"] == "198.18.0.2"
    assert "authorization" not in req.headers
    # no token values anywhere in the answer
    assert b"nap_" not in res.body


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
    res = h.call("GET", b"/readyz")
    assert res.status == 503
    assert res.json["upstream"] == state


def test_readyz_connect_failure():
    h = Harness()
    h.upstream.on("GET", "/api/status", raiser(httpx.ConnectError))
    res = h.call("GET", b"/readyz")
    assert res.status == 503
    assert res.json["upstream"] == "connect"


def test_metrics_exposed():
    h = Harness()
    h.signed("GET", "/api/status")
    h.signed("GET", "/v1/models", identity="anon")
    text = h.call("GET", b"/metrics").body.decode()
    assert 'newapi_proxy_requests_total{code="2xx",identity="anon",route="anon.status"} 1' in text
    assert 'newapi_proxy_rejections_total{kind="policy",reason="route"} 1' in text
    assert 'newapi_proxy_inflight{identity="user"} 0' in text


def test_health_paths_reject_query_and_encoded_variants():
    h = Harness()
    assert h.call("GET", b"/healthz", b"x=1").status == 403
    # /%68ealthz is not /healthz: it goes through the proxy pipeline and fails auth
    assert h.call("GET", b"/%68ealthz").status == 400


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
