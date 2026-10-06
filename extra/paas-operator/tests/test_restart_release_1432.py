"""#1432：租戶「重新啟動」——rollout restart release 下所有 Deployment＋StatefulSet。"""
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException, KubernetesService


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


URL = "/api/releases/paas-ws-x/svc-x/restart"


class TestRestartEndpoint:
    @patch("src.api.releases.k8s_service")
    def test_restarts_deployments_and_statefulsets(self, mock_k8s, client):
        """open-design 形：console Deployment ＋ daemon StatefulSet 兩個都要重啟。"""
        mock_k8s.get_deployments.return_value = [{"name": "svc-x-console"}]
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-x-open-design"}]

        resp = client.post(URL)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deployments"] == ["svc-x-console"]
        assert body["statefulsets"] == ["svc-x-open-design"]
        assert body["rollout_awaited"] is False
        mock_k8s.rollout_restart_deployment.assert_called_once_with("paas-ws-x", "svc-x-console")
        mock_k8s.rollout_restart_statefulset.assert_called_once_with(
            "paas-ws-x", "svc-x-open-design")
        selector = mock_k8s.get_deployments.call_args.kwargs["label_selector"]
        assert selector == "app.kubernetes.io/instance=svc-x"

    @patch("src.api.releases.k8s_service")
    def test_no_workloads_is_404(self, mock_k8s, client):
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        resp = client.post(URL)
        assert resp.status_code == 404

    @patch("src.api.releases.k8s_service")
    def test_kubectl_failure_is_500(self, mock_k8s, client):
        mock_k8s.get_deployments.return_value = [{"name": "svc-x-app"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.rollout_restart_deployment.side_effect = KubectlException(
            "boom", "kubectl rollout restart", "forbidden")
        resp = client.post(URL)
        assert resp.status_code == 500

    def test_foreign_namespace_403(self, client):
        resp = client.post("/api/releases/kube-system/x/restart")
        assert resp.status_code == 403


class TestRolloutRestartStatefulset:
    @patch("subprocess.run")
    def test_argv(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        KubernetesService().rollout_restart_statefulset("paas-ws-x", "svc-x-daemon")
        argv = mock_run.call_args.args[0]
        assert argv[1:4] == ["rollout", "restart", "statefulset/svc-x-daemon"]
        assert "paas-ws-x" in argv

    def test_rejects_foreign_namespace(self):
        with pytest.raises(ValueError):
            KubernetesService().rollout_restart_statefulset("kube-system", "x")
