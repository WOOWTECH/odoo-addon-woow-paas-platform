"""#748 守線：/init 的 stale cleanup 只能碰「我們自管的 addon」的 record。

背景（真的發生過，不是假想）——2026-08-15 stg 租戶 my-stg-odoo-2：

- cleanup 用「掃 ``odoo.conf`` 的 addons_path 目錄」算 on_disk
- 但 Odoo core 的 656 顆模組住在 conf **沒列**、``initialize_sys_path``
  無條件納入的隱含路徑 ``<odoo package>/addons``
- cloud-odoo image 的 ``/opt/bitnami/odoo/addons`` 是指向空 PVC 的 symlink
  ⇒ addons_path 實際只解析出 selected-addons 的 3 顆
- ⇒ 617 筆 core record 被整批 unlink，租戶再也裝不了任何「依賴未安裝 core
  模組」的 addon（``mcp_server`` 依賴 ``mail``，是第一顆踩到的）

Eugene 的裁決是**域限制**而不是「把 on_disk 探針修準」：刪除候選集必須
⊆ 自管 addon 清單，非自管模組在任何探針失效情境下都不可被動到。所以本檔的
斷言主軸是「哪些 record 還在」，而不是「on_disk 算得對不對」。

測試策略——不 mock 被測邏輯：直接把 ``odoo_init.UPDATE_MODULE_LIST_SCRIPT``
（``kubectl exec`` 真的送進容器的那份 bytes）放進假 odoo 環境裡執行。
``test_shipped_command_carries_the_script_under_test`` 負責把「本檔測的東西」
和「實際送出去的東西」釘在一起——少了那條，這裡全綠也可能只是在測一份
根本沒被部署的腳本。
"""
import contextlib
import io
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from src.services import odoo_init

SCRIPT = odoo_init.UPDATE_MODULE_LIST_SCRIPT

# 沿用 #748 現場的模組分佈：core 一部分 installed 一部分 uninstalled，
# 自管 addon 同樣兩種狀態都有。差別只有 state 的那幾顆正是 bug 的指紋
# （base/base_setup/web 活下來、mail 消失）。
CORE_INSTALLED = ("base", "base_setup", "web")
CORE_UNINSTALLED = ("mail", "sale", "account", "contacts", "product")
MANAGED_ADDONS = ("ai_base_gt", "mcp_server", "odoo_ai_assistant_chatgpt_connector")

DEFAULT_STATES = {
    **{name: "installed" for name in CORE_INSTALLED},
    **{name: "uninstalled" for name in CORE_UNINSTALLED},
    "ai_base_gt": "installed",
    "mcp_server": "uninstalled",
    "odoo_ai_assistant_chatgpt_connector": "uninstalled",
}


# --- 假 Odoo 環境 ---------------------------------------------------------


def _matches(domain, name, state):
    """評估腳本用到的那幾種 domain operator。"""
    values = {"name": name, "state": state}
    for field, operator, expected in domain:
        if operator not in ("=", "!=", "in", "not in"):
            raise AssertionError(f"unsupported domain operator: {operator}")
        actual = values[field]
        if operator == "=" and actual != expected:
            return False
        if operator == "!=" and actual == expected:
            return False
        if operator == "in" and actual not in expected:
            return False
        if operator == "not in" and actual in expected:
            return False
    return True


class FakeModuleTable:
    """ir_module_module 的內容 + 磁碟探針，共用給所有 recordset。"""

    def __init__(self, states, get_modules):
        self.states = dict(states)
        self.get_modules = get_modules
        self.unlinked = []


class FakeRecordset:
    """``ir.module.module`` 的最小替身，只實作腳本用到的幾個方法。"""

    def __init__(self, table, names):
        self._table = table
        self._names = list(names)

    def __len__(self):
        return len(self._names)

    def __bool__(self):
        return bool(self._names)

    @property
    def names(self):
        return list(self._names)

    def update_list(self):
        """回傳 ``[updated, added]`` —— 與 Odoo 18 ``ir_module.update_list``
        同序（該處寫死 ``res = [0, 0]  # [update, add]``）。"""
        res = [0, 0]
        for name in self._table.get_modules():
            if name not in self._table.states:
                self._table.states[name] = "uninstalled"
                res[1] += 1
        return res

    def search(self, domain):
        names = [
            name
            for name, state in self._table.states.items()
            if _matches(domain, name, state)
        ]
        return FakeRecordset(self._table, names)

    def unlink(self):
        for name in self._names:
            del self._table.states[name]
        self._table.unlinked.extend(self._names)
        return True


class FakeEnv:
    def __init__(self, table):
        self._table = table

    def __getitem__(self, model_name):
        assert model_name == "ir.module.module", model_name
        return FakeRecordset(self._table, [])


class FakeCursor:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


