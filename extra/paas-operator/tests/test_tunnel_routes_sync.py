"""Tests for multi-route ingress sync on dedicated tunnels."""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareService


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.dns_enabled = True
    return svc


class TestConfigureTunnelIngress:
    @pytest.mark.asyncio
    async def test_builds_rules_with_host_header_and_catchall(self, cf_service):
        """Each route becomes one ingress rule; httpHostHeader set when flagged; catch-all 404 last."""
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True}

        async def fake_put(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

        with patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake_put)):
            await cf_service.configure_tunnel_ingress(
                tunnel_id="tun-1",
                rules=[
                    {"hostname": "a.woowtech.io", "service": "http://localhost:80",
                     "http_host_header": True},
                    {"hostname": "b.woowtech.io", "service": "http_status:503",
                     "http_host_header": False},
                ],
            )

        ingress = captured["payload"]["config"]["ingress"]
        assert len(ingress) == 3  # 2 routes + catch-all
        assert ingress[0]["hostname"] == "a.woowtech.io"
        assert ingress[0]["service"] == "http://localhost:80"
        assert ingress[0]["originRequest"] == {"httpHostHeader": "a.woowtech.io"}
        assert "originRequest" not in ingress[1]
        assert ingress[-1] == {"service": "http_status:404"}
        assert "tun-1/configurations" in captured["url"]

    @pytest.mark.asyncio
    async def test_raises_on_cf_error(self, cf_service):
        class FakeResponse:
            status_code = 400
            text = "bad"

            @staticmethod
            def json():
                return {"success": False}

        with patch("httpx.AsyncClient.put", new=AsyncMock(return_value=FakeResponse())):
            from src.services.cloudflare import CloudflareException
            with pytest.raises(CloudflareException):
                await cf_service.configure_tunnel_ingress(
                    tunnel_id="tun-1",
                    rules=[{"hostname": "a.woowtech.io",
                            "service": "http://localhost:80",
                            "http_host_header": True}],
                )


class TestCreateDnsRecordForTunnel:
    """Exercise the REAL create_dns_record_for_tunnel against a patched httpx
    client (not a mock of the method itself) so the comment-stamping and the
    per-tunnel CNAME target actually land in the outgoing payload — guards the
    BUG-8-class CF contract that method-level mocks in the endpoint tests hide.
    """

    @pytest.mark.asyncio
    async def test_create_stamps_comment_and_per_tunnel_target(self, cf_service):
        """New record → POST payload carries comment + this tunnel's target."""
        captured = {}

        class FakeResponse:
            status_code = 201

            @staticmethod
            def json():
                return {"success": True, "result": {"id": "rec-new"}}

        async def fake_post(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return FakeResponse()

        with patch.object(cf_service, "get_dns_record",
                          new=AsyncMock(return_value=None)), \
             patch("httpx.AsyncClient.post",
                   new=AsyncMock(side_effect=fake_post)):
            rec_id = await cf_service.create_dns_record_for_tunnel(
                subdomain="a", tunnel_id="tun-1", comment="paas-sa-x-y",
            )

        assert rec_id == "rec-new"
        assert captured["payload"]["comment"] == "paas-sa-x-y"
        assert captured["payload"]["content"] == "tun-1.cfargotunnel.com"
        assert captured["payload"]["name"] == "a"

    @pytest.mark.asyncio
    async def test_repoint_existing_uses_per_tunnel_target_not_shared(
        self, cf_service
    ):
        """Existing CNAME pointing at the WRONG tunnel must be repointed at
        THIS tunnel's target — not silently "fixed" onto the shared default
        (which would be a routing black hole). Regression guard for the
        _update_dns_record content-parameter fix.
        """
        cf_service.tunnel_id = "shared-tunnel"  # the shared/default target
        existing = {"id": "rec-1", "content": "other-tunnel.cfargotunnel.com"}
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"success": True}

        async def fake_put(url, headers=None, json=None, timeout=None):
            captured["payload"] = json
            return FakeResponse()

        with patch.object(cf_service, "get_dns_record",
                          new=AsyncMock(return_value=existing)), \
             patch("httpx.AsyncClient.put",
                   new=AsyncMock(side_effect=fake_put)):
            rec_id = await cf_service.create_dns_record_for_tunnel(
                subdomain="a", tunnel_id="tun-1",
            )

        assert rec_id == "rec-1"
        # MUST land on the dedicated tunnel, NOT the shared default.
        assert captured["payload"]["content"] == "tun-1.cfargotunnel.com"
        assert captured["payload"]["content"] != "shared-tunnel.cfargotunnel.com"


