"""#1370／#1186：Odoo／n8n 管理員密碼重設（operator 端）。

三層：

1. Odoo 腳本本體——把 ``ODOO_RESET_ADMIN_SCRIPT``（``kubectl exec`` 真的送進容器的
   那份 bytes）放進假 odoo 環境執行，斷言「失敗時什麼都不寫、成功才 commit」。
2. ``AppAdminPasswordService``——只換掉 KubernetesService 邊界：新密碼只走 stdin、
   失敗分類（not_executed／failed／unknown）。
3. API——打真的 FastAPI route：回應與 log 一律不含新密碼。

n8n 腳本（node）的真實行為由 tests/integration/test_app_admin_password_reset.py 在真
n8n 容器（sqlite 與 postgres）裡驗；這裡只釘它的 stdin 契約。
"""
import io
import json
import logging
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import app_admin_password as svc_mod
from src.services.app_admin_password import (
    N8N_RESET_OWNER_SCRIPT,
    ODOO_RESET_ADMIN_SCRIPT,
    AppAdminPasswordService,
    AppAdminResetError,
)
from src.services.helm import KubectlException

NEW_PASSWORD = "N3w-Adm1n-Passw0rd-1370"


def _load_odoo_script():
    namespace = {"__name__": "odoo_reset_admin_password_under_test"}
    exec(compile(ODOO_RESET_ADMIN_SCRIPT, "reset_admin_password.py", "exec"), namespace)
    return namespace


# --- 1. Odoo 腳本 --------------------------------------------------------------


class FakeUser:
    def __init__(self, active=True, login="admin@example.com", totp=None, has_totp_field=True):
        self.active = active
        self.login = login
        self.totp_secret = totp
        self._fields = {"password": None, "login": None}
        if has_totp_field:
            self._fields["totp_secret"] = None
        self.writes = []
        self.revoked = 0

    def __bool__(self):
        return True

    def write(self, vals):
        self.writes.append(dict(vals))
        if "totp_secret" in vals:
            self.totp_secret = vals["totp_secret"]

    def _revoke_all_devices(self):
        self.revoked += 1


class FakeEnv:
    def __init__(self, user):
        self.user = user
        self.refs = []

    def ref(self, xmlid, raise_if_not_found=True):
        self.refs.append((xmlid, raise_if_not_found))
        return self.user


def test_odoo_script_resets_password_and_clears_totp():
    script = _load_odoo_script()
    user = FakeUser(totp="SECRET")
    env = FakeEnv(user)

    payload = script["reset_admin"](env, NEW_PASSWORD)

    assert payload == {"success": True, "login": "admin@example.com", "second_factor_cleared": True}
    # xmlid、不寫死 id=2；找不到時不 raise（自己回報 admin_missing）。
    assert env.refs == [("base.user_admin", False)]
    assert user.writes == [{"password": NEW_PASSWORD}, {"totp_secret": False}]
    assert user.revoked == 1


def test_odoo_script_without_auth_totp_only_sets_password():
    script = _load_odoo_script()
    user = FakeUser(has_totp_field=False)

    payload = script["reset_admin"](FakeEnv(user), NEW_PASSWORD)

    assert payload["success"] is True and payload["second_factor_cleared"] is False
    assert user.writes == [{"password": NEW_PASSWORD}]
    assert user.revoked == 0


@pytest.mark.parametrize("user, code", [
    (None, "admin_missing"),
    (FakeUser(active=False), "admin_inactive"),
])
def test_odoo_script_refuses_without_writing(user, code):
    script = _load_odoo_script()

    class Missing:
        def __bool__(self):
            return False

    payload = script["reset_admin"](FakeEnv(user if user is not None else Missing()), NEW_PASSWORD)

    assert payload["success"] is False and payload["code"] == code
    if user is not None:
        assert user.writes == []
    assert NEW_PASSWORD not in json.dumps(payload)


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_odoo_script_rejects_password_length_without_echo(password):
    script = _load_odoo_script()
    user = FakeUser()

    payload = script["reset_admin"](FakeEnv(user), password)

    assert payload["code"] == "invalid_password"
    assert user.writes == []
    assert password not in json.dumps(payload)


@pytest.mark.parametrize("raw, expected", [
    (NEW_PASSWORD, NEW_PASSWORD),
    (NEW_PASSWORD + "\n", NEW_PASSWORD),
    (NEW_PASSWORD + "\r\n", NEW_PASSWORD),
    (" " + NEW_PASSWORD + " ", " " + NEW_PASSWORD + " "),
])
def test_odoo_script_reads_password_from_stdin_verbatim(raw, expected):
    script = _load_odoo_script()
    assert script["read_new_password"](io.StringIO(raw)) == expected


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append("close")
        return False

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")


