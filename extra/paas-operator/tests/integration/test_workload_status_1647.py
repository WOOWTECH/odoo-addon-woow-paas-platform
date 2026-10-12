"""#1647：真 K8s 上，status API（``?include_workloads=true``）報得出「workload 期望有 Pod、
但被 namespace quota 擋下，一顆都沒有」。

2026-10-10 正式站 omnigent（svc 201）的形狀：``limits.cpu`` 超過 namespace ResourceQuota，
Recreate Deployment 的新 Pod 被拒（ReplicaSet ``FailedCreate: exceeded quota``），剩 0 顆
Pod，而 status 端點的 Pod 清單只是「少一顆」。這裡在測試 namespace 裡重現：

- 很小的 ResourceQuota（``limits.cpu: 200m``）；
- limits 超過 quota 的 Recreate Deployment → 期望 1、實際 0、``ReplicaFailure`` condition；
- 同樣超過的 StatefulSet → 期望 1、實際 0、StatefulSet 身上的 ``FailedCreate`` 事件；
- 對照組：放得下的 Deployment（Pod 建得出來）→ 不附任何診斷。

這支同時驗證兩個只能在真叢集確認的假設（unit test 的 kubectl JSON 是手寫的）：

1. Recreate Deployment 被 quota 擋下時，Deployment 的 status 會帶 ``ReplicaFailure=True``
   （reason ``FailedCreate``、message 含 ``exceeded quota``）；
2. StatefulSet 的 ``FailedCreate`` 事件掛在 **StatefulSet 物件**上（不是 ReplicaSet）。

沒有任何 Pod 會真的跑起來：被擋的沒有 Pod，對照組的 Pod 用不存在的 nodeSelector 停在
Pending——不拉 image、不佔節點。
"""
import json
import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.integration

_RELEASE = "svc-1647"
_LABELS = {"app.kubernetes.io/instance": _RELEASE}
_QUOTA_CPU = "200m"
_OVER_QUOTA_CPU = "500m"
_FITS_CPU = "100m"


def _kubectl(*args, stdin=None):
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True, input=stdin,
    ).stdout


def _pod_template(name, cpu_limit):
    """Pod template：quota 追蹤 ``limits.cpu`` ⇒ 每個容器都必須寫 limits.cpu。"""
    return {
        "metadata": {"labels": {**_LABELS, "app": name}},
        "spec": {
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 1,
            # 排不上任何節點：對照組的 Pod 物件存在（Deployment 的 replicas=1），但不拉
            # image、不佔節點。被擋的兩個根本到不了排程這一步。
            "nodeSelector": {"kubernetes.io/hostname": "no-such-node-1647"},
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{
                "name": "main", "image": "busybox:1.36", "command": ["sleep", "600"],
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                              "limits": {"cpu": cpu_limit, "memory": "32Mi"}},
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]}},
            }],
        },
    }


def _deployment(namespace, name, cpu_limit, strategy):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(_LABELS)},
        "spec": {
            "replicas": 1, "strategy": {"type": strategy},
            "selector": {"matchLabels": {"app": name}},
            "template": _pod_template(name, cpu_limit),
        },
    }


def _statefulset(namespace, name, cpu_limit):
    return {
        "apiVersion": "apps/v1", "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(_LABELS)},
        "spec": {
            "replicas": 1, "serviceName": name,
            "selector": {"matchLabels": {"app": name}},
            "template": _pod_template(name, cpu_limit),
        },
    }


def _install_release(namespace, tmp_path):
    """status 端點要一筆真 Helm release；不依賴任何產品 chart（同 #1417 的做法）。"""
    chart = tmp_path / "status-chart"
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: workload-1647\nversion: 0.1.0\n")
    (chart / "templates" / "config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: workload-1647\n")
    subprocess.run([os.environ["HELM_BINARY"], "install", _RELEASE, str(chart),
                    "--namespace", namespace], check=True, capture_output=True)


