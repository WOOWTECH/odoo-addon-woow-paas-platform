"""#1647 integration：對真 ResourceQuota 讀上限與已佔用量。

單元測試餵的是手寫的 quota JSON，看不到兩件只有真叢集才有的事：

1. quota controller 實際寫進 ``status.used`` 的字串形狀（``250m``、``64Mi``、``8Mi``）與
   哪些物件被計入——停在 Pending 的 Pod 與還沒 Bound 的 PVC **都算**（quota 計的是宣告
   的 requests／limits，不看有沒有排上節點），這正是工作區頁要顯示的「已佔用」；
2. operator 的 ServiceAccount／kubeconfig 對 ``resourcequotas`` 有沒有 list 權限，以及
   kubectl 對「不存在的 namespace」做 list 是不是真的回空清單而不是報錯。

Pod 用不存在的 node label 當 nodeSelector：永遠排不上節點、不拉 image、不佔任何實際資源。
PVC 用不存在的 StorageClass：停在 Pending、不 provision 任何 volume（同
``test_storage_quota_1348.py`` 的手法）。namespace 由 fixture 收掉。
"""
import json
import subprocess
import time
import uuid

import pytest

from tests.namespace_scope import integration_namespace_prefix

pytestmark = pytest.mark.integration

_NO_PROVISION_SC = "woow-int-1647-no-provisioner"
_MI = 1024 ** 2
_GI = 1024 ** 3

# 等 status.used 收斂時比對的原始字串（quota controller 寫出來的形狀）。
_EXPECTED_USED = {
    "requests.cpu": "100m",
    "limits.cpu": "250m",
    "requests.memory": "32Mi",
    "limits.memory": "64Mi",
    "requests.storage": "8Mi",
    "persistentvolumeclaims": "1",
}


def _kubectl_apply(manifest: str) -> None:
    subprocess.run(["kubectl", "apply", "-f", "-"], input=manifest, text=True,
                   check=True, capture_output=True)


def _quota_manifest(namespace: str) -> str:
    return (
        "apiVersion: v1\nkind: ResourceQuota\nmetadata:\n"
        f"  name: {namespace}-quota\n  namespace: {namespace}\n"
        "spec:\n  hard:\n"
        "    requests.cpu: \"2\"\n"
        "    limits.cpu: \"4\"\n"
        "    requests.memory: 1Gi\n"
        "    limits.memory: 2Gi\n"
        "    requests.storage: 1Gi\n"
        "    persistentvolumeclaims: \"5\"\n"
    )


def _pending_pod_manifest(namespace: str) -> str:
    return (
        "apiVersion: v1\nkind: Pod\nmetadata:\n"
        f"  name: quota-probe-1647\n  namespace: {namespace}\n"
        "spec:\n"
        "  restartPolicy: Never\n"
        "  automountServiceAccountToken: false\n"
        "  nodeSelector:\n"
        "    woow.tw/int-1647-no-such-node: \"true\"\n"
        "  containers:\n"
        "    - name: probe\n"
        "      image: registry.k8s.io/pause:3.9\n"
        "      resources:\n"
        "        requests:\n          cpu: 100m\n          memory: 32Mi\n"
        "        limits:\n          cpu: 250m\n          memory: 64Mi\n"
    )


def _pending_pvc_manifest(namespace: str) -> str:
    return (
        "apiVersion: v1\nkind: PersistentVolumeClaim\nmetadata:\n"
        f"  name: quota-probe-1647-data\n  namespace: {namespace}\n"
        "spec:\n"
        f"  storageClassName: {_NO_PROVISION_SC}\n"
        "  accessModes: [ReadWriteOnce]\n"
        "  resources:\n    requests:\n      storage: 8Mi\n"
    )


