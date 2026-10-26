"""Cloudflare Custom Hostnames API contract and security boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.custom_domains import normalize_hostname
from src.api import custom_domains as api
from src.config import settings
from src.main import app


API_KEY = "custom-domain-test-key"
HEADERS = {"X-API-Key": API_KEY}
BASE = "/api/custom-domains"
ROUTE = {"namespace": "paas-ws-demo", "service_name": "odoo-demo"}
DOMAIN = {"hostname": "shop.customer.example",
          "origin_hostname": "tenant.origin.example.net"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_key", API_KEY)
    monkeypatch.setattr(settings, "custom_domains_enabled", True)
    monkeypatch.setattr(settings, "cloudflare_api_token", "provider-secret")
    monkeypatch.setattr(settings, "cloudflare_zone_id", "zone-123")
    monkeypatch.setattr(settings, "cloudflare_domain", "origin.example.net")
    monkeypatch.setattr(settings, "custom_domains_target_hostname", "customers.origin.example.net")
    monkeypatch.setattr("src.api.custom_domains.gateway_configured", lambda: True)
    monkeypatch.setattr("src.api.custom_domains.route_loaded", AsyncMock(return_value=True))
    monkeypatch.setattr("src.api.custom_domains._edge_ready", AsyncMock(return_value=True))
    return TestClient(app)


def cf_response(status_code=200, *, result=None, success=True):
    return httpx.Response(
        status_code,
        json={"success": success, "errors": [], "result": result},
        request=httpx.Request("GET", "https://api.cloudflare.com"),
    )


def resource(**changes):
    value = {
        "id": "provider_123",
        "hostname": "shop.customer.example",
        "status": "pending",
        "custom_origin_server": "customers.origin.example.net",
        "ownership_verification": {
            "type": "txt",
            "name": "_cf-custom-hostname.shop.customer.example",
            "value": "ownership-token",
        },
        "ssl": {
            "status": "pending_validation",
            "validation_records": [{
                "txt_name": "_acme-challenge.shop.customer.example",
                "txt_value": "ssl-token",
            }],
            "certificate": "must-not-leak",
        },
        "unrelated_secret": "must-not-leak",
    }
    value.update(changes)
    return value


def test_capabilities_is_authenticated_safe_and_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "api_key", API_KEY)
    monkeypatch.setattr(settings, "custom_domains_enabled", False)
    monkeypatch.setattr(settings, "cloudflare_api_token", "")
    monkeypatch.setattr(settings, "cloudflare_zone_id", "")
    monkeypatch.setattr(settings, "cloudflare_domain", "origin.example.net")
    monkeypatch.setattr(settings, "custom_domains_target_hostname", "customers.origin.example.net")
    monkeypatch.setattr("src.api.custom_domains.gateway_configured", lambda: False)
    response = TestClient(app).get(f"{BASE}/capabilities", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "target_hostname": "customers.origin.example.net",
        "supported": False,
        "reason": "dedicated_gateway_not_configured",
        "dcv_delegation_hostname": None,
    }
    assert TestClient(app).get(f"{BASE}/capabilities").status_code == 401


def test_delegated_dcv_replaces_rotating_txt_and_preserves_hostname_proof(client, monkeypatch):
    suffix = "ffe2fa273c1a1bd6.dcv.cloudflare.com"
    monkeypatch.setattr(settings, "custom_domains_dcv_delegation_hostname", suffix)
    result = api._dto(resource(), DOMAIN["origin_hostname"])
    records = [record.model_dump() for record in result.validation_records]
    assert records == [
        {"type": "TXT", "name": "_cf-custom-hostname.shop.customer.example", "value": "ownership-token"},
        {"type": "CNAME", "name": "_acme-challenge.shop.customer.example",
         "value": f"shop.customer.example.{suffix}"},
    ]
    response = client.get(f"{BASE}/capabilities", headers=HEADERS)
    assert response.json()["dcv_delegation_hostname"] == suffix


def test_invalid_delegation_suffix_closes_onboarding(client, monkeypatch):
    monkeypatch.setattr(settings, "custom_domains_dcv_delegation_hostname", "bad.example.com")
    assert api._configured() is False


def test_post_fails_closed_when_gateway_is_not_configured(client, monkeypatch):
    monkeypatch.setattr("src.api.custom_domains.gateway_configured", lambda: False)
    with patch("httpx.AsyncClient.request", AsyncMock()) as mocked:
        response = client.post(
            BASE,
            headers=HEADERS,
            json={**DOMAIN, **ROUTE},
        )
    assert response.status_code == 503
    mocked.assert_not_called()


def test_post_never_creates_provider_hostname_until_gateway_loaded(client, monkeypatch):
    monkeypatch.setattr("src.api.custom_domains.route_loaded", AsyncMock(return_value=False))
    with patch("httpx.AsyncClient.request", AsyncMock()) as mocked:
        response = client.post(BASE, headers=HEADERS, json={**DOMAIN, **ROUTE})
    assert response.status_code == 409
    mocked.assert_not_called()


async def test_cloudflare_client_bare_create_contract_uses_txt_dv():
    """Lock the provider contract independently of the fail-closed API gate."""
    from src.services.custom_domains import CustomDomainsClient

    mocked = AsyncMock(return_value=cf_response(201, result=resource()))
    with patch("httpx.AsyncClient.request", mocked):
        result = await CustomDomainsClient(
            zone_id="zone-123", api_token="provider-secret"
        ).create("shop.customer.example", "customers.origin.example.net")
    assert result["id"] == "provider_123"
    mocked.assert_awaited_once()
    args, kwargs = mocked.await_args
    assert args[:2] == (
        "POST",
        "https://api.cloudflare.com/client/v4/zones/zone-123/custom_hostnames",
    )
    assert kwargs["json"] == {
        "hostname": "shop.customer.example",
        "custom_origin_server": "customers.origin.example.net",
        "ssl": {"method": "txt", "type": "dv"},
    }


async def test_fallback_origin_probe_is_read_only_and_requires_valid_resource():
    from src.services.custom_domains import CustomDomainsClient

    mocked = AsyncMock(return_value=cf_response(result={
        "origin": "customers.origin.example.net", "status": "active"}))
    with patch("httpx.AsyncClient.request", mocked):
        result = await CustomDomainsClient(
            zone_id="zone-123", api_token="provider-secret"
        ).get_fallback_origin()
    assert result["status"] == "active"
    assert mocked.await_args.args[:2] == (
        "GET",
        "https://api.cloudflare.com/client/v4/zones/zone-123/custom_hostnames/fallback_origin",
    )


def test_custom_hostname_credential_is_separate_from_tunnel_credential(monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_api_token", "tunnel-only")
    monkeypatch.setattr(settings, "custom_domains_api_token", "ssl-only")
    assert api._client().api_token == "ssl-only"
    assert settings.cloudflare_api_token == "tunnel-only"
    monkeypatch.setattr(settings, "custom_domains_api_token", "")
    assert api._client().api_token == "tunnel-only"


async def test_edge_readiness_requires_active_fallback_and_exact_tunnel(monkeypatch):
    tunnel_id = "11111111-1111-4111-8111-111111111111"
    target = "customers.origin.example.net"
    gateway_url = "http://paas-custom-domain-gateway.paas-cloudflare-system.svc.cluster.local:8080"
    monkeypatch.setattr(settings, "custom_domains_target_hostname", target)
    monkeypatch.setattr(settings, "cloudflare_domain", "origin.example.net")
    monkeypatch.setattr(settings, "custom_domains_gateway_url", gateway_url)
    monkeypatch.setattr(settings, "custom_domains_gateway_tunnel_id", tunnel_id)
    monkeypatch.setattr(api, "_configured", lambda: True)
    cf = SimpleNamespace(
        enabled=True, dns_enabled=True,
        get_tunnel_config=AsyncMock(return_value={"config": {"ingress": [
            {"service": gateway_url}]} }),
        get_dns_record=AsyncMock(return_value={
            "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True}),
    )
    provider = SimpleNamespace(
        can_manage=AsyncMock(return_value=True),
        get_fallback_origin=AsyncMock(return_value={
            "origin": target, "status": "active"}),
    )
    monkeypatch.setattr(api, "CloudflareService", lambda: cf)
    monkeypatch.setattr(api, "_client", lambda: provider)
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(return_value=httpx.Response(
        200, request=httpx.Request("GET", f"{gateway_url}/healthz"))))

    async def ready():
        monkeypatch.setattr(api, "_edge_cache", (0.0, False))
        return await api._edge_ready()

    assert await ready() is True
    cf.get_dns_record.assert_awaited_with(target, raise_on_error=True)
    cf.get_tunnel_config.return_value = {"config": {"ingress": [
        {"hostname": "only.example.net", "service": gateway_url}]} }
    assert await ready() is False
    cf.get_tunnel_config.return_value = {"config": {"ingress": [
        {"service": gateway_url}]} }
    provider.get_fallback_origin.return_value = {"origin": target, "status": "pending_deployment"}
    assert await ready() is False
    provider.get_fallback_origin.return_value = {"origin": "other.origin.example.net", "status": "active"}
    assert await ready() is True
    for invalid in (None, {}, {"origin": "attacker.example", "status": "active"},
                    {"origin": "", "status": "active"}):
        provider.get_fallback_origin.return_value = invalid
        assert await ready() is False
    provider.get_fallback_origin.return_value = {"origin": "other.origin.example.net", "status": "pending_deployment"}
    assert await ready() is False
    provider.get_fallback_origin.return_value = {"origin": target, "status": "active"}
    provider.can_manage.return_value = False
    assert await ready() is False


@pytest.mark.parametrize("hostname", [
    "https://shop.customer.example", "*.customer.example", "127.0.0.1",
    "shop.customer.example:443", "localhost", "bad_label.customer.example",
])
def test_strict_hostname_rejection(client, hostname):
    with patch("httpx.AsyncClient.request", AsyncMock()) as mocked:
        response = client.post(
            BASE,
            headers=HEADERS,
            json={**DOMAIN, **ROUTE, "hostname": hostname},
        )
    assert response.status_code == 422
    mocked.assert_not_called()


def test_provider_owned_customer_and_untrusted_origin_are_rejected(client):
    for payload in (
        {**DOMAIN, **ROUTE, "hostname": "customer.origin.example.net"},
        {**DOMAIN, **ROUTE, "origin_hostname": "attacker.example.org"},
    ):
        with patch("httpx.AsyncClient.request", AsyncMock()) as mocked:
            response = client.post(BASE, headers=HEADERS, json=payload)
        assert response.status_code == 422
        mocked.assert_not_called()


def test_get_requires_exact_provider_resource_identity(client):
    mocked = AsyncMock(return_value=cf_response(result=resource()))
    with patch("httpx.AsyncClient.request", mocked):
        ok = client.get(
            f"{BASE}/provider_123",
            headers=HEADERS,
            params={**DOMAIN, **ROUTE},
        )
    assert ok.status_code == 200
    assert ok.json()["route_ready"] is True
    assert ok.json()["validation_records"] == [
        {"type": "TXT", "name": "_cf-custom-hostname.shop.customer.example", "value": "ownership-token"},
        {"type": "TXT", "name": "_acme-challenge.shop.customer.example", "value": "ssl-token"},
    ]

    mocked = AsyncMock(return_value=cf_response(result=resource()))
    with patch("httpx.AsyncClient.request", mocked):
        mismatch = client.get(
            f"{BASE}/provider_123",
            headers=HEADERS,
            params={**DOMAIN, **ROUTE, "hostname": "other.customer.example"},
        )
    assert mismatch.status_code == 409

    # A provider object pointing at a tenant's old canonical ingress would
    # bypass the dedicated gateway, even though its hostname/ID still match.
    wrong_origin = AsyncMock(return_value=cf_response(result=resource(
        custom_origin_server="tenant.origin.example.net")))
    with patch("httpx.AsyncClient.request", wrong_origin):
        unsafe = client.get(f"{BASE}/provider_123", headers=HEADERS,
                            params={**DOMAIN, **ROUTE})
    assert unsafe.status_code == 502


def test_delete_gets_and_checks_identity_before_delete(client):
    mocked = AsyncMock(side_effect=[
        cf_response(result=resource()),
        cf_response(202, result={"id": "provider_123"}),
    ])
    with patch("httpx.AsyncClient.request", mocked):
        response = client.delete(
            f"{BASE}/provider_123",
            headers=HEADERS,
            params={"hostname": "shop.customer.example", "origin_hostname": "tenant.origin.example.net"},
        )
    assert response.status_code == 204
    assert [call.args[0] for call in mocked.await_args_list] == ["GET", "DELETE"]


def test_delete_is_idempotent_when_stored_provider_id_is_absent(client):
    mocked = AsyncMock(return_value=cf_response(404, result=None, success=False))
    with patch("httpx.AsyncClient.request", mocked):
        response = client.delete(
            f"{BASE}/provider_123",
            headers=HEADERS,
            params={"hostname": "shop.customer.example", "origin_hostname": "tenant.origin.example.net"},
        )
    assert response.status_code == 204
    mocked.assert_awaited_once()
    assert mocked.await_args.args[0] == "GET"


def test_kill_switch_still_permits_existing_domain_cleanup(client, monkeypatch):
    monkeypatch.setattr(settings, "custom_domains_enabled", False)
    mocked = AsyncMock(side_effect=[
        cf_response(result=resource()),
        cf_response(202, result={"id": "provider_123"}),
    ])
    with patch("httpx.AsyncClient.request", mocked):
        response = client.delete(
            f"{BASE}/provider_123", headers=HEADERS,
            params={"hostname": "shop.customer.example",
                    "origin_hostname": "tenant.origin.example.net"},
        )
    assert response.status_code == 204
    assert [call.args[0] for call in mocked.await_args_list] == ["GET", "DELETE"]


def test_ambiguous_create_transport_failure_is_not_retried_or_adopted(client):
    mocked = AsyncMock(side_effect=httpx.ReadTimeout("unknown outcome"))
    with patch("httpx.AsyncClient.request", mocked):
        response = client.post(
            BASE,
            headers=HEADERS,
            json={**DOMAIN, **ROUTE},
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Custom domain provider request failed"
    mocked.assert_awaited_once()
    assert mocked.await_args.args[0] == "POST"


def test_create_response_identity_mismatch_is_quarantined(client):
    mocked = AsyncMock(return_value=cf_response(
        201, result=resource(hostname="other.customer.example")))
    with patch("httpx.AsyncClient.request", mocked):
        response = client.post(
            BASE,
            headers=HEADERS,
            json={**DOMAIN, **ROUTE},
        )
    assert response.status_code == 502
    mocked.assert_awaited_once()


def test_fallback_target_must_be_dedicated_subdomain(client, monkeypatch):
    monkeypatch.setattr(settings, "custom_domains_target_hostname", "origin.example.net")
    response = client.get(f"{BASE}/capabilities", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_idna_normalization_is_stable():
    assert normalize_hostname("BÜCHER.example") == "xn--bcher-kva.example"
