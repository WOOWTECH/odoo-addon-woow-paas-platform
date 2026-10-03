"""重設租戶 Odoo 的管理員（``base.user_admin``）密碼，並清掉它的兩步驟驗證（#1370／#1186）。

由 ``src/services/app_admin_password.py`` 讀成字串後在租戶 Odoo 容器內以
``python3 -c <source>`` 執行。package docstring 列的限制（只用 stdlib + odoo、
不得用 ``__file__``、最後一行必須是 JSON）在本檔一律適用。

⛔ **新密碼只從 stdin 讀**（operator 以 ``kubectl exec --stdin`` 送入）：不進 argv
（``ps``／kubectl 的 log 看得到 argv）、不進任何輸出與錯誤訊息。

為什麼要 exec 而不是 helm upgrade：woow-odoo chart 的 ``conf.adminPassword`` 只在
首次 ``-i base`` 由 bootstrap 寫進 DB（同交易寫 ``bootstrap_admin_password_set``
標記），之後重送不覆寫——換 Secret 不會換密碼，只會「假裝成功」。密碼的權威在
租戶 DB，所以直接在 DB 改。

流程（同一個 transaction）：

1. 解析 ``odoo.conf`` 取得 db_name
2. ``env.ref("base.user_admin")``——用 xmlid，不寫死 id=2；login 不變
3. 找不到／已停用 ⇒ 回報失敗、**什麼都不改**（fail-loud，平台轉成可行動訊息）
4. ``write({"password": ...})``（經 ``_set_password`` 以 passlib hash）
5. ``auth_totp`` 有裝時清掉 ``totp_secret`` 與受信任裝置
6. commit

改密碼會改變 session token ⇒ 已登入的 session 會自然失效（預期行為）。
不需要重啟：Odoo 每次登入都讀 DB。
"""
import json
import sys

# woow-odoo chart 的 conf 掛載點；與 ``update_module_list.py`` 同一個來源。
ODOO_CONF_PATH = "/etc/odoo/odoo.conf"

# 與平台鑄造／租戶自設密碼的級距一致；這裡只是最後一道健全性檢查，
# 真正的規則在平台端（腳本不回顯密碼內容，只回報長度不合）。
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


def read_new_password(stream):
    """從 stdin 讀新密碼。只去掉結尾的一個換行（``echo`` 式的輸入），其餘原樣保留。"""
    data = stream.read()
    if data.endswith("\n"):
        data = data[:-1]
        if data.endswith("\r"):
            data = data[:-1]
    return data


def reset_admin(env, new_password):
    """在給定的 env（SUPERUSER）上重設 ``base.user_admin``；回傳要 print 的 payload。

    失敗時回傳 ``success: False`` 與 ``code``，**呼叫端負責 rollback**——本函式
    在回報失敗之前不會寫任何東西。
    """
    if not (MIN_PASSWORD_LENGTH <= len(new_password) <= MAX_PASSWORD_LENGTH):
        return {
            "success": False,
            "code": "invalid_password",
            "error": "new password length out of range",
        }

    user = env.ref("base.user_admin", raise_if_not_found=False)
    if not user:
        return {
            "success": False,
            "code": "admin_missing",
            "error": "the administrator account (base.user_admin) no longer exists",
        }
    if not user.active:
        return {
            "success": False,
            "code": "admin_inactive",
            "error": "the administrator account (base.user_admin) is archived",
        }

    user.write({"password": new_password})

    second_factor_cleared = False
    # auth_totp 沒裝時欄位不存在。⛔ 不可呼叫 action_totp_disable()／
    # revoke_all_devices()：兩者掛 @check_identity，沒有 HTTP request 時直接
    # UserError("This method can only be accessed over HTTP")。
    if "totp_secret" in user._fields:
        second_factor_cleared = bool(user.totp_secret)
        user.write({"totp_secret": False})
        revoke = getattr(user, "_revoke_all_devices", None)
        if callable(revoke):
            revoke()

    return {
        "success": True,
        "login": user.login,
        "second_factor_cleared": second_factor_cleared,
    }


def run(new_password):
    """開 registry cursor、重設、成功才 commit；回傳要 print 出去的 payload dict。"""
    import odoo

    odoo.tools.config.parse_config(["-c", ODOO_CONF_PATH, "--no-http"])
    db_name = odoo.tools.config["db_name"]
    if not db_name:
        return {"success": False, "error": "No db_name found in odoo.conf"}

    registry = odoo.registry(db_name)
    with registry.cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        payload = reset_admin(env, new_password)
        if payload.get("success"):
            cr.commit()
        else:
            cr.rollback()
    return payload


def main():
    try:
        new_password = read_new_password(sys.stdin)
        payload = run(new_password)
    except Exception as exc:  # noqa: BLE001 — 一律轉成 JSON 回報給 operator
        # 例外訊息理論上不含密碼（密碼只在 write 的 vals 裡），但 passlib／ORM 的
        # 例外字串不歸我們管——只回報例外類別，細節留在容器自己的 stderr 之外。
        payload = {"success": False, "error": "%s while resetting" % type(exc).__name__}

    print(json.dumps(payload))
    if not payload.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
