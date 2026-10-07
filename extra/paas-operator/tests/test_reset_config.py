"""Tests for #357 surgical admin-password reset (+ #449 carrier branching)."""
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.releases import (
    RESET_CLEANUP_EXEC_TIMEOUT_SECONDS,
    RESET_EXEC_TIMEOUT_SECONDS,
    RESET_HARD_CAP_SECONDS,
    RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS,
    RESET_HELM_TIMEOUT_SECONDS,
    RESET_KUBECTL_SLACK_SECONDS,
    RESET_QUEUE_TIMEOUT_SECONDS,
    _validate_namespace_or_raise,
    reset_release_config,
)
from src.config import settings
from src.models.schemas import ReleaseResetConfigRequest
from src.services.helm import (
    HELM_CLI_OVERHEAD_SECONDS,
    HelmException,
    KubectlException,
    KubernetesService,
)


def _request(**overrides):
    kwargs = dict(
        values={"config": {"nonSensitive": {"connection": {"url": "https://tenant.example"}}}},
        sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        chart="oci://example/mcp-server",
        version="0.1.5",
    )
    kwargs.update(overrides)
    return ReleaseResetConfigRequest(**kwargs)


def _helm_secret_request(**overrides):
    """#449：hermes-agent / open-design 形狀（同一 sensitive_values 結構，換載體）。"""
    kwargs = dict(
        admin_password_carrier="helm_secret",
        chart="oci://example/hermes-agent",
        version="0.1.0",
    )
    kwargs.update(overrides)
    return _request(**kwargs)


