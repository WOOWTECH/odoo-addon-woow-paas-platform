"""#1263：init 失敗分類（error_step／error_kind）與 kubectl timeout 的可判別屬性。

分類依「遠端副作用是否可能已發生」（#1257 Q11）：readiness 探測唯讀 ⇒ not_executed；
收到明確錯誤回應 ⇒ failed；n8n 已拿到 api_key 但 Secret 沒寫成 ⇒ partial；其餘
（timeout、exec 失敗、輸出不可解析、非預期例外）⇒ unknown。
"""
import json
import re
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException, KubernetesService


@patch("subprocess.run")
def test_kubectl_timeout_is_distinguishable_without_parsing_message(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=5)

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService()._run_command(["exec", "p"], timeout=5)

    assert excinfo.value.timed_out is True


@patch("subprocess.run")
def test_kubectl_non_zero_exit_is_not_a_timeout(mock_run):
    mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService()._run_command(["get", "pods"])

    assert excinfo.value.timed_out is False


@patch("subprocess.run")
def test_exec_in_pod_rewrap_keeps_timeout_flag(mock_run, monkeypatch):
    """exec_in_pod 會重包例外；重包不可把 timeout 資訊丟掉。"""
    monkeypatch.setattr("src.services.helm.validate_namespace", lambda ns: None)
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=5)

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService().exec_in_pod("paas-ws-x", "p", "c", ["true"], timeout=5)

    assert excinfo.value.timed_out is True


@patch("subprocess.run")
def test_exec_in_pod_failure_keeps_stdout(mock_run, monkeypatch):
    """腳本先印 JSON 再 exit 1 時，那行回報要穿過 _run_command 與 exec_in_pod 的重包。"""
    monkeypatch.setattr("src.services.helm.validate_namespace", lambda ns: None)
    mock_run.return_value = subprocess.CompletedProcess(
        [], 1, stdout='{"success": false}\n', stderr="command terminated with exit code 1")

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService().exec_in_pod("paas-ws-x", "p", "c", ["true"])

    assert excinfo.value.stdout == '{"success": false}\n'
    assert excinfo.value.timed_out is False


def test_kubectl_exception_legacy_constructor_defaults():
    """既有呼叫端（含測試）以三個位置參數建構，不可因新屬性而壞。"""
    exc = KubectlException("m", "cmd", "err")
    assert (exc.timed_out, exc.stdout) == (False, "")


# ---- n8n：打真的 /init/n8n，只換掉 KubernetesService 邊界 -----------------------

_OWNER = {"owner_email": "o@example.com", "owner_password": "pw-1263"}
_SETTINGS_NEEDS_SETUP = json.dumps({"data": {"userManagement": {"showSetupOnFirstLoad": True}}})


def _http(status, body="", headers=None):
    out = {"status": status, "body": body}
    if headers is not None:
        out["headers"] = json.dumps(headers)
    return MagicMock(stdout=json.dumps(out))


