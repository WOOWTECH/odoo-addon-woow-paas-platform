"""重新整理 Odoo module list，並清掉自管 addon 留下的 stale record。

由 ``src/services/odoo_init.py`` 讀成字串後在租戶 Odoo 容器內以
``python3 -c <source>`` 執行。package docstring 列的限制（只用 stdlib + odoo、
不得用 ``__file__``、最後一行必須是 JSON）在本檔一律適用。

流程：

1. 解析 ``odoo.conf`` 取得 db_name
2. 以 SUPERUSER_ID 開 registry cursor
3. 呼叫 ``ir.module.module.update_list()``
4. 清掉「**自管** addon 已經不在磁碟上」的 ``uninstalled`` record
5. commit

第 4 步存在的理由：``update_list()`` 只會新增／更新 record，從來不刪除已經
從檔案系統消失的 addon 的 record（Odoo 8 以來的已知行為，
github.com/odoo/odoo/issues/1677）。租戶取消勾選 addon 之後，
Apps 清單裡會留下一筆點下去就爆的殘影，所以要自己清。

⛔ 第 4 步的**域限制**（#748，別把它改回「算準 on_disk 就好」）：

刪除候選集必須是自管 addon 清單的子集。舊版沒有這層限制、直接刪「所有
state=uninstalled 且不在 addons_path 目錄裡」的 record，結果在 cloud-odoo
的佈局下把 617 筆 Odoo core record 整批 unlink——core 模組住在 conf 沒列、
``initialize_sys_path`` 無條件納入的隱含路徑，而 ``/opt/bitnami/odoo/addons``
是指向空 PVC 的 symlink，所以 addons_path 只解析得出 selected-addons 那幾顆。
租戶從此裝不了任何依賴未安裝 core 模組的 addon（``mcp_server`` 依賴 ``mail``）。

域限制與探針準確度是兩件獨立的事，兩件都要做，但**前者才是安全性質**：
探針怎麼壞，傷害都關在自管清單裡。
"""
import json
import sys

# 平台側 woow_paas_platform `src/models/cloud_service.py` 的 `ODOO_SELECTED_ADDONS_PATH`
# 會被 sed append 進這份 odoo.conf 的 addons_path（initContainer 與 post-init script 各一段）；
# 本腳本讀到的 addons_path 就是那次 append 的結果。改任一邊的路徑先看另一邊（#1052）。
ODOO_CONF_PATH = "/opt/bitnami/odoo/conf/odoo.conf"

# 自管 addon 全集的來源。cloud-odoo image 由 CI 掃 candidate addon 的
# __manifest__.py 產生這份 catalog，key 就是我們自己上架的 addon 技術名；
# 平台的 CloudOdooAddon record（＝租戶能勾選的清單）也是從它 sync。
# 租戶能選的 addon ⊆ catalog keys ⇒ 它就是「我們自管」可證明的全集。
#
# 為什麼不用 /init 的 API 參數當來源：``POST /init/odoo`` 沒有 request body，
# operator 手上並沒有這次該保留哪些 addon 的清單；而且就算加參數，那也只是
# 「這次選了什麼」而不是「哪些 addon 歸我們管」——域限制要的是後者。
MANAGED_CATALOG_PATH = "/all-addons/catalog.json"


def load_managed_addons(catalog_path=None):
    """讀出自管 addon 全集。

    Returns:
        set of addon 名稱；讀不到／格式不對／是空的一律回 ``None``。

    ``None`` 與 ``set()`` 是兩件不同的事：``None`` 代表「不知道自管清單是什麼」，
    呼叫端必須整段跳過 cleanup。空 catalog 也歸到 ``None``——一份零 addon 的
    catalog 幾乎一定是產生失敗，不是真的沒有 addon，而把它當成「自管全集為空」
    在語意上雖然安全（刪除候選集也是空的），當成未知才會留下 log。
    """
    path = MANAGED_CATALOG_PATH if catalog_path is None else catalog_path
    try:
        with open(path, "r") as handle:
            catalog = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(catalog, dict):
        return None
    names = {name for name in catalog if isinstance(name, str) and name}
    return names or None


def load_on_disk_addons():
    """磁碟上 Odoo 找得到的模組全集。

    直接用 ``odoo.modules.get_modules()``——``update_list()`` 內部用的就是它
    （``ir_module.py``：``for mod_name in modules.get_modules()``）。用同一個
    來源，「Odoo 認為存在」與「我們認為存在」就不可能漂掉；自己掃
    ``odoo.conf`` 的 addons_path 目錄正是 #748 的原料，那個做法看不到
    ``initialize_sys_path`` 無條件加進去的隱含 core 路徑。

    Returns:
        set of 模組名稱；探針拿不到時回 ``None``（呼叫端跳過 cleanup）。
    """
    try:
        from odoo.modules import get_modules

        return set(get_modules())
    except Exception:  # noqa: BLE001 — 探不到就跳過，不能當成「磁碟上沒有」
        return None


def select_removable(managed, on_disk):
    """要清掉的 addon 名單 —— **恆為 managed 的子集**。

    這個減法就是域限制本身：不論 ``on_disk`` 準不準、是不是空的，
    非自管的模組名都不可能出現在回傳值裡。
    """
    return sorted(set(managed) - set(on_disk))


def cleanup_stale_records(module_model):
    """清掉自管 addon 的 stale ``uninstalled`` record。

    Returns:
        ``(cleaned, skipped_reason)``。``skipped_reason`` 非 None 時
        ``cleaned`` 必為 0。
    """
    managed = load_managed_addons()
    if managed is None:
        return 0, "managed addon catalog unavailable: %s" % MANAGED_CATALOG_PATH

    on_disk = load_on_disk_addons()
    if on_disk is None:
        return 0, "on-disk module probe unavailable (odoo.modules.get_modules)"

    removable = select_removable(managed, on_disk)
    if not removable:
        return 0, None

    stale = module_model.search(
        [
            ("state", "=", "uninstalled"),
            # 只列舉自管且已不在磁碟上的名字。這裡刻意用正向的 ``in``
            # 而不是 ``not in <on_disk>``：候選集由白名單枚舉出來，
            # 探針失效時集合只會變化、不會擴散到別人的 record。
            ("name", "in", removable),
        ]
    )
    cleaned = len(stale)
    if stale:
        stale.unlink()
    return cleaned, None


def run():
    """執行 update_list + stale cleanup，回傳要 print 出去的 payload dict。"""
    import odoo

    odoo.tools.config.parse_config(["-c", ODOO_CONF_PATH, "--no-http"])
    db_name = odoo.tools.config["db_name"]
    if not db_name:
        return {"success": False, "error": "No db_name found in odoo.conf"}

    registry = odoo.registry(db_name)
    with registry.cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        module_model = env["ir.module.module"]

        # Odoo 18 的 update_list() 回 [updated, added]（ir_module.py 寫死
        # ``res = [0, 0]  # [update, add]``），不是 [added, updated]。
        result = module_model.update_list()
        updated = result[0] if isinstance(result, (list, tuple)) else 0
        added = result[1] if isinstance(result, (list, tuple)) else 0

        cleaned, skipped_reason = cleanup_stale_records(module_model)

        cr.commit()

    payload = {
        "success": True,
        "added": added,
        "updated": updated,
        "cleaned": cleaned,
    }
    if skipped_reason:
        payload["cleanup_skipped"] = skipped_reason
    return payload


def main():
    try:
        payload = run()
    except Exception as exc:  # noqa: BLE001 — 一律轉成 JSON 回報給 operator
        payload = {"success": False, "error": str(exc)}

    print(json.dumps(payload))
    if not payload.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
