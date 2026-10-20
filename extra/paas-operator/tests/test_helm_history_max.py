"""helm 歷史上限明確化（商用驗收 INFRA-FIX-018-1／AI-HER-003-1）。

修正前 operator 的 ``helm upgrade``／``helm rollback`` 都沒帶 ``--history-max``，吃 helm 的
預設 10：連續幾次失敗的 upgrade（每次留 failed revision）、設定修改、回復、值不變的
Redeploy 加起來超過 10 筆，就把最後一個可回復的好版本從歷史裡清掉（Headscale 238 的
rev 7、Hermes 247 的 rev 1 都是實例）。

這裡釘住三件事：
* upgrade 與 rollback 都帶 ``--history-max <HELM_HISTORY_MAX>``（rollback 也會寫一筆新
  revision 並照自己的上限修剪；只改 upgrade 的話，下一次回復又會修回 10 筆）；
* install 不帶（helm install 沒有這個旗標，第一筆 revision 也沒有東西可以修剪）；
* revisions API 回報目前的上限，平台的回復對話框據此寫「最多保留最近 N 版」。
"""
import json
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.config import Settings
from src.main import app
from src.models.schemas import ReleaseRevision, ReleaseStatus
from src.services.helm import HelmService


def _release_stdout(revision=2):
    return json.dumps({
        "name": "r", "namespace": "paas-ws-test",
        "info": {"status": "deployed", "revision": revision, "last_deployed": ""},
        "chart": {"metadata": {"name": "n8n", "version": "1.0.0", "appVersion": "1.0.0"}},
    })


def _history_max(cmd):
    assert cmd.count("--history-max") == 1, cmd
    return cmd[cmd.index("--history-max") + 1]


def _helm_calls(mock_run, verb):
    """helm 本身那一次呼叫（排除 _live_snapshot 等 kubectl 查詢）。"""
    calls = [c[0][0] for c in mock_run.call_args_list]
    return [c for c in calls if len(c) > 1 and c[1] == verb]


class TestHistoryMaxFlag:
    @pytest.fixture
    def helm_service(self):
        return HelmService()

    @patch("subprocess.run")
    def test_upgrade_pins_history_max_to_setting_default(self, mock_run, helm_service):
        mock_run.return_value = Mock(returncode=0, stdout=_release_stdout(), stderr="")

        helm_service.upgrade(namespace="paas-ws-test", name="r", chart="n8n")

        (cmd,) = _helm_calls(mock_run, "upgrade")
        assert _history_max(cmd) == "20"

    @patch("subprocess.run")
    def test_upgrade_history_max_follows_setting(self, mock_run, helm_service, monkeypatch):
        monkeypatch.setattr("src.services.helm.settings.helm_history_max", 35)
        mock_run.return_value = Mock(returncode=0, stdout=_release_stdout(), stderr="")

        helm_service.upgrade(namespace="paas-ws-test", name="r", chart="n8n")

        (cmd,) = _helm_calls(mock_run, "upgrade")
        assert _history_max(cmd) == "35"

    @patch("subprocess.run")
    def test_dry_run_upgrade_carries_the_same_flag(self, mock_run, helm_service):
        """#1407 preflight 的 dry-run 必須是「同一條指令＋--dry-run」。"""
        mock_run.return_value = Mock(
            returncode=0, stdout=json.dumps({"manifest": "---\n"}), stderr="")

        helm_service.upgrade(namespace="paas-ws-test", name="r", chart="n8n", dry_run=True)

        (cmd,) = _helm_calls(mock_run, "upgrade")
        assert _history_max(cmd) == "20"
        assert "--dry-run" in cmd

    @patch("subprocess.run")
    def test_rollback_pins_history_max(self, mock_run, helm_service, monkeypatch):
        monkeypatch.setattr("src.services.helm.settings.helm_history_max", 25)
        mock_run.return_value = Mock(returncode=0, stdout="Rollback was a success", stderr="")

        helm_service.rollback(namespace="paas-ws-test", name="r", revision=3)

        (cmd,) = _helm_calls(mock_run, "rollback")
        assert _history_max(cmd) == "25"
        # revision 仍是 rollback 的位置參數（旗標加在後面不影響）。
        assert cmd[:3] == ["helm", "rollback", "r"] or cmd[1:3] == ["rollback", "r"]
        assert "3" in cmd

    @patch("subprocess.run")
    def test_install_does_not_pass_history_max(self, mock_run, helm_service):
        """helm install 沒有 --history-max（帶了會 unknown flag 直接失敗）。"""
        mock_run.return_value = Mock(returncode=0, stdout=_release_stdout(1), stderr="")

        helm_service.install(namespace="paas-ws-test", name="r", chart="n8n")

        (cmd,) = _helm_calls(mock_run, "install")
        assert "--history-max" not in cmd


class TestHistoryMaxSetting:
    def test_default_is_20(self, monkeypatch):
        monkeypatch.delenv("HELM_HISTORY_MAX", raising=False)
        assert Settings().helm_history_max == 20

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HELM_HISTORY_MAX", "40")
        assert Settings().helm_history_max == 40

    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_zero_or_negative_rejected(self, monkeypatch, raw):
        """helm 的 0＝不限：release Secret 無限累積（每筆都含當時的明文 values／憑證）。"""
        monkeypatch.setenv("HELM_HISTORY_MAX", raw)
        with pytest.raises(ValidationError):
            Settings()


class TestRevisionsReportHistoryMax:
    @pytest.fixture
    def client(self):
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "test-api-key"
            mock_settings.namespace_prefix = "paas-ws-"
            yield TestClient(app, headers={"X-API-Key": "test-api-key"})

    @patch("src.api.releases.helm_service")
    def test_revisions_response_carries_history_max(self, mock_helm, client, monkeypatch):
        monkeypatch.setattr("src.api.releases.settings.helm_history_max", 30)
        mock_helm.history.return_value = [
            ReleaseRevision(
                revision=1, updated="2024-01-01T00:00:00Z", status=ReleaseStatus.DEPLOYED,
                chart="nginx-1.0.0", app_version="1.0.0", description="Install complete",
            ),
        ]

        response = client.get("/api/releases/paas-ws-test/r/revisions")

        assert response.status_code == 200, response.text
        assert response.json()["history_max"] == 30
