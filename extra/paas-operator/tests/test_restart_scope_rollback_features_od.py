"""AI-OD-004-2／AI-OD-006-1（商用驗收 open-design 244）。

- AI-OD-004-2：改金鑰後平台對 release 下**所有** workload 下 rollout restart ⇒ 不吃金鑰的
  console Deployment 也被重建、進行中的 console 終端機斷線。restart 端點新增選填的
  ``component``（``app.kubernetes.io/component``），平台可以只重啟吃金鑰 env 的 daemon。
- AI-OD-006-1：回復到 MCP 啟用前的 revision，``mcp.enabled`` 一起回到舊值，內建 MCP
  容器消失。回復請求新增 ``preserve_when_equal``：範本宣告的平台功能開關，在守衛路徑
  （例如 ``image``）目標與目前相同時維持目前值；守衛不同時開關跟著目標 revision 回復
  （開關與 image 是一組，目標 image 可能沒有新功能的執行檔）。
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.releases import overlay_preserved_when_equal
from src.main import app
from src.models.schemas import (
    ReleaseInfo,
    ReleaseRevision,
    ReleaseRollbackRequest,
    ReleaseStatus,
)

IMAGE = {"repository": "jcr/open-design", "tag": "20261015.0617@sha256:c683"}
OLD_IMAGE = {"repository": "jcr/open-design", "tag": "20261014.1921@sha256:3ae1"}

CURRENT = {
    "image": IMAGE,
    "mcp": {"enabled": True},
    "omnigentHost": {"enabled": False},
    "config": {"sensitive": {"admin_password": "CURRENT-PW"}},
}
TARGET_SAME_IMAGE = {
    "image": IMAGE,
    "omnigentHost": {"enabled": False},
    "config": {"sensitive": {"admin_password": "REVOKED-PW"}},
}
TARGET_OLD_IMAGE = {
    "image": OLD_IMAGE,
    "config": {"sensitive": {"admin_password": "REVOKED-PW"}},
}

FEATURES = {"mcp.enabled": ["image"], "omnigentHost.enabled": ["image"]}


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


# ── AI-OD-004-2：restart 只重啟指定 component ─────────────────────────────────


RESTART_URL = "/api/releases/paas-ws-x/svc-x/restart"


class TestRestartComponentScope:
    @patch("src.api.releases.k8s_service")
    def test_component_narrows_selector_to_daemon(self, mock_k8s, client):
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-x-open-design"}]

        resp = client.post(RESTART_URL, params={"component": "daemon"})

        assert resp.status_code == 200, resp.text
        expected = "app.kubernetes.io/instance=svc-x,app.kubernetes.io/component=daemon"
        assert mock_k8s.get_deployments.call_args.kwargs["label_selector"] == expected
        assert mock_k8s.get_statefulsets.call_args.kwargs["label_selector"] == expected
        mock_k8s.rollout_restart_deployment.assert_not_called()
        mock_k8s.rollout_restart_statefulset.assert_called_once_with(
            "paas-ws-x", "svc-x-open-design")
        body = resp.json()
        assert body["component"] == "daemon"
        assert body["deployments"] == []
        assert body["statefulsets"] == ["svc-x-open-design"]

    @patch("src.api.releases.k8s_service")
    def test_without_component_restarts_everything(self, mock_k8s, client):
        """租戶「重新啟動」（#1432）不帶 component：行為不變。"""
        mock_k8s.get_deployments.return_value = [{"name": "svc-x-console"}]
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-x-open-design"}]
        resp = client.post(RESTART_URL)
        assert resp.status_code == 200, resp.text
        assert mock_k8s.get_deployments.call_args.kwargs["label_selector"] == (
            "app.kubernetes.io/instance=svc-x")
        assert resp.json()["component"] is None
        mock_k8s.rollout_restart_deployment.assert_called_once()
        mock_k8s.rollout_restart_statefulset.assert_called_once()

    @patch("src.api.releases.k8s_service")
    def test_component_matching_nothing_is_404(self, mock_k8s, client):
        """舊 chart 沒有該 component label：404，平台據此退回全部重啟。"""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        resp = client.post(RESTART_URL, params={"component": "daemon"})
        assert resp.status_code == 404
        mock_k8s.rollout_restart_deployment.assert_not_called()
        mock_k8s.rollout_restart_statefulset.assert_not_called()

    @pytest.mark.parametrize("bad", ["Daemon", "a,b", "a=b", "-x", "x" * 64, "a b"])
    @patch("src.api.releases.k8s_service")
    def test_rejects_malformed_component(self, mock_k8s, bad, client):
        """component 直接拼進 label selector：只收 DNS-1123 label，擋 selector 注入。"""
        resp = client.post(RESTART_URL, params={"component": bad})
        assert resp.status_code == 422
        mock_k8s.get_deployments.assert_not_called()


