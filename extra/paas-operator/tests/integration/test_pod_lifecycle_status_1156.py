"""#1156：真 K8s initContainer／刪除中的 Pod 經 status API 保留生命週期訊號。"""
import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.integration


_FINALIZER = "tests.woow.tw/readiness-1156"


def _kubectl(*args):
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True,
    ).stdout


def _pod(namespace, name, *, init=False, finalizer=False):
    container = {
        "name": "main", "image": "busybox:1.36", "command": ["sleep", "600"],
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                      "limits": {"cpu": "100m", "memory": "32Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]}},
    }
    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/instance": "svc-1156"}},
        "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                 "terminationGracePeriodSeconds": 1,
                 "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                     "seccompProfile": {"type": "RuntimeDefault"}},
                 "containers": [container]},
    }
    if init:
        pod["spec"]["initContainers"] = [{**container, "name": "bootstrap"}]
    if finalizer:
        pod["metadata"]["finalizers"] = [_FINALIZER]
    return pod


def test_status_keeps_pending_and_terminating_pod_metadata(
    app_client, test_namespace, tmp_path,
):
    # 只為 status 的真 Helm 查詢建一筆 release；不依賴任何產品 chart 或外部 repo。
    chart = tmp_path / "status-chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: readiness-status\nversion: 0.1.0\n",
    )
    (chart / "templates" / "config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: readiness-status\n",
    )
    subprocess.run([
        os.environ["HELM_BINARY"], "install", "svc-1156", str(chart),
        "--namespace", test_namespace,
    ], check=True, capture_output=True)
    old_uid = None
    try:
        for name, options in (("postgres", {}), ("odoo", {"init": True}),
                              ("old-odoo", {"finalizer": True})):
            manifest = tmp_path / f"{name}.json"
            manifest.write_text(json.dumps(_pod(test_namespace, name, **options)))
            _kubectl("apply", "-f", str(manifest))
            if name == "old-odoo":
                old_uid = json.loads(_kubectl(
                    "get", "pod", name, "-n", test_namespace, "-o", "json",
                ))["metadata"]["uid"]
        for name in ("postgres", "old-odoo"):
            _kubectl("wait", "pod", name, "-n", test_namespace,
                     "--for=condition=Ready", "--timeout=120s")
        _kubectl("wait", "pod", "odoo", "-n", test_namespace,
                 "--for=jsonpath={.status.initContainerStatuses[0].started}=true",
                 "--timeout=120s")
        _kubectl("delete", "pod", "old-odoo", "-n", test_namespace, "--wait=false")
        response = app_client.get(f"/api/releases/{test_namespace}/svc-1156/status")
        assert response.status_code == 200
        body = response.json()
        assert body["pod_retrieval_error"] is None
        pods = {pod["name"]: pod for pod in body["pods"]}
        assert pods["postgres"]["phase"] == "Running"
        assert pods["postgres"]["ready"] == "1/1"
        assert pods["postgres"]["deletion_timestamp"] is None
        assert pods["odoo"]["phase"] == "Pending"
        assert pods["odoo"]["ready"] in ("0/0", "0/1")
        live_old = json.loads(_kubectl(
            "get", "pod", "old-odoo", "-n", test_namespace, "-o", "json",
        ))
        assert pods["old-odoo"]["deletion_timestamp"] == live_old["metadata"]["deletionTimestamp"]
    finally:
        # 只移除本測試加的 finalizer；核對 UID 與清單以免改到替代 Pod／其他
        # finalizer。不鎖整個 resourceVersion，避免 kubelet 終止更新讓清理誤失敗。
        if old_uid is not None:
            old = json.loads(_kubectl("get", "pod", "old-odoo", "-n", test_namespace, "-o", "json"))
            assert old["metadata"]["uid"] == old_uid
            finalizers = old["metadata"].get("finalizers", [])
            if _FINALIZER in finalizers:
                patch_file = tmp_path / "remove-finalizer.json"
                patch_file.write_text(json.dumps([
                    {"op": "test", "path": "/metadata/uid", "value": old_uid},
                    {"op": "test", "path": "/metadata/finalizers", "value": finalizers},
                    {"op": "remove", "path": f"/metadata/finalizers/{finalizers.index(_FINALIZER)}"},
                ]))
                _kubectl("patch", "pod", "old-odoo", "-n", test_namespace,
                         "--type=json", "--patch-file", str(patch_file))
