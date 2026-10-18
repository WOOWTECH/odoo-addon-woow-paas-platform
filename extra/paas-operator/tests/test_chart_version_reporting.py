"""#552：operator 必須把 helm 實際解析出來的 chart 版本回報給平台。

## 缺陷

版本資訊是被**主動丟掉**的，兩個 parser 各丟一次：

* ``_parse_release_info``（install / upgrade 的 JSON 輸出）只讀
  ``chart.metadata.name``，隔壁的 ``version`` 從來沒被讀過。
* ``_parse_list_release_info``（``helm list`` 輸出）拿到的是
  ``"<name>-<version>"``，程式碼 ``rsplit("-", 1)[0]`` 明碼**只取前半**，註解
  自述 "without version"。

結果：``ReleaseInfo`` 裡根本沒有版本欄位 ⇒ Odoo 從頭到尾收不到實際版本 ⇒
``helm_chart_version`` 留空時 helm 解析成 registry **latest**，平台卻沒有任何
記錄，租戶在服務詳情頁看到的是首裝那一刻的舊值。

## 為什麼 reset 兩條分支都要測

``reset_release_config`` 在 ``releases.py`` 依 ``admin_password_carrier`` 分流成
**兩個獨立實作**（``config_json`` inline 路與 ``_reset_via_helm_secret``），各自
組自己的回應 dict。只補一條，走 ``helm_secret`` 的 open-design / hermes-agent
帳本仍然瞎——這正是 #449 註解說的「兩條 reset 路都必須自己補回來」。
"""
import json
from unittest.mock import MagicMock, patch

from src.api.releases import reset_release_config
from src.models.schemas import ReleaseInfo, ReleaseResetConfigRequest, ReleaseStatus
from src.services.helm import HelmService


def _release_info(**overrides):
    kwargs = dict(
        name="svc-abc",
        namespace="paas-ws-test",
        revision=3,
        status=ReleaseStatus.DEPLOYED,
        chart="mcp-server",
        chart_version="0.1.5",
        app_version="1.0.0",
        updated="2026-08-03T00:00:00Z",
    )
    kwargs.update(overrides)
    return ReleaseInfo(**kwargs)


class TestReleaseInfoSchema:
    """向後相容契約：欄位必須是 ``Optional[str] = None``。"""

    def test_chart_version_is_optional_and_defaults_to_none(self):
        """必填會同時打爆既有測試與「舊 operator 部署」的相容性。

        平台側是 ``release_info.get('chart_version') or <原值>``——只有在本欄
        缺席時能安全解讀成 None，平台才會「保留原值」而不是把帳本洗成空。
        """
        info = ReleaseInfo(
            name="svc-abc",
            namespace="paas-ws-test",
            revision=1,
            status=ReleaseStatus.DEPLOYED,
            chart="nginx",
            app_version="1.0.0",
            updated="2026-08-03T00:00:00Z",
        )
        assert info.chart_version is None

    def test_chart_version_survives_json_serialisation(self):
        """平台讀的是 HTTP JSON body，不是 python 物件。"""
        assert json.loads(_release_info().model_dump_json())["chart_version"] == "0.1.5"


class TestParseReleaseInfo:
    """install / upgrade 的 JSON 輸出（``chart.metadata``）。"""

    def _parse(self, metadata):
        return HelmService()._parse_release_info({
            "name": "svc-abc",
            "namespace": "paas-ws-test",
            "version": 2,
            "info": {"status": "deployed", "last_deployed": "x"},
            "chart": {"metadata": metadata},
        })

    def test_reads_version_from_chart_metadata(self):
        info = self._parse({"name": "n8n", "version": "2.0.1", "appVersion": "1.99"})
        # chart 名稱不得因此改變（同一份 metadata 的兩個 leaf）。
        assert (info.chart, info.chart_version) == ("n8n", "2.0.1")

    def test_missing_version_becomes_none_not_empty_string(self):
        """None 才是「不知道」；空字串會讓平台的 ``or`` fallback 語意分岔。"""
        assert self._parse({"name": "n8n", "appVersion": "1.99"}).chart_version is None

    def test_empty_version_is_normalised_to_none(self):
        assert self._parse({"name": "n8n", "version": ""}).chart_version is None