# ── AI-OD-006-1：回復時平台功能開關跟著 image 走 ─────────────────────────────


class TestOverlayWhenEqual:
    def test_same_image_keeps_current_switches(self):
        merged, kept, reverted = overlay_preserved_when_equal(
            TARGET_SAME_IMAGE, CURRENT, FEATURES)
        assert merged["mcp"]["enabled"] is True
        assert merged["omnigentHost"]["enabled"] is False
        assert kept == ["mcp.enabled", "omnigentHost.enabled"]
        assert reverted == []
        # 其餘照目標 revision
        assert merged["config"]["sensitive"]["admin_password"] == "REVOKED-PW"

    def test_different_image_leaves_target_switches(self):
        """目標 image 可能沒有 MCP runtime（/opt/woow）：硬留 mcp.enabled=true 會 CrashLoop。"""
        merged, kept, reverted = overlay_preserved_when_equal(
            TARGET_OLD_IMAGE, CURRENT, FEATURES)
        assert "mcp" not in merged
        assert merged["image"] == OLD_IMAGE
        assert kept == []
        assert reverted == ["mcp.enabled", "omnigentHost.enabled"]

    def test_absent_in_current_is_removed(self):
        current = {"image": IMAGE}
        target = {"image": IMAGE, "mcp": {"enabled": True}}
        merged, kept, _reverted = overlay_preserved_when_equal(
            target, current, {"mcp.enabled": ["image"]})
        assert merged["mcp"] == {}
        assert kept == ["mcp.enabled"]

    def test_no_guard_always_keeps(self):
        merged, kept, _r = overlay_preserved_when_equal(
            TARGET_OLD_IMAGE, CURRENT, {"omnigentHost.enabled": []})
        assert merged["omnigentHost"]["enabled"] is False
        assert kept == ["omnigentHost.enabled"]

    def test_inputs_not_mutated(self):
        target = {"image": IMAGE}
        overlay_preserved_when_equal(target, CURRENT, FEATURES)
        assert target == {"image": IMAGE}


class TestSchemaWhenEqual:
    def test_default_empty(self):
        assert ReleaseRollbackRequest(revision=3).preserve_when_equal == {}

    def test_requires_chart(self):
        with pytest.raises(ValidationError):
            ReleaseRollbackRequest(revision=3, preserve_when_equal={"mcp.enabled": ["image"]})

    @pytest.mark.parametrize("bad", [
        {"mcp..enabled": ["image"]},
        {"mcp.enabled": ["im age"]},
        {"a.*": []},
    ])
    def test_rejects_malformed_paths(self, bad):
        with pytest.raises(ValidationError):
            ReleaseRollbackRequest(chart="oci://x/c", preserve_when_equal=bad)

    def test_rejects_too_many(self):
        with pytest.raises(ValidationError):
            ReleaseRollbackRequest(
                chart="oci://x/c",
                preserve_when_equal={f"f{i}.enabled": ["image"] for i in range(17)})