def _fake_odoo(user, log):
    config = {"db_name": "tenant"}
    odoo = types.ModuleType("odoo")
    odoo.SUPERUSER_ID = 1
    odoo.tools = types.SimpleNamespace(config=MagicMock())
    odoo.tools.config.__getitem__ = lambda self, key: config[key]
    odoo.tools.config.parse_config = lambda args: log.append(("parse_config", tuple(args)))
    odoo.registry = lambda db: types.SimpleNamespace(cursor=lambda: _FakeCursor(log))
    odoo.api = types.SimpleNamespace(Environment=lambda cr, uid, ctx: FakeEnv(user))
    return odoo


@pytest.mark.parametrize("user, committed", [(FakeUser(), True), (FakeUser(active=False), False)])
def test_odoo_script_main_commits_only_on_success(user, committed, monkeypatch, capsys):
    log = []
    monkeypatch.setitem(sys.modules, "odoo", _fake_odoo(user, log))
    monkeypatch.setattr(sys, "stdin", io.StringIO(NEW_PASSWORD))
    script = _load_odoo_script()

    if committed:
        script["main"]()
    else:
        with pytest.raises(SystemExit):
            script["main"]()

    out = capsys.readouterr().out
    assert json.loads(out.strip().splitlines()[-1])["success"] is committed
    assert NEW_PASSWORD not in out
    assert ("parse_config", ("-c", "/etc/odoo/odoo.conf", "--no-http")) in log
    assert ("commit" in log) is committed
    assert ("rollback" in log) is (not committed)


def test_odoo_script_main_reports_exception_class_only(monkeypatch, capsys):
    """passlib／ORM 的例外字串不歸我們管，只回報類別名。"""
    user = FakeUser()

    def boom(vals):
        raise ValueError("leaky detail " + vals.get("password", ""))

    user.write = boom
    monkeypatch.setitem(sys.modules, "odoo", _fake_odoo(user, []))
    monkeypatch.setattr(sys, "stdin", io.StringIO(NEW_PASSWORD))

    with pytest.raises(SystemExit):
        _load_odoo_script()["main"]()

    out = capsys.readouterr().out
    assert json.loads(out.strip())["error"] == "ValueError while resetting"
    assert NEW_PASSWORD not in out


# --- n8n 腳本的 stdin 契約 -------------------------------------------------------


def test_n8n_script_reads_password_only_from_stdin():
    assert "process.stdin" in N8N_RESET_OWNER_SCRIPT
    assert "process.argv" not in N8N_RESET_OWNER_SCRIPT
    # owner 必須恰好一列；MFA 欄位清成與 `n8n mfa:disable` 相同的值。
    assert "owner_ambiguous" in N8N_RESET_OWNER_SCRIPT
    assert '"mfaRecoveryCodes" = \'\'' in N8N_RESET_OWNER_SCRIPT


# --- 2. Service ------------------------------------------------------------------


def _ok(stdout):
    result = MagicMock()
    result.stdout = stdout
    return result


@pytest.fixture
def k8s():
    k8s = MagicMock()
    k8s.get_pod_name.return_value = "pod-0"
    return k8s


@pytest.mark.parametrize("method, container, argv0", [
    ("reset_odoo_admin", "odoo", "python3"),
    ("reset_n8n_owner", "n8n", "node"),
])
def test_service_sends_password_on_stdin_only(k8s, method, container, argv0):
    k8s.exec_in_pod.return_value = _ok(
        'odoo log line\n{"success": true, "login": "a@b.c", "second_factor_cleared": true}\n')

    result = getattr(AppAdminPasswordService(k8s), method)("paas-ws-x", "rel", NEW_PASSWORD)

    assert result == {"success": True, "login": "a@b.c", "second_factor_cleared": True}
    kwargs = k8s.exec_in_pod.call_args.kwargs
    assert kwargs["input_data"] == NEW_PASSWORD
    assert kwargs["container"] == container
    assert kwargs["command"][0] == argv0
    assert all(NEW_PASSWORD not in part for part in kwargs["command"])
    script = ODOO_RESET_ADMIN_SCRIPT if argv0 == "python3" else N8N_RESET_OWNER_SCRIPT
    assert kwargs["command"][-1] == script


def test_service_pod_not_found_is_not_executed(k8s):
    k8s.get_pod_name.side_effect = KubectlException("no pod", "kubectl get", "")

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_n8n_owner("paas-ws-x", "rel", NEW_PASSWORD)

    assert (exc.value.step, exc.value.kind) == ("find_pod", "not_executed")
    k8s.exec_in_pod.assert_not_called()


def test_service_namespace_outside_operator_scope_is_not_executed(k8s):
    """validate_namespace 的 ValueError：還沒碰任何東西，不能落成 500 unknown。"""
    k8s.get_pod_name.side_effect = ValueError("Namespace must start with 'paas-ws-'")

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_odoo_admin("kube-system", "rel", NEW_PASSWORD)

    assert exc.value.kind == "not_executed"
    k8s.exec_in_pod.assert_not_called()


