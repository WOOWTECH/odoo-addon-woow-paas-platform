#!/usr/bin/env bash
# .sandbox hook 的 WOOW_LLM_SANDBOX opt-in（LLM-01b；DESIGN §2.11、〔REV〕18、46、47）— 不碰叢集。
#
# Run:  bash .sandbox/tests/test_llm_sandbox_hooks.sh
#   PATH 前面放一支假 kubectl：只回答 paas-stg-system/paas-stg-newapi-proxy-client 的 web／cron
#   （值是測試自己產生的假金鑰）、沙盒 chart Secret 的 extra-env-woow-llm-secret-keys（模擬
#   upgrade 路徑的現值）、post-create 用到的幾個呼叫（prefix 登記 ConfigMap、Odoo deployment 的 env
#   名、pod 內探測、psql）——其他呼叫一律失敗，真 kubectl 一次都不會被叫到。
#   選配：WOOWPRD_CHART_DIR=<含 SBX-01 的 charts/odoo-dev-sandbox> ⇒ 另以真 chart render，
#         驗 Secret 多三個 key（含 hook 回頭讀的 extra-env-woow-llm-secret-keys）、Odoo container
#         多三條 optional secretKeyRef、NP egress 有 new-api-stg。
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
#   FAKE_EXISTING_LLM_KEYS=<值>   沙盒 chart Secret 已有的加密金鑰（模擬 helm upgrade 路徑）
#   FAKE_LOOKUP=ok｜error｜two     讀那個 Secret 的結果（error＝kubectl 失敗；two＝兩份 Secret）
mkdir -p "$TMP/bin"
cat > "$TMP/bin/kubectl" <<'SH'
#!/usr/bin/env bash
args="$*"
if [[ "$args" == *"get secret -n"*"-l app.kubernetes.io/instance="*"extra-env-woow-llm-secret-keys"* ]]; then
  case "${FAKE_LOOKUP:-ok}" in
    error) exit 1 ;;
    two) printf 'eA==\neQ==\n'; exit 0 ;;
  esac
  [ "${FAKE_KUBECTL_MODE:-ok}" = missing ] && exit 1
  if [ -n "${FAKE_EXISTING_LLM_KEYS:-}" ]; then
    printf '%s' "$FAKE_EXISTING_LLM_KEYS" | base64 | tr -d '\n'; printf '\n'
  else
    printf '\n'
  fi
  exit 0
fi
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
    NAMESPACE=dev-sandbox-t SLUG=t \
    FAKE_WEB="$WEB_KEY" FAKE_CRON="$CRON_KEY" "$@" bash "$HOOK" >"$TMP/stdout" 2>"$TMP/stderr"
}

# keys_in <values 檔>：extraSecretEnv 的 WOOW_LLM_SECRET_KEYS（沒有就空字串）
keys_in() {
  python3 - "$1" <<'PY'
import sys, yaml
data = yaml.safe_load(open(sys.argv[1]).read()) or {}
print(((data.get("odoo") or {}).get("extraSecretEnv") or {}).get("WOOW_LLM_SECRET_KEYS", ""))
PY
}

