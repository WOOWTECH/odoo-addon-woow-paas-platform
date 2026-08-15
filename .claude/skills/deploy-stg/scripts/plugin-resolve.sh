#!/usr/bin/env bash
#
# woow-dev plugin 的 k8s-dev-sandbox create.sh 解析器（deploy-stg 用）。
#
# 這份是從 deploy-stg.sh 原地抽出來的。抽出的唯一理由是**可測**：deploy-stg.sh 是
# 541 行的頂層命令式流程（無 main() 守衛），source 進測試就會直接開始打 CI 與叢集，
# 解析函式無從單獨行使。測試：../tests/test-resolve-plugin-create-sh.sh
#
# 為什麼放在 .claude/skills/deploy-stg/scripts/ 而不是共用的 .claude/skills/lib/：
# repo-lint workflow 的 paths 白名單有 `.claude/skills/deploy-stg/**`、**沒有**
# `.claude/skills/lib/**`（同一份白名單的檔頭自己寫著這個維護義務）。放到 lib/ 的話，
# 「只改這支解析器」的 PR 不會觸發 repo-lint ⇒ 守著它的測試對它最該守的改動完全休眠，
# 而輸出與「跑了而且沒事」一模一樣。旁證：`.claude/skills/lib/kube-context_test.sh`
# 今天就處在這個狀態（不在任何 tests/ 目錄下，也不在白名單內，沒有 CI 會跑它）。
#
# 用法：source 本檔 → 呼叫 resolve_plugin_create_sh
#   成功：把絕對路徑印到 stdout、同時寫進 PLUGIN_RESOLVE_PATH，return 0
#   失敗：return 1；此時 plugin_resolve_tried_report 可印出**實際找過**的候選
# ⚠ 呼叫端不要用 `$( )` 包 —— 那是 subshell，PLUGIN_RESOLVE_* 全域回不來。
#
# ---------------------------------------------------------------------------
# 🔴 兩種 base 形狀（#728 的根因，改這支之前先讀懂）
#
# 「plugin 安裝在哪」有兩種完全不同的目錄形狀，而它們**不能共用同一段相對路徑**：
#
#   plugin 根      <plugin-root>/skills/k8s-dev-sandbox/scripts/create.sh
#                  ← marketplace cache 的 <mkt>/<plugin>/<ver>/ 就是這一層
#   marketplace 根 <repo-root>/plugins/<plugin>/skills/k8s-dev-sandbox/scripts/create.sh
#                  ← plugins-local 底下放的是**整個 plugin repo**，是這一層
#
# #728 之前的版本只有一個 `rel`（plugin 根形狀），卻拿它去接 plugins-local 的
# marketplace 根 ⇒ 優先序 2 永不命中，一路落到 cache。它的失效形狀是假綠：
# 在 plugins-local 改完程式跑 deploy-stg，實際跑的是 cache 裡的舊版，helm 綠、
# 部署成功、行為是舊的 ——「我改的東西沒被執行」與「我改的東西沒有效果」輸出一樣。
#
# ⚠ 反過來也是陷阱：把 `plugins/<name>/` 一律補進 `rel`，會把**目前唯一在運作的**
#   優先序 3、4（cache，plugin 根形狀）一起打壞。故兩段相對路徑分開持有，
#   由「base 是哪種形狀」決定用哪一段。tests/ 的 B 組就是守這件事的。
# ---------------------------------------------------------------------------
#
# 解析順序（CLAUDE_PLUGIN_ROOT 在純 bash 不可用，只能自己找）：
#   1) WOOWPRD_PLUGIN_ROOT env 覆寫（最高優先；指到的檔存在才採用，否則往下）。
#      🔴 語意＝**plugin 根**。但 env 名字取自 marketplace（`woowprd`）、plugin 目錄
#      卻叫 `woow-dev`，指到 repo 根是可預期的誤用，故 plugin 根落空時再往
#      `<root>/plugins/<plugin>/` 找一次 —— 兩種形狀都接得住，且兩種都會出現在
#      失敗訊息的「已找」清單裡。
#   2) 本機 dev copy（plugins-local，開發中的活版本）＝**marketplace 根**形狀。
#   3) marketplace cache（cache/<mkt>/<plugin>/<ver>/…）＝**plugin 根**形狀。
#      plugin 目錄名改過：現行 `woow-dev`（優先），舊 `woowprd`（相容）。同名可能
#      並存多版本目錄 → 收集後 version-sort 倒序取最新（1.10.0 > 1.9.0）。
#   4) fallback：不綁 plugin 名，cache 根下遞迴搜同一相對路徑，避免未來 plugin 再
#      改名（如 woow-dev → 其他）整組又壞掉。
#
# 註：bash nullglob off，glob 不匹配會留 literal → `[ -f ]` 失敗自動跳過。
# 註：本函式在 `if ! ...`（set -e 暫停）context 呼叫，故內部非零回傳不會誤殺主流程；
#     每個成功分支都顯式 `return 0`，避免 pipefail 下 sort|head 的 SIGPIPE 汙染回傳值。

