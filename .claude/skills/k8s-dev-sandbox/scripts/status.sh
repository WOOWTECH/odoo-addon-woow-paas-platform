#!/usr/bin/env bash
#
# Show detailed status of a K8s Dev Sandbox
# 顯示指定 Kubernetes 開發沙盒的詳細狀態
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/status.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If not provided, auto-detect from current branch.
#
# Options:
#   -h, --help          Show usage
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/status.sh
#   .claude/skills/k8s-dev-sandbox/scripts/status.sh epic-smarthome

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

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Show detailed status of a K8s dev sandbox."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If not provided, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0"
            echo "  $0 epic-smarthome"
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
    echo ""
fi

NAMESPACE="dev-sandbox-${SLUG}"

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
    echo -e "${YELLOW}Use "${SCRIPT_DIR}/list.sh" to see active sandboxes.${NC}" >&2
    exit 1
fi

# --- 計算時間差的輔助函式 (macOS 相容) ---
to_epoch() {
    local dt="$1"
    if date -jf "%Y-%m-%dT%H:%M:%SZ" "$dt" +%s >/dev/null 2>&1; then
        date -jf "%Y-%m-%dT%H:%M:%SZ" "$dt" +%s
    else
        date -d "$dt" +%s 2>/dev/null || echo "0"
    fi
}

format_duration() {
    local total_secs=$1
    if [ "$total_secs" -le 0 ]; then
        echo "0s"
        return
    fi

    local days=$((total_secs / 86400))
    local hours=$(( (total_secs % 86400) / 3600 ))
    local minutes=$(( (total_secs % 3600) / 60 ))

    if [ "$days" -gt 0 ]; then
        echo "${days}d${hours}h"
    elif [ "$hours" -gt 0 ]; then
        echo "${hours}h${minutes}m"
    else
        echo "${minutes}m"
    fi
}

# --- 取得 namespace annotations ---
NS_JSON=$(kubectl get namespace "$NAMESPACE" -o json 2>/dev/null)
ANNOTATIONS=$(echo "$NS_JSON" | python3 -c "
import sys, json
ann = json.load(sys.stdin).get('metadata', {}).get('annotations', {})
print(ann.get('sandbox.woow.tw/branch', '-'))
print(ann.get('sandbox.woow.tw/mode', '-'))
print(ann.get('sandbox.woow.tw/created', '-'))
print(ann.get('sandbox.woow.tw/ttl', '-'))
print(ann.get('sandbox.woow.tw/expires', '-'))
" 2>/dev/null)

BRANCH=$(echo "$ANNOTATIONS" | sed -n '1p')
MODE=$(echo "$ANNOTATIONS" | sed -n '2p')
CREATED=$(echo "$ANNOTATIONS" | sed -n '3p')
TTL=$(echo "$ANNOTATIONS" | sed -n '4p')
EXPIRES=$(echo "$ANNOTATIONS" | sed -n '5p')

NOW_EPOCH=$(date +%s)

# --- 計算 Age ---
AGE_STR="-"
if [ -n "$CREATED" ] && [ "$CREATED" != "-" ]; then
    CREATED_EPOCH=$(to_epoch "$CREATED")
    if [ "$CREATED_EPOCH" != "0" ]; then
        AGE_SECS=$((NOW_EPOCH - CREATED_EPOCH))
        AGE_STR=$(format_duration "$AGE_SECS")
    fi
fi

# --- 計算 TTL-LEFT ---
EXPIRES_STR="-"
TTL_LEFT_STR="-"
if [ "$EXPIRES" = "never" ]; then
    EXPIRES_STR="never"
    TTL_LEFT_STR="never"
elif [ -n "$EXPIRES" ] && [ "$EXPIRES" != "-" ]; then
    EXPIRES_STR="$EXPIRES"
    EXPIRES_EPOCH=$(to_epoch "$EXPIRES")
    if [ "$EXPIRES_EPOCH" != "0" ]; then
        LEFT_SECS=$((EXPIRES_EPOCH - NOW_EPOCH))
        if [ "$LEFT_SECS" -le 0 ]; then
            TTL_LEFT_STR="expired"
        else
            TTL_LEFT_STR=$(format_duration "$LEFT_SECS")
        fi
    fi
fi

# --- 顯示 Sandbox 資訊 ---
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}Sandbox: ${NAMESPACE}${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BLUE}Branch:${NC}    $BRANCH"
echo -e "  ${BLUE}Mode:${NC}      $MODE"
echo -e "  ${BLUE}Created:${NC}   $CREATED ($AGE_STR ago)"
echo -e "  ${BLUE}TTL:${NC}       $TTL"
if [ "$TTL_LEFT_STR" = "expired" ]; then
    echo -e "  ${BLUE}Expires:${NC}   $EXPIRES_STR (${RED}expired${NC})"
