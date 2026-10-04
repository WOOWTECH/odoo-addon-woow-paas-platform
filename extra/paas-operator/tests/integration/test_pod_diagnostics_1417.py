"""#1417：真 K8s 上，status API 對未就緒的 Pod 報出卡住的容器與最近一筆 Warning 事件。

2026-10-04 svc 147 的 Pod 停在 Init（Longhorn 排程額度不足，FailedAttachVolume），平台卻
說成「Running but never became ready」。這裡在測試 namespace 裡重現幾種「沒跑起來」：

- 排不上節點（nodeSelector 指向不存在的節點）→ ``FailedScheduling``；
- PVC 指向不存在的 StorageClass（磁碟永遠綁不上）→ ``FailedScheduling``；
- image 拉不到 → ``waiting``＝ErrImagePull／ImagePullBackOff；
- initContainer 還在跑 → 主容器 ``waiting``＝PodInitializing；
- 對照組：就緒的 Pod 不附任何診斷。

會真的跑起來的 Pod 一律避開 ubuntuserver-59mqj（正式站＋Artifactory、系統碟吃緊）。
"""
import json
import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

_RELEASE = "svc-1417"
_AVOID_NODE = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
    "nodeSelectorTerms": [{"matchExpressions": [{
        "key": "kubernetes.io/hostname", "operator": "NotIn",
        "values": ["ubuntuserver-59mqj"]}]}]}}}


def _kubectl(*args, stdin=None):
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True, input=stdin,
    ).stdout


def _container(name, image="busybox:1.36", command=("sleep", "600")):
    return {
        "name": name, "image": image, "command": list(command),
        "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                      "limits": {"cpu": "100m", "memory": "32Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]}},
    }


def _pod(namespace, name, containers, init=None, volumes=None, node_selector=None):
    spec = {
        "restartPolicy": "Always", "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": 1,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "affinity": _AVOID_NODE,
        "containers": containers,
    }
    if init:
        spec["initContainers"] = init
    if volumes:
        spec["volumes"] = volumes
    if node_selector:
        spec["nodeSelector"] = node_selector
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"app.kubernetes.io/instance": _RELEASE}},
            "spec": spec}


def _install_release(namespace, tmp_path):
    """status 端點要一筆真 Helm release；不依賴任何產品 chart。"""
    chart = tmp_path / "status-chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: diag-1417\nversion: 0.1.0\n")
    (chart / "templates" / "config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: diag-1417\n")
    subprocess.run([os.environ["HELM_BINARY"], "install", _RELEASE, str(chart),
                    "--namespace", namespace], check=True, capture_output=True)


def test_status_reports_why_pods_are_stuck(app_client, test_namespace, tmp_path):
    _install_release(test_namespace, tmp_path)
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
           "metadata": {"name": "data-1417", "namespace": test_namespace},
           "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "no-such-class-1417",
                    "resources": {"requests": {"storage": "1Gi"}}}}
    manifests = [
        pvc,
        _pod(test_namespace, "ok", [_container("main")]),
        _pod(test_namespace, "unschedulable", [_container("main")],
             node_selector={"kubernetes.io/hostname": "no-such-node-1417"}),
        _pod(test_namespace, "unbound-pvc", [{**_container("main"), "volumeMounts": [
            {"name": "data", "mountPath": "/data"}]}],
             volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "data-1417"}}]),
        _pod(test_namespace, "bad-image",
             [_container("main", image="registry.invalid/woow-1417/none:0")]),
        _pod(test_namespace, "init-wait", [_container("main")],
             init=[_container("bootstrap")]),
    ]
    _kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List",
                                                   "items": manifests}))
    _kubectl("wait", "pod", "ok", "-n", test_namespace, "--for=condition=Ready",
             "--timeout=120s")

    def _diagnosed(body):
        pods = {p["name"]: p for p in body["pods"]}
        warning = {n: (p["last_warning"] or {}).get("reason") for n, p in pods.items()}
        waiting = {n: (p["waiting"] or {}).get("reason") for n, p in pods.items()}
        return (warning.get("unschedulable") == "FailedScheduling"
                and warning.get("unbound-pvc") == "FailedScheduling"
                and waiting.get("bad-image") in ("ErrImagePull", "ImagePullBackOff")
                and waiting.get("init-wait") == "PodInitializing")

    deadline = time.time() + 180
    body = None
    while time.time() < deadline:
        response = app_client.get(f"/api/releases/{test_namespace}/{_RELEASE}/status")
        assert response.status_code == 200, response.text
        body = response.json()
        if _diagnosed(body):
            break
        time.sleep(5)
    # 證據：-s 跑時印出整份 body（PR 附件用）。
    print(json.dumps(body, indent=2, ensure_ascii=False))
    assert body["pod_retrieval_error"] is None
    assert _diagnosed(body), json.dumps(body["pods"], indent=2)

    pods = {p["name"]: p for p in body["pods"]}
    assert pods["ok"]["waiting"] is None and pods["ok"]["last_warning"] is None
    assert pods["unschedulable"]["phase"] == "Pending"
    assert "node" in pods["unschedulable"]["last_warning"]["message"].lower()
    assert "persistentvolumeclaim" in pods["unbound-pvc"]["last_warning"]["message"].lower()
    assert pods["bad-image"]["waiting"]["container"] == "main"
    assert pods["bad-image"]["waiting"]["init_container"] is False
    assert pods["init-wait"]["phase"] == "Pending"
