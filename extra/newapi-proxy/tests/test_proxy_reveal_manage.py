"""Reveal only within 120 s (〔REV〕2); add_quota add-only, target pre-check, hourly cap (〔REV〕1).

Also: how pre-check answers are classified (DESIGN §3.4), when the hourly
budget is given back, and that the audit line exists *before* the call.
"""

import asyncio
import json
import logging

import httpx
import pytest

from newapi_proxy.policy import MANAGE_MAX_VALUE
from newapi_proxy.settings import QUOTA_PER_POINT
from proxy_harness import AP_TOKEN, NOW, REVEALED_KEY, TENANT_IP, USER_PAT, Harness, ok


def audit_lines(caplog, action):
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "audit" and r.msg.get("action") == action
    ]


def results(caplog, action):
    return [line["result"] for line in audit_lines(caplog, action)]


def refusal(status, **body):
    return httpx.Response(status, json={"success": False, **body})


# --- reveal -------------------------------------------------------------------


def token(token_id=42, created=NOW - 5, name="wp_7_ab12"):
    return {"id": token_id, "name": name, "created_time": created, "key": "abcd**********wxyz", "status": 1}


@pytest.fixture
def h():
    harness = Harness()
    harness.upstream.on("POST", "/api/token/42/key", ok({"key": REVEALED_KEY}))
    return harness


@pytest.mark.parametrize("age", [0, 5, 119, 120])
def test_reveal_of_fresh_key_is_forwarded(h, caplog, age):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h.upstream.on("GET", "/api/token/42", ok(token(created=NOW - age)))
    res = h.user("POST", "/api/token/42/key", key="web")
    assert res.status == 200
    assert res.json["data"]["key"] == REVEALED_KEY
    # pre-check used the same UP and the same synthetic IP
    pre, reveal = h.upstream.requests
    assert (pre.method, pre.url.path) == ("GET", "/api/token/42")
    for req in (pre, reveal):
        assert req.headers["authorization"] == f"Bearer {USER_PAT}"
        assert req.headers["x-forwarded-for"] == TENANT_IP
    sending, done = audit_lines(caplog, "reveal")
    assert sending["result"] == "sending" and sending["token_id"] == 42
    assert done["token_id"] == 42 and done["result"] == "forwarded" and done["status"] == 200
    assert done["kid"] == "web-2026a" and done["ip"] == TENANT_IP
    assert h.proxy.metrics.get("newapi_proxy_reveal_total", result="forwarded") == 1


@pytest.mark.parametrize(
    "tok, reason",
    [
        (token(created=NOW - 121), "reveal_window"),
        (token(created=NOW - 180), "reveal_window"),  # "3 minutes ago"
        (token(created=NOW - 86400 * 30), "reveal_window"),
        (token(created=NOW + 60), "reveal_window"),  # from the future
        (token(name="default"), "reveal_name"),  # not a PaaS key
        (token(name="wp_7_AB12"), "reveal_name"),
        (token(token_id=43), "reveal_token_mismatch"),
        ({"id": 42, "name": "wp_7_ab12"}, "reveal_window"),  # no created_time
        ({**token(), "created_time": "1799999999"}, "reveal_window"),
    ],
)
def test_reveal_denied_without_sending_reveal(h, caplog, tok, reason):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h.upstream.on("GET", "/api/token/42", ok(tok))
    res = h.user("POST", "/api/token/42/key")
    assert res.status == 403
    # an expected business outcome for this one key, not a PaaS bug: no circuit
    assert res.proxy_error == "denied"
    assert res.json["reason"] == reason
    assert h.upstream.calls("POST") == []
    assert results(caplog, "reveal") == ["denied"]


def test_reveal_of_unknown_token_is_denied_not_found(h):
    h.upstream.on("GET", "/api/token/42", refusal(200, message="record not found"))
    res = h.user("POST", "/api/token/42/key")
    assert res.status == 403
    assert res.proxy_error == "denied"
    assert res.json["reason"] == "reveal_not_found"
    assert h.upstream.calls("POST") == []


@pytest.mark.parametrize(
    "reply, reason",
    [
        (refusal(200, message="database is locked"), "reveal_precheck_body"),  # DB hiccup: retry
        (httpx.Response(200, content=b"<html>"), "reveal_precheck_body"),
        (httpx.Response(500, content=b""), "reveal_precheck_http_500"),  # limiter lost Valkey
        (httpx.Response(500, json={"error": {"type": "new_api_panic"}}), "reveal_precheck_http_500"),
        (httpx.Response(404, content=b""), "reveal_precheck_http_404"),
    ],
)
def test_reveal_precheck_without_definite_answer_is_connect(h, caplog, reply, reason):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h.upstream.on("GET", "/api/token/42", reply)
    res = h.user("POST", "/api/token/42/key")
    assert res.status == 502
    assert res.proxy_error == "connect"  # NotExecuted: safe to re-send
    assert res.json["reason"] == reason
    assert h.upstream.calls("POST") == []
    assert results(caplog, "reveal") == ["not_sent"]


