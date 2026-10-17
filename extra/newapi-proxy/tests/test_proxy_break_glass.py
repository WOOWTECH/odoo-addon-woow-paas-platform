"""Break-glass (bg, 〔REV〕1, DESIGN §1.4.2-2): CRON key only, only while
``PROXY_BREAK_GLASS_UNTIL`` is in the future, two routes only, every use audited.

* ``PUT /api/user/`` (free-tier fallback, §1.3.3): the client names a user and
  ``default``／``free_blocked``; the proxy reads the user with AP and sends the
  full update new-api needs (it overwrites every field it is sent) with only
  ``group`` changed.
* ``POST /api/user/manage`` ``mode: subtract`` (claw-back, §4.2.4): never for
  ap, never ``add``／``override`` for bg; the target is pre-checked the same way.
"""

import json
import logging

import httpx
import pytest

from newapi_proxy.settings import QUOTA_PER_POINT
from proxy_harness import AP_TOKEN, NOW, Harness, ok

USER = {
    "id": 123,
    "username": "wsab0000000123",
    "display_name": "PaaS 123",
    "role": 1,
    "group": "default",
    "remark": "",
    "status": 1,
    "quota": 15625,
    "email": "x@example.com",
    "setting": "{}",
}
GROUP = {"id": 123, "group": "free_blocked"}
SUBTRACT = {"id": 123, "action": "add_quota", "mode": "subtract", "value": 3 * QUOTA_PER_POINT}


def audit_lines(caplog, action="bg"):
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "audit" and r.msg.get("action") == action
    ]


def events(caplog, name):
    return [r for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("event") == name]


@pytest.fixture
def bg():
    harness = Harness(PROXY_BREAK_GLASS_UNTIL=str(NOW + 3600))
    harness.upstream.on("GET", "/api/user/123", ok(USER))
    harness.upstream.on("PUT", "/api/user/", ok(request_id="req-put-1"))
    harness.upstream.on("POST", "/api/user/manage", ok(request_id="req-sub-1"))
    return harness


def test_group_change_rebuilds_the_full_update(bg, caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    res = bg.signed("PUT", "/api/user/", identity="bg", body=GROUP)
    assert res.status == 200, res.body
    pre, put = bg.upstream.requests
    assert (pre.method, pre.url.path) == ("GET", "/api/user/123")
    for req in (pre, put):
        assert req.headers["authorization"] == f"Bearer {AP_TOKEN}"
        assert req.headers["x-forwarded-for"] == "198.18.0.4"  # bg stands out in new-api's audit
    assert json.loads(put.content) == {
        "id": 123,
        "username": "wsab0000000123",
        "display_name": "PaaS 123",
        "group": "free_blocked",
        "remark": "",
        "role": 1,
    }
    sending, done = audit_lines(caplog)[-2:]
    assert sending["result"] == "sending" and sending["op"] == "user_group" and sending["group"] == "free_blocked"
    assert done["result"] == "forwarded" and done["status"] == 200 and done["target_user_id"] == 123
    assert len(events(caplog, "break_glass_used")) == 1
    assert events(caplog, "break_glass_used")[0].levelno == logging.WARNING
    assert bg.proxy.metrics.get("newapi_proxy_break_glass_calls_total", result="forwarded") == 1


def test_group_back_to_default(bg):
    bg.upstream.on("GET", "/api/user/123", ok({**USER, "group": "free_blocked", "remark": None}))
    res = bg.signed("PUT", "/api/user/", identity="bg", body={"id": 123, "group": "default"})
    assert res.status == 200
    body = json.loads(bg.upstream.calls("PUT")[0].content)
    assert body["group"] == "default" and body["remark"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {"id": 123, "group": "internal"},
        {"id": 123, "group": "free"},
        {"id": 123, "group": "default", "role": 10},
        {"id": 123, "group": "default", "password": "x" * 40},
        {"id": 123, "group": "default", "admin_permissions": {}},
        {"id": 123, "group": "default", "username": "root"},
        {"id": 123, "group": "default", "quota": 1},
        {"id": 123},
        {"id": 0, "group": "default"},
        {"id": "123", "group": "default"},
        {"ID": 123, "group": "default"},
    ],
)
def test_group_change_body_policy(bg, body):
    res = bg.signed("PUT", "/api/user/", identity="bg", body=body)
    assert res.status == 403, body
    assert res.proxy_error == "policy"
    assert bg.upstream.requests == []


