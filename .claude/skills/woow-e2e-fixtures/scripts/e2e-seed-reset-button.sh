#!/usr/bin/env bash
#
# Seed the Reset-button-visibility e2e fixture into a running K8s Dev Sandbox.
# 把 #464 headed E2E 需要的 fixture（1 workspace + 4 個代表模板的 CloudService）
# 冪等地 seed 進跑著的 sandbox，並輸出一份 JSON manifest 給 spec 讀。
#
# 為什麼需要這支腳本：
#   tests/e2e/service-reset-button/reset-button-visibility.spec.ts 要對「四個
#   代表模板的服務詳情頁」斷言 Reset 按鈕可見性。按鈕的判準是 service payload 的
#   template.has_admin_gui（#460 S1），**純前端渲染**——不需要真的把 Helm release
#   裝到 K8s 上。所以 fixture 只要 DB 裡有 state=running 且掛對 template 的
#   CloudService 列即可，毋須 operator、毋須真部署。
#
#   ⚠️ Contract: 下方 SLUGS 的四個 slug 必須與 spec 的 CASES 對齊，spec 才找得到
#      對應 service id。改其一就要同步另一邊。manifest 路徑同理（--manifest /
#      spec 的 RESET_BTN_E2E_FIXTURE）。
#
# Usage:
#   .claude/skills/woow-e2e-fixtures/scripts/e2e-seed-reset-button.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If omitted, auto-detect from current branch.
#
# Options:
#   --context <name>    kubectl context (optional). If omitted, use the active context
#                       (same behaviour as e2e-seed.sh). Provide it for standalone runs.
#   --manifest <path>   Where to write the fixture JSON (default:
#                       /tmp/reset-button-e2e-fixture.json — MUST match the spec default).
#   --purge             Delete the fixture workspace (cascade-deletes its services) and exit.
#   -h, --help          Show usage.
#
# Examples:
#   .claude/skills/woow-e2e-fixtures/scripts/e2e-seed-reset-button.sh e2e464 --context docker-desktop
#   .claude/skills/woow-e2e-fixtures/scripts/e2e-seed-reset-button.sh e2e464 --purge

set -euo pipefail

# --- 顏色輸出 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# --- 預設值 ---
SLUG=""
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
MANIFEST_PATH="/tmp/reset-button-e2e-fixture.json"   # MUST stay in lock-step with the spec
WORKSPACE_NAME="reset-button-e2e-ws"
PURGE=false

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case $1 in
        --context)
            KUBE_CONTEXT="$2"; shift 2 ;;
        --manifest)
            MANIFEST_PATH="$2"; shift 2 ;;
        --purge)
            PURGE=true; shift ;;
        -h|--help)
            sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}" >&2; exit 1 ;;
        *)
            SLUG="$1"; shift ;;
    esac
done

# --- 自動偵測 sandbox slug（同 e2e-seed.sh）---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# #32：這支原本連「context 存不存在」都沒驗，直接 use-context 未經檢查的輸入，是三處
# 裡最弱的一份。改走共用守衛（驗存在 + 值域白名單 + kubeconfig 隔離）。
# 註：PROJECT_ROOT 的計算被搬到此區塊之前 —— 守衛要用它找 .sandbox.yml。
# shellcheck source=../../lib/kube-context.sh
source "${PROJECT_ROOT}/.claude/skills/lib/kube-context.sh"
if [ -n "$KUBE_CONTEXT" ]; then
    wk_require_context "$KUBE_CONTEXT" e2e-seed "$PROJECT_ROOT" || exit 2
fi

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

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Namespace '$NAMESPACE' does not exist. Is the sandbox running?${NC}" >&2
    echo -e "${YELLOW}Create one with: /woow-dev:k8s-dev-sandbox create --context <ctx>${NC}" >&2
    exit 2
fi