def test_reveal_precheck_transport_failure_and_429_mean_not_sent(h, caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    h.upstream.on("GET", "/api/token/42", refuse)
    res = h.user("POST", "/api/token/42/key")
    assert res.status == 502
    assert res.proxy_error == "connect"
    assert h.upstream.calls("POST") == []
    h.upstream.on("GET", "/api/token/42", httpx.Response(429, headers={"Retry-After": "40"}))
    res = h.user("POST", "/api/token/42/key")
    assert res.proxy_error == "connect"
    assert res.headers["retry-after"] == "40"
    assert results(caplog, "reveal") == ["not_sent", "not_sent"]


@pytest.mark.parametrize(
    "status, body",
    [
        (401, {"message": "unauthorized", "code": "AUTH_UNAUTHORIZED"}),
        (401, {"message": "expired", "code": "ACCESS_TOKEN_EXPIRED"}),
        (403, {"message": "scope", "code": "ACCESS_TOKEN_SCOPE_DENIED"}),
        (500, {"message": "x", "code": "AUTH_INTERNAL_ERROR"}),
        (200, {"message": "nope", "message_key": "Some definite refusal"}),
    ],
)
def test_reveal_precheck_refusal_with_code_is_relayed(h, caplog, status, body):
    """A revoked / expired UP answers the pre-check with a stable code: the
    client must see that code (UP 401 → re-sign; §3.4), not a retryable 502."""
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h.upstream.on("GET", "/api/token/42", refusal(status, **body))
    res = h.user("POST", "/api/token/42/key")
    assert res.status == status
    assert res.json == {"success": False, **body}
    assert res.proxy_error is None
    assert res.headers["x-woow-proxy-precheck"] == "reveal"
    assert h.upstream.calls("POST") == []
    assert results(caplog, "reveal") == ["precheck_relayed"]


def test_reveal_has_no_body(h):
    h.upstream.on("GET", "/api/token/42", ok(token()))
    assert h.user("POST", "/api/token/42/key", body=b'{"x":1}').status == 400


def test_reveal_audit_is_written_before_the_call_and_survives_cancellation(h, caplog):
    """If the task dies mid-call (shutdown timeout), the audit trail still says
    the reveal may have reached new-api."""
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h.upstream.on("GET", "/api/token/42", ok(token()))
    entered = asyncio.Event()

    async def hang(request):
        entered.set()
        await asyncio.Event().wait()

    h.upstream.on("POST", "/api/token/42/key", hang)

    async def scenario():
        headers = h.sign_headers("POST", "/api/token/42/key", identity="user", client_ip=TENANT_IP)
        headers.append(("Authorization", f"Bearer {USER_PAT}"))
        task = asyncio.create_task(h.acall("POST", b"/api/token/42/key", b"", headers))
        await asyncio.wait_for(entered.wait(), 2)
        assert results(caplog, "reveal") == ["sending"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert results(caplog, "reveal") == ["sending", "cancelled"]
    assert h.proxy.inflight.current["user"] == 0


# --- add_quota ----------------------------------------------------------------

TARGET = {"id": 123, "username": "wsab0000000123", "role": 1, "group": "default", "status": 1, "quota": 0}
ADD = {"id": 123, "action": "add_quota", "mode": "add", "value": 50 * QUOTA_PER_POINT}


@pytest.fixture
def m():
    harness = Harness()
    harness.upstream.on("GET", "/api/user/123", ok(TARGET))
    harness.upstream.on("POST", "/api/user/manage", ok(request_id="req-add-1"))
    return harness


def test_add_quota_happy_path(m, caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    res = m.signed("POST", "/api/user/manage", identity="ap", body=ADD)
    assert res.status == 200
    assert res.headers["x-oneapi-request-id"] == "req-add-1"
    pre, post = m.upstream.requests
    assert pre.method == "GET" and pre.url.path == "/api/user/123"
    assert pre.headers["authorization"] == f"Bearer {AP_TOKEN}"
    assert post.headers["x-forwarded-for"] == "198.18.0.1"
    assert post.headers["connection"] == "close"  # never a pooled socket for a write
    assert json.loads(post.content) == ADD
    sending, done = audit_lines(caplog, "add_quota")
    assert sending == {**sending, "result": "sending", "target_user_id": 123, "value": ADD["value"]}
    assert done["target_user_id"] == 123 and done["value"] == ADD["value"]
    assert done["result"] == "forwarded" and done["request_id"] == "req-add-1" and done["budget"] == "kept"
    assert m.proxy.metrics.get("newapi_proxy_add_quota_value_total") == ADD["value"]


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": "override"},
        {"mode": "subtract"},
        {"mode": "ADD"},
        {"action": "disable"},
        {"action": "promote"},
        {"value": 0},
        {"value": -15625},
        {"value": MANAGE_MAX_VALUE + 1},
        {"value": 15625.0},
        {"value": "15625"},
        {"value": True},
        {"id": 0},
        {"id": "123"},
        {"id": True},
        {"Mode": "override"},
        {"MODE": "override"},
        {"reason": "x"},
    ],
)
def test_add_quota_negative_never_reaches_upstream(m, patch):
    res = m.signed("POST", "/api/user/manage", identity="ap", body={**ADD, **patch})
    assert res.status == 403, patch
    assert res.proxy_error == "policy"
    assert m.upstream.requests == []


def test_policy_denied_add_quota_audit_names_target_and_value(m, caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    m.signed("POST", "/api/user/manage", identity="ap", body={**ADD, "mode": "override", "value": 7})
    m.signed("POST", "/api/user/manage", identity="ap", body={**ADD, "id": True, "value": "7"})
    first, second = audit_lines(caplog, "add_quota")
    assert (first["target_user_id"], first["value"], first["reason"]) == (123, 7, "manage_mode")
    # only exact ints are recorded (bool / str values are not)
    assert "target_user_id" not in second and "value" not in second


@pytest.mark.parametrize(
    "target",
    [
        {**TARGET, "username": "wp0000000123"},  # prod prefix
        {**TARGET, "username": "wpsvc-stg"},  # the service account
        {**TARGET, "username": "root"},
        {**TARGET, "role": 10},
        {**TARGET, "role": 100},
        {**TARGET, "group": "internal"},
        {**TARGET, "group": "free_blocked"},
        {**TARGET, "id": 124},
    ],
)
def test_add_quota_target_must_be_paas_user(m, target):
    m.upstream.on("GET", "/api/user/123", ok(target))
    res = m.signed("POST", "/api/user/manage", identity="ap", body=ADD)
    assert res.status == 403
    assert res.proxy_error == "denied"
    assert res.json["reason"] == "manage_target"
    assert m.upstream.calls("POST") == []
    assert m.proxy.budget.used(m.mono) == 0  # released


def test_add_quota_target_missing_is_denied_not_found(m):
    m.upstream.on("GET", "/api/user/123", refusal(200, message="record not found"))
    res = m.signed("POST", "/api/user/manage", identity="ap", body=ADD)
    assert res.status == 403
    assert res.proxy_error == "denied"
    assert res.json["reason"] == "manage_not_found"
    assert m.upstream.calls("POST") == []
    assert m.proxy.budget.used(m.mono) == 0


def test_add_quota_precheck_db_error_is_connect_not_policy(m):
    """A new-api DB hiccup is not a PaaS bug: no needs_review, no circuit."""
    m.upstream.on("GET", "/api/user/123", refusal(200, message="driver: bad connection"))
    res = m.signed("POST", "/api/user/manage", identity="ap", body=ADD)
    assert res.status == 502
    assert res.proxy_error == "connect"
    assert res.json["reason"] == "manage_precheck_body"
    assert m.upstream.calls("POST") == []
    assert m.proxy.budget.used(m.mono) == 0


@pytest.mark.parametrize(
    "status, body",
    [
        (401, {"message": "x", "code": "AUTH_UNAUTHORIZED"}),  # AP revoked / expired
        (403, {"message": "x", "code": "ACCESS_TOKEN_SCOPE_DENIED"}),
        (200, {"message": "x", "message_key": "No permission to access users of same or higher level"}),
    ],
)
def test_add_quota_precheck_refusal_is_relayed(m, status, body):
    m.upstream.on("GET", "/api/user/123", refusal(status, **body))
    res = m.signed("POST", "/api/user/manage", identity="ap", body=ADD)
    assert res.status == status
    assert res.json == {"success": False, **body}
    assert res.proxy_error is None
    assert res.headers["x-woow-proxy-precheck"] == "manage"
    assert m.upstream.calls("POST") == []
    assert m.proxy.budget.used(m.mono) == 0


def test_add_quota_hourly_cap():
    h = Harness(PROXY_ADD_QUOTA_HOURLY_CAP_POINTS="200", PROXY_REPLICAS="2")  # 100 points per replica
    h.upstream.on("GET", "/api/user/123", ok(TARGET))
    body = {**ADD, "value": 60 * QUOTA_PER_POINT}
    assert h.signed("POST", "/api/user/manage", identity="ap", body=body).status == 200
    res = h.signed("POST", "/api/user/manage", identity="ap", body=body)
    assert res.status == 429
    assert res.proxy_error == "budget"
    assert len(h.upstream.calls("POST")) == 1
    # 40 points still fit
    assert (
        h.signed("POST", "/api/user/manage", identity="ap", body={**ADD, "value": 40 * QUOTA_PER_POINT}).status == 200
    )
    # an hour later the window has moved on
    h.mono += 3600
    assert h.signed("POST", "/api/user/manage", identity="ap", body=body).status == 200


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(429, headers={"Retry-After": "30"}),  # GA bucket: before the handler
        httpx.Response(500, content=b""),  # limiter lost Valkey
        httpx.Response(401, json={"success": False, "message": "x", "code": "AUTH_UNAUTHORIZED"}),
        httpx.Response(403, json={"success": False, "message": "x", "code": "ACCESS_TOKEN_SCOPE_DENIED"}),
        httpx.Response(500, json={"success": False, "message": "x", "code": "AUTH_INTERNAL_ERROR"}),
        httpx.Response(200, json={"success": False, "message": "x", "message_key": "User does not exist"}),
    ],
)
def test_add_quota_budget_released_when_new_api_proves_nothing_ran(reply, caplog):
    """Repeated definite refusals must not eat the hourly budget (they used to,
    so a few re-sent orders could block every conversion for an hour)."""
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h = Harness(PROXY_ADD_QUOTA_HOURLY_CAP_POINTS="4", PROXY_REPLICAS="2")  # 2 points per replica
    h.upstream.on("GET", "/api/user/123", ok(TARGET))
    h.upstream.on("POST", "/api/user/manage", reply)
    body = {**ADD, "value": 2 * QUOTA_PER_POINT}
    for _ in range(3):
        res = h.signed("POST", "/api/user/manage", identity="ap", body=body)
        assert res.status == reply.status_code
        assert res.proxy_error is None
    assert h.proxy.budget.used(h.mono) == 0
    assert [line.get("budget") for line in audit_lines(caplog, "add_quota") if line["result"] == "forwarded"] == [
        "released"
    ] * 3


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"success": False, "message": "pq: could not serialize access"}),  # no message_key
        httpx.Response(500, json={"error": {"type": "new_api_panic"}}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"success": True, "message": ""}),
    ],
)
def test_add_quota_budget_kept_when_the_outcome_may_be_credited(reply):
    h = Harness(PROXY_ADD_QUOTA_HOURLY_CAP_POINTS="4", PROXY_REPLICAS="2")
    h.upstream.on("GET", "/api/user/123", ok(TARGET))
    h.upstream.on("POST", "/api/user/manage", reply)
    body = {**ADD, "value": 2 * QUOTA_PER_POINT}
    assert h.signed("POST", "/api/user/manage", identity="ap", body=body).status == reply.status_code
    assert h.proxy.budget.used(h.mono) == 2 * QUOTA_PER_POINT
    assert h.signed("POST", "/api/user/manage", identity="ap", body=body).proxy_error == "budget"