class TestResetConfigSchema:
    def test_defaults(self):
        request = ReleaseResetConfigRequest()
        assert request.values == {}
        assert request.sensitive_values == {}
        assert request.reuse_values is True
        # #411 A 案接通：欄位存在且預設 False（不主動改 helm 語意）。
        assert request.reset_values is False

    def test_carrier_defaults_to_config_json(self):
        """#449 的向後相容支點：舊平台不帶本欄 → 既有 saga，零行為變化。

        operator 必須能先發版、平台後發版；這條斷言就是那個順序安全的契約。
        """
        assert ReleaseResetConfigRequest().admin_password_carrier == "config_json"

    def test_carrier_rejects_unknown_value(self):
        """未知載體要 422 大聲拒絕，不可靜默 fallback 成 config_json。

        靜默 fallback 會讓「平台打錯字」表現成 #449 本身（helm_secret chart 被
        當成 mcp 家族做 exec patch → 500），把設定錯誤偽裝成基礎設施故障。
        """
        with pytest.raises(ValidationError):
            ReleaseResetConfigRequest(admin_password_carrier="chart_secret")

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
        assert response["admin_password_carrier"] == "config_json"
        mock_upgrade.assert_called_once_with(
            namespace="paas-ws-test",
            name="svc-abc",
            chart="oci://example/mcp-server",
            # #448/#449：reset 路也要注入 pull secret（呼叫端一旦傳
            # reuse_values=False 就會踩到與 upgrade 端點同一個 gap）。
            values={
                **_request().values,
                "imagePullSecrets": [{"name": settings.registry_secret_name}],
            },
            sensitive_values=_request().sensitive_values,
            version="0.1.5",
            reset_values=False,
            reuse_values=True,
            atomic=True,
            # #461：兩個預算都要傳。少了 subprocess_timeout_seconds，atomic 失敗
            # 時的自動 rollback 會被 SIGKILL 腰斬（見 T7 的算式守線）。
            timeout_seconds=RESET_HELM_TIMEOUT_SECONDS,
            subprocess_timeout_seconds=RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS,
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
        # #461：restart 只下達、**不等**。等 rollout 就是把呼叫端預算綁在 image
        # pull／RWO PVC re-attach 上（#459 實測冷 cache 必逾時），而密碼在 helm
        # upgrade 回來時就已經是權威值了。
        mock_status.assert_not_called()
        assert response["rollout_awaited"] is False
        assert mock_exec.call_count == 2  # patch + backup cleanup

    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp-server")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_converges_default_sa(
        self, mock_get_release, mock_upgrade, mock_restart, mock_status,
        mock_pod, mock_exec, mock_deployment, sa_pull_secret_convergence,
    ):
        """#423：reset 與 install/upgrade 共用同一個 helper，SA 收斂一樣要生效。"""
        reset_release_config("paas-ws-test", "svc-abc", _request())
        sa_pull_secret_convergence.assert_called_once_with("paas-ws-test")

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
    def test_restart_issue_failure_restores_config_and_secret(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_restart,
        mock_status, mock_rollback, mock_deployment,
    ):
        """#461：觸發點由「rollout status 逾時」改成「restart 下達失敗」。

        rollout 的**完成**已移出 request，所以那裡不再有失敗可補償；仍然同步的
        最後一步是「下達 restart」，它失敗時補償語意與原本完全相同（還原
        config.json ＋ helm rollback ＋ 重新下達 restart）。
        """
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_restart.side_effect = [
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
        # #461：補償路徑同樣不等 rollout——否則一個失敗的 reset 要 2×130s 才回
        # 500，補償反而衝破本次要建立的預算天花板。
        mock_status.assert_not_called()

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


class TestResetConfigHelmSecretCarrier:
    """#449：載體＝chart Secret 的 reset 路（hermes-agent / open-design）。"""

    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch(
        "src.api.releases.k8s_service.get_deployment_names",
        return_value=["svc-abc-hermes-agent", "svc-abc-hermes-agent-console"],
    )
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_upgrades_and_restarts_every_deployment_without_touching_config_json(
        self, mock_get, mock_upgrade, mock_names, mock_restart, mock_status,
        mock_pod, mock_exec,
    ):
        response = reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())

        assert response["helm_revision"] == 8
        assert response["admin_password_carrier"] == "helm_secret"
        assert response["config_json_patched"] is False
        assert response["restarted_deployments"] == [
            "svc-abc-hermes-agent", "svc-abc-hermes-agent-console",
        ]

        # #449 的核心：這條路一次都不能 exec 進 pod。config.json 不存在，patch
        # 會炸、備份建不起來、rollback 跟著炸 —— 那就是 issue 描述的 500。
        mock_exec.assert_not_called()
        mock_pod.assert_not_called()

        # 兩顆 Deployment 都要下 restart（少下一顆＝那個入口還吃舊密碼，#449 的
        # 靜默失敗），但 #461 起**不等** rollout 完成。
        assert [c.kwargs["deployment_name"] for c in mock_restart.call_args_list] == [
            "svc-abc-hermes-agent", "svc-abc-hermes-agent-console",
        ]
        mock_status.assert_not_called()
        assert response["rollout_awaited"] is False
        mock_names.assert_called_once_with(
            namespace="paas-ws-test",
            label_selector="app.kubernetes.io/instance=svc-abc",
        )

    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_injects_image_pull_secrets(
        self, mock_get, mock_upgrade, mock_names, mock_restart, mock_status,
    ):
        """#448 的 gap 兩條 reset 路都要收掉，不是只收 config_json 路。"""
        reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())

        sent = mock_upgrade.call_args.kwargs["values"]
        assert sent["imagePullSecrets"] == [{"name": settings.registry_secret_name}]
        # 呼叫端原本的 values 不可被吃掉。
        assert sent["config"] == _helm_secret_request().values["config"]

    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_converges_default_sa(
        self, mock_get, mock_upgrade, mock_names, mock_restart, mock_status,
        sa_pull_secret_convergence,
    ):
        """#423：helm_secret 這條 reset 路同樣要收斂 ns default SA。"""
        reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())
        sa_pull_secret_convergence.assert_called_once_with("paas-ws-test")

    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_forwards_reset_values_and_upgrades_atomically(
        self, mock_get, mock_upgrade, mock_names, mock_restart, mock_status,
    ):
        reset_release_config(
            "paas-ws-test", "svc-abc",
            _helm_secret_request(reset_values=True, reuse_values=False),
        )

        kwargs = mock_upgrade.call_args.kwargs
        assert kwargs["reset_values"] is True
        assert kwargs["reuse_values"] is False
        # atomic：upgrade 自己失敗時 helm 自動回滾，fail-safe 不靠我們補償。
        assert kwargs["atomic"] is True
        assert kwargs["sensitive_values"] == _helm_secret_request().sensitive_values

    @patch("src.api.releases.k8s_service.get_deployment_names")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_upgrade_failure_does_not_restart_anything(
        self, mock_get, mock_upgrade, mock_names,
    ):
        mock_upgrade.side_effect = HelmException("failed", "helm", "stderr")
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())
        assert error.value.status_code == 500
        assert "chart Secret" in error.value.detail
        mock_names.assert_not_called()

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1", "d2"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_restart_issue_failure_rolls_helm_back_and_re_rolls_pods(
        self, mock_get, mock_upgrade, mock_names, mock_restart, mock_status,
        mock_rollback,
    ):
        """補償必須「helm rollback + 再滾一次 pod」——只 rollback 不夠。

        Secret 回到舊密碼，但已經重啟成功的那顆 pod 仍跑著新密碼；不再滾一次，
        叢集就停在「Secret 說 A、pod 用 B」的分裂狀態。

        #461：觸發點改成「第二顆的 restart 下達失敗」（rollout 的完成已不在
        request 內，那裡沒有可補償的失敗了）。這也是最貼近真實的形狀：d1 已滾、
        d2 沒滾 ⇒ 半套狀態，正是補償要收的。
        """
        mock_restart.side_effect = [
            None,  # d1 下達成功
            KubectlException("failed", "kubectl", "stderr"),  # d2 失敗
            None, None,  # 補償重新下達
        ]

        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())

        assert error.value.status_code == 500
        assert "previous password restored" in error.value.detail
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)
        # 主路 2 次（d1 成功、d2 炸）＋補償再 2 次。
        assert len(mock_restart.call_args_list) == 4
        assert mock_restart.call_args_list[-1].args == ("paas-ws-test", "d2")
        # 補償同樣不等 rollout（否則失敗路徑要 2×130s 才回 500）。
        mock_status.assert_not_called()

    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.k8s_service.rollout_status_deployment")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1"])
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_recovery_failure_asks_for_intervention(
        self, mock_get, mock_upgrade, mock_names, mock_restart, mock_status,
        mock_rollback,
    ):
        mock_restart.side_effect = KubectlException("failed", "kubectl", "stderr")
        mock_rollback.side_effect = HelmException("rollback failed", "helm", "stderr")

        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())

        assert error.value.status_code == 500
        assert "operator intervention required" in error.value.detail
        # helm rollback 失敗時 Secret 還停在新密碼；再滾 pod 只會把「沒人持有的
        # 密碼」推上線，所以補償必須到此為止（主路的 1 次 restart 之外不再滾）。
        assert len(mock_restart.call_args_list) == 1

    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch(
        "src.api.releases.k8s_service.get_deployment_names",
        side_effect=KubectlException("no deployments", "kubectl", ""),
    )
    @patch("src.api.releases.helm_service.rollback")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_discovery_failure_still_rolls_helm_back(
        self, mock_get, mock_upgrade, mock_rollback, mock_names, mock_exec,
    ):
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _helm_secret_request())

        assert error.value.status_code == 500
        mock_rollback.assert_called_once_with("paas-ws-test", "svc-abc", 7)
        mock_exec.assert_not_called()

    def test_still_rejects_missing_password(self):
        """400 前置檢查在分流之前，兩條路共用。"""
        with pytest.raises(HTTPException) as error:
            reset_release_config(
                "paas-ws-test", "svc-abc",
                ReleaseResetConfigRequest(admin_password_carrier="helm_secret"),
            )
        assert error.value.status_code == 400


