"""#1526：「回復版本」不得讓 chart 版本低於目前部署中的版本。

稽核重現（sbx-b #9）：pi-agent 裝 0.1.4 → 升 0.1.6 → 平台以 .210 的參數回復到 rev1，
operator 照目標 revision 的 chart 版本 ``helm upgrade --version 0.1.4``：egress 又變回
``- {}``、容器沒有 ephemeral-storage 上限，k8s API／節點 6443／Longhorn UI 全部可達。
正式站 5 台 0.1.6 的 pi-agent 歷史裡都還有 0.1.4 的 revision。

本檔守 operator 這一半（兩種回復模式都要擋）：

1. 版本比較（SemVer 2：prerelease < 正式版、逐段比較、build metadata 不參與）。
2. 保留憑證的回復（helm upgrade）與純 helm rollback：目標 chart 比部署中的舊 ⇒ 409，
   在讀任何 values（含明文憑證）之前就擋下。
3. 同版、較新的版本照常可回復；最新 revision 是失敗的升級時，以部署中那一版當下限。
4. 版本讀不到或讀不懂 ⇒ 拒絕（fail-closed）。
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.releases import (
    ROLLBACK_CHART_DOWNGRADE_ERROR_CODE,
    chart_version_key,
    chart_version_not_lower,
)
from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseRevision, ReleaseStatus

URL = "/api/releases/paas-ws-x/svc-x/rollback"
PRESERVE = {
    "chart": "oci://registry/charts/pi-agent",
    "preserve_value_paths": ["config.sensitive", "auth.openrouterApiKey"],
}


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _rev(revision, version, status):
    return ReleaseRevision(
        revision=revision, updated="2026-10-07T00:00:00Z", status=status,
        chart=f"pi-agent-{version}", app_version="1", description="",
    )


def _helm(mock_helm, revisions):
    """``revisions``：[(revision, chart_version, status)]。"""
    versions = {r: v for r, v, _ in revisions}
    mock_helm.history.return_value = [_rev(r, v, s) for r, v, s in revisions]
    mock_helm.get_revision_chart_version.side_effect = (
        lambda ns, name, revision: versions.get(revision))
    mock_helm.get_values.return_value = {
        "config": {"sensitive": {"admin_password": "PLAINTEXT-PW"}},
        "auth": {"openrouterApiKey": "PLAINTEXT-KEY"},
    }
    mock_helm.upgrade.side_effect = lambda **kw: (
        "" if kw.get("dry_run") else ReleaseInfo(
            name="svc-x", namespace="paas-ws-x", revision=max(versions) + 1,
            status=ReleaseStatus.DEPLOYED, chart="pi-agent",
            chart_version=kw.get("version") or "", app_version="1", updated="",
        )
    )
    mock_helm.rollback.return_value = {"message": "Rollback was a success!"}
    return mock_helm


# 稽核的形狀：rev1 0.1.4、rev2–3 0.1.6（rev3 部署中）。
AUDIT_HISTORY = [
    (1, "0.1.4", ReleaseStatus.SUPERSEDED),
    (2, "0.1.6", ReleaseStatus.SUPERSEDED),
    (3, "0.1.6", ReleaseStatus.DEPLOYED),
]


class TestVersionOrder:
    @pytest.mark.parametrize("lower, higher", [
        ("0.1.4", "0.1.6"),
        ("0.2.9", "0.2.10"),          # 數字比較，不是字串比較
        ("0.9.0", "1.0.0"),
        ("1.0.0-rc.1", "1.0.0"),      # prerelease 低於正式版
        ("1.0.0-alpha", "1.0.0-alpha.1"),
        ("1.0.0-alpha.1", "1.0.0-alpha.beta"),  # 數字識別字低於英數識別字
        ("1.0.0-beta.2", "1.0.0-beta.11"),
        ("v0.1.4", "0.1.6"),
    ])
    def test_order(self, lower, higher):
        assert chart_version_key(lower) < chart_version_key(higher)
        assert chart_version_not_lower(higher, lower)
        assert not chart_version_not_lower(lower, higher)

    def test_build_metadata_is_ignored(self):
        assert chart_version_key("1.2.3+build.5") == chart_version_key("1.2.3")

    @pytest.mark.parametrize("bad", [None, "", "latest", "1.2", "1.2.3.4", "x-1.2.3"])
    def test_unparseable_is_none(self, bad):
        assert chart_version_key(bad) is None

    def test_unknown_or_unparseable_is_not_allowed(self):
        assert not chart_version_not_lower(None, "0.1.6")
        assert not chart_version_not_lower("0.1.6", None)
        assert not chart_version_not_lower("weird", "0.1.6")

    def test_identical_strings_are_allowed_even_if_not_semver(self):
        assert chart_version_not_lower("2024.06", "2024.06")


class TestPreservingRollbackFloor:
    @patch("src.api.releases.helm_service")
    def test_older_chart_is_refused_before_reading_values(self, mock_helm, client):
        _helm(mock_helm, AUDIT_HISTORY)

        resp = client.post(URL, json={"revision": 1, **PRESERVE})

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == ROLLBACK_CHART_DOWNGRADE_ERROR_CODE
        assert detail["target_chart_version"] == "0.1.4"
        assert detail["current_chart_version"] == "0.1.6"
        assert detail["target_revision"] == 1
        mock_helm.upgrade.assert_not_called()
        mock_helm.rollback.assert_not_called()
        # 擋在讀 values（含明文憑證）之前
        mock_helm.get_values.assert_not_called()
        assert "PLAINTEXT" not in resp.text

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_same_chart_revision_is_allowed(self, mock_helm, _live, _pull, client):
        _helm(mock_helm, AUDIT_HISTORY)

        resp = client.post(URL, json={"revision": 2, **PRESERVE})

        assert resp.status_code == 200, resp.text
        assert resp.json()["chart_version"] == "0.1.6"
        real = [c for c in mock_helm.upgrade.call_args_list if not c.kwargs.get("dry_run")]
        assert len(real) == 1 and real[0].kwargs["version"] == "0.1.6"

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_newer_chart_revision_is_allowed(self, mock_helm, _live, _pull, client):
        """已經被（修正前的回復）降過版的服務，可以回到較新的 revision 自救。"""
        _helm(mock_helm, [
            (1, "0.1.4", ReleaseStatus.SUPERSEDED),
            (2, "0.1.6", ReleaseStatus.SUPERSEDED),
            (3, "0.1.4", ReleaseStatus.DEPLOYED),
        ])

        resp = client.post(URL, json={"revision": 2, **PRESERVE})

        assert resp.status_code == 200, resp.text
        assert resp.json()["chart_version"] == "0.1.6"

    @patch("src.api.releases._ensure_image_pull_secrets", side_effect=lambda ns, v: dict(v))
    @patch("src.api.releases._live_objects", return_value=None)
    @patch("src.api.releases.helm_service")
    def test_failed_upgrade_does_not_raise_the_floor(self, mock_helm, _live, _pull, client):
        """最新 revision 是失敗的升級（0.1.7 failed、0.1.6 仍 deployed）：退回 0.1.6 要放行。"""
        _helm(mock_helm, [
            (1, "0.1.6", ReleaseStatus.DEPLOYED),
            (2, "0.1.7", ReleaseStatus.FAILED),
        ])

        resp = client.post(URL, json={"revision": 1, **PRESERVE})

        assert resp.status_code == 200, resp.text

    @patch("src.api.releases.helm_service")
    def test_revision_omitted_previous_is_older(self, mock_helm, client):
        _helm(mock_helm, [
            (1, "0.1.4", ReleaseStatus.SUPERSEDED),
            (2, "0.1.6", ReleaseStatus.DEPLOYED),
        ])

        resp = client.post(URL, json=PRESERVE)

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["target_revision"] == 1

    @patch("src.api.releases.helm_service")
    def test_unknown_current_version_fails_closed(self, mock_helm, client):
        _helm(mock_helm, AUDIT_HISTORY)
        mock_helm.get_revision_chart_version.side_effect = (
            lambda ns, name, revision: "0.1.6" if revision == 2 else None)

        resp = client.post(URL, json={"revision": 2, **PRESERVE})

        assert resp.status_code == 409, resp.text
        mock_helm.upgrade.assert_not_called()


class TestPlainRollbackFloor:
    """沒有平台憑證的模板（Odoo、PostgreSQL、HA…）走純 helm rollback，同一道下限。"""

    @patch("src.api.releases.helm_service")
    def test_older_chart_is_refused(self, mock_helm, client):
        _helm(mock_helm, AUDIT_HISTORY)

        resp = client.post(URL, json={"revision": 1})

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error_code"] == ROLLBACK_CHART_DOWNGRADE_ERROR_CODE
        mock_helm.rollback.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_same_chart_is_allowed_and_reports_the_version(self, mock_helm, client):
        """回報回復後的 chart 版本，平台帳本才能跟叢集一致。"""
        _helm(mock_helm, AUDIT_HISTORY)

        resp = client.post(URL, json={"revision": 2})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["chart_version"] == "0.1.6"
        assert body["mode"] == "rollback"
        assert body["target_revision"] == 2
        mock_helm.rollback.assert_called_once_with("paas-ws-x", "svc-x", 2)

    @patch("src.api.releases.helm_service")
    def test_revision_omitted_resolves_previous_explicitly(self, mock_helm, client):
        """不帶 revision＝上一版；送 helm 的是解析過、檢查過的那個 revision。"""
        _helm(mock_helm, [
            (1, "0.1.6", ReleaseStatus.SUPERSEDED),
            (2, "0.1.6", ReleaseStatus.DEPLOYED),
        ])

        resp = client.post(URL, json={})

        assert resp.status_code == 200, resp.text
        mock_helm.rollback.assert_called_once_with("paas-ws-x", "svc-x", 1)

    @patch("src.api.releases.helm_service")
    def test_explicit_zero_or_negative_revision_is_400(self, mock_helm, client):
        """AUDITFIX rollback-api-input-validation：明確送 0 不再被解讀成「上一版」。

        helm 把 0 當上一版；原本 ``requested or (current - 1)`` 也這樣解，誤送 0 會真的回復並
        重啟（INFRA-EMQX-013-1）。平台已改成只送 ≥ 1 的明確號碼，這裡是 defense-in-depth。
        """
        _helm(mock_helm, AUDIT_HISTORY)

        for revision in (0, -1):
            resp = client.post(URL, json={"revision": revision})
            assert resp.status_code == 400, resp.text

        mock_helm.rollback.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_unknown_revision_is_400(self, mock_helm, client):
        _helm(mock_helm, AUDIT_HISTORY)

        resp = client.post(URL, json={"revision": 9})

        assert resp.status_code == 400
        mock_helm.rollback.assert_not_called()

    def test_foreign_namespace_403(self, client):
        resp = client.post("/api/releases/kube-system/x/rollback", json={"revision": 1})
        assert resp.status_code == 403
