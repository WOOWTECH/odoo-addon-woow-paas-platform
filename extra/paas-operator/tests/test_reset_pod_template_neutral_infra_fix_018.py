"""INFRA-FIX-018（商用驗收 INFRA-HS-002-1）：reset-config 不得改 pod template。

現場（Headscale 238，ns paas-ws-5e41efa6，2026-10-16 10:45～10:51Z）：真 UI 改管理員帳密
3/3 次失敗。operator 的 helm_secret 路是 ``helm upgrade --atomic --timeout 15s``，而平台送來的
values 是 DB 快照——快照裡的方案資源是舊 overlay 的**頂層** ``resources``（chart 把它 deep-merge
進 headplane），叢集現況卻是 #1637 之後的 ``headscale.resources``。於是 reset 把兩個容器的
resources 對調 ⇒ pod template 改變 ⇒ StatefulSet 滾動 ⇒ RWO 重新 attach 45 秒以上 ⇒ 15 秒逾時、
atomic 回滾又滾一次；每次多 2 筆 failed revision（8～13），租戶看到「重設失敗」。

``helm get values`` 實測（rev 8 為失敗的 reset，rev 17 為回復到 rev 7 的內容）：
    只在 rev 8：``resources.{limits,requests}.{cpu,memory}`` = 1／1Gi／1／1Gi
    只在 rev 17：``headscale.resources`` = limits 1／1Gi、requests 250m／512Mi
本檔用同形狀的 values 守住：reset 前先把這類鍵釘到叢集現況，pod template 不再被 reset 改動。
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.releases import (
    RESET_CLEANUP_EXEC_TIMEOUT_SECONDS,
    RESET_EXEC_TIMEOUT_SECONDS,
    RESET_HARD_CAP_SECONDS,
    RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS,
    RESET_KUBECTL_SLACK_SECONDS,
    RESET_LIVE_VALUES_TIMEOUT_SECONDS,
    RESET_QUEUE_TIMEOUT_SECONDS,
    overlay_preserved_paths,
    reset_release_config,
)
from src.models.schemas import ReleaseResetConfigRequest
from src.services.helm import HelmException, HelmService

# 平台 DB 快照（舊 overlay 的頂層 resources，chart 0.1.4 會把它蓋到 headplane）。
STORED_SNAPSHOT = {
    "headscale": {"dns": {"nameservers": "cloudflare"}},
    "entrance": {"basicAuthUsername": "qaadmin1011"},
    "resources": {
        "limits": {"cpu": "1", "memory": "1Gi"},
        "requests": {"cpu": "1", "memory": "1Gi"},
    },
}
# 叢集現況（#1637 之後：方案資源寫在 headscale.resources，頂層 resources 不存在）。
LIVE_VALUES = {
    "headscale": {
        "dns": {"nameservers": "cloudflare"},
        "resources": {
            "limits": {"cpu": "1", "memory": "1Gi"},
            "requests": {"cpu": "250m", "memory": "512Mi"},
        },
    },
    "entrance": {"basicAuthUsername": "qaadmin1011"},
    "config": {"sensitive": {"admin_password": "old-secret-never-asserted-on"}},
}
PATHS = ["headscale.resources", "resources"]


def _request(**overrides):
    kwargs = dict(
        admin_password_carrier="helm_secret",
        chart="oci://example/headscale",
        version="0.1.4",
        values=STORED_SNAPSHOT,
        sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        reset_values=True,
        reuse_values=False,
        preserve_value_paths=PATHS,
    )
    kwargs.update(overrides)
    return ReleaseResetConfigRequest(**kwargs)


class TestSchema:
    def test_default_is_empty_so_an_older_platform_changes_nothing(self):
        assert ReleaseResetConfigRequest().preserve_value_paths == []

    @pytest.mark.parametrize("bad", ["a b", "a..b", "resources[0]", "a.*", "", ".a"])
    def test_rejects_malformed_paths(self, bad):
        with pytest.raises(ValidationError):
            ReleaseResetConfigRequest(preserve_value_paths=[bad])

    def test_caps_the_number_of_paths(self):
        with pytest.raises(ValidationError):
            ReleaseResetConfigRequest(preserve_value_paths=[f"p{i}" for i in range(17)])


class TestOverlayShape:
    """純函式層：真實 HS-002 形狀下，釘完之後這兩條 resources 鍵與叢集一致。"""

    def test_stored_snapshot_diverges_from_live_before_pinning(self):
        # 這就是缺陷本身：不釘的話 reset 送出去的 resources 與叢集不同。
        assert STORED_SNAPSHOT.get("headscale", {}).get("resources") is None
        assert "resources" in STORED_SNAPSHOT and "resources" not in LIVE_VALUES

    def test_pinned_paths_equal_live_and_everything_else_is_untouched(self):
        pinned = overlay_preserved_paths(STORED_SNAPSHOT, LIVE_VALUES, PATHS)
        assert pinned["headscale"]["resources"] == LIVE_VALUES["headscale"]["resources"]
        # 叢集沒有頂層 resources ⇒ 快照那份舊 overlay 殘值必須被拿掉（不是留著）。
        assert "resources" not in pinned
        # 沒列在 paths 的鍵不從叢集帶（尤其憑證：config.sensitive 絕不能被帶進來）。
        assert pinned["headscale"]["dns"] == STORED_SNAPSHOT["headscale"]["dns"]
        assert "config" not in pinned
        # 不改呼叫端的物件。
        assert "resources" in STORED_SNAPSHOT


def _workloads(statefulsets=("svc-abc-headscale",)):
    return patch(
        "src.api.releases._credential_reset_workloads",
        return_value=([], list(statefulsets)),
    )


class TestResetUsesLiveValues:
    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.helm_service.get_values", return_value=LIVE_VALUES)
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_upgrade_receives_values_pinned_to_the_live_release(
        self, mock_get, mock_upgrade, mock_live, mock_restart,
    ):
        with _workloads():
            response = reset_release_config("paas-ws-test", "svc-abc", _request())

        assert response["helm_revision"] == 8
        mock_live.assert_called_once_with(
            "paas-ws-test", "svc-abc", timeout_seconds=RESET_LIVE_VALUES_TIMEOUT_SECONDS,
        )
        sent = mock_upgrade.call_args.kwargs["values"]
        # 紅綠核心：修前送的是 STORED_SNAPSHOT（頂層 resources、無 headscale.resources）。
        assert sent["headscale"]["resources"] == LIVE_VALUES["headscale"]["resources"]
        assert "resources" not in sent
        # 憑證仍只走 sensitive_values；叢集的舊密碼沒有被帶進 values。
        assert "config" not in sent
        assert mock_upgrade.call_args.kwargs["sensitive_values"] == {
            "config": {"sensitive": {"admin_password": "new-pwd-xyz"}},
        }
        # atomic 與預算旋鈕不變。
        assert mock_upgrade.call_args.kwargs["atomic"] is True
        assert (
            mock_upgrade.call_args.kwargs["subprocess_timeout_seconds"]
            == RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS
        )
        mock_restart.assert_called_once()

    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.helm_service.get_values")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_without_paths_behaviour_is_exactly_as_before(
        self, mock_get, mock_upgrade, mock_live, mock_restart,
    ):
        """舊平台（不帶欄位）：不讀叢集、values 照送快照（operator 可先發版）。"""
        with _workloads():
            reset_release_config(
                "paas-ws-test", "svc-abc", _request(preserve_value_paths=[]),
            )

        mock_live.assert_not_called()
        sent = mock_upgrade.call_args.kwargs["values"]
        assert sent["resources"] == STORED_SNAPSHOT["resources"]
        assert "resources" not in sent.get("headscale", {})

    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.helm_service.get_values")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_a_path_missing_on_both_sides_stays_missing(
        self, mock_get, mock_upgrade, mock_live, mock_restart,
    ):
        mock_live.return_value = {"entrance": {"basicAuthUsername": "qaadmin1011"}}
        with _workloads():
            reset_release_config(
                "paas-ws-test", "svc-abc",
                _request(values={"entrance": {"basicAuthUsername": "qaadmin1011"}}),
            )
        sent = mock_upgrade.call_args.kwargs["values"]
        assert "resources" not in sent
        assert "resources" not in sent.get("headscale", {})

    @pytest.mark.parametrize("failure", [
        HelmException(message="boom", command="helm get values", stderr="x"),
        ValueError("namespace"),
    ])
    @patch("src.api.releases.k8s_service.rollout_restart_statefulset")
    @patch("src.api.releases.helm_service.get_values")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_unreadable_live_release_fails_closed_before_any_write(
        self, mock_get, mock_upgrade, mock_live, mock_restart, failure,
    ):
        mock_live.side_effect = failure
        with pytest.raises(HTTPException) as error:
            reset_release_config("paas-ws-test", "svc-abc", _request())

        assert error.value.status_code == 500
        assert "nothing was changed" in error.value.detail
        # 什麼都沒動：沒 upgrade、沒 restart、沒讀 revision。
        mock_upgrade.assert_not_called()
        mock_restart.assert_not_called()
        mock_get.assert_not_called()

    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.get_values")
    @patch("src.api.releases.helm_service.upgrade", return_value=MagicMock(revision=8))
    @patch("src.api.releases.helm_service.get", return_value=MagicMock(revision=7))
    def test_config_json_carrier_ignores_the_field(
        self, mock_get, mock_upgrade, mock_live, mock_restart, mock_deployment,
        mock_exec, mock_pod,
    ):
        """config_json 路沒有空的預算可借（exec＋cleanup 都要用），所以不讀、不改。"""
        mock_exec.return_value = MagicMock(returncode=0, stdout="", stderr="")
        reset_release_config(
            "paas-ws-test", "svc-abc",
            _request(admin_password_carrier="config_json"),
        )
        mock_live.assert_not_called()
        assert mock_upgrade.call_args.kwargs["values"]["resources"] == STORED_SNAPSHOT["resources"]


class TestBudgetLadderIsUnchanged:
    def test_live_read_borrows_from_budget_the_helm_secret_path_never_spends(self):
        # helm_secret 路沒有 config.json exec 與備份清理；讀 values 不得超過這兩段之和。
        assert RESET_LIVE_VALUES_TIMEOUT_SECONDS <= (
            RESET_EXEC_TIMEOUT_SECONDS + RESET_CLEANUP_EXEC_TIMEOUT_SECONDS
        )
        # 硬上限仍是原本那條算式（沒有為新步驟加項目）：平台 75s 的階梯因此不動。
        assert RESET_HARD_CAP_SECONDS == (
            RESET_QUEUE_TIMEOUT_SECONDS
            + RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS
            + RESET_EXEC_TIMEOUT_SECONDS
            + RESET_CLEANUP_EXEC_TIMEOUT_SECONDS
            + RESET_KUBECTL_SLACK_SECONDS
        )

    @patch("subprocess.run")
    def test_get_values_passes_the_small_budget_to_the_subprocess(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"a": 1}', stderr="")
        HelmService().get_values("paas-ws-x", "svc-x", timeout_seconds=5)
        assert mock_run.call_args.kwargs["timeout"] == 5

    @patch("subprocess.run")
    def test_get_values_default_timeout_is_unchanged(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        service = HelmService()
        service.get_values("paas-ws-x", "svc-x")
        assert mock_run.call_args.kwargs["timeout"] == service.timeout