class TestResetBudgetLadder:
    """#461：reset 的時間預算是算出來的，不是猜的。"""

    def test_subprocess_budget_covers_the_atomic_rollback_too(self):
        """helm `--timeout` 是 per-operation ⇒ atomic 失敗要吃兩次 wait。

        修前（也是本 repo 改動前的實況）：subprocess 上限沒有把「自動 rollback 的
        wait」算進去 ⇒ helm 會在 rollback **進行中**被 SIGKILL ⇒ release 卡
        pending-*、Secret 狀態不明 ⇒ 打破「2xx 之前失敗保證叢集仍是舊密碼」。
        這條斷言把 `2n + O` 的會計釘死；只改 n 或只改 O 都會紅。
        """
        assert RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS == (
            2 * RESET_HELM_TIMEOUT_SECONDS + HELM_CLI_OVERHEAD_SECONDS
        )
        # 不只釘算式、也釘值：算式對但 n 被改小到不夠 helm 跑完一次 upgrade，
        # 對租戶就是「reset 永遠失敗」。
        assert RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS == 45
        assert RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS > RESET_HELM_TIMEOUT_SECONDS * 2

    def test_hard_cap_equals_sum_of_its_parts(self):
        """硬上限＝各子步之和；外層（平台 75s／前端 90s）必須大於它。

        跨層的那道不等式由 scripts/audit/repo-lint.py 的
        check_reset_budget_ladder 驗（它讀得到平台與前端的常數，這裡讀不到）。
        """
        assert RESET_HARD_CAP_SECONDS == (
            RESET_QUEUE_TIMEOUT_SECONDS  # #1552：等 helm 名額
            + RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS
            + RESET_EXEC_TIMEOUT_SECONDS
            + RESET_CLEANUP_EXEC_TIMEOUT_SECONDS
            + RESET_KUBECTL_SLACK_SECONDS
        )
        assert RESET_HARD_CAP_SECONDS == 73


