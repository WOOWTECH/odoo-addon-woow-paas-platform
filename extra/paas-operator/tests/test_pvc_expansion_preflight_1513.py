"""#1513：升級前先讓 Kubernetes／Longhorn 判斷 PVC 擴容放不放得下——放不下就在 helm 套用任何東西前擋。

起因（woow_paas_platform #1158 實測，2026-10-06）：換方案把 woow-odoo 的 data PVC 從 20Gi 加到
30Gi，Longhorn admission webhook 以 ``CheckReplicasSizeExpansion`` 拒絕（replica 所在磁碟的
排程額度不夠）。helm upgrade 不是交易：Deployment／Secret 已換成新規格、backup PVC 也擴了，
只有 data PVC 沒擴，release 標 ``failed``。

做法：#1407 的升級預檢已經 dry-run 出 manifest、讀了 live PVC。這裡再挑出「要變大」的 PVC，
各送一次 ``kubectl patch --dry-run=server``——Longhorn 的 validator 宣告 ``sideEffects: None``，
server dry-run 會照常呼叫它，所以判斷由 Longhorn 自己做，不必在 operator 重寫排程規則、也不必
多給 ``replicas.longhorn.io`` 的權限。另開唯讀端點 ``POST …/upgrade-preflight``，讓平台在**扣款前**
用同一組參數問一次。
"""
from unittest.mock import patch

import pytest
import yaml

from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import KubectlException
from src.services.upgrade_preflight import (
    STORAGE_EXPANSION_ERROR_CODE,
    build_expansion_error_detail,
    classify_expansion_rejection,
    find_pvc_expansions,
    longhorn_volume_expansion_blocker,
)

_LONGHORN_DENIAL = (
    'Error from server (Forbidden): admission webhook "validator.longhorn.io" denied the request: '
    'error while CheckReplicasSizeExpansion for volume pvc-e47cfb99: cannot schedule 10737418240 '
    'more bytes to disk 6b4d9c67-1626-40e8-bf2c-6cf6b3f362c1 with &{DiskUUID:6b4d9c67 '
    'StorageAvailable:307547340800 StorageMaximum:738673549312 StorageReserved:31526778470 '
    'StorageScheduled:1408749273088 OverProvisioningPercentage:200 MinimalAvailablePercentage:25}\n'
)
_NO_EXPANSION_SC = (
    'Error from server (Forbidden): persistentvolumeclaims "r-data" is forbidden: only dynamically '
    'provisioned pvc can be resized and the storageclass that provisions the pvc must support resize\n'
)


def _pvc(name, size, kind="PersistentVolumeClaim", volume=None):
    spec = {"accessModes": ["ReadWriteOnce"], "storageClassName": "longhorn-delete",
            "resources": {"requests": {"storage": size}}}
    if volume:
        spec["volumeName"] = volume
    return {"kind": kind, "metadata": {"name": name}, "spec": spec}


def _lh_volume(scheduled="True", state="attached"):
    return {"kind": "Volume", "metadata": {"name": "pvc-1"},
            "status": {"state": state, "robustness": "degraded",
                       "conditions": [{"type": "Scheduled", "status": scheduled,
                                       "reason": "ReplicaSchedulingFailure",
                                       "message": "insufficient storage"}]}}


# --- 純函式 ------------------------------------------------------------------------

def test_only_growing_live_pvcs_are_expansions():
    rendered = [_pvc("r-data", "30Gi"), _pvc("r-backup", "20Gi"), _pvc("r-new", "50Gi"),
                _pvc("r-same", "8Gi"), {"kind": "Deployment", "metadata": {"name": "r-data"}}]
    live = [_pvc("r-data", "20Gi"), _pvc("r-backup", "20Gi"), _pvc("r-same", "8Gi")]
    assert find_pvc_expansions(rendered, live) == [
        {"name": "r-data", "live": "20Gi", "rendered": "30Gi"}]


def test_expansion_carries_the_bound_volume_name():
    rendered = [_pvc("r-data", "30Gi")]
    live = [_pvc("r-data", "20Gi", volume="pvc-1")]
    assert find_pvc_expansions(rendered, live) == [
        {"name": "r-data", "live": "20Gi", "rendered": "30Gi", "volume": "pvc-1"}]


def test_degraded_longhorn_volume_cannot_expand():
    """longhorn-manager v1.10.2 ``VolumeManager.Expand``：``Scheduled`` 條件不是 True 就拒絕
    （"cannot expand volume before replica scheduling success"）——admission 放行、事後才卡住。"""
    assert "replica" in longhorn_volume_expansion_blocker(_lh_volume(scheduled="False"))