class FakeRegistry:
    def __init__(self, cursor):
        self._cursor = cursor

    @contextlib.contextmanager
    def cursor(self):
        yield self._cursor


class FakeConfig(dict):
    def __init__(self, values):
        super().__init__(values)
        self.parsed_args = []

    def parse_config(self, args):
        self.parsed_args.append(list(args))


def _fake_odoo_modules(table, cursor, addons_path, db_name, expose_get_modules=True):
    odoo_mod = types.ModuleType("odoo")
    tools_mod = types.ModuleType("odoo.tools")
    api_mod = types.ModuleType("odoo.api")
    modules_mod = types.ModuleType("odoo.modules")
    module_mod = types.ModuleType("odoo.modules.module")

    tools_mod.config = FakeConfig(
        {"db_name": db_name, "addons_path": addons_path}
    )
    api_mod.Environment = lambda cr, uid, ctx: FakeEnv(table)
    # expose_get_modules=False 模擬「Odoo 本身好好的，但我們拿來當探針的
    # API 不在」——例如換 Odoo 版本後 import 路徑變了。這時 update_list()
    # 照樣成功（它走 Odoo 自己內部的 get_modules），只有 cleanup 探不到。
    if expose_get_modules:
        module_mod.get_modules = table.get_modules
        modules_mod.get_modules = table.get_modules
    modules_mod.module = module_mod

    odoo_mod.tools = tools_mod
    odoo_mod.api = api_mod
    odoo_mod.modules = modules_mod
    odoo_mod.SUPERUSER_ID = 1
    odoo_mod.registry = lambda name: FakeRegistry(cursor)

    return {
        "odoo": odoo_mod,
        "odoo.tools": tools_mod,
        "odoo.api": api_mod,
        "odoo.modules": modules_mod,
        "odoo.modules.module": module_mod,
    }


class ScriptRun:
    def __init__(self, payload, table, cursor, exit_code, stdout):
        self.payload = payload
        self.table = table
        self.cursor = cursor
        self.exit_code = exit_code
        self.stdout = stdout

    @property
    def surviving(self):
        return set(self.table.states)


def run_script(
    *,
    states=None,
    on_disk_modules=None,
    addons_path="",
    catalog_path=None,
    db_name="bitnami_odoo",
    as_main=False,
    expose_get_modules=True,
):
    """在假 odoo 環境跑腳本，回傳 payload 與跑完後的 module table。

    Args:
        states: 起始的 ``{module_name: state}``。
        on_disk_modules: ``get_modules()`` 要回傳什麼。
        addons_path: ``odoo.conf`` 的 addons_path 字串（舊掃目錄邏輯會讀）。
        catalog_path: 自管 addon catalog 的路徑；``None`` 表示不覆寫腳本預設值。
        as_main: True 時以 ``__name__ == "__main__"`` 執行，驗 entrypoint 本身。
        expose_get_modules: False 時假 odoo 不提供 ``get_modules``，模擬探針 API 不存在。
    """
    listing = list(on_disk_modules or [])
    get_modules = lambda: list(listing)  # noqa: E731

    table = FakeModuleTable(
        DEFAULT_STATES if states is None else states, get_modules
    )
    cursor = FakeCursor()
    fake_modules = _fake_odoo_modules(
        table, cursor, addons_path, db_name, expose_get_modules
    )

    namespace = {"__name__": "__main__" if as_main else "_update_module_list"}
    stdout = io.StringIO()
    exit_code = None

    with patch.dict(sys.modules, fake_modules), contextlib.redirect_stdout(stdout):
        try:
            exec(compile(SCRIPT, "update_module_list.py", "exec"), namespace)
            if not as_main:
                if catalog_path is not None:
                    namespace["MANAGED_CATALOG_PATH"] = str(catalog_path)
                namespace["main"]()
        except SystemExit as exc:
            exit_code = exc.code

    # 與 odoo_init._update_module_list 同一套解析：取最後一個非空行的 JSON
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else None
    return ScriptRun(payload, table, cursor, exit_code, stdout.getvalue())


@pytest.fixture
def catalog(tmp_path):
    """寫一份自管 addon catalog，回傳 (path, write) 供測試改內容。"""
    path = tmp_path / "catalog.json"

    def write(names_or_raw):
        if isinstance(names_or_raw, str):
            path.write_text(names_or_raw, encoding="utf-8")
        else:
            path.write_text(
                json.dumps({name: {"name": name} for name in names_or_raw}),
                encoding="utf-8",
            )
        return path

    write(MANAGED_ADDONS)
    return path, write


