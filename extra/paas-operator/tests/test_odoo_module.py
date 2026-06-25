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
