#!/usr/bin/env bash
# Unit tests for .claude/skills/lib/kube-context.sh（#32）
# Run:  bash .claude/skills/lib/kube-context_test.sh
#
# 全程使用假 kubeconfig，不接觸任何真實叢集，也不改動 ~/.kube/config
# （最後一項測試就是驗這件事）。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/kube-context.sh"

fails=0
assert_eq() { # name actual expected
    if [[ "$2" == "$3" ]]; then echo "ok: $1"; else echo "FAIL: $1 — got [$2] want [$3]"; fails=$((fails+1)); fi
}

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

# =============================================================================
# wk_allowed_contexts — 解析 .sandbox.yml
# =============================================================================
cat > "$T/.sandbox.yml" <<'YML'
module: whatever
allowed_kube_contexts: [fallback-a, fallback-b]
allowed_kube_contexts_stg: [prod-cluster]
allowed_kube_contexts_local:
  - dev-a
  - dev-b
allowed_kube_contexts_ci_local: [dev-a, prod-cluster]
config_params:
  some.key: "not a context"       # 相鄰的縮排 map 不可被吃進白名單
YML
assert_eq "stg (inline)"          "$(wk_allowed_contexts stg "$T")"      "prod-cluster"
assert_eq "local (block form)"    "$(wk_allowed_contexts local "$T")"    "dev-a dev-b"
assert_eq "mode dash→底線"         "$(wk_allowed_contexts ci-local "$T")" "dev-a prod-cluster"
assert_eq "未宣告 task 退回無後綴"  "$(wk_allowed_contexts destroy "$T")"  "fallback-a fallback-b"

cat > "$T/.sandbox.yml" <<'YML'
module: whatever
YML
assert_eq "完全未宣告→空" "$(wk_allowed_contexts stg "$T")" ""
assert_eq "沒有 .sandbox.yml→空" "$(wk_allowed_contexts stg "$T/nope")" ""

# 真檔對照：本 repo 自己的 .sandbox.yml 必須解析得出東西。
# 少了這條，上面全部用合成 fixture 的 ok 只證明了 parser 會動，沒證明它讀得懂實際那份。
REPO_ROOT_REAL="$(cd "${HERE}/../../.." && pwd)"
if [[ -f "$REPO_ROOT_REAL/.sandbox.yml" ]]; then
    assert_eq "真 .sandbox.yml stg" "$(wk_allowed_contexts stg "$REPO_ROOT_REAL")" "woow-k3s"
    assert_eq "真 .sandbox.yml local" "$(wk_allowed_contexts local "$REPO_ROOT_REAL")" "docker-desktop"
    # ci-local 必須同時含兩者：程式碼打進 image，遠端叢集也是正規用法（#433）。
    # 只列 docker-desktop 會 fail-closed 擋掉有文件的流程。
    assert_eq "真 .sandbox.yml ci-local" "$(wk_allowed_contexts ci-local "$REPO_ROOT_REAL")" "docker-desktop woow-k3s"
else
    echo "FAIL: 找不到 repo 的 .sandbox.yml（$REPO_ROOT_REAL）"; fails=$((fails+1))
fi

# =============================================================================
# wk_require_context — 陽性對照 + kubeconfig 隔離
# =============================================================================
cat > "$T/kubeconfig.yaml" <<'YML'
apiVersion: v1
kind: Config
current-context: ctx-right
clusters:
- {name: c1, cluster: {server: 'https://127.0.0.1:59996'}}
contexts:
- {name: ctx-right, context: {cluster: c1, user: u}}
- {name: ctx-wrong, context: {cluster: c1, user: u}}
users:
- {name: u, user: {token: t}}
YML
mkdir -p "$T/allow"; cat > "$T/allow/.sandbox.yml" <<'YML'
allowed_kube_contexts_stg: [ctx-right]
YML

OUT="$T/out.txt"
REJECT='不是這次任務該用的叢集'
SWITCH='Using kubectl context:'

# 判準說明（與 plugin 端測試同一套教訓）：
#   - 不能用 rc —— 這裡沒有 cluster-info，rc 反而只反映白名單，但仍不足以區分
#     「印了訊息」與「真的停下來」。
#   - 不能只看拒絕訊息有沒有印 —— 訊息在 `return 1` 之前，拿掉 return 照印。
#   真正的判準是「有沒有走到切換那一步」：被擋下時 KUBECONFIG 不得被前置 overlay。
probe() { # <ctx> <task> <repo>
    (
        unset _WK_OVERLAY HELM_KUBECONTEXT
        export KUBECONFIG="$T/kubeconfig.yaml"
        wk_require_context "$1" "$2" "$3" > "$OUT" 2>&1
        if [[ "$KUBECONFIG" != "$T/kubeconfig.yaml" ]]; then printf 'yes'; else printf 'no'; fi
    )
}

