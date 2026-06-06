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
