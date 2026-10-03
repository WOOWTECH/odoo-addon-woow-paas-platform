"""#1355：新開租戶 Odoo 的台灣在地化預設（zh_TW、Asia/Taipei、公司國家 TW、幣別 TWD）。

兩層：

* 腳本本體——直接執行 ``odoo_init.LOCALIZE_TENANT_SCRIPT``（``kubectl exec`` 真的送進
  容器的那份 bytes）裡的 ``localize(env)``，餵假的 Odoo env。守的是「只改出廠值、只跑
  一次、已使用中的 DB 不動、有會計分錄不換幣別」。真 Odoo 18 上的行為另在沙盒實測
  （PR 描述）。
* operator 端——在地化是 init 的第 4 步，**任何失敗都不得讓 init 失敗**，結果經回應的
  ``localization`` 欄位帶給平台。
"""
import ast
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import odoo_init
from src.services.helm import KubectlException
from src.services.odoo_init import LOCALIZE_TENANT_SCRIPT, LOCALIZE_TIMEOUT, OdooInitService

MARKER = "woow_paas_platform.tenant_localized"


# --- 假 Odoo env -----------------------------------------------------------


class Rec:
    """最小的單筆 record：屬性讀寫＋``write``／``sudo``／比較。"""

    def __init__(self, xmlid, **fields):
        self.xmlid = xmlid
        self.__dict__.update(fields)
        self.writes = []

    def sudo(self):
        return self

    def write(self, vals):
        self.writes.append(dict(vals))
        for key, value in vals.items():
            if key == "country_id":
                value = FakeEnv.current.records_by_id[value]
            if key == "currency_id":
                value = FakeEnv.current.records_by_id[value]
            setattr(self, key, value)
        return True

    @property
    def ids(self):
        return [self.id]

    def __bool__(self):
        return True

    def __eq__(self, other):
        return isinstance(other, Rec) and other.xmlid == self.xmlid

    def __hash__(self):
        return hash(self.xmlid)


class Params:
    def __init__(self, env):
        self.env = env

    def sudo(self):
        return self

    def get_param(self, key, default=False):
        return self.env.params.get(key, default)

    def set_param(self, key, value):
        self.env.params[key] = value


class Counter:
    def __init__(self, count):
        self.count = count

    def sudo(self):
        return self

    def search_count(self, domain, limit=None):
        return self.count


class Langs:
    def __init__(self, env):
        self.env = env

    def with_context(self, **ctx):
        assert ctx == {"active_test": False}, "要看得到未啟用的語言"
        return self

    def search(self, domain, limit=None):
        assert domain == [("code", "=", "zh_TW")]
        return self.env.zh_tw or EmptyRecordset()


class EmptyRecordset:
    def __bool__(self):
        return False


class LangWizard:
    def __init__(self, env):
        self.env = env

    def create(self, vals):
        env = self.env
        wizard = MagicMock()

        def lang_install():
            env.installed_langs.append(vals)
            env.zh_tw.active = True

        wizard.lang_install.side_effect = lang_install
        return wizard


