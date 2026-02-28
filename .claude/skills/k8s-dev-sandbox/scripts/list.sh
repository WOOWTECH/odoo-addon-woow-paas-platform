#!/usr/bin/env bash
#
# List all K8s Dev Sandboxes
# 列出所有 Kubernetes 開發沙盒環境
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/list.sh [OPTIONS]
#
# Options:
#   -h, --help          Show usage
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/list.sh

set -euo pipefail

# --- 顏色輸出 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "List all active K8s dev sandboxes."
            echo ""
            echo "Options:"
            echo "  -h, --help          Show usage"
            echo ""
            echo "Examples:"
            echo "  $0"
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
if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
    echo -e "${RED}Cannot connect to Kubernetes cluster. Check your kubeconfig.${NC}" >&2
    exit 1
fi

# --- 取得所有 sandbox namespace ---
NS_JSON=$(kubectl get namespaces -l sandbox.woow.tw/managed=true -o json 2>/dev/null)
NS_COUNT=$(echo "$NS_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null || echo "0")

if [ "$NS_COUNT" = "0" ]; then
    echo -e "${YELLOW}No sandboxes found.${NC}"
    exit 0
fi

# --- 計算時間差的輔助函式 (macOS 相容) ---
# 將 ISO datetime 轉為 epoch seconds
to_epoch() {
    local dt="$1"
    # macOS date: 需要 -jf 格式
    if date -jf "%Y-%m-%dT%H:%M:%SZ" "$dt" +%s >/dev/null 2>&1; then
        date -jf "%Y-%m-%dT%H:%M:%SZ" "$dt" +%s
    else
        # Linux date
        date -d "$dt" +%s 2>/dev/null || echo "0"
    fi
}

# 將秒數格式化為人類可讀的時間
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

# --- 取得每個 namespace 的 pod 狀態摘要 ---
get_pod_status() {
    local ns="$1"
    local pods_json
    pods_json=$(kubectl get pods -n "$ns" -o json 2>/dev/null || echo '{"items":[]}')

    local total running pending failed
    total=$(echo "$pods_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('items',[])))" 2>/dev/null || echo "0")

    if [ "$total" = "0" ]; then
        echo "No Pods"
        return
    fi

    running=$(echo "$pods_json" | python3 -c "
import sys,json
items = json.load(sys.stdin).get('items',[])
print(sum(1 for p in items if p.get('status',{}).get('phase')=='Running'))
" 2>/dev/null || echo "0")

    pending=$(echo "$pods_json" | python3 -c "
import sys,json
items = json.load(sys.stdin).get('items',[])
print(sum(1 for p in items if p.get('status',{}).get('phase')=='Pending'))
" 2>/dev/null || echo "0")

    failed=$(echo "$pods_json" | python3 -c "
import sys,json
items = json.load(sys.stdin).get('items',[])
print(sum(1 for p in items if p.get('status',{}).get('phase') in ('Failed','Unknown')))
" 2>/dev/null || echo "0")

    if [ "$failed" -gt 0 ]; then
        echo "Error (${running}/${total})"
    elif [ "$pending" -gt 0 ]; then
        echo "Pending (${running}/${total})"
    elif [ "$running" = "$total" ]; then
        echo "Running"
    else
        echo "${running}/${total} Ready"
    fi
}

# --- 輸出表頭 ---
NOW_EPOCH=$(date +%s)

printf "${BLUE}%-34s %-26s %-8s %-12s %-14s${NC}\n" "NAME" "BRANCH" "AGE" "TTL-LEFT" "STATUS"
printf "${BLUE}%-34s %-26s %-8s %-12s %-14s${NC}\n" "----" "------" "---" "--------" "------"

# --- 解析並輸出每個 sandbox ---
echo "$NS_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for item in data.get('items', []):
    name = item['metadata']['name']
    ann = item['metadata'].get('annotations', {})
    branch = ann.get('sandbox.woow.tw/branch', '-')
    created = ann.get('sandbox.woow.tw/created', '')
    expires = ann.get('sandbox.woow.tw/expires', '')
    ttl = ann.get('sandbox.woow.tw/ttl', '')
    mode = ann.get('sandbox.woow.tw/mode', '')
    print(f'{name}|{branch}|{created}|{expires}|{ttl}|{mode}')
" 2>/dev/null | while IFS='|' read -r NAME BRANCH CREATED EXPIRES TTL MODE; do

    # 計算 Age
    if [ -n "$CREATED" ] && [ "$CREATED" != "-" ]; then
        CREATED_EPOCH=$(to_epoch "$CREATED")
        if [ "$CREATED_EPOCH" != "0" ]; then
            AGE_SECS=$((NOW_EPOCH - CREATED_EPOCH))
            AGE=$(format_duration "$AGE_SECS")
        else
            AGE="-"
        fi
    else
        AGE="-"
    fi

    # 計算 TTL-LEFT
    if [ "$EXPIRES" = "never" ]; then
        TTL_LEFT="never"
    elif [ -n "$EXPIRES" ] && [ "$EXPIRES" != "-" ]; then
        EXPIRES_EPOCH=$(to_epoch "$EXPIRES")
        if [ "$EXPIRES_EPOCH" != "0" ]; then
            LEFT_SECS=$((EXPIRES_EPOCH - NOW_EPOCH))
            if [ "$LEFT_SECS" -le 0 ]; then
                TTL_LEFT="${RED}expired${NC}"
            else
                TTL_LEFT=$(format_duration "$LEFT_SECS")
            fi
        else
            TTL_LEFT="-"
        fi
    else
        TTL_LEFT="-"
    fi

    # 取得 Pod 狀態
    STATUS=$(get_pod_status "$NAME")

    # 狀態上色
    case "$STATUS" in
        Running)
            STATUS_COLORED="${GREEN}${STATUS}${NC}"
            ;;
        Error*)
            STATUS_COLORED="${RED}${STATUS}${NC}"
            ;;
        Pending*|*Ready)
            STATUS_COLORED="${YELLOW}${STATUS}${NC}"
            ;;
        *)
            STATUS_COLORED="${STATUS}"
            ;;
    esac

    printf "%-34s %-26s %-8s %-12b %-14b\n" "$NAME" "$BRANCH" "$AGE" "$TTL_LEFT" "$STATUS_COLORED"
done

echo ""
echo -e "${BLUE}Total: ${NS_COUNT} sandbox(es)${NC}"
echo ""
