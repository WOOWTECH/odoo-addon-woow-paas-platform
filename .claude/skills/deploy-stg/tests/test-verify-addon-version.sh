#!/usr/bin/env bash
#
# verify-addon-version.sh 判定層自測（#665）
#
# 這支測試守的是 gate 的**比較器本身**：manifest 版號解析（期望值取樣的最後一哩）與
# ir_module_module 列的判定。它刻意零外部相依 —— 不碰 kubectl / kubeconfig / 網路 /
# Odoo，所以可以在 CI 容器、也可以在 deploy-stg 執行當下即時跑一遍。
#
# 🔴 排序是刻意的：**紅燈案例排在綠燈之前**。
#    一道從沒被看過會失敗的檢查，與一道什麼都沒檢查的檢查，輸出長得一模一樣。先證明
#    每一種真實失效形態都會被判紅，最後才證明一致時會放行（沒有這個陽性對照，「永遠紅」
#    也會全綠通過）。
#
# 執行：bash .claude/skills/deploy-stg/tests/test-verify-addon-version.sh
# Exit code：0 全過，1 有失敗。

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${TEST_DIR}/../scripts/verify-addon-version.sh"

if [ ! -f "$TARGET" ]; then
    echo "✗ 找不到被測腳本：${TARGET}"
    exit 1
fi

# shellcheck source=../scripts/verify-addon-version.sh
. "$TARGET"
# 被測腳本開頭有 `set -euo pipefail`，source 進來會污染本測試的 shell：
# 第一個「刻意失敗」的斷言就會讓整份測試 exit，剩下的一支都不會跑（而且輸出看起來
# 像是提早結束、不像少測）。把 -e / pipefail 沖掉，-u 留著。
set +e
set +o pipefail

PASSED=0
FAILED=0

_contains() {
    # _contains <needle> <haystack>
    case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac
}

_report() {
    # $1 = ok|fail, $2 = 描述, $3 = 細節
    if [ "$1" = "ok" ]; then
        PASSED=$((PASSED + 1))
        printf '  ✓ %s\n' "$2"
    else
        FAILED=$((FAILED + 1))
        printf '  ✗ %s\n      %s\n' "$2" "$3"
    fi
}

# assert_eval <描述> <psql列> <期望版號> <want_rc> <reason 應含的字串>
assert_eval() {
    local desc="$1" row="$2" expected="$3" want_rc="$4" want_sub="${5:-}"
    local rc
    evaluate_module_row "$row" "$expected"
    rc=$?
    if [ "$rc" -ne "$want_rc" ]; then
        _report fail "$desc" "rc=${rc}（期望 ${want_rc}）reason='${EVAL_REASON}'"
        return
    fi
    if [ -n "$want_sub" ] && ! _contains "$want_sub" "$EVAL_REASON"; then
        _report fail "$desc" "reason 不含 '${want_sub}'：'${EVAL_REASON}'"
        return
    fi
    _report ok "$desc"
}

# assert_extract <描述> <manifest 內容> <want_rc> <want_value 或 reason 應含的字串>
assert_extract() {
    local desc="$1" content="$2" want_rc="$3" want="${4:-}"
    local out rc tmp
    # 用暫存檔接 stdout 而不是 $(...)：herestring + 重導向不開 subshell，這樣同一次
    # 呼叫就同時拿得到 rc、stdout 與 EXTRACT_REASON（$(...) 會把 reason 留在 subshell）。
    tmp="$(mktemp)"
    EXTRACT_REASON=""
    extract_manifest_version <<< "$content" >"$tmp" 2>/dev/null
    rc=$?
    out="$(cat "$tmp")"
    rm -f "$tmp"
    if [ "$rc" -ne "$want_rc" ]; then
        _report fail "$desc" "rc=${rc}（期望 ${want_rc}）out='${out}' reason='${EXTRACT_REASON}'"
        return
    fi
    if [ "$want_rc" -eq 0 ]; then
        if [ "$out" != "$want" ]; then
            _report fail "$desc" "版號='${out}'（期望 '${want}'）"
            return
        fi
    elif [ -n "$want" ] && ! _contains "$want" "$EXTRACT_REASON"; then
        _report fail "$desc" "reason 不含 '${want}'：'${EXTRACT_REASON}'"
        return
    fi
    _report ok "$desc"
}

assert_eq() {
    local desc="$1" actual="$2" want="$3"
    if [ "$actual" = "$want" ]; then
        _report ok "$desc"
    else
        _report fail "$desc" "得到 '${actual}'（期望 '${want}'）"
    fi
}

# =============================================================================
# 0. 守衛自己的鑑別力
# =============================================================================
echo "0. 測試骨架本身"

