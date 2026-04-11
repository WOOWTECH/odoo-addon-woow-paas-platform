"""Tests for release scale API endpoint."""
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException


@pytest.fixture
def client():
    """Create FastAPI test client with auth disabled."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = ""
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app)


class TestScaleRelease:
    """Test POST /api/releases/{namespace}/{name}/scale endpoint."""

    @patch("src.api.releases.k8s_service")
    def test_scale_down_success(self, mock_k8s, client):
        """Test scaling down to 0 replicas (suspend)."""
        mock_k8s.get_deployments.return_value = [
            {"name": "svc-test-main"},
        ]
        mock_k8s.scale_deployment.return_value = None

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 deployment" in data["message"]
        mock_k8s.scale_deployment.assert_called_once_with(
            "paas-ws-test", "svc-test-main", 0,
        )

    @patch("src.api.releases.k8s_service")
    def test_scale_up_success(self, mock_k8s, client):
        """Test scaling up to 1 replica (resume)."""
        mock_k8s.get_deployments.return_value = [
            {"name": "svc-test-main"},
            {"name": "svc-test-worker"},
        ]
        mock_k8s.scale_deployment.return_value = None

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 1},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 2 deployment" in data["message"]
        assert mock_k8s.scale_deployment.call_count == 2

    @patch("src.api.releases.k8s_service")
    def test_scale_no_deployments(self, mock_k8s, client):
        """Test scaling when no deployments found."""
        mock_k8s.get_deployments.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-missing/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 404
        assert "No deployments found" in response.json()["detail"]

    @patch("src.api.releases.k8s_service")
    def test_scale_kubectl_failure(self, mock_k8s, client):
        """Test scaling when kubectl fails."""
        mock_k8s.get_deployments.return_value = [
            {"name": "svc-test-main"},
        ]
        mock_k8s.scale_deployment.side_effect = KubectlException(
            message="kubectl scale failed",
            command="kubectl scale",
            stderr="error",
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 500
