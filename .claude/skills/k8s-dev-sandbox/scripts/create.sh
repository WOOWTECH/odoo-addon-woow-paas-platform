#!/usr/bin/env bash
#
# Create a K8s Dev Sandbox
# 在 Kubernetes 中建立 Odoo 開發沙盒環境
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/create.sh [OPTIONS]
#
# Options:
#   --branch <name>     Override branch slug (default: auto-detect from git)
#   --ttl <duration>    Override TTL (default: 168h = 7 days)
#   --image <tag>       Use custom image (implies ci-local mode unless --mode is set)
#   --mode <mode>       Override sandbox mode: local, ci-local, stg (default: auto)
#   --values <file>     Additional Helm values file
#   --pgadmin           Enable PgAdmin
#   --ingress           Enable Ingress ({slug}.woowtech.io)
#   -h, --help          Show usage
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/create.sh
#   .claude/skills/k8s-dev-sandbox/scripts/create.sh --branch feature-login --ttl 48h
#   .claude/skills/k8s-dev-sandbox/scripts/create.sh --image woow-odoo-dev:main-abc1234
#   .claude/skills/k8s-dev-sandbox/scripts/create.sh --image registry/repo:tag --mode stg

set -euo pipefail

# --- 顏色輸出 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- 取得專案根目錄 ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# --- 預設值 ---
BRANCH_OVERRIDE=""
TTL="168h"
IMAGE_TAG=""
MODE_OVERRIDE=""
EXTRA_VALUES=""
PGADMIN=false
INGRESS=false
# KUBE_CONTEXT must be set explicitly (--context flag or KUBE_CONTEXT env).
# Defaulting to the current context silently lets a user with stale
# `kubectl config use-context docker-desktop` accidentally target the wrong
# cluster; for `helm install` that means "Namespace not found" failures
# leaving partial state, plus a cross-cluster footgun if a name collides.
KUBE_CONTEXT="${KUBE_CONTEXT:-}"

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)
            BRANCH_OVERRIDE="$2"
            shift 2
            ;;
        --ttl)
            TTL="$2"
            shift 2
            ;;
        --image)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --mode)
            MODE_OVERRIDE="$2"
            shift 2
            ;;
        --values)
            EXTRA_VALUES="$2"
            shift 2
            ;;
        --pgadmin)
            PGADMIN=true
            shift
            ;;
        --ingress)
            INGRESS=true
            shift
            ;;
        --context)
            KUBE_CONTEXT="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Create a K8s dev sandbox for the current branch."
            echo ""
            echo "Options:"
            echo "  --branch <name>     Override branch slug (default: auto-detect from git)"
            echo "  --ttl <duration>    Override TTL (default: 168h = 7 days)"
            echo "  --image <tag>       Use custom image (implies ci-local mode unless --mode is set)"
            echo "  --mode <mode>       Override sandbox mode: local, ci-local, stg (default: auto)"
            echo "  --values <file>     Additional Helm values file"
            echo "  --pgadmin           Enable PgAdmin"
            echo "  --ingress           Enable Ingress ({slug}.woowtech.io)"
            echo "  --context <name>    Required: kubectl context (e.g. woow-k3s, docker-desktop)."
            echo "                      Can also be set via KUBE_CONTEXT env var."
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0 --context woow-k3s"
            echo "  $0 --context woow-k3s --branch feature-login --ttl 48h"
            echo "  KUBE_CONTEXT=woow-k3s $0 --image woow-odoo-dev:main-abc1234"
            echo "  $0 --context woow-k3s --image registry/repo:tag --mode stg"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# --- 先決條件檢查 ---
echo -e "${BLUE}Checking prerequisites...${NC}"

if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 1
fi

