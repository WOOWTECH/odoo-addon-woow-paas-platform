#!/usr/bin/env bash
#
# Seed the e2e fixture (project + draft skill) into a running K8s Dev Sandbox.
# 把 e2e fixture（project + draft skill）冪等地 seed 進跑著的 sandbox。
#
# 為什麼需要這支腳本：
#   tests/e2e/skill/skill-admin-form.spec.ts 需要 DB 裡先存在一筆 draft skill
#   （可見文字 = SKILL_E2E_NAME，預設 'browser-e2e-skill'），spec 才能驅動
#   draft→publish→deprecate→archive 並斷言 is_latest。過去這筆要手動 odoo shell
#   建，極脆弱（見 tests/e2e/skill/README.md）。本腳本把它自動化、冪等、可重跑。
#
#   ⚠️ Contract: --name / --version 預設值必須與 spec 的 SKILL_E2E_NAME /
#      'browser-e2e-skill' 對齊，否則 spec 找不到列。改其一就要同步另一邊。
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/e2e-seed.sh [NAME] [OPTIONS]
#
# Arguments:
#   NAME                Sandbox name (slug). If omitted, auto-detect from current branch.
#
# Options:
#   --context <name>    kubectl context (optional). If omitted, use the active context
#                       (same behaviour as woow-test.sh). Provide it for standalone runs.
#   --name <skill>      Skill name to seed (default: browser-e2e-skill — MUST match spec).
#   --version <v>       Skill version (default: 1.0.0).
#   --project <name>    Holder project name (default: e2e-skill-fixture).
#   --reset             Force the existing fixture back to draft / is_latest=False
#                       (the spec advances it to archived, so reset before each run).
#   --purge             Delete the fixture project (cascade-deletes its skills) and exit.
#   -h, --help          Show usage.
#
# Examples:
#   .claude/skills/k8s-dev-sandbox/scripts/e2e-seed.sh
#   .claude/skills/k8s-dev-sandbox/scripts/e2e-seed.sh feat-skill-list --context docker-desktop
#   .claude/skills/k8s-dev-sandbox/scripts/e2e-seed.sh feat-skill-list --reset

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
KUBE_CONTEXT="${KUBE_CONTEXT:-}"
SKILL_NAME="browser-e2e-skill"   # MUST stay in lock-step with the spec default
SKILL_VERSION="1.0.0"
PROJECT_NAME="e2e-skill-fixture"
RESET=false
PURGE=false

# --- 解析參數 ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --context)
            KUBE_CONTEXT="$2"
            shift 2
            ;;
        --name)
            SKILL_NAME="$2"
            shift 2
            ;;
        --version)
            SKILL_VERSION="$2"
            shift 2
            ;;
        --project)
            PROJECT_NAME="$2"
            shift 2
            ;;
        --reset)
            RESET=true
            shift
            ;;
        --purge)
            PURGE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [NAME] [OPTIONS]"
            echo ""
            echo "Seed the e2e fixture (project + draft skill) into a running dev sandbox."
            echo ""
            echo "Arguments:"
            echo "  NAME                Sandbox name (slug). If omitted, auto-detect from current branch."
            echo ""
            echo "Options:"
            echo "  --context <name>    kubectl context (optional; default: active context)."
            echo "  --name <skill>      Skill name (default: browser-e2e-skill — MUST match the spec)."
            echo "  --version <v>       Skill version (default: 1.0.0)."
            echo "  --project <name>    Holder project name (default: e2e-skill-fixture)."
            echo "  --reset             Force existing fixture back to draft / is_latest=False."
            echo "  --purge             Delete the fixture project (cascade) and exit."
            echo "  -h, --help          Show usage."
            exit 0
            ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
        *)
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

