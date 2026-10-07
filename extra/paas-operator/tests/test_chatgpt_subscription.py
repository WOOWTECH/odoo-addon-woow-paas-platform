"""LiteLLM「ChatGPT 訂閱」登入（operator 端）：``POST …/chatgpt-subscription/{action}``。

operator 只是轉接：白名單動作、驗模型名稱、找 proxy pod、``kubectl exec`` chart 的 CLI
（``python /woow/py/woow_chatgpt.py <action> [args]``，woow-paas-charts litellm 0.2.0）、
解析它印的那一行 JSON、分類結果。CLI 本身（device flow、token 存檔／DB）已在 chart PR
的沙盒驗過；這裡釘住 operator 這一側的契約：

1. service——selector／container／argv／timeout、各種 stdout／例外的分類、token 鍵
   一律刪掉、不合法的名稱與動作不 exec。
2. API——真的 FastAPI route（X-API-Key、403、422）；一次性登入代碼 ``user_code``
   回得到平台、但不進任何 log（含 KubernetesService 的 ``Executing: kubectl …``）。
"""

import json
import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import chatgpt_subscription as svc_mod
from src.services.chatgpt_subscription import (
    CHATGPT_ACTIONS,
    ChatGPTSubscriptionService,
)
from src.services.helm import KubectlException, KubernetesService

NS = "paas-ws-x"
REL = "svc-abc"
POD = "svc-abc-litellm-7d9-x"
USER_CODE = "QZXW-81723"
SELECTOR = ("app.kubernetes.io/instance=svc-abc,app.kubernetes.io/name=litellm,"
            "app.kubernetes.io/component=app")
OLD_CHART_STDERR = (
    "python: can't open file '/woow/py/woow_chatgpt.py': [Errno 2] No such file or directory\n"
    "command terminated with exit code 2\n"
)
START_OK = {
    "ok": True, "state": "pending", "connected": False,
    "pending": {"verification_url": "https://auth.openai.com/codex/device",
                "user_code": USER_CODE, "expires_at": 1791622119, "interval": 5},
}


def _line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _ok(stdout):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _exit(code, stdout="", stderr=""):
    return KubectlException(f"kubectl exec failed: kubectl command failed with code {code}",
                            "kubectl exec …", stderr, stdout=stdout)


@pytest.fixture
def k8s():
    k8s = MagicMock()
    k8s.get_pod_name.return_value = POD
    return k8s


def _run(k8s, action, **kwargs):
    return ChatGPTSubscriptionService(k8s).run(NS, REL, action, **kwargs)


# --- 1. service：找 pod／exec 的形狀 ------------------------------------------------


def test_status_execs_the_cli_in_the_proxy_container(k8s):
    k8s.exec_in_pod.return_value = _ok(_line({
        "ok": True, "connected": True, "email": "a@b.c", "plan_type": "plus",
        "persisted": True, "pending": None, "models": ["chatgpt/gpt-5.4"]}))

    result = _run(k8s, "status")

    assert result == {
        "success": True,
        "data": {"connected": True, "email": "a@b.c", "plan_type": "plus",
                 "persisted": True, "pending": None, "models": ["chatgpt/gpt-5.4"]},
        "error": None, "error_code": None, "error_kind": None,
    }
    assert k8s.get_pod_name.call_args.args == (NS, SELECTOR)
    kwargs = k8s.exec_in_pod.call_args.kwargs
    assert kwargs["namespace"] == NS and kwargs["pod_name"] == POD
    assert kwargs["container"] == "litellm"
    assert kwargs["command"] == ["python", "/woow/py/woow_chatgpt.py", "status"]
    # 沒有秘密要送進容器 ⇒ 不開 stdin。
    assert kwargs.get("input_data") is None


@pytest.mark.parametrize("action, timeout", [
    ("status", 30), ("start", 30), ("poll", 30), ("disconnect", 30),
    ("add-models", 60), ("test", 150),
])
def test_exec_timeout_per_action(k8s, action, timeout):
    k8s.exec_in_pod.return_value = _ok(_line({"ok": True}))

    _run(k8s, action)

    assert k8s.exec_in_pod.call_args.kwargs["timeout"] == timeout
    assert k8s.exec_in_pod.call_args.kwargs["command"][2] == action