if command -v kubectl >/dev/null 2>&1; then
    # 陽性對照：白名單是 ctx-right，餵 ctx-wrong ⇒ 必須在切換前擋下
    r="$(probe ctx-wrong stg "$T/allow")"
    if [[ "$r" == "no" ]] && grep -q "$REJECT" "$OUT" && ! grep -q "$SWITCH" "$OUT"; then
        echo "ok: 陽性對照 — 錯的 context 在切換前被擋下"
    else
        echo "FAIL: 錯的 context 沒被擋下 (reached_switch=${r})"; fails=$((fails+1))
    fi

    # 陰性對照：名單內的 context ⇒ 必須走到切換（證明它在分辨，不是一律拒絕）
    r="$(probe ctx-right stg "$T/allow")"
    if [[ "$r" == "yes" ]] && ! grep -q "$REJECT" "$OUT"; then
        echo "ok: 名單內的 context 通過並走到切換"
    else
        echo "FAIL: 名單內的 context 被擋下 (reached_switch=${r})"; fails=$((fails+1))
    fi

    # 未宣告白名單 ⇒ 放行 + 出聲
    r="$(probe ctx-wrong stg "$T")"
    if [[ "$r" == "yes" ]]; then echo "ok: 未宣告白名單→放行"
    else echo "FAIL: 未宣告白名單卻擋下"; fails=$((fails+1)); fi
    if grep -q '值域守衛未武裝' "$OUT"; then echo "ok: 未宣告白名單有出聲"
    else echo "FAIL: 未宣告白名單靜默放行"; fails=$((fails+1)); fi

    # 逃生門
    r="$(SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1 probe ctx-wrong stg "$T/allow")"
    if [[ "$r" == "yes" ]]; then echo "ok: SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1 可覆寫"
    else echo "FAIL: 逃生門沒生效"; fails=$((fails+1)); fi

    # 隔離：切換後原 kubeconfig 的 current-context 不變，本次執行看到的是目標 context
    p="$(
        unset _WK_OVERLAY HELM_KUBECONTEXT
        export KUBECONFIG="$T/kubeconfig.yaml"
        wk_require_context ctx-wrong "" "$T" >/dev/null 2>&1
        printf '%s\t%s' "$(KUBECONFIG="$T/kubeconfig.yaml" kubectl config current-context)" \
                        "$(kubectl config current-context)"
    )"
    assert_eq "隔離：原 kubeconfig 未被改動"     "${p%%$'\t'*}" "ctx-right"
    assert_eq "隔離：本次執行看到目標 context"   "${p#*$'\t'}"  "ctx-wrong"

    # overlay 失效 ⇒ 必須 fail-loud，不得靜默 fallback 回原 kubeconfig。
    # 請求的 context 必須與 base 的 current-context 不同，否則 fallback 與成功
    # 產生同一個觀測值，測試恆綠。
    OV="$T/ovtmp"; mkdir -p "$OV/woow-paas-kubeconfig/overlay-$$.yaml"
    (
        unset _WK_OVERLAY HELM_KUBECONTEXT
        export KUBECONFIG="$T/kubeconfig.yaml" TMPDIR="$OV"
        wk_require_context ctx-wrong "" "$T" > "$OUT" 2>&1
    ) || true
    if grep -q '無法寫入 kubeconfig overlay' "$OUT" && ! grep -q "$SWITCH" "$OUT"; then
        echo "ok: overlay 失效時在隔離關就停住"
    else
        echo "FAIL: overlay 失效沒擋住 — 會靜默打到 default context"; fails=$((fails+1))
    fi
    rm -rf "$OV"

    # 最後對照：整輪測試跑完，真實 kubeconfig 一次都不該被碰到
    assert_eq "真實 kubeconfig 未被改動" \
        "$(KUBECONFIG="$T/kubeconfig.yaml" kubectl config current-context)" "ctx-right"
else
    echo "skip: kubectl 不在 PATH，跳過 wk_require_context 測試"
fi

echo "----------------------------------------"
if [[ $fails -eq 0 ]]; then echo "ALL PASS"; else echo "$fails FAILED"; exit 1; fi
