"""#1456：Pod 主要容器上一次結束的原因（``lastState.terminated.reason``）穿過 status API。

商用驗收 DATA-N8N-010-1（2026-10-16）：n8n 250 的 Code 節點配置 3000 MB Buffer，容器被
``OOMKilled``（exit 137、restartCount 0→1），readiness 失敗約 35 秒，平台全程 ``running``。
平台要把「跑著卻沒有一顆 Pod 就緒」標成異常、而且說得出原因，operator 就得把「上一次為什麼
死掉」報上來——``waiting.reason`` 只看得到現在卡在 ``CrashLoopBackOff``，看不到是記憶體
不足。

- ``PodInfo.last_terminated_reason``：只看主要容器（``containerStatuses``；init 容器的
  ``Completed`` 是正常結束，不是線索）；任一容器是 ``OOMKilled`` 就回它，否則回第一個有值的
  reason；都沒有回 None（不是錯誤）。

只替換 subprocess 邊界，讓真的 ``get_pods`` 解析 kubectl JSON。
"""
import json
import subprocess
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import KubernetesService, _pod_last_terminated_reason

_NS = "paas-ws-test-1456"


def _cs(name, ready=True, last_reason=None, waiting=None):
    cs = {"name": name, "ready": ready, "restartCount": 1 if last_reason else 0,
          "state": {"waiting": waiting} if waiting else {"running": {}}}
    if last_reason:
        cs["lastState"] = {"terminated": {"reason": last_reason, "exitCode": 137}}
    return cs


def _pod(name, statuses, init_statuses=None):
    return {
        "metadata": {"name": name, "uid": "uid-" + name, "namespace": _NS},
        "status": {"phase": "Running", "containerStatuses": statuses,
                   "initContainerStatuses": init_statuses or []},
    }


def _ok(items):
    return subprocess.CompletedProcess([], 0, stdout=json.dumps({"items": items}), stderr="")


def test_oom_killed_wins_over_other_reasons():
    status = {"containerStatuses": [_cs("sidecar", last_reason="Error"),
                                    _cs("n8n", ready=False, last_reason="OOMKilled")]}
    assert _pod_last_terminated_reason(status) == "OOMKilled"


def test_first_reason_when_nothing_was_oom_killed():
    status = {"containerStatuses": [_cs("app"), _cs("worker", last_reason="Error"),
                                    _cs("other", last_reason="Completed")]}
    assert _pod_last_terminated_reason(status) == "Error"


def test_init_containers_are_not_evidence():
    """init 容器跑完都是 ``Completed``；它不是主要容器死掉的原因。"""
    status = {"initContainerStatuses": [_cs("migrate", last_reason="OOMKilled")],
              "containerStatuses": [_cs("app")]}
    assert _pod_last_terminated_reason(status) is None


def test_nothing_terminated_is_none():
    assert _pod_last_terminated_reason({"containerStatuses": [_cs("app")]}) is None
    assert _pod_last_terminated_reason({"phase": "Pending"}) is None
    garbage = {"containerStatuses": [None, "x", {"lastState": "weird"},
                                     {"lastState": {"terminated": None}}]}
    assert _pod_last_terminated_reason(garbage) is None


def test_get_pods_carries_the_reason():
    pods = [_pod("svc-n8n-0", [_cs("n8n", ready=False, last_reason="OOMKilled",
                                   waiting={"reason": "CrashLoopBackOff"})]),
            _pod("svc-db-0", [_cs("db")])]
    with patch("subprocess.run", side_effect=[_ok(pods)]):
        result = KubernetesService().get_pods(_NS)
    assert [p.last_terminated_reason for p in result] == ["OOMKilled", None]


def test_status_endpoint_reports_the_reason():
    release = ReleaseInfo(
        name="svc", namespace=_NS, revision=1, status=ReleaseStatus.DEPLOYED,
        chart="n8n", app_version="2.43.6", updated="2026-10-16T01:00:00Z",
    )
    crashing = _pod("svc-n8n-0", [_cs("n8n", ready=False, last_reason="OOMKilled",
                                      waiting={"reason": "CrashLoopBackOff"})])
    no_events = _ok([])
    with patch("src.main.settings.api_key", "test-1456"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("subprocess.run", side_effect=[_ok([crashing]), no_events]):
        response = TestClient(app, headers={"X-API-Key": "test-1456"}).get(
            f"/api/releases/{_NS}/svc/status")
    assert response.status_code == 200, response.text
    pod = response.json()["pods"][0]
    assert pod["last_terminated_reason"] == "OOMKilled"
    assert pod["waiting"]["reason"] == "CrashLoopBackOff"
