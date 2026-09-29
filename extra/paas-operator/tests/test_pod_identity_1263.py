"""#1263：Pod 身分（component／task／owner_kind）必須穿過 status API（#1251 Q19／Q44）。

身分證據＝label＋Pod **直接** owner 的 kind（controller=true 那筆），沿用 #1129
``validate_fallback_pod`` 的語意，不追到 Deployment UID。只替換 subprocess 邊界，
讓真的 ``get_pods`` 解析 kubectl JSON。
"""
import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import PodInfo, ReleaseInfo, ReleaseStatus


def _pod(name, labels=None, owners=None):
    metadata = {"name": name, "labels": labels or {}}
    if owners is not None:
        metadata["ownerReferences"] = owners
    return {
        "metadata": metadata,
        "status": {
            "phase": "Running",
            "containerStatuses": [{"ready": True, "restartCount": 0}],
        },
    }


def _owner(kind, controller=True):
    ref = {"apiVersion": "apps/v1", "kind": kind, "name": f"x-{kind.lower()}", "uid": "u"}
    if controller is not None:
        ref["controller"] = controller
    return ref


_INSTANCE = {"app.kubernetes.io/instance": "svc-1263", "app.kubernetes.io/name": "odoo"}


def _get_status(pods):
    release = ReleaseInfo(
        name="svc-1263", namespace="paas-ws-test", revision=1,
        status=ReleaseStatus.DEPLOYED, chart="woow-odoo", app_version="18.0",
        updated="2026-09-29T00:00:00Z",
    )
    with patch("src.main.settings.api_key", "test-1263"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"items": pods}), stderr="",
            )):
        response = TestClient(app, headers={"X-API-Key": "test-1263"}).get(
            "/api/releases/paas-ws-test/svc-1263/status",
        )
    assert response.status_code == 200
    return response.json()


def test_status_exposes_woow_odoo_pod_identity():
    """chart 的 server／PG／backup Job Pod 各自帶正確的身分。"""
    body = _get_status([
        _pod("svc-1263-abc", {**_INSTANCE, "app.kubernetes.io/component": "server"},
             [_owner("ReplicaSet")]),
        _pod("svc-1263-postgresql-0", {**_INSTANCE, "app.kubernetes.io/component": "postgresql"},
             [_owner("StatefulSet")]),
        _pod("svc-1263-backup-29000000-xyz",
             {**_INSTANCE, "app.kubernetes.io/component": "db-backup", "woow.paas/task": "backup"},
             [_owner("Job")]),
    ])

    assert body["pod_identity_included"] is True
    identity = {p["name"]: (p["component"], p["task"], p["owner_kind"]) for p in body["pods"]}
    assert identity == {
        "svc-1263-abc": ("server", None, "ReplicaSet"),
        "svc-1263-postgresql-0": ("postgresql", None, "StatefulSet"),
        "svc-1263-backup-29000000-xyz": ("db-backup", "backup", "Job"),
    }


def test_owner_kind_comes_only_from_the_controller_reference():
    body = _get_status([
        # 非 controller 的 owner 不算直接 owner。
        _pod("adopted", _INSTANCE, [_owner("ConfigMap", controller=False), _owner("ReplicaSet")]),
        _pod("no-controller-flag", _INSTANCE, [_owner("ReplicaSet", controller=None)]),
        _pod("only-non-controller", _INSTANCE, [_owner("ReplicaSet", controller=False)]),
        _pod("bare", None, None),
    ])

    owner_kind = {p["name"]: p["owner_kind"] for p in body["pods"]}
    assert owner_kind == {
        "adopted": "ReplicaSet",
        "no-controller-flag": None,
        "only-non-controller": None,
        "bare": None,
    }
    bare = next(p for p in body["pods"] if p["name"] == "bare")
    assert (bare["component"], bare["task"]) == (None, None)


def test_identity_flag_is_set_even_when_pod_retrieval_fails():
    """旗標宣告的是「這個 operator 會填身分欄位」；抓 Pod 失敗另由
    ``pod_retrieval_error`` 表達。否則平台會把暫時性的讀取失敗誤判成
    「operator 缺身分欄位」這個 HARD 結論。"""
    release = ReleaseInfo(
        name="svc-1263", namespace="paas-ws-test", revision=1,
        status=ReleaseStatus.DEPLOYED, chart="woow-odoo", app_version="18.0",
        updated="2026-09-29T00:00:00Z",
    )
    with patch("src.main.settings.api_key", "test-1263"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 1, stdout="", stderr="forbidden",
            )):
        body = TestClient(app, headers={"X-API-Key": "test-1263"}).get(
            "/api/releases/paas-ws-test/svc-1263/status",
        ).json()

    assert body["pods"] == []
    assert body["pod_retrieval_error"]
    assert body["pod_identity_included"] is True


def test_legacy_pod_info_needs_no_identity_fields():
    pod = PodInfo(name="odoo", phase="Running", ready="1/1", restarts=0, age="1m")
    assert (pod.component, pod.task, pod.owner_kind) == (None, None, None)