else
    echo -e "  ${BLUE}Expires:${NC}   $EXPIRES_STR (${TTL_LEFT_STR} left)"
fi
echo ""

# --- 顯示 Pods ---
echo -e "${BLUE}Pods:${NC}"
kubectl get pods -n "$NAMESPACE" -o wide 2>/dev/null || echo -e "  ${YELLOW}No pods found.${NC}"
echo ""

# --- 顯示 Resource 使用量 ---
echo -e "${BLUE}Resources:${NC}"

# 取得 ResourceQuota
QUOTA_JSON=$(kubectl get resourcequota -n "$NAMESPACE" -o json 2>/dev/null || echo '{"items":[]}')
QUOTA_COUNT=$(echo "$QUOTA_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null || echo "0")

if [ "$QUOTA_COUNT" != "0" ]; then
    # 解析 quota hard/used
    echo "$QUOTA_JSON" | python3 -c "
import sys, json

data = json.load(sys.stdin)
for item in data.get('items', []):
    hard = item.get('status', {}).get('hard', {})
    used = item.get('status', {}).get('used', {})

    def parse_cpu(val):
        if not val:
            return 0
        val = str(val)
        if val.endswith('m'):
            return int(val[:-1])
        return int(float(val) * 1000)

    def format_cpu(millicores):
        if millicores >= 1000:
            return f'{millicores/1000:.0f} cores'
        return f'{millicores}m'

    def parse_mem(val):
        if not val:
            return 0
        val = str(val)
        multipliers = {'Ki': 1024, 'Mi': 1024**2, 'Gi': 1024**3, 'Ti': 1024**4}
        for suffix, mult in multipliers.items():
            if val.endswith(suffix):
                return int(float(val[:-len(suffix)]) * mult)
        if val.endswith('k'):
            return int(float(val[:-1]) * 1000)
        if val.endswith('M'):
            return int(float(val[:-1]) * 1000000)
        if val.endswith('G'):
            return int(float(val[:-1]) * 1000000000)
        return int(val)

    def format_mem(b):
        if b >= 1024**3:
            return f'{b/1024**3:.1f}Gi'
        if b >= 1024**2:
            return f'{b/1024**2:.0f}Mi'
        if b >= 1024:
            return f'{b/1024:.0f}Ki'
        return f'{b}B'

    cpu_used = parse_cpu(used.get('limits.cpu', '0'))
    cpu_hard = parse_cpu(hard.get('limits.cpu', '0'))
    mem_used = parse_mem(used.get('limits.memory', '0'))
    mem_hard = parse_mem(hard.get('limits.memory', '0'))

    cpu_pct = int(cpu_used / cpu_hard * 100) if cpu_hard > 0 else 0
    mem_pct = int(mem_used / mem_hard * 100) if mem_hard > 0 else 0

    print(f'  CPU: {format_cpu(cpu_used)} / {format_cpu(cpu_hard)} ({cpu_pct}%)')
    print(f'  Memory: {format_mem(mem_used)} / {format_mem(mem_hard)} ({mem_pct}%)')
" 2>/dev/null || echo -e "  ${YELLOW}Could not parse resource quota.${NC}"
else
    echo -e "  ${YELLOW}No resource quota configured.${NC}"
fi

# 嘗試使用 metrics-server 取得實際用量
if kubectl top pods -n "$NAMESPACE" >/dev/null 2>&1; then
    echo ""
    echo -e "${BLUE}Pod Metrics:${NC}"
    kubectl top pods -n "$NAMESPACE" 2>/dev/null || true
fi

echo ""

# --- 顯示存取資訊 ---
FULLNAME="${SLUG}-odoo-dev-sandbox"

echo -e "${BLUE}Access:${NC}"
echo -e "  kubectl port-forward -n $NAMESPACE svc/${FULLNAME}-nginx 8080:80"
echo -e "  Then open: ${YELLOW}http://localhost:8080${NC}"
echo ""
echo -e "${BLUE}Commands:${NC}"
echo -e "  Logs:     ${YELLOW}"${SCRIPT_DIR}/logs.sh" $SLUG${NC}"
echo -e "  Destroy:  ${YELLOW}"${SCRIPT_DIR}/destroy.sh" $SLUG${NC}"
echo ""
