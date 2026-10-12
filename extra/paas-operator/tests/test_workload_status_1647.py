"""#1647：workload（Deployment／StatefulSet）的副本數與「建不出 Pod」的證據穿過 status API。

2026-10-10 正式站 omnigent（svc 201）：server 與同 namespace 內 PG 的 CPU limit 被調成合計
16，namespace 的 ``limits.cpu`` 上限是 12。Deployment 是 ``Recreate``——舊 Pod 先被砍，新
Pod 被 quota 拒絕（ReplicaSet ``FailedCreate: exceeded quota``），剩 0 顆 Pod。平台紀錄
一直是 ``running``：status 端點只回 Pod 清單，而被擋的 Pod **根本沒有物件**，清單只是少一顆，
事件查詢也只看 ``involvedObject.kind=Pod``。平台要發現，operator 就得把 controller 這一層
報上來：

- ``?include_workloads=true`` 才查（既有呼叫端不多付 kubectl）；
- ``WorkloadInfo``：``desired_replicas``／``replicas``／``ready_replicas``、
  ``replica_failure``（``ReplicaFailure=True`` condition）；
- ``WorkloadInfo.last_warning``：namespace 的 Warning 事件**一次 list**，依 involvedObject
  （Deployment／ReplicaSet／StatefulSet）對到**副本不足**的 workload；查詢失敗不能讓端點失敗；
- workload 查詢本身失敗：仍回 200，``workloads: null`` ＋ ``workload_retrieval_error``。

只替換 subprocess 邊界，讓真的 ``get_workloads``／``get_workload_warning_events`` 解析
kubectl JSON。
"""
import json
import logging
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import KubernetesService

_NS = "paas-ws-test-1647"
_SELECTOR = "app.kubernetes.io/instance=svc"
_QUOTA_MESSAGE = (
    'pods "svc-7d9c8b6f5-" is forbidden: exceeded quota: paas-ws-test-1647-quota, '
    "requested: limits.cpu=8, used: limits.cpu=8, limited: limits.cpu=12"
)


def _deployment(name, desired=1, strategy="Recreate", **status):
    spec = {"strategy": {"type": strategy}}
    if desired is not None:
        spec["replicas"] = desired
    return {"kind": "Deployment", "metadata": {"name": name, "namespace": _NS},
            "spec": spec, "status": status}


def _statefulset(name, desired=1, **status):
    return {"kind": "StatefulSet", "metadata": {"name": name, "namespace": _NS},
            "spec": {"replicas": desired, "updateStrategy": {"type": "RollingUpdate"}},
            "status": status}


def _replica_failure_condition(message=_QUOTA_MESSAGE):
    return {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
            "message": message, "lastTransitionTime": "2026-10-10T18:07:45Z"}


def _event(kind, name, reason, message, last, **extra):
    return {"type": "Warning", "reason": reason, "message": message,
            "lastTimestamp": last,
            "involvedObject": {"kind": kind, "name": name, "namespace": _NS}, **extra}


def _ok(items):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": items}), stderr="")


def _healthy_pod(name="svc-db-0"):
    return {
        "metadata": {"name": name, "uid": "uid-%s" % name, "namespace": _NS},
        "status": {"phase": "Running", "containerStatuses": [
            {"name": "app", "ready": True, "restartCount": 0, "state": {"running": {}}}]},
    }


#: 被 quota 擋的 Recreate Deployment：status 沒有 replicas（0 的計數整個 key 不出現）。
_BLOCKED = _deployment("svc", unavailableReplicas=1,
                       conditions=[_replica_failure_condition()])
_HEALTHY_STS = _statefulset("svc-db", replicas=1, readyReplicas=1, updatedReplicas=1)