def _history(*revisions):
    return [
        ReleaseRevision(
            revision=r, updated="2026-10-16T00:00:00Z",
            status=ReleaseStatus.DEPLOYED if r == max(revisions) else ReleaseStatus.SUPERSEDED,
            chart="open-design-0.5.0", app_version="0.24.1", description="Upgrade complete",
        )
        for r in revisions
    ]


def _mock_helm(mock_helm, target):
    mock_helm.history.return_value = _history(6, 12, 13)
    mock_helm.get_revision_chart_version.return_value = "0.5.0"
    mock_helm.get_values.side_effect = lambda ns, n, revision=None: (
        target if revision == 6 else CURRENT)
    mock_helm.upgrade.side_effect = lambda **kw: (
        "" if kw.get("dry_run") else ReleaseInfo(
            name="svc-x", namespace="paas-ws-x", revision=14,
            status=ReleaseStatus.DEPLOYED, chart="open-design", chart_version="0.5.0",
            app_version="0.24.1", updated="",
        )
    )


class TestRollbackEndpointWhenEqual:
    URL = "/api/releases/paas-ws-x/svc-x/rollback"

    def _post(self, client, **extra):
        body = {"revision": 6, "chart": "oci://registry/charts/open-design",
                "preserve_value_paths": ["config.sensitive"]}
        body.update(extra)
        return client.post(self.URL, json=body)

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_same_image_keeps_mcp(self, mock_helm, _live, _pull, client):
        _mock_helm(mock_helm, TARGET_SAME_IMAGE)
        resp = self._post(client, preserve_when_equal=FEATURES)
        assert resp.status_code == 200, resp.text
        real = [c for c in mock_helm.upgrade.call_args_list if not c.kwargs.get("dry_run")]
        values = real[0].kwargs["values"]
        assert values["mcp"]["enabled"] is True
        assert values["config"]["sensitive"]["admin_password"] == "CURRENT-PW"
        body = resp.json()
        assert body["feature_paths_preserved"] == ["mcp.enabled", "omnigentHost.enabled"]
        assert body["feature_paths_reverted"] == []
        assert "CURRENT-PW" not in resp.text and "REVOKED-PW" not in resp.text

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_old_image_reverts_switches_with_it(self, mock_helm, _live, _pull, client):
        _mock_helm(mock_helm, TARGET_OLD_IMAGE)
        resp = self._post(client, preserve_when_equal=FEATURES)
        assert resp.status_code == 200, resp.text
        real = [c for c in mock_helm.upgrade.call_args_list if not c.kwargs.get("dry_run")]
        values = real[0].kwargs["values"]
        assert "mcp" not in values
        assert values["image"] == OLD_IMAGE
        assert resp.json()["feature_paths_reverted"] == ["mcp.enabled", "omnigentHost.enabled"]

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_features_alone_select_preserving_mode(self, mock_helm, _live, _pull, client):
        """沒有平台憑證、只有功能開關的範本也要走保留模式（純 helm rollback 做不到）。"""
        _mock_helm(mock_helm, TARGET_SAME_IMAGE)
        resp = client.post(self.URL, json={
            "revision": 6, "chart": "oci://registry/charts/open-design",
            "preserve_when_equal": {"mcp.enabled": ["image"]},
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["mode"] == "upgrade_preserving"
        mock_helm.rollback.assert_not_called()

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_without_features_response_shape_unchanged(self, mock_helm, _live, _pull, client):
        _mock_helm(mock_helm, TARGET_SAME_IMAGE)
        resp = self._post(client)
        assert resp.status_code == 200, resp.text
        real = [c for c in mock_helm.upgrade.call_args_list if not c.kwargs.get("dry_run")]
        assert "mcp" not in real[0].kwargs["values"]   # 舊行為：開關照目標 revision
        assert resp.json()["feature_paths_preserved"] == []