no_secret_in_output() {
  for secret in "$WEB_KEY" "$CRON_KEY" ${EXTRA_SECRETS:-}; do
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
k1="$(keys_in "$TMP/llm.yaml")"; k2="$(keys_in "$TMP/llm2.yaml")"
[ -n "$k1" ] && [ "$k1" != "$k2" ] && ok "每個新沙盒的加密金鑰都是新產生的" || bad "加密金鑰重複或缺"

# 2b. upgrade 路徑（finding 1）：同一個 namespace 再跑一次 ⇒ 沿用 chart Secret 的現值，不重產
run_hook "$TMP/up.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_EXISTING_LLM_KEYS="$k1"; rc=$?
k_up="$(keys_in "$TMP/up.yaml")"
[ "$rc" = 0 ] && [ "$k_up" = "$k1" ] && ok "upgrade 路徑：加密金鑰跨兩次執行穩定" || bad "upgrade 路徑重產了加密金鑰（rc=${rc}）"
EXTRA_SECRETS="$k1" no_secret_in_output "upgrade 路徑"
grep -q 'allowEgressToNamespaces: \[paas-stg-system, new-api-stg\]' "$TMP/up.yaml" \
  && ok "upgrade 路徑：egress 照樣放行 new-api-stg" || bad "upgrade 路徑掉了 new-api-stg egress"

# 2c. 已是 LLM 沙盒、重跑時沒帶 WOOW_LLM_SANDBOX ⇒ sticky（沿用、WARN、rc=0）
run_hook "$TMP/sticky.yaml" MODE=stg FAKE_EXISTING_LLM_KEYS="$k1"; rc=$?
[ "$rc" = 0 ] && [ "$(keys_in "$TMP/sticky.yaml")" = "$k1" ] && grep -q 'new-api-stg' "$TMP/sticky.yaml" \
  && grep -q 'WARN pre-install' "$TMP/stderr" \
  && ok "沒帶 WOOW_LLM_SANDBOX 重跑既有 LLM 沙盒：沿用金鑰與 egress＋WARN" || bad "沒帶 opt-in 重跑會丟掉金鑰（rc=${rc}）"

# 2d. 明確 WOOW_LLM_SANDBOX=0 想關 ⇒ 拒絕（照樣沿用、rc≠0）
run_hook "$TMP/off.yaml" MODE=stg WOOW_LLM_SANDBOX=0 FAKE_EXISTING_LLM_KEYS="$k1"; rc=$?
[ "$rc" != 0 ] && [ "$(keys_in "$TMP/off.yaml")" = "$k1" ] && grep -q 'ERROR pre-install' "$TMP/stderr" \
  && ok "WOOW_LLM_SANDBOX=0 關不掉既有 LLM 沙盒（金鑰保留、rc≠0）" || bad "WOOW_LLM_SANDBOX=0 把金鑰拿掉了（rc=${rc}）"

# 2e. 輪替：新的一把放前面、舊的保留
run_hook "$TMP/rot.yaml" MODE=stg WOOW_LLM_SANDBOX=1 WOOW_LLM_ROTATE_SECRET_KEYS=1 FAKE_EXISTING_LLM_KEYS="$k1"; rc=$?
k_rot="$(keys_in "$TMP/rot.yaml")"
[ "$rc" = 0 ] && [[ "$k_rot" =~ ^[A-Za-z0-9_-]{43}=,${k1}$ ]] \
  && ok "輪替：前面加一把新的、舊的保留" || bad "輪替結果不對（rc=${rc}）"
run_hook "$TMP/rot2.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_EXISTING_LLM_KEYS="$k_rot"
[ "$(keys_in "$TMP/rot2.yaml")" = "$k_rot" ] && ok "輪替後的清單在下一次執行也穩定" || bad "輪替後的清單被改了"
run_hook "$TMP/x.yaml" MODE=stg WOOW_LLM_ROTATE_SECRET_KEYS=yes; rc=$?
[ "$rc" != 0 ] && ok "WOOW_LLM_ROTATE_SECRET_KEYS=yes fail-loud" || bad "WOOW_LLM_ROTATE_SECRET_KEYS=yes 應 fail-loud"

# 2f. 讀不到現值 ⇒ 不產生新的、整段不寫、rc≠0
for lookup in error two; do
  run_hook "$TMP/lk.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_LOOKUP="$lookup"; rc=$?
  [ "$rc" != 0 ] && ! grep -q 'extraSecretEnv\|new-api-stg' "$TMP/lk.yaml" \
    && grep -q 'allowEgressToNamespaces: \[paas-stg-system\]$' "$TMP/lk.yaml" \
    && ok "讀不到現值（${lookup}）：不產生新金鑰、LLM 段不寫、其餘 values 照寫、rc≠0" || bad "讀不到現值（${lookup}）時仍寫了 LLM values 或 rc=0"
done
run_hook "$TMP/lk.yaml" MODE=stg FAKE_LOOKUP=error; rc=$?
[ "$rc" = 0 ] && grep -q 'WARN pre-install: 無法確認' "$TMP/stderr" && ! grep -q 'extraSecretEnv' "$TMP/lk.yaml" \
  && ok "沒 opt-in 而讀不到現值：只 WARN、rc=0（一般沙盒不因 kubectl 一時失敗報 hook 失敗）" || bad "沒 opt-in 讀不到現值的處理不對（rc=${rc}）"
run_hook "$TMP/lk.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_EXISTING_LLM_KEYS='not-a-fernet-key'; rc=$?
[ "$rc" != 0 ] && ! grep -q 'extraSecretEnv' "$TMP/lk.yaml" && ok "現值格式不合：不覆寫、rc≠0" || bad "現值格式不合仍覆寫（rc=${rc}）"

# 2g. 現值在、但這次讀不到簽章金鑰 ⇒ 只保留加密金鑰、不放行 new-api-stg
run_hook "$TMP/keep.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_KUBECTL_MODE=short FAKE_EXISTING_LLM_KEYS="$k1"; rc=$?
python3 - "$TMP/keep.yaml" "$k1" <<'PY' && [ "$rc" != 0 ] && ok "簽章金鑰讀不到時只保留加密金鑰（rc≠0）" || bad "簽章金鑰讀不到時把加密金鑰丟了（rc=${rc}）"
import sys, yaml
data = yaml.safe_load(open(sys.argv[1]).read())
assert data["odoo"]["extraSecretEnv"] == {"WOOW_LLM_SECRET_KEYS": sys.argv[2]}, data["odoo"]
assert data["networkPolicy"]["allowEgressToNamespaces"] == ["paas-stg-system"]
PY

# 3. opt-in、讀不到或格式不合 ⇒ 整段不寫（fail closed），沙盒照建（values 照寫）
#    short：簽章金鑰格式不合（新沙盒）⇒ WARN、rc=0；
#    missing：kubectl 整個不通（context 不對）⇒ 連現值都讀不到 ⇒ ERROR、rc≠0（create.sh 照樣套 values）。
for mode in missing short; do
  run_hook "$TMP/nokey.yaml" MODE=stg WOOW_LLM_SANDBOX=1 FAKE_KUBECTL_MODE="$mode"; rc=$?
  want_rc=0; want_msg='WARN pre-install'
  [ "$mode" = missing ] && { want_rc=1; want_msg='ERROR pre-install'; }
  [ "$rc" = "$want_rc" ] && ok "金鑰 ${mode}：rc=${rc}" || bad "金鑰 ${mode}：rc=${rc}（預期 ${want_rc}）"
  grep -q 'extraSecretEnv\|new-api-stg' "$TMP/nokey.yaml" && bad "金鑰 ${mode}：仍寫了 LLM values" || ok "金鑰 ${mode}：LLM 段整段不寫"
  grep -q 'allowEgressToNamespaces: \[paas-stg-system\]$' "$TMP/nokey.yaml" && ok "金鑰 ${mode}：其餘 values 照寫" || bad "金鑰 ${mode}：其餘 values 沒寫"
  grep -q "$want_msg" "$TMP/stderr" && ok "金鑰 ${mode}：有 ${want_msg%% *}" || bad "金鑰 ${mode}：沒有 ${want_msg%% *}"
done

# 4. 值域與 lockdown
for v in yes true 2 on; do
  run_hook "$TMP/x.yaml" MODE=stg WOOW_LLM_SANDBOX="$v"; rc=$?
  [ "$rc" != 0 ] && ok "WOOW_LLM_SANDBOX=${v} fail-loud" || bad "WOOW_LLM_SANDBOX=${v} 應 fail-loud"
done
for var in WOOW_LLM_SANDBOX=1 WOOW_LLM_SANDBOX=0 WOOW_LLM_ROTATE_SECRET_KEYS=1 WOOW_LLM_SECRET_KEYS=x WOOW_NEWAPI_PROXY_AUTH_WEB=x WOOW_NEWAPI_PROXY_AUTH_CRON=x; do
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

# 5b. salt：同 slug＋salt 穩定、換 salt 換身分、salt 值域
s0="$(bash "$IDENTITY" aimodels 0)"; s1a="$(bash "$IDENTITY" aimodels 1)"; s1b="$(bash "$IDENTITY" aimodels 1)"
[ "$s0" = "$a1" ] && ok "salt 0＝舊行為（只看 slug）" || bad "salt 0 改變了既有沙盒的身分"
[ "$s1a" = "$s1b" ] && [ "$s1a" != "$s0" ] && ok "salt 1 穩定且與 salt 0 不同（${s1a}）" || bad "salt 不穩定或沒有作用"
bash "$IDENTITY" aimodels x >/dev/null 2>&1 && bad "salt=x 應被拒絕" || ok "salt 值域受檢查"

# 8. post-create 的前綴分配（finding 6）：登記表（ConfigMap，原子 create）＋ new-api-stg 探測
mkdir -p "$TMP/pbin"
cat > "$TMP/pbin/kubectl" <<'SH'
#!/usr/bin/env bash
args="$*"
printf '%s\n' "${args%%$'\n'*}" >> "$FAKE_DIR/calls"
case "$args" in
  *"get deploy -n dev-sandbox-p odoo-deploy"*)
    printf 'POSTGRES_HOST\nWOOW_NEWAPI_PROXY_AUTH_WEB\nWOOW_NEWAPI_PROXY_AUTH_CRON\nWOOW_LLM_SECRET_KEYS\n'
    exit 0 ;;
  "create -f -")
    body="$(cat)"
    name="$(sed -n 's/^  name: //p' <<<"$body")"
    prefix="${name#woow-llm-sandbox-prefix-}"
    if [ "${FAKE_CREATE:-ok}" = error ]; then
      echo 'Error from server (Forbidden): configmaps is forbidden' >&2; exit 1
    fi
    if [[ " ${FAKE_CLAIMED:-} " == *" ${prefix} "* ]]; then
      echo "Error from server (AlreadyExists): configmaps \"${name}\" already exists" >&2; exit 1
    fi
    printf '%s\n' "$body" > "$FAKE_DIR/claim-${prefix}"
    echo "configmap/${name} created"; exit 0 ;;
  "exec -i -n dev-sandbox-p deploy/odoo-deploy -- python3 -c "*)
    cat > "$FAKE_DIR/probe-stdin"
    prefix="${args##* }"
    if [ "${FAKE_PROBE:-ok}" = error ]; then echo 'error status_502_connect'; exit 0; fi
    if [[ " ${FAKE_TAKEN:-} " == *" ${prefix} "* ]]; then echo taken; else echo free; fi
    exit 0 ;;
  "annotate configmap"*) exit 0 ;;
  "exec -i -n dev-sandbox-p statefulset/pg -- psql"*)
    n="$(find "$FAKE_DIR" -name 'psql*' | wc -l | tr -d ' ')"
    cat > "$FAKE_DIR/psql${n}"; exit 0 ;;
