#!/usr/bin/env bash
#
# woow_paas_platform — sandbox post-create hook (post-install seed).
#
# 由 woow-dev plugin 的通用 create.sh 在「module install 完成、odoo 仍 --stop-after-init
# 停著、最終 restart 之前」執行，傳入：
#   NAMESPACE / DB_NAME / PG_STATEFULSET / MODE / OPERATOR_NS / SLUG / KUBE_CONTEXT
#   PRIMARY_MODULE / INSTALL_MODULES / ODOO_DEPLOY / NGINX_SVC / REPO_ROOT / PLUGIN_SKILL_ROOT
#
# seed operator / paas_domain / topup / AI / ECPay / subscription journal，讓 woow sandbox
# 免手動 Settings 即可 test/e2e。全部冪等（ON CONFLICT / UPDATE-where）。只走 psql（odoo 停著
# 也能跑）；seed 完由 create.sh restart odoo 讓 ir.config_parameter 快取重載。
# 原本寫死在舊 in-repo create.sh:588-719；外置至此讓通用工具不帶任何 woow 分支。
# ECPay 值為 ECPay PUBLIC stage 測試商店帳號（payment-stage.ecpay.com.tw）。
set -euo pipefail

: "${NAMESPACE:?post-create hook 需要 NAMESPACE}"
: "${DB_NAME:?post-create hook 需要 DB_NAME}"
: "${PG_STATEFULSET:?post-create hook 需要 PG_STATEFULSET}"
MODE="${MODE:-local}"
OPERATOR_NS="${OPERATOR_NS:-paas-system}"

echo "Seeding woow sandbox credentials (operator / paas_domain / topup / AI / ECPay / journal)..."

OPERATOR_URL="${PAAS_OPERATOR_URL:-}"
OPERATOR_API_KEY="${PAAS_OPERATOR_API_KEY:-}"
PAAS_DOMAIN_VALUE="${PAAS_DOMAIN:-}"
NS_PREFIX="${PAAS_NAMESPACE_PREFIX:-$([ "$MODE" = "stg" ] && echo paas-ws-stg- || echo paas-ws-)}"
# stg: auto-enable agent runtime for new projects (runtime-gated by readiness).
DEFAULT_ENABLE_AR="$([ "$MODE" = "stg" ] && echo True || echo False)"

# lockdown（#1205；prod clone 彩排）：不探索、不讀 operator secret——沙盒裡不留任何真 operator 憑證，
# 直接寫 #1162 D4 的黑洞 sentinel（與 src/data/neutralize.sql 同值），agent runtime 關閉。
# 值域與 pre-install.sh 一致；不合法的值在 pre-install 就已 fail-loud，這裡再擋一次。
case "${SANDBOX_NEUTRALIZE_LOCKDOWN:-}" in
    1)
        OPERATOR_URL="http://127.0.0.1:9"
        OPERATOR_API_KEY="neutralized"
        NS_PREFIX="paas-ws-stg-"
        DEFAULT_ENABLE_AR="False"
        echo "  SANDBOX_NEUTRALIZE_LOCKDOWN=1: operator 寫黑洞 sentinel，不探索叢集內 operator。"
        ;;
    ""|0) ;;
    *)
        echo "ERROR post-create: SANDBOX_NEUTRALIZE_LOCKDOWN='${SANDBOX_NEUTRALIZE_LOCKDOWN}' 不合法（只收 1／0／未設）。" >&2
        exit 1
        ;;
esac

# AI 模型分頁（LLM-01b）的 opt-in：值域與 pre-install 一致，在碰叢集之前就檢查（段落在檔尾）。
case "${WOOW_LLM_SANDBOX:-}" in
    1) _llm=1 ;;
    ""|0) _llm=0 ;;
    *)
        echo "ERROR post-create: WOOW_LLM_SANDBOX='${WOOW_LLM_SANDBOX}' 不合法（只收 1／0／未設）。" >&2
        exit 1
        ;;
esac

