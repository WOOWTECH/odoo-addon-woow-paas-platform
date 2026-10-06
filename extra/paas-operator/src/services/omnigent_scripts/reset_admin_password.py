"""Omnigent 管理員密碼重設（operator exec 進 omnigent server 容器執行；#1370 後續）。

新密碼從 stdin 讀（不經 argv）。帳號＝OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME（chart 固定
admin）。用 omnigent 自己的 AccountStore／hash_password，與 Members → Reset 同一條寫入路
（update_password；同交易、SQLite BEGIN IMMEDIATE），不碰 account_generation——與上游
admin reset 行為一致：已登入的 session 不會被登出，配對過的 host 也不受影響。

原因碼沿用 operator AppAdminPasswordResetResponse.error_code 的既有值域：invalid_password、
db_unsupported（沒有 DATABASE_URL／不是 accounts 模式）、admin_missing、admin_inactive（帳號已
不是管理員）。

最後一行一律印一個 JSON：成功 {"success": true, "login": ...}；拒絕 {"success": false,
"code": ..., "error": ...} 並 exit 1（交易未寫入）。
"""
import json
import os
import sys


def _out(payload, code=0):
    print(json.dumps(payload))
    sys.exit(code)


def main():
    new_password = sys.stdin.read().rstrip("\r\n")
    if len(new_password) < 8:
        _out({"success": False, "code": "invalid_password", "error": "new password must be at least 8 characters"}, 1)

    db_uri = os.environ.get("DATABASE_URL", "").strip()
    if not db_uri:
        _out({"success": False, "code": "db_unsupported", "error": "DATABASE_URL is not set"}, 1)
    if os.environ.get("OMNIGENT_AUTH_PROVIDER", "accounts").strip() != "accounts":
        _out({"success": False, "code": "db_unsupported",
              "error": "omnigent is not running in accounts auth mode"}, 1)
    username = (os.environ.get("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME") or "admin").strip()

    from omnigent.db.account_authority import target_account_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.passwords import hash_password

    store = SqlAlchemyAccountStore(db_uri)
    user = store.get_user(username)
    if user is None:
        # 首次開機還沒建出 admin（或被改名／刪除）：不在這裡憑空建帳號。
        _out({"success": False, "code": "admin_missing",
              "error": "administrator account '%s' not found" % username}, 1)
    if not getattr(user, "is_admin", False):
        _out({"success": False, "code": "admin_inactive",
              "error": "account '%s' is no longer an administrator" % username}, 1)

    with target_account_scope(username, user.account_generation):
        store.update_password(username, hash_password(new_password))
    _out({"success": True, "login": username, "second_factor_cleared": False})


if __name__ == "__main__":
    main()
