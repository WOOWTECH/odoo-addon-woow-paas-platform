"""Dedicated gateway route registry: ownership, CAS, and rollout guards."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.services import custom_domain_gateway as gateway
from src.services.helm import KubectlException


HOST = "shop.customer.example"
ORIGIN = "tenant.origin.example.net"
NS = "paas-ws-demo"
SERVICE = "odoo-demo"
TARGET = {"namespace": NS, "service": SERVICE, "port": 80}


class FakeK8s:
    def __init__(self):
        self.routes = {}
        self.rv = 1
        self.conflicts = 0
        self.services = [{"name": SERVICE, "clusterIP": "10.43.1.2",
                          "ports": [{"port": 80, "protocol": "TCP"}]}]

    def get_services(self, namespace, label_selector=None):
        assert namespace == NS
        assert label_selector == f"app.kubernetes.io/instance={SERVICE}"
        return self.services

    def _run_command(self, args, input_data=None):
        if args[:2] == ["get", "configmap"]:
            body = {"apiVersion": "v1", "kind": "ConfigMap",
                    "metadata": {"name": gateway.CONFIGMAP_NAME,
                                 "namespace": gateway.gateway_namespace(),
                                 "resourceVersion": str(self.rv)},
                    "data": {gateway.ROUTES_KEY: json.dumps(self.routes)}}
            return type("Result", (), {"stdout": json.dumps(body)})()
        assert args[:2] == ["replace", "--filename"]
        if self.conflicts:
            self.conflicts -= 1
            raise KubectlException("conflict", "kubectl replace", "Conflict")
        body = json.loads(input_data)
        assert body["metadata"]["resourceVersion"] == str(self.rv)
        self.routes = json.loads(body["data"][gateway.ROUTES_KEY])
        self.rv += 1
        return type("Result", (), {"stdout": ""})()


@pytest.fixture
def configured(monkeypatch):
    fake = FakeK8s()
    monkeypatch.setattr(settings, "paas_env", "prod")
    monkeypatch.setattr(settings, "cloudflared_namespace", "")
    monkeypatch.setattr(settings, "namespace_prefix", "paas-ws-")
    monkeypatch.setattr(settings, "custom_domains_gateway_url",
                        "http://paas-custom-domain-gateway.paas-cloudflare-system.svc.cluster.local:8080")
    monkeypatch.setattr(settings, "custom_domains_gateway_token", "route-test-token")
    monkeypatch.setattr(settings, "custom_domains_gateway_tunnel_id",
                        "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(settings, "cloudflare_tunnel_id",
                        "22222222-2222-4222-8222-222222222222")
    monkeypatch.setattr(gateway, "KubernetesService", lambda: fake)
    monkeypatch.setattr(gateway, "_verified_public_route", AsyncMock())
    return fake


def test_publish_is_idempotent_and_uses_resource_version_cas(configured):
    configured.conflicts = 1
    asyncio.run(gateway.publish(HOST, ORIGIN, NS, SERVICE))
    assert configured.routes == {HOST: TARGET}
    assert configured.rv == 2
    asyncio.run(gateway.publish(HOST, ORIGIN, NS, SERVICE))
    assert configured.rv == 2


def test_cross_service_takeover_and_unpublish_are_rejected(configured):
    configured.routes[HOST] = {"namespace": "paas-ws-other",
                               "service": "other-odoo", "port": 80}
    with pytest.raises(gateway.GatewayRouteError, match="already belongs"):
        asyncio.run(gateway.publish(HOST, ORIGIN, NS, SERVICE))
    with pytest.raises(gateway.GatewayRouteError, match="belongs to another"):
        asyncio.run(gateway.unpublish(HOST, NS, SERVICE))
    assert HOST in configured.routes


def test_namespace_and_service_constraints_fail_before_kubernetes_or_cloudflare(configured):
    for namespace, service in (("kube-system", SERVICE),
                               ("paas-ws-stg-demo", SERVICE),
                               (NS, "127.0.0.1"), (NS, "bad/name")):
        with pytest.raises(gateway.GatewayRouteError):
            asyncio.run(gateway.publish(HOST, ORIGIN, namespace, service))
    assert configured.routes == {}


def test_gateway_requires_matching_environment_and_distinct_tunnel(configured, monkeypatch):
    assert gateway.gateway_configured() is True
    monkeypatch.setattr(settings, "namespace_prefix", "paas-ws-stg-")
    assert gateway.gateway_configured() is False
    monkeypatch.setattr(settings, "namespace_prefix", "paas-ws-")
    monkeypatch.setattr(settings, "custom_domains_gateway_tunnel_id",
                        settings.cloudflare_tunnel_id.upper())
    assert gateway.gateway_configured() is False


def test_target_must_be_non_headless_release_owned_odoo_service(configured):
    configured.services = [{"name": SERVICE, "clusterIP": "None",
                            "ports": [{"port": 80, "protocol": "TCP"}]}]
    with pytest.raises(gateway.GatewayRouteError, match="not ready"):
        asyncio.run(gateway.publish(HOST, ORIGIN, NS, SERVICE))
    configured.services = [{"name": SERVICE, "clusterIP": "10.43.1.2",
                            "ports": [{"port": 5432, "protocol": "TCP"}]}]
    with pytest.raises(gateway.GatewayRouteError, match="not ready"):
        asyncio.run(gateway.publish(HOST, ORIGIN, NS, SERVICE))


def test_gateway_must_observe_mount_before_provider_create(configured, monkeypatch):
    configured.routes[HOST] = TARGET

    async def fake_get(_self, url, **kwargs):
        assert url.endswith("/_internal/route")
        assert kwargs["headers"]["Authorization"] == "Bearer route-test-token"
        return httpx.Response(200, json={"loaded": False},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert asyncio.run(gateway.route_loaded(HOST, NS, SERVICE, present=True)) is False


def test_gateway_route_endpoints_require_auth_and_cleanup_survives_kill_switch(
        configured, monkeypatch):
    monkeypatch.setattr(settings, "api_key", "api-test")
    monkeypatch.setattr(settings, "cloudflare_domain", "origin.example.net")
    monkeypatch.setattr(settings, "cloudflare_zone_id", "zone-test")
    monkeypatch.setattr(settings, "cloudflare_api_token", "provider-test")
    monkeypatch.setattr(settings, "custom_domains_target_hostname",
                        "customers.origin.example.net")
    monkeypatch.setattr(settings, "custom_domains_enabled", True)
    monkeypatch.setattr("src.api.custom_domains.publish", AsyncMock())
    monkeypatch.setattr("src.api.custom_domains.unpublish", AsyncMock())
    monkeypatch.setattr("src.api.custom_domains.route_loaded", AsyncMock(return_value=True))
    monkeypatch.setattr("src.api.custom_domains._edge_ready", AsyncMock(return_value=True))
    client = TestClient(app)
    body = {"hostname": HOST, "origin_hostname": ORIGIN,
            "namespace": NS, "service_name": SERVICE}
    assert client.post("/api/custom-domains/routes", json=body).status_code == 401
    assert client.post("/api/custom-domains/routes", json=body,
                       headers={"X-API-Key": "api-test"}).status_code == 204
    monkeypatch.setattr(settings, "custom_domains_enabled", False)
    response = client.request("DELETE", "/api/custom-domains/routes", json=body,
                              headers={"X-API-Key": "api-test"})
    assert response.status_code == 204