def _get_status(side_effect, params=None):
    release = ReleaseInfo(
        name="svc", namespace=_NS, revision=1, status=ReleaseStatus.DEPLOYED,
        chart="omnigent", app_version="0.17", updated="2026-10-10T18:00:00Z",
    )
    with patch("src.main.settings.api_key", "test-1647"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("subprocess.run", side_effect=side_effect) as run:
        response = TestClient(app, headers={"X-API-Key": "test-1647"}).get(
            f"/api/releases/{_NS}/svc/status", params=params)
    assert response.status_code == 200, response.text
    return response.json(), run


# --------------------------------------------------------------------------- #
#  opt-in：沒帶旗標就不查                                                        #
# --------------------------------------------------------------------------- #

def test_workloads_are_not_queried_unless_asked():
    """既有呼叫端（每分鐘的背景 poll）不多付一次 kubectl，回應是「沒查」不是「沒有」。"""
    body, run = _get_status([_ok([_healthy_pod()])])
    assert run.call_count == 1, "不帶 include_workloads 時 subprocess 呼叫數必須與以前相同"
    assert run.call_args_list[0].args[0][1:3] == ["get", "pods"]
    assert body["workloads"] is None
    assert body["workloads_included"] is False
    assert body["workload_retrieval_error"] is None


# --------------------------------------------------------------------------- #
#  副本不足：ReplicaFailure condition 與 controller 事件                         #
# --------------------------------------------------------------------------- #

def test_deployment_blocked_by_quota_reports_replica_failure():
    events = [_event("ReplicaSet", "svc-7d9c8b6f5", "FailedCreate",
                     "Error creating: " + _QUOTA_MESSAGE, "2026-10-10T23:31:12Z", count=23)]
    body, run = _get_status([_ok([_healthy_pod()]), _ok([_BLOCKED, _HEALTHY_STS]), _ok(events)],
                            params={"include_workloads": "true"})

    workload_args = run.call_args_list[1].args[0]
    assert workload_args[1:3] == ["get", "deployments,statefulsets"]
    assert _NS in workload_args and _SELECTOR in workload_args

    assert body["workloads_included"] is True
    assert body["workload_retrieval_error"] is None
    blocked = next(w for w in body["workloads"] if w["name"] == "svc")
    assert (blocked["kind"], blocked["desired_replicas"], blocked["replicas"],
            blocked["ready_replicas"], blocked["updated_replicas"], blocked["strategy"]) == (
        "Deployment", 1, 0, 0, 0, "Recreate")
    assert blocked["replica_failure"] == {
        "reason": "FailedCreate", "message": _QUOTA_MESSAGE,
        "last_transition": "2026-10-10T18:07:45Z"}
    assert (blocked["last_warning"]["reason"], blocked["last_warning"]["count"],
            blocked["last_warning"]["last_seen"]) == ("FailedCreate", 23, "2026-10-10T23:31:12Z")
    assert "exceeded quota" in blocked["last_warning"]["message"]
    # Pod 清單照常：被擋的 Pod 沒有物件，所以只看得到 PG 那顆。
    assert [p["name"] for p in body["pods"]] == ["svc-db-0"]


def test_statefulset_failed_create_event_is_attached():
    """StatefulSet controller 直接建 Pod：沒有 ReplicaFailure condition，證據只在事件裡。"""
    blocked_sts = _statefulset("svc-db")
    events = [
        _event("StatefulSet", "svc-db", "FailedCreate", "create Pod svc-db-0 failed: old",
               "2026-10-10T18:08:00Z"),
        # 較新那筆勝出。
        _event("StatefulSet", "svc-db", "FailedCreate",
               "create Pod svc-db-0 in StatefulSet svc-db failed error: " + _QUOTA_MESSAGE,
               "2026-10-10T23:30:00Z", count=40),
    ]
    with patch("subprocess.run", side_effect=[_ok([blocked_sts]), _ok(events)]) as run:
        workloads = KubernetesService().get_workloads(_NS, _SELECTOR)

    event_args = run.call_args_list[1].args[0]
    assert event_args[1:4] == ["get", "events", "--namespace"] and _NS in event_args
    assert "type=Warning" in event_args, "三種 kind 一次查完，不加 involvedObject.kind"
    assert run.call_args_list[1].kwargs["timeout"] == KubernetesService.POD_EVENTS_TIMEOUT_SECONDS

    sts = workloads[0]
    assert (sts.kind, sts.desired_replicas, sts.replicas, sts.strategy) == (
        "StatefulSet", 1, 0, "RollingUpdate")
    assert sts.replica_failure is None
    assert (sts.last_warning.reason, sts.last_warning.count) == ("FailedCreate", 40)
    assert "exceeded quota" in sts.last_warning.message


def test_replicaset_event_maps_to_its_own_deployment():
    """ReplicaSet 名字是 ``<deployment>-<hash>``：只去掉最後一段。``svc-db-<hash>`` 的事件
    屬於 ``svc-db``，不會落到名字是它前綴的 ``svc``。"""
    workloads = [_deployment("svc"), _deployment("svc-db")]
    events = [_event("ReplicaSet", "svc-db-5c7e6b8a4", "FailedCreate", "db blocked",
                     "2026-10-10T23:00:00Z")]
    with patch("subprocess.run", side_effect=[_ok(workloads), _ok(events)]):
        by_name = {w.name: w for w in KubernetesService().get_workloads(_NS, _SELECTOR)}
    assert by_name["svc-db"].last_warning.message == "db blocked"
    assert by_name["svc"].last_warning is None

    # Deployment 自己身上的事件也認；同一個 workload 取最後一筆。
    events = [
        _event("ReplicaSet", "svc-7d9c8b6f5", "FailedCreate", "rs", "2026-10-10T23:00:00Z"),
        _event("Deployment", "svc", "ReplicaSetCreateError", "deploy", "2026-10-10T23:05:00Z"),
    ]
    with patch("subprocess.run", side_effect=[_ok(workloads), _ok(events)]):
        by_name = {w.name: w for w in KubernetesService().get_workloads(_NS, _SELECTOR)}
    assert by_name["svc"].last_warning.reason == "ReplicaSetCreateError"
    assert by_name["svc-db"].last_warning is None


# --------------------------------------------------------------------------- #
#  副本充足：不查事件                                                            #
# --------------------------------------------------------------------------- #

def test_healthy_workloads_skip_the_event_query():
    healthy = [_deployment("svc", strategy="RollingUpdate", replicas=1, readyReplicas=1,
                           updatedReplicas=1), _HEALTHY_STS]
    with patch("subprocess.run", side_effect=[_ok(healthy)]) as run:
        workloads = KubernetesService().get_workloads(_NS, _SELECTOR)
    assert run.call_count == 1, "全部充足時不該多查一次事件"
    assert [(w.kind, w.name, w.replicas, w.ready_replicas) for w in workloads] == [
        ("Deployment", "svc", 1, 1), ("StatefulSet", "svc-db", 1, 1)]
    assert all(w.replica_failure is None and w.last_warning is None for w in workloads)


def test_scaled_to_zero_is_not_short():
    """停權（scale 到 0）是期望 0、實際 0：不是副本不足，不查事件。"""
    with patch("subprocess.run", side_effect=[_ok([_deployment("svc", desired=0)])]) as run:
        workloads = KubernetesService().get_workloads(_NS, _SELECTOR)
    assert run.call_count == 1
    assert (workloads[0].desired_replicas, workloads[0].replicas) == (0, 0)
    assert workloads[0].last_warning is None


def test_missing_spec_replicas_defaults_to_one():
    """``spec.replicas`` 缺值＝API server 的預設 1（不是 0）——否則被擋的 workload 會被當成停權。"""
    with patch("subprocess.run", side_effect=[_ok([_deployment("svc", desired=None)]), _ok([])]):
        workloads = KubernetesService().get_workloads(_NS, _SELECTOR)
    assert (workloads[0].desired_replicas, workloads[0].replicas) == (1, 0)


# --------------------------------------------------------------------------- #
#  失敗模式                                                                      #
# --------------------------------------------------------------------------- #

def test_event_lookup_failure_is_swallowed(caplog):
    forbidden = subprocess.CompletedProcess([], 1, stdout="", stderr="events is forbidden")
    timeout = subprocess.TimeoutExpired(cmd="kubectl", timeout=10)
    garbage = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
    for failure in (forbidden, timeout, garbage):
        with caplog.at_level(logging.WARNING, logger="src.services.helm"), \
                patch("subprocess.run", side_effect=[_ok([_BLOCKED]), failure]):
            workloads = KubernetesService().get_workloads(_NS, _SELECTOR)
        assert workloads[0].last_warning is None
        assert workloads[0].replica_failure.reason == "FailedCreate", (
            "replica_failure 來自 workload 本身，不受事件查詢影響")
    assert any("workload warning events" in r.getMessage() for r in caplog.records)


def test_workload_lookup_failure_keeps_pods_and_reports_error():
    forbidden = subprocess.CompletedProcess([], 1, stdout="", stderr="deployments is forbidden")
    body, run = _get_status([_ok([_healthy_pod()]), forbidden],
                            params={"include_workloads": "true"})
    assert run.call_count == 2
    assert body["workloads_included"] is True, "旗標是 request 的 echo，與這次查詢成敗無關"
    assert body["workloads"] is None, "查詢失敗不是「沒有 workload」（[]）"
    assert body["workload_retrieval_error"]
    assert body["pod_retrieval_error"] is None
    assert [p["name"] for p in body["pods"]] == ["svc-db-0"]
    assert body["release"]["status"] == "deployed"


def test_messages_are_truncated():
    limit = KubernetesService.WORKLOAD_DIAGNOSTIC_TEXT_LIMIT
    assert limit == 500
    blocked = _deployment("svc", conditions=[_replica_failure_condition("x" * 2000)])
    events = [_event("ReplicaSet", "svc-7d9c8b6f5", "FailedCreate", "y" * 2000,
                     "2026-10-10T23:31:12Z")]
    with patch("subprocess.run", side_effect=[_ok([blocked]), _ok(events)]):
        workload = KubernetesService().get_workloads(_NS, _SELECTOR)[0]
    assert len(workload.replica_failure.message) == limit
    assert workload.replica_failure.message.endswith("…")
    assert len(workload.last_warning.message) == limit
    assert workload.last_warning.message.endswith("…")


def test_other_kinds_are_ignored():
    """kubectl 的 list 與事件裡混進別種 kind 時略過：workload 只認 Deployment／StatefulSet，
    Pod／PVC 的事件由 ``get_pod_warning_events`` 管，不得被當成 workload 的證據。"""
    items = [
        {"kind": "DaemonSet", "metadata": {"name": "svc-agent"}, "spec": {}, "status": {}},
        _deployment("svc"),
        {"kind": "ReplicaSet", "metadata": {"name": "svc-7d9c8b6f5"},
         "spec": {"replicas": 1}, "status": {}},
    ]
    events = [
        _event("Pod", "svc", "FailedScheduling", "pod event", "2026-10-10T23:40:00Z"),
        _event("PersistentVolumeClaim", "svc", "ProvisioningFailed", "pvc event",
               "2026-10-10T23:41:00Z"),
        # 沒有 reason 的事件跳過。
        {"type": "Warning", "message": "no reason", "lastTimestamp": "2026-10-10T23:42:00Z",
         "involvedObject": {"kind": "Deployment", "name": "svc"}},
    ]
    with patch("subprocess.run", side_effect=[_ok(items), _ok(events)]):
        workloads = KubernetesService().get_workloads(_NS, _SELECTOR)
    assert [(w.kind, w.name) for w in workloads] == [("Deployment", "svc")]
    assert workloads[0].last_warning is None


def test_non_true_replica_failure_condition_is_not_reported():
    """``ReplicaFailure`` 只有 ``status == "True"`` 才算；恢復後 controller 會把它拿掉或設 False。"""
    recovered = _deployment("svc", replicas=1, readyReplicas=1, updatedReplicas=1, conditions=[
        {"type": "ReplicaFailure", "status": "False", "reason": "FailedCreate", "message": "old"},
        {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"},
    ])
    with patch("subprocess.run", side_effect=[_ok([recovered])]) as run:
        workloads = KubernetesService().get_workloads(_NS, _SELECTOR)
    assert run.call_count == 1
    assert workloads[0].replica_failure is None
