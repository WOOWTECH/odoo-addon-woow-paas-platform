#!/usr/bin/env bash
#
# kube-context.sh — 本 repo 各 skill script 共用的 kubectl context 守衛（issue #32）
#
# 解決兩件事：
#
#   Part A｜kubeconfig 污染
#     `kubectl config use-context` 改的是 ~/.kube/config，腳本跑完操作者 shell 的
#     default context 就停在腳本設的值 —— footgun 只是被推遲到下一條指令。
#     這裡改用 KUBECONFIG overlay：寫一份「只有 current-context」的暫存檔，前置到
#     KUBECONFIG 搜尋鏈。kubectl / helm 合併多檔時純量取鏈上第一個有定義的檔 ⇒
#     overlay 勝出；clusters / users 仍從原檔合併（不複製憑證、相對路徑仍相對原檔
#     解析）。`use-context` 也只會寫進 overlay。~/.kube/config 全程不動。
#
#   Part B｜值域白名單
#     原本的檢查只驗「這個 context 可用」（存在 + 連得上），驗不了「這個 context 是
#     這次任務該用的那一個」。docker-desktop 兩關全過，卻是 stg 部署最不該打的叢集
#     （#722）。白名單值從 repo 根的 .sandbox.yml 讀 —— 與 woow-dev plugin 同一個
#     載體、同一個鍵名，兩邊守衛看的是同一份宣告。
#
# 為什麼不 source plugin 的 lib/common.sh：本 repo 的 script 目前都不 source 它
# （deploy-stg.sh 只解析出 create.sh 的「路徑」），且 plugin 解析可能命中
# ~/.claude/plugins/cache 下的舊版 —— 舊版沒有這些函式，會變成靜默沒有守衛。
# 故 repo 端自帶一份（沿用 verify-addon-version.sh 對 compute_db_name 的同樣取捨）。
#
# Usage:
#   source "<repo>/.claude/skills/lib/kube-context.sh"
#   wk_require_context "$KUBE_CONTEXT" stg      # 驗證 + 隔離 + 切換
#
# 逃生門：SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1 明示覆寫白名單（會大聲警告）。

