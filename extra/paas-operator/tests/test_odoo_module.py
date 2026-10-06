"""Tests for Odoo module state query endpoints and service."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException
from src.services.odoo_module import OdooModuleError, OdooModuleService


@pytest.fixture
def client():
    """Create FastAPI test client (F-PC-4: real key + matching header)."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestModuleStatesEndpoint:
    """Test POST /{namespace}/{release_name}/module-states endpoint."""

    @patch("src.api.odoo_modules.odoo_module_service")
    def test_returns_module_states(self, mock_service, client):
        """Test successful module states query."""
        mock_service.get_module_states.return_value = {
            "success": True,
            "modules": {
                "sale": {"state": "installed", "dependents": []},
                "stock": {"state": "uninstalled", "dependents": []},
            },
        }

        response = client.post(
            "/api/odoo/paas-ws-test/my-odoo/module-states",
            json={"module_names": ["sale", "stock"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["modules"]["sale"]["state"] == "installed"
        assert data["modules"]["stock"]["state"] == "uninstalled"

    @patch("src.api.odoo_modules.odoo_module_service")
    def test_returns_dependents(self, mock_service, client):
        """Test that dependents list is included in response."""
        mock_service.get_module_states.return_value = {
            "success": True,
            "modules": {
                "base": {
                    "state": "installed",
                    "dependents": ["sale", "stock", "account"],
                },
            },
        }

        response = client.post(
            "/api/odoo/paas-ws-test/my-odoo/module-states",
            json={"module_names": ["base"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        dependents = data["modules"]["base"]["dependents"]
        assert "sale" in dependents
        assert "stock" in dependents
        assert "account" in dependents

    @patch("src.api.odoo_modules.odoo_module_service")
    def test_handles_odoo_error(self, mock_service, client):
        """Test that OdooModuleError returns success=False with error message."""
        mock_service.get_module_states.side_effect = OdooModuleError(
            "Cannot find Odoo pod for release my-odoo in paas-ws-test",
            step="find_pod",
        )

        response = client.post(
            "/api/odoo/paas-ws-test/my-odoo/module-states",
            json={"module_names": ["sale"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "find_pod" in data["error"]
        assert "Cannot find Odoo pod" in data["error"]


class TestOdooModuleService:
    """Test OdooModuleService business logic."""

    def _make_service(self, mock_k8s):
        """Create OdooModuleService with a mocked KubernetesService."""
        return OdooModuleService(k8s_service=mock_k8s)

    def test_get_module_states_parses_output(self):
        """Test that get_module_states correctly parses kubectl exec JSON output."""
        mock_k8s = MagicMock()
        mock_k8s.get_pod_name.return_value = "my-odoo-pod-abc123"
        json_output = json.dumps({
            "success": True,
            "modules": {
                "sale": {"state": "installed", "dependents": ["pos_sale"]},
                "purchase": {"state": "installed", "dependents": []},
            },
        })
        mock_k8s.exec_in_pod.return_value = MagicMock(stdout=json_output)

        service = self._make_service(mock_k8s)
        result = service.get_module_states(
            namespace="paas-ws-test",
            release_name="my-odoo",
            module_names=["sale", "purchase"],
        )

        assert result["success"] is True
        assert result["modules"]["sale"]["state"] == "installed"
        assert "pos_sale" in result["modules"]["sale"]["dependents"]
        assert result["modules"]["purchase"]["dependents"] == []

    def test_raises_on_pod_not_found(self):
        """Test that KubectlException from get_pod_name raises OdooModuleError."""
        mock_k8s = MagicMock()
        mock_k8s.get_pod_name.side_effect = KubectlException(
            message="No pods found",
            command="kubectl get pods",
            stderr="No resources found",
        )

        service = self._make_service(mock_k8s)

        with pytest.raises(OdooModuleError, match="Cannot find Odoo pod"):
            service.get_module_states(
                namespace="paas-ws-test",
                release_name="my-odoo",
                module_names=["sale"],
            )


# --- #1477：module-states 走 woow-odoo exec 契約（與 init/odoo 同源）---------------


def _module_states_output():
    return MagicMock(stdout=json.dumps({
        "success": True,
        "modules": {"mcp_server": {"state": "uninstalled", "dependents": []}},
    }))


class TestModuleStatesWoowOdooContract1477:
    """exec 參數斷言比照 #1249 的 ``test_odoo_init_contract.py``。"""

    def test_targets_server_pod_with_chart_contract(self):
        """同 release 的 PG／cron 不得取代 server；容器 odoo、PATH python3、/etc/odoo/odoo.conf。"""
        k8s = MagicMock()
        pods = [
            ("pg-0", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/component": "postgresql"}),
            ("cron-0", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/name": "odoo",
                        "app.kubernetes.io/component": "cron"}),
            ("web-0", {"app.kubernetes.io/instance": "tenant", "app.kubernetes.io/name": "odoo",
                       "app.kubernetes.io/component": "server"}),
        ]

        def get_pod_name(namespace, selector):
            requested = dict(part.split("=", 1) for part in selector.split(","))
            for name, labels in pods:
                if requested.items() <= labels.items():
                    return name
            raise KubectlException("No pods", "get pods", "")

        k8s.get_pod_name.side_effect = get_pod_name
        k8s.exec_in_pod.return_value = _module_states_output()

        result = OdooModuleService(k8s).get_module_states(
            "paas-ws-test", "tenant", ["mcp_server"],
        )

        assert result["modules"]["mcp_server"]["state"] == "uninstalled"
        k8s.get_pod_name.assert_called_once_with(
            "paas-ws-test",
            "app.kubernetes.io/instance=tenant,app.kubernetes.io/component=server",
        )
        k8s.get_pod.assert_not_called()
        call = k8s.exec_in_pod.call_args
        assert call.kwargs["pod_name"] == "web-0"
        assert call.kwargs["container"] == "odoo"
        command = call.kwargs["command"]
        assert command[:2] == ["python3", "-c"]
        assert "'/etc/odoo/odoo.conf'" in command[2]
        assert "bitnami" not in " ".join(command)

    @staticmethod
    def _legacy_pod():
        return {
            "metadata": {
                "name": "legacy-0",
                "namespace": "paas-ws-test",
                "labels": {"app.kubernetes.io/instance": "tenant"},
            },
            "spec": {"containers": [{"name": "odoo"}]},
        }

    def _fallback_k8s(self, pod):
        k8s = MagicMock()
        k8s.get_pod_name.side_effect = [
            KubectlException("No server", "get pods", ""), "legacy-0",
        ]
        k8s.get_pod.return_value = pod
        k8s.exec_in_pod.return_value = _module_states_output()
        return k8s

    @pytest.mark.parametrize("case", [
        "cron", "postgresql", "job-owner", "no-odoo-container", "foreign-instance", "get-failure",
    ])
    def test_rejects_unverified_fallback_without_exec(self, case):
        pod = self._legacy_pod()
        labels = pod["metadata"]["labels"]
        if case in ("cron", "postgresql"):
            labels["app.kubernetes.io/component"] = case
        elif case == "job-owner":
            pod["metadata"]["ownerReferences"] = [{"kind": "Job", "name": "backup"}]
        elif case == "no-odoo-container":
            # Bitnami 時代的容器名 <release>-odoo 也不再被接受。
            pod["spec"]["containers"] = [{"name": "tenant-odoo"}]
        elif case == "foreign-instance":
            labels["app.kubernetes.io/instance"] = "other"
        k8s = self._fallback_k8s(pod)
        if case == "get-failure":
            k8s.get_pod.side_effect = KubectlException("Forbidden", "get pod", "")

        with pytest.raises(OdooModuleError) as error:
            OdooModuleService(k8s).get_module_states("paas-ws-test", "tenant", ["mcp_server"])

        assert error.value.step == "find_pod"
        k8s.exec_in_pod.assert_not_called()

    def test_verified_legacy_fallback_still_queries(self):
        k8s = self._fallback_k8s(self._legacy_pod())

        result = OdooModuleService(k8s).get_module_states(
            "paas-ws-test", "tenant", ["mcp_server"],
        )

        assert result["success"] is True
        assert k8s.exec_in_pod.call_args.kwargs["pod_name"] == "legacy-0"
        assert k8s.exec_in_pod.call_args.kwargs["container"] == "odoo"