# 純函式沒被載進來的話，底下每一個 assert 都會 rc=127 —— 但若有人把 want_rc 寫寬鬆，
# 整份測試會變成「什麼都沒測還全綠」。先把載入這件事本身斷言掉。
assert_eq "被測純函式已載入（evaluate_module_row）" "$(type -t evaluate_module_row 2>/dev/null)" "function"
assert_eq "被測純函式已載入（extract_manifest_version）" "$(type -t extract_manifest_version 2>/dev/null)" "function"
assert_eq "被測純函式已載入（compute_db_name_local）" "$(type -t compute_db_name_local 2>/dev/null)" "function"
assert_eq "被測純函式已載入（vav_repo_behind_count）" "$(type -t vav_repo_behind_count 2>/dev/null)" "function"
# source 時不得執行 main（否則測試會去打叢集）
assert_eq "source 不觸發 main" "${VAV_MAIN_RAN:-no}" "no"

# 斷言 helper 真的會判失敗：餵一個必然不符的期望，helper 應該記一筆 FAILED。
# 沒有這一條，「helper 永遠 _report ok」也會讓整份測試全綠。
_harness_probe="$(
    PASSED=0; FAILED=0
    assert_eval "（探針）一致卻宣稱應該紅" "installed|1.0.0" "1.0.0" 1 >/dev/null 2>&1
    printf '%s' "$FAILED"
)"
assert_eq "斷言 helper 會判失敗（探針）" "$_harness_probe" "1"

# =============================================================================
# 1. 紅燈：這些形態一個都不准放行
# =============================================================================
echo "1. 紅燈（真實失效形態）"

# #665 本體：換 image 沒升 addon ⇒ DB 版號落後跑著的 code
assert_eval "DB 版號落後（#665 本體）" \
    "installed|18.0.1.0.123" "18.0.1.0.124" 1 "版號不一致"

# 升級卡在半路：版號相同也不能算過（新 code 已在服務、schema 還沒收斂）
assert_eval "state='to upgrade'（升級未收斂）" \
    "to upgrade|18.0.1.0.124" "18.0.1.0.124" 1 "to upgrade"

# #85 家族：模組根本沒裝
assert_eval "state='uninstalled'" \
    "uninstalled|18.0.1.0.124" "18.0.1.0.124" 1 "uninstalled"

# 🔴 壞掉的探針最愛回答「查不到」，而「查不到」在寬鬆判準下與「一切正常」同形
assert_eval "DB 查無此模組（空輸出）" \
    "" "18.0.1.0.124" 1 "查不到"

# 期望值那一側取樣失敗時，不准拿空字串去比「相等」
assert_eval "期望版號為空（期望值取樣失敗）" \
    "installed|18.0.1.0.124" "" 1 "期望版號為空"

# psql 出錯時 stderr 被吞、stdout 可能是別的東西 ⇒ 解析不出來要紅
assert_eval "psql 輸出無欄位分隔符" \
    "ERROR:  relation \"ir_module_module\" does not exist" "18.0.1.0.124" 1 "無法解析"

# 守住 evaluate_module_row 裡那個 \$'\n' 的坑（寫成 "\$(printf '\n')" 會讓每一列都被
# 判成多列 ⇒ 永遠紅）。這一條與下面 G1 成對：只有兩條同時綠，才代表換行判斷是對的。
assert_eval "psql 回多列" \
    "installed|18.0.1.0.124
installed|18.0.1.0.123" "18.0.1.0.124" 1 "多列"

assert_eval "latest_version 為空" \
    "installed|" "18.0.1.0.124" 1 "為空"

# =============================================================================
# 2. 綠燈（陽性對照）：一致時必須放行
# =============================================================================
echo "2. 綠燈（陽性對照）"

assert_eval "state=installed 且版號吻合" \
    "installed|18.0.1.0.124" "18.0.1.0.124" 0 "installed"

# =============================================================================
# 3. manifest 版號解析
# =============================================================================
echo "3. manifest 版號解析"

_MANIFEST_OK='{
    "name": "Woow PaaS Platform",
    "version": "18.0.1.0.124",
    "depends": ["base", "web"],
}'
assert_extract "雙引號 manifest" "$_MANIFEST_OK" 0 "18.0.1.0.124"

_MANIFEST_SQ="{
    'name': 'Woow PaaS Platform',
    'version': '18.0.1.0.124',
}"
assert_extract "單引號 manifest" "$_MANIFEST_SQ" 0 "18.0.1.0.124"

# CRLF：\r 若被帶進版號，字串比對會永遠不相等（假紅），且肉眼看不出來
_MANIFEST_CRLF="$(printf '{\r\n    "version": "18.0.1.0.124",\r\n}\r\n')"
assert_extract "CRLF manifest 不把 \\r 帶進版號" "$_MANIFEST_CRLF" 0 "18.0.1.0.124"

assert_extract "沒有 version 鍵" '{
    "name": "x",
}' 1 "找不到 version"