def test_status_reports_workloads_blocked_by_quota(app_client, test_namespace, tmp_path):
    _install_release(test_namespace, tmp_path)
    quota = {"apiVersion": "v1", "kind": "ResourceQuota",
             "metadata": {"name": f"{test_namespace}-quota", "namespace": test_namespace},
             "spec": {"hard": {"limits.cpu": _QUOTA_CPU}}}
    _kubectl("apply", "-f", "-", stdin=json.dumps(quota))
    # quota 要先被 controller 算出 status.hard 才會開始攔；沒等到就建 workload，admission
    # 可能還沒生效而讓超量的 Pod 溜進去。
    hard = ""
    deadline = time.time() + 60
    while time.time() < deadline:
        hard = _kubectl("get", "resourcequota", f"{test_namespace}-quota", "-n", test_namespace,
                        "-o", "jsonpath={.status.hard.limits\\.cpu}")
        if hard:
            break
        time.sleep(1)
    assert hard, "ResourceQuota 的 status.hard 一直沒被算出來"

    manifests = [
        # 對照組先建：先佔 100m，後面兩個（各 500m）不論先後都放不下。
        _deployment(test_namespace, "fits", _FITS_CPU, "RollingUpdate"),
        _deployment(test_namespace, "blocked", _OVER_QUOTA_CPU, "Recreate"),
        _statefulset(test_namespace, "blocked-sts", _OVER_QUOTA_CPU),
    ]
    _kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List",
                                                   "items": manifests}))

    def _by_name(body):
        return {w["name"]: w for w in body.get("workloads") or []}

    def _reported(body):
        workloads = _by_name(body)
        deploy = workloads.get("blocked") or {}
        sts = workloads.get("blocked-sts") or {}
        fits = workloads.get("fits") or {}
        return ((deploy.get("replica_failure") or {}).get("reason") == "FailedCreate"
                and (sts.get("last_warning") or {}).get("reason") == "FailedCreate"
                and fits.get("replicas") == 1)

    deadline = time.time() + 180
    body = None
    while time.time() < deadline:
        response = app_client.get(f"/api/releases/{test_namespace}/{_RELEASE}/status",
                                  params={"include_workloads": "true"})
        assert response.status_code == 200, response.text
        body = response.json()
        if _reported(body):
            break
        time.sleep(5)
    # 證據：-s 跑時印出整份 body（PR 附件用）。
    print(json.dumps(body, indent=2, ensure_ascii=False))
    assert body["workloads_included"] is True
    assert body["workload_retrieval_error"] is None
    assert _reported(body), json.dumps(body["workloads"], indent=2)

    workloads = _by_name(body)
    assert set(workloads) == {"blocked", "blocked-sts", "fits"}

    # 假設 1：Recreate Deployment 被 quota 擋下 ⇒ ReplicaFailure condition。
    deploy = workloads["blocked"]
    assert (deploy["kind"], deploy["desired_replicas"], deploy["replicas"],
            deploy["strategy"]) == ("Deployment", 1, 0, "Recreate")
    assert deploy["replica_failure"]["reason"] == "FailedCreate"
    assert "exceeded quota" in deploy["replica_failure"]["message"]
    assert f"{test_namespace}-quota" in deploy["replica_failure"]["message"]
    # ReplicaSet 的 FailedCreate 事件對回它的 Deployment（名字去掉最後一段 hash）。
    assert (deploy["last_warning"] or {}).get("reason") == "FailedCreate"
    assert "exceeded quota" in deploy["last_warning"]["message"]

    # 假設 2：StatefulSet 沒有 ReplicaFailure condition，證據是掛在 StatefulSet 上的事件。
    sts = workloads["blocked-sts"]
    assert (sts["kind"], sts["desired_replicas"], sts["replicas"]) == ("StatefulSet", 1, 0)
    assert sts["last_warning"]["reason"] == "FailedCreate"
    assert "exceeded quota" in sts["last_warning"]["message"]

    # 對照組：Pod 物件建得出來（停在 Pending）⇒ 副本充足，不附任何診斷。
    fits = workloads["fits"]
    assert (fits["kind"], fits["desired_replicas"], fits["replicas"]) == ("Deployment", 1, 1)
    assert fits["replica_failure"] is None and fits["last_warning"] is None

    # Pod 清單只看得到對照組那顆：被擋的 Pod 根本沒有物件——這正是需要 workloads 的理由。
    pod_names = [p["name"] for p in body["pods"]]
    assert len(pod_names) == 1 and pod_names[0].startswith("fits-"), pod_names


def test_status_without_flag_is_unchanged(app_client, test_namespace, tmp_path):
    """不帶旗標：workload 欄位是「沒查」（None／False），既有欄位照舊。"""
    _install_release(test_namespace, tmp_path)
    _kubectl("apply", "-f", "-", stdin=json.dumps(
        _deployment(test_namespace, "fits", _FITS_CPU, "RollingUpdate")))

    response = app_client.get(f"/api/releases/{test_namespace}/{_RELEASE}/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workloads_included"] is False
    assert body["workloads"] is None, "沒查是 None，不是 []"
    assert body["workload_retrieval_error"] is None
    assert body["release"]["name"] == _RELEASE
    assert body["pod_identity_included"] is True
    assert body["pod_retrieval_error"] is None
    assert body["vm_included"] is False and body["vmis"] is None

    # 同一個 release 帶旗標就查得到——證明上面的 None 來自旗標，不是因為沒有 workload。
    flagged = app_client.get(f"/api/releases/{test_namespace}/{_RELEASE}/status",
                             params={"include_workloads": "true"}).json()
    assert flagged["workloads_included"] is True
    assert [w["name"] for w in flagged["workloads"]] == ["fits"]
