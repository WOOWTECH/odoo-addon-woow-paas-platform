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
    """Create FastAPI test client (F-PC-4: real key + matching header)."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


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
        # #423 W4.3: the VM branch is consulted when there is no container
        # workload, so it must be stubbed empty or the MagicMock default
        # (truthy) would suppress the 404 this test exists to prove.
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-missing/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 404
        assert "No deployments, statefulsets or virtualmachines" in response.json()["detail"]

    # ---- #423 W4.3: VirtualMachine branch ----------------------------------
    # Same shape as the BUG-7 StatefulSet regression above: a workload kind
    # the endpoint did not know about returned 404, and the Odoo caller turned
    # that into state='error'. For VMs that meant every unpaid HAOS tenant
    # landing in a bogus error state instead of being suspended.

    @patch("src.api.releases.k8s_service")
    def test_scale_down_virtualmachine_maps_to_halted(self, mock_k8s, client):
        """replicas=0 (suspend) → runStrategy Halted, NOT a 404."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 workload" in data["message"]
        assert data["virtualmachines"] == "svc-haos"
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Halted",
        )
        mock_k8s.scale_deployment.assert_not_called()
        mock_k8s.scale_statefulset.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_up_virtualmachine_maps_to_always(self, mock_k8s, client):
        """replicas=1 (resume) → runStrategy Always."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 1},
        )

        assert response.status_code == 200
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Always",
        )

    @patch("src.api.releases.k8s_service")
    def test_any_nonzero_replicas_maps_to_always(self, mock_k8s, client):
        """A VM has no notion of 'N replicas' — everything >=1 is Always."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 3},
        )

        assert response.status_code == 200
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Always",
        )

    @patch("src.api.releases.k8s_service")
    def test_container_release_never_queries_virtualmachines(self, mock_k8s, client):
        """Clusters without KubeVirt must not pay for a CRD lookup, and the
        existing container path must stay byte-identical."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        mock_k8s.get_virtualmachines.assert_not_called()
        mock_k8s.patch_vm_run_strategy.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_missing_kubevirt_crd_still_404s(self, mock_k8s, client):
        """No CRD → genuinely no VM workload → the honest answer is 404.

        (Unlike the readiness path, a KubectlException here is expected on
        container-only clusters, so it is logged and folded into the 404
        rather than raised as a 500.)
        """
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.side_effect = KubectlException(
            message="no CRD",
            command="kubectl get virtualmachines",
            stderr='the server doesn\'t have a resource type "virtualmachines"',
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-missing/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 404

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
