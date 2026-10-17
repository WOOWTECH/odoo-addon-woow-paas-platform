"""Route whitelist, identities and the WEB / CRON split (N23, 〔REV〕14)."""

import pytest

from newapi_proxy import routetable
from proxy_harness import NOW, Harness


@pytest.fixture
def h():
    return Harness()


@pytest.mark.parametrize(
    "method, path, identity",
    [
        ("POST", "/v1/chat/completions", "user"),
        ("GET", "/v1/models", "anon"),
        ("GET", "/pg/chat", "user"),
        ("GET", "/api/option/", "ap"),
        ("GET", "/api/option/", "anon"),
        ("GET", "/api/option/", "user"),
        ("PUT", "/api/option/", "op"),
        ("GET", "/api/option/request_policy", "op"),
        ("GET", "/api/channel/", "ap"),
        ("GET", "/api/redemption/", "ap"),
        ("GET", "/api/user/topup", "ap"),
        ("POST", "/api/user/topup/complete", "ap"),
        ("POST", "/api/user/auth/refresh", "user"),
        ("POST", "/api/user/auth/logout", "user"),
        ("PUT", "/api/user/", "ap"),  # only bg, and not in LLM-01a
        ("DELETE", "/api/user/5", "ap"),
        ("POST", "/api/token/batch/keys", "user"),
        ("GET", "/api/log/self/export", "user"),
        ("GET", "/api/user/", "user"),
        ("GET", "/api/status", "ap"),  # status is anon-only in this ticket
        ("POST", "/api/user/manage", "user"),
        ("GET", "/api/token/", "ap"),  # AP cannot manage API keys
        ("GET", "/api/setup", "anon"),
        ("POST", "/api/setup", "anon"),
        ("GET", "/healthz/", "anon"),
        ("HEAD", "/api/status", "anon"),
        ("OPTIONS", "/api/token/", "user"),
        ("GET", "/api/user/sessions/abc", "user"),  # DELETE only
    ],
)
def test_unlisted_routes_are_403_policy(h, method, path, identity):
    kwargs = {"headers": [("Authorization", "Bearer nap_" + "u" * 43)]} if identity == "user" else {}
    res = h.signed(method, path, identity=identity, **kwargs)
    assert res.status == 403, (method, path, res.body)
    assert res.proxy_error == "policy"
    assert h.upstream.requests == []


def test_option_get_for_op_with_cron_key(h):
    res = h.signed("GET", "/api/option/", identity="op")
    assert res.status == 200
    assert h.upstream.requests[0].headers["authorization"].startswith("Bearer nap_OpToken")


@pytest.mark.parametrize(
    "method, path, identity, query",
    [
        ("GET", "/api/user/search", "ap", [("keyword", "wsab0000000001")]),
        ("GET", "/api/user/5", "ap", []),
        ("GET", "/api/status", "anon", []),
        ("GET", "/api/option/", "op", []),
    ],
)
def test_web_key_cannot_use_system_identities(h, method, path, identity, query):
    res = h.signed(method, path, query, identity=identity, key="web")
    assert res.status == 403
    assert res.json["reason"] == "key_scope"
    assert h.upstream.requests == []


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/api/user/login", {"username": "wsab0000000042", "password": "x" * 40}),
        ("GET", "/api/user/access_tokens", None),
        ("DELETE", "/api/user/sessions/0f0e0d0c-aaaa-bbbb-cccc-000000000001", None),
    ],
)
def test_web_key_cannot_use_cron_only_user_routes(h, method, path, body):
    auth = None if path.endswith("login") else "nap_" + "u" * 43
    res = h.user(method, path, auth=auth, body=body, key="web")
    assert res.status == 403
    assert res.json["reason"] == "key_scope"
    assert h.upstream.requests == []


@pytest.mark.parametrize(
    "method, path, query",
    [
        ("GET", "/api/user/self", []),
        ("GET", "/api/token/", [("p", "1"), ("page_size", "10")]),
        ("GET", "/api/token/42", []),
        ("GET", "/api/log/self", [("type", "2")]),
    ],
)
def test_web_key_can_use_interactive_user_routes(h, method, path, query):
    res = h.user(method, path, query_pairs=query, key="web")
    assert res.status == 200, res.body
    assert len(h.upstream.requests) == 1


def test_cron_key_can_use_everything_listed(h):
    assert h.signed("GET", "/api/status").status == 200
    assert h.signed("GET", "/api/user/7", identity="ap").status == 200
    assert h.user("GET", "/api/user/access_tokens").status == 200
    assert h.user("DELETE", "/api/user/sessions/0f0e0d0c-aaaa-bbbb-cccc-000000000001").status == 200


def test_identity_must_match_route(h):
    res = h.user("GET", "/api/user/7")  # an ap route used as user
    assert res.status == 403


def test_unknown_identity_is_400(h):
    res = h.signed("GET", "/api/status", identity="root")
    assert res.status == 400
    assert res.json["reason"] == "identity_unknown"
    res = h.signed("GET", "/api/status", identity="ANON")
    assert res.status == 400


def test_break_glass_off_by_default(h):
    res = h.signed("PUT", "/api/user/", identity="bg", body={"id": 1, "group": "free_blocked"})
    assert res.status == 403
    assert res.json["reason"] == "break_glass_off"
    assert h.upstream.requests == []


def test_break_glass_needs_cron_key():
    h = Harness(PROXY_BREAK_GLASS_UNTIL=str(NOW + 600))
    res = h.signed("GET", "/api/user/7", identity="bg", key="web")
    assert res.status == 403
    assert res.json["reason"] == "key_scope"


def test_break_glass_window_open_but_no_bg_routes_yet():
    h = Harness(PROXY_BREAK_GLASS_UNTIL=str(NOW + 600))
    res = h.signed("PUT", "/api/user/", identity="bg", body={"id": 1, "group": "free_blocked"})
    assert res.status == 403
    assert res.json["reason"] == "route"
    assert h.proxy.metrics.get("newapi_proxy_break_glass_total", result="active") == 1
    # Window closes: off again.
    h.now = NOW + 601
    res = h.signed("PUT", "/api/user/", identity="bg", body={"id": 1, "group": "free_blocked"})
    assert res.json["reason"] == "break_glass_off"


def test_route_table_has_no_bg_routes_and_web_only_on_user():
    for route in routetable.ROUTES:
        assert "bg" not in route.identities
        if route.web:
            assert route.identities == frozenset({"user"})
        if route.special == "manage":
            assert route.identities == frozenset({"ap"})


def test_missing_ap_token_is_503_config():
    h = Harness(PROXY_AP_TOKEN="")
    res = h.signed("GET", "/api/user/7", identity="ap")
    assert res.status == 503
    assert res.proxy_error == "config"
    assert h.upstream.requests == []
