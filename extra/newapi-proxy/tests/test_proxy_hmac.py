"""HMAC verification: headers, skew, nonce, replay (local and across replicas), kid, tampering, rotation."""

import pytest

from newapi_proxy import signing
from proxy_harness import CRON_KID, NOW, TENANT_IP, WEB_KID, FakeReplayStore, Harness, ok

NONCE = "Nonce0123456789abcdefgh"


@pytest.fixture
def h():
    harness = Harness()
    harness.upstream.on("GET", "/api/status", ok({"version": "v1.0.0-rc.44"}))
    return harness


def rejection(h, reason):
    return h.proxy.metrics.get("newapi_proxy_rejections_total", kind="auth", reason=reason)


def test_signed_anon_status_passes(h):
    res = h.signed("GET", "/api/status")
    assert res.status == 200
    assert res.json["success"] is True
    assert len(h.upstream.calls("GET", "/api/status")) == 1


@pytest.mark.parametrize(
    "drop", ["x-woow-proxy-kid", "x-woow-proxy-ts", "x-woow-proxy-nonce", "x-woow-proxy-sig", "x-woow-identity"]
)
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


@pytest.mark.parametrize(
    "nonce", ["short", "x" * 21, "x" * 65, "has space nonce 0123456789", "plus+plus+plus+plus+plus", "é" * 22]
)
def test_malformed_nonce_is_401(h, nonce):
    headers = [
        (k, nonce if k.lower() == "x-woow-proxy-nonce" else v)
        for k, v in h.sign_headers("GET", "/api/status", nonce="A" * 22)
    ]
    res = h.call("GET", b"/api/status", b"", headers)
    assert res.status == 401
    assert h.upstream.requests == []


def test_identical_requests_in_the_same_second_both_pass(h):
    """Finding: two legitimate, byte-identical calls in one second (double
    click, two tabs, two cron threads) must not look like a replay."""
    first = h.sign_headers(
        "GET", "/api/token/", [("p", "1"), ("page_size", "10")], identity="user", client_ip=TENANT_IP, key="web"
    )
    second = h.sign_headers(
        "GET", "/api/token/", [("p", "1"), ("page_size", "10")], identity="user", client_ip=TENANT_IP, key="web"
    )
    sig = lambda hdrs: dict(hdrs)["X-Woow-Proxy-Sig"]  # noqa: E731
    assert sig(first) != sig(second)  # the nonce differs
    auth = [("Authorization", "Bearer nap_" + "u" * 43)]
    for headers in (first, second):
        res = h.call("GET", b"/api/token/", b"p=1&page_size=10", headers + auth)
        assert res.status == 200, res.body
    assert len(h.upstream.calls("GET", "/api/token/")) == 2


def test_replay_within_the_window_is_401(h):
    headers = h.sign_headers("GET", "/api/status")
    assert h.call("GET", b"/api/status", b"", headers).status == 200
    h.mono += 10
    again = h.call("GET", b"/api/status", b"", headers)
    assert again.status == 401
    assert again.proxy_error == "auth"
    assert rejection(h, "auth_replay") == 1
    assert len(h.upstream.calls()) == 1


def test_replay_record_is_kid_and_nonce_with_full_ttl(h):
    h.signed("GET", "/api/status", nonce=NONCE)
    assert h.replay_store.claims == [(f"{CRON_KID}:{NONCE}", signing.REPLAY_TTL_SECONDS)]
    assert signing.REPLAY_TTL_SECONDS >= 2 * signing.MAX_SKEW_SECONDS + 2


def test_replay_to_another_replica_is_401_even_for_add_quota():
    """Finding: with 2 replicas behind a Service, a replay used to reach the
    other replica about half the time — including add_quota (double credit)."""
    shared = FakeReplayStore()
    a, b = Harness(replay_store=shared), Harness(replay_store=shared)
    target = {"id": 123, "username": "wsab0000000123", "role": 1, "group": "default"}
    for replica in (a, b):
        replica.upstream.on("GET", "/api/user/123", ok(target))
    body = b'{"id":123,"action":"add_quota","mode":"add","value":15625}'
    headers = a.sign_headers("POST", "/api/user/manage", body=body, identity="ap")
    assert a.call("POST", b"/api/user/manage", b"", headers, body).status == 200
    replayed = b.call("POST", b"/api/user/manage", b"", headers, body)
    assert replayed.status == 401
    assert b.upstream.requests == []  # not even the pre-check
    assert len(a.upstream.calls("POST", "/api/user/manage")) == 1


