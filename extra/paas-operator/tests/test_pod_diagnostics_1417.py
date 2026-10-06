"""#1417：未就緒 Pod 的診斷（卡住的容器＋最近一筆 Warning 事件）穿過 status API。

2026-10-04 正式站 svc 147（Nextcloud）：Pod 從頭到尾停在 ``Init:0/1``（phase Pending），
事件是 ``FailedAttachVolume … not ready for workloads``（Longhorn 排程額度不足），平台的
逾時訊息卻說「pods are Running but never became ready — check container logs」。平台要能
分辨，operator 就得把「卡在哪」報上來：

- ``PodInfo.waiting``：先看 initContainerStatuses 再看 containerStatuses，實質 reason
  （ImagePullBackOff、CrashLoopBackOff…）優先於「還沒輪到」的 PodInitializing／
  ContainerCreating；
- ``PodInfo.last_warning``：namespace 的 Warning 事件**一次 list**、依 involvedObject 對到
  **未就緒**的 Pod，只在 status 端點開；查詢失敗不能讓端點失敗。

只替換 subprocess 邊界，讓真的 ``get_pods``／``get_pod_warning_events`` 解析 kubectl JSON。
"""
import json
import logging
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import KubernetesService, _pod_waiting

_NS = "paas-ws-test-1417"


def _cs(name, ready=False, waiting=None, running=False, restarts=0):
    state = {"waiting": waiting} if waiting else ({"running": {}} if running else {})
    return {"name": name, "ready": ready, "restartCount": restarts, "state": state}


def _pod(name, uid, phase="Running", statuses=None, init_statuses=None, **meta):
    return {
        "metadata": {"name": name, "uid": uid, "namespace": _NS, **meta},
        "status": {
            "phase": phase,
            "initContainerStatuses": init_statuses or [],
            "containerStatuses": statuses if statuses is not None else [
                _cs("app", ready=True, running=True)],
        },
    }


def _event(uid, name, reason, message, last, **extra):
    obj = {"kind": "Pod", "name": name, "namespace": _NS}
    if uid:
        obj["uid"] = uid
    return {"type": "Warning", "reason": reason, "message": message,
            "lastTimestamp": last, "involvedObject": obj, **extra}


def _ok(items):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": items}), stderr="")


_STUCK = _pod("svc-0", "uid-0", phase="Pending",
              init_statuses=[_cs("volume-permissions", waiting={"reason": "PodInitializing"})],
              statuses=[_cs("app", waiting={"reason": "PodInitializing"})])
_READY = _pod("svc-db-0", "uid-db")


# --------------------------------------------------------------------------- #
#  waiting：卡住的容器                                                          #
# --------------------------------------------------------------------------- #

def test_waiting_prefers_init_containers():
    info = _pod_waiting(_STUCK["status"], 300)
    assert (info.container, info.init_container, info.reason) == (
        "volume-permissions", True, "PodInitializing")


def test_substantive_reason_beats_generic_ones():
    """init 還在 PodInitializing、主容器拉不到 image 不可能同時發生；但同一層裡 sidecar 的
    ContainerCreating 不能蓋掉 app 的 ImagePullBackOff。"""
    status = {"containerStatuses": [
        _cs("sidecar", waiting={"reason": "ContainerCreating"}),
        _cs("app", waiting={"reason": "ImagePullBackOff",
                            "message": "Back-off pulling image \"x/y:1\""}),
    ]}
    info = _pod_waiting(status, 300)
    assert (info.container, info.init_container, info.reason) == ("app", False, "ImagePullBackOff")
    assert info.message == 'Back-off pulling image "x/y:1"'


def test_init_crash_loop_is_reported_on_the_init_container():
    status = {
        "initContainerStatuses": [_cs("migrate", waiting={"reason": "CrashLoopBackOff"})],
        "containerStatuses": [_cs("app", waiting={"reason": "PodInitializing"})],
    }
    info = _pod_waiting(status, 300)
    assert (info.container, info.init_container, info.reason) == ("migrate", True, "CrashLoopBackOff")