@pytest.mark.parametrize(
    "target",
    [
        {**USER, "username": "root"},
        {**USER, "username": "wp0000000123"},  # another environment
        {**USER, "role": 10},
        {**USER, "group": "internal"},
        {**USER, "id": 124},
        {**USER, "display_name": 5},
        {**USER, "display_name": "x" * 21},
        {**USER, "remark": "r" * 256},
    ],
)
def test_group_change_target_must_be_a_paas_user(bg, target):
    bg.upstream.on("GET", "/api/user/123", ok(target))
    res = bg.signed("PUT", "/api/user/", identity="bg", body=GROUP)
    assert res.status == 403 and res.proxy_error == "denied" and res.json["reason"] == "manage_target"
    assert bg.upstream.calls("PUT") == []


def test_group_change_precheck_refusal_is_relayed(bg):
    bg.upstream.on("GET", "/api/user/123", httpx.Response(401, json={"success": False, "code": "AUTH_UNAUTHORIZED"}))
    res = bg.signed("PUT", "/api/user/", identity="bg", body=GROUP)
    assert res.status == 401
    assert res.headers["x-woow-proxy-precheck"] == "manage"
    assert bg.upstream.calls("PUT") == []


def test_subtract_is_bg_only_and_prechecked(bg, caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    res = bg.signed("POST", "/api/user/manage", identity="bg", body=SUBTRACT)
    assert res.status == 200
    pre, post = bg.upstream.requests
    assert pre.url.path == "/api/user/123" and post.headers["x-forwarded-for"] == "198.18.0.4"
    assert json.loads(post.content) == SUBTRACT
    done = audit_lines(caplog)[-1]
    assert done["op"] == "manage_subtract" and done["value"] == SUBTRACT["value"]
    assert done["result"] == "forwarded" and "budget" not in done
    assert bg.proxy.budget.used(bg.mono) == 0  # the add_quota budget is not touched
    assert audit_lines(caplog, "add_quota") == []


def test_subtract_also_reaches_free_blocked_users(bg):
    bg.upstream.on("GET", "/api/user/123", ok({**USER, "group": "free_blocked"}))
    assert bg.signed("POST", "/api/user/manage", identity="bg", body=SUBTRACT).status == 200


@pytest.mark.parametrize("mode", ["add", "override", "SUBTRACT"])
def test_bg_never_adds_or_overrides(bg, mode):
    res = bg.signed("POST", "/api/user/manage", identity="bg", body={**SUBTRACT, "mode": mode})
    assert res.status == 403 and res.json["reason"] == "manage_mode"
    assert bg.upstream.requests == []


def test_ap_never_subtracts(bg):
    res = bg.signed("POST", "/api/user/manage", identity="ap", body=SUBTRACT)
    assert res.status == 403 and res.json["reason"] == "manage_mode"
    assert bg.upstream.requests == []


def test_ap_never_changes_groups(bg):
    res = bg.signed("PUT", "/api/user/", identity="ap", body=GROUP)
    assert res.status == 403 and res.json["reason"] == "route"


@pytest.mark.parametrize(
    "method, path, body",
    [("PUT", "/api/user/", GROUP), ("POST", "/api/user/manage", SUBTRACT)],
)
def test_bg_is_off_unless_opened_and_never_for_web(method, path, body, caplog):
    caplog.set_level(logging.INFO, logger="newapi_proxy")
    off = Harness()
    res = off.signed(method, path, identity="bg", body=body)
    assert res.status == 403 and res.json["reason"] == "break_glass_off"
    assert off.upstream.requests == []
    assert events(caplog, "break_glass_used") == []
    too_far = Harness(PROXY_BREAK_GLASS_UNTIL=str(NOW + 25 * 3600))
    assert "break_glass_until_too_far" in too_far.warnings
    assert too_far.signed(method, path, identity="bg", body=body).json["reason"] == "break_glass_off"
    opened = Harness(PROXY_BREAK_GLASS_UNTIL=str(NOW + 600))
    res = opened.signed(method, path, identity="bg", body=body, key="web")
    assert res.status == 403 and res.json["reason"] == "key_scope"


def test_bg_with_its_own_authorization_is_400(bg):
    res = bg.signed("PUT", "/api/user/", identity="bg", body=GROUP, headers=[("Authorization", f"Bearer {AP_TOKEN}")])
    assert res.status == 400 and res.json["reason"] == "authorization_not_allowed"
