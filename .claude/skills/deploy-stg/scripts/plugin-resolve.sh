#!/usr/bin/env bash
#
# woow-dev plugin 的 k8s-dev-sandbox create.sh 解析器（deploy-stg 用）。
#
# 這份是從 deploy-stg.sh 原地抽出來的（行為逐字保留，本 commit 不改語意）。抽出的
# 唯一理由是**可測**：deploy-stg.sh 是 541 行的頂層命令式流程（無 main() 守衛），
# source 進測試就會直接開始打 CI 與叢集，解析函式無從單獨行使。
#
# 為什麼放在 .claude/skills/deploy-stg/scripts/ 而不是共用的 .claude/skills/lib/：
# repo-lint workflow 的 paths 白名單有 `.claude/skills/deploy-stg/**`、**沒有**
# `.claude/skills/lib/**`（同一份白名單的檔頭自己寫著這個維護義務）。放到 lib/ 的話，
# 「只改這支解析器」的 PR 不會觸發 repo-lint ⇒ 守著它的測試對它最該守的改動完全休眠，
# 而輸出與「跑了而且沒事」一模一樣。旁證：`.claude/skills/lib/kube-context_test.sh`
# 今天就處在這個狀態（不在任何 tests/ 目錄下，也不在白名單內，沒有 CI 會跑它）。
#
# 用法：source 本檔後呼叫 resolve_plugin_create_sh；成功時把絕對路徑印到 stdout。
#
# deploy-stg 把「建沙盒」委派給 woow-dev plugin 的 create.sh（即
# /woow-dev:k8s-dev-sandbox 的引擎），不再自帶一份重複的 create.sh。
# CLAUDE_PLUGIN_ROOT 在純 bash 不可用，故依序解析 plugin 安裝路徑：
#   1) WOOWPRD_PLUGIN_ROOT env 覆寫（最高優先；指到的檔存在才採用，否則往下）
#   2) 本機 dev copy（plugins-local，開發中的活版本）
#   3) marketplace cache（cache/<market>/<plugin>/<ver>/skills/...）。plugin 目錄名
#      改過：現行 `woow-dev`（優先），舊 `woowprd`（相容）。同名可能並存多版本目錄
#      → 收集後 version-sort 倒序取最新（1.10.0 > 1.9.0）。
#   4) fallback：不綁 plugin 名，cache 根下遞迴搜同一相對路徑，避免未來 plugin 再
#      改名（如 woow-dev → 其他）整組又壞掉。
# 註：bash nullglob off，glob 不匹配會留 literal → `[ -f ]` 失敗自動跳過。
# 註：本函式只在 `if ! ...`（set -e 暫停）context 呼叫，故內部非零回傳不會誤殺主流程；
#     每個成功分支都顯式 `return 0`，避免 pipefail 下 sort|head 的 SIGPIPE 汙染回傳值。

resolve_plugin_create_sh() {
    local rel="skills/k8s-dev-sandbox/scripts/create.sh"
    local cache_root="$HOME/.claude/plugins/cache"
    local c glob_mid newest

    # 優先序 1-2：確定路徑（env override 最高優先），命中即回。
    for c in \
        ${WOOWPRD_PLUGIN_ROOT:+"${WOOWPRD_PLUGIN_ROOT}/${rel}"} \
        "$HOME/.claude/plugins-local/woowprd/${rel}"; do
        [ -f "$c" ] && { printf '%s\n' "$c"; return 0; }
    done

    # 優先序 3：marketplace cache，現行名 woow-dev 優先、舊名 woowprd 相容。
    for glob_mid in woow-dev woowprd; do
        local -a hits=()
        for c in "$cache_root/"*"/${glob_mid}/"*"/${rel}"; do
            [ -f "$c" ] && hits+=("$c")
        done
        if [ "${#hits[@]}" -gt 0 ]; then
            newest="$(printf '%s\n' "${hits[@]}" | sort -Vr | head -n1)"
            [ -n "$newest" ] || newest="${hits[0]}"  # sort -V 缺席時退化為 glob 第一筆
            printf '%s\n' "$newest"
            return 0
        fi
    done

    # 優先序 4（fallback）：cache 根下遞迴搜同一相對路徑，同樣取版本最新。
    local -a fhits=()
    while IFS= read -r c; do
        [ -n "$c" ] && fhits+=("$c")
    done < <(find "$cache_root" -type f -path "*/${rel}" 2>/dev/null)
    if [ "${#fhits[@]}" -gt 0 ]; then
        newest="$(printf '%s\n' "${fhits[@]}" | sort -Vr | head -n1)"
        [ -n "$newest" ] || newest="${fhits[0]}"
        printf '%s\n' "$newest"
        return 0
    fi
    return 1
}
