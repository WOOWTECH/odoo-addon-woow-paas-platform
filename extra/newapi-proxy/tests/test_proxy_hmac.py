"""HMAC verification: headers, skew, replay, kid, tampering, key rotation."""

import pytest

from newapi_proxy import signing
from proxy_harness import CRON_KID, NOW, TENANT_IP, Harness, ok


@pytest.fixture
def h():
    harness = Harness()
    harness.upstream.on("GET", "/api/status", ok({"version": "v1.0.0-rc.44"}))
    return harness


def test_signed_anon_status_passes(h):
    res = h.signed("GET", "/api/status")
    assert res.status == 200
    assert res.json["success"] is True
    assert len(h.upstream.calls("GET", "/api/status")) == 1


@pytest.mark.parametrize("drop", ["x-woow-proxy-kid", "x-woow-proxy-ts", "x-woow-proxy-sig", "x-woow-identity"])
def test_missing_auth_header_is_401(h, drop):
    headers = [kv for kv in h.sign_headers("GET", "/api/status") if kv[0].lower() != drop]
    res = h.call("GET", b"/api/status", b"", headers)
    assert res.status == 401
    assert res.proxy_error == "auth"
    assert h.upstream.requests == []


def test_no_signature_at_all_is_401(h):
    res = h.call("GET", b"/api/status")
    assert res.status == 401
    assert res.json == {"success": False, "message": "newapi-proxy: auth", "proxy_error": "auth", "reason": "auth"}


@pytest.mark.parametrize("offset", [31, -31, 3600, -86400])
def test_expired_or_future_ts_is_401(h, offset):
    res = h.signed("GET", "/api/status", ts=NOW + offset)
    assert res.status == 401
    assert h.upstream.requests == []


@pytest.mark.parametrize("offset", [30, -30, 0])
def test_ts_within_30s_accepted(h, offset):
    assert h.signed("GET", "/api/status", ts=NOW + offset).status == 200


def test_replay_within_60s_is_401(h):
    headers = h.sign_headers("GET", "/api/status")
    assert h.call("GET", b"/api/status", b"", headers).status == 200
    h.mono += 10
    again = h.call("GET", b"/api/status", b"", headers)
    assert again.status == 401
    assert again.proxy_error == "auth"
    assert len(h.upstream.calls()) == 1


def test_wrong_kid_is_401(h):
    headers = [
        (k, "cron-unknown" if k.lower() == "x-woow-proxy-kid" else v) for k, v in h.sign_headers("GET", "/api/status")
    ]
    assert h.call("GET", b"/api/status", b"", headers).status == 401


def test_right_kid_wrong_secret_is_401(h):
    message = signing.string_to_sign("GET", "/api/status", "", b"", str(NOW), "anon", "")
    headers = [
        ("X-Woow-Proxy-Kid", CRON_KID),
        ("X-Woow-Proxy-Ts", str(NOW)),
        ("X-Woow-Proxy-Sig", signing.sign(b"another-secret-of-sufficient-length-000", message)),
        ("X-Woow-Identity", "anon"),
    ]
    assert h.call("GET", b"/api/status", b"", headers).status == 401


def test_uppercase_or_short_signature_is_401(h):
    headers = h.sign_headers("GET", "/api/status")
    upper = [(k, v.upper() if k.lower() == "x-woow-proxy-sig" else v) for k, v in headers]
    assert h.call("GET", b"/api/status", b"", upper).status == 401
    short = [(k, v[:-2] if k.lower() == "x-woow-proxy-sig" else v) for k, v in headers]
    assert h.call("GET", b"/api/status", b"", short).status == 401


def test_tampered_fields_are_401(h):
    # Signed for /api/status, sent elsewhere / with another identity / body / query.
    headers = h.sign_headers("GET", "/api/user/search", [("keyword", "wsab0000000001")], identity="ap")
    assert h.call("GET", b"/api/user/search", b"keyword=wsab0000000002", headers).status == 401
    headers = h.sign_headers("GET", "/api/status", identity="anon")
    swapped = [(k, "ap" if k.lower() == "x-woow-identity" else v) for k, v in headers]
    assert h.call("GET", b"/api/status", b"", swapped).status == 401
    headers = h.sign_headers("POST", "/api/token/", body=b"{}", identity="user", client_ip=TENANT_IP)
    assert h.call("POST", b"/api/token/", b"", headers, b'{"x":1}').status == 401
    headers = h.sign_headers("GET", "/api/token/1", identity="user", client_ip=TENANT_IP)
    other_ip = [(k, "198.18.17.6" if k.lower() == "x-woow-client-ip" else v) for k, v in headers]
    assert h.call("GET", b"/api/token/1", b"", other_ip).status == 401
    assert h.upstream.requests == []


def test_next_key_in_list_is_accepted_for_rotation(h):
    res = h.user("GET", "/api/token/7", key="web_next")
    assert res.status == 200


def test_duplicate_auth_header_is_400(h):
    headers = h.sign_headers("GET", "/api/status")
    headers.append(("X-Woow-Identity", "anon"))
    res = h.call("GET", b"/api/status", b"", headers)
    assert res.status == 400
    assert res.json["reason"] == "duplicate_header"


def test_header_with_newline_style_garbage_is_rejected_without_echo(h):
    headers = [(k, v) for k, v in h.sign_headers("GET", "/api/status") if k.lower() != "x-woow-proxy-kid"]
    headers.append(("X-Woow-Proxy-Kid", "cron-2026a WebSecretAaaa0000Bbbb1111Cccc2222Dddd3333"))
    res = h.call("GET", b"/api/status", b"", headers)
    assert res.status == 401
    assert b"WebSecret" not in res.body
