"""產生 ``GET /api/releases/{ns}/{name}/status`` 的 wire 契約 golden——**打真的端點**。

## 為什麼要這支（#1263／#1251 Q44）

#1251 的 readiness 判定靠 ``PodInfo`` 的 ``component``／``task``／``owner_kind`` 與
``pod_identity_included``。這些值由 ``KubernetesService.get_pods`` 從 kubectl JSON
抽出來；平台端測試若自己手寫 Pod dict，釘住的只是作者的信念——label key 打錯、
owner 取錯筆，平台測試照樣全綠而 prod 判定全錯。

## 判準：以**真的 HTTP body** 為權威

``TestClient`` 打 ``GET …/status``，只換掉兩個邊界：

- ``helm_service.get``（release 本體不是本契約的重點）；
- ``subprocess.run``（回傳 kubectl ``get pods -o json``，Pod 的 labels／ownerReferences
  照 woow-odoo chart ``758cdf6c``（0.2.2）的 template 寫）。

另外 stub ``_calculate_age``：``age`` 是隨 wall clock 變的人讀顯示值，不 stub 的話
golden 每跑一次就漂；其餘全部走真碼。

## 重現

    cd extra/paas-operator
    .venv/bin/python tests/generate_status_contract_golden.py

輸出寫到 ``src/tests/fixtures/status_contract_golden.json``（**Odoo 側**：``extra/`` 不會
被打進 addon）。operator 端由 ``tests/test_status_contract_golden_1263.py`` 重跑
``build_cases()`` 斷言 == golden；平台端 factory 的 key 集合由
``scripts/audit/test_operator_contract_shape.py`` 對到 golden。
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from src.main import app  # noqa: E402
from src.models.schemas import ReleaseInfo, ReleaseStatus  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT = REPO_ROOT / "src" / "tests" / "fixtures" / "status_contract_golden.json"

_NS = "paas-ws-test-1263"
_RELEASE = "svc-1263"
_INSTANCE = {"app.kubernetes.io/instance": _RELEASE, "app.kubernetes.io/name": "odoo"}
_CREATED = "2026-09-29T01:00:00Z"


def _owner(kind, controller=True):
    return {"apiVersion": "apps/v1", "kind": kind, "name": f"{_RELEASE}-{kind.lower()}",
            "uid": f"uid-{kind.lower()}", "controller": controller,
            "blockOwnerDeletion": True}


def _pod(name, component, owner, phase="Running", ready=(1, 1), task=None, **meta):
    labels = {**_INSTANCE, "app.kubernetes.io/component": component}
    if task:
        labels["woow.paas/task"] = task
    ready_count, total = ready
    return {
        "metadata": {
            "name": name, "namespace": _NS, "labels": labels,
            "ownerReferences": [owner], "creationTimestamp": _CREATED, **meta,
        },
        "status": {
            "phase": phase,
            "containerStatuses": [
                {"ready": i < ready_count, "restartCount": 0} for i in range(total)
            ],
        },
    }


#: woow-odoo chart 一個 release 會出現的 Pod（label／owner 照 chart template）。
_WOOW_ODOO_PODS = [
    # deployment.yaml：component=server，odoo＋nginx 兩個容器，owner＝ReplicaSet。
    _pod("svc-1263-6d8f7c9b5-server", "server", _owner("ReplicaSet"), ready=(2, 2)),
    # Recreate 收尾中的舊 server（#1156：phase 仍 Running，靠 deletion_timestamp 辨識）。
    _pod("svc-1263-5c7e6b8a4-old", "server", _owner("ReplicaSet"), ready=(0, 2),
         deletionTimestamp="2026-09-29T01:05:00Z"),
    # cron-deployment.yaml（platform profile）：component=cron。
    _pod("svc-1263-cron-7f9d8c6b5-x", "cron", _owner("ReplicaSet")),
    # postgres.yaml：component=postgresql，owner＝StatefulSet。
    _pod("svc-1263-postgresql-0", "postgresql", _owner("StatefulSet")),
    # backup-cronjob.yaml：component=db-backup＋woow.paas/task，owner＝Job。
    _pod("svc-1263-backup-29000000-abcde", "db-backup", _owner("Job"),
         phase="Succeeded", ready=(0, 1), task="backup"),
]

#: #1417：10-04 事件的形狀——Pod 卡在 Init（磁碟掛不上）、另一顆反覆重啟、一顆就緒。
#: 事件由同一個端點的第二次 kubectl（``get events``）回來；就緒那顆的舊事件不得被附上，
#: 同名但不同 UID（前一顆 Pod）的事件也不得被附上。
_DIAG_NS_EVENTS_UID = {"stuck": "uid-stuck-0", "crash": "uid-crash-0", "ok": "uid-ok-0"}


def _diag_pod(name, uid, phase, init_statuses, statuses):
    return {
        "metadata": {
            "name": name, "namespace": _NS, "uid": uid, "creationTimestamp": _CREATED,
            "labels": {"app.kubernetes.io/instance": _RELEASE},
            "ownerReferences": [_owner("StatefulSet")],
        },
        "spec": {"containers": [{"name": "app", "image": "example/app:1"}]},
        "status": {"phase": phase, "initContainerStatuses": init_statuses,
                   "containerStatuses": statuses},
    }


def _cs(name, ready=False, waiting=None, running=False, restarts=0):
    state = {"waiting": waiting} if waiting else ({"running": {}} if running else {})
    return {"name": name, "ready": ready, "restartCount": restarts, "state": state}


_DIAG_PODS = [
    # Init:0/1、主容器 PodInitializing（2026-10-04 svc 147 Nextcloud 的實際形狀）。
    _diag_pod("svc-1263-stuck-0", _DIAG_NS_EVENTS_UID["stuck"], "Pending",
              [_cs("volume-permissions", waiting={"reason": "PodInitializing"})],
              [_cs("app", waiting={"reason": "PodInitializing"})]),
    # Running 但主容器反覆重啟：實質 reason（CrashLoopBackOff）排在 sidecar 的 generic 之前。
    _diag_pod("svc-1263-crash-0", _DIAG_NS_EVENTS_UID["crash"], "Running", [],
              [_cs("sidecar", waiting={"reason": "ContainerCreating"}),
               _cs("app", waiting={"reason": "CrashLoopBackOff",
                                   "message": "back-off 5m0s restarting failed container=app"},
                   restarts=7)]),
    _diag_pod("svc-1263-ok-0", _DIAG_NS_EVENTS_UID["ok"], "Running", [],
              [_cs("app", ready=True, running=True)]),
]


def _event(uid, name, reason, message, last, count=1):
    return {"type": "Warning", "reason": reason, "message": message, "count": count,
            "lastTimestamp": last,
            "involvedObject": {"kind": "Pod", "name": name, "uid": uid, "namespace": _NS}}


_DIAG_EVENTS = [
    _event("uid-stuck-0", "svc-1263-stuck-0", "FailedScheduling",
           "0/4 nodes are available: pod has unbound immediate PersistentVolumeClaims.",
           "2026-10-04T01:17:30Z"),
    # 較新那筆勝出；訊息照 2026-10-04 的事件原文（Longhorn 排程額度不足）。
    _event("uid-stuck-0", "svc-1263-stuck-0", "FailedAttachVolume",
           'AttachVolume.Attach failed for volume "pvc-0a1b" : rpc error: code = Aborted '
           'desc = volume pvc-0a1b is not ready for workloads',
           "2026-10-04T01:46:02Z", count=12),
    _event("uid-crash-0", "svc-1263-crash-0", "BackOff",
           "Back-off restarting failed container app in pod svc-1263-crash-0",
           "2026-10-04T01:40:00Z", count=30),
    # 就緒的 Pod 的舊事件：不附。
    _event("uid-ok-0", "svc-1263-ok-0", "Unhealthy", "Readiness probe failed: 503",
           "2026-10-04T01:00:00Z"),
    # 同名、前一顆（不同 UID）的事件：不附。
    _event("uid-stuck-old", "svc-1263-stuck-0", "FailedMount", "old pod",
           "2026-10-04T01:59:00Z"),
]

#: #1647：2026-10-10 事故的形狀——服務顯示 running，server 的 Recreate Deployment 被
#: namespace quota 擋下（ReplicaSet ``FailedCreate: exceeded quota``），一顆 Pod 都沒有；
#: 同 release 的 PG StatefulSet 照常。Pod 清單裡只剩 PG 那顆（而且健康），看不出「server
#: 本來該有」——所以要有 workloads。
_QUOTA_MESSAGE = (
    'pods "svc-1263-server-7d9c8b6f5-" is forbidden: exceeded quota: '
    "paas-ws-test-1263-quota, requested: limits.cpu=8, used: limits.cpu=8, "
    "limited: limits.cpu=12"
)
_BLOCKED_WORKLOADS = [
    {
        "kind": "Deployment",
        "metadata": {"name": "svc-1263-server", "namespace": _NS, "labels": dict(_INSTANCE)},
        "spec": {"replicas": 1, "strategy": {"type": "Recreate"}},
        # 被擋時 status 沒有 replicas／readyReplicas（0 的計數整個 key 不出現）。
        "status": {
            "observedGeneration": 4,
            "unavailableReplicas": 1,
            "conditions": [
                {"type": "Available", "status": "False", "reason": "MinimumReplicasUnavailable",
                 "lastTransitionTime": "2026-10-10T18:07:44Z"},
                {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
                 "message": _QUOTA_MESSAGE, "lastTransitionTime": "2026-10-10T18:07:45Z"},
            ],
        },
    },
    {
        "kind": "StatefulSet",
        "metadata": {"name": "svc-1263-postgresql", "namespace": _NS,
                     "labels": dict(_INSTANCE)},
        "spec": {"replicas": 1, "updateStrategy": {"type": "RollingUpdate"}},
        "status": {"replicas": 1, "readyReplicas": 1, "updatedReplicas": 1},
    },
]
#: quota 的 FailedCreate 掛在 ReplicaSet（``<deployment>-<pod-template-hash>``）上。
_BLOCKED_EVENTS = [
    {"type": "Warning", "reason": "FailedCreate", "count": 23,
     "message": "Error creating: " + _QUOTA_MESSAGE,
     "lastTimestamp": "2026-10-10T23:31:12Z",
     "involvedObject": {"kind": "ReplicaSet", "name": "svc-1263-server-7d9c8b6f5",
                        "namespace": _NS, "uid": "uid-rs-server"}},
]

#: 逐情形的 query string（沒列的情形不帶參數，與既有呼叫端相同）。
_PARAMS = {"workload_create_blocked": {"include_workloads": "true"}}


def _workload(body, kind):
    return next(w for w in body["workloads"] if w["kind"] == kind)


#: 每一格宣告它**應該**長成的樣子；產生後自檢，沒走到的分支不會變成看似正常的 golden。
CASES = {
    "woow_odoo_chart": lambda body: (
        body["pod_identity_included"] is True
        and body["pod_retrieval_error"] is None
        # #1647：沒帶 include_workloads ⇒ 沒查（None，不是 []），旗標 echo 為 False。
        and body["workloads"] is None and body["workloads_included"] is False
        and {p["name"]: (p["component"], p["task"], p["owner_kind"]) for p in body["pods"]} == {
            "svc-1263-6d8f7c9b5-server": ("server", None, "ReplicaSet"),
            "svc-1263-5c7e6b8a4-old": ("server", None, "ReplicaSet"),
            "svc-1263-cron-7f9d8c6b5-x": ("cron", None, "ReplicaSet"),
            "svc-1263-postgresql-0": ("postgresql", None, "StatefulSet"),
            "svc-1263-backup-29000000-abcde": ("db-backup", "backup", "Job"),
        }
        and all(p["creation_timestamp"] == _CREATED for p in body["pods"])
    ),
    # 抓 Pod 失敗：pods 為空、錯誤另外說，旗標仍為 True（能力宣告，與這次成敗無關）。
    "pod_retrieval_failed": lambda body: (
        body["pods"] == [] and bool(body["pod_retrieval_error"])
        and body["pod_identity_included"] is True
    ),
    # #1417：未就緒 Pod 的診斷（waiting 容器＋最近一筆 Warning 事件）。
    "unready_pod_diagnostics": lambda body: (
        {p["name"]: ((p["waiting"] or {}).get("reason"), (p["waiting"] or {}).get("init_container"),
                     (p["last_warning"] or {}).get("reason")) for p in body["pods"]} == {
            "svc-1263-stuck-0": ("PodInitializing", True, "FailedAttachVolume"),
            "svc-1263-crash-0": ("CrashLoopBackOff", False, "BackOff"),
            "svc-1263-ok-0": (None, None, None),
        }
    ),
    # #1647：帶 include_workloads——被 quota 擋的 Deployment 有 ReplicaFailure 與 ReplicaSet
    # 的 FailedCreate 事件；健康的 StatefulSet 兩者皆 None（充足的 workload 不附事件）。
    "workload_create_blocked": lambda body: (
        body["workloads_included"] is True
        and body["workload_retrieval_error"] is None
        and (_workload(body, "Deployment")["desired_replicas"],
             _workload(body, "Deployment")["replicas"]) == (1, 0)
        and _workload(body, "Deployment")["replica_failure"]["reason"] == "FailedCreate"
        and "exceeded quota" in _workload(body, "Deployment")["replica_failure"]["message"]
        and _workload(body, "Deployment")["last_warning"]["reason"] == "FailedCreate"
        and _workload(body, "StatefulSet")["replica_failure"] is None
        and _workload(body, "StatefulSet")["last_warning"] is None
    ),
}


def _kubectl(returncode, items=None):
    stdout = json.dumps({"items": items}) if items is not None else ""
    return subprocess.CompletedProcess([], returncode, stdout=stdout,
                                       stderr="" if returncode == 0 else "forbidden")


def build_cases():
    """打真的端點，回傳 {情形: HTTP body}。golden 與 operator 端測試共用這一支。"""
    release = ReleaseInfo(
        name=_RELEASE, namespace=_NS, revision=3, status=ReleaseStatus.DEPLOYED,
        chart="woow-odoo", chart_version="0.2.2", app_version="18.0",
        updated="2026-09-29T01:00:00Z",
    )
    # 每格是依序回給 subprocess.run 的 kubectl 結果：先 get pods，有未就緒的 Pod 時再
    # get events（#1417）。全部就緒的格子只會有第一次呼叫。
    kubectl = {
        "woow_odoo_chart": [_kubectl(0, _WOOW_ODOO_PODS)],
        "pod_retrieval_failed": [_kubectl(1)],
        "unready_pod_diagnostics": [_kubectl(0, _DIAG_PODS), _kubectl(0, _DIAG_EVENTS)],
        # #1647：get pods（只剩健康的 PG，不觸發 Pod 事件查詢）→ get deployments,statefulsets
        # → 有副本不足的 workload，再 get events。
        "workload_create_blocked": [
            _kubectl(0, [_pod("svc-1263-postgresql-0", "postgresql", _owner("StatefulSet"))]),
            _kubectl(0, _BLOCKED_WORKLOADS),
            _kubectl(0, _BLOCKED_EVENTS),
        ],
    }
    cases = {}
    with patch("src.main.settings.api_key", "golden-1263"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("src.services.helm.KubernetesService._calculate_age",
                  staticmethod(lambda created: "5m")):
        client = TestClient(app, headers={"X-API-Key": "golden-1263"})
        for name in CASES:
            with patch("subprocess.run", side_effect=list(kubectl[name])):
                response = client.get(f"/api/releases/{_NS}/{_RELEASE}/status",
                                      params=_PARAMS.get(name))
            assert response.status_code == 200, (name, response.text)
            cases[name] = response.json()
    return cases


def _fingerprint():
    """從 repo-lint 取契約指紋（單一權威，同 route golden 的理由）。"""
    import importlib.util
    lint_path = REPO_ROOT / "scripts" / "audit" / "repo-lint.py"
    spec = importlib.util.spec_from_file_location("_repo_lint", lint_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fp, err = mod.status_contract_fingerprint(str(REPO_ROOT))
    if err:
        raise SystemExit("無法計算契約指紋：%s" % err)
    return fp


def main():
    cases = build_cases()
    bad = [name for name, ok in CASES.items() if not ok(cases[name])]
    if bad:
        raise SystemExit(
            "這幾格沒長成宣稱的樣子（分支沒被走到？）：%s\n%s"
            % (bad, json.dumps({k: cases[k] for k in bad}, indent=2, ensure_ascii=False)))
    cases["_contract_fingerprint"] = _fingerprint()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    print("== 寫出 golden:", OUT)


if __name__ == "__main__":
    main()
