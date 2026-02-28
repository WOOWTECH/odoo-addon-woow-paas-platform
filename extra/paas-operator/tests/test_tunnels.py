"""Tests for dedicated Cloudflare Tunnel CRUD API endpoints."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.cloudflare import CloudflareException


@pytest.fixture
def client():
    """Create FastAPI test client with auth disabled."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = ""
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app)


class TestCreateTunnel:
    """Test POST /api/tunnels endpoint."""

    @patch("src.api.tunnels.cloudflare_service")
    def test_create_tunnel_success(self, mock_cf, client):
        """Test successful tunnel creation with full provisioning flow."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"
        mock_cf.dns_enabled = True

        mock_cf.create_tunnel = AsyncMock(
            return_value={
                "tunnel_id": "tun-123",
                "tunnel_name": "smarthome-test",
            }
        )
        mock_cf.get_tunnel_token = AsyncMock(return_value="eyJ0dW5uZWxfaWQiOiJ0dW4tMTIzIn0=")
        mock_cf.configure_tunnel = AsyncMock()
        mock_cf.create_dns_record_for_tunnel = AsyncMock(return_value="dns-rec-456")

        response = client.post(
            "/api/tunnels",
            json={
                "name": "smarthome-test",
                "hostname": "ha.example.com",
                "service_url": "http://localhost:8123",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["tunnel_id"] == "tun-123"
        assert data["tunnel_name"] == "smarthome-test"
        assert data["tunnel_token"] == "eyJ0dW5uZWxfaWQiOiJ0dW4tMTIzIn0="
        assert data["hostname"] == "ha.example.com"
        assert data["dns_record_id"] == "dns-rec-456"

        mock_cf.create_tunnel.assert_called_once_with(name="smarthome-test")
        mock_cf.get_tunnel_token.assert_called_once_with("tun-123")
        mock_cf.configure_tunnel.assert_called_once_with(
            tunnel_id="tun-123",
            hostname="ha.example.com",
            service_url="http://localhost:8123",
        )

    @patch("src.api.tunnels.cloudflare_service")
    def test_create_tunnel_default_service_url(self, mock_cf, client):
        """Test tunnel creation uses default service_url when not provided."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"
        mock_cf.dns_enabled = False

        mock_cf.create_tunnel = AsyncMock(
            return_value={"tunnel_id": "tun-789", "tunnel_name": "test-tunnel"}
        )
        mock_cf.get_tunnel_token = AsyncMock(return_value="token-abc")
        mock_cf.configure_tunnel = AsyncMock()

        response = client.post(
            "/api/tunnels",
            json={
                "name": "test-tunnel",
                "hostname": "test.example.com",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["dns_record_id"] is None

        mock_cf.configure_tunnel.assert_called_once_with(
            tunnel_id="tun-789",
            hostname="test.example.com",
            service_url="http://localhost:8123",
        )

    @patch("src.api.tunnels.cloudflare_service")
    def test_create_tunnel_dns_failure_continues(self, mock_cf, client):
        """Test tunnel creation continues even if DNS record creation fails."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"
        mock_cf.dns_enabled = True

        mock_cf.create_tunnel = AsyncMock(
            return_value={"tunnel_id": "tun-456", "tunnel_name": "dns-fail-tunnel"}
        )
        mock_cf.get_tunnel_token = AsyncMock(return_value="token-xyz")
        mock_cf.configure_tunnel = AsyncMock()
        mock_cf.create_dns_record_for_tunnel = AsyncMock(
            side_effect=CloudflareException("DNS creation failed", status_code=500)
        )

        response = client.post(
            "/api/tunnels",
            json={
                "name": "dns-fail-tunnel",
                "hostname": "fail.example.com",
                "service_url": "http://localhost:8080",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["tunnel_id"] == "tun-456"
        assert data["dns_record_id"] is None

    @patch("src.api.tunnels.cloudflare_service")
    def test_create_tunnel_api_failure(self, mock_cf, client):
        """Test tunnel creation failure returns error."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.create_tunnel = AsyncMock(
            side_effect=CloudflareException("API error", status_code=500)
        )

        response = client.post(
            "/api/tunnels",
            json={
                "name": "fail-tunnel",
                "hostname": "fail.example.com",
            },
        )

        assert response.status_code == 500
        data = response.json()
        assert "Failed to create tunnel" in data["detail"]

    @patch("src.api.tunnels.cloudflare_service")
    def test_create_tunnel_missing_credentials(self, mock_cf, client):
        """Test tunnel creation fails when credentials are missing."""
        mock_cf.api_token = ""
        mock_cf.account_id = ""

        response = client.post(
            "/api/tunnels",
            json={
                "name": "no-creds",
                "hostname": "nocreds.example.com",
            },
        )

        assert response.status_code == 400
        assert "required" in response.json()["detail"].lower()

    def test_create_tunnel_missing_name(self, client):
        """Test tunnel creation fails with missing required fields."""
        response = client.post(
            "/api/tunnels",
            json={
                "hostname": "noname.example.com",
            },
        )

        assert response.status_code == 422


class TestGetTunnelStatus:
    """Test GET /api/tunnels/{tunnel_id} endpoint."""

    @patch("src.api.tunnels.cloudflare_service")
    def test_get_tunnel_status_success(self, mock_cf, client):
        """Test successful tunnel status retrieval."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.get_tunnel_status = AsyncMock(
            return_value={
                "tunnel_id": "tun-123",
                "name": "smarthome-test",
                "status": "healthy",
                "connections": [
                    {
                        "connector_id": "conn-1",
                        "type": "cloudflared",
                        "origin_ip": "192.168.1.100",
                        "opened_at": "2026-01-01T00:00:00Z",
                    }
                ],
                "created_at": "2026-01-01T00:00:00Z",
            }
        )

        response = client.get("/api/tunnels/tun-123")

        assert response.status_code == 200
        data = response.json()
        assert data["tunnel_id"] == "tun-123"
        assert data["name"] == "smarthome-test"
        assert data["status"] == "healthy"
        assert len(data["connections"]) == 1
        assert data["connections"][0]["connector_id"] == "conn-1"
        assert data["created_at"] == "2026-01-01T00:00:00Z"

    @patch("src.api.tunnels.cloudflare_service")
    def test_get_tunnel_status_not_found(self, mock_cf, client):
        """Test tunnel status when tunnel does not exist."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.get_tunnel_status = AsyncMock(
            side_effect=CloudflareException("Tunnel not found", status_code=404)
        )

        response = client.get("/api/tunnels/nonexistent-id")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @patch("src.api.tunnels.cloudflare_service")
    def test_get_tunnel_status_no_connections(self, mock_cf, client):
        """Test tunnel status with no active connections."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.get_tunnel_status = AsyncMock(
            return_value={
                "tunnel_id": "tun-inactive",
                "name": "inactive-tunnel",
                "status": "inactive",
                "connections": [],
                "created_at": "2026-01-01T00:00:00Z",
            }
        )

        response = client.get("/api/tunnels/tun-inactive")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "inactive"
        assert data["connections"] == []


class TestGetTunnelToken:
    """Test GET /api/tunnels/{tunnel_id}/token endpoint."""

    @patch("src.api.tunnels.cloudflare_service")
    def test_get_tunnel_token_success(self, mock_cf, client):
        """Test successful tunnel token retrieval."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.get_tunnel_token = AsyncMock(return_value="eyJhbGciOiJIUzI1NiJ9.token")

        response = client.get("/api/tunnels/tun-123/token")

        assert response.status_code == 200
        data = response.json()
        assert data["tunnel_id"] == "tun-123"
        assert data["token"] == "eyJhbGciOiJIUzI1NiJ9.token"

    @patch("src.api.tunnels.cloudflare_service")
    def test_get_tunnel_token_failure(self, mock_cf, client):
        """Test tunnel token retrieval failure."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.get_tunnel_token = AsyncMock(
            side_effect=CloudflareException("Token retrieval failed", status_code=500)
        )

        response = client.get("/api/tunnels/tun-bad/token")

        assert response.status_code == 500
        assert "Failed to get tunnel token" in response.json()["detail"]

    @patch("src.api.tunnels.cloudflare_service")
    def test_get_tunnel_token_missing_credentials(self, mock_cf, client):
        """Test tunnel token fails when credentials are missing."""
        mock_cf.api_token = ""
        mock_cf.account_id = ""

        response = client.get("/api/tunnels/tun-123/token")

        assert response.status_code == 400


class TestDeleteTunnel:
    """Test DELETE /api/tunnels/{tunnel_id} endpoint."""

    @patch("src.api.tunnels.cloudflare_service")
    def test_delete_tunnel_success(self, mock_cf, client):
        """Test successful tunnel deletion."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.delete_tunnel = AsyncMock()

        response = client.delete("/api/tunnels/tun-123")

        assert response.status_code == 200
        data = response.json()
        assert "tun-123" in data["message"]
        assert "deleted" in data["message"].lower()

        mock_cf.delete_tunnel.assert_called_once_with("tun-123")

    @patch("src.api.tunnels.cloudflare_service")
    def test_delete_tunnel_failure(self, mock_cf, client):
        """Test tunnel deletion failure."""
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"

        mock_cf.delete_tunnel = AsyncMock(
            side_effect=CloudflareException("Deletion failed", status_code=500)
        )

        response = client.delete("/api/tunnels/tun-bad")

        assert response.status_code == 500
        assert "Failed to delete tunnel" in response.json()["detail"]

    @patch("src.api.tunnels.cloudflare_service")
    def test_delete_tunnel_missing_credentials(self, mock_cf, client):
        """Test tunnel deletion fails when credentials are missing."""
        mock_cf.api_token = ""
        mock_cf.account_id = ""

        response = client.delete("/api/tunnels/tun-123")

        assert response.status_code == 400