# --- 白名單讀取 ---------------------------------------------------------------
# 讀 <repo>/.sandbox.yml 的 allowed_kube_contexts[_<task>]，支援 inline [a, b] 與
# 區塊 "- a" 兩種寫法（與 plugin parser 同語法）。task 名的 '-' 底線化，對齊
# config_params_<mode> 慣例（ci-local → ci_local）。找不到 task 專屬條目就退回無後綴
# 的 allowed_kube_contexts。輸出空格分隔；沒宣告則輸出空字串。
wk_allowed_contexts() {
    local task="${1:-}" repo="${2:-${WK_REPO_ROOT:-}}"
    task="${task//-/_}"
    [[ -n "$repo" && -f "${repo}/.sandbox.yml" ]] || return 0
    awk -v task="$task" '
        # 剝行內註解與 CR
        { sub(/\r$/, ""); sub(/#.*/, "") }
        # 頂層鍵：allowed_kube_contexts[_<task>]
        /^allowed_kube_contexts[a-zA-Z0-9_]*:/ {
            split($0, kv, ":")
            key = kv[1]
            scope = (key == "allowed_kube_contexts") ? "*" : substr(key, length("allowed_kube_contexts_") + 1)
            rest = substr($0, index($0, ":") + 1)
            gsub(/^[ \t]+|[ \t]+$/, "", rest)
            cur = scope
            if (rest ~ /^\[.*\]$/) {                       # inline list
                gsub(/^\[|\]$/, "", rest)
                n = split(rest, items, ",")
                for (i = 1; i <= n; i++) { emit(cur, items[i]) }
                cur = ""
            }
            next
        }
        /^[a-zA-Z_]/ { cur = ""; next }                    # 其他頂層鍵 → 離開區塊
        /^[ \t]+-[ \t]*/ {                                 # 區塊列表項
            if (cur == "") next
            item = $0
            sub(/^[ \t]+-[ \t]*/, "", item)
            emit(cur, item)
            next
        }
        function emit(scope, v) {
            gsub(/^[ \t]+|[ \t]+$/, "", v)
            gsub(/^["'"'"']|["'"'"']$/, "", v)
            if (v == "") return
            if (scope == task) { scoped = scoped sep1 v; sep1 = " " }
            else if (scope == "*") { base = base sep2 v; sep2 = " " }
        }
        END { print (scoped != "" ? scoped : base) }
    ' "${repo}/.sandbox.yml"
}

# --- kubeconfig 隔離 -----------------------------------------------------------
# 刻意不裝 trap EXIT：呼叫端可能自己有 EXIT trap，而 bash 的 trap 是取代不是疊加，
# 裝了會被靜默蓋掉。改為進入時掃除 PID 已消失的殘留。overlay 只含 context 名、不含
# 任何憑證，殘留無安全影響。
wk_isolate_kubeconfig() {
    local ctx="$1"
    local dir="${TMPDIR:-/tmp}/woow-paas-kubeconfig"
    if ! mkdir -p "$dir" 2>/dev/null; then
        echo "ERROR: 無法建立 kubeconfig overlay 目錄 ${dir}；沒有隔離就只能改全域 kubeconfig，拒絕繼續。" >&2
        return 1
    fi
    chmod 700 "$dir" 2>/dev/null || true

    # 判準刻意保守：PID 已不存在 **且** mtime 超過 24h 才刪。只看 PID 不夠 —— PID 會被
    # 回收，誤刪一個活著的 overlay 沒有任何錯誤訊息，只會讓那支腳本剩下的 kubectl 靜默
    # fallback 回操作者的全域 kubeconfig。
    local f pid
    for f in "$dir"/overlay-*.yaml; do
        [[ -e "$f" ]] || continue
        pid="${f##*/overlay-}"; pid="${pid%.yaml}"
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        [[ "$pid" == "$$" ]] && continue
        kill -0 "$pid" 2>/dev/null && continue
        [[ -n "$(find "$f" -mmin +1440 2>/dev/null)" ]] && rm -f "$f"
    done

    # 不 export _WK_OVERLAY：子行程要有自己的 overlay，否則它切 context 會回頭改到
    # 父行程那一份，父行程之後的 kubectl 就打到子行程的叢集。
    if [[ -z "${_WK_OVERLAY:-}" ]]; then
        _WK_OVERLAY="${dir}/overlay-$$.yaml"
        export KUBECONFIG="${_WK_OVERLAY}:${KUBECONFIG:-$HOME/.kube/config}"
    fi
    # 寫不進去必須 fail-loud：KUBECONFIG 鏈的第一個檔缺席時，kubectl 是**靜默**
    # fallback 到下一個檔（rc=0、無警告），本次執行整段改打操作者的 default context，
    # 而所有訊息與成功一模一樣。
    if ! printf 'apiVersion: v1\nkind: Config\ncurrent-context: %s\n' "$ctx" > "$_WK_OVERLAY" 2>/dev/null; then
        echo "ERROR: 無法寫入 kubeconfig overlay：${_WK_OVERLAY}" >&2
        echo "  overlay 缺席時 kubectl 會靜默改用你的全域 kubeconfig —— 拒絕繼續。" >&2
        return 1
    fi

    # helm 的 HELM_KUBECONTEXT 會壓過 kubeconfig 的 current-context，而 kubectl 完全
    # 忽略它 ⇒ 環境裡殘留一個值就能做出「kubectl 打 A、helm 打 B」的 split-brain，
    # 而所有前置檢查都是 kubectl 做的、不會有紅燈。釘成同值讓兩邊不可能分家。
    if [[ -n "${HELM_KUBECONTEXT:-}" && "${HELM_KUBECONTEXT}" != "$ctx" ]]; then
        echo "WARNING: HELM_KUBECONTEXT='${HELM_KUBECONTEXT}' 與目標 '${ctx}' 不符 — 覆寫為 '${ctx}'。" >&2
    fi
    export HELM_KUBECONTEXT="$ctx"
}

# --- 完整守衛 -----------------------------------------------------------------
# Usage: wk_require_context <context> <task> [repo-root]
#   task     白名單的 scope（如 stg / e2e-seed）；也用在訊息裡講清楚這次任務是什麼
#   repo-root 省略時取 $WK_REPO_ROOT
wk_require_context() {
    local ctx="${1:-}" task="${2:-}" repo="${3:-${WK_REPO_ROOT:-}}"
    local allowlist

    if ! command -v kubectl >/dev/null 2>&1; then
        echo "ERROR: kubectl not found." >&2; return 1
    fi
    if [[ -z "$ctx" ]]; then
        echo "ERROR: kubectl context is required (--context <name> 或 export KUBE_CONTEXT)。" >&2
        kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
        return 1
    fi
    if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$ctx"; then
        echo "ERROR: kubectl context '${ctx}' not found in kubeconfig." >&2
        kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
        return 1
    fi

    allowlist="$(wk_allowed_contexts "$task" "$repo")"
    if [[ -z "$allowlist" ]]; then
        echo "WARNING: .sandbox.yml 沒有 allowed_kube_contexts_${task//-/_}（也沒有 allowed_kube_contexts）" >&2
        echo "         — context 值域守衛未武裝，只驗了可用性。" >&2
    elif [[ " ${allowlist} " == *" ${ctx} "* ]]; then
        echo "Context allowlist (${task}): ${allowlist} — '${ctx}' 在名單內。"
    elif [[ "${SANDBOX_ALLOW_ANY_KUBE_CONTEXT:-}" == "1" ]]; then
        echo "WARNING: '${ctx}' 不在白名單 [${allowlist}]（${task}），但 SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1 已設 — 放行。" >&2
    else
        echo "ERROR: kubectl context '${ctx}' 不是這次任務該用的叢集（${task}）。" >&2
        echo "  允許值：${allowlist}" >&2
        echo "  '${ctx}' 本身可用（存在於 kubeconfig），但用在這個任務上是錯的叢集。" >&2
        echo "  來源：${repo:-<repo>}/.sandbox.yml 的 allowed_kube_contexts[_<task>]。" >&2
        echo "  真要打別的叢集：改 .sandbox.yml，或 SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1 明示覆寫。" >&2
        return 1
    fi

    # 注意這裡**沒有** `kubectl config use-context`：overlay 本身就帶 current-context，
    # 切換已經完成。再呼叫一次不只多餘，還是唯一剩下的「可能寫到 ~/.kube/config」的
    # 路徑（overlay 若在那一瞬間缺席，它會靜默寫進鏈上的下一個檔並回 0）。
    wk_isolate_kubeconfig "$ctx" || return 1

    # 回讀斷言：唯一能分辨「隔離生效」與「靜默 fallback」的觀測。
    local seen
    seen="$(kubectl config current-context 2>/dev/null || true)"
    if [[ "$seen" != "$ctx" ]]; then
        echo "ERROR: kubeconfig 隔離失效：要求 '${ctx}'，回讀到 '${seen:-<空>}'。" >&2
        echo "  overlay: ${_WK_OVERLAY:-<未建立>}" >&2
        echo "  繼續下去整段會打到錯的叢集 —— 中止。" >&2
        return 1
    fi
    export KUBE_CONTEXT="$ctx"

    # context 名不等於叢集身分（同名在不同 kubeconfig 可指向不同叢集）。把實際的 API
    # server 印出來，讓「打錯叢集」在事後 log 裡看得見。
    local srv
    srv="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
    echo "Using kubectl context: ${ctx}${srv:+  (server: ${srv})}"
    return 0
}