def test_actions_are_exactly_the_design_whitelist():
    assert set(CHATGPT_ACTIONS) == {"status", "start", "poll", "disconnect", "add-models", "test"}
    assert set(svc_mod.CHATGPT_EXEC_TIMEOUT_SECONDS) == set(CHATGPT_ACTIONS)


@pytest.mark.parametrize("action, platform_timeout", [
    ("status", 45), ("start", 45), ("poll", 45), ("disconnect", 45),
    ("add-models", 90), ("test", 180),
])
def test_exec_budget_fits_under_the_platform_client_timeout(action, platform_timeout):
    """預算階梯（DESIGN §3 的平台 client 逾時）：operator 必須先放棄、平台才收得到分類結果。"""
    assert (svc_mod.CHATGPT_EXEC_TIMEOUT_SECONDS[action]
            + svc_mod.CHATGPT_POD_LOOKUP_SLACK_SECONDS) < platform_timeout


def test_add_models_passes_names_as_argv(k8s):
    k8s.exec_in_pod.return_value = _ok(_line({
        "ok": True, "added": ["chatgpt/gpt-5.4"], "kept": ["chatgpt/gpt-5.5"], "failed": []}))

    result = _run(k8s, "add-models", models=["gpt-5.4", "chatgpt/gpt-5.5"])

    assert k8s.exec_in_pod.call_args.kwargs["command"] == [
        "python", "/woow/py/woow_chatgpt.py", "add-models", "gpt-5.4", "chatgpt/gpt-5.5"]
    assert result["success"] is True
    assert result["data"] == {"added": ["chatgpt/gpt-5.4"], "kept": ["chatgpt/gpt-5.5"], "failed": []}


@pytest.mark.parametrize("models", [None, []])
def test_add_models_without_names_uses_the_chart_default_list(k8s, models):
    k8s.exec_in_pod.return_value = _ok(_line({"ok": True, "added": [], "kept": [], "failed": []}))

    _run(k8s, "add-models", models=models)

    assert k8s.exec_in_pod.call_args.kwargs["command"] == [
        "python", "/woow/py/woow_chatgpt.py", "add-models"]


def test_add_models_accepts_twelve_names(k8s):
    k8s.exec_in_pod.return_value = _ok(_line({"ok": True, "added": [], "kept": [], "failed": []}))
    names = [f"gpt-{i}" for i in range(12)]

    _run(k8s, "add-models", models=names)

    assert k8s.exec_in_pod.call_args.kwargs["command"][3:] == names


@pytest.mark.parametrize("model, argv_tail", [
    ("chatgpt/gpt-5.4", ["chatgpt/gpt-5.4"]),
    ("gpt-5.4", ["gpt-5.4"]),
    (None, []),
    ("", []),
])
def test_test_passes_the_model_as_argv(k8s, model, argv_tail):
    k8s.exec_in_pod.return_value = _ok(_line({"ok": True, "model": "chatgpt/gpt-5.4",
                                              "seconds": 2.1, "reply": "OK"}))

    _run(k8s, "test", model=model)

    assert k8s.exec_in_pod.call_args.kwargs["command"] == [
        "python", "/woow/py/woow_chatgpt.py", "test", *argv_tail]


@pytest.mark.parametrize("action, models, model", [
    ("status", ["$(id)"], None),
    ("start", None, "bad name"),
    ("poll", ["x;rm"], "-x"),
    ("disconnect", ["a" * 200], None),
])
def test_fields_of_other_actions_are_ignored(k8s, action, models, model):
    k8s.exec_in_pod.return_value = _ok(_line({"ok": True}))

    _run(k8s, action, models=models, model=model)

    assert k8s.exec_in_pod.call_args.kwargs["command"] == [
        "python", "/woow/py/woow_chatgpt.py", action]


# --- 1b. 不 exec 的情況 -------------------------------------------------------------

_BAD_NAMES = [
    "-rf", "--help", "gpt 5", "gpt;id", "gpt$(id)", "gpt`id`", "../etc/passwd", "a/b",
    "chatgpt/", "chatgpt/-x", "openai/gpt-5", "gpt-5\n", "\ngpt-5", "", "a" * 65,
    "chatgpt/" + "a" * 65, "gpt/5", "gpt\x00", "ｇｐｔ",
]