def _n8n_k8s(overrides=None, secrets="svc-app-secret"):
    """假 n8n：依 node script 裡的 method＋path 回應；``overrides`` 可換成例外或別的回應。

    預設是一條完整成功的路：settings 需要 setup → owner 建立 → 登入拿 cookie → 建 key。
    """
    routes = {
        ("GET", "/rest/settings"): _http(200, _SETTINGS_NEEDS_SETUP),
        ("POST", "/rest/owner/setup"): _http(200, json.dumps({"data": {"id": "u1"}})),
        ("POST", "/rest/login"): _http(200, "{}", {"set-cookie": ["n8n-auth=tok; Path=/"]}),
        ("GET", "/rest/api-keys"): _http(200, json.dumps({"data": {"items": [], "counts": {}}})),
        ("GET", "/rest/api-keys/scopes"): _http(200, json.dumps({"data": ["workflow:read"]})),
        ("POST", "/rest/api-keys"): _http(200, json.dumps({"data": {"rawApiKey": "key-1263"}})),
    }
    routes.update(overrides or {})

    def exec_in_pod(namespace, pod_name, container, command, timeout=30):
        script = command[-1]
        method = re.search(r'method:"([A-Z]+)"', script).group(1)
        path = re.search(r'path:"([^"]+)"', script).group(1)
        answer = routes[(method, path)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    k8s = MagicMock()
    k8s.get_pod_name.return_value = "n8n-0"
    k8s.exec_in_pod.side_effect = exec_in_pod
    k8s._run_command.return_value = MagicMock(stdout=secrets)
    k8s.get_deployments.return_value = [{"name": "svc"}]
    return k8s


def _post_init_n8n(k8s):
    with patch("src.main.settings.api_key", "test-1263"), \
            patch("src.api.init.n8n_init_service.k8s", k8s), \
            patch("src.services.n8n_init.time.sleep"):
        return TestClient(app, headers={"X-API-Key": "test-1263"}).post(
            "/api/releases/paas-ws-x/svc/init/n8n", json=_OWNER,
        )


def _timeout():
    return KubectlException("kubectl exec failed: timed out", "exec", "", timed_out=True)


def _not_found_k8s():
    k8s = _n8n_k8s()
    k8s.get_pod_name.side_effect = KubectlException("No pods", "get pods", "")
    return k8s


@pytest.mark.parametrize("k8s_factory, step, kind", [
    # readiness 探測唯讀。
    (_not_found_k8s, "find_pod", "not_executed"),
    (lambda: _n8n_k8s({("GET", "/rest/settings"): _http(503, "starting")}),
     "wait_ready", "not_executed"),
    # 收到 n8n 明確的錯誤回應 ⇒ 確定失敗。
    (lambda: _n8n_k8s({("POST", "/rest/owner/setup"): _http(400, "already setup")}),
     "setup_owner", "failed"),
    (lambda: _n8n_k8s({("POST", "/rest/login"): _http(401, "wrong password")}),
     "login", "failed"),
    (lambda: _n8n_k8s({("POST", "/rest/api-keys"): _http(403, "forbidden")}),
     "create_api_key", "failed"),
    # 同一步驟的 exec timeout：請求可能已送達 n8n ⇒ 未知。
    (lambda: _n8n_k8s({("POST", "/rest/owner/setup"): _timeout()}), "setup_owner", "unknown"),
    (lambda: _n8n_k8s({("POST", "/rest/login"): _timeout()}), "login", "unknown"),
    (lambda: _n8n_k8s({("POST", "/rest/api-keys"): _timeout()}), "create_api_key", "unknown"),
], ids=["find_pod", "wait_ready", "setup_owner-http", "login-http", "create_api_key-http",
        "setup_owner-timeout", "login-timeout", "create_api_key-timeout"])
def test_init_n8n_failure_carries_error_step_and_kind(k8s_factory, step, kind):
    response = _post_init_n8n(k8s_factory())

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert body["error_step"] == step
    assert body["error_kind"] == kind
    assert body["error"].startswith(f"[{step}] ")


@pytest.mark.parametrize("overrides, step", [
    # 沒拿到 HTTP 回應（node 端 request error）≠ 明確的錯誤回應。
    ({("POST", "/rest/api-keys"): _http(0, "socket hang up")}, "create_api_key"),
    # 200 但 body 不合預期：n8n 已接受請求，副作用可能已發生。
    ({("POST", "/rest/api-keys"): _http(200, json.dumps({"data": {}}))}, "create_api_key"),
    ({("POST", "/rest/owner/setup"): _http(200, json.dumps({"data": {}}))}, "setup_owner"),
    ({("POST", "/rest/login"): _http(200, "{}", {})}, "login"),
    ({("POST", "/rest/login"): _http(200, "{}", {"set-cookie": ["; Path=/"]})}, "login"),
    # 非 timeout 的 exec 失敗：分不出請求有沒有送達。
    ({("POST", "/rest/api-keys"): KubectlException("kubectl exec failed", "exec", "")},
     "create_api_key"),
], ids=["no-http-response", "200-without-key", "200-without-owner-id",
        "login-without-set-cookie", "login-empty-cookie", "exec-failed"])
def test_init_n8n_ambiguous_outcome_is_unknown(overrides, step):
    body = _post_init_n8n(_n8n_k8s(overrides)).json()

    assert body["success"] is False
    assert body["error_step"] == step
    assert body["error_kind"] == "unknown"


@pytest.mark.parametrize("k8s_factory, step", [
    (lambda: _n8n_k8s(secrets=""), "find_secret"),
    (lambda: _patch_fails(_n8n_k8s()), "patch_secret"),
], ids=["secret-missing", "patch-failed"])
def test_init_n8n_key_created_but_secret_not_written_is_partial(k8s_factory, step):
    body = _post_init_n8n(k8s_factory()).json()

    assert body["success"] is False
    assert body["api_key"] == "key-1263"
    assert body["error_step"] == step
    assert body["error_kind"] == "partial"


def _patch_fails(k8s):
    k8s.patch_secret.side_effect = KubectlException("patch failed", "patch", "")
    return k8s


def test_init_n8n_success_has_no_error_classification():
    body = _post_init_n8n(_n8n_k8s()).json()

    assert body["success"] is True
    assert body["api_key"] == "key-1263"
    assert body["error_step"] is None
    assert body["error_kind"] is None


def test_init_n8n_unexpected_exception_is_500_with_structured_unknown():
    k8s = _n8n_k8s()
    k8s.get_pod_name.side_effect = RuntimeError("kaboom")

    response = _post_init_n8n(k8s)

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail == {
        "message": "n8n initialization failed: kaboom",
        "error_step": None,
        "error_kind": "unknown",
    }