# Require explicit kubectl context — see KUBE_CONTEXT comment near the top.
if [[ -z "$KUBE_CONTEXT" ]]; then
    echo -e "${RED}ERROR: kubectl context is required (use --context <name> or export KUBE_CONTEXT).${NC}" >&2
    echo -e "${YELLOW}This is required to prevent accidental cross-cluster deploys.${NC}" >&2
    echo -e "${YELLOW}Available contexts:${NC}" >&2
    kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || echo "  (none configured)" >&2
    echo -e "${YELLOW}Example: $0 --context woow-k3s${NC}" >&2
    exit 1
fi
if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$KUBE_CONTEXT"; then
    echo -e "${RED}ERROR: kubectl context '${KUBE_CONTEXT}' not found in kubeconfig.${NC}" >&2
    echo -e "${YELLOW}Available contexts:${NC}" >&2
    kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
    exit 1
fi
# Switch to the requested context; subsequent kubectl/helm calls inherit it.
# Visible echo so the operator sees exactly which cluster they're targeting.
echo -e "${BLUE}Using kubectl context:${NC} ${GREEN}${KUBE_CONTEXT}${NC}"
kubectl config use-context "$KUBE_CONTEXT" >/dev/null
export KUBE_CONTEXT

if ! kubectl --request-timeout=30s cluster-info >/dev/null 2>&1; then
    echo -e "${RED}Cannot connect to Kubernetes cluster '${KUBE_CONTEXT}'. Check your kubeconfig.${NC}" >&2
    exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
    echo -e "${RED}helm not found. Please install Helm 3+ first.${NC}" >&2
    exit 1
fi

HELM_VERSION=$(helm version --short 2>/dev/null | head -1)
HELM_MAJOR=$(echo "$HELM_VERSION" | grep -oE '^v[0-9]+' | tr -d 'v')
if [[ -z "$HELM_MAJOR" ]] || [[ "$HELM_MAJOR" -lt 3 ]]; then
    echo -e "${RED}Helm 3+ is required. Current version: ${HELM_VERSION}${NC}" >&2
    exit 1
fi

CHART_DIR="${PROJECT_ROOT}/charts/odoo-dev-sandbox"
if [ ! -d "$CHART_DIR" ]; then
    echo -e "${RED}Chart directory not found: charts/odoo-dev-sandbox/${NC}" >&2
    exit 1
fi

echo -e "${GREEN}All prerequisites met.${NC}"
echo ""

# --- 解析 Branch 與 Slug ---
if [ -n "$BRANCH_OVERRIDE" ]; then
    BRANCH="$BRANCH_OVERRIDE"
else
    BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
fi

# Convert branch name to slug:
#   - replace non-alphanumeric with dash
#   - lowercase
#   - collapse consecutive dashes
#   - trim leading/trailing dashes
#   - truncate to 40 characters
SLUG=$(echo "$BRANCH" \
    | sed 's/[^a-zA-Z0-9]/-/g' \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/-\{2,\}/-/g' \
    | sed 's/^-//;s/-$//' \
    | cut -c1-40)

NAMESPACE="dev-sandbox-${SLUG}"
DB_NAME="woow_$(echo "$SLUG" | sed 's/-/_/g')"

# --- 決定模式 ---
# Priority: --mode > --image (implies ci-local) > default (local)
if [ -n "$MODE_OVERRIDE" ]; then
    MODE="$MODE_OVERRIDE"
elif [ -n "$IMAGE_TAG" ]; then
    MODE="ci-local"
else
    MODE="local"
fi
EXTRA_ARGS=""

if [ -n "$IMAGE_TAG" ]; then
    # Parse repository:tag
    if [[ "$IMAGE_TAG" == *:* ]]; then
        IMAGE_REPO="${IMAGE_TAG%%:*}"
        IMAGE_TAG_ONLY="${IMAGE_TAG##*:}"
    else
        IMAGE_REPO="woow-odoo-dev"
        IMAGE_TAG_ONLY="$IMAGE_TAG"
    fi
    EXTRA_ARGS="$EXTRA_ARGS --set odoo.image.repository=$IMAGE_REPO --set odoo.image.tag=$IMAGE_TAG_ONLY"
fi