@pytest.fixture
def selected_addons_dir(tmp_path):
    """模擬 cloud-odoo 的 selected-addons 目錄（initContainer 複製過去的那份）。"""

    def build(names):
        root = tmp_path / "selected-addons"
        root.mkdir(exist_ok=True)
        for name in names:
            (root / name).mkdir(exist_ok=True)
        # 與租戶 conf 一致：第一段是指向空 PVC 的 symlink，解析不出任何 addon
        return f"/opt/bitnami/odoo/addons,{root}"

    return build


# --- #748 (a) RED case ----------------------------------------------------


def test_core_module_records_survive_when_addons_path_excludes_core(
    catalog, selected_addons_dir
):
    """conf 的 addons_path 完全不含 core 模組時，core 的 record 必須存活。

    這就是 #748 的現場：``mail`` 是 uninstalled 又不在 addons_path 裡，
    舊邏輯會把它連同其他 616 顆 core record 一起 unlink。
    """
    catalog_path, _ = catalog

    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, *MANAGED_ADDONS],
        addons_path=selected_addons_dir(MANAGED_ADDONS),
        catalog_path=catalog_path,
    )

    assert run.payload["success"] is True
    assert "mail" in run.surviving
    assert run.table.states["mail"] == "uninstalled"
    for name in (*CORE_INSTALLED, *CORE_UNINSTALLED):
        assert name in run.surviving, f"core module record 被刪了: {name}"
    assert run.table.unlinked == []
    assert run.payload["cleaned"] == 0


def test_mcp_server_dependencies_all_survive(catalog, selected_addons_dir):
    """#748 的判別式：mcp_server 的四個依賴不能一活一死。

    base/base_setup/web（installed）本來就活著，mail（uninstalled）才是
    ``("state", "=", "uninstalled")`` filter 的受害者——四顆一起檢查，
    才驗得到「差別只有 state」這個指紋消失了。
    """
    catalog_path, _ = catalog

    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, *MANAGED_ADDONS],
        addons_path=selected_addons_dir(MANAGED_ADDONS),
        catalog_path=catalog_path,
    )

    for dependency in ("base", "base_setup", "mail", "web"):
        assert dependency in run.surviving, f"mcp_server 的依賴不見了: {dependency}"


# --- #748 (b) 陽性 case ---------------------------------------------------


def test_deselected_managed_addon_record_is_cleaned(catalog, selected_addons_dir):
    """取消勾選的自管 addon，uninstalled record 要被清掉。"""
    catalog_path, _ = catalog
    still_selected = ("ai_base_gt", "mcp_server")

    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, *still_selected],
        addons_path=selected_addons_dir(still_selected),
        catalog_path=catalog_path,
    )

    assert run.table.unlinked == ["odoo_ai_assistant_chatgpt_connector"]
    assert "odoo_ai_assistant_chatgpt_connector" not in run.surviving
    assert run.payload["cleaned"] == 1
    assert "mail" in run.surviving


def test_installed_managed_addon_is_never_cleaned(catalog, selected_addons_dir):
    """自管但還 installed 的 addon 不刪——那要走 uninstall 前置檢查流程。"""
    catalog_path, _ = catalog
    states = dict(DEFAULT_STATES)
    states["odoo_ai_assistant_chatgpt_connector"] = "installed"

    run = run_script(
        states=states,
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, "ai_base_gt", "mcp_server"],
        addons_path=selected_addons_dir(("ai_base_gt", "mcp_server")),
        catalog_path=catalog_path,
    )

    assert "odoo_ai_assistant_chatgpt_connector" in run.surviving
    assert run.payload["cleaned"] == 0


# --- #748 (2) 白名單域限制：探針失效也不能碰別人的 record -----------------


def test_empty_on_disk_probe_cannot_touch_foreign_records(
    catalog, selected_addons_dir
):
    """磁碟探針壞成空集合（＝#748 現場的形狀）時，只有自管 record 會被清。

    這條驗的是「域限制」而不是「探針準不準」：探針回空集合是最壞情況，
    此時傷害必須完全侷限在自管清單內。
    """
    catalog_path, _ = catalog

    run = run_script(
        on_disk_modules=[],
        addons_path=selected_addons_dir(()),
        catalog_path=catalog_path,
    )

    for name in (*CORE_INSTALLED, *CORE_UNINSTALLED):
        assert name in run.surviving, f"非自管 record 被刪了: {name}"
    assert sorted(run.table.unlinked) == [
        "mcp_server",
        "odoo_ai_assistant_chatgpt_connector",
    ]


def test_cleanup_skipped_when_on_disk_probe_unavailable(catalog, selected_addons_dir):
    """探針 API 不存在時跳過 cleanup，但 update_list 的成果照樣 commit。

    Odoo 本身沒壞（``update_list()`` 正常跑完），只是我們拿來當探針的
    ``get_modules`` import 不到。這種半殘狀態最危險——不能因為「探不到磁碟」
    就把整批 record 當成不存在。
    """
    catalog_path, _ = catalog

    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, *MANAGED_ADDONS],
        addons_path=selected_addons_dir(MANAGED_ADDONS),
        catalog_path=catalog_path,
        expose_get_modules=False,
    )

    assert run.payload["success"] is True
    assert run.payload["cleaned"] == 0
    assert "cleanup_skipped" in run.payload
    assert run.table.unlinked == []
    assert run.cursor.commits == 1


