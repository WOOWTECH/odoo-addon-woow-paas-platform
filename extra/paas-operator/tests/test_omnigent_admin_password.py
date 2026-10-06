"""#1370 後續：Omnigent 管理員密碼重設（operator 端）。

omnigent 的密碼權威在它自己的 SQLite（/data/omnigent.db），chart 的 admin_password 只在
第一次開機建 admin 時用一次——所以平台原本只能說「請在服務內修改」，忘記密碼就沒救。
這裡 exec 一支腳本進 server 容器，用 omnigent 自己的 AccountStore／hash_password 寫入
（與 app 內 Members → Reset 同一條路）。

腳本的真實行為（真 omnigent image、真 SQLite、登入 API）已在沙盒驗過（見 PR）；這裡用
假的 omnigent 模組釘住它的 stdin／拒絕／輸出契約，以及 service／API 的接線。
"""

import io
import json
import logging
import sys
import types
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import app_admin_password as svc_mod
from src.services.app_admin_password import (
    OMNIGENT_RESET_ADMIN_SCRIPT,
    AppAdminPasswordService,
    AppAdminResetError,
)
from src.services.helm import KubectlException

NEW_PASSWORD = "N3w-Omn1gent-Passw0rd"


# --- 1. 腳本（假 omnigent 模組） -------------------------------------------------


class _FakeStore:
    users = {}
    writes = []

    def __init__(self, uri):
        self.uri = uri

    def get_user(self, user_id):
        return self.users.get(user_id)

    def update_password(self, user_id, password_hash):
        self.writes.append((user_id, password_hash, _FakeScope.active))


class _FakeScope:
    active = None


@contextmanager
def _target_account_scope(user_id, generation):
    _FakeScope.active = (user_id, generation)
    try:
        yield
    finally:
        _FakeScope.active = None


@pytest.fixture
def fake_omnigent(monkeypatch):
    _FakeStore.users = {"admin": types.SimpleNamespace(is_admin=True, account_generation="gen-1")}
    _FakeStore.writes = []
    mods = {
        "omnigent": types.ModuleType("omnigent"),
        "omnigent.db": types.ModuleType("omnigent.db"),
        "omnigent.db.account_authority": types.ModuleType("omnigent.db.account_authority"),
        "omnigent.server": types.ModuleType("omnigent.server"),
        "omnigent.server.accounts_store": types.ModuleType("omnigent.server.accounts_store"),
        "omnigent.server.passwords": types.ModuleType("omnigent.server.passwords"),
    }
    mods["omnigent.db.account_authority"].target_account_scope = _target_account_scope
    mods["omnigent.server.accounts_store"].SqlAlchemyAccountStore = _FakeStore
    mods["omnigent.server.passwords"].hash_password = lambda pw: "hashed:" + pw
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("DATABASE_URL", "sqlite:////data/omnigent.db")
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.delenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", raising=False)
    return _FakeStore


def _run_script(monkeypatch, capsys, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = 0
    try:
        exec(compile(OMNIGENT_RESET_ADMIN_SCRIPT, "reset_admin_password.py", "exec"),
             {"__name__": "__main__"})
    except SystemExit as e:
        code = e.code or 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    return code, json.loads(lines[-1])


def test_script_resets_admin_inside_its_account_scope(fake_omnigent, monkeypatch, capsys):
    code, out = _run_script(monkeypatch, capsys, NEW_PASSWORD + "\n")

    assert code == 0
    assert out == {"success": True, "login": "admin", "second_factor_cleared": False}
    # 與 Members → Reset 同形：target_account_scope(帳號, 既有 generation) 內寫 hash。
    assert fake_omnigent.writes == [("admin", "hashed:" + NEW_PASSWORD, ("admin", "gen-1"))]


def test_script_uses_chart_admin_username(fake_omnigent, monkeypatch, capsys):
    fake_omnigent.users = {"owner": types.SimpleNamespace(is_admin=True, account_generation="g")}
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "owner")

    code, out = _run_script(monkeypatch, capsys, NEW_PASSWORD)

    assert code == 0 and out["login"] == "owner"


@pytest.mark.parametrize("users, env, expected", [
    ({}, {}, "admin_missing"),
    ({"admin": types.SimpleNamespace(is_admin=False, account_generation="g")}, {}, "admin_inactive"),
    ({"admin": types.SimpleNamespace(is_admin=True, account_generation="g")},
     {"DATABASE_URL": ""}, "db_unsupported"),
    ({"admin": types.SimpleNamespace(is_admin=True, account_generation="g")},
     {"OMNIGENT_AUTH_PROVIDER": "header"}, "db_unsupported"),
])
def test_script_refuses_without_writing(fake_omnigent, monkeypatch, capsys, users, env, expected):
    fake_omnigent.users = users
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    code, out = _run_script(monkeypatch, capsys, NEW_PASSWORD)

    assert code == 1
    assert out["success"] is False and out["code"] == expected
    assert fake_omnigent.writes == []
    assert NEW_PASSWORD not in json.dumps(out)