# STG mode: pass registry credentials via temp file to avoid shell history exposure
if [ "$MODE" = "stg" ] && [ -n "${JCR_USER:-}" ] && [ -n "${JCR_PASS:-}" ]; then
    _REGISTRY_VALUES=$(mktemp)
    trap "rm -f $_REGISTRY_VALUES" EXIT
    cat > "$_REGISTRY_VALUES" <<REGEOF
registry:
  server: jcr-prod.woowtech.io
  username: "${JCR_USER}"
  password: "${JCR_PASS}"
REGEOF
    EXTRA_ARGS="$EXTRA_ARGS -f $_REGISTRY_VALUES"
fi

# STG mode: allow cloudflare tunnel ingress + egress to paas-operator
if [ "$MODE" = "stg" ]; then
    EXTRA_ARGS="$EXTRA_ARGS --set networkPolicy.allowIngressFromNamespaces[0]=paas-stg-cloudflare-system"
    EXTRA_ARGS="$EXTRA_ARGS --set networkPolicy.allowEgressToNamespaces[0]=paas-stg-system"
    EXTRA_ARGS="$EXTRA_ARGS --set odoo.filestore.persistence.storageClass=longhorn-delete"
    EXTRA_ARGS="$EXTRA_ARGS --set postgres.storage.storageClass=longhorn-delete"
fi

if [ "$PGADMIN" = true ]; then
    EXTRA_ARGS="$EXTRA_ARGS --set pgadmin.enabled=true"
fi

if [ "$INGRESS" = true ]; then
    EXTRA_ARGS="$EXTRA_ARGS --set ingress.enabled=true --set ingress.domain=woowtech.io"
fi

if [ -n "$EXTRA_VALUES" ]; then
    if [ ! -f "$EXTRA_VALUES" ]; then
        echo -e "${RED}Values file not found: ${EXTRA_VALUES}${NC}" >&2
        exit 1
    fi
    EXTRA_ARGS="$EXTRA_ARGS -f $EXTRA_VALUES"
fi

# --- 顯示配置 ---
echo -e "${BLUE}Sandbox Configuration:${NC}"
echo -e "  ${BLUE}Branch:${NC}     $BRANCH"
echo -e "  ${BLUE}Slug:${NC}       $SLUG"
echo -e "  ${BLUE}Namespace:${NC}  $NAMESPACE"
echo -e "  ${BLUE}Database:${NC}   $DB_NAME"
echo -e "  ${BLUE}TTL:${NC}        $TTL"
echo -e "  ${BLUE}Mode:${NC}       $MODE"
if [ "$PGADMIN" = true ]; then
    echo -e "  ${BLUE}PgAdmin:${NC}    enabled"
fi
if [ "$INGRESS" = true ]; then
    echo -e "  ${BLUE}Ingress:${NC}    ${SLUG}.woowtech.io"
fi
echo ""

# --- 檢查是否已存在 ---
EXISTING=$(helm list -n "$NAMESPACE" -q 2>/dev/null | grep -w "$SLUG" || true)
if [ -n "$EXISTING" ]; then
    echo -e "${YELLOW}Sandbox '$SLUG' already exists in namespace '$NAMESPACE'.${NC}"
    echo -n -e "${YELLOW}Upgrade instead? (yes/no): ${NC}"
    read -r UPGRADE_ANSWER
    if [[ "$UPGRADE_ANSWER" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        HELM_CMD="upgrade"
    else
        echo -e "${YELLOW}Aborted.${NC}"
        exit 0
    fi
else
    HELM_CMD="install"
fi

# --- 構建 Helm 命令 ---
echo -e "${BLUE}Running helm ${HELM_CMD}...${NC}"

HELM_INSTALL_ARGS=(
    "$HELM_CMD" "$SLUG" "$CHART_DIR"
    --namespace "$NAMESPACE"
    --create-namespace
    --set "sandbox.branch=$SLUG"
    --set "sandbox.ttl=$TTL"
    --set "sandbox.mode=$MODE"
)

# 套用對應模式的 values overlay
# 沒套用會讓 chart default（networkPolicy.enabled=true）擋掉 pip install 對 pypi.org 的 egress
case "$MODE" in
    local|ci-local)
        HELM_INSTALL_ARGS+=( -f "$CHART_DIR/values-local.yaml" )
        ;;
    stg)
        HELM_INSTALL_ARGS+=( -f "$CHART_DIR/values-ci.yaml" )
        ;;