def test_add_quota_budget_released_only_when_certainly_not_sent(caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    h = Harness(PROXY_ADD_QUOTA_HOURLY_CAP_POINTS="200", PROXY_REPLICAS="2")
    h.upstream.on("GET", "/api/user/123", ok(TARGET))
    body = {**ADD, "value": 100 * QUOTA_PER_POINT}

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    h.upstream.on("POST", "/api/user/manage", refuse)
    assert h.signed("POST", "/api/user/manage", identity="ap", body=body).proxy_error == "connect"
    assert h.proxy.budget.used(h.mono) == 0

    def reset(request):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)

    h.upstream.on("POST", "/api/user/manage", reset)
    assert h.signed("POST", "/api/user/manage", identity="ap", body=body).proxy_error == "reset"
    # outcome unknown: the amount stays counted
    assert h.proxy.budget.used(h.mono) == 100 * QUOTA_PER_POINT
    assert results(caplog, "add_quota") == ["sending", "connect", "sending", "reset"]


def test_add_quota_cap_zero_disables_it():
    h = Harness(PROXY_ADD_QUOTA_HOURLY_CAP_POINTS="0")
    h.upstream.on("GET", "/api/user/123", ok(TARGET))
    assert h.signed("POST", "/api/user/manage", identity="ap", body=ADD).status == 429
    assert h.upstream.requests == []
