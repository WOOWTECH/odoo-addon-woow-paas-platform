"""重新整理 Odoo module list，並清掉自管 addon 留下的 stale record。

由 ``src/services/odoo_init.py`` 讀成字串後在租戶 Odoo 容器內以
``python3 -c <source>`` 執行。package docstring 列的限制（只用 stdlib + odoo、
不得用 ``__file__``、最後一行必須是 JSON）在本檔一律適用。

流程：

1. 解析 ``odoo.conf`` 取得 db_name
2. 以 SUPERUSER_ID 開 registry cursor
3. 呼叫 ``ir.module.module.update_list()``
4. 清掉「自管 addon 已經不在磁碟上」的 ``uninstalled`` record
5. commit

第 4 步存在的理由：``update_list()`` 只會新增／更新 record，從來不刪除已經
從檔案系統消失的 addon 的 record（Odoo 8 以來的已知行為，
github.com/odoo/odoo/issues/1677）。租戶取消勾選 addon 之後，
Apps 清單裡會留下一筆點下去就爆的殘影，所以要自己清。
"""
import json
import os
import sys

ODOO_CONF_PATH = "/opt/bitnami/odoo/conf/odoo.conf"


def collect_on_disk_addons(addons_path):
    """掃 ``addons_path`` 下的子目錄名，當作「磁碟上有的 addon」。

    Args:
        addons_path: 目錄字串的 iterable（``odoo.conf`` 的 addons_path 逗號拆開）。

    Returns:
        set of addon 目錄名。
    """
    on_disk = set()
    for raw_path in addons_path:
        path = raw_path.strip()
        if os.path.isdir(path):
            on_disk.update(
                name
                for name in os.listdir(path)
                if os.path.isdir(os.path.join(path, name))
            )
    return on_disk


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

        result = module_model.update_list()
        added = result[0] if isinstance(result, (list, tuple)) else 0
        updated = result[1] if isinstance(result, (list, tuple)) else 0

        on_disk = collect_on_disk_addons(
            odoo.tools.config["addons_path"].split(",")
        )
        stale = module_model.search(
            [
                ("state", "=", "uninstalled"),
                ("name", "not in", sorted(on_disk)),
                ("name", "!=", "base"),
            ]
        )
        cleaned = len(stale)
        if stale:
            stale.unlink()

        cr.commit()

    return {
        "success": True,
        "added": added,
        "updated": updated,
        "cleaned": cleaned,
    }


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
