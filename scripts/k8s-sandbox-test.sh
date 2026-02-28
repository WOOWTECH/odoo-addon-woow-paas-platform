#!/usr/bin/env bash
#
# Execute Odoo test suite in a K8s Dev Sandbox
# 在 Kubernetes 沙盒環境中執行 Odoo 測試套件
#
# Usage:
#   scripts/k8s-sandbox-test.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If not provided, auto-detect from current branch.
#
# Options:
#   --module <name>     Module to test (default: woow_paas_platform)
#   --keep-on-fail      If tests fail, extend sandbox TTL by 24h for debugging
#   -h, --help          Show usage
#
# Examples:
#   scripts/k8s-sandbox-test.sh
#   scripts/k8s-sandbox-test.sh epic-smarthome
#   scripts/k8s-sandbox-test.sh --module sale_management
#   scripts/k8s-sandbox-test.sh epic-smarthome --module woow_paas_platform --keep-on-fail

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
SLUG=""
MODULE="woow_paas_platform"
KEEP_ON_FAIL=false

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --module)
            MODULE="$2"
            shift 2
            ;;
        --keep-on-fail)
            KEEP_ON_FAIL=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Execute Odoo test suite in a K8s dev sandbox."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If not provided, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  --module <name>     Module to test (default: woow_paas_platform)"
            echo "  --keep-on-fail      If tests fail, extend sandbox TTL by 24h for debugging"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0"
            echo "  $0 epic-smarthome"
            echo "  $0 --module sale_management"
            echo "  $0 epic-smarthome --module woow_paas_platform --keep-on-fail"
            exit 0
            ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
        *)
            # Positional argument: sandbox name
            if [ -z "$SLUG" ]; then
                SLUG="$1"
            else
                echo -e "${RED}Unexpected argument: $1${NC}" >&2
                echo "Use --help for usage information" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# --- 如果未提供名稱，從 branch 自動偵測 ---
if [ -z "$SLUG" ]; then
    BRANCH=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    SLUG=$(echo "$BRANCH" \
        | sed 's/[^a-zA-Z0-9]/-/g' \
        | tr '[:upper:]' '[:lower:]' \
        | sed 's/-\{2,\}/-/g' \
        | sed 's/^-//;s/-$//' \
        | cut -c1-40)
    echo -e "${BLUE}Auto-detected sandbox from branch: ${BRANCH}${NC}"
fi

NAMESPACE="dev-sandbox-${SLUG}"
FULLNAME="${SLUG}-odoo-dev-sandbox"

# --- 先決條件檢查 ---
if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 2
fi

# --- 檢查 namespace 是否存在 ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Namespace '$NAMESPACE' does not exist. Is the sandbox running?${NC}" >&2
    echo -e "${YELLOW}Create one with: ./scripts/k8s-sandbox-create.sh${NC}" >&2
    exit 2
fi

# --- 檢查 odoo pod 是否就緒 ---
echo -e "${BLUE}Checking odoo pod status...${NC}"

POD_READY=$(kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
    -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")

if [ "$POD_READY" != "True" ]; then
    echo -e "${RED}Odoo pod is not running or not ready in namespace '$NAMESPACE'.${NC}" >&2
    echo -e "${YELLOW}Current pod status:${NC}" >&2
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null || true
    exit 2
fi

echo -e "${GREEN}Odoo pod is ready.${NC}"

# --- 取得資料庫名稱 ---
DB_NAME=$(kubectl get secret -n "$NAMESPACE" "${FULLNAME}-secret" \
    -o jsonpath='{.data.postgres-database}' 2>/dev/null | base64 -d 2>/dev/null || echo "")

if [ -z "$DB_NAME" ]; then
    echo -e "${RED}Could not retrieve database name from secret '${FULLNAME}-secret'.${NC}" >&2
    exit 2
fi

# --- 顯示測試配置 ---
echo ""
echo -e "${BLUE}Test Configuration:${NC}"
echo -e "  ${BLUE}Sandbox:${NC}    $SLUG"
echo -e "  ${BLUE}Namespace:${NC}  $NAMESPACE"
echo -e "  ${BLUE}Database:${NC}   $DB_NAME"
echo -e "  ${BLUE}Module:${NC}     $MODULE"
echo -e "  ${BLUE}Keep:${NC}       $KEEP_ON_FAIL"
echo ""

# --- 執行測試 ---
echo -e "${BLUE}Running tests for module: ${MODULE} in sandbox: ${NAMESPACE}${NC}"
echo ""

# Disable errexit so we can capture the exit code from kubectl exec
set +e
kubectl exec -n "$NAMESPACE" "deploy/${FULLNAME}-odoo" -- \
    odoo --test-enable --test-tags "$MODULE" --stop-after-init \
    --log-level=test -d "$DB_NAME" -c /etc/odoo/odoo.conf
TEST_EXIT_CODE=$?
set -e

echo ""

# --- 處理測試結果 ---
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✅ All tests passed for module: ${MODULE}${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # Restart odoo after test (--stop-after-init stops the process)
    echo ""
    echo -e "${BLUE}Restarting Odoo...${NC}"
    kubectl rollout restart -n "$NAMESPACE" "deploy/${FULLNAME}-odoo"
    kubectl rollout status -n "$NAMESPACE" "deploy/${FULLNAME}-odoo" --timeout=120s 2>/dev/null || true

    exit 0
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ Tests failed for module: ${MODULE}${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if [ "$KEEP_ON_FAIL" = true ]; then
        echo ""
        echo -e "${YELLOW}Extending sandbox TTL by 24h for debugging...${NC}"
        if [ -x "${SCRIPT_DIR}/k8s-sandbox-extend.sh" ]; then
            "${SCRIPT_DIR}/k8s-sandbox-extend.sh" "$SLUG" --ttl 24h
            echo -e "${YELLOW}⚠️  Tests failed. Sandbox TTL extended by 24h for debugging.${NC}"
        else
            echo -e "${YELLOW}⚠️  k8s-sandbox-extend.sh not found. Manually extend TTL if needed.${NC}"
        fi
    fi

    # Restart odoo after test (--stop-after-init stops the process)
    echo ""
    echo -e "${BLUE}Restarting Odoo...${NC}"
    kubectl rollout restart -n "$NAMESPACE" "deploy/${FULLNAME}-odoo"
    kubectl rollout status -n "$NAMESPACE" "deploy/${FULLNAME}-odoo" --timeout=120s 2>/dev/null || true

    exit 1
fi
