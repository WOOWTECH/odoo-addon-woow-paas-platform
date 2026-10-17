# EMQX Dashboard 管理員密碼重設（operator exec 進 emqx 容器、以 `sh -c` 執行；
# woow_paas_platform #1774／商用驗收 INFRA-EMQX-007-1）。
#
# EMQX 的 dashboard.default_password 只在第一次開機寫進它自己的 mnesia，之後改 chart
# Secret／helm upgrade 都換不到密碼。這裡走 EMQX 內建的 `emqx ctl admins passwd`
# （與 Dashboard「修改密碼」同一個 emqx_dashboard_admin:change_password：檢查密碼規則、
# 單一 mria 交易寫入、成功後撤銷該帳號所有 Dashboard token ⇒ 已登入的 Dashboard 會被登出）。
# 不重啟、不 helm upgrade。
#
# 新密碼從 stdin 讀（operator 不放 argv）。`emqx ctl` 只收 argv，所以密碼只在這個容器
# 自己的程序表裡出現約一秒（Pod 自成 PID namespace，叢集其他地方看不到），不進 operator
# 的 kubectl 指令列與 log。
#
# 只用 dash builtin＋emqx（image 沒有保證其他工具）。`emqx ctl` 不論成敗都 exit 0，成功
# 印 `ok`、失敗印 `Error: <原因>`（5.8.9 實測），所以依輸出判斷。
#
# 原因碼沿用 operator AppAdminPasswordResetResponse.error_code 的既有值域：
#   invalid_password（不合 EMQX 密碼規則）、admin_missing（帳號不在）、
#   db_unsupported（ctl 連不上 broker 節點：開機中、已停止或 cookie 不符）。
# 其他無法辨識的輸出不印 JSON、exit 1 ⇒ operator 判 unknown（不宣稱密碼未變）。
# ⛔ 不回傳 emqx ctl 的原文（避免任何原文外流），只回固定訊息。
#
# 最後一行一律是 JSON：成功 {"success": true, "login": ...}；拒絕 {"success": false,
# "code": ..., "error": ...} 並 exit 1（EMQX 沒有寫入）。

pw=""
IFS= read -r pw || [ -n "$pw" ] || pw=""
# 去掉 CRLF 輸入可能殘留的 \r。
cr=$(printf '\r')
pw=${pw%"$cr"}

user=${EMQX_DASHBOARD__DEFAULT_USERNAME:-admin}
case "$user" in
    *[!A-Za-z0-9_.@-]*|"") user=admin ;;
esac

fail() {
    printf '{"success": false, "code": "%s", "error": "%s"}\n' "$1" "$2"
    exit 1
}

# EMQX 5.8 legal_password：8–64 字元、可見 ASCII、至少兩類（字母／數字／符號）。這裡先擋長度，
# 其餘交給 EMQX 自己判（它回 Error: Bad password…）。
if [ "${#pw}" -lt 8 ] || [ "${#pw}" -gt 64 ]; then
    fail invalid_password "new password must be 8-64 characters"
fi

if ! out=$(emqx ctl admins passwd "$user" "$pw" 2>&1); then
    out="${out}
__rc_nonzero__"
fi
pw=""

case "$out" in
    ok|ok"$cr"|*"
ok")
        printf '{"success": true, "login": "%s", "second_factor_cleared": false}\n' "$user"
        exit 0
        ;;
    *username_not_found*)
        fail admin_missing "administrator account '$user' not found"
        ;;
    *"Bad password"*|*"range of password length"*)
        fail invalid_password "EMQX rejected the new password (8-64 characters, at least two of letters, digits and symbols)"
        ;;
    *"is not running"*|*"not responding to pings"*|*"cookie used by"*)
        # ctl 連不上 broker 節點（還在開機、已停止、cookie 不符）：什麼都沒寫入。
        fail db_unsupported "the EMQX node is not reachable"
        ;;
esac
# 看不懂的輸出：不知道寫入了沒有。不印 JSON ⇒ operator 判 unknown。
echo "unrecognised emqx ctl output" >&2
exit 1