# --- #748 (3) fail-safe：自管清單讀不到就跳過 -----------------------------


def test_cleanup_skipped_when_catalog_missing(tmp_path, selected_addons_dir):
    """catalog 讀不到時跳過 cleanup，不得退化成大範圍刪除。

    連「真的該被清掉」的自管 record 都要留著——寧可留殘影，不可誤刪。
    """
    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, "ai_base_gt"],
        addons_path=selected_addons_dir(("ai_base_gt",)),
        catalog_path=tmp_path / "does-not-exist.json",
    )

    assert run.payload["success"] is True
    assert run.payload["cleaned"] == 0
    assert "cleanup_skipped" in run.payload
    assert run.table.unlinked == []
    assert "mail" in run.surviving
    assert "odoo_ai_assistant_chatgpt_connector" in run.surviving


@pytest.mark.parametrize(
    "raw",
    [
        "{ not json at all",
        "[]",
        "{}",
        '"a string"',
    ],
    ids=["malformed", "list", "empty-dict", "scalar"],
)
def test_cleanup_skipped_when_catalog_unusable(catalog, selected_addons_dir, raw):
    """catalog 格式不對／空的一律當「不知道自管清單」，跳過 cleanup。"""
    catalog_path, write = catalog
    write(raw)

    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, "ai_base_gt"],
        addons_path=selected_addons_dir(("ai_base_gt",)),
        catalog_path=catalog_path,
    )

    assert run.payload["success"] is True
    assert run.payload["cleaned"] == 0
    assert "cleanup_skipped" in run.payload
    assert run.table.unlinked == []


# --- 回傳值與 entrypoint --------------------------------------------------


def test_added_and_updated_follow_odoo_return_order(catalog, selected_addons_dir):
    """Odoo 的 ``update_list()`` 回 ``[updated, added]``，不是 ``[added, updated]``。

    位置弄反時 operator 日誌會把 641 筆新增報成 "updated=641, added=1"，
    #748 調查當下就被這行誤導過。
    """
    catalog_path, _ = catalog
    states = {"base": "installed"}

    run = run_script(
        states=states,
        on_disk_modules=["base", "mail", "web"],
        addons_path=selected_addons_dir(()),
        catalog_path=catalog_path,
    )

    assert run.payload["added"] == 2  # mail + web 是新發現的
    assert run.payload["updated"] == 0


def test_missing_db_name_fails_loudly(catalog, selected_addons_dir):
    catalog_path, _ = catalog

    run = run_script(
        db_name="",
        on_disk_modules=[],
        addons_path=selected_addons_dir(()),
        catalog_path=catalog_path,
    )

    assert run.payload["success"] is False
    assert run.exit_code == 1


def test_script_entrypoint_prints_json_when_run_as_main(selected_addons_dir):
    """腳本是用 ``python3 -c`` 跑的 → ``__name__ == "__main__"``，
    entrypoint 那一行不能漏。"""
    run = run_script(
        on_disk_modules=[*CORE_INSTALLED, *CORE_UNINSTALLED, *MANAGED_ADDONS],
        addons_path=selected_addons_dir(MANAGED_ADDONS),
        as_main=True,
    )

    assert run.payload is not None
    assert run.payload["success"] is True


# --- 把「本檔測的腳本」與「實際送進容器的腳本」釘在一起 -------------------


def test_shipped_command_carries_the_script_under_test():
    """``kubectl exec`` 送出去的 argv 必須就是本檔測的那份 bytes。

    沒有這條，上面每一條都可能是在測一份根本沒被部署的腳本（假綠）。
    """
    service = odoo_init.OdooInitService(k8s_service=MagicMock())
    service.k8s.exec_in_pod.return_value = MagicMock(
        stdout=json.dumps(
            {"success": True, "added": 0, "updated": 0, "cleaned": 0}
        )
    )

    service._update_module_list("ns", "pod", "container")

    command = service.k8s.exec_in_pod.call_args.kwargs["command"]
    assert command[:2] == ["/opt/bitnami/odoo/venv/bin/python3", "-c"]
    assert command[2] == SCRIPT


def test_script_only_imports_stdlib_and_odoo():
    """腳本在租戶容器裡跑，import 到 operator 自己的模組會直接 ImportError。"""
    import ast

    tree = ast.parse(SCRIPT)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & {"src", "fastapi", "pydantic"}, imported
