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
        values={"config": {"nonSensitive": {"connection": {"url": "https://tenant.example"}}}},
        sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        chart="oci://example/mcp-server",
        version="0.1.5",
    )


class TestResetConfigSchema:
    def test_defaults(self):
        request = ReleaseResetConfigRequest()
        assert request.values == {}
        assert request.sensitive_values == {}
        assert request.reuse_values is True

    def test_namespace_validation(self):
        _validate_namespace_or_raise("paas-ws-test")
        with pytest.raises(HTTPException) as error:
            _validate_namespace_or_raise("kube-system")
        assert error.value.status_code == 403


class TestResetConfigEndpoint:
    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp-server")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_happy_path_uses_stdin_and_preserves_config(
        self, mock_get_release, mock_upgrade, mock_restart, mock_status,
        mock_pod, mock_exec, mock_deployment,
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
            sensitive_values=_request().sensitive_values,
            version="0.1.5",
            reuse_values=True,
            atomic=True,
        )
        patch_call = mock_exec.call_args_list[0].kwargs
        assert patch_call["command"][:3] == ["python3", "-c", patch_call["command"][2]]
        assert "admin_password" in patch_call["command"][2]
        # C19: substring, not list membership — a bare `not in [list]` only
        # catches a whole-element match and would miss an f-string that
        # interpolated the password into the script body.
        assert "new-pwd-xyz" not in " ".join(patch_call["command"])
        assert patch_call["input_data"] == json.dumps({"admin_password": "new-pwd-xyz"})
        assert patch_call["command"][3].startswith("/data/.config.json.reset-backup-")
        # C04: the Deployment is '{release}-mcp-server', discovered by label —
        # never the release name itself.
        mock_deployment.assert_called_once_with(
            namespace="paas-ws-test",
            label_selector="app.kubernetes.io/instance=svc-abc",
        )
        mock_restart.assert_called_once_with(
            namespace="paas-ws-test", deployment_name="svc-abc-mcp-server",
        )
        mock_status.assert_called_once_with(
            namespace="paas-ws-test", deployment_name="svc-abc-mcp-server",
        )
        assert mock_exec.call_count == 2  # patch + backup cleanup

    def test_rejects_missing_password(self):
        with pytest.raises(HTTPException) as error:
            reset_release_config(
                "paas-ws-test",
                "svc-abc",
                ReleaseResetConfigRequest(values={}, sensitive_values={}),
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

    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp-server")
    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_rollout_failure_restores_config_and_secret(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_restart,
        mock_status, mock_rollback, mock_deployment,
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
        # C04 (the lethal half): the compensating rollout must resolve the real
        # Deployment too. Reusing the release name here meant a failed main path
        # dragged the rollback down with it — recovery_failed=True and a 500
        # telling the tenant to call an operator.
        assert mock_restart.call_args_list[-1].args == ("paas-ws-test", "svc-abc-mcp-server")
        assert mock_status.call_args_list[-1].args == ("paas-ws-test", "svc-abc-mcp-server")

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_patch_failure_when_recovery_also_fails_requires_intervention(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_rollback,
    ):
        mock_exec.side_effect = KubectlException("failed", "kubectl", "stderr")
        mock_rollback.side_effect = HelmException("rollback failed", "helm", "stderr")
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _request())
        assert error.value.status_code == 500
        assert "operator intervention required" in error.value.detail


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
        # C19: same list-membership blind spot as the argv assertion above —
        # `not in args` passes even when a secret is embedded inside an element.
        assert "secret" not in " ".join(args)
        assert run.call_args.kwargs["input_data"] == '{"admin_password":"secret"}'


class TestGetDeploymentName:
    """C04: a release name is not a Deployment name — discover it by label."""

    def test_resolves_fullname_from_label_selector(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(
                returncode=0, stdout="svc-abc-mcp-server\n", stderr="",
            )
            name = KubernetesService().get_deployment_name(
                namespace="paas-ws-test",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
        assert name == "svc-abc-mcp-server"
        args = run.call_args.args[0]
        assert args[:2] == ["get", "deployments"]
        assert "--selector" in args
        assert args[args.index("--selector") + 1] == "app.kubernetes.io/instance=svc-abc"
        # '.items[*]' (not '.items[0]') is what makes the >1 check below possible.
        assert "jsonpath={.items[*].metadata.name}" in args

    def test_raises_when_no_deployment_matches(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(KubectlException) as error:
                KubernetesService().get_deployment_name(
                    namespace="paas-ws-test",
                    label_selector="app.kubernetes.io/instance=svc-gone",
                )
        assert "No deployments found" in error.value.message

    def test_raises_when_selector_is_ambiguous(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(
                returncode=0, stdout="svc-abc-mcp-server svc-abc-sidecar\n", stderr="",
            )
            with pytest.raises(KubectlException) as error:
                KubernetesService().get_deployment_name(
                    namespace="paas-ws-test",
                    label_selector="app.kubernetes.io/instance=svc-abc",
                )
        # Fail loudly rather than restarting an arbitrary workload: today the
        # chart renders exactly one Deployment per release, and this assertion
        # is what stops that assumption from silently rotting.
        assert "Ambiguous selector" in error.value.message
        assert "svc-abc-mcp-server" in error.value.message
        assert "svc-abc-sidecar" in error.value.message

    def test_rejects_namespace_outside_prefix(self):
        with pytest.raises(ValueError):
            KubernetesService().get_deployment_name(
                namespace="kube-system",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
