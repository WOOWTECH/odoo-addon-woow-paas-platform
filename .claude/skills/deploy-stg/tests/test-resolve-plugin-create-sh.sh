#!/usr/bin/env bash
#
# plugin-resolve.sh 的解析器自測（#728）
#
# 這支測試守的是 deploy-stg「要跑哪一份 create.sh」的判定。它為什麼非有不可：
# 解析錯了的形狀是**假綠** —— 落到 marketplace cache 的舊版照樣「找得到檔案」、
# helm 照樣綠、部署照樣成功，只是跑的是舊程式。**「我改的東西沒被執行」與
# 「我改的東西沒有效果」輸出完全一樣**（#728 現象段）。因此任何「路徑存在／
# 有解析到東西」的檢查都不具鑑別力，本測試一律斷言**解析到哪一條**。
#
# 🔴 案例分成兩組，兩組都不能省：
#   A. 修 #728 才會綠的（plugins-local / env override 的 marketplace 根形狀）
#   B. **修之前就該是綠、修之後仍必須是綠**的（cache glob、遞迴 fallback、
#      版本排序、fail-loud）。B 組就是「別把目前唯一在運作的路徑打壞」的守衛 ——
#      #728 最自然的修法（直接在 rel 前面補 plugins/woow-dev/）會讓 B 組全滅。
#
# 零外部相依：每個案例自建 fixture 目錄樹，HOME 換成 fixture 根，
# WOOWPRD_PLUGIN_ROOT 明確設定或明確 unset。不碰真的 ~/.claude、不碰叢集、
# 不碰網路，cwd 無關（路徑由 BASH_SOURCE 推）。
#
# 執行：bash .claude/skills/deploy-stg/tests/test-resolve-plugin-create-sh.sh
# Exit code：0 全過，1 有失敗。

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${TEST_DIR}/../scripts/plugin-resolve.sh"

if [ ! -f "$TARGET" ]; then
    echo "✗ 找不到被測檔：${TARGET}"
    exit 1
fi
# shellcheck source=../scripts/plugin-resolve.sh
. "$TARGET"

if ! declare -f resolve_plugin_create_sh >/dev/null; then
    echo "✗ source 了 ${TARGET} 但 resolve_plugin_create_sh 沒有被定義 —— 測試等於沒測到東西"
    exit 1
fi

REL="skills/k8s-dev-sandbox/scripts/create.sh"
PASSED=0
FAILED=0
FIX_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/plugin-resolve-test.XXXXXX")"
trap 'rm -rf "$FIX_ROOT"' EXIT

# --- helpers ---------------------------------------------------------------

mkfix() {
    # mkfix <case-name> → 印出該案例的 fixture 根（同時當假 HOME）
    local d="${FIX_ROOT}/$1"
    mkdir -p "$d"
    printf '%s\n' "$d"
}

mkcreate() {
    # mkcreate <create.sh 絕對路徑> —— 造出可辨識的假 create.sh
    mkdir -p "$(dirname "$1")"
    printf '#!/usr/bin/env bash\necho "fixture: %s"\n' "$1" >"$1"
    chmod +x "$1"
}

run_resolve() {
    # run_resolve <假 HOME> [WOOWPRD_PLUGIN_ROOT]；印出解析結果，回傳原 rc。
    # 用 subshell 隔離 env，避免案例之間互相污染（尤其是跑測試的人自己
    # shell 裡可能真的設了 WOOWPRD_PLUGIN_ROOT —— 那會讓案例靜默改變意義）。
    local h="$1" envroot="${2-}"
    (
        export HOME="$h"
        if [ -n "$envroot" ]; then
            export WOOWPRD_PLUGIN_ROOT="$envroot"
        else
            unset WOOWPRD_PLUGIN_ROOT
        fi
        resolve_plugin_create_sh
    )
}

pass() { PASSED=$((PASSED + 1)); echo "  ✓ $1"; }
fail() { FAILED=$((FAILED + 1)); echo "  ✗ $1"; }

assert_resolves_to() {
    # assert_resolves_to <label> <期望路徑> <假 HOME> [env override]
    local label="$1" want="$2" h="$3" envroot="${4-}"
    local got rc
    got="$(run_resolve "$h" "$envroot")"
    rc=$?
    if [ "$rc" -ne 0 ]; then
        fail "${label}：期望解析到 ${want}，但回傳非零（rc=${rc}）"
        return
    fi
    if [ "$got" != "$want" ]; then
        fail "${label}
      期望：${want}
      實際：${got}"
        return
    fi
    if [ ! -f "$got" ]; then
        fail "${label}：解析結果不是實際存在的檔案：${got}"
        return
    fi
    if [ "$(printf '%s\n' "$got" | wc -l | tr -d ' ')" != "1" ]; then
        fail "${label}：stdout 不是單一路徑（呼叫端會整條吃進變數）"
        return
    fi
    pass "$label"
}