class FakeEnv:
    current = None

    def __init__(self, *, admin_lang="en_US", admin_tz=False, country=None,
                 currency="USD", zh_active=False, logins=0, move_lines=None,
                 params=None, zh_present=True):
        FakeEnv.current = self
        self.params = dict(params or {})
        self.installed_langs = []
        self.tw = Rec("base.tw", id=233)
        self.jp = Rec("base.jp", id=113)
        self.usd = Rec("base.USD", id=1, active=True)
        self.twd = Rec("base.TWD", id=140, active=False)
        self.eur = Rec("base.EUR", id=125, active=True)
        self.records_by_id = {r.id: r for r in (self.tw, self.jp, self.usd, self.twd, self.eur)}
        self.zh_tw = Rec("base.lang_zh_TW", id=50, active=zh_active) if zh_present else None
        self.admin = Rec("base.user_admin", id=2, lang=admin_lang, tz=admin_tz)
        currency_rec = {"USD": self.usd, "TWD": self.twd, "EUR": self.eur}[currency]
        self.company = Rec("base.main_company", id=1,
                           country_id=country or False, currency_id=currency_rec)
        self.logins = logins
        self.move_lines = move_lines  # None ＝ 沒裝 account

    def __contains__(self, model):
        return model == "account.move.line" and self.move_lines is not None

    def __getitem__(self, model):
        return {
            "ir.config_parameter": lambda: Params(self),
            "res.users.log": lambda: Counter(self.logins),
            "res.lang": lambda: Langs(self),
            "base.language.install": lambda: LangWizard(self),
            "account.move.line": lambda: Counter(self.move_lines or 0),
        }[model]()

    def ref(self, xmlid, raise_if_not_found=True):
        table = {r.xmlid: r for r in (self.tw, self.jp, self.usd, self.twd, self.eur,
                                       self.admin, self.company)}
        if xmlid not in table and raise_if_not_found:
            raise ValueError(xmlid)
        return table.get(xmlid)


def _localize(env):
    namespace = {"__name__": "localize_tenant_under_test"}
    exec(compile(LOCALIZE_TENANT_SCRIPT, "localize_tenant.py", "exec"), namespace)
    return namespace["localize"](env)


# --- 腳本行為 ---------------------------------------------------------------


def test_fresh_database_gets_taiwan_defaults_once():
    env = FakeEnv()
    result = _localize(env)

    assert result == {"success": True, "changes": [
        "lang_installed", "admin_lang", "admin_tz", "company_country", "company_currency"]}
    assert env.installed_langs == [{"lang_ids": [(6, 0, [50])], "overwrite": False}]
    assert (env.admin.lang, env.admin.tz) == ("zh_TW", "Asia/Taipei")
    assert env.company.country_id == env.tw
    assert env.company.currency_id == env.twd
    assert env.twd.active is True, "TWD 出廠是停用的，要先啟用才能當公司幣別"
    assert env.params[MARKER] == "1"


def test_marker_makes_rerun_a_no_op():
    """init 會在每次 redeploy／改 addon 重跑：標記存在就整段不碰。"""
    env = FakeEnv(params={MARKER: "1"})
    result = _localize(env)

    assert result["skipped"] == "already localized"
    assert env.admin.writes == [] and env.company.writes == []
    assert env.installed_langs == []


def test_database_in_use_is_left_alone():
    """本功能上線前開的服務、還原回來的 DB：有人登入過就不改，只補標記。"""
    env = FakeEnv(logins=3)
    result = _localize(env)

    assert "in use" in result["skipped"]
    assert env.admin.writes == [] and env.company.writes == []
    assert env.installed_langs == []
    assert env.params[MARKER] == "skipped-in-use"


def test_values_the_tenant_already_changed_are_kept():
    """只改出廠值：語言、時區、國家、幣別任何一項不是出廠值就不動那一項。"""
    env = FakeEnv(admin_lang="ja_JP", admin_tz="Asia/Tokyo", country=None, currency="EUR",
                  zh_active=True)
    env.company.country_id = env.jp
    result = _localize(env)

    assert result == {"success": True, "changes": []}
    assert env.installed_langs == [], "zh_TW 已啟用就不再跑安裝精靈"
    assert (env.admin.lang, env.admin.tz) == ("ja_JP", "Asia/Tokyo")
    assert env.company.country_id == env.jp
    assert env.company.currency_id == env.eur
    assert env.params[MARKER] == "1"


def test_currency_is_not_switched_once_journal_items_exist():
    env = FakeEnv(move_lines=5)
    result = _localize(env)

    assert "company_currency" not in result["changes"]
    assert env.company.currency_id == env.usd
    assert "company_country" in result["changes"], "其他項目照常"


