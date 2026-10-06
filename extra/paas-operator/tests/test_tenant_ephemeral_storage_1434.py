"""#1434：租戶 ns 的 ephemeral-storage 上限（LimitRange 預設＋ResourceQuota）。

R8 實測：所有租戶容器的 resources 只有 cpu／memory，/tmp 等 overlay 路徑可無限寫進
節點系統碟（含正式站節點）。operator 端的修法兩件：

1. LimitRange 給沒宣告的容器補 ephemeral-storage 的 request／limit 預設。
2. 平台送 ``ephemeral_storage_*`` 時，ResourceQuota 收 ``requests/limits.ephemeral-storage``；
   沒送（舊平台）時 hard map 與 #1434 之前一字不差。

而且順序必須是「先 LimitRange、後 quota」：quota 一宣告 limits.ephemeral-storage，
admission 就拒收沒宣告的容器；預設若還沒補上，那一瞬間建的 Pod 會被拒。
"""
import json
from unittest.mock import Mock, patch

import pytest
import yaml

from src.models.schemas import NamespaceCreateRequest, NamespaceQuotaUpdateRequest
from src.services.helm import KubernetesService


@pytest.fixture
def k8s_service():
    return KubernetesService()


def _applied_docs(mock_run):
    """每次 `kubectl apply -f <tmp>` 當下讀出的 manifest（依呼叫順序）。"""
    return mock_run.applied


def _recording_run():
    mock_run = Mock()
    mock_run.applied = []

    def _side_effect(cmd, *args, **kwargs):
        if "apply" in cmd and "-f" in cmd:
            path = cmd[cmd.index("-f") + 1]
            with open(path) as fh:
                mock_run.applied.append(yaml.safe_load(fh))
        return Mock(returncode=0, stdout="{}", stderr="")

    mock_run.side_effect = _side_effect
    return mock_run


class TestLimitRangeDefaults:
    def test_limitrange_carries_ephemeral_defaults(self, k8s_service):
        entry = k8s_service._build_limitrange_manifest("paas-ws-eph")["spec"]["limits"][0]
        assert entry["default"]["ephemeral-storage"] == "4Gi"
        assert entry["defaultRequest"]["ephemeral-storage"] == "128Mi"

    def test_limitrange_defaults_follow_settings(self, k8s_service, monkeypatch):
        monkeypatch.setattr(
            "src.config.settings.tenant_ephemeral_storage_default_limit", "6Gi")
        monkeypatch.setattr(
            "src.config.settings.tenant_ephemeral_storage_default_request", "256Mi")
        entry = k8s_service._build_limitrange_manifest("paas-ws-eph")["spec"]["limits"][0]
        assert entry["default"]["ephemeral-storage"] == "6Gi"
        assert entry["defaultRequest"]["ephemeral-storage"] == "256Mi"


class TestQuotaManifest:
    def test_without_ephemeral_is_unchanged(self, k8s_service):
        """舊平台不送 → hard map 與 #1434 之前完全相同（operator 可先發版）。"""
        hard = k8s_service._build_quota_manifest(
            "paas-ws-eph", "8", "8Gi", "100Gi")["spec"]["hard"]
        assert hard == {
            "requests.cpu": "8",
            "requests.memory": "8Gi",
            "limits.cpu": "8",
            "limits.memory": "8Gi",
            "persistentvolumeclaims": "10",
            "requests.storage": "100Gi",
        }

    def test_with_ephemeral_adds_both_keys(self, k8s_service):
        hard = k8s_service._build_quota_manifest(
            "paas-ws-eph", "8", "8Gi", "100Gi", "4Gi", "80Gi")["spec"]["hard"]
        assert hard["requests.ephemeral-storage"] == "4Gi"
        assert hard["limits.ephemeral-storage"] == "80Gi"


class TestApplyOrder:
    def test_create_namespace_applies_limitrange_before_quota(self, k8s_service):
        mock_run = _recording_run()
        with patch("subprocess.run", mock_run), \
                patch.object(KubernetesService, "ensure_registry_secret"), \
                patch.object(KubernetesService, "ensure_default_sa_image_pull_secret"):
            k8s_service.create_namespace(
                "paas-ws-eph", "8", "8Gi", "100Gi",
                ephemeral_storage_request="4Gi", ephemeral_storage_limit="80Gi",
            )
        kinds = [d["kind"] for d in _applied_docs(mock_run)]
        assert kinds.index("LimitRange") < kinds.index("ResourceQuota")
        quota = next(d for d in _applied_docs(mock_run) if d["kind"] == "ResourceQuota")
        assert quota["spec"]["hard"]["limits.ephemeral-storage"] == "80Gi"

    def test_update_quota_with_ephemeral_reapplies_limitrange_first(self, k8s_service):
        """既有 ns 第一次收到含 ephemeral 的 quota：LimitRange 可能還是舊的，要先補。"""
        mock_run = _recording_run()
        with patch("subprocess.run", mock_run):
            k8s_service.update_namespace_quota(
                "paas-ws-eph", "8", "8Gi", "100Gi",
                ephemeral_storage_request="4Gi", ephemeral_storage_limit="80Gi",
            )
        kinds = [d["kind"] for d in _applied_docs(mock_run)]
        assert kinds == ["LimitRange", "ResourceQuota"]

    def test_update_quota_without_ephemeral_touches_quota_only(self, k8s_service):
        mock_run = _recording_run()
        with patch("subprocess.run", mock_run):
            k8s_service.update_namespace_quota("paas-ws-eph", "8", "8Gi", "100Gi")
        kinds = [d["kind"] for d in _applied_docs(mock_run)]
        assert kinds == ["ResourceQuota"]
        assert "limits.ephemeral-storage" not in _applied_docs(mock_run)[0]["spec"]["hard"]


class TestSchemas:
    def test_create_request_ephemeral_optional(self):
        req = NamespaceCreateRequest(name="paas-ws-eph")
        assert req.ephemeral_storage_request is None
        assert req.ephemeral_storage_limit is None

    def test_quota_update_request_accepts_ephemeral(self):
        req = NamespaceQuotaUpdateRequest(
            cpu_limit="8", memory_limit="8Gi", storage_limit="100Gi",
            ephemeral_storage_request="4Gi", ephemeral_storage_limit="80Gi",
        )
        assert req.ephemeral_storage_limit == "80Gi"


class TestNamespaceApiWiring:
    def test_put_quota_forwards_ephemeral(self):
        from fastapi.testclient import TestClient
        from src.main import app

        with patch("src.main.settings") as mock_settings, \
                patch("src.api.namespaces.k8s_service") as mock_k8s:
            mock_settings.api_key = "k"
            mock_settings.namespace_prefix = "paas-ws-"
            mock_k8s.update_namespace_quota.return_value = {"message": "ok"}
            client = TestClient(app, headers={"X-API-Key": "k"})
            resp = client.put("/api/namespaces/paas-ws-eph/quota", json={
                "cpu_limit": "8", "memory_limit": "8Gi", "storage_limit": "100Gi",
                "ephemeral_storage_request": "4Gi", "ephemeral_storage_limit": "80Gi",
            })
        assert resp.status_code == 200, resp.text
        kwargs = mock_k8s.update_namespace_quota.call_args.kwargs
        assert kwargs["ephemeral_storage_request"] == "4Gi"
        assert kwargs["ephemeral_storage_limit"] == "80Gi"