assert_fails() {
    # assert_fails <label> <假 HOME> [env override]
    local label="$1" h="$2" envroot="${3-}"
    local got rc
    got="$(run_resolve "$h" "$envroot")"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        fail "${label}：期望非零退出（fail-loud），實際 rc=0 且輸出 '${got}'"
        return
    fi
    if [ -n "$got" ]; then
        fail "${label}：失敗時 stdout 應為空，實際輸出 '${got}'"
        return
    fi
    pass "$label"
}

# ===========================================================================
# A 組：#728 —— 修好才會綠
# ===========================================================================
echo "── A 組：plugins-local dev copy 與 env override（#728 本體）"

# A1 🔴 本 issue 的核心。fixture 刻意讓 cache 也有一份 —— 這正是現場的情況，
#    也是為什麼「有沒有找到檔案」這種檢查驗不出東西：兩份都在，錯的那份也存在。
h="$(mkfix a1)"
mkcreate "${h}/.claude/plugins-local/woowprd/plugins/woow-dev/${REL}"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}"
assert_resolves_to "A1 plugins-local 的 dev copy 優先於 cache（cache 同時存在）" \
    "${h}/.claude/plugins-local/woowprd/plugins/woow-dev/${REL}" "$h"

# A2 plugin 目錄舊名相容（marketplace 目錄名不變、plugin 目錄名還沒改的情況）
h="$(mkfix a2)"
mkcreate "${h}/.claude/plugins-local/woowprd/plugins/woowprd/${REL}"
assert_resolves_to "A2 plugins-local 的舊 plugin 目錄名 woowprd 也命中" \
    "${h}/.claude/plugins-local/woowprd/plugins/woowprd/${REL}" "$h"

# A3 marketplace 目錄本身被改名成 woow-dev 的情況（repo 端硬編 woowprd 的相容面）
h="$(mkfix a3)"
mkcreate "${h}/.claude/plugins-local/woow-dev/plugins/woow-dev/${REL}"
assert_resolves_to "A3 plugins-local 的 marketplace 目錄名改成 woow-dev 也命中" \
    "${h}/.claude/plugins-local/woow-dev/plugins/woow-dev/${REL}" "$h"

# A4 env override 指到 **plugin 根**（＝註解宣告的語意）
h="$(mkfix a4)"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}"
envroot="${FIX_ROOT}/a4-plugin-root"
mkcreate "${envroot}/${REL}"
assert_resolves_to "A4 env override 指 plugin 根，優先於 cache" \
    "${envroot}/${REL}" "$h" "$envroot"

# A5 env override 指到 **marketplace/repo 根**。env 名字取自 marketplace（woowprd）、
#    plugin 目錄卻叫 woow-dev，指錯層是可預期的誤用 —— 兩種形狀都要接得住。
h="$(mkfix a5)"
envroot="${FIX_ROOT}/a5-repo-root"
mkcreate "${envroot}/plugins/woow-dev/${REL}"
assert_resolves_to "A5 env override 指 marketplace/repo 根也命中" \
    "${envroot}/plugins/woow-dev/${REL}" "$h" "$envroot"

# A6 env override 優先於 plugins-local dev copy
h="$(mkfix a6)"
mkcreate "${h}/.claude/plugins-local/woowprd/plugins/woow-dev/${REL}"
envroot="${FIX_ROOT}/a6-plugin-root"
mkcreate "${envroot}/${REL}"
assert_resolves_to "A6 env override 優先於 plugins-local" \
    "${envroot}/${REL}" "$h" "$envroot"

# ===========================================================================
# B 組：目前唯一在運作的路徑 —— 修之前就綠，修之後**仍必須綠**
# ===========================================================================
echo "── B 組：cache 路徑不得被打壞（優先序 3、4 的守衛）"

# B1 優先序 3：cache glob。plugin 根形狀（cache/<mkt>/<plugin>/<ver>/skills/...），
#    與 plugins-local 的 marketplace 根形狀不同 —— 這就是「單一 rel 服務不了兩種
#    base」的那個分歧點。把 plugins/<name>/ 一律補進 rel 的修法會讓本案例變紅。
h="$(mkfix b1)"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}"
assert_resolves_to "B1 cache glob 命中（優先序 3）" \
    "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}" "$h"

