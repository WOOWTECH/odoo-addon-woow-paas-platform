"""#1348：開通前的工作區儲存配額檢查（PVC 數量／requests.storage）。

保留中的孤兒 PVC（#317，``paas.woowtech.io/orphaned-at``）仍計入 namespace 的
ResourceQuota；配額滿了之後 helm install 在 PVC admission 被拒，平台原本只拿到
「Helm installation failed」。這裡守：

* 純函式（quantity 解析、用量摘要、helm stderr 辨識）的判準；
* ``POST /api/releases``：配額已滿 ⇒ 409＋固定 ``error_code``、**不**碰叢集寫入；
  有餘裕或讀不到配額 ⇒ 照舊 install；helm 被 quota 擋 ⇒ 同一個 409；其他 helm
  失敗 ⇒ 維持原本的 500 訊息。
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import HelmException, KubectlException, KubernetesService
from src.services.storage_quota import (
    ORPHANED_AT_LABEL,
    STORAGE_QUOTA_ERROR_CODE,
    build_quota_error_detail,
    exhausted_resources,
    is_storage_quota_rejection,
    parse_quantity,
    summarize,
)

NS = "paas-ws-test1348"

# 2026-10-03 正式站 ws16 的實際拒絕訊息形狀（名稱換成測試值）
PVC_REJECTION = (
    'Error: INSTALLATION FAILED: persistentvolumeclaims "svc-x-open-design-data" is '
    "forbidden: exceeded quota: paas-ws-test1348-quota, requested: persistentvolumeclaims=1, "
    "used: persistentvolumeclaims=10, limited: persistentvolumeclaims=10"
)
STORAGE_REJECTION = (
    'Error: INSTALLATION FAILED: persistentvolumeclaims "svc-x-data" is forbidden: '
    "exceeded quota: paas-ws-test1348-quota, requested: requests.storage=10Gi, "
    "used: requests.storage=95Gi, limited: requests.storage=100Gi"
)
CPU_REJECTION = (
    'Error: INSTALLATION FAILED: pods "x" is forbidden: exceeded quota: q, '
    "requested: limits.cpu=2, used: limits.cpu=4, limited: limits.cpu=4"
)


def _quota(name, hard, used):
    return {"metadata": {"name": name}, "spec": {"hard": dict(hard)},
            "status": {"hard": dict(hard), "used": dict(used)}}


def _pvc(name, size, orphaned_at=None, grace_days=None, release=None):
    labels = {}
    if orphaned_at is not None:
        labels[ORPHANED_AT_LABEL] = str(orphaned_at)
    if grace_days is not None:
        labels["paas.woowtech.io/grace-period-days"] = str(grace_days)
    if release:
        labels["paas.woowtech.io/release-name"] = release
    return {"metadata": {"name": name, "labels": labels},
            "spec": {"resources": {"requests": {"storage": size}}}}


FULL_PVC_QUOTA = _quota(
    f"{NS}-quota",
    {"persistentvolumeclaims": "10", "requests.storage": "100Gi", "requests.cpu": "4"},
    {"persistentvolumeclaims": "10", "requests.storage": "95Gi", "requests.cpu": "1"},
)
ROOMY_QUOTA = _quota(
    f"{NS}-quota",
    {"persistentvolumeclaims": "10", "requests.storage": "100Gi"},
    {"persistentvolumeclaims": "3", "requests.storage": "30Gi"},
)


class TestParseQuantity:
    @pytest.mark.parametrize("raw,expected", [
        ("10", Decimal(10)),
        ("95Gi", Decimal(95 * 1024 ** 3)),
        ("1.5Gi", Decimal("1.5") * 1024 ** 3),
        ("100G", Decimal(100 * 10 ** 9)),
        ("500Mi", Decimal(500 * 1024 ** 2)),
        ("1e3", Decimal(1000)),
        ("250m", Decimal("0.25")),
        (7, Decimal(7)),
    ])
    def test_known_forms(self, raw, expected):
        assert parse_quantity(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "lots", "10Xi", "Gi", True])
    def test_unknown_forms_are_none_not_zero(self, raw):
        """看不懂＝不知道：不得變成 0（0 會被當成「已滿」或「沒用量」誤判）。"""
        assert parse_quantity(raw) is None


class TestSummarize:
    def test_counts_only_orphaned_pvcs(self):
        summary = summarize([FULL_PVC_QUOTA], [
            _pvc("a-data", "10Gi", orphaned_at=1_000, grace_days=90, release="svc-a"),
            _pvc("b-data", "5Gi", orphaned_at=2_000, grace_days=90, release="svc-b"),
            _pvc("b-pg", "1Gi", orphaned_at=2_000, grace_days=90, release="svc-b"),
            _pvc("live-data", "50Gi"),
        ])
        assert summary["orphaned_pvc_count"] == 3
        assert summary["orphaned_storage"] == "16Gi"
        assert summary["orphaned_releases"] == ["svc-a", "svc-b"]
        assert summary["next_release_epoch"] == 1_000 + 90 * 86400
        assert summary["quotas"]["persistentvolumeclaims"]["used"] == "10"
        assert summary["quotas"]["requests.storage"]["hard"] == "100Gi"

    def test_orphan_without_grace_label_has_no_release_time(self):
        summary = summarize([], [_pvc("a", "1Gi", orphaned_at=5)])
        assert summary["orphaned_pvc_count"] == 1
        assert summary["next_release_epoch"] is None

    def test_tightest_quota_wins(self):
        """多個 quota 宣告同一資源：admission 每個都要過，剩餘最少的那個決定擋不擋。"""
        loose = _quota("loose", {"persistentvolumeclaims": "50"}, {"persistentvolumeclaims": "10"})
        tight = _quota("tight", {"persistentvolumeclaims": "10"}, {"persistentvolumeclaims": "10"})
        summary = summarize([loose, tight], [])
        assert summary["quotas"]["persistentvolumeclaims"]["quota"] == "tight"
        assert exhausted_resources(summary) == ["persistentvolumeclaims"]

    def test_quota_without_storage_keys_is_ignored(self):
        cpu_only = _quota("q", {"requests.cpu": "1"}, {"requests.cpu": "1"})
        assert exhausted_resources(summarize([cpu_only], [])) == []

    def test_missing_used_counts_as_zero(self):
        fresh = {"metadata": {"name": "q"}, "spec": {"hard": {"persistentvolumeclaims": "10"}}}
        assert exhausted_resources(summarize([fresh], [])) == []


class TestExhausted:
    def test_full_pvc_count(self):
        assert exhausted_resources(summarize([FULL_PVC_QUOTA], [])) == ["persistentvolumeclaims"]

    def test_full_storage(self):
        q = _quota("q", {"persistentvolumeclaims": "10", "requests.storage": "100Gi"},
                   {"persistentvolumeclaims": "4", "requests.storage": "100Gi"})
        assert exhausted_resources(summarize([q], [])) == ["requests.storage"]

    def test_headroom_is_not_exhausted(self):
        assert exhausted_resources(summarize([ROOMY_QUOTA], [])) == []


class TestHelmStderr:
    def test_pvc_count_rejection(self):
        assert is_storage_quota_rejection(PVC_REJECTION) == ["persistentvolumeclaims"]

    def test_storage_rejection(self):
        assert is_storage_quota_rejection(STORAGE_REJECTION) == ["requests.storage"]

    def test_combined_rejection(self):
        both = ("exceeded quota: q, requested: persistentvolumeclaims=1,requests.storage=8Gi, "
                "used: persistentvolumeclaims=10,requests.storage=95Gi, "
                "limited: persistentvolumeclaims=10,requests.storage=100Gi")
        assert is_storage_quota_rejection(both) == ["persistentvolumeclaims", "requests.storage"]

    @pytest.mark.parametrize("stderr", [None, "", CPU_REJECTION,
                                        "Error: INSTALLATION FAILED: timed out waiting"])
    def test_other_failures_are_not_quota(self, stderr):
        """CPU／記憶體的 quota 拒絕不歸本檢查（訊息會誤導租戶去清儲存空間）。"""
        assert is_storage_quota_rejection(stderr) == []


class TestErrorDetail:
    def test_detail_without_summary_still_carries_code(self):
        detail = build_quota_error_detail(NS, None, ["persistentvolumeclaims"])
        assert detail["error_code"] == STORAGE_QUOTA_ERROR_CODE
        assert detail["exhausted"] == ["persistentvolumeclaims"]
        assert detail["usage"] == {}
        assert "orphaned_pvc_count" not in detail


class TestSnapshotKubectl:
    def test_reads_quota_and_pvcs_read_only(self):
        svc = KubernetesService()
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            out = MagicMock()
            out.stdout = '{"items": [{"metadata": {"name": "x"}}]}'
            return out

        with patch.object(svc, "_run_command", side_effect=fake_run):
            snap = svc.get_storage_quota_snapshot(NS)
        assert calls == [
            ["get", "resourcequota", "--namespace", NS, "-o", "json"],
            ["get", "pvc", "--namespace", NS, "-o", "json"],
        ]
        assert snap["quotas"] == [{"metadata": {"name": "x"}}]
        assert snap["pvcs"] == [{"metadata": {"name": "x"}}]

    def test_rejects_foreign_namespace(self):
        with pytest.raises(ValueError):
            KubernetesService().get_storage_quota_snapshot("kube-system")


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _release():
    return ReleaseInfo(name="svc-x", namespace=NS, revision=1, status=ReleaseStatus.DEPLOYED,
                       chart="open-design", app_version="1", updated="2026-10-03T00:00:00Z")


def _post(client):
    return client.post("/api/releases", json={"namespace": NS, "name": "svc-x",
                                              "chart": "oci://example.invalid/open-design"})


class TestCreateReleaseQuotaGate:
    @patch("src.api.releases.helm_service")
    def test_full_quota_returns_409_before_any_cluster_write(
        self, mock_helm, client, storage_quota_snapshot, sa_pull_secret_convergence
    ):
        storage_quota_snapshot.return_value = {
            "quotas": [FULL_PVC_QUOTA],
            "pvcs": [_pvc(f"o{i}", "10Gi", orphaned_at=1_000 + i, grace_days=90,
                          release=f"svc-{i}") for i in range(9)] + [_pvc("live", "5Gi")],
        }
        resp = _post(client)

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == STORAGE_QUOTA_ERROR_CODE
        assert detail["exhausted"] == ["persistentvolumeclaims"]
        assert detail["usage"]["persistentvolumeclaims"] == {"used": "10", "hard": "10"}
        assert detail["orphaned_pvc_count"] == 9
        assert detail["orphaned_storage"] == "90Gi"
        assert detail["next_release_epoch"] == 1_000 + 90 * 86400
        mock_helm.install.assert_not_called()
        sa_pull_secret_convergence.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_headroom_installs_as_before(self, mock_helm, client, storage_quota_snapshot):
        storage_quota_snapshot.return_value = {"quotas": [ROOMY_QUOTA], "pvcs": []}
        mock_helm.install.return_value = _release()
        assert _post(client).status_code == 201
        mock_helm.install.assert_called_once()

    @patch("src.api.releases.helm_service")
    def test_unreadable_quota_fails_open(self, mock_helm, client, storage_quota_snapshot):
        """讀不到配額＝不知道：放行，行為與加入檢查前相同（不是新的閘門）。"""
        storage_quota_snapshot.side_effect = KubectlException(
            message="boom", command="kubectl get resourcequota", stderr="forbidden")
        mock_helm.install.return_value = _release()
        assert _post(client).status_code == 201
        mock_helm.install.assert_called_once()

    @patch("src.api.releases.helm_service")
    def test_helm_quota_rejection_maps_to_same_409(
        self, mock_helm, client, storage_quota_snapshot
    ):
        """剩 1 顆、chart 要 2 顆：事前檢查放行，helm 被 admission 擋 ⇒ 同一個錯誤碼。"""
        storage_quota_snapshot.return_value = {"quotas": [_quota(
            "q", {"persistentvolumeclaims": "10"}, {"persistentvolumeclaims": "9"})], "pvcs": []}
        mock_helm.install.side_effect = HelmException(
            message="helm command failed with code 1", command="helm install", stderr=PVC_REJECTION)
        resp = _post(client)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == STORAGE_QUOTA_ERROR_CODE
        assert detail["exhausted"] == ["persistentvolumeclaims"]

    @patch("src.api.releases.helm_service")
    def test_other_helm_failures_keep_the_generic_500(self, mock_helm, client):
        mock_helm.install.side_effect = HelmException(
            message="helm command failed with code 1", command="helm install", stderr=CPU_REJECTION)
        resp = _post(client)
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Helm installation failed. Check operator logs for details."
