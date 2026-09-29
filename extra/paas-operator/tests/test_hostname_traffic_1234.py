"""Per-hostname traffic (#1234): CF GraphQL analytics → route traffic.

The authz payload below is the verbatim shape Cloudflare returned on
2026-09-29 for the prod operator token (no Zone Analytics Read) — the UI must
be able to explain that state, so it is a value, not an exception.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.services import cf_analytics

HOSTS = ["woowsa-a.woowtech.io", "woowsa-b.woowtech.io"]

AUTHZ_PAYLOAD = {
    "data": None,
    "errors": [{
        "message": "Actor 'com.cloudflare.api.token.x' does not have permission "
                   "'com.cloudflare.api.account.zone.analytics.read' for zone z",
        "path": None,
        "extensions": {"code": "authz"},
    }],
}

OK_PAYLOAD = {
    "data": {"viewer": {"zones": [{
        "totals": [
            {"count": 10, "dimensions": {"clientRequestHTTPHost": "woowsa-a.woowtech.io", "edgeResponseStatus": 200}},
            {"count": 3, "dimensions": {"clientRequestHTTPHost": "woowsa-a.woowtech.io", "edgeResponseStatus": 502}},
            {"count": 1, "dimensions": {"clientRequestHTTPHost": "woowsa-a.woowtech.io", "edgeResponseStatus": 302}},
            {"count": 9, "dimensions": {"clientRequestHTTPHost": "other.woowtech.io", "edgeResponseStatus": 200}},
        ],
        "last0": [{"dimensions": {"datetimeMinute": "2026-09-29T00:50:00Z"}}],
        "last1": [],
    }]}},
    "errors": None,
}


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    cf_analytics._cache.clear()
    monkeypatch.setattr(cf_analytics.settings, "cloudflare_enabled", True)
    monkeypatch.setattr(cf_analytics.settings, "cloudflare_api_token", "test-token")
    monkeypatch.setattr(cf_analytics.settings, "cloudflare_zone_id", "test-zone")
    monkeypatch.setattr(cf_analytics.settings, "cloudflare_domain", "woowtech.io")
    yield
    cf_analytics._cache.clear()


def _post_returning(payload, status_code=200):
    return patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_Resp(payload, status_code)))


async def test_parses_totals_and_latest_minute():
    with _post_returning(OK_PAYLOAD) as post:
        res = await cf_analytics.get_hostname_traffic(HOSTS)
    assert res["available"] is True
    a, b = res["hosts"]["woowsa-a.woowtech.io"], res["hosts"]["woowsa-b.woowtech.io"]
    assert a["requests"] == 14 and a["status_2xx"] == 10 and a["status_5xx"] == 3 and a["status_3xx"] == 1
    assert a["last_request_at"] == "2026-09-29T00:50:00Z"
    assert b == {"requests": 0, "status_2xx": 0, "status_3xx": 0, "status_4xx": 0,
                 "status_5xx": 0, "last_request_at": None}
    assert "other.woowtech.io" not in res["hosts"]
    # hostnames travel as variables, never interpolated into the document
    body = post.await_args.kwargs["json"]
    assert "woowsa-a" not in body["query"]
    assert body["variables"]["h0"] == "woowsa-a.woowtech.io"


async def test_missing_permission_is_a_value():
    with _post_returning(AUTHZ_PAYLOAD):
        res = await cf_analytics.get_hostname_traffic(HOSTS)
    assert res["available"] is False
    assert res["reason"] == "permission"


async def test_transport_failure_is_a_value():
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=RuntimeError("boom"))):
        res = await cf_analytics.get_hostname_traffic(HOSTS)
    assert res == {"available": False, "reason": "error", "detail": "RuntimeError", "hosts": {}}


async def test_disabled_without_zone(monkeypatch):
    monkeypatch.setattr(cf_analytics.settings, "cloudflare_zone_id", "")
    with patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
        res = await cf_analytics.get_hostname_traffic(HOSTS)
    assert res["reason"] == "disabled"
    post.assert_not_awaited()


async def test_success_is_cached_failure_is_not():
    with _post_returning(AUTHZ_PAYLOAD) as post:
        await cf_analytics.get_hostname_traffic(HOSTS)
        await cf_analytics.get_hostname_traffic(HOSTS)
    assert post.await_count == 2, "a permission failure must not be cached (it may be fixed any minute)"
    with _post_returning(OK_PAYLOAD) as post:
        await cf_analytics.get_hostname_traffic(HOSTS)
        await cf_analytics.get_hostname_traffic(list(reversed(HOSTS)))
    assert post.await_count == 1


# ---- API layer ------------------------------------------------------------ #

@pytest.fixture
def client():
    from src.main import app
    with patch("src.main.settings.api_key", "test-api-key"):
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def test_api_returns_service_result(client):
    fake = AsyncMock(return_value={"available": False, "reason": "permission", "detail": "", "hosts": {}})
    with patch("src.api.analytics.cf_analytics.get_hostname_traffic", new=fake):
        resp = client.post("/api/analytics/traffic", json={"hostnames": HOSTS})
    assert resp.status_code == 200
    assert resp.json()["reason"] == "permission"
    fake.assert_awaited_once_with(HOSTS, 24)


def test_api_rejects_hostnames_outside_platform_domain(client):
    fake = AsyncMock()
    with patch("src.api.analytics.cf_analytics.get_hostname_traffic", new=fake):
        resp = client.post("/api/analytics/traffic",
                           json={"hostnames": ["woowsa-a.woowtech.io", "evil.example.com"]})
    assert resp.status_code == 422
    fake.assert_not_awaited()


def test_api_caps_hostname_count_and_window(client):
    too_many = [f"h{i}.woowtech.io" for i in range(cf_analytics.MAX_HOSTNAMES + 1)]
    assert client.post("/api/analytics/traffic", json={"hostnames": too_many}).status_code == 422
    assert client.post("/api/analytics/traffic",
                       json={"hostnames": HOSTS, "window_hours": 999}).status_code == 422
