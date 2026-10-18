"""#1774（商用驗收 INFRA-EMQX-007-1）：EMQX Dashboard 管理員密碼重設（operator 端）。

EMQX 的 dashboard.default_password 只在第一次開機寫進它自己的 mnesia，之後改 chart Secret
或 helm upgrade 都換不到密碼——平台原本只能說「請在服務內修改」，擁有者忘記密碼（或被人
改掉）就沒有任何救援。這裡 exec 一支 sh 腳本進 emqx 容器，呼叫 EMQX 內建的
``emqx ctl admins passwd``（與 Dashboard「修改密碼」同一個 change_password）。

腳本的真實行為（平台釘的 5.8.9 image、真 broker、登入 API）已在沙盒驗過（見 PR）；這裡用
假的 ``emqx`` 指令跑**真的腳本**（dash），釘住 stdin／輸出判讀／拒絕契約，以及
service／API 的接線。
"""

import json
import logging
import os
import shutil
import subprocess
import textwrap
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import app_admin_password as svc_mod
from src.services.app_admin_password import (
    EMQX_RESET_ADMIN_SCRIPT,
    AppAdminPasswordService,
    AppAdminResetError,
)
from src.services.helm import KubectlException

NEW_PASSWORD = "N3w-Emqx-Passw0rd_xyz"

SH = shutil.which("dash") or shutil.which("sh")


# --- 1. 腳本（真腳本＋假 emqx 指令） ---------------------------------------------