class TestParseListReleaseInfo:
    """``helm list`` 輸出（``chart`` 欄是 ``"<name>-<version>"``）。"""

    def _parse(self, chart):
        return HelmService()._parse_list_release_info({
            "name": "svc-abc",
            "namespace": "paas-ws-test",
            "status": "deployed",
            "revision": "2",
            "updated": "x",
            "chart": chart,
            "app_version": "1.0.0",
        })

    def test_splits_name_and_version(self):
        info = self._parse("postgresql-18.2.3")
        assert (info.chart, info.chart_version) == ("postgresql", "18.2.3")

    def test_chart_name_containing_dashes_still_splits_on_the_last_one(self):
        """``mcp-server-0.1.5`` 是本 repo 實際在用的形狀，不是假想案例。"""
        info = self._parse("mcp-server-0.1.5")
        assert (info.chart, info.chart_version) == ("mcp-server", "0.1.5")

    def test_no_dash_keeps_name_and_reports_unknown_version(self):
        info = self._parse("postgresql")
        assert (info.chart, info.chart_version) == ("postgresql", None)

    def test_empty_chart_field_is_not_a_crash(self):
        info = self._parse("")
        assert (info.chart, info.chart_version) == ("", None)

    def test_name_and_version_come_from_the_same_single_split(self):
        """兩半不得由兩次獨立的 split 產生，否則邊界可能對不起來。

        以 chart 名稱本身含 ``-`` 的樣本驗證：拼回去必須完全還原原字串。
        """
        raw = "woow-agent-runtime-0.4.10"
        info = self._parse(raw)
        assert f"{info.chart}-{info.chart_version}" == raw


def _reset_request(**overrides):
    kwargs = dict(
        values={"config": {"nonSensitive": {}}},
        sensitive_values={"config": {"sensitive": {"admin_password": "new-pwd-xyz"}}},
        chart="oci://example/mcp-server",
        version="0.1.5",
    )
    kwargs.update(overrides)
    return ReleaseResetConfigRequest(**kwargs)


class TestResetConfigReportsChartVersion:
    """🔴 兩條 reset 分支都要回報——只補一條 = helm_secret 家族帳本仍然瞎。"""

    @patch("src.api.releases.k8s_service.get_deployment_name", return_value="svc-abc-mcp-server")
    @patch("src.api.releases.k8s_service.exec_in_pod")
    @patch("src.api.releases.k8s_service.get_pod_name", return_value="mcp-abc")
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_config_json_branch_reports_chart_version(
        self, mock_get, mock_upgrade, *_mocks,
    ):
        mock_get.return_value = MagicMock(revision=2)
        mock_upgrade.return_value = _release_info(revision=3, chart_version="0.1.6")

        result = reset_release_config("paas-ws-test", "svc-abc", _reset_request())

        # 前提斷言：這條真的是 config_json 分支且真的跑完了 upgrade——少了它，
        # 下一行在「分支根本沒被走到」時也可能是 KeyError 以外的形狀。
        assert result["admin_password_carrier"] == "config_json"
        assert result["helm_revision"] == 3
        assert result["chart_version"] == "0.1.6"

    @patch("src.api.releases.k8s_service.get_deployment_names",
           return_value=["svc-abc-daemon", "svc-abc-console"])
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_helm_secret_branch_reports_chart_version(
        self, mock_get, mock_upgrade, *_mocks,
    ):
        mock_get.return_value = MagicMock(revision=2)
        mock_upgrade.return_value = _release_info(revision=3, chart_version="0.1.1")

        result = reset_release_config(
            "paas-ws-test", "svc-abc",
            _reset_request(admin_password_carrier="helm_secret",
                           chart="oci://example/hermes-agent", version="0.1.0"),
        )

        assert result["admin_password_carrier"] == "helm_secret"
        assert result["helm_revision"] == 3
        # ⚠ 回報的是 helm **實際解析**出來的版本（0.1.1），不是 request 帶進去的
        # pin（0.1.0）——這正是本單的重點：空 pin 時 helm 會挑 latest。
        assert result["chart_version"] == "0.1.1"

    @patch("src.api.releases.k8s_service.get_deployment_names", return_value=["svc-abc-daemon"])
    @patch("src.api.releases.k8s_service.rollout_restart_deployment")
    @patch("src.api.releases.helm_service.upgrade")
    @patch("src.api.releases.helm_service.get")
    def test_unknown_version_is_reported_as_none_not_omitted(
        self, mock_get, mock_upgrade, *_mocks,
    ):
        """鍵必須在、值為 None——平台用 ``.get(...) or 原值`` 兩種都吃，但鍵
        恆在讓「舊 operator」與「新 operator 但版本不明」在 log 上可區分。"""
        mock_get.return_value = MagicMock(revision=2)
        mock_upgrade.return_value = _release_info(chart_version=None)

        result = reset_release_config(
            "paas-ws-test", "svc-abc",
            _reset_request(admin_password_carrier="helm_secret"),
        )

        assert "chart_version" in result
        assert result["chart_version"] is None
