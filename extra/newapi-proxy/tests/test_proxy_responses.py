"""``GET /api/user/`` answers are rebuilt, not relayed (LLM-02b review round 2).

rc.44 ``GetAllUsers`` lists every row (root and admins included) with e-mail, ``setting``,
``aff_code``, Stripe and OAuth ids, and skips the role check ``GET /api/user/{id}`` has.
"""

import json

import httpx
import pytest

from newapi_proxy import responses
from newapi_proxy.settings import load_settings
from proxy_harness import NOW, USERNAME, Harness, base_env, ok

LIST = [("p", "1"), ("page_size", "100"), ("sort_by", "id"), ("sort_order", "asc")]
WEBHOOK_SECRET = "whsec_" + "RootWebhookSecret0123456789abcdef"
GOTIFY = "AGotifyToken9876543210zyx"

ROOT = {
    "id": 1,
    "username": "root",
    "display_name": "Root User",
    "role": 100,
    "status": 1,
    "email": "owner@example.com",
    "github_id": "gh-123",
    "oidc_id": "oidc-root",
    "quota": 999999999,
    "used_quota": 5,
    "request_count": 9,
    "group": "default",
    "aff_code": "ROOTAFF1",
    "stripe_customer": "cus_ROOT123",
    "setting": json.dumps({"webhook_secret": WEBHOOK_SECRET, "gotify_token": GOTIFY}),
    "DeletedAt": None,
}
OTHER = {**ROOT, "id": 2, "username": "elmo-test", "role": 1, "email": "elmo@example.com", "setting": ""}
PAAS = {
    "id": 42,
    "username": USERNAME,
    "display_name": "PaaS 42",
    "role": 1,
    "status": 1,
    "email": "",
    "quota": -15625,
    "used_quota": 2**40,
    "request_count": 11,
    "group": "default",
    "aff_code": "PAASAFF",
    "setting": json.dumps({"webhook_secret": "paas-user-secret"}),
    "DeletedAt": None,
}
DELETED = {**PAAS, "id": 43, "username": "wsab0000000043", "DeletedAt": "2026-10-17T03:04:05.123456+08:00"}


@pytest.fixture
def h():
    return Harness()


def _listed(h, *items, total=None, **data):
    page = {"page": 1, "page_size": 100, "total": len(items) if total is None else total, "items": list(items)}
    page.update(data)
    h.upstream.on("GET", "/api/user/", ok(page))
    return h.signed("GET", "/api/user/", LIST, identity="ap")


def test_only_paas_users_and_only_balance_fields_come_back(h):
    res = _listed(h, ROOT, OTHER, PAAS, DELETED)
    assert res.status == 200, res.body
    assert res.proxy_error is None
    assert res.json == {
        "success": True,
        "message": "",
        "data": {
            "page": 1,
            "page_size": 100,
            "total": 4,
            "items": [
                {"id": 1, "username": None},
                {"id": 2, "username": None},
                {
                    "id": 42,
                    "username": USERNAME,
                    "quota": -15625,
                    "used_quota": 2**40,
                    "request_count": 11,
                    "status": 1,
                    "DeletedAt": None,
                },
                {
                    "id": 43,
                    "username": "wsab0000000043",
                    "quota": -15625,
                    "used_quota": 2**40,
                    "request_count": 11,
                    "status": 1,
                    "DeletedAt": "2026-10-17T03:04:05.123456+08:00",
                },
            ],
        },
    }
    for leaked in (WEBHOOK_SECRET, GOTIFY, "owner@example.com", "elmo@example.com", "ROOTAFF1", "cus_ROOT123",
                   "gh-123", "oidc-root", "paas-user-secret", "PAASAFF", "Root User", '"role"', '"setting"'):
        assert leaked.encode() not in res.body, leaked
    assert res.headers["cache-control"] == "no-store"


def test_item_count_never_changes(h):
    """The refresh ends a round on a short page: masking must not drop rows."""
    res = _listed(h, *([ROOT, OTHER] * 50))
    assert res.status == 200
    assert len(res.json["data"]["items"]) == 100