@pytest.fixture
def fake_emqx(tmp_path):
    """PATH 上放一支假 ``emqx``：把 argv 逐行記到檔案、印 FAKE_OUT、以 FAKE_RC 結束。"""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_log = tmp_path / "argv.log"
    stub = bindir / "emqx"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        for a in "$@"; do printf '%s\\n' "$a" >> '{argv_log}'; done
        printf '%s\\n' "$FAKE_OUT"
        exit "${{FAKE_RC:-0}}"
        """))
    stub.chmod(0o755)

    def run(stdin, out="ok", rc=0, env=None):
        full_env = {"PATH": f"{bindir}:/usr/bin:/bin", "FAKE_OUT": out, "FAKE_RC": str(rc)}
        full_env.update(env or {})
        proc = subprocess.run([SH, "-c", EMQX_RESET_ADMIN_SCRIPT], input=stdin, env=full_env,
                              capture_output=True, text=True, timeout=10)
        argv = argv_log.read_text().splitlines() if argv_log.exists() else []
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        reported = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else None
        return proc, reported, argv

    return run


def test_script_resets_admin_via_emqx_ctl(fake_emqx):
    proc, out, argv = fake_emqx(NEW_PASSWORD)

    assert proc.returncode == 0
    assert out == {"success": True, "login": "admin", "second_factor_cleared": False}
    assert argv == ["ctl", "admins", "passwd", "admin", NEW_PASSWORD]


def test_script_strips_crlf_and_uses_chart_username(fake_emqx):
    proc, out, argv = fake_emqx(NEW_PASSWORD + "\r\n", env={"EMQX_DASHBOARD__DEFAULT_USERNAME": "boss"})

    assert proc.returncode == 0 and out["login"] == "boss"
    assert argv == ["ctl", "admins", "passwd", "boss", NEW_PASSWORD]


@pytest.mark.parametrize("ctl_out, ctl_rc, expected", [
    # emqx 5.8.9 實測輸出（不論成敗 rc=0）。
    ("Error: username_not_found", 0, "admin_missing"),
    ("Error: Bad password. At least two different kind of characters from groups of letters, "
     "numbers, and special characters.", 0, "invalid_password"),
    ("Error: The range of password length is 8~64", 0, "invalid_password"),
    # ctl 連不上 broker 節點：什麼都沒寫入。
    ("ERROR: Node emqx@127.0.0.1 is not running?", 1, "db_unsupported"),
    ("ERROR: EMQX_NODE__COOKIE is different from the cookie used by emqx@127.0.0.1", 1,
     "db_unsupported"),
])
def test_script_maps_emqx_errors_to_failed_codes(fake_emqx, ctl_out, ctl_rc, expected):
    proc, out, _ = fake_emqx(NEW_PASSWORD, out=ctl_out, rc=ctl_rc)

    assert proc.returncode == 1
    assert out["success"] is False and out["code"] == expected
    # 不回顯密碼，也不回傳 emqx ctl 的原文。
    assert NEW_PASSWORD not in proc.stdout + proc.stderr
    assert ctl_out not in proc.stdout


@pytest.mark.parametrize("stdin", ["", "short1", "x" * 65])
def test_script_rejects_bad_length_without_calling_emqx(fake_emqx, stdin):
    proc, out, argv = fake_emqx(stdin)

    assert proc.returncode == 1 and out["code"] == "invalid_password"
    assert argv == []


def test_script_unrecognised_output_is_unknown_not_a_refusal(fake_emqx):
    """看不懂的輸出＝不知道寫入了沒有：不印 JSON，讓 operator 判 unknown。"""
    proc, out, _ = fake_emqx(NEW_PASSWORD, out="something new")

    assert proc.returncode == 1
    assert out is None
    assert NEW_PASSWORD not in proc.stdout + proc.stderr


def test_script_reads_password_only_from_stdin():
    assert "read -r pw" in EMQX_RESET_ADMIN_SCRIPT
    # 不把密碼印到任何地方、不開 xtrace。
    assert "set -x" not in EMQX_RESET_ADMIN_SCRIPT
    assert 'echo "$pw"' not in EMQX_RESET_ADMIN_SCRIPT


# --- 2. service ------------------------------------------------------------------


@pytest.fixture
def k8s():
    k8s = MagicMock()
    k8s.get_pod_name.return_value = "svc-abc-emqx-0"
    return k8s


def _ok(stdout):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def test_service_execs_emqx_container_with_stdin_only(k8s):
    k8s.exec_in_pod.return_value = _ok('{"success": true, "login": "admin", "second_factor_cleared": false}\n')

    result = AppAdminPasswordService(k8s).reset_emqx_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert result == {"success": True, "login": "admin", "second_factor_cleared": False}
    selector = k8s.get_pod_name.call_args.args[1]
    assert selector == ("app.kubernetes.io/instance=svc-abc,app.kubernetes.io/name=emqx,"
                        "app.kubernetes.io/component=broker")
    kwargs = k8s.exec_in_pod.call_args.kwargs
    assert kwargs["container"] == "emqx"
    assert kwargs["command"] == ["sh", "-c", EMQX_RESET_ADMIN_SCRIPT]
    assert kwargs["input_data"] == NEW_PASSWORD
    assert kwargs["timeout"] == svc_mod.EMQX_RESET_EXEC_TIMEOUT_SECONDS
    assert NEW_PASSWORD not in json.dumps(kwargs["command"])


@pytest.mark.parametrize("side_effect", [
    KubectlException("no pod", "kubectl get", ""),
    ValueError("Namespace must start with 'paas-ws-'"),
])
def test_service_pod_lookup_failure_is_not_executed(k8s, side_effect):
    k8s.get_pod_name.side_effect = side_effect

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_emqx_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert (exc.value.step, exc.value.kind) == ("find_pod", "not_executed")
    k8s.exec_in_pod.assert_not_called()


def test_service_script_refusal_is_failed_with_code(k8s):
    k8s.exec_in_pod.side_effect = KubectlException(
        "exit 1", "kubectl exec", "command terminated with exit code 1",
        stdout='{"success": false, "code": "admin_missing", "error": "missing"}\n')

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_emqx_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert (exc.value.kind, exc.value.code) == ("failed", "admin_missing")


def test_service_unparsable_failure_is_unknown(k8s):
    k8s.exec_in_pod.side_effect = KubectlException(
        "exit 1", "kubectl exec", "unrecognised emqx ctl output", stdout="")

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_emqx_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert exc.value.kind == "unknown"


def test_service_timeout_fits_the_platform_budget():
    assert (svc_mod.EMQX_RESET_EXEC_TIMEOUT_SECONDS
            + svc_mod.APP_ADMIN_RESET_LOOKUP_SLACK_SECONDS) < 75


# --- 3. API ----------------------------------------------------------------------


def _post(body, service):
    with patch("src.main.settings.api_key", "test-emqx"), \
            patch("src.api.init.app_admin_password_service", service):
        return TestClient(app, headers={"X-API-Key": "test-emqx"}, raise_server_exceptions=False).post(
            "/api/releases/paas-ws-x/svc-abc/emqx/admin-password", json=body)


def test_api_success_without_echo(caplog):
    service = MagicMock()
    service.reset_emqx_admin.return_value = {
        "success": True, "login": "admin", "second_factor_cleared": False}
    caplog.set_level(logging.DEBUG)

    resp = _post({"new_password": NEW_PASSWORD}, service)

    assert resp.status_code == 200
    assert resp.json()["success"] is True and resp.json()["login"] == "admin"
    service.reset_emqx_admin.assert_called_once_with(
        namespace="paas-ws-x", release_name="svc-abc", new_password=NEW_PASSWORD)
    assert NEW_PASSWORD not in resp.text and NEW_PASSWORD not in caplog.text


def test_api_business_failure_is_200_with_classification():
    service = MagicMock()
    service.reset_emqx_admin.side_effect = AppAdminResetError(
        "administrator account 'admin' not found", step="reset", kind="failed", code="admin_missing")

    resp = _post({"new_password": NEW_PASSWORD}, service)

    body = resp.json()
    assert resp.status_code == 200
    assert (body["success"], body["error_kind"], body["error_code"]) == (False, "failed", "admin_missing")
