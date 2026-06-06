"""Tests for GET /api/routes/availability."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app


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
        assert data == {
            "subdomain": "waltek-ha",
            "hostname": "waltek-ha.woowtech.io",
            "available": True,
        }

    @patch("src.api.routes.cloudflare_service")
    def test_taken_when_record_exists(self, mock_cf, client):
        mock_cf.api_token = "t"
        mock_cf.domain = "woowtech.io"
        mock_cf.get_dns_record = AsyncMock(return_value={"id": "rec-1"})
        response = client.get("/api/routes/availability",
                              params={"subdomain": "waltek-ha"})
        assert response.status_code == 200
        assert response.json()["available"] is False