esac
exit 1
SH
chmod +x "$TMP/pbin/kubectl"

# run_post <dir> [VAR=val…]：乾淨環境跑 post-create 的 LLM 段（SLUG=aimodels）
run_post() {
  local dir="$1"; shift
  rm -rf "$dir"; mkdir -p "$dir"
  env -i PATH="$TMP/pbin:$PATH" HOME="$TMP" FAKE_DIR="$dir" NAMESPACE=dev-sandbox-p DB_NAME=d \
    PG_STATEFULSET=pg ODOO_DEPLOY=odoo-deploy MODE=stg SLUG=aimodels OPERATOR_NS=paas-stg-system \
    WOOW_LLM_SANDBOX=1 "$@" bash "$POST" >"$dir/out" 2>"$dir/err"
}
# sql_value <dir> <key 尾段>：LLM 段寫進 DB 的值
sql_value() { grep -h "'woow_paas_platform.$2'" "$1"/psql* 2>/dev/null | sed -n "s/.*'woow_paas_platform.$2', '\([^']*\)'.*/\1/p"; }
read -r p0 b0 < <(bash "$IDENTITY" aimodels 0)
read -r p1 b1 < <(bash "$IDENTITY" aimodels 1)

run_post "$TMP/pc1"
[ "$(sql_value "$TMP/pc1" newapi_username_prefix)" = "$p0" ] && [ "$(sql_value "$TMP/pc1" llm_synthetic_ip_block)" = "$b0" ] \
  && [ -f "$TMP/pc1/claim-$p0" ] && ok "post-create：沒人用過 ⇒ 登記並設定 salt 0 的 ${p0}/${b0}" || bad "post-create 沒有用 salt 0 的前綴"
