#!/usr/bin/env bash
#
# Execute Odoo test suite in a K8s Dev Sandbox
# 在 Kubernetes 沙盒環境中執行 Odoo 測試套件
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/woow-test.sh [NAME] [OPTIONS]
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
#   .claude/skills/k8s-dev-sandbox/scripts/woow-test.sh
#   .claude/skills/k8s-dev-sandbox/scripts/woow-test.sh epic-smarthome
#   .claude/skills/k8s-dev-sandbox/scripts/woow-test.sh --module sale_management
#   .claude/skills/k8s-dev-sandbox/scripts/woow-test.sh epic-smarthome --module woow_paas_platform --keep-on-fail

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
MODULE="woow_paas_platform"
KEEP_ON_FAIL=false
E2E_SEED=false

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
        --e2e-seed)
            E2E_SEED=true
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
            echo "  --e2e-seed          After tests pass, seed the e2e fixture (project + draft skill)"
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

# --- 先決條件檢查 ---
if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 2
fi

# --- 檢查 namespace 是否存在 ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Namespace '$NAMESPACE' does not exist. Is the sandbox running?${NC}" >&2
    echo -e "${YELLOW}Create one with: /woow-dev:k8s-dev-sandbox create --context <ctx>${NC}" >&2
    exit 2
fi

# --- 解析 odoo deploy 實名（用 label selector，非字面串接）---
# chart 的 fullname 是 printf "%s-%s" <release-name> "odoo-dev-sandbox" | trunc 43
# （見 charts/odoo-dev-sandbox/templates/_helpers.tpl），release name 就是 slug，
# 所以 slug 一長就被截斷成 -odoo-dev-san / -odoo-dev / -od 等不同形狀，
# 字面串接只有在 slug 長度 <= 26 那一格對得上。同 e2e-seed.sh 的作法。
ODOO_DEPLOY=$(kubectl get deploy -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$ODOO_DEPLOY" ]; then
    echo -e "${RED}Could not find the odoo deployment in namespace '${NAMESPACE}'.${NC}" >&2
    echo -e "${YELLOW}Looked for: deploy -l app.kubernetes.io/instance=${SLUG},app.kubernetes.io/component=odoo${NC}" >&2
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
# secret 的名字同樣受上面那個 trunc 影響，所以也不能字面串接。但 secret 沒有 component
# label（_helpers.tpl 的 labels helper 不含 component，component 是各 workload 自己額外
# 加的），無法用 (instance, component) 兩段直接定位 —— 改成用 instance label 列出候選、
# 再挑真的帶 postgres-database 的那顆。不要直接取 items[0]：stg 模式下同 namespace 還有
# 一顆 image-registry-creds 帶著相同的 instance label，依名字排序它還排在前面。
#
# 取不到就 exit 2，刻意不做「用 db_prefix 推導 DB 名」的 fallback：woow-test.sh 是驗證
# 載體，推錯 DB 名的失敗形狀是「測試跑起來了但跑在別的 DB」（假綠），比直接失敗難發現。
DB_NAME=""
SECRET_REFS=$(kubectl get secret -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=$SLUG" -o name 2>/dev/null || echo "")
for _secret_ref in $SECRET_REFS; do
    _db_b64=$(kubectl get -n "$NAMESPACE" "$_secret_ref" \
        -o jsonpath='{.data.postgres-database}' 2>/dev/null || echo "")
    if [ -n "$_db_b64" ]; then
        DB_NAME=$(printf '%s' "$_db_b64" | base64 -d 2>/dev/null || echo "")
        [ -n "$DB_NAME" ] && break
    fi
done

if [ -z "$DB_NAME" ]; then
    echo -e "${RED}Could not retrieve the database name from any sandbox secret.${NC}" >&2
    echo -e "${YELLOW}Looked in namespace '${NAMESPACE}' for a secret matching app.kubernetes.io/instance=${SLUG} that carries a 'postgres-database' key.${NC}" >&2
    echo -e "${YELLOW}Secrets matching that label: ${SECRET_REFS:-<none>}${NC}" >&2
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

# `/$MODULE` (slash-prefixed) selects the module's tests. A bare `$MODULE` is
# parsed by Odoo as a *tag name* — no test carries that tag, so it matches 0
# tests (silent false green). See docs/tracking/pitfalls/sandbox-module-test-false-green.md.
# Disable errexit so we can capture the exit code from kubectl exec (tee keeps
# the live log; PIPESTATUS[0] is kubectl's real exit code, not tee's).
TEST_LOG="$(mktemp)"
set +e
kubectl exec -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
    odoo -u "$MODULE" --test-enable --test-tags "/$MODULE" --stop-after-init \
    --http-port=18069 --gevent-port=18072 --log-level=test \
    -d "$DB_NAME" -c /etc/odoo/odoo.conf 2>&1 | tee "$TEST_LOG"
TEST_EXIT_CODE=${PIPESTATUS[0]}
set -e

# False-green guard: exit 0 with 0 collected tests (uninstalled module → `-u`
# is a no-op, or a non-matching tag) must NOT be reported as passing.
if grep -qE "of 0 tests when loading database" "$TEST_LOG"; then
    echo -e "${RED}❌ 0 tests ran — module likely not installed (install with -i first) or tag mismatch. Treating as failure.${NC}"
    TEST_EXIT_CODE=1
fi
rm -f "$TEST_LOG"

echo ""

# --- 處理測試結果 ---
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}✅ All tests passed for module: ${MODULE}${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    # Restart odoo after test (--stop-after-init stops the process)
    echo ""
    echo -e "${BLUE}Restarting Odoo...${NC}"
    kubectl rollout restart -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}"
    kubectl rollout status -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" --timeout=120s 2>/dev/null || true

    # Seed the e2e fixture last — after the rollout above the pod is serving again
    # (module tests ran with --stop-after-init). --reset because the e2e run advances
    # the fixture to archived, so each run needs a clean draft starting point.
    if [ "$E2E_SEED" = true ]; then
        echo ""
        echo -e "${BLUE}Seeding e2e fixture...${NC}"
        "${SCRIPT_DIR}/e2e-seed.sh" "$SLUG" --reset
    fi

    exit 0
else
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ Tests failed for module: ${MODULE}${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    if [ "$KEEP_ON_FAIL" = true ]; then
        echo ""
        echo -e "${YELLOW}Extending sandbox TTL by 24h for debugging...${NC}"
        if [ -x "${SCRIPT_DIR}/woow-extend.sh" ]; then
            "${SCRIPT_DIR}/woow-extend.sh" "$SLUG" --ttl 24h
            echo -e "${YELLOW}⚠️  Tests failed. Sandbox TTL extended by 24h for debugging.${NC}"
        else
            echo -e "${YELLOW}⚠️  woow-extend.sh not found. Manually extend TTL if needed.${NC}"
        fi
    fi

    # Restart odoo after test (--stop-after-init stops the process)
    echo ""
    echo -e "${BLUE}Restarting Odoo...${NC}"
    kubectl rollout restart -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}"
    kubectl rollout status -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" --timeout=120s 2>/dev/null || true

    exit 1
fi
