"""#552 層 4：reset-config 對**遠端** chart ref 必須要求明確版本（fail-closed）。

## 為什麼是這個端點

reset 是一次真正的 `helm upgrade`（兩條載體分支都是），而 `helm_service.upgrade`
對 chart 必填（helm 不保存原始 chart URL）⇒ chart 一定會被重新解析。少了
`--version`，helm 取 registry 當下最新那一版：租戶按一下換密碼，底層 chart 靜默
跳版，而 `--atomic` 只在**失敗**時回滾，成功跳版永久生效。

## 為什麼守在載體分流之前

`reset_release_config` 在 `:879` 依 `admin_password_carrier` 分成兩個**獨立實作**，
各有自己的 `helm_service.upgrade(` 呼叫點。守在任一個呼叫點必漏另一條
（open-design / hermes-agent 走 helm_secret 那條）。本檔對**兩個載體**都斷言。

## 為什麼不是無條件判空

`--version` 只對需要解析的 ref 有意義。本機路徑／`.tgz` 的版本寫在 chart 自己的
`Chart.yaml` 裡，helm 不會查任何 registry ⇒ 對它們判空等於宣稱一件不成立的事，
而且會誤擋 integration 測試實際在用的本機 chart。判準因此是「helm 需不需要去
外面查」。
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.releases import _chart_ref_is_remote, reset_release_config
from src.models.schemas import ReleaseResetConfigRequest, ReleaseStatus
from src.models.schemas import ReleaseInfo


def _request(**overrides):
    kwargs = dict(
        values={},
        sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        chart="oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server",
        version="0.1.5",
    )
    kwargs.update(overrides)
    return ReleaseResetConfigRequest(**kwargs)


def _release():
    return ReleaseInfo(
        name="svc-abc", namespace="paas-ws-test", revision=3,
        status=ReleaseStatus.DEPLOYED, chart="mcp-server", chart_version="0.1.5",
        app_version="1.0.0", updated="2026-08-03T00:00:00Z",
    )


class TestChartRefIsRemote:
    """判準本身：這個 ref 會不會被 helm 拿去 registry 解析。"""

    def test_oci_ref_is_remote(self):
        assert _chart_ref_is_remote("oci://registry-1.docker.io/bitnamicharts/odoo")

    def test_classic_repo_ref_is_remote(self):
        """⚠ 這條是「別寫成 oci:// 前綴白名單」的理由——classic ref 一樣會跳版。"""
        assert _chart_ref_is_remote("bitnami/postgresql")

    def test_local_chart_directory_is_not_remote(self, tmp_path):
        chart_dir = tmp_path / "probe-chart"
        chart_dir.mkdir()
        assert not _chart_ref_is_remote(str(chart_dir))

    def test_missing_chart_is_not_this_gates_business(self):
        """chart 缺席由 `helm_service.upgrade` 自己 raise ValueError。

        這裡回 False 讓那個錯誤照原路拋出自己的訊息，不要把它偽裝成「沒釘版本」。
        """
        assert not _chart_ref_is_remote(None)
        assert not _chart_ref_is_remote("")


@pytest.mark.parametrize("carrier", ["config_json", "helm_secret"])
class TestResetRequiresVersionForRemoteChart:
    """🔴 兩個載體都要擋——只擋一個等於另一條分支照樣跳版。"""

    def test_remote_chart_without_version_is_rejected(self, carrier):
        with pytest.raises(HTTPException) as err:
            reset_release_config(
                "paas-ws-test", "svc-abc",
                _request(version=None, admin_password_carrier=carrier),
            )
        assert err.value.status_code == 400
        assert "chart version" in err.value.detail

    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1"])
    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="d1")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="p1")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_remote_chart_with_version_still_passes(
        self, mock_get, mock_upgrade, mock_restart, mock_pod, mock_exec,
        mock_dep_name, mock_dep_names, carrier,
    ):
        """陽性對照：閘門不是恆擋——不然上一條零鑑別力。"""
        mock_get.return_value = MagicMock(revision=2)
        mock_upgrade.return_value = _release()

        result = reset_release_config(
            "paas-ws-test", "svc-abc",
            _request(version="0.1.5", admin_password_carrier=carrier),
        )

        assert result["admin_password_carrier"] == carrier
        assert mock_upgrade.called, "前提：真的走到了 helm upgrade"

    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["d1"])
    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="d1")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="p1")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_local_chart_without_version_is_not_blocked(
        self, mock_get, mock_upgrade, mock_restart, mock_pod, mock_exec,
        mock_dep_name, mock_dep_names, carrier, tmp_path,
    ):
        """本機 chart 的版本在 Chart.yaml 裡，helm 不會查 registry ⇒ 不該擋。

        少了這條，閘門會誤擋 integration 測試實際在用的本機 chart，而那個誤擋
        看起來就像「被測程式壞了」。
        """
        chart_dir = tmp_path / "local-chart"
        chart_dir.mkdir()
        mock_get.return_value = MagicMock(revision=2)
        mock_upgrade.return_value = _release()

        result = reset_release_config(
            "paas-ws-test", "svc-abc",
            _request(chart=str(chart_dir), version=None,
                     admin_password_carrier=carrier),
        )

        assert result["helm_upgraded"] is True
        assert mock_upgrade.called


class TestGateOrdering:
    """閘門必須在**分流之前**——用「被擋時兩條分支都沒動 helm」證明。"""

    @patch("src.api.releases._reset_via_helm_secret")
    @patch("src.api.releases.helm_service.upgrade")
    def test_helm_is_never_touched_when_the_gate_fires(
        self, mock_upgrade, mock_secret_branch,
    ):
        with pytest.raises(HTTPException):
            reset_release_config(
                "paas-ws-test", "svc-abc",
                _request(version=None, admin_password_carrier="helm_secret"),
            )
        assert not mock_secret_branch.called, (
            "閘門若排在分流之後，helm_secret 分支會先被叫進去——那條有自己的 "
            "helm upgrade 呼叫點，等於漏守")
        assert not mock_upgrade.called