grep -q 'def string_to_sign' "$TMP/pc1/probe-stdin" && ok "post-create：探測經 stdin 帶 signed_request.py（不落地）" || bad "探測沒拿到 signed_request.py"
grep -q "app.kubernetes.io/part-of: woow-llm-sandbox" "$TMP/pc1/claim-$p0" && grep -q 'slug: "aimodels"' "$TMP/pc1/claim-$p0" \
  && ok "post-create：登記 ConfigMap 帶 label 與 slug" || bad "登記 ConfigMap 內容不對"
[ "$(sql_value "$TMP/pc1" newapi_proxy_url)" = "http://newapi-proxy.new-api-stg.svc.cluster.local:8080" ] \
  && [ "$(sql_value "$TMP/pc1" show_llm)" = True ] && ok "post-create：代理位址與 show_llm" || bad "post-create 的代理位址／show_llm 不對"

run_post "$TMP/pc2" FAKE_CLAIMED="$p0"
[ "$(sql_value "$TMP/pc2" newapi_username_prefix)" = "$p1" ] && [ ! -f "$TMP/pc2/claim-$p0" ] \
  && ok "post-create：salt 0 已被登記（同 slug 重建或別的 slug）⇒ 用 salt 1 的 ${p1}" || bad "撞到已登記的前綴沒有換"

