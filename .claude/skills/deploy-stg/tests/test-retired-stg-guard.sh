#!/usr/bin/env bash
#
# dev-sandbox-stg 退役守衛自測（woow-paas/paas-app-bundle-cd#289、#342）
#
# 守的是兩件事：
#   A. 判定層（retired-stg.sh 的純函式）：stg 的各種寫法都被認出來，其他 slug 不誤擋。
#   B. 接線：deploy-stg.sh／verify-addon-version.sh 對 stg（含未給 slug）在**任何外部
#      工具被呼叫之前**就非 0 退出；其他 slug 會走過守衛（陽性對照）。
#
# 🔴 B 組的安全網：兩支腳本都在「只有 stub 的 PATH」底下跑。kubectl／helm／tea／git／
#    jq 都是只寫 log、回 1 的假工具，PATH 裡沒有真的版本 ⇒ 就算守衛壞了，測試也不可能
#    碰到叢集、Gitea 或網路。stub log 同時是判準：stg 案例 log 必須是空的（守衛先擋），
#    陽性對照 log 必須非空（證明守衛放行後真的往下走了，否則「全部被擋」也會全綠）。
#
# 零外部相依：不碰 kubeconfig／叢集／網路，cwd 無關。
#
# 執行：bash .claude/skills/deploy-stg/tests/test-retired-stg-guard.sh
# Exit code：0 全過，1 有失敗。

set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="${TEST_DIR}/../scripts"
LIB="${SCRIPTS_DIR}/retired-stg.sh"
DEPLOY="${SCRIPTS_DIR}/deploy-stg.sh"
VERIFY="${SCRIPTS_DIR}/verify-addon-version.sh"

for f in "$LIB" "$DEPLOY" "$VERIFY"; do
    if [ ! -f "$f" ]; then
        echo "✗ 找不到被測檔：${f}"
        exit 1
    fi
done

# shellcheck source=../scripts/retired-stg.sh
. "$LIB"
if ! declare -f dstg_is_retired_target >/dev/null; then
    echo "✗ source 了 ${LIB} 但 dstg_is_retired_target 沒有被定義 —— 測試等於沒測到東西"
    exit 1
fi

PASSED=0
FAILED=0
_report() {
    if [ "$1" = "ok" ]; then
        PASSED=$((PASSED + 1))
        printf '  ✓ %s\n' "$2"
    else
        FAILED=$((FAILED + 1))
        printf '  ✗ %s\n      %s\n' "$2" "$3"
    fi
}
_contains() { case "$2" in *"$1"*) return 0 ;; *) return 1 ;; esac; }

# =============================================================================
# A. 判定層
# =============================================================================
echo "A. 判定層（retired-stg.sh）"

assert_retired() {
    # assert_retired <描述> <want: yes|no> <slug> [namespace]
    local desc="$1" want="$2" got
    if dstg_is_retired_target "$3" "${4:-}"; then got=yes; else got=no; fi
    if [ "$got" = "$want" ]; then
        _report ok "$desc"
    else
        _report fail "$desc" "slug='$3' ns='${4:-}' → ${got}（期望 ${want}）"
    fi
}

# 紅燈（該擋）在前：先證明每種寫法都會被認出來
assert_retired "slug=stg 擋"                       yes "stg"
assert_retired "slug=STG（大寫，create.sh 會轉小寫）擋" yes "STG"
assert_retired "slug=stg.（標點會被換成 - 再去尾）擋"   yes "stg."
assert_retired "slug=-stg-（頭尾 - 會被去掉）擋"        yes "-stg-"
assert_retired "slug=' stg '（空白）擋"               yes " stg "
assert_retired "ns=dev-sandbox-stg 擋（slug 空）"      yes "" "dev-sandbox-stg"
assert_retired "ns=dev-sandbox-stg 擋（slug 是別的）"   yes "other" "dev-sandbox-stg"
# 綠燈（不該擋）：其他沙盒照常
assert_retired "slug=stg-test 不擋"                    no "stg-test"
assert_retired "slug=stg2 不擋"                        no "stg2"
assert_retired "slug=aimodels 不擋"                    no "aimodels"
assert_retired "slug=llmdev＋對應 ns 不擋"             no "llmdev" "dev-sandbox-llmdev"
assert_retired "ns=paas-stg-system 不擋（不是舊沙盒）"  no "" "paas-stg-system"
assert_retired "slug 與 ns 都空不擋（由呼叫端要求必填）" no "" ""

got="$(dstg_normalize_slug 'Feat/My_Branch--X.')"
if [ "$got" = "feat-my-branch-x" ]; then
    _report ok "正規化與 plugin compute_slug 同規則（Feat/My_Branch--X. → feat-my-branch-x）"
else
    _report fail "正規化與 plugin compute_slug 同規則" "得到 '${got}'"
fi

notice="$(dstg_print_retired_notice "x" 2>&1 >/dev/null)"
missing=""
for needle in "paas-stg-system" "paas-odoo-platform-stg" "woow_stg" "paas-app-bundle-cd" \
              "values-stg.yaml" "platform-odoo-stg-runbook.md" "沒有繞過旗標"; do
    _contains "$needle" "$notice" || missing="${missing} ${needle}"