def test_currency_switch_when_account_installed_but_empty():
    env = FakeEnv(move_lines=0)
    assert "company_currency" in _localize(env)["changes"]


def test_missing_language_fails_without_marker():
    """zh_TW 不在 res.lang（異常 image）：回失敗、不寫標記，下次 init 再試。"""
    env = FakeEnv(zh_present=False)
    result = _localize(env)

    assert result["success"] is False
    assert MARKER not in env.params
    assert env.admin.writes == []


def test_script_follows_the_exec_payload_contract():
    """package docstring 的限制：只用 stdlib＋odoo、不碰 __file__、讀同一份 conf。"""
    tree = ast.parse(LOCALIZE_TENANT_SCRIPT)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "__file__" not in names
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module.split(".")[0])
    assert imported <= {"json", "sys", "odoo"}, imported
    assert 'ODOO_CONF_PATH = "/etc/odoo/odoo.conf"' in LOCALIZE_TENANT_SCRIPT


# --- operator：在地化不得讓 init 失敗 -----------------------------------------


def _k8s(localize_exec):
    k8s = MagicMock()
    k8s.get_pod_name.return_value = "web-0"
    effects = [
        MagicMock(stdout=json.dumps({"ready": True, "version": "18.0"})),
        MagicMock(stdout=json.dumps({"success": True, "added": 1, "updated": 0, "cleaned": 0})),
        localize_exec,
    ]
    k8s.exec_in_pod.side_effect = effects
    return k8s


def test_localization_exec_ships_the_script_with_its_own_timeout():
    k8s = _k8s(MagicMock(stdout='log\n{"success": true, "changes": ["admin_lang"]}\n'))
    result = OdooInitService(k8s).initialize("paas-ws-test1355", "tenant")

    call = k8s.exec_in_pod.call_args_list[2]
    assert call.kwargs["command"] == ["python3", "-c", LOCALIZE_TENANT_SCRIPT]
    assert call.kwargs["timeout"] == LOCALIZE_TIMEOUT
    assert call.kwargs["container"] == "odoo" and call.kwargs["pod_name"] == "web-0"
    assert result["success"] is True
    assert result["localization"] == {"success": True, "changes": ["admin_lang"]}


@pytest.mark.parametrize("localize_exec, error_part", [
    (KubectlException("exit 1", "exec", "", stdout='{"success": false, "error": "boom"}'), "boom"),
    (KubectlException("timed out", "exec", "", timed_out=True,
                      stdout='{"success": false, "error": "stale"}'), "exec failed"),
    (MagicMock(stdout="no json here"), "no JSON"),
    (RuntimeError("kaboom"), "kaboom"),
], ids=["script-failed", "timeout-output-untrusted", "garbage-output", "unexpected"])
def test_localization_failure_never_fails_init(localize_exec, error_part):
    result = OdooInitService(_k8s(localize_exec)).initialize("paas-ws-test1355", "tenant")

    assert result["success"] is True, "在地化不是服務可用的前提"
    assert result["modules_added"] == 1
    assert result["localization"]["success"] is False
    assert error_part in result["localization"]["error"]


def test_init_odoo_response_carries_localization():
    k8s = _k8s(MagicMock(stdout='{"success": true, "skipped": "already localized", "changes": []}'))
    with patch("src.main.settings.api_key", "test-1355"), \
            patch("src.api.init.odoo_init_service.k8s", k8s), \
            patch("src.services.odoo_init.time.sleep"):
        response = TestClient(app, headers={"X-API-Key": "test-1355"}).post(
            "/api/releases/paas-ws-test1355/tenant/init/odoo")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["localization"] == {
        "success": True, "skipped": "already localized", "changes": []}


def test_script_is_loaded_at_import_time():
    """與 update_module_list 同：檔案不見要在 operator 啟動當下就炸。"""
    assert odoo_init.LOCALIZE_TENANT_SCRIPT.startswith('"""#1355')
