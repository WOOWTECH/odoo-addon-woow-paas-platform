"""#1534：owner 已存在時，n8n init 不再用平台存的 owner 密碼登入。

租戶在 n8n 介面改了 owner 密碼或開了 MFA 之後，平台那組密碼登入會 401（MFA 是
``{"code":998}``）。以前 Redeploy 的 init 一律 ``POST /rest/login`` ⇒ 每次都 failed，五次後
服務轉 error，租戶的「重設管理員密碼」又要求 running ⇒ 自己救不回來。

修正：owner 已存在 ⇒ exec ``n8n_scripts/ensure_api_key.js`` 沿用或簽發 init 用的 key（參數走
stdin）。這裡只換掉 KubernetesService 邊界、打真的 ``/init/n8n``；腳本本身對真 n8n 2.35.3 的
行為（改密碼、開 MFA、key 被刪、owner 不是開通時那位）由 integration
``tests/integration/test_n8n_init_owner_changed_1534.py`` 驗。
"""
import json
import re
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException
from src.services.n8n_init import ENSURE_API_KEY_SCRIPT
from tests.test_init_error_kind_1263 import _http

_SETTINGS_OWNER_DONE = json.dumps({"data": {"userManagement": {"showSetupOnFirstLoad": False}}})
_PASSWORD = "pw-1534-platform-stored"


def _ok(api_key="key-1534", reused=False):
    return MagicMock(stdout=json.dumps({"success": True, "api_key": api_key, "reused": reused}) + "\n")


def _script_failure(code, error="boom"):
    out = json.dumps({"success": False, "code": code, "error": error}) + "\n"
    return KubectlException("kubectl exec failed: code 1", "exec", "", stdout=out)


class _FakeN8n:
    """owner 已完成 setup 的 n8n：REST 依 method＋path 回應，ensure 腳本由 ``ensure`` 決定。"""

    def __init__(self, ensure=None):
        self.ensure = ensure if ensure is not None else _ok()
        self.rest_calls = []
        self.ensure_calls = []
        self.k8s = MagicMock()
        self.k8s.get_pod_name.return_value = "n8n-0"
        self.k8s.exec_in_pod.side_effect = self._exec
        self.k8s._run_command.return_value = MagicMock(stdout="svc-n8n-app-secret")
        self.k8s.get_deployments.return_value = [{"name": "svc-n8n"}]

    def _exec(self, namespace, pod_name, container, command, timeout=30, input_data=None):
        script = command[-1]
        if script == ENSURE_API_KEY_SCRIPT:
            self.ensure_calls.append({"command": command, "timeout": timeout, "stdin": input_data})
            if isinstance(self.ensure, Exception):
                raise self.ensure
            return self.ensure
        method = re.search(r'method:"([A-Z]+)"', script).group(1)
        path = re.search(r'path:"([^"]+)"', script).group(1)
        self.rest_calls.append((method, path))
        if (method, path) == ("GET", "/rest/settings"):
            return _http(200, _SETTINGS_OWNER_DONE)
        raise AssertionError(f"owner 已存在時不該打 {method} {path}")

    def post(self, **extra):
        body = {"owner_email": "Tenant@Example.com", "owner_password": _PASSWORD, **extra}
        with patch("src.main.settings.api_key", "test-1534"), \
                patch("src.api.init.n8n_init_service.k8s", self.k8s), \
                patch("src.services.n8n_init.time.sleep"):
            return TestClient(app, headers={"X-API-Key": "test-1534"}).post(
                "/api/releases/paas-ws-x/svc/init/n8n", json=body,
            )


def test_existing_owner_never_logs_in_with_the_platform_password():
    fake = _FakeN8n()
    body = fake.post(owner_confirmed=True).json()

    assert body["success"] is True
    assert body["api_key"] == "key-1534"
    assert ("POST", "/rest/login") not in fake.rest_calls
    assert ("POST", "/rest/owner/setup") not in fake.rest_calls
    assert len(fake.ensure_calls) == 1


def test_key_is_written_to_the_secret_and_pods_restart_as_before():
    fake = _FakeN8n(ensure=_ok(api_key="key-reused", reused=True))
    body = fake.post(owner_confirmed=True).json()

    assert body["secret_patched"] is True and body["pod_restarted"] is True
    fake.k8s.patch_secret.assert_called_once_with(
        namespace="paas-ws-x", secret_name="svc-n8n-app-secret", data={"N8N_API_KEY": "key-reused"})
    fake.k8s.rollout_restart_deployment.assert_called_once()


