#!/usr/bin/env bash
#
# Deploy Staging (paas-odoo)
# 觸發 Gitea CI 建置 staging image，等待完成後部署到 K8s dev sandbox。
#
# Usage:
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh [OPTIONS] [ADDON_REF]
#
# Options:
#   --slug <name>         Sandbox slug (default: stg)
#   --image <full-image>  Skip CI build, use existing image (format: registry/repo:tag)
#   --skip-build          Skip CI build, only poll CI status for existing tag
#   --ingress             Enable Ingress ({slug}.woowtech.io)
#   --tag <tag>           Override git tag (default: stg-YYYYMMDD.HHMM, docker tag auto-derived)
#   --timeout <seconds>   Build wait timeout (default: 900 = 15 min)
#   --interval <seconds>  Poll interval (default: 30)
#   --context <name>      kubectl context (default: woow-k3s; override only when
#                         intentionally targeting a different cluster)
#   -h, --help            Show usage
#
# Prerequisites:
#   tea CLI with login configured for git-prod.woowtech.io
#   (run: tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>)
#
# Examples:
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh v18.0.1.0.5
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh --slug stg-test
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:20260329.1400

set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# --- Project root ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# --- Configuration ---
GITEA_URL="https://git-prod.woowtech.io"
CI_REPO="woow-paas/paas-odoo-ci"
REGISTRY_HOST="jcr-prod.woowtech.io"
IMAGE_PATH="woow-paas-docker-local/paas-odoo-stg"

# --- Defaults ---
SLUG="stg"
ADDON_REF=""
IMAGE_OVERRIDE=""
GIT_TAG_OVERRIDE=""
SKIP_BUILD=false
INGRESS=false
POLL_TIMEOUT=900
POLL_INTERVAL=30
# Staging always targets woow-k3s — this is the only valid stg destination,
# and hardcoding the default closes the cross-cluster footgun (current
# kubectl context could be anything, e.g. docker-desktop). --context still
# overrides for the rare case (e.g. testing the pipeline against a
# secondary cluster), and the override is logged loudly so it's never
# accidental.
KUBE_CONTEXT="${KUBE_CONTEXT:-woow-k3s}"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --slug)
            SLUG="$2"
            shift 2
            ;;
        --image)
            IMAGE_OVERRIDE="$2"
            shift 2
            ;;
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        --ingress)
            INGRESS=true
            shift
            ;;
        --tag)
            GIT_TAG_OVERRIDE="$2"
            shift 2
            ;;
        --timeout)
            POLL_TIMEOUT="$2"
            shift 2
            ;;
        --interval)
            POLL_INTERVAL="$2"
            shift 2
            ;;
        --context)
            KUBE_CONTEXT="$2"
            shift 2
            ;;
        -h|--help)
            head -29 "$0" | tail -27
            exit 0
            ;;
        -*)
            echo -e "${RED}Unknown option: $1${NC}" >&2
            exit 1
            ;;
        *)
            ADDON_REF="$1"
            shift
            ;;
    esac
done

# --- Prerequisites ---
echo -e "${BLUE}Checking prerequisites...${NC}"

for cmd in tea jq kubectl helm git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}Missing required tool: ${cmd}${NC}" >&2
        exit 1
    fi
done

# Verify the kubectl context exists in kubeconfig, then switch + export
# so child scripts (notably create.sh) inherit the same target.
if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx "$KUBE_CONTEXT"; then
    echo -e "${RED}ERROR: kubectl context '${KUBE_CONTEXT}' not found in kubeconfig.${NC}" >&2
    echo -e "${YELLOW}Available contexts:${NC}" >&2
    kubectl config get-contexts -o name 2>/dev/null | sed 's/^/  /' >&2 || true
    echo -e "${YELLOW}Default for stg is 'woow-k3s'; override with --context only if you really mean it.${NC}" >&2
    exit 1
fi
# Loud echo: if the operator is overriding away from woow-k3s, make it
# obvious before any helm/kubectl operation runs.
if [[ "$KUBE_CONTEXT" != "woow-k3s" ]]; then
    echo -e "${YELLOW}WARNING: targeting non-default context '${KUBE_CONTEXT}' (stg default is 'woow-k3s').${NC}"
fi
echo -e "${BLUE}Using kubectl context:${NC} ${GREEN}${KUBE_CONTEXT}${NC}"
kubectl config use-context "$KUBE_CONTEXT" >/dev/null
export KUBE_CONTEXT