@pytest.mark.parametrize("name", _BAD_NAMES)
def test_invalid_model_name_for_add_models_is_not_executed(k8s, name):
    result = _run(k8s, "add-models", models=["gpt-5.4", name])

    assert (result["success"], result["error_code"], result["error_kind"]) == (
        False, "invalid_model", "not_executed")
    k8s.get_pod_name.assert_not_called()
    k8s.exec_in_pod.assert_not_called()


@pytest.mark.parametrize("name", [n for n in _BAD_NAMES if n])
def test_invalid_model_name_for_test_is_not_executed(k8s, name):
    result = _run(k8s, "test", model=name)

    assert (result["error_code"], result["error_kind"]) == ("invalid_model", "not_executed")
    k8s.exec_in_pod.assert_not_called()


def test_more_than_twelve_models_is_not_executed(k8s):
    result = _run(k8s, "add-models", models=[f"gpt-{i}" for i in range(13)])

    assert (result["error_code"], result["error_kind"]) == ("invalid_model", "not_executed")
    k8s.exec_in_pod.assert_not_called()


def test_valid_name_limits():
    ok = svc_mod._MODEL_NAME_RE.fullmatch
    assert ok("a" * 64) and ok("chatgpt/" + "a" * 64)
    assert ok("gpt-5.4-codex_mini") and ok("chatgpt/GPT-5")


@pytest.mark.parametrize("action", ["restore", "selfcheck", "", "status;id", "STATUS", "add_models"])
def test_unknown_action_is_not_executed(k8s, action):
    result = _run(k8s, action)

    assert (result["success"], result["error_code"], result["error_kind"]) == (
        False, "invalid_action", "not_executed")
    k8s.get_pod_name.assert_not_called()
    k8s.exec_in_pod.assert_not_called()


@pytest.mark.parametrize("side_effect", [
    KubectlException("No pods found matching selector", "kubectl get pods", ""),
    ValueError("Namespace must start with 'paas-ws-'"),
])
def test_pod_lookup_failure_is_pod_not_found(k8s, side_effect):
    k8s.get_pod_name.side_effect = side_effect

    result = _run(k8s, "status")

    assert result == {"success": False, "data": None, "error": "LiteLLM pod not found",
                      "error_code": "pod_not_found", "error_kind": "not_executed"}
    k8s.exec_in_pod.assert_not_called()


@pytest.mark.parametrize("release", ["svc,app.kubernetes.io/name=other", "svc=x", "svc!", "a" * 64])
def test_release_name_that_is_not_a_label_value_never_reaches_the_selector(k8s, release):
    result = ChatGPTSubscriptionService(k8s).run(NS, release, "status")

    assert (result["error_code"], result["error_kind"]) == ("pod_not_found", "not_executed")
    k8s.get_pod_name.assert_not_called()


# --- 1c. 結果分類 -------------------------------------------------------------------


def test_cli_failure_on_exit_1_is_failed_with_its_code(k8s):
    k8s.exec_in_pod.side_effect = _exit(
        1, stdout=_line({"ok": False, "error": "device_code_request_failed", "status_code": 429}),
        stderr="command terminated with exit code 1")

    result = _run(k8s, "start")

    assert result == {
        "success": False,
        "data": {"error": "device_code_request_failed", "status_code": 429},
        "error": "device_code_request_failed",
        "error_code": "device_code_request_failed",
        "error_kind": "failed",
    }


def test_poll_error_keeps_the_state(k8s):
    k8s.exec_in_pod.side_effect = _exit(
        1, stdout=_line({"ok": False, "state": "error", "error": "token_exchange_failed",
                         "status_code": 400}))

    result = _run(k8s, "poll")

    assert (result["error_code"], result["error_kind"]) == ("token_exchange_failed", "failed")
    assert result["data"]["state"] == "error"


def test_ok_false_on_exit_0_is_still_failed(k8s):
    k8s.exec_in_pod.return_value = _ok(_line({"ok": False, "error": "not_connected"}))

    result = _run(k8s, "add-models")

    assert (result["success"], result["error_code"], result["error_kind"]) == (
        False, "not_connected", "failed")


