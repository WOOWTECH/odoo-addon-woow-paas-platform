#!/usr/bin/env bash
#
# Extend (or set) TTL of a K8s Dev Sandbox
# 延長 Kubernetes 中 Odoo 開發沙盒的存活時間
#
# Usage:
#   scripts/k8s-sandbox-extend.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If not provided, auto-detect from current branch.
#
# Options:
#   --ttl <duration>    New TTL duration (e.g., "168h", "24h", "0" for never)
#   -h, --help          Show usage
#
# Examples:
#   scripts/k8s-sandbox-extend.sh --ttl 48h
#   scripts/k8s-sandbox-extend.sh epic-smarthome --ttl 168h
#   scripts/k8s-sandbox-extend.sh --ttl 0

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
TTL=""

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ttl)
            TTL="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Extend (or set) the TTL of a K8s dev sandbox."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If not provided, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  --ttl <duration>    New TTL duration (e.g., \"168h\", \"24h\", \"0\" for never)"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0 --ttl 48h"
            echo "  $0 epic-smarthome --ttl 168h"
            echo "  $0 --ttl 0"
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

# --- TTL 必須提供 ---
if [ -z "$TTL" ]; then
    echo -e "${RED}--ttl is required.${NC}" >&2
    echo "Use --help for usage information" >&2
    exit 1
fi

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

# --- 檢查 namespace 是否存在 ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Namespace '$NAMESPACE' does not exist.${NC}" >&2
    echo -e "${YELLOW}Create a sandbox first: ./scripts/k8s-sandbox-create.sh${NC}" >&2
    exit 1
fi

# --- 計算新的到期時間 ---
if [ "$TTL" = "0" ]; then
    NEW_EXPIRES="never"
    echo -e "${BLUE}Setting TTL to never (no auto-cleanup)${NC}"
else
    # Parse duration: extract hours from format like "168h"
    if [[ "$TTL" =~ ^([0-9]+)h$ ]]; then
        HOURS="${BASH_REMATCH[1]}"
    else
        echo -e "${RED}Invalid TTL format: $TTL${NC}" >&2
        echo -e "${YELLOW}Use format like: 24h, 48h, 168h, or 0 for never${NC}" >&2
        exit 1
    fi

    # Calculate new expires time: current time + TTL duration (macOS compatible)
    NEW_EXPIRES=$(date -v+"${HOURS}"H -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || \
        date -u -d "+${HOURS} hours" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "")

    if [ -z "$NEW_EXPIRES" ]; then
        echo -e "${RED}Failed to calculate new expires time.${NC}" >&2
        exit 1
    fi
fi

# --- 取得舊的到期時間 ---
OLD_EXPIRES=$(kubectl get namespace "$NAMESPACE" \
    -o jsonpath='{.metadata.annotations.sandbox\.woow\.tw/expires}' 2>/dev/null || echo "unknown")

# --- 更新 annotations ---
echo -e "${BLUE}Updating sandbox TTL...${NC}"

kubectl annotate namespace "$NAMESPACE" \
    sandbox.woow.tw/ttl="$TTL" \
    sandbox.woow.tw/expires="$NEW_EXPIRES" \
    --overwrite

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Sandbox TTL updated: ${NAMESPACE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BLUE}Sandbox:${NC}      $SLUG"
echo -e "  ${BLUE}Namespace:${NC}    $NAMESPACE"
echo -e "  ${BLUE}TTL:${NC}          $TTL"
echo -e "  ${BLUE}Old expires:${NC}  $OLD_EXPIRES"
echo -e "  ${BLUE}New expires:${NC}  $NEW_EXPIRES"
echo ""