# --- Helper: seed web.base.url + freeze for HTTPS ECPay callbacks ---
#
# A fresh sandbox DB has no web.base.url, so Odoo defaults to the request
# host without a scheme — leaking out as `http://stg.woowtech.io/...` in
# any URL we hand to ECPay (notably the bind/topup OrderResultURL). When
# ECPay's HTTPS 3DS page POSTs back, Chrome blocks the cross-scheme form
# as Mixed Content and the user sees "your data isn't protected" before
# they can return. Setting `web.base.url.freeze=True` also stops Odoo
# from rewriting the value on the first authenticated request.
#
# This is idempotent (ON CONFLICT DO UPDATE) and safe to run every deploy.
# We rollout-restart Odoo afterwards because `ir.config_parameter` is
# cached in-process; SQL upserts don't invalidate it.
init_odoo_system_params() {
    local slug="$1"
    local namespace="dev-sandbox-${slug}"
    local pg_pod="${slug}-odoo-dev-sandbox-postgres-0"
    local odoo_deploy="${slug}-odoo-dev-sandbox-odoo"
    local db_name="woow_${slug}"
    local hostname="${slug}.woowtech.io"

    echo ""
    echo -e "${BLUE}Initializing Odoo system parameters (web.base.url=https://${hostname})...${NC}"

    kubectl wait --for=condition=ready "pod/${pg_pod}" -n "$namespace" --timeout=120s

    kubectl exec -n "$namespace" "$pg_pod" -- psql -U odoo -d "$db_name" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES
  ('web.base.url', 'https://${hostname}', 1, NOW(), 1, NOW()),
  ('web.base.url.freeze', 'True', 1, NOW(), 1, NOW())
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_date = NOW(), write_uid = 1;
SQL

    echo -e "${BLUE}Restarting Odoo to invalidate ir.config_parameter cache...${NC}"
    kubectl rollout restart deployment -n "$namespace" "$odoo_deploy"
    kubectl rollout status deployment -n "$namespace" "$odoo_deploy" --timeout=180s
}

# Check tea login (not needed for --image override)
if [ -z "$IMAGE_OVERRIDE" ]; then
    if ! tea login list &>/dev/null; then
        echo -e "${RED}Error: No tea login configured for git-prod.woowtech.io${NC}" >&2
        echo -e "Run: tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>" >&2
        exit 1
    fi
fi

# --- Resolve addon ref ---
cd "$PROJECT_ROOT"

if [ -z "$ADDON_REF" ]; then
    ADDON_REF=$(git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD)
fi

# --- Resolve git tag and docker tag ---
if [ -n "$GIT_TAG_OVERRIDE" ]; then
    GIT_TAG="$GIT_TAG_OVERRIDE"
else
    GIT_TAG="stg-$(date -u +'%Y%m%d.%H%M')"
fi

# Docker image tag = git tag with stg- prefix stripped
DOCKER_TAG="${GIT_TAG#stg-}"
FULL_IMAGE="${REGISTRY_HOST}/${IMAGE_PATH}:${DOCKER_TAG}"

# --- Handle --image override (skip everything, go straight to deploy) ---
if [ -n "$IMAGE_OVERRIDE" ]; then
    FULL_IMAGE="$IMAGE_OVERRIDE"
    echo -e "${BLUE}Using provided image: ${FULL_IMAGE}${NC}"
    # Jump to deploy
    CREATE_ARGS=(--branch "$SLUG" --image "$FULL_IMAGE" --mode stg)
    if [ "$INGRESS" = true ]; then
        CREATE_ARGS+=(--ingress)
    fi
    bash "${PROJECT_ROOT}/.claude/skills/k8s-dev-sandbox/scripts/create.sh" "${CREATE_ARGS[@]}"
    init_odoo_system_params "$SLUG"
    exit 0
fi

# --- Display configuration ---
echo ""
echo -e "${BLUE}Deploy Staging Configuration:${NC}"
echo -e "  ${BLUE}Addon Ref:${NC}    $ADDON_REF"
echo -e "  ${BLUE}Git Tag:${NC}      $GIT_TAG"
echo -e "  ${BLUE}Docker Tag:${NC}   $DOCKER_TAG"
echo -e "  ${BLUE}Full Image:${NC}   $FULL_IMAGE"
echo -e "  ${BLUE}Slug:${NC}         $SLUG"
echo -e "  ${BLUE}Namespace:${NC}    dev-sandbox-${SLUG}"
echo -e "  ${BLUE}Trigger:${NC}      Gitea API create tag ${GIT_TAG}"
echo ""

