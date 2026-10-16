#!/usr/bin/env bash
# .sandbox hook 的 WOOW_LLM_SANDBOX opt-in（LLM-01b；DESIGN §2.11、〔REV〕18、46、47）— 不碰叢集。
#
# Run:  bash .sandbox/tests/test_llm_sandbox_hooks.sh
#   PATH 前面放一支假 kubectl：只回答 paas-stg-system/paas-stg-newapi-proxy-client 的 web／cron
#   （值是測試自己產生的假金鑰），其他呼叫一律失敗——真 kubectl 一次都不會被叫到。
#   選配：WOOWPRD_CHART_DIR=<含 SBX-01 的 charts/odoo-dev-sandbox> ⇒ 另以真 chart render，
#         驗 Secret 多三個 key、Odoo container 多三條 optional secretKeyRef、NP egress 有 new-api-stg。
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/../pre-install.sh"
POST="$HERE/../post-create.sh"
IDENTITY="$HERE/../llm-identity.sh"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"
fails=0
ok()  { echo "ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

WEB_KEY="web1:$(head -c 30 /dev/urandom | base64 | tr '+/=' '-_x' | tr -d '\n')"
CRON_KEY="cron1:$(head -c 30 /dev/urandom | base64 | tr '+/=' '-_x' | tr -d '\n')"

# 假 kubectl：FAKE_KUBECTL_MODE=ok｜missing｜short
mkdir -p "$TMP/bin"
cat > "$TMP/bin/kubectl" <<'SH'
#!/usr/bin/env bash
args="$*"
case "${FAKE_KUBECTL_MODE:-ok}" in
  missing) exit 1 ;;
esac
if [[ "$args" == *"get secret -n paas-stg-system paas-stg-newapi-proxy-client"* ]]; then
  if [ "${FAKE_KUBECTL_MODE:-ok}" = short ]; then
    printf 'k:short\n' | base64; exit 0
  fi
  case "$args" in
    *"{.data.web}"*)  printf '%s\n' "$FAKE_WEB"  | base64 | tr -d '\n'; exit 0 ;;
    *"{.data.cron}"*) printf '%s\n' "$FAKE_CRON" | base64 | tr -d '\n'; exit 0 ;;
  esac
fi
echo "fake kubectl: unexpected call" >&2
exit 1
SH
chmod +x "$TMP/bin/kubectl"

# run_hook <out> [VAR=val…]：乾淨環境跑 pre-install，PATH 先找假 kubectl
run_hook() {
  local out="$1"; shift
  : > "$out"
  env -i PATH="$TMP/bin:$PATH" HOME="$TMP" EXTRA_VALUES_FILE="$out" \
    FAKE_WEB="$WEB_KEY" FAKE_CRON="$CRON_KEY" "$@" bash "$HOOK" >"$TMP/stdout" 2>"$TMP/stderr"
}

no_secret_in_output() {
  for secret in "$WEB_KEY" "$CRON_KEY"; do
    if grep -qF -- "${secret#*:}" "$TMP/stdout" "$TMP/stderr"; then
      bad "$1：金鑰值出現在 hook 的 stdout／stderr"; return
    fi
  done
  ok "$1：stdout／stderr 沒有金鑰值"
}

# 1. 未 opt-in：與改動前相同（新的 key 一個都沒有）
run_hook "$TMP/plain.yaml" MODE=stg; rc=$?
[ "$rc" = 0 ] && ok "未 opt-in rc=0" || bad "未 opt-in rc=${rc}"
grep -q 'allowEgressToNamespaces: \[paas-stg-system\]$' "$TMP/plain.yaml" && ok "未 opt-in egress 不變" || bad "未 opt-in egress 被改了"
grep -q 'extraSecretEnv\|new-api-stg\|WOOW_' "$TMP/plain.yaml" && bad "未 opt-in 卻出現 LLM values" || ok "未 opt-in 沒有 LLM values"