@pytest.mark.parametrize(
    "item",
    [
        {**PAAS, "quota": "100"},
        {**PAAS, "used_quota": None},
        {**PAAS, "request_count": True},
        {**PAAS, "status": 1.0},
        {**PAAS, "id": "42"},
        {**PAAS, "id": 2**31},
        {**PAAS, "DeletedAt": "yesterday; webhook_secret=x"},
        {**PAAS, "username": USERNAME + " "},
        {key: value for key, value in PAAS.items() if key != "quota"},
    ],
)
def test_a_paas_user_with_unexpected_values_is_masked(h, item):
    res = _listed(h, item)
    assert res.status == 200
    masked = res.json["data"]["items"][0]
    assert masked["username"] is None
    assert set(masked) == {"id", "username"}


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        b"[1, 2]",
        b'{"success": true, "data": []}',
        b'{"success": true, "data": {"items": null}}',
        b'{"success": true, "data": {"total": 3}}',
        b'{"success": true, "data": {"items": [1]}}',
        b'{"success": true, "data": {"items": [], "total": "3"}}',
        b'{"success": true, "data": {"items": [], "total": -1}}',
        b'{"success": true, "success": true, "data": {"items": []}}',
        b'{"success": true, "data": {"items": [], "total": NaN}}',
    ],
)
def test_the_wrong_shape_fails_closed(h, body):
    h.upstream.on(
        "GET",
        "/api/user/",
        httpx.Response(200, content=body + WEBHOOK_SECRET.encode(), headers={"Content-Type": "application/json"})
        if body.startswith(b"<")
        else httpx.Response(200, content=body, headers={"Content-Type": "application/json"}),
    )
    res = h.signed("GET", "/api/user/", LIST, identity="ap")
    assert (res.status, res.proxy_error) == (500, "internal")
    assert res.json["reason"].startswith("response_")
    assert WEBHOOK_SECRET.encode() not in res.body


def test_refusals_keep_only_what_the_client_classifies_by(h):
    h.upstream.on(
        "GET",
        "/api/user/",
        httpx.Response(
            401,
            json={
                "success": False,
                "message": "access token expired",
                "code": "ACCESS_TOKEN_EXPIRED",
                "data": [ROOT],
                "setting": WEBHOOK_SECRET,
            },
        ),
    )
    res = h.signed("GET", "/api/user/", LIST, identity="ap")
    assert res.status == 401
    assert res.proxy_error is None
    assert res.json == {"success": False, "message": "access token expired", "code": "ACCESS_TOKEN_EXPIRED"}
    h.upstream.on(
        "GET",
        "/api/user/",
        httpx.Response(200, json={"success": False, "message": "db", "message_key": "Database error", "data": [ROOT]}),
    )
    res = h.signed("GET", "/api/user/", LIST, identity="ap")
    assert res.json == {"success": False, "message": "db", "message_key": "Database error"}
    h.upstream.on("GET", "/api/user/", httpx.Response(500, json={"error": {"type": "new_api_panic", "message": "x"}}))
    res = h.signed("GET", "/api/user/", LIST, identity="ap")
    assert (res.status, res.json) == (500, {"error": {"type": "new_api_panic"}})
    h.upstream.on("GET", "/api/user/", httpx.Response(500, content=b""))
    res = h.signed("GET", "/api/user/", LIST, identity="ap")
    assert (res.status, res.body, res.proxy_error) == (500, b"", None)


def test_other_routes_are_still_relayed_as_is(h):
    h.upstream.on("GET", "/api/user/42", ok(PAAS))
    res = h.signed("GET", "/api/user/42", identity="ap")
    assert res.json["data"] == PAAS


def test_the_filter_itself_is_pure():
    settings, _warnings = load_settings(base_env(), NOW)
    body = json.dumps({"success": True, "message": "", "data": {"items": [ROOT, PAAS]}}).encode()
    out = json.loads(responses.user_list(200, body, settings))
    assert out["data"]["items"][0] == {"id": 1, "username": None}
    assert out["data"]["items"][1]["username"] == USERNAME
    assert "total" not in out["data"]
    assert responses.user_list(502, b"  ", settings) == b"  "
