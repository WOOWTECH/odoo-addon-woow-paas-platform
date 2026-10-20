#!/usr/bin/env bash
# .sandbox/pre-install.sh：allowAgentRuntimePods 只在 agent-runtime 真的啟用時才開，且限 env=stg
# （#1202；商用驗收 sandbox-allow-internal-netpol）— 不碰叢集。
#
# 舊行為：stg mode 無條件寫 allowAgentRuntimePods: true ⇒ plugin chart 的 allow-internal 對
# 所有 paas-managed=true 的 ns（含 45 個 prod 租戶 ns）雙向放行。agent-runtime 本身由
# AGENT_RUNTIME_NAMESPACE_SECRET＋operator secret 決定（fail-closed），網路放行要跟它同一個條件。
#
# Run:  bash .sandbox/tests/test_agent_runtime_netpol_gate.sh
#   選配：WOOWPRD_CHART_DIR=<cc/woowprd 的 plugins/woow-dev/skills/k8s-dev-sandbox/charts/odoo-dev-sandbox>
#         ⇒ 另以真 chart render NP；需要含 networkPolicy.agentRuntimePodsEnv 的 chart（cc/woowprd #1202 PR）。
#         沒給就印 SKIP（不是 PASS）。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/../pre-install.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
ok()  { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

# 假 kubectl：永遠失敗（測試絕不可碰真叢集；也模擬 operator secret 讀不到）
mkdir -p "$TMP/bin"
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/kubectl"; chmod +x "$TMP/bin/kubectl"

run_hook() {
  local out="$1"; shift
  : > "$out"
  env -i PATH="$TMP/bin:$PATH" HOME="$TMP" EXTRA_VALUES_FILE="$out" "$@" bash "$HOOK" >"$TMP/stdout" 2>"$TMP/stderr"
}

np_check() {  # np_check <values.yaml> <want_allow:true|false> <want_env:''|stg> <want_agentRuntime:0|1>
  python3 - "$@" <<'PY'
import sys, yaml
path, want_allow, want_env, want_ar = sys.argv[1], sys.argv[2] == "true", sys.argv[3], sys.argv[4] == "1"
text = open(path).read()
assert text.count("\nnetworkPolicy:") + text.startswith("networkPolicy:") == 1, "networkPolicy 不只一次"
v = yaml.safe_load(text)
np = v["networkPolicy"]
errs = []
if np.get("allowAgentRuntimePods") is not want_allow:
    errs.append(f"allowAgentRuntimePods={np.get('allowAgentRuntimePods')!r}，應為 {want_allow}")
if (np.get("agentRuntimePodsEnv") or "") != want_env:
    errs.append(f"agentRuntimePodsEnv={np.get('agentRuntimePodsEnv')!r}，應為 {want_env!r}")
if ("agentRuntime" in v) != want_ar:
    errs.append(f"agentRuntime 段存在={'agentRuntime' in v}，應為 {want_ar}")
if errs:
    print("; ".join(errs)); sys.exit(1)
PY
}

# 1. 一般 stg 沙盒（沒開 agent-runtime）⇒ 不放行 paas-managed
run_hook "$TMP/plain.yaml" MODE=stg; rc=$?
[ "$rc" = 0 ] && ok "一般 stg rc=0" || bad "一般 stg rc=${rc}"
msg="$(np_check "$TMP/plain.yaml" false '' 0)" && ok "一般 stg ⇒ allowAgentRuntimePods: false、無 env、無 agentRuntime 段" \
  || bad "一般 stg：${msg}"

# 2. agent-runtime 啟用（三個 secret 都有）⇒ 放行，但限 env=stg
run_hook "$TMP/ar.yaml" MODE=stg AGENT_RUNTIME_NAMESPACE_SECRET=ns-x \
  AGENT_RUNTIME_AUTH_SECRET_CURRENT=auth-x AGENT_RUNTIME_WEBHOOK_SECRET=wh-x; rc=$?
[ "$rc" = 0 ] && ok "agent-runtime stg rc=0" || { bad "agent-runtime stg rc=${rc}"; cat "$TMP/stderr"; }
msg="$(np_check "$TMP/ar.yaml" true stg 1)" && ok "agent-runtime ⇒ allowAgentRuntimePods: true＋agentRuntimePodsEnv: stg＋agentRuntime 段" \
  || bad "agent-runtime：${msg}"

# 3. 有 NAMESPACE secret 但 operator secret 讀不到（fail-closed）⇒ 網路也不放行
run_hook "$TMP/half.yaml" MODE=stg AGENT_RUNTIME_NAMESPACE_SECRET=ns-x; rc=$?
[ "$rc" = 0 ] && ok "agent-runtime 半套 rc=0（照舊 WARN 不失敗）" || bad "agent-runtime 半套 rc=${rc}"
msg="$(np_check "$TMP/half.yaml" false '' 0)" && ok "agent-runtime 半套 ⇒ agentRuntime 段不寫、網路也不放行" \
  || bad "agent-runtime 半套：${msg}"
grep -q 'agent-runtime 維持關閉' "$TMP/stderr" && ok "半套仍印 fail-closed WARN" || bad "半套沒印 WARN"

# 4. 只有 auth／webhook、沒有 NAMESPACE secret ⇒ 不啟用
run_hook "$TMP/nons.yaml" MODE=stg AGENT_RUNTIME_AUTH_SECRET_CURRENT=a AGENT_RUNTIME_WEBHOOK_SECRET=w; rc=$?
msg="$(np_check "$TMP/nons.yaml" false '' 0)" && ok "沒有 NAMESPACE secret ⇒ 不啟用、不放行" || bad "沒有 NAMESPACE secret：${msg}"

# 5. local／ci-local 照舊 no-op
for m in local ci-local; do
  run_hook "$TMP/m.yaml" MODE="$m" AGENT_RUNTIME_NAMESPACE_SECRET=ns-x AGENT_RUNTIME_AUTH_SECRET_CURRENT=a AGENT_RUNTIME_WEBHOOK_SECRET=w; rc=$?
  [ "$rc" = 0 ] && [ ! -s "$TMP/m.yaml" ] && ok "MODE=${m} 照舊 no-op" || bad "MODE=${m} 應 no-op（rc=${rc}）"
done

# 6. 選配：真 chart render
if [ -n "${WOOWPRD_CHART_DIR:-}" ] && command -v helm >/dev/null 2>&1; then
  np="$(helm template t "$WOOWPRD_CHART_DIR" -s templates/network-policy.yaml -f "$TMP/plain.yaml" 2>&1)" \
    || bad "helm template（一般 stg）失敗：${np}"
  grep -q 'paas-managed' <<<"$np" && bad "一般 stg render 仍有 paas-managed" || ok "一般 stg render 無 paas-managed"
  np="$(helm template t "$WOOWPRD_CHART_DIR" -s templates/network-policy.yaml -f "$TMP/ar.yaml" 2>&1)" \
    || bad "helm template（agent-runtime）失敗：${np}"
  printf '%s\n' "$np" > "$TMP/np.yaml"
  python3 - "$TMP/np.yaml" <<'PY' && ok "agent-runtime render：每條 paas-managed selector 都 AND env=stg" || bad "agent-runtime render 有未收窄的 paas-managed selector"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
n = 0
for d in docs:
    for key, peer_key in (("ingress", "from"), ("egress", "to")):
        for r in d["spec"].get(key) or []:
            for p in r.get(peer_key) or []:
                m = (p.get("namespaceSelector") or {}).get("matchLabels") or {}
                if "paas-managed" in m:
                    n += 1
                    assert m.get("paas.woowtech.io/env") == "stg", m
assert n == 2, n
PY
else
  echo "SKIP: 沒設 WOOWPRD_CHART_DIR（或沒有 helm），真 chart render 未驗"
fi

echo "----------------------------------------"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "${fails} FAILED"; exit 1; fi
