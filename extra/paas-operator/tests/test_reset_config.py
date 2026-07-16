"""Tests for #357 surgical admin-password reset."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.releases import _validate_namespace_or_raise, reset_release_config
from src.models.schemas import ReleaseResetConfigRequest
from src.services.helm import HelmException, KubectlException, KubernetesService


def _request():
    return ReleaseResetConfigRequest(
        values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        chart="oci://example/mcp-server",
        version="0.1.5",
    )


class TestResetConfigSchema:
    def test_defaults(self):
        request = ReleaseResetConfigRequest()
        assert request.values == {}
        assert request.reuse_values is True

    def test_namespace_validation(self):
        _validate_namespace_or_raise("paas-ws-test")
        with pytest.raises(HTTPException) as error:
            _validate_namespace_or_raise("kube-system")
        assert error.value.status_code == 403


class TestResetConfigEndpoint:
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_happy_path_uses_stdin_and_preserves_config(
        self, mock_get_release, mock_upgrade, mock_restart, mock_status,
        mock_pod, mock_exec,
    ):
        mock_get_release.return_value = MagicMock(revision=7)
        mock_upgrade.return_value = MagicMock(revision=8)
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")

        response = reset_release_config("paas-ws-test", "svc-abc", _request())

        assert response["helm_revision"] == 8
        mock_upgrade.assert_called_once_with(
            namespace="paas-ws-test",
            name="svc-abc",
            chart="oci://example/mcp-server",
            values=_request().values,
            version="0.1.5",
            reuse_values=True,
            atomic=True,
        )
        patch_call = mock_exec.call_args_list[0].kwargs
        assert patch_call["command"][:3] == ["python3", "-c", patch_call["command"][2]]
        assert "admin_password" in patch_call["command"][2]
        assert "new-pwd-xyz" not in patch_call["command"]
        assert patch_call["input_data"] == json.dumps({"admin_password": "new-pwd-xyz"})
        assert patch_call["command"][3].startswith("/data/.config.json.reset-backup-")
        mock_restart.assert_called_once_with(namespace="paas-ws-test", deployment_name="svc-abc")
        mock_status.assert_called_once_with(namespace="paas-ws-test", deployment_name="svc-abc")
        assert mock_exec.call_count == 2  # patch + backup cleanup

    def test_rejects_missing_password(self):
        with pytest.raises(HTTPException) as error:
            reset_release_config(
                "paas-ws-test",
                "svc-abc",
                ReleaseResetConfigRequest(values={"config": {"sensitive": {}}}),
            )
        assert error.value.status_code == 400

    @patch("src.api.releases.helm_service.upgrade")
    def test_upgrade_failure_does_not_patch_config(self, mock_upgrade):
        mock_upgrade.side_effect = HelmException("failed", "helm", "stderr")
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _request())
        assert error.value.status_code == 500
        assert "chart Secret" in error.value.detail

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_patch_failure_rolls_secret_back(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_rollback,
    ):
        mock_exec.side_effect = KubectlException("failed", "kubectl", "stderr")
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _request())
        assert error.value.status_code == 500
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_rollout_failure_restores_config_and_secret(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_restart,
        mock_status, mock_rollback,
    ):
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_status.side_effect = [
            KubectlException("failed", "kubectl", "stderr"),
            None,
        ]
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _request())
        assert error.value.status_code == 500
        assert "previous password restored" in error.value.detail
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)
        restore_call = mock_exec.call_args_list[1].kwargs
        assert "os.replace" in restore_call["command"][2]


class TestExecInPodStdin:
    def test_stdin_is_not_an_argv_value(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            KubernetesService().exec_in_pod(
                namespace="paas-ws-test",
                pod_name="mcp-abc",
                container="",
                command=["python3", "-c", "pass"],
                input_data='{"admin_password":"secret"}',
            )
        args = run.call_args.args[0]
        assert "--stdin" in args
        assert "secret" not in args
        assert run.call_args.kwargs["input_data"] == '{"admin_password":"secret"}'