# --- Trigger CI build via Gitea API ---
if [ "$SKIP_BUILD" = false ]; then
    echo -e "${BLUE}Triggering CI build via Gitea API...${NC}"

    # Get main branch latest commit SHA
    if ! BRANCH_RESP=$(tea api -r "${CI_REPO}" "repos/{owner}/{repo}/branches/main" 2>&1); then
        echo -e "${RED}Error: Failed to get main branch info${NC}" >&2
        echo "$BRANCH_RESP" >&2
        exit 1
    fi
    MAIN_SHA=$(echo "$BRANCH_RESP" | jq -r '.commit.id // empty')

    if [ -z "$MAIN_SHA" ]; then
        echo -e "${RED}Error: Failed to parse main branch SHA${NC}" >&2
        echo "$BRANCH_RESP" | jq . 2>/dev/null || echo "$BRANCH_RESP" >&2
        exit 1
    fi

    echo -e "  Main branch SHA: ${MAIN_SHA:0:12}"

    # Create tag on CI repo
    TAG_RESP=$(tea api -X POST -r "${CI_REPO}" "repos/{owner}/{repo}/tags" \
        -f "tag_name=${GIT_TAG}" -f "target=${MAIN_SHA}" 2>&1) || true
    TAG_NAME=$(echo "$TAG_RESP" | jq -r '.name // empty' 2>/dev/null)

    if [ -z "$TAG_NAME" ]; then
        # Check if tag already exists (409 conflict)
        if echo "$TAG_RESP" | grep -qi "exist"; then
            echo -e "${YELLOW}Tag ${GIT_TAG} already exists, proceeding to poll CI status...${NC}"
        else
            echo -e "${RED}Error: Failed to create tag ${GIT_TAG}${NC}" >&2
            echo "$TAG_RESP" >&2
            exit 1
        fi
    else
        echo -e "${GREEN}CI build triggered: tag ${TAG_NAME} created${NC}"
    fi

    echo -e "  Monitor: ${GITEA_URL}/${CI_REPO}/actions"
else
    echo -e "${YELLOW}Skipping CI build (--skip-build)${NC}"
fi

# --- Poll CI status via Gitea API ---
echo ""
echo -e "${BLUE}Waiting for CI to complete...${NC}"
echo -e "  Git Tag: ${GIT_TAG}"
echo -e "  Timeout: ${POLL_TIMEOUT}s (poll every ${POLL_INTERVAL}s)"
echo ""

MAX_ATTEMPTS=$(( POLL_TIMEOUT / POLL_INTERVAL ))
CI_DONE=false

for i in $(seq 1 "$MAX_ATTEMPTS"); do
    # Query action tasks, filter by head_branch matching our tag
    TASKS_RESP=$(tea api -r "${CI_REPO}" "repos/{owner}/{repo}/actions/tasks?limit=10" 2>/dev/null || echo "{}")

    # Extract statuses of all tasks matching our tag
    STATUSES=$(echo "$TASKS_RESP" | jq -r \
        ".workflow_runs // [] | map(select(.head_branch == \"${GIT_TAG}\")) | .[].status" 2>/dev/null)

    ELAPSED=$(( i * POLL_INTERVAL ))

    if [ -z "$STATUSES" ]; then
        echo -e "  [${ELAPSED}s/${POLL_TIMEOUT}s] CI run not found yet, waiting ${POLL_INTERVAL}s..."
        sleep "$POLL_INTERVAL"
        continue
    fi

    # Check for failure/cancelled in any task
    if echo "$STATUSES" | grep -qE '^(failure|cancelled)$'; then
        FAILED_STATUS=$(echo "$STATUSES" | grep -E '^(failure|cancelled)$' | head -1)
        echo -e "${RED}CI failed (status: ${FAILED_STATUS})${NC}" >&2
        echo -e "Check: ${GITEA_URL}/${CI_REPO}/actions" >&2
        exit 1
    fi

    # Check if all tasks are successful
    NON_SUCCESS=$(echo "$STATUSES" | grep -v '^success$' || true)
    if [ -z "$NON_SUCCESS" ]; then
        echo -e "${GREEN}CI completed successfully! (attempt ${i}/${MAX_ATTEMPTS})${NC}"
        CI_DONE=true
        break
    fi

    # Still in progress
    CURRENT=$(echo "$STATUSES" | head -1)
    echo -e "  [${ELAPSED}s/${POLL_TIMEOUT}s] CI status: ${CURRENT}, waiting ${POLL_INTERVAL}s..."
    sleep "$POLL_INTERVAL"
done

if [ "$CI_DONE" = false ]; then
    echo -e "${RED}Timeout: CI not completed after ${POLL_TIMEOUT}s${NC}" >&2
    echo -e "Check CI status: ${GITEA_URL}/${CI_REPO}/actions" >&2
    exit 1
fi

# --- Deploy to sandbox ---
echo ""
echo -e "${BLUE}Deploying to sandbox...${NC}"

CREATE_ARGS=(--branch "$SLUG" --image "$FULL_IMAGE" --mode stg)
if [ "$INGRESS" = true ]; then
    CREATE_ARGS+=(--ingress)
fi
bash "${PROJECT_ROOT}/.claude/skills/k8s-dev-sandbox/scripts/create.sh" "${CREATE_ARGS[@]}"

# --- Initialize Odoo system parameters (web.base.url + freeze) ---
init_odoo_system_params "$SLUG"

# --- Verify ---
NAMESPACE="dev-sandbox-${SLUG}"
echo ""
echo -e "${BLUE}Verifying deployment...${NC}"
kubectl get pods -n "$NAMESPACE"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Staging deployment complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "  Namespace: ${NAMESPACE}"
echo -e "  Image:     ${FULL_IMAGE}"
echo -e "  Addon Ref: ${ADDON_REF}"
echo ""