def test_script_rejects_short_password_without_echo(fake_omnigent, monkeypatch, capsys):
    code, out = _run_script(monkeypatch, capsys, "short")

    assert code == 1 and out["code"] == "invalid_password"
    assert fake_omnigent.writes == []


def test_script_reads_password_only_from_stdin():
    assert "sys.stdin.read()" in OMNIGENT_RESET_ADMIN_SCRIPT
    assert "sys.argv" not in OMNIGENT_RESET_ADMIN_SCRIPT
    # 不動 account_generation：與上游 admin reset 一致，配對過的 host 不會失效。
    assert "account_generation=" not in OMNIGENT_RESET_ADMIN_SCRIPT


# --- 2. service ------------------------------------------------------------------


@pytest.fixture
def k8s():
    k8s = MagicMock()
    k8s.get_pod_name.return_value = "svc-abc-omnigent-7d9-x"
    return k8s


def _ok(stdout):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def test_service_execs_server_container_with_stdin_only(k8s):
    k8s.exec_in_pod.return_value = _ok('{"success": true, "login": "admin", "second_factor_cleared": false}\n')

    result = AppAdminPasswordService(k8s).reset_omnigent_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert result == {"success": True, "login": "admin", "second_factor_cleared": False}
    selector = k8s.get_pod_name.call_args.args[1]
    assert selector == ("app.kubernetes.io/instance=svc-abc,app.kubernetes.io/name=omnigent,"
                        "app.kubernetes.io/component=server")
    kwargs = k8s.exec_in_pod.call_args.kwargs
    assert kwargs["container"] == "server"
    assert kwargs["command"] == ["/opt/venv/bin/python", "-c", OMNIGENT_RESET_ADMIN_SCRIPT]
    assert kwargs["input_data"] == NEW_PASSWORD
    assert kwargs["timeout"] == svc_mod.OMNIGENT_RESET_EXEC_TIMEOUT_SECONDS


@pytest.mark.parametrize("side_effect", [
    KubectlException("no pod", "kubectl get", ""),
    ValueError("Namespace must start with 'paas-ws-'"),
])
def test_service_pod_lookup_failure_is_not_executed(k8s, side_effect):
    k8s.get_pod_name.side_effect = side_effect

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_omnigent_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert (exc.value.step, exc.value.kind) == ("find_pod", "not_executed")
    k8s.exec_in_pod.assert_not_called()


def test_service_script_refusal_is_failed_with_code(k8s):
    k8s.exec_in_pod.side_effect = KubectlException(
        "exit 1", "kubectl exec", "command terminated with exit code 1",
        stdout='{"success": false, "code": "admin_missing", "error": "missing"}\n')

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_omnigent_admin("paas-ws-x", "svc-abc", NEW_PASSWORD)

    assert (exc.value.kind, exc.value.code) == ("failed", "admin_missing")


def test_service_timeout_fits_the_platform_budget():
    assert (svc_mod.OMNIGENT_RESET_EXEC_TIMEOUT_SECONDS
            + svc_mod.APP_ADMIN_RESET_LOOKUP_SLACK_SECONDS) < 75


# --- 3. API ----------------------------------------------------------------------


def _post(body, service):
    with patch("src.main.settings.api_key", "test-omni"), \
            patch("src.api.init.app_admin_password_service", service):
        return TestClient(app, headers={"X-API-Key": "test-omni"}, raise_server_exceptions=False).post(
            "/api/releases/paas-ws-x/svc-abc/omnigent/admin-password", json=body)


def test_api_success_without_echo(caplog):
    service = MagicMock()
    service.reset_omnigent_admin.return_value = {
        "success": True, "login": "admin", "second_factor_cleared": False}
    caplog.set_level(logging.DEBUG)

    resp = _post({"new_password": NEW_PASSWORD}, service)

    assert resp.status_code == 200
    assert resp.json()["success"] is True and resp.json()["login"] == "admin"
    service.reset_omnigent_admin.assert_called_once_with(
        namespace="paas-ws-x", release_name="svc-abc", new_password=NEW_PASSWORD)
    assert NEW_PASSWORD not in resp.text and NEW_PASSWORD not in caplog.text


def test_api_business_failure_is_200_with_classification():
    service = MagicMock()
    service.reset_omnigent_admin.side_effect = AppAdminResetError(
        "administrator account 'admin' not found", step="reset", kind="failed", code="admin_missing")

    resp = _post({"new_password": NEW_PASSWORD}, service)

    body = resp.json()
    assert resp.status_code == 200
    assert (body["success"], body["error_kind"], body["error_code"]) == (False, "failed", "admin_missing")