def test_service_script_failure_is_failed_with_code(k8s):
    k8s.exec_in_pod.side_effect = KubectlException(
        "exit 1", "kubectl exec", "command terminated with exit code 1",
        stdout='{"success": false, "code": "admin_inactive", "error": "archived"}\n')

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_odoo_admin("paas-ws-x", "rel", NEW_PASSWORD)

    assert (exc.value.kind, exc.value.code, str(exc.value)) == ("failed", "admin_inactive", "archived")


@pytest.mark.parametrize("side_effect", [
    KubectlException("timeout", "kubectl exec", "", timed_out=True,
                     stdout='{"success": false, "code": "admin_missing"}'),
    KubectlException("exit 137", "kubectl exec", "OOMKilled"),
])
def test_service_ambiguous_exec_failure_is_unknown(k8s, side_effect):
    k8s.exec_in_pod.side_effect = side_effect

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_odoo_admin("paas-ws-x", "rel", NEW_PASSWORD)

    assert exc.value.kind == "unknown"
    assert "OOMKilled" not in str(exc.value)


@pytest.mark.parametrize("stdout", ["", "not json", '["list"]'])
def test_service_unparsable_output_is_unknown(k8s, stdout):
    k8s.exec_in_pod.return_value = _ok(stdout)

    with pytest.raises(AppAdminResetError) as exc:
        AppAdminPasswordService(k8s).reset_n8n_owner("paas-ws-x", "rel", NEW_PASSWORD)

    assert exc.value.kind == "unknown"


def test_service_timeouts_fit_the_budget():
    assert svc_mod.ODOO_RESET_EXEC_TIMEOUT_SECONDS + svc_mod.APP_ADMIN_RESET_LOOKUP_SLACK_SECONDS < 75
    assert svc_mod.N8N_RESET_EXEC_TIMEOUT_SECONDS + svc_mod.APP_ADMIN_RESET_LOOKUP_SLACK_SECONDS < 75


# --- 3. API ----------------------------------------------------------------------


def _post(path, body, service):
    with patch("src.main.settings.api_key", "test-1370"), \
            patch("src.api.init.app_admin_password_service", service):
        return TestClient(app, headers={"X-API-Key": "test-1370"}, raise_server_exceptions=False).post(
            f"/api/releases/paas-ws-x/rel/{path}", json=body)


@pytest.mark.parametrize("path, method", [
    ("odoo/admin-password", "reset_odoo_admin"),
    ("n8n/owner-password", "reset_n8n_owner"),
])
def test_api_success(path, method, caplog):
    service = MagicMock()
    getattr(service, method).return_value = {"success": True, "login": "a@b.c", "second_factor_cleared": False}
    caplog.set_level(logging.DEBUG)

    resp = _post(path, {"new_password": NEW_PASSWORD}, service)

    assert resp.status_code == 200
    assert resp.json()["success"] is True and resp.json()["login"] == "a@b.c"
    getattr(service, method).assert_called_once_with(
        namespace="paas-ws-x", release_name="rel", new_password=NEW_PASSWORD)
    assert NEW_PASSWORD not in resp.text
    assert NEW_PASSWORD not in caplog.text


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_api_rejects_length_without_echo(password):
    service = MagicMock()

    resp = _post("odoo/admin-password", {"new_password": password}, service)

    body = resp.json()
    assert resp.status_code == 200
    assert (body["success"], body["error_kind"], body["error_code"]) == (False, "not_executed", "invalid_password")
    assert password not in resp.text
    service.reset_odoo_admin.assert_not_called()


def test_api_business_failure_is_200_with_classification():
    service = MagicMock()
    service.reset_n8n_owner.side_effect = AppAdminResetError(
        "no n8n owner account found", step="reset", kind="failed", code="admin_missing")

    resp = _post("n8n/owner-password", {"new_password": NEW_PASSWORD}, service)

    body = resp.json()
    assert resp.status_code == 200
    assert (body["success"], body["error_kind"], body["error_code"], body["error_step"]) == (
        False, "failed", "admin_missing", "reset")


def test_api_unexpected_error_is_500_unknown_without_echo(caplog):
    caplog.set_level(logging.DEBUG)
    service = MagicMock()
    service.reset_odoo_admin.side_effect = RuntimeError("detail mentioning " + NEW_PASSWORD)

    resp = _post("odoo/admin-password", {"new_password": NEW_PASSWORD}, service)

    assert resp.status_code == 500
    assert resp.json()["detail"]["error_kind"] == "unknown"
    assert NEW_PASSWORD not in resp.text
    assert NEW_PASSWORD not in caplog.text


def test_api_requires_api_key():
    with patch("src.main.settings.api_key", "test-1370"):
        resp = TestClient(app).post("/api/releases/paas-ws-x/rel/odoo/admin-password",
                                    json={"new_password": NEW_PASSWORD})
    assert resp.status_code == 401
