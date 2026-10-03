"""#1355：新開租戶 Odoo 的台灣在地化預設（繁中、台北時區、公司國家 TW、幣別 TWD）。

由 ``src/services/odoo_init.py`` 讀成字串後在租戶 Odoo 容器內以 ``python3 -c <source>``
執行，package docstring 列的限制（只用 stdlib + odoo、不得用 ``__file__``、最後一行
必須是 JSON）在本檔一律適用。

為什麼在這裡做：chart（woow-odoo bootstrap）只 ``-i base`` ＋設定管理員帳密；平台開通
後的 post-deploy init（``POST /init/odoo``）是唯一會對新 DB 跑 ORM 的平台步驟。新開的
Odoo 因此是 en_US、未設時區、公司沒有國家、幣別 USD——之後租戶一裝會計／銷售，
Odoo 找不到國家就套 generic chart（15% 稅）。

做什麼（每一項都只在**仍是出廠值**時才改，租戶改過的一律不碰）：

1. 安裝並啟用 zh_TW（含已安裝模組的翻譯）；
2. 管理員（``base.user_admin``）語言 en_US → zh_TW、未設時區 → Asia/Taipei；
3. 主公司沒有國家 → 台灣（``base.tw``）；
4. 主公司幣別仍是 USD 且還沒有任何會計分錄 → 啟用並改成 TWD（``base.TWD``）。

稅與會計科目**不在這裡裝**：Odoo 18 ``account`` 的 ``_auto_install_l10n`` 會依公司國家
自動裝對應的在地化模組（TW ⇒ ``l10n_tw``，5% 營業稅），所以第 3 步就讓「之後裝會計」
走對的路，不必預裝會計（租戶沒要會計時不該多一堆模組）。

只跑一次：完成後寫 ``ir.config_parameter`` 標記；init 會在每次 redeploy／改 addon 重跑，
標記存在就整段跳過。另一道閘：管理員**已經登入過**（``res.users.log`` 有紀錄）的 DB 視為
已在使用中——例如本功能上線前開的服務、還原回來的 DB——一律不改，只補標記。
"""
import json
import sys

ODOO_CONF_PATH = "/etc/odoo/odoo.conf"

MARKER_KEY = "woow_paas_platform.tenant_localized"
TARGET_LANG = "zh_TW"
TARGET_TZ = "Asia/Taipei"
TARGET_COUNTRY_XMLID = "base.tw"
TARGET_CURRENCY_XMLID = "base.TWD"
FACTORY_LANG = "en_US"
FACTORY_CURRENCY_XMLID = "base.USD"


def _has_accounting_entries(env):
    """有任何會計分錄就不能（也不該）換公司幣別；沒裝 account 時一定沒有。"""
    if "account.move.line" not in env:
        return False
    return bool(env["account.move.line"].sudo().search_count([], limit=1))


def localize(env):
    """套用在地化預設；回傳要放進 JSON 回報的 dict（不 commit，由呼叫端決定）。"""
    icp = env["ir.config_parameter"].sudo()
    if icp.get_param(MARKER_KEY):
        return {"success": True, "skipped": "already localized", "changes": []}

    if env["res.users.log"].sudo().search_count([], limit=1):
        icp.set_param(MARKER_KEY, "skipped-in-use")
        return {"success": True, "skipped": "database already in use (users have logged in)",
                "changes": []}

    changes = []

    lang = env["res.lang"].with_context(active_test=False).search(
        [("code", "=", TARGET_LANG)], limit=1)
    if not lang:
        return {"success": False, "error": "language %s not found in res.lang" % TARGET_LANG}
    if not lang.active:
        # 與 Settings › Languages › Add 同一條路：啟用語言並載入已安裝模組的翻譯。
        env["base.language.install"].create({
            "lang_ids": [(6, 0, lang.ids)], "overwrite": False,
        }).lang_install()
        changes.append("lang_installed")

    admin = env.ref("base.user_admin", raise_if_not_found=False)
    if admin:
        vals = {}
        if admin.lang in (FACTORY_LANG, False):
            vals["lang"] = TARGET_LANG
        if not admin.tz:
            vals["tz"] = TARGET_TZ
        if vals:
            admin.sudo().write(vals)
            changes.extend("admin_%s" % key for key in sorted(vals))

    company = env.ref("base.main_company", raise_if_not_found=False)
    if company:
        if not company.country_id:
            company.sudo().write({"country_id": env.ref(TARGET_COUNTRY_XMLID).id})
            changes.append("company_country")
        usd = env.ref(FACTORY_CURRENCY_XMLID, raise_if_not_found=False)
        twd = env.ref(TARGET_CURRENCY_XMLID, raise_if_not_found=False)
        if (twd and company.currency_id == usd and company.currency_id != twd
                and not _has_accounting_entries(env)):
            if not twd.active:
                twd.sudo().write({"active": True})
            company.sudo().write({"currency_id": twd.id})
            changes.append("company_currency")

    icp.set_param(MARKER_KEY, "1")
    return {"success": True, "changes": changes}


def run():
    import odoo

    odoo.tools.config.parse_config(["-c", ODOO_CONF_PATH, "--no-http"])
    db_name = odoo.tools.config["db_name"]
    if not db_name:
        return {"success": False, "error": "No db_name found in odoo.conf"}

    registry = odoo.registry(db_name)
    with registry.cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        payload = localize(env)
        if payload.get("success"):
            cr.commit()
        else:
            cr.rollback()
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