def test_fully_scheduled_volume_is_not_blocked():
    assert longhorn_volume_expansion_blocker(_lh_volume(scheduled="True")) is None


def test_unreadable_volume_shape_is_not_a_verdict():
    for obj in ({}, {"status": {}}, {"status": {"conditions": "x"}}, None):
        assert longhorn_volume_expansion_blocker(obj) is None


def test_unit_change_without_growth_is_not_an_expansion():
    assert find_pvc_expansions([_pvc("r-data", "20480Mi")], [_pvc("r-data", "20Gi")]) == []


def test_shrink_is_not_an_expansion():
    """縮小是 #1407 的不可變衝突，不在這裡處理（避免同一件事報兩次）。"""
    assert find_pvc_expansions([_pvc("r-data", "10Gi")], [_pvc("r-data", "20Gi")]) == []


def test_classify_longhorn_capacity_denial():
    assert classify_expansion_rejection(_LONGHORN_DENIAL) == "capacity"


def test_classify_other_api_rejection():
    assert classify_expansion_rejection(_NO_EXPANSION_SC) == "rejected"


def test_unknown_failures_are_not_a_verdict():
    """逾時、連不上 API server 都是「不知道」——放行，行為與本檢查加入前相同。"""
    for stderr in ("Unable to connect to the server: dial tcp: i/o timeout",
                   "error: timed out waiting for the condition", "", None):
        assert classify_expansion_rejection(stderr) is None


def test_error_detail_is_machine_readable_and_chinese():
    detail = build_expansion_error_detail("paas-ws-1", [
        {"name": "r-data", "live": "20Gi", "rendered": "30Gi", "reason": "capacity",
         "message": _LONGHORN_DENIAL.strip()}])
    assert detail["error_code"] == STORAGE_EXPANSION_ERROR_CODE
    assert detail["reason"] == "capacity"
    assert detail["namespace"] == "paas-ws-1"
    assert detail["pvcs"][0]["name"] == "r-data"
    assert "r-data" in detail["message"] and "20Gi" in detail["message"] and "30Gi" in detail["message"]
    assert "沒有套用任何變更" in detail["message"]


def test_any_non_capacity_rejection_makes_the_reason_rejected():
    detail = build_expansion_error_detail("ns", [
        {"name": "a", "live": "1Gi", "rendered": "2Gi", "reason": "capacity", "message": "x"},
        {"name": "b", "live": "1Gi", "rendered": "2Gi", "reason": "rejected", "message": "y"}])
    assert detail["reason"] == "rejected"


# --- kubectl seam ------------------------------------------------------------------

@patch("src.services.helm.subprocess.run")
def test_dry_run_resize_is_a_server_dry_run_patch(mock_run):
    from src.services.helm import KubernetesService

    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = ""
    KubernetesService().dry_run_pvc_resize("paas-ws-1", "r-data", "30Gi")
    cmd = mock_run.call_args.args[0]
    assert cmd[1:4] == ["patch", "pvc", "r-data"]
    assert "--dry-run=server" in cmd
    assert "--namespace" in cmd and "paas-ws-1" in cmd
    patch_arg = cmd[cmd.index("--patch") + 1]
    assert yaml.safe_load(patch_arg) == {"spec": {"resources": {"requests": {"storage": "30Gi"}}}}


def test_dry_run_resize_rejects_bad_namespace():
    from src.services.helm import KubernetesService

    with pytest.raises(ValueError):
        KubernetesService().dry_run_pvc_resize("kube-system", "r-data", "30Gi")


# --- endpoint ----------------------------------------------------------------------

_INFO = ReleaseInfo(name="r", namespace="paas-ws-test", revision=3,
                    status=ReleaseStatus.DEPLOYED, chart="woow-odoo", app_version="18.0",
                    updated="2024-01-02T00:00:00Z")
_BODY = {"chart": "woow-odoo", "version": "0.2.7", "reset_values": True,
         "values": {"persistence": {"size": "30Gi"}}}


def _dry_run_aware(manifest_docs):
    def upgrade(**kwargs):
        if kwargs.get("dry_run"):
            return yaml.safe_dump_all(manifest_docs)
        return _INFO
    return upgrade


