"""helm_secret reset 也要滾 opt-in 的 StatefulSet（hermes-agent 0.3.0，平台 #1360 後續）。

hermes-agent 0.3.0 把 agent 從 Deployment 改成 StatefulSet（pod 名固定，console Role 才能
用 resourceNames 綁死 pods/exec）。agent 啟動時從 env 讀 admin_password，所以重設密碼若只
滾 Deployment，就會回成功、agent 卻還吃舊密碼——#449 那種靜默失敗。chart 在 StatefulSet 上
打 ``paas.woowtech.io/restart-on-credential-reset=true``，operator 只滾有這個 label 的
（open-design 0.3.0 的 daemon 熱載 htpasswd，不可被重啟）。
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.releases import reset_release_config
from src.models.schemas import ReleaseResetConfigRequest
from src.services.helm import (
    CREDENTIAL_RESET_RESTART_LABEL,
    HelmException,
    KubectlException,
    KubernetesService,
)


def _req() -> ReleaseResetConfigRequest:
    return ReleaseResetConfigRequest(
        admin_password_carrier="helm_secret",
        chart="oci://example/hermes-agent",
        version="0.3.0",
        values={"config": {"nonSensitive": {}}},
        sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
    )


class TestResetRollsOptedInStatefulSets:

    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names",
           return_value=["svc-abc-hermes-agent-console"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_restarts_console_deployment_and_agent_statefulset(
        self, mock_get, mock_upgrade, mock_deps, mock_restart_dep, mock_restart_sts,
        credential_reset_statefulsets,
    ):
        credential_reset_statefulsets.return_value = ["svc-abc-hermes-agent"]

        response = reset_release_config("paas-ws-test", "svc-abc", _req())

        credential_reset_statefulsets.assert_called_once_with(
            namespace="paas-ws-test",
            label_selector="app.kubernetes.io/instance=svc-abc",
        )
        assert [c.kwargs["deployment_name"] for c in mock_restart_dep.call_args_list] == [
            "svc-abc-hermes-agent-console",
        ]
        assert [c.kwargs["statefulset_name"] for c in mock_restart_sts.call_args_list] == [
            "svc-abc-hermes-agent",
        ]
        assert response["restarted_deployments"] == ["svc-abc-hermes-agent-console"]
        assert response["restarted_statefulsets"] == ["svc-abc-hermes-agent"]
        assert response["rollout_awaited"] is False

    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_console_disabled_zero_deployments_is_fine_when_statefulset_opted_in(
        self, mock_get, mock_upgrade, mock_deps, mock_restart_dep, mock_restart_sts,
        credential_reset_statefulsets,
    ):
        """hermes-agent console.enabled=false：release 一顆 Deployment 都沒有。"""
        mock_deps.side_effect = KubectlException(
            "No deployments found matching selector 'app.kubernetes.io/instance=svc-abc'",
            "kubectl", "",
        )
        credential_reset_statefulsets.return_value = ["svc-abc-hermes-agent"]

        response = reset_release_config("paas-ws-test", "svc-abc", _req())

        mock_restart_dep.assert_not_called()
        assert mock_restart_sts.call_count == 1
        assert response["restarted_deployments"] == []
        assert response["restarted_statefulsets"] == ["svc-abc-hermes-agent"]

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_no_workload_at_all_stays_loud_and_rolls_back(
        self, mock_get, mock_upgrade, mock_deps, mock_restart_dep, mock_restart_sts,
        mock_rollback, credential_reset_statefulsets,
    ):
        mock_deps.side_effect = KubectlException(
            "No deployments found matching selector 'x'", "kubectl", "",
        )
        credential_reset_statefulsets.return_value = []

        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _req())

        assert error.value.status_code == 500
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)
        mock_restart_dep.assert_not_called()
        mock_restart_sts.assert_not_called()

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names",
           side_effect=KubectlException("connection refused", "kubectl", ""))
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_other_deployment_lookup_errors_are_not_swallowed(
        self, mock_get, mock_upgrade, mock_deps, mock_restart_dep, mock_restart_sts,
        mock_rollback, credential_reset_statefulsets,
    ):
        """只有「查無 Deployment」可以被 opt-in StatefulSet 頂替；kubectl 故障不行。"""
        credential_reset_statefulsets.return_value = ["svc-abc-hermes-agent"]

        with pytest.raises(HTTPException):
            reset_release_config("paas-ws-test", "svc-abc", _req())

        mock_restart_sts.assert_not_called()
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names",
           return_value=["svc-abc-hermes-agent-console"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_statefulset_restart_failure_rolls_back_and_re_rolls_both_kinds(
        self, mock_get, mock_upgrade, mock_deps, mock_restart_dep, mock_restart_sts,
        mock_rollback, credential_reset_statefulsets,
    ):
        credential_reset_statefulsets.return_value = ["svc-abc-hermes-agent"]
        mock_restart_sts.side_effect = [
            KubectlException("failed", "kubectl", "stderr"),  # 主路失敗
            None,  # 補償再滾
        ]

        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _req())

        assert "previous password restored" in error.value.detail
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)
        # 主路滾了 console 一次＋補償再滾一次；agent StatefulSet 主路一次（失敗）＋補償一次。
        assert mock_restart_dep.call_count == 2
        assert mock_restart_sts.call_count == 2
        assert mock_restart_sts.call_args_list[-1].args == ("paas-ws-test", "svc-abc-hermes-agent")

    @patch("src.api.releases.helm_service.rollback",
           side_effect=HelmException("rollback failed", "helm", "stderr"))
    @patch("src.api.releases.k8s_service.rollout_restart_statefulset",
           side_effect=KubectlException("failed", "kubectl", "stderr"))
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["c"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_helm_rollback_failure_does_not_re_roll(
        self, mock_get, mock_upgrade, mock_deps, mock_restart_dep, mock_restart_sts,
        mock_rollback, credential_reset_statefulsets,
    ):
        credential_reset_statefulsets.return_value = ["svc-abc-hermes-agent"]
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _req())
        assert "operator intervention required" in error.value.detail
        assert mock_restart_sts.call_count == 1



class TestStatefulSetOnlyRelease:
    """headscale／tailscale 0.1.1：release 只有一顆 opt-in StatefulSet、零 Deployment。

    這兩個 chart 從來沒有 Deployment。改之前正式站的重設必定 500——log 是
    「No deployments found matching selector」→ rollback。這裡只假 ``_run_command``
    （kubectl 的唯一出口），``get_deployment_names``／``get_credential_reset_statefulset_names``
    ／``rollout_restart_*`` 都走真的 ``KubernetesService`` 方法，順便把
    ``_credential_reset_workloads`` 賴以判斷「可被 StatefulSet 頂替」的那句
    「No deployments found」訊息契約也釘住。
    """

    @staticmethod
    def _fake_kubectl(statefulsets_stdout: str, calls: list):
        def run(args, *a, **kw):
            calls.append(list(args))
            if args[:2] == ["get", "deployments"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if args[:2] == ["get", "statefulsets"]:
                selector = args[args.index("--selector") + 1]
                assert f"{CREDENTIAL_RESET_RESTART_LABEL}=true" in selector
                return MagicMock(returncode=0, stdout=statefulsets_stdout, stderr="")
            if args[:2] == ["rollout", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected kubectl call: {args}")
        return run

    @staticmethod
    def _headscale_req() -> ReleaseResetConfigRequest:
        return ReleaseResetConfigRequest(
            admin_password_carrier="helm_secret",
            chart="oci://example/headscale",
            version="0.1.1",
            values={"config": {"nonSensitive": {}}},
            sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        )

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=3))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=2))
    def test_reset_succeeds_and_rolls_only_that_statefulset(
        self, mock_get, mock_upgrade, mock_rollback, credential_reset_statefulsets,
    ):
        from src.api import releases

        calls: list = []
        real = releases.k8s_service
        credential_reset_statefulsets.side_effect = (
            lambda **kw: KubernetesService.get_credential_reset_statefulset_names(real, **kw)
        )
        with patch.object(
            real, "_run_command", side_effect=self._fake_kubectl("svc-hs-headscale", calls),
        ):
            response = reset_release_config("paas-ws-test", "svc-hs", self._headscale_req())

        mock_rollback.assert_not_called()
        assert response["restarted_deployments"] == []
        assert response["restarted_statefulsets"] == ["svc-hs-headscale"]
        assert response["admin_password_carrier"] == "helm_secret"
        restarts = [c for c in calls if c[:2] == ["rollout", "restart"]]
        assert restarts == [
            ["rollout", "restart", "statefulset/svc-hs-headscale", "--namespace", "paas-ws-test"],
        ]

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=3))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=2))
    def test_without_opt_in_label_it_is_the_old_500_and_rollback(
        self, mock_get, mock_upgrade, mock_rollback, credential_reset_statefulsets,
    ):
        """chart 0.1.0（StatefulSet 沒帶 label）：照舊查無 → rollback → 500，什麼都不滾。"""
        from src.api import releases

        calls: list = []
        real = releases.k8s_service
        credential_reset_statefulsets.side_effect = (
            lambda **kw: KubernetesService.get_credential_reset_statefulset_names(real, **kw)
        )
        with patch.object(real, "_run_command", side_effect=self._fake_kubectl("", calls)):
            with pytest.raises(HTTPException) as error:
                reset_release_config("paas-ws-test", "svc-hs", self._headscale_req())

        assert error.value.status_code == 500
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-hs", 2)
        assert not [c for c in calls if c[:2] == ["rollout", "restart"]]


class TestKubernetesServiceStatefulSetPrimitives:

    def test_selector_adds_opt_in_label_and_empty_is_a_list(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            names = KubernetesService().get_credential_reset_statefulset_names(
                namespace="paas-ws-test",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
        assert names == []
        args = run.call_args.args[0]
        assert args[:2] == ["get", "statefulsets"]
        sel = args[args.index("--selector") + 1]
        assert sel == f"app.kubernetes.io/instance=svc-abc,{CREDENTIAL_RESET_RESTART_LABEL}=true"

    def test_returns_names(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(returncode=0, stdout="svc-abc-hermes-agent\n", stderr="")
            names = KubernetesService().get_credential_reset_statefulset_names(
                namespace="paas-ws-test",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
        assert names == ["svc-abc-hermes-agent"]

    def test_rollout_restart_statefulset_command(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            KubernetesService().rollout_restart_statefulset("paas-ws-test", "svc-abc-hermes-agent")
        assert run.call_args.args[0] == [
            "rollout", "restart", "statefulset/svc-abc-hermes-agent", "--namespace", "paas-ws-test",
        ]

    def test_rejects_namespace_outside_prefix(self):
        with pytest.raises(ValueError):
            KubernetesService().get_credential_reset_statefulset_names(
                namespace="kube-system", label_selector="app.kubernetes.io/instance=x",
            )
        with pytest.raises(ValueError):
            KubernetesService().rollout_restart_statefulset("kube-system", "x")
