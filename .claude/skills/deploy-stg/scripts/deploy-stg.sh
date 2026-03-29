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
#   --skip-build          Skip CI build, poll registry for existing image
#   --tag <tag>           Override image tag (default: stg-YYYYMMDD.HHMM)
#   --timeout <seconds>   Build wait timeout (default: 900 = 15 min)
#   --interval <seconds>  Poll interval (default: 30)
#   -h, --help            Show usage
#
# Environment:
#   JCR_USER      (optional) JFrog JCR username for registry auth
#   JCR_PASS      (optional) JFrog JCR password for registry auth
#
# Examples:
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh v18.0.1.0.5
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh --slug stg-test
#   .claude/skills/deploy-stg/scripts/deploy-stg.sh --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:stg-20260329.1400

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
CI_REPO_PATH="${CI_REPO_PATH:-$(cd "${PROJECT_ROOT}/../../woow-paas-repo/paas-odoo-ci" 2>/dev/null && pwd || echo "")}"
REGISTRY_URL="https://jcr-prod.woowtech.io"
IMAGE_PATH="woow-paas-docker-local/paas-odoo-stg"
REGISTRY_HOST="jcr-prod.woowtech.io"

# --- Defaults ---
SLUG="stg"
ADDON_REF=""
IMAGE_OVERRIDE=""
IMAGE_TAG_OVERRIDE=""
SKIP_BUILD=false
POLL_TIMEOUT=900
POLL_INTERVAL=30

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
        --tag)
            IMAGE_TAG_OVERRIDE="$2"
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
        -h|--help)
            head -25 "$0" | tail -23
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

for cmd in curl jq kubectl helm git; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}Missing required tool: ${cmd}${NC}" >&2
        exit 1
    fi
done

# Check CI repo exists locally
if [ -z "$IMAGE_OVERRIDE" ] && [ "$SKIP_BUILD" = false ]; then
    if [ ! -d "$CI_REPO_PATH/.git" ]; then
        echo -e "${RED}Error: CI repo not found at ${CI_REPO_PATH}${NC}" >&2
        echo -e "Clone it: git clone ${GITEA_URL}/${CI_REPO}.git ${CI_REPO_PATH}" >&2
        exit 1
    fi
fi

# --- Resolve addon ref ---
cd "$PROJECT_ROOT"

if [ -z "$ADDON_REF" ]; then
    ADDON_REF=$(git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD)
fi

# --- Resolve image tag ---
if [ -n "$IMAGE_TAG_OVERRIDE" ]; then
    IMAGE_TAG="$IMAGE_TAG_OVERRIDE"
else
    IMAGE_TAG="stg-$(date -u +'%Y%m%d.%H%M')"
fi

FULL_IMAGE="${REGISTRY_HOST}/${IMAGE_PATH}:${IMAGE_TAG}"

# --- Handle --image override (skip everything, go straight to deploy) ---
if [ -n "$IMAGE_OVERRIDE" ]; then
    FULL_IMAGE="$IMAGE_OVERRIDE"
    echo -e "${BLUE}Using provided image: ${FULL_IMAGE}${NC}"
    # Jump to deploy
    exec bash "${PROJECT_ROOT}/.claude/skills/k8s-dev-sandbox/scripts/create.sh" \
        --branch "$SLUG" \
        --image "$FULL_IMAGE"
fi

# --- Display configuration ---
echo ""
echo -e "${BLUE}Deploy Staging Configuration:${NC}"
echo -e "  ${BLUE}Addon Ref:${NC}  $ADDON_REF"
echo -e "  ${BLUE}Image Tag:${NC}  $IMAGE_TAG"
echo -e "  ${BLUE}Full Image:${NC} $FULL_IMAGE"
echo -e "  ${BLUE}Slug:${NC}       $SLUG"
echo -e "  ${BLUE}Namespace:${NC}  dev-sandbox-${SLUG}"
echo -e "  ${BLUE}CI Repo:${NC}    $CI_REPO_PATH"
echo -e "  ${BLUE}Trigger:${NC}   git tag ${IMAGE_TAG} + push"
echo ""

# --- Trigger CI build via git tag ---
if [ "$SKIP_BUILD" = false ]; then
    echo -e "${BLUE}Triggering CI build via git tag...${NC}"

    # Push tag to paas-odoo-ci repo (triggers stg-* workflow)
    git -C "$CI_REPO_PATH" fetch origin main --quiet
    git -C "$CI_REPO_PATH" tag -f "$IMAGE_TAG" origin/main
    git -C "$CI_REPO_PATH" push origin "$IMAGE_TAG" --force 2>/dev/null

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}CI build triggered: tag ${IMAGE_TAG} pushed${NC}"
        echo -e "  Monitor: ${GITEA_URL}/${CI_REPO}/actions"
    else
        echo -e "${RED}Failed to push tag ${IMAGE_TAG}${NC}" >&2
        exit 1
    fi
else
    echo -e "${YELLOW}Skipping CI build (--skip-build)${NC}"
fi

# --- Poll registry for image ---
echo ""
echo -e "${BLUE}Waiting for image to be available in registry...${NC}"
echo -e "  Image: ${IMAGE_PATH}:${IMAGE_TAG}"
echo -e "  Timeout: ${POLL_TIMEOUT}s (poll every ${POLL_INTERVAL}s)"
echo ""

MAX_ATTEMPTS=$(( POLL_TIMEOUT / POLL_INTERVAL ))
IMAGE_READY=false

# Build registry auth header
REGISTRY_AUTH_ARGS=""
if [ -n "${JCR_USER:-}" ] && [ -n "${JCR_PASS:-}" ]; then
    REGISTRY_AUTH_ARGS="-u ${JCR_USER}:${JCR_PASS}"
fi

for i in $(seq 1 "$MAX_ATTEMPTS"); do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
        ${REGISTRY_AUTH_ARGS} \
        "${REGISTRY_URL}/v2/${IMAGE_PATH}/manifests/${IMAGE_TAG}" \
        -H "Accept: application/vnd.docker.distribution.manifest.v2+json" 2>/dev/null || echo "000")

    if [ "$HTTP" = "200" ]; then
        echo -e "${GREEN}Image is ready! (attempt ${i}/${MAX_ATTEMPTS})${NC}"
        IMAGE_READY=true
        break
    fi

    ELAPSED=$(( i * POLL_INTERVAL ))
    echo -e "  [${ELAPSED}s/${POLL_TIMEOUT}s] Not ready (HTTP ${HTTP}), waiting ${POLL_INTERVAL}s..."
    sleep "$POLL_INTERVAL"
done

if [ "$IMAGE_READY" = false ]; then
    echo -e "${RED}Timeout: image not found after ${POLL_TIMEOUT}s${NC}" >&2
    echo -e "Check CI status: ${GITEA_URL}/${CI_REPO}/actions" >&2
    exit 1
fi

# --- Deploy to sandbox ---
echo ""
echo -e "${BLUE}Deploying to sandbox...${NC}"

bash "${PROJECT_ROOT}/.claude/skills/k8s-dev-sandbox/scripts/create.sh" \
    --branch "$SLUG" \
    --image "$FULL_IMAGE"

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