def test_parameters_go_through_stdin_never_argv():
    fake = _FakeN8n()
    fake.post(owner_confirmed=True)

    call = fake.ensure_calls[0]
    assert call["command"] == ["node", "-e", ENSURE_API_KEY_SCRIPT]
    stdin = json.loads(call["stdin"])
    # DATA-N8N-006-1：期限改成「現在＋TTL」、多帶輪替窗口（細節見 test_n8n_sidecar_key_ttl_dn006.py）
    assert set(stdin) == {"label", "expected_email", "expires_at", "rotate_before_seconds"}
    assert stdin["label"] == "mcp-sidecar" and stdin["expected_email"] is None
    assert _PASSWORD not in call["stdin"], "owner 密碼完全用不到，也不該送進容器"
    assert call["timeout"] == 40


def test_unconfirmed_owner_must_match_the_provisioning_email():
    """平台還沒確認過 owner（舊平台不送這個欄位也一樣）⇒ 腳本要核對 Email。"""
    for extra in ({}, {"owner_confirmed": False}):
        fake = _FakeN8n()
        assert fake.post(**extra).json()["success"] is True
        assert json.loads(fake.ensure_calls[0]["stdin"])["expected_email"] == "Tenant@Example.com"


def test_owner_identity_failures_are_failed_with_their_own_step():
    for code in ("owner_mismatch", "owner_missing", "owner_ambiguous"):
        body = _FakeN8n(ensure=_script_failure(code)).post().json()
        assert body["success"] is False
        assert (body["error_step"], body["error_kind"]) == ("owner_identity", "failed"), code
        assert code in body["error"]
        assert body["api_key"] is None


def test_key_failures_reported_by_the_script_are_failed():
    for code in ("key_unverified", "signing_secret_unavailable", "db_unsupported", "module_unavailable"):
        body = _FakeN8n(ensure=_script_failure(code)).post(owner_confirmed=True).json()
        assert (body["error_step"], body["error_kind"]) == ("ensure_api_key", "failed"), code


def test_ambiguous_script_outcomes_are_unknown():
    cases = {
        "timeout": KubectlException("kubectl exec failed: timed out", "exec", "", timed_out=True),
        "exec error without output": KubectlException("kubectl exec failed", "exec", "boom"),
        "unparsable output": MagicMock(stdout="node: something went wrong\n"),
        "unexpected exception inside the script": MagicMock(
            stdout=json.dumps({"success": False, "error": "Error while ensuring the API key"})),
        "success without a key": MagicMock(stdout=json.dumps({"success": True})),
    }
    for name, ensure in cases.items():
        body = _FakeN8n(ensure=ensure).post(owner_confirmed=True).json()
        assert body["success"] is False, name
        assert (body["error_step"], body["error_kind"]) == ("ensure_api_key", "unknown"), name


def test_timeout_does_not_trust_partial_stdout():
    exc = KubectlException("kubectl exec failed: timed out", "exec", "", timed_out=True,
                           stdout=json.dumps({"success": False, "code": "owner_mismatch"}))
    body = _FakeN8n(ensure=exc).post().json()
    assert (body["error_step"], body["error_kind"]) == ("ensure_api_key", "unknown")


# ---- 腳本的靜態契約（與 reset_owner_password.js 同一套限制）------------------------

def test_script_reads_parameters_only_from_stdin():
    assert "process.argv" not in ENSURE_API_KEY_SCRIPT
    assert "process.stdin" in ENSURE_API_KEY_SCRIPT


def test_script_requires_only_builtins_or_the_n8n_installation():
    direct = set(re.findall(r'\brequire\("([^"]+)"\)', ENSURE_API_KEY_SCRIPT))
    assert direct <= {"crypto", "fs", "http", "path", "module"}, direct
    via_n8n = set(re.findall(r'requireFromN8n\(req, "([^"]+)"\)', ENSURE_API_KEY_SCRIPT))
    # n8n-core：n8n 2.43.x 起簽章密鑰在 DB 裡以 AES-256-GCM 包起來，解包要用 n8n 自己的 Cipher
    # （DATA-N8N-006-1 附帶發現；n8n-core 就是 n8n 安裝目錄裡的套件，不是額外相依）。
    assert via_n8n <= {"sqlite3", "pg", "n8n-core"}, via_n8n


def _code_lines():
    return [line for line in ENSURE_API_KEY_SCRIPT.splitlines() if not line.lstrip().startswith("//")]


def test_script_does_not_depend_on_filename():
    code = "\n".join(_code_lines())
    assert "__filename" not in code
    assert "__dirname" not in code


def test_script_never_echoes_the_owner_email_in_failures():
    """失敗訊息會進平台的 attempt 記錄；owner_mismatch 不回傳實際的 owner Email。"""
    mismatch = re.search(r'owner_mismatch", "([^"]+)"', ENSURE_API_KEY_SCRIPT).group(1)
    assert "${" not in mismatch
