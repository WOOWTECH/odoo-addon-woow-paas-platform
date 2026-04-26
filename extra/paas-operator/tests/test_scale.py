"""Tests for release scale API endpoint.

Covers Deployment + StatefulSet workloads since Bitnami-based releases
(Redis, PostgreSQL, Odoo, ...) ship as StatefulSets — without the
StatefulSet branch the operator returned 404 for those releases (BUG-7).
"""
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
    def test_scale_down_deployment_only(self, mock_k8s, client):
        """Deployment release scales to 0 (suspend)."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.scale_deployment.return_value = None

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 workload" in data["message"]
        assert data["deployments"] == "svc-test-main"
        assert data["statefulsets"] == ""
        mock_k8s.scale_deployment.assert_called_once_with(
            "paas-ws-test", "svc-test-main", 0,
        )
        mock_k8s.scale_statefulset.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_up_multiple_deployments(self, mock_k8s, client):
        """Release with multiple Deployments scales them all to 1."""
        mock_k8s.get_deployments.return_value = [
            {"name": "svc-test-main"},
            {"name": "svc-test-worker"},
        ]
        mock_k8s.get_statefulsets.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 1},
        )

        assert response.status_code == 200
        assert "Scaled 2 workload" in response.json()["message"]
        assert mock_k8s.scale_deployment.call_count == 2
        mock_k8s.scale_statefulset.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_statefulset_only(self, mock_k8s, client):
        """Bitnami-style release (StatefulSet only) — BUG-7 regression."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = [
            {"name": "svc-redis-master"},
        ]
        mock_k8s.scale_statefulset.return_value = None

        response = client.post(
            "/api/releases/paas-ws-test/svc-redis/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 workload" in data["message"]
        assert data["statefulsets"] == "svc-redis-master"
        mock_k8s.scale_statefulset.assert_called_once_with(
            "paas-ws-test", "svc-redis-master", 0,
        )
        mock_k8s.scale_deployment.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_mixed_deployment_and_statefulset(self, mock_k8s, client):
        """Release with both kinds (e.g. Odoo + Postgres) scales both."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-app"}]
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-db"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 2 workload" in data["message"]
        assert data["deployments"] == "svc-app"
        assert data["statefulsets"] == "svc-db"
        mock_k8s.scale_deployment.assert_called_once()
        mock_k8s.scale_statefulset.assert_called_once()

    @patch("src.api.releases.k8s_service")
    def test_scale_no_workloads(self, mock_k8s, client):
        """Release that owns nothing → 404."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-missing/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 404
        assert "No deployments or statefulsets" in response.json()["detail"]

    @patch("src.api.releases.k8s_service")
    def test_scale_kubectl_failure(self, mock_k8s, client):
        """kubectl error during scale → 500."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []
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