run_post "$TMP/pc3" FAKE_TAKEN="$p0"
[ "$(sql_value "$TMP/pc3" newapi_username_prefix)" = "$p1" ] && grep -q "annotate configmap -n paas-stg-system woow-llm-sandbox-prefix-$p0" "$TMP/pc3/calls" \
  && ok "post-create：new-api-stg 已有 ${p0}0000000001 ⇒ 標記並換 ${p1}" || bad "撞到 new-api-stg 既有使用者沒有換"

run_post "$TMP/pc4" FAKE_CREATE=error
[ -z "$(sql_value "$TMP/pc4" newapi_username_prefix)" ] && grep -q 'WARN post-create: 無法在 paas-stg-system 登記' "$TMP/pc4/err" \
  && ok "post-create：登記失敗 ⇒ fail closed（不設前綴、不開分頁）" || bad "登記失敗仍設了前綴"

run_post "$TMP/pc5" FAKE_PROBE=error
[ "$(sql_value "$TMP/pc5" newapi_username_prefix)" = "$p0" ] && grep -q 'WARN post-create: 查不了 new-api-stg' "$TMP/pc5/err" \
  && ok "post-create：探測失敗 ⇒ 沿用已登記的 ${p0}＋WARN" || bad "探測失敗的處理不對"

