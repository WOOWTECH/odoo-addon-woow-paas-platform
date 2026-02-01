"""Tests for Helm service."""
import json
import subprocess
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.services.helm import HelmException, HelmService, KubernetesService


class TestHelmService:
    """Test cases for HelmService."""

    @pytest.fixture
    def helm_service(self):
        """Create HelmService instance."""
        return HelmService()

    def test_validate_namespace_valid(self, helm_service):
        """Test namespace validation with valid prefix."""
        # Should not raise
        helm_service._validate_namespace("paas-ws-test")
        helm_service._validate_namespace("paas-ws-123")

    def test_validate_namespace_invalid(self, helm_service):
        """Test namespace validation with invalid prefix."""
        with pytest.raises(ValueError, match="must start with"):
            helm_service._validate_namespace("invalid-namespace")

        with pytest.raises(ValueError, match="must start with"):
            helm_service._validate_namespace("test")

    @patch("subprocess.run")
    def test_run_command_success(self, mock_run, helm_service):
        """Test successful command execution."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout='{"test": "data"}',
            stderr="",
        )

        result = helm_service._run_command(["version"])

        assert result.returncode == 0
        assert result.stdout == '{"test": "data"}'
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_run_command_failure(self, mock_run, helm_service):
        """Test command execution failure."""
        mock_run.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Error: command failed",
        )

        with pytest.raises(HelmException) as exc_info:
            helm_service._run_command(["invalid"])

        assert "failed with code 1" in exc_info.value.message
        assert exc_info.value.stderr == "Error: command failed"

    @patch("subprocess.run")
    def test_run_command_timeout(self, mock_run, helm_service):
        """Test command timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["helm", "test"],
            timeout=300,
        )

        with pytest.raises(HelmException) as exc_info:
            helm_service._run_command(["test"])

        assert "timed out" in exc_info.value.message

    @patch("subprocess.run")
    def test_install_success(self, mock_run, helm_service):
        """Test successful Helm install."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "test-release",
                "namespace": "paas-ws-test",
                "info": {
                    "status": "deployed",
                    "revision": 1,
                    "last_deployed": "2024-01-01T00:00:00Z",
                },
                "chart": {
                    "metadata": {
                        "name": "nginx",
                        "appVersion": "1.0.0",
                    }
                },
            }),
            stderr="",
        )

        result = helm_service.install(
            namespace="paas-ws-test",
            name="test-release",
            chart="nginx",
        )

        assert result.name == "test-release"
        assert result.namespace == "paas-ws-test"
        assert result.chart == "nginx"

    @patch("subprocess.run")
    def test_install_invalid_namespace(self, mock_run, helm_service):
        """Test install with invalid namespace."""
        with pytest.raises(ValueError):
            helm_service.install(
                namespace="invalid",
                name="test",
                chart="nginx",
            )

        # Should not call subprocess
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_get_release(self, mock_run, helm_service):
        """Test get release info."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "name": "test-release",
                "namespace": "paas-ws-test",
                "info": {
                    "status": "deployed",
                    "revision": 2,
                    "last_deployed": "2024-01-01T00:00:00Z",
                },
                "chart": {
                    "metadata": {
                        "name": "nginx",
                        "appVersion": "1.0.0",
                    }
                },
            }),
            stderr="",
        )

        result = helm_service.get("paas-ws-test", "test-release")

        assert result.name == "test-release"
        assert result.revision == 2

    @patch("subprocess.run")
    def test_uninstall(self, mock_run, helm_service):
        """Test uninstall release."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="release 'test-release' uninstalled",
            stderr="",
        )

        result = helm_service.uninstall("paas-ws-test", "test-release")

        assert "uninstalled" in result["message"]

    @patch("subprocess.run")
    def test_history(self, mock_run, helm_service):
        """Test get release history."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps([
                {
                    "revision": 1,
                    "updated": "2024-01-01T00:00:00Z",
                    "status": "superseded",
                    "chart": "nginx-1.0.0",
                    "app_version": "1.0.0",
                    "description": "Install complete",
                },
                {
                    "revision": 2,
                    "updated": "2024-01-02T00:00:00Z",
                    "status": "deployed",
                    "chart": "nginx-1.0.1",
                    "app_version": "1.0.1",
                    "description": "Upgrade complete",
                },
            ]),
            stderr="",
        )

        result = helm_service.history("paas-ws-test", "test-release")

        assert len(result) == 2
        assert result[0].revision == 1
        assert result[1].revision == 2
        assert result[1].status.value == "deployed"

    @patch("subprocess.run")
    def test_get_version(self, mock_run, helm_service):
        """Test get Helm version."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout="v3.13.3+g3ff1c00",
            stderr="",
        )

        result = helm_service.get_version()

        assert "v3.13" in result


class TestKubernetesService:
    """Test cases for KubernetesService."""

    @pytest.fixture
    def k8s_service(self):
        """Create KubernetesService instance."""
        return KubernetesService()

    @patch("subprocess.run")
    def test_get_pods(self, mock_run, k8s_service):
        """Test get pods in namespace."""
        mock_run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "items": [
                    {
                        "metadata": {
                            "name": "test-pod-1",
                            "creationTimestamp": "2024-01-01T00:00:00Z",
                        },
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {
                                    "ready": True,
                                    "restartCount": 0,
                                }
                            ],
                        },
                    }
                ]
            }),
            stderr="",
        )

        result = k8s_service.get_pods("paas-ws-test")

        assert len(result) == 1
        assert result[0].name == "test-pod-1"
        assert result[0].phase.value == "Running"
        assert result[0].ready == "1/1"
        assert result[0].restarts == 0

    @patch("subprocess.run")
    def test_create_namespace(self, mock_run, k8s_service):
        """Test create namespace with quota."""
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")

        result = k8s_service.create_namespace(
            name="paas-ws-new",
            cpu_limit="2",
            memory_limit="4Gi",
            storage_limit="20Gi",
        )

        assert "created" in result["message"]
        # Should be called twice (namespace + quota)
        assert mock_run.call_count == 2

    def test_create_namespace_invalid_prefix(self, k8s_service):
        """Test create namespace with invalid prefix."""
        with pytest.raises(ValueError):
            k8s_service.create_namespace(
                name="invalid",
                cpu_limit="2",
                memory_limit="4Gi",
                storage_limit="20Gi",
            )
