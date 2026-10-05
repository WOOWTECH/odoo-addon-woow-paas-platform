#!/usr/bin/env bash
# .sandbox hook 的 SANDBOX_NEUTRALIZE_LOCKDOWN gate（#1205；#1162 D7）— 不碰叢集。
#
# Run:  bash .sandbox/tests/test_lockdown_hooks.sh
#   選配：WOOWPRD_CHART_DIR=<cc/woowprd 的 charts/odoo-dev-sandbox> ⇒ 另以真 chart render NP，
#         驗 lockdown 後沒有 0.0.0.0/0／paas-managed／paas-* selector；沒給就印 SKIP（不是 PASS）。
#         ⚠ 需要含 cc/woowprd#5（networkPolicy.allowInternetEgress）的 chart。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/../pre-install.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
ok()  { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

# run_hook <out> [VAR=val…]：乾淨環境跑 hook（不繼承呼叫端的 JCR／AGENT_RUNTIME_*），回傳 hook 的 exit code
run_hook() {
  local out="$1"; shift
  : > "$out"
  env -i PATH="$PATH" HOME="$TMP" EXTRA_VALUES_FILE="$out" "$@" bash "$HOOK" >"$TMP/stdout" 2>"$TMP/stderr"
}

# 1. 未開 lockdown：stg 行為不變
run_hook "$TMP/stg.yaml" MODE=stg; rc=$?
[ "$rc" = 0 ] && ok "stg 未開 lockdown rc=0" || bad "stg 未開 lockdown rc=${rc}"
grep -q 'allowEgressToNamespaces: \[paas-stg-system\]' "$TMP/stg.yaml" && ok "stg 照舊放行 paas-stg-system" || bad "stg 行為被改到了"
grep -q 'allowInternetEgress' "$TMP/stg.yaml" && bad "未開 lockdown 卻寫了 allowInternetEgress" || ok "未開 lockdown 不寫 allowInternetEgress"

# 2. lockdown：NP 鍵
run_hook "$TMP/ld.yaml" MODE=stg SANDBOX_NEUTRALIZE_LOCKDOWN=1; rc=$?
[ "$rc" = 0 ] && ok "lockdown rc=0" || { bad "lockdown rc=${rc}"; cat "$TMP/stderr"; }
for want in 'allowInternetEgress: false' 'allowAgentRuntimePods: false' 'allowEgressToNamespaces: \[\]' 'allowIngressFromNamespaces: \[\]'; do
  grep -q "  ${want}" "$TMP/ld.yaml" && ok "lockdown 寫 ${want}" || bad "lockdown 缺 ${want}"
done
grep -q 'paas-stg' "$TMP/ld.yaml" && bad "lockdown 仍有 paas-stg-* namespace" || ok "lockdown 無 paas-stg-* namespace"
python3 - "$TMP/ld.yaml" <<'PY' && ok "lockdown values 是合法 YAML、networkPolicy 只出現一次" || bad "lockdown values YAML 不合法或重複鍵"
import sys, yaml
text = open(sys.argv[1]).read()
assert text.count("\nnetworkPolicy:") + text.startswith("networkPolicy:") == 1
yaml.safe_load(text)
PY

# 3. lockdown fail-loud 的三種情況
run_hook "$TMP/x.yaml" MODE=stg SANDBOX_NEUTRALIZE_LOCKDOWN=1 AGENT_RUNTIME_NAMESPACE_SECRET=x; rc=$?
[ "$rc" != 0 ] && ok "lockdown＋AGENT_RUNTIME_NAMESPACE_SECRET 拒絕" || bad "lockdown＋AGENT_RUNTIME_NAMESPACE_SECRET 應拒絕"
for m in local ci-local; do
  run_hook "$TMP/x.yaml" MODE="$m" SANDBOX_NEUTRALIZE_LOCKDOWN=1; rc=$?
  [ "$rc" != 0 ] && ok "lockdown＋MODE=${m} 拒絕" || bad "lockdown＋MODE=${m} 應拒絕"
done
for v in true yes on 2; do
  run_hook "$TMP/x.yaml" MODE=stg SANDBOX_NEUTRALIZE_LOCKDOWN="$v"; rc=$?
  [ "$rc" != 0 ] && ok "SANDBOX_NEUTRALIZE_LOCKDOWN=${v} fail-loud" || bad "SANDBOX_NEUTRALIZE_LOCKDOWN=${v} 應 fail-loud"
done
run_hook "$TMP/x.yaml" MODE=stg SANDBOX_NEUTRALIZE_LOCKDOWN=0; rc=$?
[ "$rc" = 0 ] && ok "SANDBOX_NEUTRALIZE_LOCKDOWN=0 視同未開" || bad "SANDBOX_NEUTRALIZE_LOCKDOWN=0 應通過"

# 4. post-create 的值域與 hook 一致（不合法值 fail-loud；lockdown 寫 sentinel、不探索 operator）
grep -q 'OPERATOR_URL="http://127.0.0.1:9"' "$HERE/../post-create.sh" && ok "post-create lockdown 寫 operator 黑洞 sentinel" \
  || bad "post-create 沒處理 lockdown"
env -i PATH="$PATH" NAMESPACE=n DB_NAME=d PG_STATEFULSET=p SANDBOX_NEUTRALIZE_LOCKDOWN=yes \
  bash "$HERE/../post-create.sh" >/dev/null 2>&1 && bad "post-create 對不合法值應 fail-loud" || ok "post-create 對不合法值 fail-loud"

# 5. 選配：真 chart render
if [ -n "${WOOWPRD_CHART_DIR:-}" ] && command -v helm >/dev/null 2>&1; then
  np="$(helm template t "$WOOWPRD_CHART_DIR" -s templates/network-policy.yaml -f "$TMP/stg.yaml" -f "$TMP/ld.yaml" 2>&1)" \
    || bad "helm template 失敗：${np}"
  for bad_pat in '0.0.0.0/0' 'paas-managed' 'paas-stg-'; do
    grep -q -- "$bad_pat" <<<"$np" && bad "lockdown render 仍有 ${bad_pat}" || ok "lockdown render 無 ${bad_pat}"
  done
  grep -q -- '-default-deny' <<<"$np" && ok "lockdown render default-deny 仍在" || bad "default-deny 不見了"
else
  echo "SKIP: 沒設 WOOWPRD_CHART_DIR（或沒有 helm），真 chart render 未驗"
fi

echo "----------------------------------------"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "${fails} FAILED"; exit 1; fi