esac

# STG 模式：首次部署需更新 web module（base 自動初始化但 web 不會）
# 使用 -u (update) 而非 -i (init)，確保 base.latest_version 正確寫入，
# 且 cron 不會因 init 模式被阻擋
if [ "$MODE" = "stg" ]; then
    HELM_INSTALL_ARGS+=(
        --set "odoo.updateModules=web"
    )
fi

# Local 模式需要掛載 hostPath
if [ "$MODE" = "local" ]; then
    SRC_PATH="${PROJECT_ROOT}/src"
    # extra-addons 來源：優先讀 .env 的 PAAS_CI_ADDONS_PATH（commit 928613a 後的 source of truth），
    # fallback 才用舊路徑 extra/extra-addons（已 deprecated，多數 worktree 不存在）
    if [ -z "${PAAS_CI_ADDONS_PATH:-}" ] && [ -f "${PROJECT_ROOT}/.env" ]; then
        PAAS_CI_ADDONS_PATH="$(grep -E '^PAAS_CI_ADDONS_PATH=' "${PROJECT_ROOT}/.env" | tail -1 | cut -d= -f2-)"
    fi
    if [ -n "${PAAS_CI_ADDONS_PATH:-}" ] && [ -d "$PAAS_CI_ADDONS_PATH" ]; then
        EXTRA_ADDONS_PATH="$PAAS_CI_ADDONS_PATH"
    else
        EXTRA_ADDONS_PATH="${PROJECT_ROOT}/extra/extra-addons"
    fi
    HELM_INSTALL_ARGS+=(
        --set "codeMount.hostPath=$SRC_PATH"
        --set "codeMount.extraAddonsHostPath=$EXTRA_ADDONS_PATH"
    )
fi

# 執行 Helm
# shellcheck disable=SC2086
if ! helm "${HELM_INSTALL_ARGS[@]}" $EXTRA_ARGS; then
    echo -e "${RED}Helm ${HELM_CMD} failed.${NC}" >&2
    echo -e "${YELLOW}Cleaning up partial resources...${NC}"
    helm uninstall "$SLUG" -n "$NAMESPACE" 2>/dev/null || true
    kubectl delete namespace "$NAMESPACE" --ignore-not-found --timeout=30s 2>/dev/null || true
    exit 1
fi

echo -e "${GREEN}Helm ${HELM_CMD} completed.${NC}"
echo ""

# --- 等待 Pod 就緒 ---
echo -e "${BLUE}Waiting for pods to be ready...${NC}"

