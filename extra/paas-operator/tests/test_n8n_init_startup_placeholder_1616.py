"""#1616：n8n 2.x 啟動中的暫時 server 不算 ready。

2.35.3 實測（沙盒 dev-sandbox-optest1616，容器內每 200ms 探測）：DB migration 跑完之前，
除了 ``/healthz/readiness`` 之外任何路徑都回 200＋純文字「n8n is starting up. Please wait」，
``POST /rest/owner/setup`` 也一樣（請求不會被處理）。chart 的 readinessProbe 打 ``/healthz``，
pod 在這段時間已經 Ready。以前 ``_wait_for_ready`` 只看「200 且 body 非空」，init 在這個空窗
送出就以 500（JSONDecodeError、error_step 空白）收場。
"""
import json

from src.services import n8n_init
from tests.test_init_error_kind_1263 import _http, _n8n_k8s, _post_init_n8n

_PLACEHOLDER = _http(200, "n8n is starting up. Please wait")


def _starting_then_ready(placeholder_responses):
    """前 ``placeholder_responses`` 次 GET /rest/settings 回暫時 server 的 200，之後是真的 API。"""
    k8s = _n8n_k8s()
    real = k8s.exec_in_pod.side_effect
    calls = []

    def exec_in_pod(namespace, pod_name, container, command, timeout=30):
        script = command[-1]
        calls.append(script)
        if 'path:"/rest/settings"' in script and 'method:"GET"' in script:
            if sum('path:"/rest/settings"' in s for s in calls) <= placeholder_responses:
                return _PLACEHOLDER
        if 'path:"/rest/owner/setup"' in script:
            assert sum('path:"/rest/settings"' in s for s in calls) > placeholder_responses, (
                "owner setup 送進了啟動中的暫時 server")
        return real(namespace, pod_name, container, command, timeout=timeout)

    k8s.exec_in_pod.side_effect = exec_in_pod
    return k8s, calls


def test_init_waits_out_the_startup_placeholder():
    k8s, calls = _starting_then_ready(placeholder_responses=3)

    body = _post_init_n8n(k8s).json()

    assert body["success"] is True, body
    assert body["api_key"] == "key-1263"
    assert sum('path:"/rest/owner/setup"' in s for s in calls) == 1


def test_placeholder_that_never_ends_is_wait_ready_not_executed():
    k8s, calls = _starting_then_ready(placeholder_responses=10 ** 6)

    body = _post_init_n8n(k8s).json()

    assert (body["success"], body["error_step"], body["error_kind"]) == (
        False, "wait_ready", "not_executed"), body
    assert not any('path:"/rest/owner/setup"' in s for s in calls)


def test_is_json_object():
    assert n8n_init._is_json_object(json.dumps({"data": {}})) is True
    assert n8n_init._is_json_object("n8n is starting up. Please wait") is False
    assert n8n_init._is_json_object("<!DOCTYPE html>") is False
    assert n8n_init._is_json_object("[]") is False
    assert n8n_init._is_json_object("") is False
    assert n8n_init._is_json_object(None) is False