# 2. opt-in、讀得到金鑰
run_hook "$TMP/llm.yaml" MODE=stg WOOW_LLM_SANDBOX=1; rc=$?
[ "$rc" = 0 ] && ok "opt-in rc=0" || { bad "opt-in rc=${rc}"; cat "$TMP/stderr"; }
no_secret_in_output "opt-in"
python3 - "$TMP/llm.yaml" "$WEB_KEY" "$CRON_KEY" <<'PY' && ok "opt-in values：egress 加 new-api-stg、extraSecretEnv 三個 key、每把格式對" || bad "opt-in values 不對"
import re, sys, yaml
text = open(sys.argv[1]).read()
assert text.count("\nnetworkPolicy:") + text.startswith("networkPolicy:") == 1
assert text.count("\nodoo:") + text.startswith("odoo:") == 1
data = yaml.safe_load(text)
assert data["networkPolicy"]["allowEgressToNamespaces"] == ["paas-stg-system", "new-api-stg"]
env = data["odoo"]["extraSecretEnv"]
assert set(env) == {"WOOW_NEWAPI_PROXY_AUTH_WEB", "WOOW_NEWAPI_PROXY_AUTH_CRON", "WOOW_LLM_SECRET_KEYS"}
assert env["WOOW_NEWAPI_PROXY_AUTH_WEB"] == sys.argv[2], "web 尾端的換行要 strip 掉"
assert env["WOOW_NEWAPI_PROXY_AUTH_CRON"] == sys.argv[3]
assert re.fullmatch(r"[A-Za-z0-9_-]{43}=", env["WOOW_LLM_SECRET_KEYS"])
assert data["odoo"]["devMode"] is False
PY
run_hook "$TMP/llm2.yaml" MODE=stg WOOW_LLM_SANDBOX=1
k1="$(grep WOOW_LLM_SECRET_KEYS "$TMP/llm.yaml")"; k2="$(grep WOOW_LLM_SECRET_KEYS "$TMP/llm2.yaml")"
[ -n "$k1" ] && [ "$k1" != "$k2" ] && ok "每個沙盒的加密金鑰都是新產生的" || bad "加密金鑰重複或缺"

# 3. opt-in、讀不到或格式不合 ⇒ 整段不寫（fail closed），沙盒照建
for mode in missing short; do
  run_hook "$TMP/nokey.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_KUBECTL_MODE="$mode"; rc=$?
  [ "$rc" = 0 ] && ok "金鑰 ${mode}：rc=0（不擋建沙盒）" || bad "金鑰 ${mode}：rc=${rc}"
  grep -q 'extraSecretEnv\|new-api-stg' "$TMP/nokey.yaml" && bad "金鑰 ${mode}：仍寫了 LLM values" || ok "金鑰 ${mode}：LLM 段整段不寫"
  grep -q 'WARN pre-install' "$TMP/stderr" && ok "金鑰 ${mode}：有 WARN" || bad "金鑰 ${mode}：沒有 WARN"
done

# 4. 值域與 lockdown
for v in yes true 2 on; do
  run_hook "$TMP/x.yaml" MODE=stg WOOW_LLM_SANDBOX="$v"; rc=$?
  [ "$rc" != 0 ] && ok "WOOW_LLM_SANDBOX=${v} fail-loud" || bad "WOOW_LLM_SANDBOX=${v} 應 fail-loud"
done
for var in WOOW_LLM_SANDBOX=1 WOOW_LLM_SANDBOX=0 WOOW_LLM_SECRET_KEYS=x WOOW_NEWAPI_PROXY_AUTH_WEB=x WOOW_NEWAPI_PROXY_AUTH_CRON=x; do
  run_hook "$TMP/x.yaml" MODE=stg SANDBOX_NEUTRALIZE_LOCKDOWN=1 "$var"; rc=$?
  [ "$rc" != 0 ] && ok "lockdown＋${var%%=*}=… fail-loud" || bad "lockdown＋${var} 應 fail-loud"