# Discover actual resource names from K8s (fullname may be truncated by Helm)
ODOO_DEPLOY=$(kubectl get deploy -n "$NAMESPACE" -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
PG_STATEFULSET=$(kubectl get statefulset -n "$NAMESPACE" -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=postgres" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
NGINX_SVC=$(kubectl get svc -n "$NAMESPACE" -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=nginx" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")

if ! kubectl wait --for=condition=ready pod \
    -l "app.kubernetes.io/instance=$SLUG" \
    -n "$NAMESPACE" \
    --timeout=300s 2>/dev/null; then
    echo ""
    echo -e "${YELLOW}Pods did not become ready within 300s.${NC}"
    echo -e "${YELLOW}Current pod status:${NC}"
    kubectl get pods -n "$NAMESPACE" -o wide 2>/dev/null || true
    echo ""
    echo -e "${YELLOW}Check logs with:${NC}"
    echo -e "  kubectl logs -n $NAMESPACE -l app.kubernetes.io/instance=$SLUG --all-containers"
    echo ""
    echo -e "${YELLOW}The sandbox was installed but may not be fully ready yet.${NC}"
    # Don't exit - the sandbox is created, just not ready
fi

echo ""

# --- 等待 Odoo pod 就緒（DB init 需要 pip packages 已安裝）---
echo -e "${BLUE}Waiting for Odoo pod to be ready (pip install + startup)...${NC}"
ODOO_READY=false
for i in $(seq 1 60); do
    if kubectl wait --for=condition=ready pod \
        -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
        -n "$NAMESPACE" --timeout=10s 2>/dev/null; then
        ODOO_READY=true
        break
    fi
    if (( i % 6 == 0 )); then
        echo -e "  Still waiting... (${i}0s elapsed)"
    fi
done

if [ "$ODOO_READY" != "true" ]; then
    echo -e "${YELLOW}Odoo pod not ready after 600s. DB init may fail.${NC}"
fi

# --- 檢查並初始化資料庫 ---
echo -e "${BLUE}Checking database...${NC}"

# issue #85 fix A: gate on whether woow_paas_platform is actually *installed*,
# not merely whether the ir_module_module table exists. A re-deploy that rebinds
# leftover postgres data (Retain PV / kept PVC) sees the table present but the
# module may be uninstalled / half-init, which previously caused the install
# step to be skipped → /woow 404. Query the module state directly instead.
#
# Empty string covers both fresh DB (table missing → psql error → 2>/dev/null
# + ||echo → "") and the module-row-not-present case; "uninstalled" covers
# half-init. Any non-"installed" value → run install. The existing
# `odoo -i "$MODULES"` command is correct for both fresh and half-init: Odoo
# only installs modules in the list that are currently uninstalled (base/web
# already installed are skipped), so the install command itself needs no change.
WOOW_MODULE_STATE=$(kubectl exec -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
    psql -U odoo -d "$DB_NAME" -tAc \
    "SELECT state FROM ir_module_module WHERE name='woow_paas_platform'" 2>/dev/null || echo "")

if [ "$WOOW_MODULE_STATE" != "installed" ]; then
    echo -e "${YELLOW}Database '$DB_NAME' needs install (woow_paas_platform state: '${WOOW_MODULE_STATE:-not present}'). Installing Odoo modules...${NC}"
    echo -e "${YELLOW}(First-time / re-install setup takes 1-3 minutes, please wait)${NC}"

    # Wait for container's pip install to complete before DB init.
    # The container entrypoint runs: pip3 install ... && exec odoo ...
    # If Odoo is not yet listening, pip may still be running.
    echo -e "  ${BLUE}Waiting for pip install to complete in container...${NC}"
    PIP_READY=false
    for i in $(seq 1 90); do
        if kubectl exec -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
            python3 -c "import packaging" 2>/dev/null; then
            PIP_READY=true
            break
        fi
        if (( i % 10 == 0 )); then
            echo -e "  Still waiting for pip... (${i}0s elapsed)"
        fi
        sleep 10
    done

    if [ "$PIP_READY" != "true" ]; then
        echo -e "${YELLOW}pip install not complete after 900s. Installing critical packages manually...${NC}"
        kubectl exec -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
            pip3 install --break-system-packages packaging 'mistune>=3.0.0' 'markdownify>=0.11.0' 'pydantic>=2.0.0,<2.11.0' 'docstring_parser>=0.15' 2>/dev/null || true
    else
        echo -e "  ${GREEN}pip packages ready.${NC}"
    fi

    # Scan for extra addons modules
    MODULES="base,web,woow_paas_platform"
    EXTRA_ADDONS_DIR="${PROJECT_ROOT}/extra/extra-addons"
    if [ -d "$EXTRA_ADDONS_DIR" ]; then
        for dir in "$EXTRA_ADDONS_DIR"/*/; do
            module_name=$(basename "$dir")
            if [[ "$module_name" != .* ]] && [ -f "$dir/__manifest__.py" ]; then
                MODULES="${MODULES},${module_name}"
            fi
        done
    fi

    echo -e "  ${BLUE}Installing modules:${NC} ${MODULES}"

    if kubectl exec -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
        odoo -c /etc/odoo/odoo.conf -d "$DB_NAME" -i "$MODULES" --stop-after-init --without-demo=all --load-language=zh_TW 2>&1 \
        | grep -E "(Loading|Installing|init db|error|Error)" | head -20; then
        echo -e "${GREEN}Database initialized successfully.${NC}"
    else
        echo -e "${YELLOW}Database initialization may have issues. Check logs for details.${NC}"
    fi

    # --- Seed sandbox credentials so test / e2e run without manual Settings input ---
    # operator_url/api_key, paas_domain, namespace_prefix, topup gate, AI pointers
    # are ir.config_parameter (cached in-process → invalidated by the rollout restart
    # below). ECPay creds + AI key live on DB records, not ICP, so they are seeded as
    # rows here too. All idempotent (ON CONFLICT / UPDATE-where). Runs only on fresh
    # install; never in prod (create.sh is dev/stg-sandbox tooling). ECPay values are
    # ECPay's PUBLIC stage test merchant creds (payment-stage.ecpay.com.tw).
    echo -e "${BLUE}Seeding sandbox credentials (operator / paas_domain / topup / AI / ECPay test creds)...${NC}"
    if ! kubectl exec -i -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
        psql -U odoo -d "$DB_NAME" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES
  ('woow_paas_platform.operator_url', 'http://paas-operator.invalid:8000', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.operator_api_key', 'sandbox-operator-key', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.paas_domain', '${SLUG}.woowtech.io', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.namespace_prefix', 'paas-ws-', 1, NOW(), 1, NOW()),
  ('woow_paas_platform.topup_payment_enabled', 'True', 1, NOW(), 1, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;

-- AI default pointers: insert only when the seeded WoowBot records exist (SELECT
-- yields 0 rows otherwise → no-op); value = the canonical record id as text.
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
SQL
    then
        echo -e "${YELLOW}Credential seed had an issue (sandbox still usable; continuing).${NC}"
    else
        echo -e "${GREEN}Sandbox credentials seeded.${NC}"
    fi

    # Restart odoo after init (--stop-after-init stops the process)
    echo -e "${BLUE}Restarting Odoo...${NC}"
    kubectl rollout restart -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}"
    kubectl rollout status -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" --timeout=120s 2>/dev/null || true
else
    # issue #85 fix A: woow_paas_platform confirmed installed → safe to skip install.
    echo -e "${GREEN}Database '$DB_NAME' already initialized (woow_paas_platform installed).${NC}"
fi

echo ""

# --- 顯示存取資訊 ---
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Sandbox created: ${NAMESPACE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BLUE}Branch:${NC}     $BRANCH"
echo -e "  ${BLUE}Namespace:${NC}  $NAMESPACE"
echo -e "  ${BLUE}Database:${NC}   $DB_NAME"
echo -e "  ${BLUE}TTL:${NC}        $TTL"
echo -e "  ${BLUE}Mode:${NC}       $MODE"
echo ""
echo -e "${BLUE}Access:${NC}"
echo -e "  kubectl port-forward -n $NAMESPACE svc/${NGINX_SVC} 8080:80"
echo -e "  Then open: ${YELLOW}http://localhost:8080${NC}"
echo -e "  Login: ${YELLOW}admin / admin${NC}"
echo ""
echo -e "${BLUE}Commands:${NC}"
echo -e "  Status:   ${YELLOW}kubectl get pods -n $NAMESPACE${NC}"
echo -e "  Logs:     ${YELLOW}kubectl logs -n $NAMESPACE deploy/${ODOO_DEPLOY} -f${NC}"
echo -e "  Destroy:  ${YELLOW}\"${SCRIPT_DIR}/destroy.sh\" $SLUG${NC}"
echo ""
