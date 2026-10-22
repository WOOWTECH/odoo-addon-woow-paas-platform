"""Route whitelist, identities and the WEB / CRON split (N23, 〔REV〕14)."""

import pytest

from newapi_proxy import routetable
from proxy_harness import NOW, Harness, ok


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
        ("PUT", "/api/user/", "ap"),  # bg only
        ("PUT", "/api/user/", "user"),
        ("DELETE", "/api/user/5", "ap"),
        ("POST", "/api/token/batch/keys", "user"),
        ("GET", "/api/log/self/export", "user"),
        ("GET", "/api/user/", "user"),
        ("GET", "/api/status", "op"),
        ("GET", "/api/pricing", "anon"),
        ("GET", "/api/log/", "user"),  # the admin log view is ap-only
        ("GET", "/api/log/self", "ap"),
        ("GET", "/api/data/", "user"),
        ("GET", "/api/data/self", "ap"),
        ("GET", "/api/audit", "ap"),  # only /api/audit/self, and only for user
        ("GET", "/api/log/search", "ap"),
        ("GET", "/api/log/export", "ap"),
        ("GET", "/api/token/search", "user"),
        ("GET", "/api/token/auto-groups", "user"),
        ("PATCH", "/api/user/access_tokens/5", "user"),
        ("DELETE", "/api/user/access_tokens/legacy", "user"),
        ("GET", "/api/user/groups", "user"),
        ("PUT", "/api/user/self", "user"),
        ("PUT", "/api/user/setting", "user"),
        ("DELETE", "/api/token/batch", "user"),
        ("POST", "/api/user/manage", "op"),
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
        ("GET", "/api/user/", "ap", [("p", "1"), ("page_size", "100")]),
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
    h.upstream.on("GET", "/api/user/", ok({"items": [], "page": 1, "page_size": 100, "total": 0}))
    listed = h.signed("GET", "/api/user/", [("p", "1"), ("page_size", "100")], identity="ap")
    assert listed.status == 200
    assert h.upstream.requests[-1].headers["authorization"].startswith("Bearer nap_")
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


def test_break_glass_window_closes():
    h = Harness(PROXY_BREAK_GLASS_UNTIL=str(NOW + 600))
    res = h.signed("GET", "/api/user/7", identity="bg")
    assert res.status == 403
    assert res.json["reason"] == "route"  # bg has only its two routes
    assert h.proxy.metrics.get("newapi_proxy_break_glass_total", result="active") == 1
    # Window closes: off again.
    h.now = NOW + 601
    res = h.signed("PUT", "/api/user/", identity="bg", body={"id": 1, "group": "free_blocked"})
    assert res.json["reason"] == "break_glass_off"


def test_route_table_shape():
    """bg has exactly its two routes; WEB only ever on user routes; one route per (method, template)."""
    seen = set()
    bg = set()
    for route in routetable.ROUTES:
        key = (route.method, route.template)
        assert key not in seen, key
        seen.add(key)
        if "bg" in route.identities:
            bg.add(key)
        if route.web:
            assert route.identities == frozenset({"user"})
        if route.special == "manage":
            assert route.identities == frozenset({"ap", "bg"})
    assert bg == {("PUT", "/api/user/"), ("POST", "/api/user/manage")}


#: DESIGN §1.4.2-1: what a WEB key may call (token CRUD, reveal, the user's own usage and profile).
WEB_ROUTES = {
    ("GET", "/api/user/self"),
    ("GET", "/api/user/models"),
    ("GET", "/api/user/self/groups"),
    ("POST", "/api/token/"),
    ("PUT", "/api/token/"),
    ("POST", "/api/token/batch"),
    ("GET", "/api/token/"),
    ("GET", "/api/token/{id}"),
    ("DELETE", "/api/token/{id}"),
    ("POST", "/api/token/{id}/key"),
    ("GET", "/api/log/self"),
    ("GET", "/api/log/self/stat"),
    ("GET", "/api/data/self"),
    ("GET", "/api/data/flow/self"),
}


def test_web_routes_are_exactly_the_interactive_user_routes():
    assert {(r.method, r.template) for r in routetable.ROUTES if r.web} == WEB_ROUTES


#: DESIGN §1.4.2-5 upstream whitelist plus the local, CRON-only control read.
DESIGN_ROUTES = {
    "anon": {("GET", "/api/status"), ("GET", "/_control/audit")},
    "op": {("GET", "/api/option/")},
    "ap": {
        ("POST", "/api/user/"),
        ("GET", "/api/user/"),
        ("GET", "/api/user/search"),
        ("GET", "/api/user/{id}"),
        ("POST", "/api/user/manage"),
        ("GET", "/api/log/"),
        ("GET", "/api/log/stat"),
        ("GET", "/api/data/"),
        ("GET", "/api/data/users"),
        ("GET", "/api/data/flow"),
        ("GET", "/api/verify/methods"),
        ("GET", "/api/status"),
        ("GET", "/api/pricing"),
    },
    "bg": {("PUT", "/api/user/"), ("POST", "/api/user/manage")},
    "user": WEB_ROUTES
    | {
        ("POST", "/api/user/login"),
        ("POST", "/api/verify"),
        ("GET", "/api/user/access_tokens"),
        ("POST", "/api/user/access_tokens"),
        ("DELETE", "/api/user/access_tokens/{id}"),
        ("DELETE", "/api/user/sessions/{sid}"),
        ("GET", "/api/audit/self"),
        ("GET", "/api/pricing"),
        ("GET", "/api/status"),
    },
}


