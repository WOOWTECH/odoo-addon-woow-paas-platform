#!/usr/bin/env bash
# prod-clone/lib.sh — neutralize-clone.sh／seed-from-prod.sh 共用（#1206）。由呼叫端 source；需要 bash 4+、python3 ≥3.11。
#
# 只作用在 platform mode（cc/woowprd#8）且 lockdown（cc/woowprd#5／#1205）的 dev-sandbox-<slug>：
# release＝slug，postgres＝<slug>-postgresql-0（容器 postgresql、本地 socket superuser postgres）。

PC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PC_TOOL="${PC_DIR}/prod_clone_tool.py"
PC_FIXTURE="${PC_DIR}/assertions.toml"
# 只給工具自身的合成資料實測用（例：合成 fixture 沒裝 website）；真彩排 ⛔ 不得覆寫，gate 會大聲標示
if [[ -n "${PROD_CLONE_FIXTURE_OVERRIDE:-}" ]]; then
    PC_FIXTURE="$PROD_CLONE_FIXTURE_OVERRIDE"
    echo -e "\033[1;33m⚠ PROD_CLONE_FIXTURE_OVERRIDE=${PC_FIXTURE}：斷言 fixture 被覆寫——只限工具實測，真彩排不得使用\033[0m" >&2
fi
PC_PROJECT_ROOT="$(cd "${PC_DIR}/../../../../.." && pwd)"
PC_LABEL_DOMAIN="dev-sandbox.io"
PC_NEUTRAL_EMAIL_FROM="noreply@sandbox.invalid"
PC_MARKER_NP="prod-clone-lockdown-marker"
PC_HOLE_NP="prod-clone-transfer-hole"
PC_RECEIVER="prod-clone-receiver"
PC_INCOMING_PVC="prod-clone-incoming"
PC_PROBE_HOST="api.line.me"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
pc_info() { echo -e "${BLUE}$*${NC}"; }
pc_ok()   { echo -e "${GREEN}✓ $*${NC}"; }
pc_warn() { echo -e "${YELLOW}⚠ $*${NC}" >&2; }
pc_die()  { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }

# pc_init <slug> <context> — context 守衛（#32）＋第 0 條目標防呆（#1162 D8）。設定 NAMESPACE／RELEASE／PG_POD。
pc_init() {
    local slug="$1" ctx="$2"
    [[ -n "$slug" ]] || pc_die "slug 必填（不從 branch 推導；#1162 D8）"
    [[ -n "$ctx" ]] || pc_die "--context 必填"
    [[ "$slug" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || pc_die "slug '${slug}' 不合法"
    command -v python3 >/dev/null || pc_die "需要 python3"
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || pc_die "需要 python3 ≥ 3.11（tomllib）"
    # shellcheck source=../../../lib/kube-context.sh
    source "${PC_PROJECT_ROOT}/.claude/skills/lib/kube-context.sh"
    wk_require_context "$ctx" prod-clone "$PC_PROJECT_ROOT" || exit 2
    NAMESPACE="dev-sandbox-${slug}"
    RELEASE="$slug"
    PG_POD="${RELEASE}-postgresql-0"
    PC_TARGET_GUARD="$(pc_target_guard)"
    [[ "$PC_TARGET_GUARD" == ok ]] || pc_die "第 0 條防呆不通過：${PC_TARGET_GUARD}"
    pc_ok "目標 ${NAMESPACE}（release ${RELEASE}）通過第 0 條防呆"
}

# pc_target_guard — 印 ok 或拒絕理由（同時給 K00 斷言用）
pc_target_guard() {
    local ns="$NAMESPACE" name mode lockdown
    [[ "$ns" =~ ^dev-sandbox-[a-z0-9-]+$ ]] || { echo "namespace ${ns} 不是 dev-sandbox-*"; return; }
    [[ "$ns" != dev-sandbox-stg ]] || { echo "dev-sandbox-stg 是共用 stg 沙盒，禁止"; return; }
    [[ "$ns" != paas-* && "$PG_POD" != paas-odoo-postgresql-0 ]] || { echo "目標是 paas-*／prod postgres，拒絕"; return; }
    name="$(kubectl get namespace "$ns" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/name}' 2>/dev/null)" \
        || { echo "namespace ${ns} 不存在"; return; }
    [[ "$name" == odoo-dev-sandbox ]] || { echo "namespace label app.kubernetes.io/name='${name}'（要 odoo-dev-sandbox）"; return; }
    mode="$(kubectl get namespace "$ns" -o jsonpath="{.metadata.annotations.dev-sandbox\.io/mode}")"
    [[ "$mode" == platform ]] || { echo "不是 platform 沙盒（mode='${mode}'）"; return; }
    lockdown="$(kubectl get namespace "$ns" -o jsonpath="{.metadata.annotations.dev-sandbox\.io/neutralize-lockdown}")"
    [[ "$lockdown" == 1 ]] || { echo "不是 lockdown 沙盒（建沙盒前 export SANDBOX_NEUTRALIZE_LOCKDOWN=1）"; return; }
    kubectl get pod -n "$ns" "$PG_POD" >/dev/null 2>&1 || { echo "找不到 ${PG_POD}"; return; }
    echo ok
}

# pc_psql <db> [psql args…] — 在沙盒 postgres pod 內以 superuser（本地 socket）跑 psql；SQL 從 stdin
pc_psql() {
    local db="$1"; shift
    kubectl exec -i -n "$NAMESPACE" "$PG_POD" -c postgresql -- \
        psql -U postgres -d "$db" -X -q -v ON_ERROR_STOP=1 "$@"
}

pc_db_exists() {
    local out
    out="$(pc_psql postgres -tA -v db="$1" <<<"SELECT count(*) FROM pg_database WHERE datname = :'db';")" || return 2
    [[ "$out" == 1 ]]
}

# pc_component_state <component> — 印「<spec.replicas> <running pods>」
pc_component_state() {
    local comp="$1" rep pods
    rep="$(kubectl get deploy -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=${comp}" \
        -o jsonpath='{.items[0].spec.replicas}' 2>/dev/null)"
    pods="$(kubectl get pod -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=${comp}" \
        --field-selector=status.phase!=Succeeded,status.phase!=Failed -o name 2>/dev/null | grep -c . || true)"
    echo "${rep:-?} ${pods}"
}

# pc_require_odoo_down — restore／搬運洞存在期間 web 與 cron 都必須是 0（含已在跑的 pod）
pc_require_odoo_down() {
    local c st
    for c in server cron; do
        st="$(pc_component_state "$c")"
        [[ "$st" == "0 0" ]] || pc_die "component=${c} 不是 0 副本（replicas／pods＝${st}）—— 先 scale ${RELEASE} --component ${c} --replicas 0"
    done
}

# ───── builtin 檢查（斷言 fixture 的 kind="builtin"）─────

pc_check_np_content() {
    kubectl get networkpolicy -n "$NAMESPACE" -o json | python3 "$PC_TOOL" np-check > "$PC_WORK/np-check.txt" || true
    sed 's/^/    /' "$PC_WORK/np-check.txt" >&2
    grep -q '^NP-LOCKDOWN-CHECK PASS$' "$PC_WORK/np-check.txt" && echo PASS || echo FAIL
}

# 正向標記 NP：腳本自己擁有的 default-deny（ingress＋egress）。外殼 release 的 NP 被 helm 改掉或刪掉時，
# 它仍在 ⇒ 沙盒維持全擋（NP 是聯集，只會更嚴不會放寬）；存在與內容也當成「腳本確實鎖過」的證據。
pc_apply_marker_np() {
    kubectl apply -n "$NAMESPACE" -f - >/dev/null <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${PC_MARKER_NP}
  labels:
    app.kubernetes.io/managed-by: prod-clone
  annotations:
    ${PC_LABEL_DOMAIN}/purpose: "prod clone lockdown marker (#1162 D7, #1206)"
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
  ingress: []
  egress: []
EOF
}

