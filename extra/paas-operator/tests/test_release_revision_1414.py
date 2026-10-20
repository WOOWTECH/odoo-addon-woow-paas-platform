"""#1414／#1456：install／upgrade 回報的 revision 必須是 Helm 真正的版次。

`helm install|upgrade --output json` 印的是 Helm 的 release 物件：版次在**頂層**
`version`，`info` 裡沒有 `revision`（`info` 只有 first_deployed／last_deployed／
deleted／description／status／notes）。`_parse_release_info` 原本讀
`info.revision` ⇒ 每次 install／upgrade 都回報 revision 0，平台帳本
`helm_revision` 只能等週期 poll／每日對帳才追上，回復對話框把舊版標成「目前」
（AI-OD-004-3「更新中短暫回 0」、INFRA-TS-003-1 等 7 個缺陷檔）。

舊測試 fixture 全是假的 `info.revision` 形狀，所以這個缺陷一路全綠。本檔的
fixture 取自 `helm install --dry-run=client --output json` 的實際輸出（只刪掉
manifest／hooks／notes 等與本題無關、體積大的欄位）。
"""
import copy
import json
from unittest.mock import Mock, patch

import pytest

from src.services.helm import HelmService

# helm v3 `install --output json` 實際輸出的形狀（2026-10-17 以 helm 3.19.5 擷取；
# release 物件的 JSON 欄位自 helm 3.0 起未變：`version` 在頂層、`info` 無 revision）。
REAL_HELM_RELEASE_JSON = {
    "name": "svc-abc",
    "info": {
        "first_deployed": "2026-10-17T15:02:00.776868502+08:00",
        "last_deployed": "2026-10-17T15:02:00.776868502+08:00",
        "deleted": "",
        "description": "Upgrade complete",
        "status": "deployed",
    },
    "chart": {
        "metadata": {
            "name": "tailscale-node",
            "version": "0.1.9",
            "apiVersion": "v2",
            "appVersion": "1.88.0",
            "type": "application",
        }
    },
    "config": {},
    "version": 7,
    "namespace": "paas-ws-test",
}


def _release(**overrides):
    data = copy.deepcopy(REAL_HELM_RELEASE_JSON)
    data.update(overrides)
    return data


class TestParseReleaseInfoRevision:
    def test_real_helm_shape_has_no_info_revision(self):
        """守住 fixture 本身：真實 helm 輸出的 info 沒有 revision。"""
        assert "revision" not in REAL_HELM_RELEASE_JSON["info"]
        assert REAL_HELM_RELEASE_JSON["version"] == 7

    def test_reads_top_level_version(self):
        info = HelmService()._parse_release_info(_release())
        assert info.revision == 7

    def test_first_install_is_revision_one_not_zero(self):
        info = HelmService()._parse_release_info(_release(version=1))
        assert info.revision == 1

    def test_legacy_info_revision_is_kept_as_fallback(self):
        """沒有頂層 version 的舊形狀（既有測試替身）仍讀得到 info.revision。"""
        data = _release()
        data.pop("version")
        data["info"]["revision"] = 3
        assert HelmService()._parse_release_info(data).revision == 3

    def test_top_level_version_wins_over_info_revision(self):
        data = _release(version=9)
        data["info"]["revision"] = 2
        assert HelmService()._parse_release_info(data).revision == 9

    @pytest.mark.parametrize("bad", [None, "", "x", True, -1, 0])
    def test_unusable_version_falls_back_to_zero_unknown(self, bad):
        """讀不到合法版次＝0（「不知道」），由平台端拒寫，不猜。"""
        data = _release(version=bad)
        assert HelmService()._parse_release_info(data).revision == 0


class TestInstallUpgradeReportRevision:
    @patch("subprocess.run")
    def test_upgrade_reports_helm_revision(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0, stdout=json.dumps(_release(version=12)), stderr="")
        result = HelmService().upgrade(
            namespace="paas-ws-test", name="svc-abc", chart="tailscale-node")
        assert result.revision == 12

    @patch("subprocess.run")
    def test_install_reports_helm_revision(self, mock_run):
        mock_run.return_value = Mock(
            returncode=0, stdout=json.dumps(_release(version=1)), stderr="")
        result = HelmService().install(
            namespace="paas-ws-test", name="svc-abc", chart="tailscale-node")
        assert result.revision == 1
