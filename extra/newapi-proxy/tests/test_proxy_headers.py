"""Header stripping and overwrite, synthetic IPs, credential injection (§1.4.2-2/-3/-6)."""

import httpx
import pytest

from proxy_harness import AP_TOKEN, OP_TOKEN, PROOF, TENANT_IP, USER_JWT, USER_PAT, Harness, ok

HOSTILE = [
    ("X-Forwarded-For", "114.32.21.18"),
    ("X-Real-IP", "114.32.21.18"),
    ("Forwarded", "for=114.32.21.18"),
    ("X-Forwarded-Host", "newapi.woowtech.io"),
    ("X-Forwarded-Proto", "https"),
    ("X-Forwarded-Port", "443"),
    ("CF-Connecting-IP", "114.32.21.18"),
    ("True-Client-IP", "114.32.21.18"),
    ("Cookie", "session=stolen"),
    ("New-Api-User", "1"),
    ("X-Security-Something", "x"),
    ("Origin", "https://evil.example"),
    ("X-Woow-Extra", "should-not-pass"),
    ("Accept-Language", "en"),
]

ALLOWED_UPSTREAM_HEADERS = {
    "host",
    "x-forwarded-for",
    "accept",
    "accept-encoding",
    "user-agent",
    "authorization",
    "content-type",
    "content-length",
    "connection",
    "x-security-proof",
}


@pytest.fixture
def h():
    return Harness()


def forwarded(h) -> httpx.Request:
    assert len(h.upstream.requests) == 1
    return h.upstream.requests[0]


def test_hostile_headers_dropped_and_xff_overwritten_for_user(h):
    res = h.user("GET", "/api/token/5", headers=HOSTILE)
    assert res.status == 200
    req = forwarded(h)
    assert req.headers.get_list("x-forwarded-for") == [TENANT_IP]
    assert set(req.headers.keys()) <= ALLOWED_UPSTREAM_HEADERS, set(req.headers.keys())
    assert "cookie" not in req.headers
    assert not [k for k in req.headers.keys() if k.startswith("x-woow")]
    assert req.headers["authorization"] == f"Bearer {USER_PAT}"


@pytest.mark.parametrize(
    "identity, method, path, query, ip, auth",
    [
        ("anon", "GET", "/api/status", [], "198.18.0.2", None),
        ("ap", "GET", "/api/user/9", [], "198.18.0.1", f"Bearer {AP_TOKEN}"),
        ("op", "GET", "/api/option/", [], "198.18.0.3", f"Bearer {OP_TOKEN}"),
    ],
)
def test_system_identities_get_fixed_ip_and_injected_credentials(h, identity, method, path, query, ip, auth):
    res = h.signed(method, path, query, identity=identity, headers=HOSTILE)
    assert res.status == 200
    req = forwarded(h)
    assert req.headers.get_list("x-forwarded-for") == [ip]
    assert req.headers.get("authorization") == auth
    assert set(req.headers.keys()) <= ALLOWED_UPSTREAM_HEADERS


@pytest.mark.parametrize("identity, path", [("ap", "/api/user/9"), ("op", "/api/option/"), ("anon", "/api/status")])
def test_system_identity_with_own_authorization_is_400(h, identity, path):
    res = h.signed("GET", path, identity=identity, headers=[("Authorization", "Bearer nap_" + "x" * 43)])
    assert res.status == 400
    assert res.json["reason"] == "authorization_not_allowed"
    assert h.upstream.requests == []


def test_system_identity_must_not_send_client_ip(h):
    res = h.signed("GET", "/api/user/9", identity="ap", client_ip=TENANT_IP)
    assert res.status == 400
    assert res.json["reason"] == "client_ip_not_allowed"


@pytest.mark.parametrize(
    "ip, reason",
    [
        ("", "client_ip_missing"),
        ("198.18.0.1", "client_ip_range"),  # AP's address
        ("198.18.0.255", "client_ip_range"),
        ("198.18.1.0", "client_ip_range"),  # prod tenant range, outside this stg proxy's range
        ("198.18.16.255", "client_ip_range"),  # stg block 0
        ("198.20.0.1", "client_ip_range"),
        ("114.32.21.18", "client_ip_range"),
        ("10.42.0.5", "client_ip_range"),
        ("198.018.17.5", "client_ip_format"),  # leading zero
        ("198.18.17", "client_ip_format"),
        ("198.18.17.256", "client_ip_format"),
    ],
)
def test_user_client_ip_validated(h, ip, reason):
    res = h.user("GET", "/api/token/5", client_ip=ip)
    assert res.status == 400
    assert res.json["reason"] == reason
    assert h.upstream.requests == []


