"""Tests for GET /api/routes/availability."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.cloudflare import CloudflareException


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestRouteAvailability:
    @patch("src.api.routes.cloudflare_service")
    def test_available_when_no_record(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.domain = "woowtech.io"
        mock_cf.get_dns_record = AsyncMock(return_value=None)
        response = client.get("/api/routes/availability",
                              params={"subdomain": "waltek-ha"})
        assert response.status_code == 200
        data = response.json()
        assert data["subdomain"] == "waltek-ha"
        assert data["hostname"] == "waltek-ha.woowtech.io"
        assert data["available"] is True
        assert data["status"] == "free"
        # availability endpoint must opt into raise_on_error so CF API
        # failures are not collapsed into "available"
        assert mock_cf.get_dns_record.await_args.kwargs.get(
            "raise_on_error") is True

    @patch("src.api.routes.cloudflare_service")
    def test_taken_when_record_exists(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.domain = "woowtech.io"
        mock_cf.get_dns_record = AsyncMock(return_value={"id": "rec-1"})
        response = client.get("/api/routes/availability",
                              params={"subdomain": "waltek-ha"})
        assert response.status_code == 200
        body = response.json()
        assert body["available"] is False
        assert body["status"] == "taken"

    @patch("src.api.routes.cloudflare_service")
    def test_unknown_when_cf_api_errors(self, mock_cf, client):
        """CF API error (401/403/429/5xx) must NOT fail-open as available."""
        mock_cf.api_token = "t"
        mock_cf.domain = "woowtech.io"
        mock_cf.get_dns_record = AsyncMock(
            side_effect=CloudflareException("rate limited", status_code=429))
        response = client.get("/api/routes/availability",
                              params={"subdomain": "waltek-ha"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unknown"
        assert body["available"] is None
