"""#1263：真 woow-odoo chart 的 Pod 經 status API 帶出正確身分（#1251 Q19／Q44）。

unit 測試用的 kubectl JSON 是照 chart template 手抄的；這支用**真 chart**（server
Deployment、postgresql StatefulSet、backup CronJob 的 jobTemplate）加真 K8s controller
建出來的 Pod，驗 label 與 ownerReferences 真的長成 operator 以為的樣子。

Pod 不必 Ready：身分只看 metadata，而 odoo image 的 digest 預設是佔位值（刻意
ImagePullBackOff）。PVC 一律改用 ``local-path``，namespace 刪除時跟著回收。

需要 ``WOOW_ODOO_CHART``＝woow-odoo chart 目錄（例：woow-paas-charts 在 pin 的
commit 上 ``git archive <commit> charts/woow-odoo | tar x``）；沒設就 skip——**skip 不
是通過**，報告要看到 passed。
"""
import json
import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

_RELEASE = "svc-1263"


def _kubectl(*args):
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True,
    ).stdout


def _wait_for_pod(namespace, selector, timeout=180):
    """等到 selector 有 Pod 物件出現即可（不等 Ready）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = json.loads(_kubectl("get", "pods", "-n", namespace, "-l", selector,
                                    "-o", "json"))["items"]
        if items:
            return items
        time.sleep(2)
    raise AssertionError(f"{timeout}s 內沒有 Pod 符合 {selector}")


def test_status_reports_identity_of_real_woow_odoo_pods(app_client, test_namespace):
    chart = os.environ.get("WOOW_ODOO_CHART")
    if not chart:
        pytest.skip("WOOW_ODOO_CHART 未設定：需要真 woow-odoo chart 目錄才能驗身分")

    subprocess.run([
        os.environ["HELM_BINARY"], "install", _RELEASE, chart,
        "--namespace", test_namespace,
        "--set", "persistence.storageClass=local-path",
        "--set", "postgres.persistence.storageClass=local-path",
        "--set", "backup.persistence.storageClass=local-path",
        "--set", "backup.housekeepingEnabled=false",
        "--set", "resources.requests.cpu=10m",
        "--set", "resources.requests.memory=32Mi",
        "--set", "postgres.resources.requests.cpu=10m",
        "--set", "postgres.resources.requests.memory=32Mi",
    ], check=True, capture_output=True, text=True)

    instance = f"app.kubernetes.io/instance={_RELEASE}"
    _wait_for_pod(test_namespace, f"{instance},app.kubernetes.io/component=server")
    _wait_for_pod(test_namespace, f"{instance},app.kubernetes.io/component=postgresql")
    # 升級前備份就是 operator 讀這支 CronJob 的 jobTemplate 自組 Job；這裡用同一份模板。
    _kubectl("create", "job", f"{_RELEASE}-backup-it", f"--from=cronjob/{_RELEASE}-backup",
             "-n", test_namespace)
    _wait_for_pod(test_namespace, f"job-name={_RELEASE}-backup-it")

    response = app_client.get(f"/api/releases/{test_namespace}/{_RELEASE}/status")

    assert response.status_code == 200
    body = response.json()
    assert body["pod_identity_included"] is True
    assert body["pod_retrieval_error"] is None
    identity = {(p["component"], p["task"], p["owner_kind"]) for p in body["pods"]}
    assert ("server", None, "ReplicaSet") in identity
    assert ("postgresql", None, "StatefulSet") in identity
    assert ("db-backup", "backup", "Job") in identity
    assert all(p["creation_timestamp"] for p in body["pods"])
