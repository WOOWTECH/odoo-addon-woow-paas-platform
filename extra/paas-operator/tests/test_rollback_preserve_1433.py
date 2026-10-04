"""#1433：「回復版本」保留目前的憑證。

純 ``helm rollback`` 重放目標 revision 整份 stored values（含
``config.sensitive.admin_password``）⇒ 已撤銷的舊密碼復活、目前的密碼失效（R8 在
open-design、code-server 各自重現）。帶 ``preserve_value_paths`` 時改走
「目標 values ＋ 目前憑證 ＋ 目標 chart 版本」的 helm upgrade。
"""
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.releases import overlay_preserved_paths
from src.main import app
from src.models.schemas import (
    ReleaseInfo,
    ReleaseRevision,
    ReleaseRollbackRequest,
    ReleaseStatus,
)
from src.services.helm import HelmException, HelmService

CURRENT = {
    "config": {
        "nonSensitive": {"model": "new-model"},
        "sensitive": {"admin_password": "CURRENT-PW"},
    },
    "auth": {"username": "current-user"},
    "imagePullSecrets": [{"name": "paas-registry"}],
}
TARGET = {
    "config": {
        "nonSensitive": {"model": "old-model"},
        "sensitive": {"admin_password": "REVOKED-PW"},
    },
    "auth": {"username": "old-user"},
    "imagePullSecrets": [{"name": "paas-registry"}],
}


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _history(*revisions):
    return [
        ReleaseRevision(
            revision=r, updated="2026-10-04T00:00:00Z",
            status=ReleaseStatus.DEPLOYED if r == max(revisions) else ReleaseStatus.SUPERSEDED,
            chart=f"open-design-0.2.{r}", app_version="1", description="Upgrade complete",
        )
        for r in revisions
    ]


def _mock_helm(mock_helm):
    mock_helm.history.return_value = _history(10, 11, 12)
    mock_helm.get_revision_chart_version.return_value = "0.2.10"

    def _get_values(namespace, name, revision=None):
        return TARGET if revision == 10 else CURRENT

    mock_helm.get_values.side_effect = _get_values
    mock_helm.upgrade.side_effect = lambda **kw: (
        "" if kw.get("dry_run") else ReleaseInfo(
            name="svc-x", namespace="paas-ws-x", revision=13,
            status=ReleaseStatus.DEPLOYED, chart="open-design", chart_version="0.2.10",
            app_version="1", updated="",
        )
    )
    return mock_helm


class TestOverlay:
    def test_current_credentials_win(self):
        merged = overlay_preserved_paths(TARGET, CURRENT, ["config.sensitive", "auth.username"])
        assert merged["config"]["sensitive"] == {"admin_password": "CURRENT-PW"}
        assert merged["auth"]["username"] == "current-user"
        # 一般設定照目標 revision 回復
        assert merged["config"]["nonSensitive"]["model"] == "old-model"

    def test_absent_in_current_is_removed_from_target(self):
        """目前沒有的憑證不得因回復而出現——那正是「已撤銷的值復活」。"""
        current = {"config": {"nonSensitive": {}}}
        merged = overlay_preserved_paths(TARGET, current, ["config.sensitive"])
        assert "sensitive" not in merged["config"]

    def test_inputs_not_mutated(self):
        target = {"config": {"sensitive": {"admin_password": "old"}}}
        overlay_preserved_paths(target, CURRENT, ["config.sensitive"])
        assert target == {"config": {"sensitive": {"admin_password": "old"}}}

    def test_creates_missing_parents(self):
        merged = overlay_preserved_paths({}, CURRENT, ["auth.username"])
        assert merged == {"auth": {"username": "current-user"}}


class TestSchema:
    def test_default_is_plain_rollback(self):
        req = ReleaseRollbackRequest(revision=3)
        assert req.preserve_value_paths == []
        assert req.chart is None

    def test_preserve_requires_chart(self):
        with pytest.raises(ValidationError):
            ReleaseRollbackRequest(revision=3, preserve_value_paths=["config.sensitive"])

    @pytest.mark.parametrize("bad", ["", "config..sensitive", "a.*", "a[0]", "../x", "a b"])
    def test_rejects_malformed_paths(self, bad):
        with pytest.raises(ValidationError):
            ReleaseRollbackRequest(chart="oci://x/c", preserve_value_paths=[bad])


