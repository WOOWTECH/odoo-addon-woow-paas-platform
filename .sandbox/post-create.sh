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