done
run_hook "$TMP/x.yaml" MODE=stg SANDBOX_NEUTRALIZE_LOCKDOWN=1; rc=$?
[ "$rc" = 0 ] && ok "lockdown 沒有 LLM env 照常通過" || bad "lockdown 沒有 LLM env rc=${rc}"
run_hook "$TMP/local.yaml" MODE=local WOOW_LLM_SANDBOX=1; rc=$?
[ "$rc" = 0 ] && [ ! -s "$TMP/local.yaml" ] && ok "local mode：略過、不寫 values" || bad "local mode rc=${rc} 或寫了 values"

# 5. 沙盒身分：同 slug 同結果、格式合代理 regex、block 1–31
a1="$(bash "$IDENTITY" aimodels)"; a2="$(bash "$IDENTITY" aimodels)"; b="$(bash "$IDENTITY" another-slug)"
[ "$a1" = "$a2" ] && ok "llm-identity 對同一個 slug 穩定（${a1}）" || bad "llm-identity 不穩定"
[ "$a1" != "$b" ] && ok "不同 slug 得到不同身分" || bad "不同 slug 撞了（機率 1/20956，換 slug 再跑）"
bad_shape=0
for slug in a b c aimodels test1 llm-01c x-y-z 1234; do
  read -r prefix block < <(bash "$IDENTITY" "$slug")
  [[ "$prefix" =~ ^ws[a-z]{2}$ ]] && [ "$block" -ge 1 ] && [ "$block" -le 31 ] || bad_shape=1
  [[ "${prefix}0000000001" =~ ^ws[a-z]{2}[0-9]{10}$ ]] || bad_shape=1
done
[ "$bad_shape" = 0 ] && ok "前綴＋10 位數合代理的 ^ws[a-z]{2}\\d{10}\$、block 在 1–31" || bad "身分格式不合"

# 6. post-create：值域在碰叢集之前就檢查
env -i PATH="$TMP/bin:$PATH" NAMESPACE=n DB_NAME=d PG_STATEFULSET=p WOOW_LLM_SANDBOX=yes \
  bash "$POST" >/dev/null 2>"$TMP/post.err"; rc=$?
[ "$rc" != 0 ] && ok "post-create 對不合法值 fail-loud" || bad "post-create 對不合法值應 fail-loud"
grep -q 'unexpected call' "$TMP/post.err" && bad "post-create 在檢查之前就叫了 kubectl" || ok "post-create 檢查在任何 kubectl 之前"

# 7. 選配：真 chart render（需含 SBX-01）
if [ -n "${WOOWPRD_CHART_DIR:-}" ] && command -v helm >/dev/null 2>&1; then
  helm template t "$WOOWPRD_CHART_DIR" -f "$TMP/llm.yaml" >"$TMP/render.yaml" 2>"$TMP/render.err" \
    || bad "helm template 失敗"
  python3 - "$TMP/render.yaml" <<'PY' && ok "chart render：Secret 三個 key、三條 optional secretKeyRef、NP egress new-api-stg" || bad "chart render 不對"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
secret_keys = set()
refs = []
egress_ns = set()
for d in docs:
    if d["kind"] == "Secret":
        secret_keys |= set((d.get("data") or {}).keys()) | set((d.get("stringData") or {}).keys())
    if d["kind"] == "Deployment" and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/component") == "odoo":
        for c in d["spec"]["template"]["spec"]["containers"]:
            for e in c.get("env", []):
                if e["name"].startswith(("WOOW_NEWAPI_PROXY_AUTH_", "WOOW_LLM_SECRET_KEYS")):
                    refs.append(e["valueFrom"]["secretKeyRef"].get("optional"))
    if d["kind"] == "NetworkPolicy":
        for rule in d["spec"].get("egress") or []:
            for peer in rule.get("to") or []:
                ns = (peer.get("namespaceSelector") or {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
                if ns:
                    egress_ns.add(ns)
assert len(refs) == 3 and all(r is True for r in refs), refs
assert len(secret_keys) >= 3
assert "new-api-stg" in egress_ns
PY
else
  echo "SKIP: 沒設 WOOWPRD_CHART_DIR（或沒有 helm），真 chart render 未驗"
fi

echo "----------------------------------------"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "${fails} FAILED"; exit 1; fi