def test_no_waiting_container_is_none():
    assert _pod_waiting(_READY["status"], 300) is None
    # 還沒排程：沒有任何 containerStatuses。
    assert _pod_waiting({"phase": "Pending"}, 300) is None


def test_waiting_message_is_truncated():
    status = {"containerStatuses": [_cs("app", waiting={"reason": "ErrImagePull",
                                                        "message": "x" * 1000})]}
    info = _pod_waiting(status, 50)
    assert len(info.message) == 50 and info.message.endswith("…")


# --------------------------------------------------------------------------- #
#  last_warning：事件查詢只在需要時、一次                                        #
# --------------------------------------------------------------------------- #

def test_events_are_not_queried_unless_asked():
    """get_pods 的其他呼叫端（ngrok 位址回讀、agent runtime）不多付一次 kubectl。"""
    with patch("subprocess.run", side_effect=[_ok([_STUCK])]) as run:
        pods = KubernetesService().get_pods(_NS, "app.kubernetes.io/instance=svc")
    assert run.call_count == 1
    assert pods[0].last_warning is None
    assert pods[0].waiting.reason == "PodInitializing"


def test_events_are_not_queried_when_every_pod_is_ready():
    finishing = _pod("svc-old", "uid-old", phase="Running",
                     statuses=[_cs("app", ready=False)], deletionTimestamp="2026-10-04T01:00:00Z")
    done = _pod("svc-job", "uid-job", phase="Succeeded", statuses=[_cs("job")])
    with patch("subprocess.run", side_effect=[_ok([_READY, finishing, done])]) as run:
        KubernetesService().get_pods(_NS, include_warning_events=True)
    assert run.call_count == 1, "收尾中／已結束的 Pod 不算未就緒，不該為它們查事件"


def test_latest_warning_is_attached_to_unready_pods_only():
    events = [
        _event("uid-0", "svc-0", "FailedScheduling", "unbound PVC", "2026-10-04T01:17:30Z"),
        _event("uid-0", "svc-0", "FailedAttachVolume",
               "AttachVolume.Attach failed: volume is not ready for workloads",
               "2026-10-04T01:46:02Z", count=12),
        _event("uid-db", "svc-db-0", "Unhealthy", "Readiness probe failed", "2026-10-04T01:50:00Z"),
        # 同名、前一顆 Pod（不同 UID）的事件——時間更晚也不得附上。
        _event("uid-gone", "svc-0", "FailedMount", "old pod", "2026-10-04T01:59:00Z"),
    ]
    with patch("subprocess.run", side_effect=[_ok([_STUCK, _READY]), _ok(events)]) as run:
        pods = KubernetesService().get_pods(_NS, include_warning_events=True)

    args = run.call_args_list[1].args[0]
    assert args[1:4] == ["get", "events", "--namespace"] and _NS in args
    assert "type=Warning,involvedObject.kind=Pod" in args
    assert run.call_args_list[1].kwargs["timeout"] == KubernetesService.POD_EVENTS_TIMEOUT_SECONDS

    by_name = {p.name: p for p in pods}
    warning = by_name["svc-0"].last_warning
    assert (warning.reason, warning.count, warning.last_seen) == (
        "FailedAttachVolume", 12, "2026-10-04T01:46:02Z")
    assert "not ready for workloads" in warning.message
    assert by_name["svc-db-0"].last_warning is None, "就緒的 Pod 的舊事件只會誤導"


def test_event_without_uid_falls_back_to_the_pod_name():
    events = [_event(None, "svc-0", "FailedMount", "MountVolume.SetUp failed", "2026-10-04T01:00:00Z")]
    with patch("subprocess.run", side_effect=[_ok([_STUCK]), _ok(events)]):
        pods = KubernetesService().get_pods(_NS, include_warning_events=True)
    assert pods[0].last_warning.reason == "FailedMount"