class TestEndpoint:
    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_preserving_rollback_upgrades_with_current_credentials(
        self, mock_helm, _live, _pull, client,
    ):
        _mock_helm(mock_helm)
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={
            "revision": 10,
            "chart": "oci://registry/charts/open-design",
            "preserve_value_paths": ["config.sensitive", "auth.username"],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "upgrade_preserving"
        assert body["target_revision"] == 10
        assert body["revision"] == 13
        assert body["chart_version"] == "0.2.10"
        # 絕不回傳明文
        assert "CURRENT-PW" not in resp.text and "REVOKED-PW" not in resp.text

        mock_helm.rollback.assert_not_called()
        real = [c for c in mock_helm.upgrade.call_args_list if not c.kwargs.get("dry_run")]
        assert len(real) == 1
        kw = real[0].kwargs
        assert kw["version"] == "0.2.10"
        assert kw["chart"] == "oci://registry/charts/open-design"
        assert kw["reset_values"] is True
        assert kw["values"]["config"]["sensitive"]["admin_password"] == "CURRENT-PW"
        assert kw["values"]["auth"]["username"] == "current-user"
        assert kw["values"]["config"]["nonSensitive"]["model"] == "old-model"
        assert "Rollback to 10" in kw["description"]
        # #1407 preflight 與真 upgrade 用同一組參數
        dry = [c for c in mock_helm.upgrade.call_args_list if c.kwargs.get("dry_run")]
        assert len(dry) == 1

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_revision_omitted_targets_previous(self, mock_helm, _live, _pull, client):
        _mock_helm(mock_helm)
        mock_helm.get_values.side_effect = lambda ns, n, revision=None: (
            TARGET if revision == 11 else CURRENT)
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={
            "chart": "oci://registry/charts/open-design",
            "preserve_value_paths": ["config.sensitive"],
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["target_revision"] == 11
        mock_helm.get_revision_chart_version.assert_called_with("paas-ws-x", "svc-x", 11)

    @patch("src.api.releases.helm_service")
    def test_unknown_revision_400(self, mock_helm, client):
        _mock_helm(mock_helm)
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={
            "revision": 4, "chart": "oci://r/c", "preserve_value_paths": ["config.sensitive"],
        })
        assert resp.status_code == 400
        mock_helm.upgrade.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_current_revision_400(self, mock_helm, client):
        _mock_helm(mock_helm)
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={
            "revision": 12, "chart": "oci://r/c", "preserve_value_paths": ["config.sensitive"],
        })
        assert resp.status_code == 400

    @patch("src.api.releases.helm_service")
    def test_unknown_chart_version_fails_closed(self, mock_helm, client):
        """讀不到目標 chart 版本：拒絕，不退回純 rollback（那會讓舊憑證復活）。"""
        _mock_helm(mock_helm)
        mock_helm.get_revision_chart_version.return_value = None
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={
            "revision": 10, "chart": "oci://r/c", "preserve_value_paths": ["config.sensitive"],
        })
        assert resp.status_code == 500
        mock_helm.upgrade.assert_not_called()
        mock_helm.rollback.assert_not_called()

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_helm_failure_does_not_fall_back(self, mock_helm, _live, _pull, client):
        _mock_helm(mock_helm)
        mock_helm.upgrade.side_effect = HelmException("boom", "helm upgrade", "Error: x")
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={
            "revision": 10, "chart": "oci://r/c", "preserve_value_paths": ["config.sensitive"],
        })
        assert resp.status_code == 500
        mock_helm.rollback.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_without_preserve_paths_is_plain_rollback(self, mock_helm, client):
        """舊平台（不帶新欄位）行為不變。"""
        mock_helm.rollback.return_value = {"message": "Rollback was a success!"}
        resp = client.post("/api/releases/paas-ws-x/svc-x/rollback", json={"revision": 10})
        assert resp.status_code == 200
        mock_helm.rollback.assert_called_once_with("paas-ws-x", "svc-x", 10)
        mock_helm.upgrade.assert_not_called()

    def test_foreign_namespace_403(self, client):
        resp = client.post("/api/releases/kube-system/x/rollback", json={
            "revision": 1, "chart": "oci://r/c", "preserve_value_paths": ["config.sensitive"],
        })
        assert resp.status_code == 403


class TestHelmServiceReaders:
    @patch("subprocess.run")
    def test_get_values_revision_argv(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout='{"a": 1}', stderr="")
        assert HelmService().get_values("paas-ws-x", "svc-x", revision=3) == {"a": 1}
        argv = mock_run.call_args.args[0]
        assert argv[1:4] == ["get", "values", "svc-x"]
        assert "--revision" in argv and "3" in argv
        assert "--all" not in argv

    @patch("subprocess.run")
    def test_get_values_null_is_empty(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="null\n", stderr="")
        assert HelmService().get_values("paas-ws-x", "svc-x") == {}

    @patch("subprocess.run")
    def test_get_revision_chart_version(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout="0.2.10-rc.1\n", stderr="")
        assert HelmService().get_revision_chart_version("paas-ws-x", "svc-x", 10) == "0.2.10-rc.1"
        argv = mock_run.call_args.args[0]
        assert argv[1:4] == ["get", "all", "svc-x"]
        assert "{{.Release.Chart.Metadata.Version}}" in argv

    @patch("subprocess.run")
    def test_upgrade_description_flag(self, mock_run):
        mock_run.return_value = Mock(returncode=0, stdout='{"info": {}, "chart": {}}', stderr="")
        HelmService().upgrade(
            namespace="paas-ws-x", name="svc-x", chart="/tmp/local-chart",
            description="Rollback to 3 (credentials preserved)",
        )
        argv = next(c.args[0] for c in mock_run.call_args_list if "upgrade" in c.args[0])
        i = argv.index("--description")
        assert argv[i + 1] == "Rollback to 3 (credentials preserved)"