# 9. 探測程式本身（在本機對一個假代理跑；簽章用代理自己的 signing.py 驗）
python3 - "$HERE/../llm-prefix-probe.py" "$HERE/../../extra/newapi-proxy/scripts/signed_request.py" \
  "$HERE/../../extra/newapi-proxy" <<'PY' && ok "llm-prefix-probe：free／taken／error 三態、AP 身分、簽章驗得過、金鑰不出現在標頭" || bad "llm-prefix-probe 不對"
import http.server, json, os, subprocess, sys, threading
probe, signed, proxy_root = sys.argv[1:4]
sys.path.insert(0, proxy_root)
from newapi_proxy import signing
secret = 'S' * 40
mode = {'v': 'free'}
seen = []
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        seen.append((self.path, dict(self.headers)))
        if mode['v'] == 'error':
            self.send_response(502); self.send_header('X-Woow-Proxy-Error', 'connect')
            self.send_header('Content-Length', '2'); self.end_headers(); self.wfile.write(b'{}')
            return
        keyword = self.path.split('keyword=')[1].split('&')[0]
        items = [{'id': 7, 'username': keyword, 'access_token': 'nap_' + 'x' * 30}] if mode['v'] == 'taken' else []
        body = json.dumps({'success': True, 'data': {'items': items, 'total': len(items)}}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:%d' % server.server_address[1]
source = open(probe).read()
env = dict(os.environ, WOOW_NEWAPI_PROXY_AUTH_CRON='kcron:' + secret)
def run(prefix):
    with open(signed) as stdin:
        return subprocess.run([sys.executable, '-c', source, url, prefix], stdin=stdin, env=env,
                              capture_output=True, text=True, timeout=30).stdout.strip()
for want in ('free', 'taken', 'error'):
    mode['v'] = want
    out = run('wsab')
    assert out.split()[0] == want, (want, out)
path, headers = seen[0]
assert path == '/api/user/search?keyword=wsab0000000001&p=1&page_size=10', path
assert headers['X-Woow-Identity'] == 'ap'
p, _, q = path.partition('?')
message = signing.string_to_sign('GET', p, q, b'', headers['X-Woow-Proxy-Ts'], headers['X-Woow-Proxy-Nonce'],
                                 headers['X-Woow-Proxy-Kid'], 'ap', '')
assert signing.signature_matches(secret.encode(), message, headers['X-Woow-Proxy-Sig'])
assert secret not in json.dumps(headers)
assert run('bad!') == 'error usage'
PY

# 7. 選配：真 chart render（需含 SBX-01）
if [ -n "${WOOWPRD_CHART_DIR:-}" ] && command -v helm >/dev/null 2>&1; then
  helm template t "$WOOWPRD_CHART_DIR" -f "$TMP/llm.yaml" >"$TMP/render.yaml" 2>"$TMP/render.err" \
    || bad "helm template 失敗"
  python3 - "$TMP/render.yaml" <<'PY' && ok "chart render：Secret 三個 key（含 hook 回頭讀的那一個、帶 release label）、三條 optional secretKeyRef、NP egress new-api-stg" || bad "chart render 不對"
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
secret_keys = set()
refs = []
egress_ns = set()
for d in docs:
    if d["kind"] == "Secret":
        keys = set((d.get("data") or {}).keys()) | set((d.get("stringData") or {}).keys())
        if "extra-env-woow-llm-secret-keys" in keys:
            # pre-install 用 -l app.kubernetes.io/instance=<slug> 找這個 key（upgrade 路徑沿用現值）
            assert d["metadata"]["labels"]["app.kubernetes.io/instance"] == "t", d["metadata"]
        secret_keys |= keys
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
assert {"extra-env-woow-newapi-proxy-auth-web", "extra-env-woow-newapi-proxy-auth-cron",
        "extra-env-woow-llm-secret-keys"} <= secret_keys, secret_keys
assert "new-api-stg" in egress_ns
PY
else
  echo "SKIP: 沒設 WOOWPRD_CHART_DIR（或沒有 helm），真 chart render 未驗"
fi

echo "----------------------------------------"
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; else echo "${fails} FAILED"; exit 1; fi
