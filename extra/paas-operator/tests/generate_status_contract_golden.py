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

#: 每一格宣告它**應該**長成的樣子；產生後自檢，沒走到的分支不會變成看似正常的 golden。
CASES = {
    "woow_odoo_chart": lambda body: (
        body["pod_identity_included"] is True
        and body["pod_retrieval_error"] is None
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
    }
    cases = {}
    with patch("src.main.settings.api_key", "golden-1263"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("src.services.helm.KubernetesService._calculate_age",
                  staticmethod(lambda created: "5m")):
        client = TestClient(app, headers={"X-API-Key": "golden-1263"})
        for name in CASES:
            with patch("subprocess.run", side_effect=list(kubectl[name])):
                response = client.get(f"/api/releases/{_NS}/{_RELEASE}/status")
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