class TestResetBackupHygiene:
    """#461：不等 rollout ⇒ 備份清理常常來不及，殘留必須有上限。"""

    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp-server")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_patch_script_sweeps_stale_backups_first(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_restart, mock_deployment,
    ):
        """patch 腳本開頭要先清掉前一次殘留的 backup。

        收尾的 cleanup exec 是 best-effort，而 restart 一下達 pod 就開始
        Terminating ⇒ 它常常失敗 ⇒ 每次 reset 都可能留下一個含**上一把明文密碼**
        的檔。腳本自清把殘留從「每次 reset 累積一個」壓成「每個 release 最多一個」。
        """
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")

        reset_release_config("paas-ws-test", "svc-abc", _request())

        script = mock_exec.call_args_list[0].kwargs["command"][2]
        assert "glob.glob('/data/.config.json.reset-backup-*')" in script
        assert "os.unlink" in script
        # 清除必須發生在讀取原檔之前（否則會把這次剛建的備份也掃掉）。
        assert script.index("glob.glob") < script.index("original=open")

    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp-server")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_exec_timeouts_come_from_the_budget_constants(
        self, mock_get, mock_upgrade, mock_pod, mock_exec, mock_restart, mock_deployment,
    ):
        """兩次 exec 的 timeout 必須來自預算常數，不能是散落的字面 30。

        散落的字面值就是 #461 的成因：硬上限算式與實際傳給 kubectl 的值各走各的，
        改一邊不會有人發現。
        """
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")

        reset_release_config("paas-ws-test", "svc-abc", _request())

        assert mock_exec.call_args_list[0].kwargs["timeout"] == RESET_EXEC_TIMEOUT_SECONDS
        assert mock_exec.call_args_list[1].kwargs["timeout"] == RESET_CLEANUP_EXEC_TIMEOUT_SECONDS


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


class TestGetDeploymentNames:
    """#449: the plural primitive — multi-Deployment charts are the norm."""

    def test_returns_every_match(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(
                returncode=0,
                stdout="svc-abc-hermes-agent svc-abc-hermes-agent-console\n",
                stderr="",
            )
            names = KubernetesService().get_deployment_names(
                namespace="paas-ws-test",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
        assert names == ["svc-abc-hermes-agent", "svc-abc-hermes-agent-console"]

    def test_single_match_is_a_one_element_list(self):
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(
                returncode=0, stdout="svc-abc-mcp-server\n", stderr="",
            )
            names = KubernetesService().get_deployment_names(
                namespace="paas-ws-test",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
        assert names == ["svc-abc-mcp-server"]

    def test_zero_matches_stays_loud(self):
        """空集合不可回 []：靜默不重啟＝回報了一次沒到達任何 pod 的輪替。"""
        with patch.object(KubernetesService, "_run_command") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with pytest.raises(KubectlException) as error:
                KubernetesService().get_deployment_names(
                    namespace="paas-ws-test",
                    label_selector="app.kubernetes.io/instance=svc-gone",
                )
        assert "No deployments found" in error.value.message

    def test_rejects_namespace_outside_prefix(self):
        with pytest.raises(ValueError):
            KubernetesService().get_deployment_names(
                namespace="kube-system",
                label_selector="app.kubernetes.io/instance=svc-abc",
            )