# --- 探索叢集內 operator（per-environment；docker-desktop: paas-system，stg: paas-stg-system）---
if [ -z "$OPERATOR_URL" ]; then
    _op_svc=$(kubectl get svc -n "$OPERATOR_NS" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -E '^paas-operator' | head -1 || true)
    if [ -n "$_op_svc" ]; then
        _op_port=$(kubectl get svc -n "$OPERATOR_NS" "$_op_svc" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null || echo 80)
        OPERATOR_URL="http://${_op_svc}.${OPERATOR_NS}.svc.cluster.local:${_op_port:-80}"
        _op_secret=$(kubectl get secret -n "$OPERATOR_NS" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -E 'paas-operator.*secret' | head -1 || true)
        if [ -n "$_op_secret" ]; then
            OPERATOR_API_KEY=$(kubectl get secret -n "$OPERATOR_NS" "$_op_secret" -o jsonpath='{.data.api-key}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
        fi
        echo "  Discovered operator: ${OPERATOR_URL} (api-key: $([ -n "$OPERATOR_API_KEY" ] && echo set || echo none))"
    else
        echo "  No paas-operator found in '${OPERATOR_NS}'; operator stays unconfigured."
    fi
fi

# --- paas_domain 對齊 operator CLOUDFLARE_DOMAIN（F-SA-1：避免 dangling hostname）---
if [ -z "$PAAS_DOMAIN_VALUE" ]; then
    _op_deploy=$(kubectl get deploy -n "$OPERATOR_NS" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -E '^paas-operator' | head -1 || true)
    if [ -n "$_op_deploy" ]; then
        _cf_domain=$(kubectl get deploy -n "$OPERATOR_NS" "$_op_deploy" -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' 2>/dev/null | grep '^CLOUDFLARE_DOMAIN=' | head -1 | cut -d= -f2- || true)
        if [ -n "$_cf_domain" ]; then
            PAAS_DOMAIN_VALUE="$_cf_domain"
            echo "  Discovered paas_domain: ${PAAS_DOMAIN_VALUE} (from operator CLOUDFLARE_DOMAIN)"
        fi
    fi
fi

if ! kubectl exec -i -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
    psql -U odoo -d "$DB_NAME" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES
  ('woow_paas_platform.operator_url', '${OPERATOR_URL}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.operator_api_key', '${OPERATOR_API_KEY}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.paas_domain', '${PAAS_DOMAIN_VALUE:-${SLUG:-sandbox}.woowtech.io}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.namespace_prefix', '${NS_PREFIX}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.topup_payment_enabled', 'True', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.default_enable_agent_runtime', '${DEFAULT_ENABLE_AR}', 1, NOW(), 1, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;

-- AI default pointers: insert only when the seeded WoowBot records exist.
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
SELECT 'woow_paas_platform.default_ai_assistant_id', res_id::text, 1, NOW(), 1, NOW()
FROM ir_model_data WHERE module = 'woow_paas_platform' AND name = 'ai_assistant_woowbot'
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;

INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
SELECT 'woow_paas_platform.default_ai_config_id', res_id::text, 1, NOW(), 1, NOW()
FROM ir_model_data WHERE module = 'woow_paas_platform' AND name = 'ai_config_woowbot'
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;

-- ECPay: public stage test creds. No-op if the ECPay provider row isn't present.
UPDATE payment_provider
SET "MerchantID" = '3002607',
    "HashKey" = 'pwFHCqoQZGmho4w6',
    "HashIV" = 'EkRm7iFT261dpevs',
    enable_ecpg = true,
    state = 'test',
    write_date = NOW(), write_uid = 1
WHERE code = 'ECPay';

-- WoowBot AI config: point the sandbox's default config at the internal
-- openai-compatible test gateway so AI features work out of the box.
UPDATE ai_config
SET "type" = 'openai_compatible',
    api_base_url = 'https://eugene-minimax.woowtech.io/v1',
    api_key = 'test',
    write_date = NOW(), write_uid = 1
WHERE id = (SELECT res_id FROM ir_model_data
            WHERE module = 'woow_paas_platform' AND name = 'ai_config_woowbot');

-- Subscription journal: link each company's sale journal ("Customer Invoices").
UPDATE res_company c
SET sh_journal_id = (SELECT j.id FROM account_journal j
                     WHERE j.company_id = c.id AND j.type = 'sale'
                     ORDER BY j.id LIMIT 1)
WHERE c.sh_journal_id IS NULL
  AND EXISTS (SELECT 1 FROM account_journal j WHERE j.company_id = c.id AND j.type = 'sale');
SQL
then
    echo "WARN: Credential seed had an issue (sandbox still usable; continuing)." >&2
else
    echo "Sandbox credentials seeded."
fi

# --- AI 模型分頁（LLM-01b；opt-in WOOW_LLM_SANDBOX=1，DESIGN N14、§2.11）-----------------------
# pre-install 已把 WEB／CRON 簽章金鑰與本沙盒的加密金鑰經 SBX-01 的 odoo.extraSecretEnv 注入；
# 這裡設定本沙盒的 new-api 身分（前綴、IP block）、代理位址與分頁開關。只在 env 真的進了
# Odoo deployment 時才設：否則（chart 不支援 extraSecretEnv、或 pre-install 讀不到金鑰）設了
# 也只會 fail closed，不如維持「未設定」（N27：LLM cron 一律 no-op）。
# 不寫進 .sandbox.yml config_params_stg：那會對本 repo 所有 stg 沙盒生效（〔REV〕47）。
#
# 前綴的分配（LLM-01b finding 6）：只有 676 個前綴，new-api-stg 的使用者又刪不掉，所以不能只靠
# slug 的雜湊（不同 slug 會撞、同一個 slug 重建一定撞——撞到的結果是新沙盒的帳戶 P1 撞名、P2
# 找到舊使用者、P4 用新密碼登入失敗 ⇒ 永久 llm_newapi_name_taken）。從 salt 0 往上試
# （.sandbox/llm-identity.sh），挑第一個同時滿足：
#   (1) 原子登記成功：kubectl create ConfigMap ${PAAS_STG_SYSTEM_NAMESPACE:-paas-stg-system}/
#       woow-llm-sandbox-prefix-<前綴>（AlreadyExists＝別的沙盒用過，含同一個 slug 的前一個沙盒）；
#       登記表**永不刪除**（new-api 的使用者也刪不掉）。列出：
#       kubectl get cm -n paas-stg-system -l app.kubernetes.io/part-of=woow-llm-sandbox
#   (2) new-api-stg 上還沒有 <前綴>0000000001（登記表之前的舊沙盒；經代理以 AP 查，
#       .sandbox/llm-prefix-probe.py 在本沙盒 Odoo pod 裡跑，AP 的 GA 1 格）。查不了（代理不通）
#       ⇒ 沿用已登記的前綴並 WARN：登記表是之後的權威，查不了不該讓整個 LLM 沙盒建不起來。
# 登記失敗（不是 AlreadyExists，例如沒有權限）⇒ fail closed：不設前綴，LLM 維持未設定。
if [ "$_llm" = 1 ] && [ "$MODE" = "stg" ] && [ "${SANDBOX_NEUTRALIZE_LOCKDOWN:-}" != 1 ]; then
    _llm_env_names=""
    if [ -n "${ODOO_DEPLOY:-}" ]; then
        _llm_env_names="$(kubectl get deploy -n "$NAMESPACE" "$ODOO_DEPLOY" \
            -o jsonpath='{range .spec.template.spec.containers[*]}{range .env[*]}{.name}{"\n"}{end}{end}' 2>/dev/null || true)"
    fi
    _llm_missing=""
    for _name in WOOW_NEWAPI_PROXY_AUTH_WEB WOOW_NEWAPI_PROXY_AUTH_CRON WOOW_LLM_SECRET_KEYS; do
        grep -qx "$_name" <<<"$_llm_env_names" || _llm_missing="$_llm_missing $_name"
    done
    if [ -n "$_llm_missing" ]; then
        echo "WARN post-create: Odoo deployment 沒有${_llm_missing}（chart 需含 SBX-01 的 odoo.extraSecretEnv，且 pre-install 要讀得到 paas-stg-system/paas-stg-newapi-proxy-client）。" >&2
        echo "WARN post-create: AI 模型分頁維持未設定（不開分頁、LLM cron 不外呼）。" >&2
    else
        : "${SLUG:?post-create 的 LLM 段需要 SLUG}"
        _llm_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        _llm_registry_ns="${PAAS_STG_SYSTEM_NAMESPACE:-paas-stg-system}"
        _llm_proxy='http://newapi-proxy.new-api-stg.svc.cluster.local:8080'
        _llm_probe_src="$(cat "${_llm_here}/llm-prefix-probe.py")"
        _llm_signed="${_llm_here}/../extra/newapi-proxy/scripts/signed_request.py"
        _llm_prefix=""
        _llm_block=""
        _llm_salt=0
        while [ "$_llm_salt" -lt 30 ]; do
            read -r _p _b < <(bash "${_llm_here}/llm-identity.sh" "$SLUG" "$_llm_salt")
            if ! [[ "$_p" =~ ^ws[a-z]{2}$ ]] || ! [[ "$_b" =~ ^([1-9]|[12][0-9]|3[01])$ ]]; then
                echo "ERROR post-create: llm-identity.sh 的輸出不合格式（prefix='${_p}' block='${_b}'）。" >&2
                exit 1
            fi
            _claim_rc=0
            _claim_out="$(kubectl create -f - 2>&1 <<YAML
apiVersion: v1
kind: ConfigMap
metadata:
  name: woow-llm-sandbox-prefix-${_p}
  namespace: ${_llm_registry_ns}
  labels:
    app.kubernetes.io/part-of: woow-llm-sandbox
    app.kubernetes.io/managed-by: woow-paas-platform-sandbox-hook
data:
  prefix: "${_p}"
  block: "${_b}"
  slug: "${SLUG}"
  namespace: "${NAMESPACE}"
  salt: "${_llm_salt}"
  claimed_at: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
YAML
)" || _claim_rc=$?
            if [ "$_claim_rc" != 0 ]; then
                if grep -q 'AlreadyExists' <<<"$_claim_out"; then
                    echo "  前綴 ${_p} 已被登記（別的 LLM 沙盒用過），試下一個。"
                    _llm_salt=$((_llm_salt + 1))
                    continue
                fi
                echo "WARN post-create: 無法在 ${_llm_registry_ns} 登記 AI 模型前綴（${_claim_out%%$'\n'*}）；AI 模型分頁維持未設定。" >&2
                break
            fi
            _probe="$(kubectl exec -i -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
                python3 -c "$_llm_probe_src" "$_llm_proxy" "$_p" <"$_llm_signed" 2>/dev/null | tail -n 1 || true)"
            case "$_probe" in
                free)
                    _llm_prefix="$_p"; _llm_block="$_b"
                    break ;;
                taken)
                    echo "  前綴 ${_p} 在 new-api-stg 已有使用者（登記表之前的沙盒），保留登記、試下一個。"
                    kubectl annotate configmap -n "$_llm_registry_ns" "woow-llm-sandbox-prefix-${_p}" \
                        woow.io/llm-legacy-users=true --overwrite >/dev/null 2>&1 || true
                    _llm_salt=$((_llm_salt + 1))
                    continue ;;
                *)
                    echo "WARN post-create: 查不了 new-api-stg 上有沒有 ${_p}0000000001（${_probe:-沒有輸出}）；沿用已登記的前綴 ${_p}。" >&2
                    _llm_prefix="$_p"; _llm_block="$_b"
                    break ;;
            esac
        done
        if [ -z "$_llm_prefix" ]; then
            echo "WARN post-create: 沒有可用的 AI 模型前綴（試了 salt 0–${_llm_salt}）；AI 模型分頁維持未設定。" >&2
        elif kubectl exec -i -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
            psql -U odoo -d "$DB_NAME" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES
  ('woow_paas_platform.newapi_proxy_url', '${_llm_proxy}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.newapi_username_prefix', '${_llm_prefix}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.llm_synthetic_ip_block', '${_llm_block}', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.show_llm', 'True', 1, NOW(), 1, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;
SQL
        then
            echo "AI 模型分頁已開啟：new-api 使用者前綴 ${_llm_prefix}、合成 IP block ${_llm_block}（代理 new-api-stg；登記 ${_llm_registry_ns}/woow-llm-sandbox-prefix-${_llm_prefix}）。"
        else
            echo "WARN post-create: AI 模型分頁的設定寫入失敗（sandbox 仍可用）。" >&2
        fi
    fi
fi