def test_replay_just_before_the_skew_window_closes_is_401():
    """ts = now + 30 is accepted until wall time ts + 30; the record must
    outlive that (the old 60 s monotonic TTL let a replay through at ~60.5 s)."""
    h = Harness()
    h.upstream.on("GET", "/api/status", ok())
    headers = h.sign_headers("GET", "/api/status", ts=NOW + 30)
    assert h.call("GET", b"/api/status", b"", headers).status == 200
    h.now += 60  # wall clock: still |now - ts| <= 30
    h.mono += 60.5
    h.replay_store.now += 60.5
    again = h.call("GET", b"/api/status", b"", headers)
    assert again.status == 401
    assert rejection(h, "auth_replay") == 1
    assert len(h.upstream.calls()) == 1


def test_local_layer_still_catches_a_replay_when_valkey_forgot():
    # Valkey restarted (no persistence) between the two copies.
    h = Harness()
    h.upstream.on("GET", "/api/status", ok())
    headers = h.sign_headers("GET", "/api/status")
    assert h.call("GET", b"/api/status", b"", headers).status == 200
    h.replay_store.keys.clear()
    assert h.call("GET", b"/api/status", b"", headers).status == 401


@pytest.mark.parametrize("kind", ["connect", "timeout", "auth", "error"])
def test_replay_store_unavailable_fails_closed_as_busy(h, kind):
    h.replay_store.fail = kind
    res = h.signed("GET", "/api/status")
    assert res.status == 503
    assert res.proxy_error == "busy"
    assert res.json["reason"] == "replay_store"
    assert h.upstream.requests == []
    assert h.proxy.metrics.get("newapi_proxy_replay_store_errors_total", kind=kind) == 1
    # the client re-sends with a new nonce once Valkey is back
    h.replay_store.fail = None
    assert h.signed("GET", "/api/status").status == 200


def test_single_replica_without_valkey_uses_the_local_record_only():
    h = Harness(PROXY_REPLICAS="1", PROXY_VALKEY_ADDR="", PROXY_VALKEY_PASSWORD="")
    assert h.proxy.shared_replay is None
    h.upstream.on("GET", "/api/status", ok())
    headers = h.sign_headers("GET", "/api/status")
    assert h.call("GET", b"/api/status", b"", headers).status == 200
    assert h.call("GET", b"/api/status", b"", headers).status == 401


def test_wrong_kid_is_401(h):
    headers = [
        (k, "cron-unknown" if k.lower() == "x-woow-proxy-kid" else v) for k, v in h.sign_headers("GET", "/api/status")
    ]
    assert h.call("GET", b"/api/status", b"", headers).status == 401


def test_signature_is_bound_to_the_kid(h):
    # Re-labelling a valid signature with another known kid must fail.
    headers = [
        (k, WEB_KID if k.lower() == "x-woow-proxy-kid" else v)
        for k, v in h.sign_headers("GET", "/api/token/1", identity="user", client_ip=TENANT_IP, key="cron")
    ]
    headers.append(("Authorization", "Bearer nap_" + "u" * 43))
    assert h.call("GET", b"/api/token/1", b"", headers).status == 401


def test_right_kid_wrong_secret_is_401(h):
    message = signing.string_to_sign("GET", "/api/status", "", b"", str(NOW), NONCE, CRON_KID, "anon", "")
    headers = [
        ("X-Woow-Proxy-Kid", CRON_KID),
        ("X-Woow-Proxy-Ts", str(NOW)),
        ("X-Woow-Proxy-Nonce", NONCE),
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
    # Signed for /api/status, sent elsewhere / with another identity / body / query / nonce.
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
    headers = h.sign_headers("GET", "/api/status")
    other_nonce = [(k, "Z" * 22 if k.lower() == "x-woow-proxy-nonce" else v) for k, v in headers]
    assert h.call("GET", b"/api/status", b"", other_nonce).status == 401
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