done
if [ -z "$missing" ]; then
    _report ok "退役說明印到 stderr，且含新 release／換版流程／runbook 指引"
else
    _report fail "退役說明內容" "缺少：${missing}"
fi

# =============================================================================
# B. 接線（只有 stub 的 PATH）
# =============================================================================
echo "B. 接線（stub PATH，叢集／Gitea／網路皆不可達）"

FIX_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/retired-stg-test.XXXXXX")"
trap 'rm -rf "$FIX_ROOT"' EXIT
STUB_BIN="${FIX_ROOT}/bin"
STUB_LOG="${FIX_ROOT}/stub.log"
mkdir -p "$STUB_BIN"

# 會碰外部的工具一律換成 stub：記錄後回 1
for tool in kubectl helm tea git jq curl python3 say; do
    cat >"${STUB_BIN}/${tool}" <<EOF
#!/bin/sh
echo "${tool} \$*" >>"${STUB_LOG}"
exit 1
EOF
    chmod +x "${STUB_BIN}/${tool}"
done
# 純本機的 coreutils 連過去（腳本解析路徑、文字處理會用到）
for tool in dirname basename cat sed tr awk grep mktemp rm mkdir cut sort head tail date seq printf env; do
    real="$(command -v "$tool" 2>/dev/null || true)"
    case "$real" in /*) ln -sf "$real" "${STUB_BIN}/${tool}" ;; esac
done
BASH_BIN="$(command -v bash)"

run_stubbed() {
    # run_stubbed <script> <args...> → 設 RUN_RC、RUN_OUT（stdout+stderr）
    : >"$STUB_LOG"
    RUN_OUT="$(cd "$FIX_ROOT" && env -i HOME="$FIX_ROOT" PATH="$STUB_BIN" \
        "$BASH_BIN" "$@" 2>&1)"
    RUN_RC=$?
}

assert_blocked() {
    # assert_blocked <描述> <script> <args...>
    local desc="$1"; shift
    run_stubbed "$@"
    if [ "$RUN_RC" -eq 0 ]; then
        _report fail "$desc" "rc=0（期望非 0）"; return
    fi
    if [ -s "$STUB_LOG" ]; then
        _report fail "$desc" "守衛之前就呼叫了外部工具：$(tr '\n' ';' <"$STUB_LOG")"; return
    fi
    if ! _contains "paas-stg-system" "$RUN_OUT" || ! _contains "platform-odoo-stg-runbook.md" "$RUN_OUT"; then
        _report fail "$desc" "輸出沒有退役說明：$(printf '%s' "$RUN_OUT" | head -5)"; return
    fi
    _report ok "$desc（rc=${RUN_RC}，零外部呼叫）"
}

assert_passes_guard() {
    # assert_passes_guard <描述> <script> <args...> —— 陽性對照：不得出現退役說明，且有往下走
    local desc="$1"; shift
    run_stubbed "$@"
    if _contains "已退役的 dev-sandbox-stg" "$RUN_OUT"; then
        _report fail "$desc" "其他 slug 被退役守衛誤擋"; return
    fi
    if [ ! -s "$STUB_LOG" ]; then
        _report fail "$desc" "沒有任何 stub 被呼叫，無從證明守衛之後的流程有被執行：$(printf '%s' "$RUN_OUT" | tail -3)"; return
    fi
    _report ok "$desc（走過守衛，第一個外部呼叫：$(head -1 "$STUB_LOG")）"
}

assert_blocked "deploy-stg.sh 不帶 --slug（舊預設 stg）擋" "$DEPLOY"
assert_blocked "deploy-stg.sh --slug stg 擋"               "$DEPLOY" --slug stg
assert_blocked "deploy-stg.sh --slug STG 擋"               "$DEPLOY" --slug STG
assert_blocked "deploy-stg.sh --slug stg --image … 擋（--image 路徑不例外）" \
    "$DEPLOY" --slug stg --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:20261022.1620
assert_blocked "verify-addon-version.sh 不帶 --slug/--namespace 擋" "$VERIFY" --no-self-test
assert_blocked "verify-addon-version.sh --slug stg 擋"      "$VERIFY" --slug stg --no-self-test
assert_blocked "verify-addon-version.sh --namespace dev-sandbox-stg 擋" \
    "$VERIFY" --namespace dev-sandbox-stg --no-self-test

assert_passes_guard "deploy-stg.sh --slug my-sandbox 走過守衛" "$DEPLOY" --slug my-sandbox
assert_passes_guard "verify-addon-version.sh --slug my-sandbox 走過守衛" \
    "$VERIFY" --slug my-sandbox --no-self-test --expected 18.0.1.0.1 --db woow_my_sandbox --timeout 0

echo ""
echo "合計：${PASSED} 通過、${FAILED} 失敗"
[ "$FAILED" -eq 0 ]