@pytest.mark.parametrize("ip", ["198.18.17.0", "198.19.255.255", "198.19.0.1"])
def test_user_client_ip_range_bounds(h, ip):
    assert h.user("GET", "/api/token/5", client_ip=ip).status == 200


def test_prod_range_accepts_block_zero():
    h = Harness(PROXY_TENANT_IP_RANGE="198.18.1.0-198.19.255.255")
    assert h.user("GET", "/api/token/5", client_ip="198.18.1.0").status == 200
    assert h.user("GET", "/api/token/5", client_ip="198.18.0.255").status == 400


@pytest.mark.parametrize(
    "value",
    [
        "nap_short",
        "Basic dXNlcjpwYXNz",
        "Bearer",
        f"bearer {USER_PAT}",
        f"Bearer  {USER_PAT}",
        f"{USER_PAT}",
        "Bearer a.b.c",
    ],
)
def test_user_authorization_format(h, value):
    res = h.signed("GET", "/api/token/5", identity="user", headers=[("Authorization", value)])
    assert res.status == 400
    assert h.upstream.requests == []


def test_user_jwt_accepted(h):
    assert h.user("GET", "/api/user/access_tokens", auth=USER_JWT).status == 200


def test_user_route_requires_authorization(h):
    res = h.user("GET", "/api/token/5", auth=None)
    assert res.status == 400
    assert res.json["reason"] == "authorization_missing"


def test_user_may_not_present_the_service_tokens(h):
    for token in (AP_TOKEN, OP_TOKEN):
        res = h.user("GET", "/api/token/", auth=token)
        assert res.status == 403
        assert res.json["reason"] == "service_token"
    assert h.upstream.requests == []


def test_login_must_not_carry_authorization(h):
    body = {"username": "wsab0000000042", "password": "P" * 40}
    res = h.user("POST", "/api/user/login", auth=USER_PAT, body=body)
    assert res.status == 400
    assert h.user("POST", "/api/user/login", auth=None, body=body).status == 200
    assert "authorization" not in h.upstream.requests[0].headers


def test_proof_only_on_pat_create(h):
    res = h.user("GET", "/api/token/5", headers=[("X-Security-Proof", PROOF)])
    assert res.status == 400
    assert res.json["reason"] == "proof_not_allowed"


def test_response_headers_filtered(h):
    h.upstream.on(
        "GET",
        "/api/token/5",
        ok(
            {"id": 5},
            request_id="20261016123000abc",
            headers={
                "Set-Cookie": "session=abc; HttpOnly",
                "Retry-After": "12",
                "X-Internal": "leak",
                "Server": "gin",
            },
        ),
    )
    res = h.user("GET", "/api/token/5")
    assert res.status == 200
    assert "set-cookie" not in res.headers
    assert "x-internal" not in res.headers
    assert res.headers["x-oneapi-request-id"] == "20261016123000abc"
    assert res.headers["retry-after"] == "12"
    assert res.headers["cache-control"] == "no-store"
    assert "x-woow-proxy-error" not in res.headers


def test_upstream_status_and_body_pass_through(h):
    h.upstream.on("GET", "/api/token/5", httpx.Response(429, headers={"Retry-After": "30"}))
    res = h.user("GET", "/api/token/5")
    assert res.status == 429
    assert res.body == b""
    assert res.headers["retry-after"] == "30"
    assert "x-woow-proxy-error" not in res.headers


def test_read_timeout_header(h):
    assert h.user("GET", "/api/token/5", headers=[("X-Woow-Read-Timeout", "7")]).status == 200
    for bad in ("0", "31", "1.5", "abc", "07"):
        res = h.user("GET", "/api/token/5", headers=[("X-Woow-Read-Timeout", bad)])
        assert res.status == 400, bad