# 兩個 version 鍵 ⇒ 猜哪個都可能錯，寧可紅
assert_extract "兩個 version 鍵" '{
    "version": "18.0.1.0.124",
    "version": "18.0.1.0.99",
}' 1 "個 version 鍵"

assert_extract "version 值不是版號形態" '{
    "version": "latest",
}' 1 "不是版號形態"

# 檔案路徑版
_tmp_missing="${TMPDIR:-/tmp}/vav-does-not-exist-$$"
parse_manifest_version "$_tmp_missing" >/dev/null 2>&1
assert_eq "manifest 檔不存在 → rc=1" "$?" "1"

_tmp_ok="${TMPDIR:-/tmp}/vav-manifest-$$.py"
printf '%s\n' "$_MANIFEST_OK" > "$_tmp_ok"
assert_eq "parse_manifest_version 讀得出檔案版號" \
    "$(parse_manifest_version "$_tmp_ok" 2>/dev/null)" "18.0.1.0.124"
rm -f "$_tmp_ok"

# =============================================================================
# 4. DB 名推導（與 woow-dev plugin lib/common.sh 同一套規則）
# =============================================================================
echo "4. DB 名推導"

assert_eq "slug=stg" "$(compute_db_name_local stg woow_)" "woow_stg"
assert_eq "slug 內的 '-' 換成 '_'" "$(compute_db_name_local stg-test woow_)" "woow_stg_test"

# =============================================================================
# 5. 落後 upstream 的偵測（#723）
# =============================================================================
# 純本地 git，不碰網路：用同一個 repo 裡的另一條 branch 當 upstream
# （`branch.<n>.remote=.` 就是 git 對本地 upstream 的正規寫法）。
#
# 🔴 紅燈案例排前面同樣是刻意的：本函式「不落後」時的正確行為是**什麼都不印、回 1**，
#    而那與「函式壞掉」的輸出一模一樣。所以先證明它在該回 1 的每種情況都回 1，
#    最後那條「落後 2 個 commit」才是唯一有鑑別力的陽性對照。
echo "5. 落後 upstream 的偵測（#723）"

_git_tmp="${TMPDIR:-/tmp}/vav-git-$$"
_git() { git -C "$_git_tmp" -c user.email=t@t -c user.name=t "$@"; }

vav_repo_behind_count "" >/dev/null 2>&1
assert_eq "沒給 repo 路徑 → rc=1" "$?" "1"

vav_repo_behind_count "${TMPDIR:-/tmp}/vav-no-such-dir-$$" >/dev/null 2>&1
assert_eq "路徑不存在 → rc=1" "$?" "1"

mkdir -p "$_git_tmp"
vav_repo_behind_count "$_git_tmp" >/dev/null 2>&1
assert_eq "不是 git repo → rc=1" "$?" "1"

_git init -q 2>/dev/null
printf 'a\n' > "$_git_tmp/f"; _git add -A >/dev/null 2>&1; _git commit -qm a >/dev/null 2>&1
_git branch -M work >/dev/null 2>&1
_git branch upstream-sim >/dev/null 2>&1

vav_repo_behind_count "$_git_tmp" >/dev/null 2>&1
assert_eq "有 git repo 但沒設 upstream → rc=1" "$?" "1"

_git config branch.work.remote . >/dev/null 2>&1
_git config branch.work.merge refs/heads/upstream-sim >/dev/null 2>&1

vav_repo_behind_count "$_git_tmp" >/dev/null 2>&1
assert_eq "有 upstream 且同步 → rc=1（不落後就不該印）" "$?" "1"

# 只領先不落後：這正是舊寫法唯一看得見的那半邊，本函式不該對它有反應
printf 'b\n' > "$_git_tmp/f"; _git add -A >/dev/null 2>&1; _git commit -qm b >/dev/null 2>&1
vav_repo_behind_count "$_git_tmp" >/dev/null 2>&1
assert_eq "只領先 1（不落後）→ rc=1" "$?" "1"

# 陽性對照：upstream 前進 2 個 commit，work 落後 2
_git checkout -q upstream-sim >/dev/null 2>&1
printf 'u1\n' > "$_git_tmp/g"; _git add -A >/dev/null 2>&1; _git commit -qm u1 >/dev/null 2>&1
printf 'u2\n' > "$_git_tmp/g"; _git add -A >/dev/null 2>&1; _git commit -qm u2 >/dev/null 2>&1
_git checkout -q work >/dev/null 2>&1

_behind_out="$(vav_repo_behind_count "$_git_tmp" 2>/dev/null)"
assert_eq "落後 2 個 commit → rc=0" "$?" "0"
assert_eq "輸出 '<behind>\\t<upstream>'" "$_behind_out" "$(printf '2\tupstream-sim')"

rm -rf "$_git_tmp"
unset -f _git

# =============================================================================
echo ""
echo "──────── 通過 ${PASSED} / 失敗 ${FAILED}"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