def _wait_default_service_account(namespace: str, timeout: float = 30) -> None:
    """namespace 剛建好時 ``default`` ServiceAccount 還沒出現，直接建 Pod 會被 admission
    拒絕（``serviceaccount "default" not found``）；Deployment 會自己重試，裸 Pod 不會。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(["kubectl", "get", "serviceaccount", "default", "-n", namespace],
                             capture_output=True, text=True)
        if out.returncode == 0:
            return
        time.sleep(0.5)
    pytest.fail(f"{namespace} 的 default ServiceAccount 在 {timeout}s 內沒有出現")


def _quota_status(namespace: str) -> dict:
    out = subprocess.run(
        ["kubectl", "get", "resourcequota", f"{namespace}-quota", "-n", namespace,
         "-o", "json"], capture_output=True, text=True)
    if out.returncode != 0:
        return {}
    return json.loads(out.stdout).get("status") or {}


def _wait_quota_used(namespace: str, expected: dict, timeout: float = 60) -> dict:
    """quota controller 非同步更新 status.used；端點讀的就是它，要等它收斂。"""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        status = _quota_status(namespace)
        last = status.get("used") or {}
        if all(last.get(key) == value for key, value in expected.items()):
            return status
        time.sleep(1)
    pytest.fail(f"quota status.used 沒有收斂到 {expected}（最後 {last!r}）")


def test_reports_real_hard_and_used(app_client, test_namespace):
    """端點回報的 hard／used 與叢集上 ResourceQuota 的 status 一致，數值換算正確。"""
    _kubectl_apply(_quota_manifest(test_namespace))
    _wait_default_service_account(test_namespace)
    _kubectl_apply(_pending_pod_manifest(test_namespace))
    _kubectl_apply(_pending_pvc_manifest(test_namespace))
    status = _wait_quota_used(test_namespace, _EXPECTED_USED)

    r = app_client.get(f"/api/namespaces/{test_namespace}/quota")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["namespace"] == test_namespace
    assert body["available"] is True
    assert body["reason"] is None
    assert body["quotas"] == [f"{test_namespace}-quota"]
    res = body["resources"]
    assert set(res) == set(_EXPECTED_USED), "quota 宣告的六個鍵都要回，且不多回"

    # 數值：寫死的期望值，不經 operator 自己的 quantity parser。
    assert res["limits.cpu"]["used_value"] == 0.25
    assert res["limits.cpu"]["hard_value"] == 4.0
    assert res["limits.cpu"]["ratio"] == 0.0625
    assert res["requests.cpu"]["used_value"] == 0.1
    assert res["requests.cpu"]["hard_value"] == 2.0
    assert res["limits.memory"]["used_value"] == 64 * _MI
    assert res["limits.memory"]["hard_value"] == 2 * _GI
    assert res["requests.memory"]["used_value"] == 32 * _MI
    assert res["requests.storage"]["used_value"] == 8 * _MI
    assert res["requests.storage"]["hard_value"] == 1 * _GI
    assert res["persistentvolumeclaims"]["used_value"] == 1
    assert res["persistentvolumeclaims"]["hard_value"] == 5
    assert res["limits.cpu"]["unit"] == "cores"
    assert res["limits.memory"]["unit"] == "bytes"
    assert res["requests.storage"]["unit"] == "bytes"
    assert res["persistentvolumeclaims"]["unit"] == "count"

    # 原始字串：與 `kubectl get resourcequota -o json` 的 status 逐鍵相同。
    for key, cell in res.items():
        assert cell["used"] == status["used"][key], key
        assert cell["hard"] == status["hard"][key], key
        assert cell["ratio"] is not None, key
        assert cell["quota"] == f"{test_namespace}-quota", key


def test_missing_namespace_is_quota_not_found(app_client):
    """合法前綴但不存在的 namespace：kubectl list 回空清單 ⇒ 200＋quota_not_found。"""
    namespace = f"{integration_namespace_prefix()}nons{uuid.uuid4().hex[:8]}"

    r = app_client.get(f"/api/namespaces/{namespace}/quota")

    assert r.status_code == 200, r.text
    assert r.json() == {
        "namespace": namespace, "available": False, "reason": "quota_not_found",
        "quotas": [], "resources": {},
    }


def test_foreign_namespace_is_400(app_client):
    """允許前綴以外的 namespace 一律 400，不對它下任何 kubectl。"""
    r = app_client.get("/api/namespaces/kube-system/quota")

    assert r.status_code == 400, r.text