def test_usage_error_exit_2_is_failed(k8s):
    k8s.exec_in_pod.side_effect = _exit(
        2, stdout=_line({"ok": False, "error": "usage", "commands": ["status"]}))

    result = _run(k8s, "status")

    assert (result["error_code"], result["error_kind"]) == ("usage", "failed")


def test_add_models_partial_failure_without_error_key(k8s):
    k8s.exec_in_pod.side_effect = _exit(1, stdout=_line({
        "ok": False, "added": ["chatgpt/gpt-5.4"], "kept": [], "failed": ["chatgpt/gpt-9"]}))

    result = _run(k8s, "add-models", models=["gpt-5.4", "gpt-9"])

    assert (result["success"], result["error_code"], result["error_kind"]) == (
        False, "models_failed", "failed")
    assert result["data"]["failed"] == ["chatgpt/gpt-9"]


def test_test_failure_with_upstream_message(k8s):
    message = ("ChatGPT subscription is not connected to this LiteLLM … "
               "此 LiteLLM 尚未連接 ChatGPT 訂閱，或登入已失效")
    k8s.exec_in_pod.side_effect = _exit(1, stdout=_line({
        "ok": False, "model": "chatgpt/gpt-5.4", "status_code": 401, "seconds": 0.4,
        "error": message}))

    result = _run(k8s, "test")

    # 上游訊息不是錯誤碼：error_code 給穩定的 test_failed，原文留在 error／data。
    assert (result["error_code"], result["error_kind"]) == ("test_failed", "failed")
    assert result["error"] == message
    assert result["data"]["error"] == message and result["data"]["status_code"] == 401


def test_cli_internal_error_code(k8s):
    k8s.exec_in_pod.side_effect = _exit(1, stdout=_line({
        "ok": False, "error": "internal", "detail": "OperationalError: x"}))

    assert _run(k8s, "status")["error_code"] == "internal"


def test_failure_without_error_key_is_command_failed(k8s):
    k8s.exec_in_pod.side_effect = _exit(1, stdout=_line({"ok": False}))

    result = _run(k8s, "disconnect")

    assert (result["error_code"], result["error_kind"]) == ("command_failed", "failed")


def test_old_chart_without_the_cli_is_unsupported_chart(k8s):
    k8s.exec_in_pod.side_effect = _exit(2, stdout="", stderr=OLD_CHART_STDERR)

    result = _run(k8s, "status")

    assert result == {
        "success": False, "data": None,
        "error": "this LiteLLM chart has no ChatGPT subscription support (redeploy to 0.2.0+)",
        "error_code": "unsupported_chart", "error_kind": "not_executed",
    }


def test_old_chart_detection_needs_the_cli_file_name(k8s):
    k8s.exec_in_pod.side_effect = _exit(
        1, stderr="python: can't open file '/somewhere/else.py': [Errno 2] No such file or directory")

    assert _run(k8s, "status")["error_code"] == "bad_output"


def test_exec_timeout_is_unknown(k8s):
    k8s.exec_in_pod.side_effect = KubectlException(
        "kubectl exec failed: kubectl command timed out after 150s", "kubectl exec …",
        "Command 'kubectl exec …' timed out after 150 seconds", timed_out=True)

    result = _run(k8s, "test")

    assert (result["success"], result["error_code"], result["error_kind"]) == (
        False, "exec_timeout", "unknown")
    assert result["data"] is None


def test_exec_failure_without_output_is_unknown_and_hides_stderr(k8s):
    k8s.exec_in_pod.side_effect = _exit(
        1, stderr='error: unable to upgrade connection: container not found ("litellm") SECRET-ish')

    result = _run(k8s, "poll")

    assert (result["error_code"], result["error_kind"]) == ("bad_output", "unknown")
    assert "SECRET-ish" not in json.dumps(result) and "container not found" not in json.dumps(result)