# 本次解析**實際找過**的候選（確定路徑逐條、glob 與遞迴則記 pattern）。
# 失敗訊息由它印出，而不是另外手寫一份常數 —— 手寫的那份會與查找邏輯漂開，
# 而漂開的當下沒有任何訊號（使用者只剩這份清單可以判斷「它到底找了哪裡」）。
PLUGIN_RESOLVE_TRIED=()
PLUGIN_RESOLVE_PATH=""

# plugin 目錄名候選：現行名優先、舊名相容。三處（env 的 marketplace 形狀、
# plugins-local、cache glob）共用同一份，才不會又出現「某一處漏改」的不一致。
PLUGIN_RESOLVE_PLUGIN_DIRS=(woow-dev woowprd)
# plugins-local 底下 marketplace（＝plugin repo）目錄名候選。今天是 `woowprd`
# （repo 名），repo 若改名成 woow-dev 也接得住。
PLUGIN_RESOLVE_LOCAL_DIRS=(woowprd woow-dev)

_plugin_resolve_try() {
    # _plugin_resolve_try <候選絕對路徑>：記帳 → 命中就印出並回 0。
    PLUGIN_RESOLVE_TRIED+=("$1")
    [ -f "$1" ] || return 1
    PLUGIN_RESOLVE_PATH="$1"
    printf '%s\n' "$1"
    return 0
}

plugin_resolve_tried_report() {
    # 印出本次實際找過的候選，一行一條（呼叫端自行導向 stderr）。
    [ "${#PLUGIN_RESOLVE_TRIED[@]}" -gt 0 ] || return 0
    printf '%s\n' "${PLUGIN_RESOLVE_TRIED[@]}"
}

resolve_plugin_create_sh() {
    # 相對「plugin 根」的路徑。marketplace 根要再前綴 plugins/<plugin>/（見檔頭）。
    local rel="skills/k8s-dev-sandbox/scripts/create.sh"
    local cache_root="$HOME/.claude/plugins/cache"
    local local_root="$HOME/.claude/plugins-local"
    local c pdir ldir newest

    PLUGIN_RESOLVE_TRIED=()
    PLUGIN_RESOLVE_PATH=""

    # 優先序 1：env 覆寫。先當 plugin 根，落空再當 marketplace 根。
    if [ -n "${WOOWPRD_PLUGIN_ROOT:-}" ]; then
        _plugin_resolve_try "${WOOWPRD_PLUGIN_ROOT}/${rel}" && return 0
        for pdir in "${PLUGIN_RESOLVE_PLUGIN_DIRS[@]}"; do
            _plugin_resolve_try "${WOOWPRD_PLUGIN_ROOT}/plugins/${pdir}/${rel}" && return 0
        done
    fi

    # 優先序 2：本機 dev copy（marketplace 根形狀 —— #728 修的就是這一段）。
    for ldir in "${PLUGIN_RESOLVE_LOCAL_DIRS[@]}"; do
        for pdir in "${PLUGIN_RESOLVE_PLUGIN_DIRS[@]}"; do
            _plugin_resolve_try "${local_root}/${ldir}/plugins/${pdir}/${rel}" && return 0
        done
    done

    # 優先序 3：marketplace cache（plugin 根形狀），現行名優先、舊名相容。
    for pdir in "${PLUGIN_RESOLVE_PLUGIN_DIRS[@]}"; do
        PLUGIN_RESOLVE_TRIED+=("${cache_root}/*/${pdir}/*/${rel}")
        local -a hits=()
        for c in "$cache_root/"*"/${pdir}/"*"/${rel}"; do
            [ -f "$c" ] && hits+=("$c")
        done
        if [ "${#hits[@]}" -gt 0 ]; then
            newest="$(printf '%s\n' "${hits[@]}" | sort -Vr | head -n1)"
            [ -n "$newest" ] || newest="${hits[0]}"  # sort -V 缺席時退化為 glob 第一筆
            PLUGIN_RESOLVE_PATH="$newest"
            printf '%s\n' "$newest"
            return 0
        fi
    done

    # 優先序 4（fallback）：cache 根下遞迴搜同一相對路徑，同樣取版本最新。
    PLUGIN_RESOLVE_TRIED+=("${cache_root}（遞迴 fallback：*/${rel}）")
    local -a fhits=()
    while IFS= read -r c; do
        [ -n "$c" ] && fhits+=("$c")
    done < <(find "$cache_root" -type f -path "*/${rel}" 2>/dev/null)
    if [ "${#fhits[@]}" -gt 0 ]; then
        newest="$(printf '%s\n' "${fhits[@]}" | sort -Vr | head -n1)"
        [ -n "$newest" ] || newest="${fhits[0]}"
        PLUGIN_RESOLVE_PATH="$newest"
        printf '%s\n' "$newest"
        return 0
    fi
    return 1
}