def _denial(stderr):
    return KubectlException(message="kubectl command failed with code 1",
                            command="kubectl patch", stderr=stderr)


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_upgrade_refuses_expansion_longhorn_cannot_place(mock_helm, mock_k8s, client,
                                                          live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    mock_k8s.dry_run_pvc_resize.side_effect = _denial(_LONGHORN_DENIAL)
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error_code"] == STORAGE_EXPANSION_ERROR_CODE
    assert detail["reason"] == "capacity"
    mock_k8s.dry_run_pvc_resize.assert_called_once_with("paas-ws-test", "r-data", "30Gi")
    # 只跑了 dry-run——什麼都沒套用（#1158 實測的半套狀態不會再出現）
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True]


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_upgrade_refuses_expanding_a_degraded_volume(mock_helm, mock_k8s, client,
                                                      live_objects_lookup):
    """#1158 e2e 實測：admission 放行、PVC 卻永遠停在 Resizing——Longhorn volume 的 replica
    沒排滿（degraded）時不給擴。這種要在 helm 之前擋，dry-run 看不到。"""
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi", volume="pvc-1")]
    mock_k8s.get_longhorn_volume.return_value = _lh_volume(scheduled="False")
    mock_k8s.dry_run_pvc_resize.return_value = None
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "capacity"
    mock_k8s.get_longhorn_volume.assert_called_once_with("pvc-1")
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True]


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_unreadable_longhorn_volume_falls_back_to_dry_run(mock_helm, mock_k8s, client,
                                                         live_objects_lookup):
    """讀不到 volumes.longhorn.io（舊 ClusterRole 沒給、volume 不是 Longhorn）＝不知道，照跑 dry-run。"""
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi", volume="pvc-1")]
    mock_k8s.get_longhorn_volume.side_effect = _denial(
        'Error from server (Forbidden): volumes.longhorn.io "pvc-1" is forbidden: User '
        '"system:serviceaccount:paas-system:paas-operator" cannot get resource "volumes"')
    mock_k8s.dry_run_pvc_resize.return_value = None
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 200
    mock_k8s.dry_run_pvc_resize.assert_called_once()


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_upgrade_proceeds_when_expansion_fits(mock_helm, mock_k8s, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    mock_k8s.dry_run_pvc_resize.return_value = None
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 200
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True, None]


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_unknown_dry_run_failure_does_not_block(mock_helm, mock_k8s, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    mock_k8s.dry_run_pvc_resize.side_effect = _denial("Unable to connect to the server: EOF")
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 200


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_no_growth_sends_no_dry_run(mock_helm, mock_k8s, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "20Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 200
    mock_k8s.dry_run_pvc_resize.assert_not_called()


@patch("src.api.releases.settings")
@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_switch_off_skips_the_expansion_check(mock_helm, mock_k8s, mock_settings, client,
                                              live_objects_lookup):
    mock_settings.storage_capacity_precheck_enabled = False
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    r = client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    assert r.status_code == 200
    mock_k8s.dry_run_pvc_resize.assert_not_called()


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_preflight_endpoint_applies_nothing(mock_helm, mock_k8s, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    mock_k8s.dry_run_pvc_resize.return_value = None
    r = client.post("/api/releases/paas-ws-test/r/upgrade-preflight", json=_BODY)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "expansions": [
        {"name": "r-data", "live": "20Gi", "rendered": "30Gi"}]}
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True]


@patch("src.api.releases.k8s_service")
@patch("src.api.releases.helm_service")
def test_preflight_endpoint_reports_the_denial(mock_helm, mock_k8s, client, live_objects_lookup):
    mock_helm.upgrade.side_effect = _dry_run_aware([_pvc("r-data", "30Gi")])
    live_objects_lookup["objects"].return_value = [_pvc("r-data", "20Gi")]
    mock_k8s.dry_run_pvc_resize.side_effect = _denial(_LONGHORN_DENIAL)
    r = client.post("/api/releases/paas-ws-test/r/upgrade-preflight", json=_BODY)
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "capacity"
    assert [c.kwargs.get("dry_run") for c in mock_helm.upgrade.call_args_list] == [True]


@patch("src.api.releases.helm_service")
def test_preflight_endpoint_sends_the_same_arguments_as_upgrade(mock_helm, client):
    mock_helm.upgrade.side_effect = _dry_run_aware([])
    client.post("/api/releases/paas-ws-test/r/upgrade-preflight", json=_BODY)
    client.patch("/api/releases/paas-ws-test/r", json=_BODY)
    pre, up_dry, up_real = mock_helm.upgrade.call_args_list
    assert pre.kwargs == up_dry.kwargs
    assert {k: v for k, v in pre.kwargs.items() if k != "dry_run"} == up_real.kwargs


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from src.main import app

    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})