pc_check_np_marker() {
    local json
    json="$(kubectl get networkpolicy -n "$NAMESPACE" "$PC_MARKER_NP" -o json 2>/dev/null)" || { echo absent; return; }
    python3 -c '
import json, sys
s = json.load(sys.stdin)["spec"]
ok = s.get("podSelector") in ({}, None) and sorted(s.get("policyTypes", [])) == ["Egress", "Ingress"] \
     and not s.get("ingress") and not s.get("egress")
print("present" if ok else "modified")' <<<"$json"
}

pc_check_transfer_hole() {
    if kubectl get networkpolicy -n "$NAMESPACE" "$PC_HOLE_NP" >/dev/null 2>&1 \
        || kubectl get pod -n "$NAMESPACE" "$PC_RECEIVER" >/dev/null 2>&1; then
        echo present
    else
        echo absent
    fi
}

pc_check_incoming_clean() {
    kubectl get pvc -n "$NAMESPACE" "$PC_INCOMING_PVC" >/dev/null 2>&1 && echo present || echo clean
}

pc_pg_image() {
    kubectl get pod -n "$NAMESPACE" "$PG_POD" -o jsonpath='{.spec.containers[?(@.name=="postgresql")].image}'
}

pc_pull_secret_yaml() {
    local s
    s="$(kubectl get pod -n "$NAMESPACE" "$PG_POD" -o jsonpath='{.spec.imagePullSecrets[*].name}')"
    [[ -n "$s" ]] && printf '  imagePullSecrets: [{name: %s}]\n' "$s"
}

# 行為探針：同 namespace 起一顆 pod，等 NP 生效窗口（kube-router 對新 pod 約 1.3 s，woowprd#13）後
# 連 api.line.me:443 必須失敗；同時連同 ns postgres 5432 當正向對照（對照失敗＝探針本身不可信）。
pc_check_np_probe() {
    local pod="prod-clone-np-probe" out
    kubectl delete pod -n "$NAMESPACE" "$pod" --ignore-not-found --wait=true >/dev/null 2>&1
    kubectl apply -n "$NAMESPACE" -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  labels: {app.kubernetes.io/instance: ${RELEASE}, app.kubernetes.io/component: prod-clone-np-probe}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
$(pc_pull_secret_yaml)
  containers:
    - name: probe
      image: $(pc_pg_image)
      command: ["/bin/bash", "-c"]
      args:
        - |
          sleep 10
          if timeout 5 bash -c '</dev/tcp/${PC_PROBE_HOST}/443' 2>/dev/null; then echo "PROBE external=open"; else echo "PROBE external=blocked"; fi
          if timeout 5 bash -c '</dev/tcp/${RELEASE}-postgresql/5432' 2>/dev/null; then echo "PROBE control=ok"; else echo "PROBE control=fail"; fi
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {cpu: 200m, memory: 128Mi}
EOF
    if ! kubectl wait -n "$NAMESPACE" --for=jsonpath='{.status.phase}'=Succeeded "pod/${pod}" --timeout=120s >/dev/null 2>&1; then
        kubectl delete pod -n "$NAMESPACE" "$pod" --ignore-not-found --wait=false >/dev/null 2>&1
        echo "probe-error"; return
    fi
    out="$(kubectl logs -n "$NAMESPACE" "$pod" 2>/dev/null)"
    kubectl delete pod -n "$NAMESPACE" "$pod" --ignore-not-found --wait=false >/dev/null 2>&1
    if grep -q 'control=ok' <<<"$out" && grep -q 'external=blocked' <<<"$out"; then echo blocked
    elif grep -q 'external=open' <<<"$out"; then echo open
    else echo "inconclusive"; fi
}