@pytest.mark.parametrize("stdout", [
    "", "\n\n", "not json\n", "[1, 2]\n", '{"ok": "yes"}\n', '{"state": "pending"}\n',
    '{"ok": true}\ntrailing garbage\n',
])
def test_unparsable_output_is_bad_output(k8s, stdout):
    k8s.exec_in_pod.return_value = _ok(stdout)

    result = _run(k8s, "status")

    assert (result["success"], result["error_code"], result["error_kind"]) == (
        False, "bad_output", "unknown")
    assert result["data"] is None


def test_only_the_last_line_counts(k8s):
    k8s.exec_in_pod.return_value = _ok("some warning on stdout\n" + _line({"ok": True, "state": "idle"}))

    assert _run(k8s, "poll") == {"success": True, "data": {"state": "idle"}, "error": None,
                                 "error_code": None, "error_kind": None}


def test_token_keys_are_removed_everywhere(k8s):
    k8s.exec_in_pod.return_value = _ok(_line({
        "ok": True, "state": "connected", "email": "a@b.c",
        "access_token": "AT", "refresh_token": "RT", "id_token": "IT",
        "nested": {"refresh_token": "RT2", "keep": 1,
                   "list": [{"id_token": "IT2", "access_token": "AT2", "x": 2}]},
        "pending": {"user_code": USER_CODE, "device_auth_id": "DA", "code_verifier": "CV",
                    "authorization_code": "AC"},
    }))

    result = _run(k8s, "poll")

    assert result["data"] == {
        "state": "connected", "email": "a@b.c",
        "nested": {"keep": 1, "list": [{"x": 2}]},
        "pending": {"user_code": USER_CODE},
    }
    dumped = json.dumps(result)
    for secret in ("AT", "RT", "IT", "DA", "CV", "AC"):
        assert f'"{secret}' not in dumped


def test_token_keys_are_removed_from_failure_data_too(k8s):
    k8s.exec_in_pod.side_effect = _exit(1, stdout=_line({
        "ok": False, "error": "internal", "refresh_token": "RT"}))

    assert "refresh_token" not in _run(k8s, "status")["data"]


def test_service_log_has_action_and_code_but_not_the_user_code(k8s, caplog):
    caplog.set_level(logging.DEBUG)
    k8s.exec_in_pod.return_value = _ok(_line(START_OK))

    result = _run(k8s, "start")

    assert result["data"]["pending"]["user_code"] == USER_CODE
    assert USER_CODE not in caplog.text
    assert "start" in caplog.text and "success=True" in caplog.text


def test_service_log_on_failure_has_only_the_code(k8s, caplog):
    caplog.set_level(logging.DEBUG)
    k8s.exec_in_pod.side_effect = _exit(1, stdout=_line({
        "ok": False, "state": "error", "error": "poll_failed", "status_code": 502,
        "pending": {"user_code": USER_CODE}}), stderr=f"stderr mentions {USER_CODE}")

    _run(k8s, "poll")

    assert "error_code=poll_failed" in caplog.text
    assert USER_CODE not in caplog.text


# --- 2. API ------------------------------------------------------------------------


def _client():
    return TestClient(app, headers={"X-API-Key": "test-chatgpt"}, raise_server_exceptions=False)


def _post(path, service, body=None, **kwargs):
    with patch("src.main.settings.api_key", "test-chatgpt"), \
            patch("src.api.chatgpt_subscription.chatgpt_subscription_service", service):
        if body is None:
            return _client().post(path, **kwargs)
        return _client().post(path, json=body, **kwargs)


URL = f"/api/releases/{NS}/{REL}/chatgpt-subscription"


def test_api_start_returns_the_code_without_logging_it(k8s, caplog):
    caplog.set_level(logging.DEBUG)
    k8s.exec_in_pod.return_value = _ok(_line(START_OK))

    resp = _post(f"{URL}/start", ChatGPTSubscriptionService(k8s))

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["error_kind"] is None
    assert body["data"]["pending"]["user_code"] == USER_CODE
    assert USER_CODE not in caplog.text


