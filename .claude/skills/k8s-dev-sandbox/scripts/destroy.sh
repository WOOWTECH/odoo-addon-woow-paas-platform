#!/usr/bin/env bash
#
# Destroy a K8s Dev Sandbox
# 銷毀 Kubernetes 中的 Odoo 開發沙盒環境
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If not provided, auto-detect from current branch.
#
# Options:
#   --force             Skip confirmation prompt
#   -h, --help          Show usage
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh epic-smarthome
#   .claude/skills/k8s-dev-sandbox/scripts/destroy.sh epic-smarthome --force

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
SLUG=""
FORCE=false

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Destroy a K8s dev sandbox."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If not provided, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  --force             Skip confirmation prompt"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0"
            echo "  $0 epic-smarthome"
            echo "  $0 epic-smarthome --force"
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

# --- 檢查先決條件 ---
if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
    echo -e "${RED}helm not found. Please install Helm 3+ first.${NC}" >&2
    exit 1
fi

# --- 檢查 namespace 是否存在 ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${YELLOW}Namespace '$NAMESPACE' does not exist. Nothing to destroy.${NC}"
    exit 0
fi

# --- 確認操作 ---
if [ "$FORCE" = false ]; then
    echo -e "${YELLOW}This will destroy sandbox: ${NAMESPACE}${NC}"
    echo -e "${YELLOW}All data will be lost.${NC}"
    echo ""

    # Show current resources
    echo -e "${BLUE}Resources in namespace:${NC}"
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null || true
    echo ""

    echo -n -e "${YELLOW}Continue? (yes/no): ${NC}"
    read -r CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        echo -e "${YELLOW}Aborted.${NC}"
        exit 0
    fi
fi

echo ""

# --- Helm uninstall ---
echo -e "${BLUE}Uninstalling Helm release '${SLUG}'...${NC}"
if helm uninstall "$SLUG" -n "$NAMESPACE" 2>/dev/null; then
    echo -e "${GREEN}Helm release uninstalled.${NC}"
else
    echo -e "${YELLOW}Helm release '${SLUG}' not found or already removed. Continuing cleanup...${NC}"
fi

# --- 刪除 namespace ---
echo -e "${BLUE}Deleting namespace '${NAMESPACE}'...${NC}"
if kubectl delete namespace "$NAMESPACE" --timeout=60s 2>/dev/null; then
    echo -e "${GREEN}Namespace deleted.${NC}"
else
    echo -e "${YELLOW}Failed to delete namespace within timeout. It may still be terminating.${NC}"
    echo -e "${YELLOW}Check with: kubectl get namespace ${NAMESPACE}${NC}"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Sandbox destroyed: ${NAMESPACE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