class TestReconcileTunnelDns:
    @pytest.mark.asyncio
    async def test_creates_missing_deletes_extra(self, cf_service):
        """Desired={a,b}; existing={b,c} pointing at this tunnel → create a, keep b, delete c."""
        cname_target = cf_service._tunnel_cname_target_for("tun-1")

        existing_records = [
            {"id": "rec-b", "name": "b.woowtech.io", "content": cname_target},
            {"id": "rec-c", "name": "c.woowtech.io", "content": cname_target},
        ]

        with patch.object(cf_service, "_list_dns_records_by_content",
                          new=AsyncMock(return_value=existing_records)), \
             patch.object(cf_service, "create_dns_record_for_tunnel",
                          new=AsyncMock(return_value="rec-a")) as mock_create, \
             patch.object(cf_service, "_delete_dns_record_by_id",
                          new=AsyncMock(return_value=True)) as mock_delete:
            result = await cf_service.reconcile_tunnel_dns(
                tunnel_id="tun-1", subdomains=["a", "b"], comment="paas-sa-x-y",
            )

        mock_create.assert_called_once_with(
            subdomain="a", tunnel_id="tun-1", comment="paas-sa-x-y",
        )
        mock_delete.assert_called_once_with("rec-c")
        assert result == {"a": "rec-a", "b": "rec-b"}


from unittest.mock import patch as sync_patch

from fastapi.testclient import TestClient

from src.main import app


@pytest.fixture
def client():
    with sync_patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestTunnelRoutesSyncEndpoint:
    @sync_patch("src.api.tunnels.cloudflare_service")
    def test_sync_routes_happy_path(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.domain = "woowtech.io"
        mock_cf.configure_tunnel_ingress = AsyncMock(return_value=None)
        mock_cf.reconcile_tunnel_dns = AsyncMock(
            return_value={"a": "rec-a", "b": "rec-b"}
        )

        response = client.put(
            "/api/tunnels/tun-1/routes",
            json={
                "routes": [
                    {"subdomain": "a", "service_url": "http://localhost:80",
                     "http_host_header": True},
                    {"subdomain": "b", "service_url": "http_status:503",
                     "http_host_header": False},
                ],
                "comment": "paas-sa-x-y",
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["tunnel_id"] == "tun-1"
        assert {r["subdomain"]: r["dns_record_id"] for r in data["routes"]} == {
            "a": "rec-a", "b": "rec-b",
        }
        # ingress rules built from routes with FQDN hostnames
        ingress_call = mock_cf.configure_tunnel_ingress.call_args
        assert ingress_call.kwargs["tunnel_id"] == "tun-1"
        assert ingress_call.kwargs["rules"][0]["hostname"] == "a.woowtech.io"
        # dns reconcile stamped with comment
        dns_call = mock_cf.reconcile_tunnel_dns.call_args
        assert dns_call.kwargs["comment"] == "paas-sa-x-y"

    @sync_patch("src.api.tunnels.cloudflare_service")
    def test_sync_routes_requires_credentials(self, mock_cf, client):
        mock_cf.api_token = None
        mock_cf.account_id = None
        response = client.put(
            "/api/tunnels/tun-1/routes",
            json={"routes": [{"subdomain": "a",
                              "service_url": "http://localhost:80"}]},
        )
        assert response.status_code == 400