# --- 基本驗證（值會以 env 傳進容器，避免 shell 注入仍先擋明顯非法字元）---
if ! [[ "$SKILL_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo -e "${RED}Invalid --name '${SKILL_NAME}' (allowed: letters, digits, . _ -).${NC}" >&2
    exit 1
fi
if ! [[ "$SKILL_VERSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo -e "${RED}Invalid --version '${SKILL_VERSION}'.${NC}" >&2
    exit 1
fi

# --- 先決條件檢查 ---
if ! command -v kubectl >/dev/null 2>&1; then
    echo -e "${RED}kubectl not found. Please install kubectl first.${NC}" >&2
    exit 2
fi

# --- 切換 context（提供才切；否則沿用 active context，與 woow-test.sh 一致）---
if [ -n "$KUBE_CONTEXT" ]; then
    if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$KUBE_CONTEXT"; then
        echo -e "${RED}ERROR: kubectl context '${KUBE_CONTEXT}' not found in kubeconfig.${NC}" >&2
        exit 2
    fi
    echo -e "${BLUE}Using kubectl context:${NC} ${GREEN}${KUBE_CONTEXT}${NC}"
    kubectl config use-context "$KUBE_CONTEXT" >/dev/null
fi

# --- 如果未提供名稱，從 branch 自動偵測（邏輯同 woow-test.sh 的「從 branch 自動偵測 SLUG」區塊）---
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

# --- 檢查 namespace ---
if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    echo -e "${RED}Namespace '$NAMESPACE' does not exist. Is the sandbox running?${NC}" >&2
    echo -e "${YELLOW}Create one with: /woow-dev:k8s-dev-sandbox create --context <ctx>${NC}" >&2
    exit 2
fi

# --- 解析 odoo deploy 名稱（用 label selector，非字面串接；helm fullname trunc 43，
#     長 slug 會截斷，見 _helpers.tpl）---
ODOO_DEPLOY=$(kubectl get deploy -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -z "$ODOO_DEPLOY" ]; then
    echo -e "${RED}Could not find the odoo deployment in '$NAMESPACE'.${NC}" >&2
    exit 2
fi

# --- 檢查 odoo pod 是否就緒 ---
POD_READY=$(kubectl get pods -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=$SLUG,app.kubernetes.io/component=odoo" \
    -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
if [ "$POD_READY" != "True" ]; then
    echo -e "${RED}Odoo pod is not ready in namespace '$NAMESPACE'.${NC}" >&2
    kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null || true
    exit 2
fi

# --- 取得 DB 名稱：優先讀 secret（邏輯同 woow-test.sh 讀取 "${FULLNAME}-secret" 的
#     postgres-database 區塊），fallback 用 slug 推導（語意同 woow-dev plugin create.sh 的
#     compute_db_name()；該檔在外部 plugin repo，行號對不上，故不標行號）---
DB_NAME=$(kubectl get secret -n "$NAMESPACE" "${FULLNAME}-secret" \
    -o jsonpath='{.data.postgres-database}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if [ -z "$DB_NAME" ]; then
    DB_NAME="woow_$(echo "$SLUG" | sed 's/-/_/g')"
fi

echo ""
echo -e "${BLUE}E2E Seed Configuration:${NC}"
echo -e "  ${BLUE}Sandbox:${NC}    $SLUG"
echo -e "  ${BLUE}Namespace:${NC}  $NAMESPACE"
echo -e "  ${BLUE}Deploy:${NC}     $ODOO_DEPLOY"
echo -e "  ${BLUE}Database:${NC}   $DB_NAME"
echo -e "  ${BLUE}Skill:${NC}      ${SKILL_NAME} ${SKILL_VERSION}"
echo -e "  ${BLUE}Project:${NC}    $PROJECT_NAME"
echo -e "  ${BLUE}Reset:${NC}      $RESET"
echo -e "  ${BLUE}Purge:${NC}      $PURGE"
echo ""

# --- The python body fed into `odoo shell` over stdin.
#     Values arrive via env (set on the in-container `env ... odoo shell` line),
#     so the heredoc is single-quoted — NO shell expansion → no injection.
#     odoo shell rolls back on exit, so we MUST env.cr.commit().
read -r -d '' SEED_PY <<'PYEOF' || true
import hashlib
import os

NAME = os.environ.get("SKILL_E2E_NAME", "browser-e2e-skill")
VERSION = os.environ.get("SKILL_E2E_VERSION", "1.0.0")
PROJECT_NAME = os.environ.get("SKILL_E2E_PROJECT", "e2e-skill-fixture")
RESET = os.environ.get("SKILL_E2E_RESET", "") == "1"
PURGE = os.environ.get("SKILL_E2E_PURGE", "") == "1"

Project = env["project.project"].sudo()
Skill = env["woow_paas_platform.skill"].sudo()

proj = Project.search([("name", "=", PROJECT_NAME)], limit=1)

if PURGE:
    if proj:
        proj.unlink()  # ondelete='cascade' on skill.project_id removes its skills
    env.cr.commit()
    print("SEED_OK purged project=%s" % PROJECT_NAME)
else:
    if not proj:
        proj = Project.create({"name": PROJECT_NAME})

    skill = Skill.search([
        ("project_id", "=", proj.id),
        ("name", "=", NAME),
        ("version", "=", VERSION),
    ], limit=1)

    digest = hashlib.sha256(("%s-%s" % (NAME, VERSION)).encode()).hexdigest()
    if not skill:
        skill = Skill.create({
            "name": NAME,
            "version": VERSION,
            "project_id": proj.id,
            "bundle_sha256": digest,
            "state": "draft",
        })
    elif RESET:
        # spec advances the record to archived; restore the draft precondition.
        skill.write({"state": "draft", "is_latest": False})

    env.cr.commit()
    print("SEED_OK id=%s name=%s state=%s is_latest=%s" % (
        skill.id, skill.name, skill.state, skill.is_latest))
PYEOF

# --- 執行 seed：在容器內以 `env VAR=...` 注入值，再啟 odoo shell 讀 stdin python ---
echo -e "${BLUE}Seeding fixture via odoo shell...${NC}"
set +e
SEED_OUT=$(printf '%s\n' "$SEED_PY" | kubectl exec -i -n "$NAMESPACE" "deploy/${ODOO_DEPLOY}" -- \
    env \
        SKILL_E2E_NAME="$SKILL_NAME" \
        SKILL_E2E_VERSION="$SKILL_VERSION" \
        SKILL_E2E_PROJECT="$PROJECT_NAME" \
        SKILL_E2E_RESET="$([ "$RESET" = true ] && echo 1 || echo 0)" \
        SKILL_E2E_PURGE="$([ "$PURGE" = true ] && echo 1 || echo 0)" \
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

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ E2E fixture seeded in ${NAMESPACE}.${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