def test_new_style_event_timestamps_are_ordered():
    """events.k8s.io 寫進來的事件 lastTimestamp 可能是空的，時間在 series／eventTime。"""
    events = [
        _event("uid-0", "svc-0", "FailedScheduling", "old", None,
               eventTime="2026-10-04T01:10:00.123456Z"),
        _event("uid-0", "svc-0", "FailedAttachVolume", "new", None,
               series={"count": 5, "lastObservedTime": "2026-10-04T01:30:00.000001Z"}),
    ]
    with patch("subprocess.run", side_effect=[_ok([_STUCK]), _ok(events)]):
        pods = KubernetesService().get_pods(_NS, include_warning_events=True)
    warning = pods[0].last_warning
    assert (warning.reason, warning.count) == ("FailedAttachVolume", 5)
    assert warning.last_seen == "2026-10-04T01:30:00.000001Z"


def test_event_lookup_failure_never_fails_get_pods(caplog):
    forbidden = subprocess.CompletedProcess([], 1, stdout="", stderr="events is forbidden")
    with caplog.at_level(logging.WARNING, logger="src.services.helm"), \
            patch("subprocess.run", side_effect=[_ok([_STUCK]), forbidden]):
        pods = KubernetesService().get_pods(_NS, include_warning_events=True)
    assert pods[0].last_warning is None
    assert pods[0].waiting.reason == "PodInitializing", "waiting 來自 Pod 本身，不受事件查詢影響"
    assert any("warning events" in r.getMessage() for r in caplog.records)


def test_event_lookup_timeout_never_fails_get_pods():
    timeout = subprocess.TimeoutExpired(cmd="kubectl", timeout=10)
    with patch("subprocess.run", side_effect=[_ok([_STUCK]), timeout]):
        pods = KubernetesService().get_pods(_NS, include_warning_events=True)
    assert pods[0].last_warning is None


def test_garbage_event_output_never_fails_get_pods():
    garbage = subprocess.CompletedProcess([], 0, stdout="not json", stderr="")
    with patch("subprocess.run", side_effect=[_ok([_STUCK]), garbage]):
        pods = KubernetesService().get_pods(_NS, include_warning_events=True)
    assert pods[0].last_warning is None


# --------------------------------------------------------------------------- #
#  status 端點                                                                  #
# --------------------------------------------------------------------------- #

def _get_status(side_effect):
    release = ReleaseInfo(
        name="svc", namespace=_NS, revision=1, status=ReleaseStatus.DEPLOYED,
        chart="nextcloud", app_version="31", updated="2026-10-04T01:17:00Z",
    )
    with patch("src.main.settings.api_key", "test-1417"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("subprocess.run", side_effect=side_effect):
        response = TestClient(app, headers={"X-API-Key": "test-1417"}).get(
            f"/api/releases/{_NS}/svc/status")
    assert response.status_code == 200, response.text
    return response.json()


def test_status_endpoint_reports_the_diagnostics():
    events = [_event("uid-0", "svc-0", "FailedAttachVolume", "not ready for workloads",
                     "2026-10-04T01:46:02Z")]
    body = _get_status([_ok([_STUCK]), _ok(events)])
    pod = body["pods"][0]
    assert pod["waiting"] == {"container": "volume-permissions", "init_container": True,
                              "reason": "PodInitializing", "message": None}
    assert pod["last_warning"]["reason"] == "FailedAttachVolume"
    assert body["pod_retrieval_error"] is None


def test_status_endpoint_survives_an_event_lookup_failure():
    forbidden = subprocess.CompletedProcess([], 1, stdout="", stderr="events is forbidden")
    body = _get_status([_ok([_STUCK]), forbidden])
    assert body["pod_retrieval_error"] is None, "事件查不到不是 Pod 查不到（平台會誤判 HARD）"
    assert body["pods"][0]["phase"] == "Pending"
    assert body["pods"][0]["last_warning"] is None