def test_api_through_the_real_kubectl_wrapper_never_logs_the_code(caplog):
    """KubernetesService 本身會記 ``Executing: kubectl …``：argv 裡只有指令，stdout 不記。"""
    caplog.set_level(logging.DEBUG)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["get", "pods"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=POD, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=_line(START_OK), stderr="")

    with patch("src.services.helm.subprocess.run", side_effect=fake_run):
        resp = _post(f"{URL}/start", ChatGPTSubscriptionService(KubernetesService()))

    assert resp.status_code == 200 and resp.json()["data"]["pending"]["user_code"] == USER_CODE
    assert calls[1][1:] == ["exec", POD, "--namespace", NS, "--container", "litellm", "--",
                            "python", "/woow/py/woow_chatgpt.py", "start"]
    assert "Executing:" in caplog.text
    assert USER_CODE not in caplog.text


def test_api_old_chart_through_the_real_kubectl_wrapper(caplog):
    def fake_run(cmd, **kwargs):
        if cmd[1:3] == ["get", "pods"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=POD, stderr="")
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr=OLD_CHART_STDERR)

    with patch("src.services.helm.subprocess.run", side_effect=fake_run):
        resp = _post(f"{URL}/status", ChatGPTSubscriptionService(KubernetesService()))

    body = resp.json()
    assert resp.status_code == 200
    assert (body["success"], body["error_code"], body["error_kind"]) == (
        False, "unsupported_chart", "not_executed")
    assert "Errno" not in resp.text


def test_api_without_body_passes_no_names():
    service = MagicMock()
    service.run.return_value = {"success": True, "data": {"connected": False}, "error": None,
                                "error_code": None, "error_kind": None}

    resp = _post(f"{URL}/status", service)

    assert resp.status_code == 200
    assert resp.json() == {"success": True, "data": {"connected": False}, "error": None,
                           "error_code": None, "error_kind": None}
    service.run.assert_called_once_with(namespace=NS, release_name=REL, action="status",
                                        models=None, model=None)


def test_api_passes_models_and_model():
    service = MagicMock()
    service.run.return_value = {"success": True, "data": {}, "error": None,
                                "error_code": None, "error_kind": None}

    _post(f"{URL}/add-models", service, body={"models": ["gpt-5.4", "chatgpt/gpt-5.5"]})
    _post(f"{URL}/test", service, body={"model": "chatgpt/gpt-5.4"})

    assert service.run.call_args_list[0].kwargs["models"] == ["gpt-5.4", "chatgpt/gpt-5.5"]
    assert service.run.call_args_list[0].kwargs["action"] == "add-models"
    assert service.run.call_args_list[1].kwargs["model"] == "chatgpt/gpt-5.4"


def test_api_invalid_model_is_200_not_executed(k8s):
    resp = _post(f"{URL}/add-models", ChatGPTSubscriptionService(k8s), body={"models": ["gpt 5"]})

    body = resp.json()
    assert resp.status_code == 200
    assert (body["success"], body["error_code"], body["error_kind"]) == (
        False, "invalid_model", "not_executed")
    k8s.exec_in_pod.assert_not_called()


@pytest.mark.parametrize("action", ["restore", "selfcheck", "add_models", "rm"])
def test_api_unknown_action_is_422_and_not_executed(action):
    service = MagicMock()

    resp = _post(f"{URL}/{action}", service)

    assert resp.status_code == 422
    service.run.assert_not_called()


def test_api_namespace_outside_the_tenant_prefix_is_403():
    service = MagicMock()

    resp = _post("/api/releases/kube-system/svc-abc/chatgpt-subscription/status", service)

    assert resp.status_code == 403
    service.run.assert_not_called()


def test_api_requires_the_api_key():
    service = MagicMock()
    with patch("src.main.settings.api_key", "test-chatgpt"), \
            patch("src.api.chatgpt_subscription.chatgpt_subscription_service", service):
        resp = TestClient(app).post(f"{URL}/status")

    assert resp.status_code == 401
    service.run.assert_not_called()


def test_api_unexpected_exception_is_500_without_the_message(caplog):
    caplog.set_level(logging.DEBUG)
    service = MagicMock()
    service.run.side_effect = RuntimeError(f"boom {USER_CODE}")

    resp = _post(f"{URL}/poll", service)

    assert resp.status_code == 500
    assert resp.json()["detail"]["error_kind"] == "unknown"
    assert USER_CODE not in resp.text and USER_CODE not in caplog.text
    assert "RuntimeError" in caplog.text