# --- 解析 odoo deploy 名稱（label selector；helm fullname trunc 43，長 slug 會截斷）---
ODOO_DEPLOY=$(kubectl get deploy -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$ODOO_DEPLOY" ]; then
    echo -e "${RED}Could not find the odoo deployment in '$NAMESPACE'.${NC}" >&2
    exit 2
fi

POD_READY=$(kubectl get pods -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
    -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
if [ "$POD_READY" != "True" ]; then
    echo -e "${RED}Odoo pod is not ready in namespace '$NAMESPACE'.${NC}" >&2
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null || true
    exit 2
fi

DB_NAME=$(kubectl get secret -n "$NAMESPACE" "${FULLNAME}-secret" \
    -o jsonpath='{.data.postgres-database}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if [ -z "$DB_NAME" ]; then
    DB_NAME="woow_$(echo "$SLUG" | sed 's/-/_/g')"
fi

echo ""
echo -e "${BLUE}Reset-button E2E Seed Configuration:${NC}"
echo -e "  ${BLUE}Sandbox:${NC}    $SLUG"
echo -e "  ${BLUE}Namespace:${NC}  $NAMESPACE"
echo -e "  ${BLUE}Deploy:${NC}     $ODOO_DEPLOY"
echo -e "  ${BLUE}Database:${NC}   $DB_NAME"
echo -e "  ${BLUE}Workspace:${NC}  $WORKSPACE_NAME"
echo -e "  ${BLUE}Manifest:${NC}   $MANIFEST_PATH"
echo -e "  ${BLUE}Purge:${NC}      $PURGE"
echo ""

# --- The python body fed into `odoo shell` over stdin.
#     Values arrive via env (set on the in-container `env ... odoo shell` line),
#     so the heredoc is single-quoted — NO shell expansion → no injection.
#     odoo shell rolls back on exit, so we MUST env.cr.commit().
read -r -d '' SEED_PY <<'PYEOF' || true
import json
import os

# #519：cloud_service 的建立要經平台自己的開閘 helper。`odoo shell` 是
# in-process，import 得到那個模組層級的 sentinel（RPC 呼叫端則不行）。
from odoo.addons.woow_paas_platform.models.cloud_service import (
    _internal_provision,
)

WS_NAME = os.environ.get("RESET_BTN_E2E_WS", "reset-button-e2e-ws")
PURGE = os.environ.get("RESET_BTN_E2E_PURGE", "") == "1"

# 四個代表模板（#464 驗收條件）。xmlid → 期望的按鈕可見性由 has_admin_gui 決定，
# 這裡**不寫死期望值**——期望值住 spec，seed 只負責把真實模板掛上去。
TEMPLATE_XMLIDS = [
    "woow_paas_platform.cloud_app_hermes_agent",
    "woow_paas_platform.cloud_app_mcp_k8s",
    "woow_paas_platform.cloud_app_mcp_odoo",
    "woow_paas_platform.cloud_app_n8n",
]

Workspace = env["woow_paas_platform.workspace"].sudo()
Service = env["woow_paas_platform.cloud_service"].sudo()
Template = env["woow_paas_platform.cloud_app_template"].sudo()

ws = Workspace.search([("name", "=", WS_NAME)], limit=1)

if PURGE:
    if ws:
        ws.service_ids.unlink()
        ws.unlink()
    env.cr.commit()
    print("SEED_OK purged workspace=%s" % WS_NAME)
else:
    if not ws:
        ws = Workspace.create({
            "name": WS_NAME,
            "owner_id": env.ref("base.user_admin").id,
        })

    services = {}
    for xmlid in TEMPLATE_XMLIDS:
        tpl = env.ref(xmlid)

        # is_hidden 是**定價／上架閘門**（呈現層過濾，見 HiddenChainMixin），與
        # has_admin_gui 正交。hermes-agent 的 seed 值是 True（#424 待定價），而
        # 服務詳情端點對 hidden 模板一律回 'Service not found' → 詳情頁根本開不了。
        # fixture 在此把它翻成 False，讓「按鈕可見性」這個受測面向能被單獨觀測
        # （stg 於 2026-07-28 為同樣理由做過等價的 DB 直改）。**不是**本次受測邏輯。
        if tpl.is_hidden:
            tpl.write({"is_hidden": False})

        svc = Service.search([
            ("workspace_id", "=", ws.id),
            ("template_id", "=", tpl.id),
        ], limit=1)
        if not svc:
            svc = _internal_provision(Service).create({
                "workspace_id": ws.id,
                "template_id": tpl.id,
                "name": "e2e-%s" % tpl.slug,
                # state=running：canPerformActions 只在 transitional state 為 False，
                # running 讓 action bar 完整渲染（按鈕可見且 enabled）。
                "state": "running",
                "subdomain": "e2e-%s" % tpl.slug,
            })
        elif svc.state != "running":
            svc.write({"state": "running"})

        services[tpl.slug] = {
            "service_id": svc.id,
            "template_id": tpl.id,
            "template_name": tpl.name,
            # DB ground truth，供 spec 交叉驗證「payload 欄位 == DB 欄位」
            "has_admin_gui": bool(tpl.has_admin_gui),
        }

    env.cr.commit()
    print("FIXTURE_JSON " + json.dumps({
        "workspace_id": ws.id,
        "workspace_name": ws.name,
        "services": services,
    }, sort_keys=True))
    print("SEED_OK workspace=%s services=%s" % (ws.id, len(services)))
PYEOF

echo -e "${BLUE}Seeding fixture via odoo shell...${NC}"
set +e
SEED_OUT=$(printf '%s\n' "$SEED_PY" | kubectl exec -i -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
    env \
        RESET_BTN_E2E_WS="$WORKSPACE_NAME" \
        RESET_BTN_E2E_PURGE="$([ "$PURGE" = true ] && echo 1 || echo 0)" \
        odoo shell -d "$DB_NAME" -c /etc/odoo/odoo.conf --no-http 2>&1)
EXEC_RC=$?
set -e

echo "$SEED_OUT"

if [ $EXEC_RC -ne 0 ] || ! echo "$SEED_OUT" | grep -q "SEED_OK"; then
    echo ""
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}❌ Seed failed (rc=${EXEC_RC}, no SEED_OK marker).${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    exit 1
fi

if [ "$PURGE" = false ]; then
    # 只取 marker 行的 JSON 段（odoo shell 會混雜 log 行）
    FIXTURE_JSON=$(echo "$SEED_OUT" | grep '^FIXTURE_JSON ' | tail -1 | sed 's/^FIXTURE_JSON //')
    if [ -z "$FIXTURE_JSON" ]; then
        echo -e "${RED}❌ No FIXTURE_JSON marker in seed output.${NC}" >&2
        exit 1
    fi
    printf '%s\n' "$FIXTURE_JSON" > "$MANIFEST_PATH"
    echo ""
    echo -e "${BLUE}Fixture manifest written to ${MANIFEST_PATH}:${NC}"
    cat "$MANIFEST_PATH"
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Reset-button E2E fixture seeded in ${NAMESPACE}.${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
