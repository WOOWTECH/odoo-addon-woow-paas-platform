"""Off-default bare create public API contract for the durable SHC prototype."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from fastapi.testclient import TestClient

from src.main import app


def test_bare_create_returns_anchor_before_token_or_configuration():
    provider = Mock(
        api_token="synthetic-provider-key",
        account_id="synthetic-account",
        zone_id="synthetic-zone",
    )
    provider.create_tunnel = AsyncMock(return_value={
        "tunnel_id": "8d484941-70ad-493d-a672-c976a7c8c101",
        "tunnel_name": "paas-qa-sm-prototype-unique",
    })
    provider.get_tunnel_token = AsyncMock()
    provider.configure_tunnel_ingress = AsyncMock()
    provider.configure_tunnel = AsyncMock()
    provider.create_dns_record_for_tunnel = AsyncMock()
    settings = SimpleNamespace(shc_durable_provision_prototype_enabled=True)
    with patch("src.main.settings", SimpleNamespace(
        api_key="synthetic-api-key", namespace_prefix="dev-sandbox-shc-",
    )), patch("src.api.tunnels.settings", settings), patch(
        "src.api.tunnels.cloudflare_service", provider,
    ):
        client = TestClient(app, headers={"X-API-Key": "synthetic-api-key"})
        response = client.post("/api/tunnels/bare", json={
            "name": "paas-qa-sm-prototype-unique",
            "expected_account_id": "synthetic-account",
        })
    assert response.status_code == 201
    assert response.json() == {
        "tunnel_id": "8d484941-70ad-493d-a672-c976a7c8c101",
        "tunnel_name": "paas-qa-sm-prototype-unique",
        "provider_account_id": "synthetic-account",
    }
    provider.create_tunnel.assert_awaited_once_with(
        name="paas-qa-sm-prototype-unique",
    )
    provider.get_tunnel_token.assert_not_awaited()
    provider.configure_tunnel_ingress.assert_not_awaited()
    provider.configure_tunnel.assert_not_awaited()
    provider.create_dns_record_for_tunnel.assert_not_awaited()


@pytest.fixture
def bare_boundary():
    provider = Mock(api_token="synthetic-provider-key", account_id="synthetic-account")
    provider.create_tunnel = AsyncMock(return_value={
        "tunnel_id": "8d484941-70ad-493d-a672-c976a7c8c101",
        "tunnel_name": "paas-qa-sm-prototype-unique",
    })
    settings = SimpleNamespace(shc_durable_provision_prototype_enabled=True)
    auth_settings = SimpleNamespace(api_key="synthetic-api-key", namespace_prefix="dev-sandbox-shc-")
    with patch("src.main.settings", auth_settings), patch(
        "src.api.tunnels.settings", settings,
    ), patch("src.api.tunnels.cloudflare_service", provider):
        yield TestClient(app), provider, settings, auth_settings


def _request(client, **overrides):
    payload = {"name": "paas-qa-sm-prototype-unique", "expected_account_id": "synthetic-account"}
    payload.update(overrides)
    return client.post("/api/tunnels/bare", headers={"X-API-Key": "synthetic-api-key"}, json=payload)


@pytest.mark.parametrize("path", ["/api/tunnels/provider-binding", "/api/tunnels/bare"])
def test_prototype_disabled_does_not_call_provider(bare_boundary, path):
    client, provider, settings, _ = bare_boundary
    settings.shc_durable_provision_prototype_enabled = False
    if path.endswith("/bare"):
        response = _request(client)
    else:
        response = client.get(path, headers={"X-API-Key": "synthetic-api-key"})
    assert response.status_code == 404
    provider.create_tunnel.assert_not_awaited()


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_bare_create_requires_platform_api_key(bare_boundary, key):
    client, provider, _, _ = bare_boundary
    response = client.post("/api/tunnels/bare", headers={} if key is None else {"X-API-Key": key}, json={
        "name": "paas-qa-sm-prototype-unique", "expected_account_id": "synthetic-account",
    })
    assert response.status_code == 401
    provider.create_tunnel.assert_not_awaited()


def test_unconfigured_platform_key_fails_before_provider(bare_boundary):
    client, provider, _, auth_settings = bare_boundary
    auth_settings.api_key = ""
    assert _request(client).status_code == 503
    provider.create_tunnel.assert_not_awaited()


def test_provider_account_change_is_known_non_creation(bare_boundary):
    client, provider, _, _ = bare_boundary
    provider.account_id = "another-account"
    response = _request(client)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "provider_binding_changed", "stage": "preflight", "outcome": "not_created",
    }
    provider.create_tunnel.assert_not_awaited()


@pytest.mark.parametrize("field", ["api_token", "account_id"])
def test_missing_provider_configuration_never_creates(bare_boundary, field):
    client, provider, _, _ = bare_boundary
    setattr(provider, field, "")
    response = _request(client)
    assert response.status_code == 400
    assert response.json()["detail"]["outcome"] == "not_created"
    provider.create_tunnel.assert_not_awaited()


@pytest.mark.parametrize("result", [
    {},
    {"tunnel_id": "not-a-uuid", "tunnel_name": "paas-qa-sm-prototype-unique"},
    {"tunnel_id": "8d484941-70ad-493d-a672-c976a7c8c101", "tunnel_name": "foreign-name"},
])
def test_invalid_provider_identity_is_unknown_and_never_compensated(bare_boundary, result):
    client, provider, _, _ = bare_boundary
    provider.create_tunnel.return_value = result
    response = _request(client)
    assert response.status_code == 502
    assert response.json()["detail"]["outcome"] == "unknown"
    provider.create_tunnel.assert_awaited_once()
    provider.delete_tunnel.assert_not_called()


def test_provider_failure_is_sanitized_and_never_compensated(bare_boundary):
    client, provider, _, _ = bare_boundary
    provider.create_tunnel.side_effect = RuntimeError("synthetic-secret-bearing-provider-body")
    response = _request(client)
    assert response.status_code == 502
    assert "synthetic-secret-bearing-provider-body" not in response.text
    provider.delete_tunnel.assert_not_called()


def test_binding_only_returns_nonsecret_account_without_provider_io(bare_boundary):
    client, provider, _, _ = bare_boundary
    response = client.get("/api/tunnels/provider-binding", headers={"X-API-Key": "synthetic-api-key"})
    assert response.status_code == 200
    assert response.json() == {"provider_account_id": "synthetic-account"}
    provider.create_tunnel.assert_not_awaited()


def test_bare_request_rejects_legacy_hostname_or_route_side_effect_input(bare_boundary):
    client, provider, _, _ = bare_boundary
    assert _request(client, hostname="unsafe.invalid").status_code == 422
    provider.create_tunnel.assert_not_awaited()