# conf email_from：conf 在 Secret 內（含密碼），只抽 email_from 一行，不印其他內容
pc_check_conf_email_from() {
    kubectl get secret -n "$NAMESPACE" "${RELEASE}-odoo-conf" -o jsonpath='{.data.odoo\.conf}' 2>/dev/null \
        | base64 -d 2>/dev/null | sed -n 's/^[[:space:]]*email_from[[:space:]]*=[[:space:]]*//p' | head -1 | tr -d '\r'
}

pc_login_secret() { echo "${RELEASE}-prod-clone-login"; }

pc_check_passwords_reset() {
    local db="$1" hash
    hash="$(kubectl get secret -n "$NAMESPACE" "$(pc_login_secret)" -o jsonpath='{.data.password-hash}' 2>/dev/null | base64 -d 2>/dev/null)"
    [[ -n "$hash" ]] || { echo "no-secret"; return; }
    pc_psql "$db" -tA -v h="$hash" <<<"SELECT count(*) FROM res_users WHERE id <> 1 AND password IS NOT NULL AND password <> :'h';" 2>/dev/null \
        || echo "sql-error"
}

# 掃 pg_database：每個非 template、非 postgres 的 DB 都必須 database.is_neutralized=true；印未中和的個數
pc_check_all_dbs() {
    local n=0 db v
    while read -r db; do
        [[ -n "$db" ]] || continue
        v="$(pc_psql "$db" -tA <<<"SELECT CASE WHEN to_regclass('public.ir_config_parameter') IS NULL THEN 'no-odoo' ELSE coalesce((SELECT lower(value) FROM ir_config_parameter WHERE key = 'database.is_neutralized'), 'missing') END;" 2>/dev/null)"
        if [[ "$v" != true ]]; then
            echo "    DB ${db}: database.is_neutralized=${v:-error}" >&2
            n=$((n + 1))
        fi
    done < <(pc_psql postgres -tA <<<"SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1;")
    echo "$n"
}

# pc_gate <db> [--no-probe] — 斷言 fixture 全跑（SQL＋builtin），印斷言表；有 FAIL 回非 0。
pc_gate() {
    local db="$1" probe=1 rc
    [[ "${2:-}" == --no-probe ]] && probe=0
    pc_info "斷言 gate（fixture：${PC_FIXTURE#"$PC_PROJECT_ROOT"/}）..."
    python3 "$PC_TOOL" assert-sql --fixture "$PC_FIXTURE" > "$PC_WORK/assert.sql" || pc_die "fixture 解析失敗"
    pc_psql "$db" -v ON_ERROR_STOP=0 < "$PC_WORK/assert.sql" > "$PC_WORK/assert.out" 2> "$PC_WORK/assert.err" || true
    if [[ -s "$PC_WORK/assert.err" ]]; then
        pc_warn "斷言 SQL 有錯誤（對應的 check 會判 FAIL）："; sed 's/^/    /' "$PC_WORK/assert.err" >&2
    fi
    local b=()
    b+=(--builtin "target_guard=$(pc_target_guard)")
    b+=(--builtin "np_content=$(pc_check_np_content)")
    b+=(--builtin "np_marker=$(pc_check_np_marker)")
    if [[ "$probe" == 1 ]]; then b+=(--builtin "np_probe=$(pc_check_np_probe)"); fi
    b+=(--builtin "transfer_hole=$(pc_check_transfer_hole)")
    b+=(--builtin "incoming_clean=$(pc_check_incoming_clean)")
    b+=(--builtin "all_dbs_neutralized=$(pc_check_all_dbs)")
    b+=(--builtin "conf_email_from=$(pc_check_conf_email_from)")
    b+=(--builtin "passwords_reset=$(pc_check_passwords_reset "$db")")
    local req=(--require-builtins)
    [[ "$probe" == 1 ]] || req=()
    python3 "$PC_TOOL" assert-eval --fixture "$PC_FIXTURE" --results "$PC_WORK/assert.out" "${b[@]}" "${req[@]}"
    rc=$?
    if [[ $rc -eq 0 ]]; then pc_ok "斷言 gate 全綠"; else echo -e "${RED}✗ 斷言 gate 未通過${NC}" >&2; fi
    return $rc
}