# B2 同名多版本並存 → version-sort 取最新（字典序會選錯：1.10.0 < 1.9.0）
h="$(mkfix b2)"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.9.0/${REL}"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.10.0/${REL}"
assert_resolves_to "B2 cache 多版本取最新 1.10.0（不是字典序的 1.9.0）" \
    "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.10.0/${REL}" "$h"

# B3 現行名 woow-dev 優先於舊名 woowprd
h="$(mkfix b3)"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woowprd/9.9.9/${REL}"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}"
assert_resolves_to "B3 cache 中 woow-dev 優先於舊名 woowprd（不是比版號）" \
    "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}" "$h"

# B4 優先序 4：plugin 目錄名不在 {woow-dev,woowprd} 清單內 → 遞迴 fallback 撿回來
h="$(mkfix b4)"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-renamed-again/2.0.0/${REL}"
assert_resolves_to "B4 遞迴 fallback：plugin 改名成清單外的名字仍找得到（優先序 4）" \
    "${h}/.claude/plugins/cache/woowprd-local/woow-renamed-again/2.0.0/${REL}" "$h"

# B5 fail-loud：什麼都沒有時必須非零退出。沒有這一格，「永遠回傳某個東西」
#    的壞解析器會把上面每一條都騙過去。
h="$(mkfix b5)"
mkdir -p "${h}/.claude/plugins/cache"
assert_fails "B5 全部落空 → 非零退出、stdout 空（cache 根存在但空）" "$h"

# B6 cache 根整個不存在（新機器）。glob 不匹配會留 literal —— 那個 literal 路徑
#    當然 `[ -f ]` 不成立，這一格就是在守 nullglob off 的既有設計沒被改掉。
h="$(mkfix b6)"
assert_fails "B6 cache 根不存在 → 非零退出（glob 留 literal 不誤判）" "$h"

# B7 env override 指到不存在的檔 → 不是直接失敗，要往下走（註解宣告的語意）
h="$(mkfix b7)"
mkcreate "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}"
assert_resolves_to "B7 env override 落空時往下走，不是直接失敗" \
    "${h}/.claude/plugins/cache/woowprd-local/woow-dev/1.0.0/${REL}" \
    "$h" "${FIX_ROOT}/b7-nonexistent"

# ===========================================================================
# C 組：fail-loud 訊息的鑑別力
# ===========================================================================
echo "── C 組：失敗訊息必須與實際找過的路徑一致"

# C1 使用者唯一的線索是「已找：…」那份清單。它若是手寫的常數，就會與實際查找
#    的路徑漂開（#728 就是這樣：文件說 override 可用、實際組出來的路徑不對），
#    而漂開的當下沒有任何訊號。故要求清單由查找過程回填、由同一份資料印出。
h="$(mkfix c1)"
tried_out="$(
    export HOME="$h"
    unset WOOWPRD_PLUGIN_ROOT
    resolve_plugin_create_sh >/dev/null
    if ! declare -f plugin_resolve_tried_report >/dev/null; then
        echo "__NO_REPORT_FN__"
    else
        plugin_resolve_tried_report
    fi
)"
c1_ok=1
c1_why=""
if [ "$tried_out" = "__NO_REPORT_FN__" ]; then
    c1_ok=0
    c1_why="沒有 plugin_resolve_tried_report()：失敗訊息只能手寫常數，必然與實際查找漂開"
else
    for want in \
        "${h}/.claude/plugins-local/woowprd/plugins/woow-dev/${REL}" \
        "${h}/.claude/plugins/cache/*/woow-dev/*/${REL}" \
        "${h}/.claude/plugins/cache"; do
        case "$tried_out" in
            *"$want"*) ;;
            *) c1_ok=0; c1_why="回報的候選清單少了：${want}" ;;
        esac
    done
fi
if [ "$c1_ok" -eq 1 ]; then
    pass "C1 失敗時可取得實際找過的候選清單（含 plugins-local、cache glob、遞迴根）"
else
    fail "C1 ${c1_why}
      實際回報：${tried_out}"
fi

# ===========================================================================
echo ""
echo "──────── 通過 ${PASSED}，失敗 ${FAILED}"
[ "$FAILED" -eq 0 ] || exit 1
