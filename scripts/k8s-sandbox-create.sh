#!/usr/bin/env bash
#
# Create a K8s Dev Sandbox
# 在 Kubernetes 中建立 Odoo 開發沙盒環境
#
# Usage:
#   scripts/k8s-sandbox-create.sh [OPTIONS]
#
# Options:
#   --branch <name>     Override branch slug (default: auto-detect from git)
#   --ttl <duration>    Override TTL (default: 168h = 7 days)
#   --image <tag>       Use CI mode with specified image tag
#   --values <file>     Additional Helm values file
#   --pgadmin           Enable PgAdmin
#   -h, --help          Show usage
#
# Examples:
#   scripts/k8s-sandbox-create.sh
#   scripts/k8s-sandbox-create.sh --branch feature-login --ttl 48h
#   scripts/k8s-sandbox-create.sh --image woow-odoo-dev:main-abc1234

set -euo pipefail

# --- 顏色輸出 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- 取得專案根目錄 ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- 預設值 ---
BRANCH_OVERRIDE=""
TTL="168h"
IMAGE_TAG=""
EXTRA_VALUES=""
PGADMIN=false

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
        --values)
            EXTRA_VALUES="$2"
            shift 2
            ;;
        --pgadmin)
            PGADMIN=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Create a K8s dev sandbox for the current branch."
            echo ""
            echo "Options:"
            echo "  --branch <name>     Override branch slug (default: auto-detect from git)"
            echo "  --ttl <duration>    Override TTL (default: 168h = 7 days)"
            echo "  --image <tag>       Use CI mode with specified image tag"
            echo "  --values <file>     Additional Helm values file"
            echo "  --pgadmin           Enable PgAdmin"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0"
            echo "  $0 --branch feature-login --ttl 48h"
            echo "  $0 --image woow-odoo-dev:main-abc1234"
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

if ! kubectl cluster-info >/dev/null 2>&1; then
    echo -e "${RED}Cannot connect to Kubernetes cluster. Check your kubeconfig.${NC}" >&2
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
MODE="local"
EXTRA_ARGS=""

if [ -n "$IMAGE_TAG" ]; then
    MODE="ci"
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

if [ "$PGADMIN" = true ]; then
    EXTRA_ARGS="$EXTRA_ARGS --set pgadmin.enabled=true"
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

# Local 模式需要掛載 hostPath
if [ "$MODE" = "local" ]; then
    SRC_PATH="${PROJECT_ROOT}/src"
    EXTRA_ADDONS_PATH="${PROJECT_ROOT}/extra/extra-addons"
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

# --- 檢查並初始化資料庫 ---
echo -e "${BLUE}Checking database...${NC}"

DB_EXISTS=$(kubectl exec -n "$NAMESPACE" "statefulset/${PG_STATEFULSET}" -- \
    psql -U odoo -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" 2>/dev/null || echo "")

if [ "$DB_EXISTS" != "1" ]; then
    echo -e "${YELLOW}Database '$DB_NAME' does not exist. Initializing Odoo...${NC}"
    echo -e "${YELLOW}(First-time setup takes 1-3 minutes, please wait)${NC}"

    # Scan for extra addons modules
    MODULES="base,woow_paas_platform"
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
        odoo -d "$DB_NAME" -i "$MODULES" --stop-after-init --without-demo=all --load-language=zh_TW 2>&1 \
        | grep -E "(Loading|Installing|init db|error|Error)" | head -20; then
        echo -e "${GREEN}Database initialized successfully.${NC}"
    else
        echo -e "${YELLOW}Database initialization may have issues. Check logs for details.${NC}"
    fi

    # Restart odoo after init (--stop-after-init stops the process)
    echo -e "${BLUE}Restarting Odoo...${NC}"
    kubectl rollout restart -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}"
    kubectl rollout status -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" --timeout=120s 2>/dev/null || true
else
    echo -e "${GREEN}Database '$DB_NAME' already exists.${NC}"
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
echo -e "  Destroy:  ${YELLOW}./scripts/k8s-sandbox-destroy.sh $SLUG${NC}"
echo ""
