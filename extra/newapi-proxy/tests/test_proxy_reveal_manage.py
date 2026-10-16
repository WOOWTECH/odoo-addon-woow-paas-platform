"""Reveal only within 120 s (〔REV〕2); add_quota add-only, target pre-check, hourly cap (〔REV〕1)."""

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
    [line] = audit_lines(caplog, "reveal")
    assert line["token_id"] == 42 and line["result"] == "forwarded" and line["status"] == 200
    assert line["kid"] == "web-2026a" and line["ip"] == TENANT_IP
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
    assert res.json["reason"] == reason
    assert h.upstream.calls("POST") == []
    [line] = audit_lines(caplog, "reveal")
    assert line["result"] == "denied"


def test_reveal_of_unknown_token_is_403(h):
    h.upstream.on("GET", "/api/token/42", httpx.Response(200, json={"success": False, "message": "record not found"}))
    res = h.user("POST", "/api/token/42/key")
    assert res.status == 403
    assert res.json["reason"] == "reveal_token_unknown"
    assert h.upstream.calls("POST") == []


def test_reveal_precheck_failure_means_not_sent(h, caplog):
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
    assert len(audit_lines(caplog, "reveal")) == 2


def test_reveal_has_no_body(h):
    h.upstream.on("GET", "/api/token/42", ok(token()))
    assert h.user("POST", "/api/token/42/key", body=b'{"x":1}').status == 400


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
    [line] = audit_lines(caplog, "add_quota")
    assert line["target_user_id"] == 123 and line["value"] == ADD["value"]
    assert line["result"] == "forwarded" and line["request_id"] == "req-add-1"
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
    assert m.upstream.requests == []


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
    assert res.json["reason"] == "manage_target"
    assert m.upstream.calls("POST") == []
    assert m.proxy.budget.used(m.mono) == 0  # released


def test_add_quota_target_missing(m):
    m.upstream.on("GET", "/api/user/123", httpx.Response(200, json={"success": False, "message": "record not found"}))
    res = m.signed("POST", "/api/user/manage", identity="ap", body=ADD)
    assert res.status == 403
    assert m.upstream.calls("POST") == []


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


def test_add_quota_budget_released_only_when_certainly_not_sent():
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


def test_add_quota_cap_zero_disables_it():
    h = Harness(PROXY_ADD_QUOTA_HOURLY_CAP_POINTS="0")
    h.upstream.on("GET", "/api/user/123", ok(TARGET))
    assert h.signed("POST", "/api/user/manage", identity="ap", body=ADD).status == 429
    assert h.upstream.requests == []
