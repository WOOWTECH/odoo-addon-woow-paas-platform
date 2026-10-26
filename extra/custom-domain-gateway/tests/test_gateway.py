import gzip
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import main


HOST = "shop.customer.example"
ROUTE = {
    HOST: {
        "service": "wassa-odoo",
        "namespace": "paas-ws-abcd1234",
        "port": 80,
    }
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    routes_file = tmp_path / "routes.json"
    routes_file.write_text(json.dumps(ROUTE), encoding="utf-8")
    monkeypatch.setattr(main.settings, "routes_file", routes_file)
    monkeypatch.setattr(main.settings, "namespace_prefix", "paas-ws-")
    monkeypatch.setattr(main.settings, "route_check_token", "route-secret")
    monkeypatch.setattr(main.settings, "max_request_bytes", 1024)
    monkeypatch.setattr(main.settings, "max_response_bytes", 2048)
    monkeypatch.setattr(
        main,
        "route_store",
        main.RouteStore(routes_file, "paas-ws-"),
    )
    return TestClient(main.app), routes_file


def response(body=b"ok", status=200, headers=None):
    return httpx.Response(
        status,
        content=body,
        headers=headers or {},
        request=httpx.Request("GET", "http://upstream/"),
    )


def test_healthz_does_not_require_a_route_or_token(client):
    test_client, _ = client
    assert test_client.get("/healthz").json() == {"status": "ok"}


def test_unknown_host_is_404_without_upstream_call(client):
    test_client, _ = client
    with patch.object(main, "_send_upstream", AsyncMock()) as send:
        result = test_client.get("/x", headers={"host": "unknown.example"})
    assert result.status_code == 404
    send.assert_not_called()


@pytest.mark.parametrize("host", [
    "SHOP.customer.example",
    "shop.customer.example:443",
    "shop.customer.example.",
    "not-a-fqdn",
    "shop_customer.example",
])
def test_malformed_host_is_400(client, host):
    test_client, _ = client
    with patch.object(main, "_send_upstream", AsyncMock()) as send:
        result = test_client.get("/", headers={"host": host})
    assert result.status_code == 400
    send.assert_not_called()


def test_route_read_and_post_forwarding_preserve_host_path_query(client):
    test_client, _ = client
    send = AsyncMock(return_value=response(b"created", 201, {"x-upstream": "yes"}))
    with patch.object(main, "_send_upstream", send):
        result = test_client.post(
            "/web/action?debug=1&x=a%2Fb",
            headers={"host": HOST, "content-type": "application/json"},
            content=b'{"a":1}',
        )
    assert result.status_code == 201
    assert result.content == b"created"
    method, url, headers, body = send.await_args.args
    assert method == "POST"
    assert url == (
        "http://wassa-odoo.paas-ws-abcd1234.svc.cluster.local:80/"
        "web/action?debug=1&x=a%2Fb"
    )
    assert body == b'{"a":1}'
    forwarded = httpx.Headers(headers)
    assert forwarded["host"] == HOST
    assert forwarded["x-forwarded-proto"] == "https"


def test_percent_encoded_path_is_not_decoded_before_odoo_receives_it(client):
    test_client, _ = client
    send = AsyncMock(return_value=response())
    with patch.object(main, "_send_upstream", send):
        result = test_client.get("/website/%2Flogo.png", headers={"host": HOST})
    assert result.status_code == 200
    assert send.await_args.args[1].endswith("/website/%2Flogo.png")


def test_spoofed_forwarding_and_hop_headers_are_removed(client):
    test_client, _ = client
    send = AsyncMock(return_value=response())
    with patch.object(main, "_send_upstream", send):
        result = test_client.get(
            "/",
            headers={
                "host": HOST,
                "forwarded": "for=attacker",
                "x-forwarded-for": "127.0.0.1",
                "x-forwarded-host": "admin.internal",
                "x-forwarded-proto": "http",
                "x-real-ip": "127.0.0.1",
                "cf-connecting-ip": "127.0.0.1",
                "true-client-ip": "127.0.0.1",
                "connection": "x-remove-me, keep-alive",
                "x-remove-me": "secret",
                "te": "trailers",
            },
        )
    assert result.status_code == 200
    headers = httpx.Headers(send.await_args.args[2])
    assert headers["x-forwarded-proto"] == "https"
    for name in (
        "forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip",
        "cf-connecting-ip", "true-client-ip",
        "connection", "x-remove-me", "te",
    ):
        assert name not in headers


@pytest.mark.parametrize("bad_route", [
    {HOST: {"service": "bad_name", "namespace": "paas-ws-abcd1234", "port": 80}},
    {HOST: {"service": "wassa-odoo", "namespace": "kube-system", "port": 80}},
    {HOST: {"service": "wassa-odoo", "namespace": "paas-ws-stg-other", "port": 80}},
    {HOST: {"service": "wassa-odoo", "namespace": "paas-ws-abcd1234", "port": 8069}},
    {HOST: {"service": "http://attacker", "namespace": "paas-ws-abcd1234", "port": 80}},
])
def test_invalid_route_file_fails_closed(client, bad_route):
    test_client, routes_file = client
    routes_file.write_text(json.dumps(bad_route), encoding="utf-8")
    result = test_client.get("/", headers={"host": HOST})
    assert result.status_code == 404


def test_malformed_reload_drops_previous_route(client):
    test_client, routes_file = client
    with patch.object(main, "_send_upstream", AsyncMock(return_value=response())):
        assert test_client.get("/", headers={"host": HOST}).status_code == 200
    routes_file.write_text("{not-json", encoding="utf-8")
    with patch.object(main, "_send_upstream", AsyncMock()) as send:
        assert test_client.get("/", headers={"host": HOST}).status_code == 404
    send.assert_not_called()


def test_missing_bootstrap_file_is_empty_then_later_projected_key_loads(
    tmp_path, monkeypatch,
):
    """Helm owns ConfigMap metadata; operator may add routes.json afterwards."""
    routes_file = tmp_path / "projected" / "routes.json"
    monkeypatch.setattr(main.settings, "route_check_token", "route-secret")
    monkeypatch.setattr(
        main,
        "route_store",
        main.RouteStore(routes_file, "paas-ws-"),
    )
    test_client = TestClient(main.app)

    with patch.object(main, "_send_upstream", AsyncMock()) as send:
        assert test_client.get("/", headers={"host": HOST}).status_code == 404
    send.assert_not_called()
    status = test_client.get(
        f"/_internal/route?hostname={HOST}",
        headers={"authorization": "Bearer route-secret"},
    )
    assert status.json() == {"hostname": HOST, "loaded": False}

    routes_file.parent.mkdir()
    routes_file.write_text(json.dumps(ROUTE), encoding="utf-8")
    with patch.object(
        main, "_send_upstream", AsyncMock(return_value=response(b"ready"))
    ) as send:
        routed = test_client.get("/web", headers={"host": HOST})
    assert routed.status_code == 200
    assert routed.content == b"ready"
    send.assert_awaited_once()
    status = test_client.get(
        f"/_internal/route?hostname={HOST}",
        headers={"authorization": "Bearer route-secret"},
    )
    assert status.json() == {"hostname": HOST, "loaded": True}


def test_internal_route_requires_bearer_and_reports_loaded_state(client):
    test_client, _ = client
    endpoint = f"/_internal/route?hostname={HOST}"
    assert test_client.get(endpoint).status_code == 401
    assert test_client.get(endpoint, headers={"authorization": "Bearer wrong"}).status_code == 401
    loaded = test_client.get(
        endpoint,
        headers={"authorization": "Bearer route-secret"},
    )
    assert loaded.status_code == 200
    assert loaded.json() == {"hostname": HOST, "loaded": True}
    missing = test_client.get(
        "/_internal/route?hostname=missing.customer.example",
        headers={"authorization": "Bearer route-secret"},
    )
    assert missing.json() == {"hostname": "missing.customer.example", "loaded": False}


def test_request_and_response_limits(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(main.settings, "max_request_bytes", 3)
    assert test_client.post("/", headers={"host": HOST}, content=b"1234").status_code == 413

    monkeypatch.setattr(main.settings, "max_response_bytes", 3)
    with patch.object(main, "_send_upstream", AsyncMock(return_value=response(b"1234"))):
        assert test_client.get("/", headers={"host": HOST}).status_code == 502


def test_upstream_hop_by_hop_response_headers_are_removed(client):
    test_client, _ = client
    upstream = response(headers={
        "connection": "x-private",
        "x-private": "remove",
        "transfer-encoding": "chunked",
        "x-safe": "keep",
    })
    with patch.object(main, "_send_upstream", AsyncMock(return_value=upstream)):
        result = test_client.get("/", headers={"host": HOST})
    assert result.headers["x-safe"] == "keep"
    assert "x-private" not in result.headers
    assert "transfer-encoding" not in result.headers


def test_compressed_upstream_is_decoded_without_stale_encoding_metadata(client):
    test_client, _ = client
    payload = b"Odoo website asset"
    compressed = gzip.compress(payload)
    upstream = response(body=compressed, headers={
        "content-encoding": "gzip",
        "content-length": str(len(compressed)),
        "etag": '"gzip-asset"',
        "content-md5": "encoded-digest",
        "content-range": "bytes 0-10/11",
        "content-type": "text/plain",
    })
    with patch.object(main, "_send_upstream", AsyncMock(return_value=upstream)):
        result = test_client.get("/web/assets", headers={"host": HOST})
    assert result.status_code == 200
    assert result.content == payload
    assert result.headers["content-length"] == str(len(payload))
    assert result.headers["content-type"] == "text/plain"
    for key in ("content-encoding", "etag", "content-md5", "content-range"):
        assert key not in result.headers


def test_repeated_set_cookie_headers_are_preserved_for_odoo_sessions(client):
    test_client, _ = client
    upstream = response(headers=[
        ("set-cookie", "session_id=one; Path=/; HttpOnly"),
        ("set-cookie", "frontend_lang=zh_TW; Path=/"),
    ])
    with patch.object(main, "_send_upstream", AsyncMock(return_value=upstream)):
        result = test_client.get("/", headers={"host": HOST})
    assert result.headers.get_list("set-cookie") == [
        "session_id=one; Path=/; HttpOnly",
        "frontend_lang=zh_TW; Path=/",
    ]
