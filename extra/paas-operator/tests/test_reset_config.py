"""Tests for #357 reset-config schema + endpoint.

Covers:
- ReleaseResetConfigRequest schema defaults + required fields path
- _validate_namespace_or_raise passes valid / rejects invalid
- reset_release_config endpoint wires values + chart + version
- exec_in_pod empty-container fallback does NOT pass --container flag
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.api.releases import (
    _validate_namespace_or_raise,
    reset_release_config,
)
from src.models.schemas import ReleaseResetConfigRequest


class TestSchema:
    def test_default_values_is_empty_dict(self):
        req = ReleaseResetConfigRequest()
        assert req.values == {}
        assert req.chart is None
        assert req.version is None
        assert req.reuse_values is True

    def test_accepts_full_payload(self):
        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": "x"}}},
            chart="oci://example/mcp-server",
            version="0.1.5",
            reuse_values=True,
        )
        assert req.values["config"]["sensitive"]["admin_password"] == "x"
        assert req.chart == "oci://example/mcp-server"
        assert req.version == "0.1.5"
        assert req.reuse_values is True


class TestNamespaceValidation:
    def test_accepts_valid_prefix(self):
        # Should not raise
        _validate_namespace_or_raise("paas-ws-test")
        _validate_namespace_or_raise("paas-stg-system")
        _validate_namespace_or_raise("paas-cloudflare-system")

    def test_rejects_invalid_prefix(self):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _validate_namespace_or_raise("kube-system")
        assert exc_info.value.status_code == 403

    def test_rejects_empty_prefix_when_settings_strict(self, monkeypatch):
        """If namespace_prefix is empty, validate_namespace fail-closed → 403."""
        from src.config import settings
        monkeypatch.setattr(settings, "namespace_prefix", "", raising=False)
        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            _validate_namespace_or_raise("anything")


class TestResetConfigEndpoint:
    """The endpoint does 3 things in order:
    1. exec_in_pod read-modify-write /data/config.json
    2. helm_service.upgrade (Secret-aligned)
    3. k8s_service.rollout_restart_deployment (Recreate cache rebuild)

    Each step is gated by the previous; a failure at step N leaves the
    state from steps 1..N-1 in place (operator returns 500, caller decides).
    """

    def _mock_release(self):
        return MagicMock(revision=2)

    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name")
    def test_happy_path_all_three_steps(
        self, mock_get_pod, mock_exec, mock_upgrade, mock_restart
    ):
        mock_get_pod.return_value = "mcp-server-abc"
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_upgrade.return_value = self._mock_release()

        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        )
        resp = reset_release_config(
            namespace="paas-ws-test",
            name="svc-abc",
            request=req,
        )

        assert resp["action"] == "reset-config"
        assert resp["namespace"] == "paas-ws-test"
        assert resp["release"] == "svc-abc"
        assert resp["helm_revision"] == 2
        assert resp["config_json_patched"] is True
        assert resp["helm_upgraded"] is True
        assert resp["rollout_restart_issued"] is True

        # exec_in_pod received the python3 read-modify-write script + new pwd
        mock_exec.assert_called_once()
        kwargs = mock_exec.call_args.kwargs
        cmd = kwargs["command"]
        assert cmd[0] == "python3"
        assert cmd[1] == "-c"
        assert "admin_password" in cmd[2]  # script body
        assert cmd[3] == "new-pwd-xyz"  # new password as argv
        assert kwargs["container"] == ""  # kubectl default = first container

    def test_rejects_missing_admin_password(self):
        from fastapi import HTTPException
        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {}}},  # missing admin_password
        )
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="paas-ws-test", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 400
        assert "admin_password" in exc_info.value.detail

    def test_rejects_empty_values(self):
        from fastapi import HTTPException
        req = ReleaseResetConfigRequest()
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="paas-ws-test", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 400

    def test_rejects_non_string_admin_password(self):
        from fastapi import HTTPException
        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": 12345}}},
        )
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="paas-ws-test", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 400

    def test_rejects_invalid_namespace(self):
        from fastapi import HTTPException
        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": "x"}}},
        )
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="kube-system", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 403

    @patch("src.api.releases.k8s_service.get_pod_name")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    def test_exec_failure_returns_500_no_helm_upgrade(
        self, mock_exec, mock_get_pod,
    ):
        from fastapi import HTTPException
        from src.services.helm import KubectlException

        mock_get_pod.return_value = "mcp-server-abc"
        mock_exec.side_effect = KubectlException(
            message="kubectl exec failed", command="...", stderr="...",
        )

        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": "x"}}},
        )
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="paas-ws-test", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 500
        assert "/data/config.json" in exc_info.value.detail

    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name")
    def test_helm_upgrade_failure_returns_500(
        self, mock_get_pod, mock_exec, mock_upgrade,
    ):
        from fastapi import HTTPException
        from src.services.helm import HelmException

        mock_get_pod.return_value = "mcp-server-abc"
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_upgrade.side_effect = HelmException(
            message="upgrade failed", command="...", stderr="...",
        )

        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": "x"}}},
        )
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="paas-ws-test", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 500
        assert "helm upgrade failed" in exc_info.value.detail

    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name")
    def test_rollout_restart_failure_signals_manual_recovery(
        self, mock_get_pod, mock_exec, mock_upgrade, mock_restart,
    ):
        from fastapi import HTTPException
        from src.services.helm import KubectlException

        mock_get_pod.return_value = "mcp-server-abc"
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_upgrade.return_value = self._mock_release()
        mock_restart.side_effect = KubectlException(
            message="rollout restart failed", command="...", stderr="...",
        )

        req = ReleaseResetConfigRequest(
            values={"config": {"sensitive": {"admin_password": "x"}}},
        )
        with pytest.raises(HTTPException) as exc_info:
            reset_release_config(
                namespace="paas-ws-test", name="svc-abc", request=req,
            )
        assert exc_info.value.status_code == 500
        assert "rollout restart failed" in exc_info.value.detail
        assert "kubectl rollout restart" in exc_info.value.detail


class TestExecInPodEmptyContainer:
    """kubectl --container <empty> is rejected; the helper must skip that flag."""

    def test_empty_container_skips_flag(self):
        from src.services.helm import KubernetesService
        with patch.object(KubernetesService, "_run_command") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            KubernetesService().exec_in_pod(
                namespace="paas-ws-test",
                pod_name="mcp-server-abc",
                container="",
                command=["echo", "hi"],
                timeout=10,
            )
            args = mock_run.call_args.args[0]
            # No "--container" flag should be present when caller left it empty.
            assert "--container" not in args
            assert args[0:2] == ["exec", "mcp-server-abc"]
            assert args[-3:] == ["--", "echo", "hi"]

    def test_named_container_includes_flag(self):
        from src.services.helm import KubernetesService
        with patch.object(KubernetesService, "_run_command") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            KubernetesService().exec_in_pod(
                namespace="paas-ws-test",
                pod_name="mcp-server-abc",
                container="mcp-server",
                command=["echo", "hi"],
                timeout=10,
            )
            args = mock_run.call_args.args[0]
            assert "--container" in args
            assert "mcp-server" in args