def test_route_table_is_the_design_whitelist():
    for identity, expected in DESIGN_ROUTES.items():
        got = {(r.method, r.template) for r in routetable.ROUTES if identity in r.identities}
        assert got == expected, identity


@pytest.mark.parametrize(
    "method, path, query",
    [
        ("GET", "/api/user/models", []),
        ("GET", "/api/user/self/groups", []),
        ("DELETE", "/api/token/42", []),
        ("GET", "/api/log/self/stat", [("type", "2"), ("start_timestamp", "1"), ("end_timestamp", "2")]),
        ("GET", "/api/data/self", [("start_timestamp", "1"), ("end_timestamp", "2")]),
        ("GET", "/api/data/flow/self", [("start_timestamp", "1"), ("end_timestamp", "2")]),
    ],
)
def test_web_key_can_use_the_new_interactive_routes(h, method, path, query):
    res = h.user(method, path, query_pairs=query, key="web")
    assert res.status == 200, res.body
    assert len(h.upstream.requests) == 1


@pytest.mark.parametrize(
    "method, path, identity, query",
    [
        ("GET", "/api/audit/self", "user", [("p", "1"), ("page_size", "10")]),
        ("GET", "/api/pricing", "user", []),
        ("GET", "/api/status", "user", []),
        ("GET", "/api/pricing", "ap", []),
        ("GET", "/api/status", "ap", []),
        ("GET", "/api/log/", "ap", [("type", "1"), ("username", "wsab0000000042"), ("p", "1")]),
        ("GET", "/api/log/stat", "ap", [("type", "2"), ("group", "free")]),
        ("GET", "/api/data/", "ap", [("username", "wsab0000000042"), ("start_timestamp", "1")]),
        ("GET", "/api/data/users", "ap", [("start_timestamp", "1"), ("end_timestamp", "2")]),
        ("GET", "/api/data/flow", "ap", [("start_timestamp", "1"), ("end_timestamp", "2")]),
        ("GET", "/api/verify/methods", "ap", [("scope", "access_token.generate")]),
    ],
)
def test_cron_only_routes(h, method, path, identity, query):
    kwargs = {"headers": [("Authorization", "Bearer nap_" + "u" * 43)]} if identity == "user" else {}
    res = h.signed(method, path, query, identity=identity, **kwargs)
    assert res.status == 200, res.body
    if identity == "ap":
        assert h.upstream.requests[-1].headers["authorization"].startswith("Bearer nap_ApToken")
    web = h.signed(method, path, query, identity=identity, key="web", **kwargs)
    assert web.status == 403 and web.json["reason"] == "key_scope"


@pytest.mark.parametrize(
    "path, query",
    [
        ("/api/log/", [("channel", "3")]),  # the channel is internal
        ("/api/log/", [("upstream_request_id", "x")]),
        ("/api/log/", [("username", "root")]),  # only this environment's PaaS usernames
        ("/api/log/", [("username", "wp0000000042")]),
        ("/api/log/stat", [("channel", "3")]),
        ("/api/data/", [("username", "admin")]),
        ("/api/data/flow", [("username", "wsab000000004")]),
        ("/api/data/users", [("username", "wsab0000000042")]),
        ("/api/verify/methods", [("scope", "x")]),
    ],
)
def test_admin_reads_refuse_other_filters(h, path, query):
    res = h.signed("GET", path, query, identity="ap")
    assert res.status == 400
    assert h.upstream.requests == []


def test_pat_delete_needs_the_revoke_proof(h):
    from proxy_harness import PROOF

    res = h.user("DELETE", "/api/user/access_tokens/9")
    assert res.status == 400 and res.json["reason"] == "proof_missing"
    res = h.user("DELETE", "/api/user/access_tokens/9", headers=[("X-Security-Proof", PROOF)])
    assert res.status == 200
    assert h.upstream.requests[-1].headers["x-security-proof"] == PROOF
    res = h.user("DELETE", "/api/user/access_tokens/9", headers=[("X-Security-Proof", PROOF)], key="web")
    assert res.status == 403 and res.json["reason"] == "key_scope"


def test_missing_ap_token_is_503_config():
    h = Harness(PROXY_AP_TOKEN="")
    res = h.signed("GET", "/api/user/7", identity="ap")
    assert res.status == 503
    assert res.proxy_error == "config"
    assert h.upstream.requests == []
