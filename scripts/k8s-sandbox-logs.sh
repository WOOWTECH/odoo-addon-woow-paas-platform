#!/usr/bin/env bash
#
# Show logs from a K8s Dev Sandbox service
# 顯示 Kubernetes 開發沙盒服務的日誌
#
# Usage:
#   scripts/k8s-sandbox-logs.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If not provided, auto-detect from current branch.
#
# Options:
#   --service <name>    Service to show logs for: odoo (default), postgres, nginx
#   -f, --follow        Follow logs in real time
#   --tail <n>          Number of lines to show (default: 100)
#   -h, --help          Show usage
#
# Examples:
#   scripts/k8s-sandbox-logs.sh
#   scripts/k8s-sandbox-logs.sh epic-smarthome
#   scripts/k8s-sandbox-logs.sh --service postgres
#   scripts/k8s-sandbox-logs.sh epic-smarthome --service odoo -f
#   scripts/k8s-sandbox-logs.sh --tail 50 --follow

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
SERVICE="odoo"
FOLLOW=false
TAIL="100"

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --service)
            SERVICE="$2"
            shift 2
            ;;
        -f|--follow)
            FOLLOW=true
            shift
            ;;
        --tail)
            TAIL="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Show logs from a K8s dev sandbox service."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If not provided, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  --service <name>    Service to show logs for: odoo (default), postgres, nginx"
            echo "  -f, --follow        Follow logs in real time"
            echo "  --tail <n>          Number of lines to show (default: 100)"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0"
            echo "  $0 epic-smarthome"
            echo "  $0 --service postgres"
            echo "  $0 epic-smarthome --service odoo -f"
            echo "  $0 --tail 50 --follow"
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

# --- 驗證 service 名稱 ---
case "$SERVICE" in
    odoo|postgres|nginx)
        ;;
    *)
        echo -e "${RED}Unknown service: $SERVICE${NC}" >&2
        echo -e "${YELLOW}Valid services: odoo, postgres, nginx${NC}" >&2
        exit 1
        ;;
esac

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
    exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
    echo -e "${RED}Cannot connect to Kubernetes cluster. Check your kubeconfig.${NC}" >&2
    exit 1
fi

# --- 檢查 namespace 是否存在 ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Namespace '$NAMESPACE' does not exist.${NC}" >&2
    echo -e "${YELLOW}Use scripts/k8s-sandbox-list.sh to see active sandboxes.${NC}" >&2
    exit 1
fi

# --- 對應 service 到 kubectl target ---
case "$SERVICE" in
    odoo)
        TARGET="deploy/${FULLNAME}-odoo"
        ;;
    postgres)
        TARGET="statefulset/${FULLNAME}-postgres"
        ;;
    nginx)
        TARGET="deploy/${FULLNAME}-nginx"
        ;;
esac

# --- 確認 target 存在 ---
RESOURCE_TYPE="${TARGET%%/*}"
RESOURCE_NAME="${TARGET##*/}"
if ! kubectl get "$RESOURCE_TYPE" "$RESOURCE_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Resource '$TARGET' not found in namespace '$NAMESPACE'.${NC}" >&2
    echo -e "${YELLOW}Available pods:${NC}" >&2
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null || true
    exit 1
fi

# --- 構建 kubectl logs 命令 ---
KUBECTL_ARGS=(
    logs
    -n "$NAMESPACE"
    "$TARGET"
    --tail="$TAIL"
)

if [ "$FOLLOW" = true ]; then
    KUBECTL_ARGS+=(-f)
fi

# --- 執行 ---
echo -e "${BLUE}Showing logs for ${SERVICE} in ${NAMESPACE}${NC}"
echo -e "${BLUE}Target: ${TARGET}  |  Tail: ${TAIL}  |  Follow: ${FOLLOW}${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

kubectl "${KUBECTL_ARGS[@]}"
