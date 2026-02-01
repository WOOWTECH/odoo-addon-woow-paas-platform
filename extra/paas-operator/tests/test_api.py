"""Tests for API endpoints."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import (
    PodInfo,
    PodPhase,
    ReleaseInfo,
    ReleaseRevision,
    ReleaseStatus,
)


@pytest.fixture
def client():
    """Create FastAPI test client."""
    # Disable API key auth for tests
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = ""
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app)


class TestHealthEndpoint:
    """Test health check endpoint."""

    def test_health_check_healthy(self, client):
        """Test health check when Helm is available."""
        with patch("src.main.HelmService") as mock_helm:
            mock_helm.return_value.get_version.return_value = "v3.13.3"

            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert "helm_version" in data

    def test_health_check_degraded(self, client):
        """Test health check when Helm is unavailable."""
        with patch("src.main.HelmService") as mock_helm:
            mock_helm.return_value.get_version.side_effect = Exception("Helm not found")

            response = client.get("/health")

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "degraded"


class TestReleaseEndpoints:
    """Test release management endpoints."""

    @patch("src.api.releases.helm_service")
    def test_create_release(self, mock_helm, client):
        """Test create release endpoint."""
        mock_helm.install.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        response = client.post(
            "/api/releases",
            json={
                "namespace": "paas-ws-test",
                "name": "test-release",
                "chart": "nginx",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "test-release"
        assert data["status"] == "deployed"

    @patch("src.api.releases.helm_service")
    def test_create_release_invalid_namespace(self, mock_helm, client):
        """Test create release with invalid namespace."""
        mock_helm.install.side_effect = ValueError("Invalid namespace")

        response = client.post(
            "/api/releases",
            json={
                "namespace": "invalid",
                "name": "test",
                "chart": "nginx",
            },
        )

        assert response.status_code == 400

    @patch("src.api.releases.helm_service")
    def test_get_release(self, mock_helm, client):
        """Test get release endpoint."""
        mock_helm.get.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=2,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        response = client.get("/api/releases/paas-ws-test/test-release")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-release"
        assert data["revision"] == 2

    @patch("src.api.releases.helm_service")
    def test_get_release_not_found(self, mock_helm, client):
        """Test get release when release doesn't exist."""
        from src.services.helm import HelmException

        mock_helm.get.side_effect = HelmException(
            message="Not found",
            command="helm get",
            stderr="Error: release not found",
        )

        response = client.get("/api/releases/paas-ws-test/missing")

        assert response.status_code == 404

    @patch("src.api.releases.helm_service")
    def test_upgrade_release(self, mock_helm, client):
        """Test upgrade release endpoint."""
        mock_helm.upgrade.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=3,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.1.0",
            updated="2024-01-02T00:00:00Z",
        )

        response = client.patch(
            "/api/releases/paas-ws-test/test-release",
            json={
                "version": "1.1.0",
                "values": {"replicas": 3},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["revision"] == 3

    @patch("src.api.releases.helm_service")
    def test_delete_release(self, mock_helm, client):
        """Test delete release endpoint."""
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}

        response = client.delete("/api/releases/paas-ws-test/test-release")

        assert response.status_code == 200
        data = response.json()
        assert "uninstalled" in data["message"].lower()

    @patch("src.api.releases.helm_service")
    def test_rollback_release(self, mock_helm, client):
        """Test rollback release endpoint."""
        mock_helm.rollback.return_value = {"message": "Rollback successful"}

        response = client.post(
            "/api/releases/paas-ws-test/test-release/rollback",
            json={"revision": 1},
        )

        assert response.status_code == 200
        data = response.json()
        assert "successful" in data["message"].lower()

    @patch("src.api.releases.helm_service")
    def test_get_release_revisions(self, mock_helm, client):
        """Test get release revisions endpoint."""
        mock_helm.history.return_value = [
            ReleaseRevision(
                revision=1,
                updated="2024-01-01T00:00:00Z",
                status=ReleaseStatus.SUPERSEDED,
                chart="nginx-1.0.0",
                app_version="1.0.0",
                description="Install complete",
            ),
            ReleaseRevision(
                revision=2,
                updated="2024-01-02T00:00:00Z",
                status=ReleaseStatus.DEPLOYED,
                chart="nginx-1.1.0",
                app_version="1.1.0",
                description="Upgrade complete",
            ),
        ]

        response = client.get("/api/releases/paas-ws-test/test-release/revisions")

        assert response.status_code == 200
        data = response.json()
        assert len(data["revisions"]) == 2
        assert data["revisions"][1]["revision"] == 2

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_get_release_status(self, mock_helm, mock_k8s, client):
        """Test get release status endpoint."""
        mock_helm.get.return_value = ReleaseInfo(
            name="test-release",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2024-01-01T00:00:00Z",
        )

        mock_k8s.get_pods.return_value = [
            PodInfo(
                name="test-pod-1",
                phase=PodPhase.RUNNING,
                ready="1/1",
                restarts=0,
                age="5h",
            )
        ]

        response = client.get("/api/releases/paas-ws-test/test-release/status")

        assert response.status_code == 200
        data = response.json()
        assert data["release"]["name"] == "test-release"
        assert len(data["pods"]) == 1
        assert data["pods"][0]["phase"] == "Running"


class TestNamespaceEndpoints:
    """Test namespace management endpoints."""

    @patch("src.api.namespaces.k8s_service")
    def test_create_namespace(self, mock_k8s, client):
        """Test create namespace endpoint."""
        mock_k8s.create_namespace.return_value = {
            "message": "Namespace paas-ws-new created"
        }

        response = client.post(
            "/api/namespaces",
            json={
                "name": "paas-ws-new",
                "cpu_limit": "2",
                "memory_limit": "4Gi",
                "storage_limit": "20Gi",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert "created" in data["message"]

    @patch("src.api.namespaces.k8s_service")
    def test_create_namespace_invalid_name(self, mock_k8s, client):
        """Test create namespace with invalid name."""
        mock_k8s.create_namespace.side_effect = ValueError("Invalid namespace")

        response = client.post(
            "/api/namespaces",
            json={
                "name": "invalid",
                "cpu_limit": "2",
                "memory_limit": "4Gi",
                "storage_limit": "20Gi",
            },
        )

        assert response.status_code == 400


class TestAuthentication:
    """Test API key authentication."""

    def test_missing_api_key(self):
        """Test request without API key when auth is enabled."""
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "secret-key"
            mock_settings.namespace_prefix = "paas-ws-"

            client = TestClient(app)
            response = client.get("/api/releases/paas-ws-test/test")

            assert response.status_code == 401

    def test_valid_api_key(self):
        """Test request with valid API key."""
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "secret-key"
            mock_settings.namespace_prefix = "paas-ws-"

            client = TestClient(app)
            with patch("src.api.releases.helm_service") as mock_helm:
                mock_helm.get.return_value = ReleaseInfo(
                    name="test",
                    namespace="paas-ws-test",
                    revision=1,
                    status=ReleaseStatus.DEPLOYED,
                    chart="nginx",
                    app_version="1.0.0",
                    updated="2024-01-01T00:00:00Z",
                )

                response = client.get(
                    "/api/releases/paas-ws-test/test",
                    headers={"X-API-Key": "secret-key"},
                )

                assert response.status_code == 200

    def test_health_bypass_auth(self):
        """Test that health endpoint bypasses authentication."""
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "secret-key"

            client = TestClient(app)
            response = client.get("/health")

            # Should succeed without API key
            assert response.status_code == 200
