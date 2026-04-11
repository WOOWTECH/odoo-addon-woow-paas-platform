#!/usr/bin/env bash
#
# Build & Publish paas-operator
# 建立 operator-v{YYYYMMDD.HHMM} git tag 觸發 CI 建置 paas-operator image。
# 版號使用 UTC 時間戳，永遠遞增，不需手動管理。
#
# Usage:
#   .claude/skills/build-operator/scripts/build-operator.sh [OPTIONS]
#
# Options:
#   --skip-push       Create tag locally only, don't push
#   --dry-run         Show what would happen without executing
#   -h, --help        Show usage
#
# Examples:
#   .claude/skills/build-operator/scripts/build-operator.sh
#   .claude/skills/build-operator/scripts/build-operator.sh --dry-run
#   .claude/skills/build-operator/scripts/build-operator.sh --skip-push

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

# --- Defaults ---
SKIP_PUSH=false
DRY_RUN=false

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-push)
      SKIP_PUSH=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h|--help)
      head -20 "$0" | tail -18
      exit 0
      ;;
    -*)
      echo -e "${RED}Unknown option: $1${NC}"
      exit 1
      ;;
    *)
      echo -e "${RED}Unknown argument: $1${NC}"
      exit 1
      ;;
  esac
done

cd "$PROJECT_ROOT"

# --- Generate timestamp version ---
VERSION=$(date -u +'%Y%m%d.%H%M')
TAG="operator-v${VERSION}"
BRANCH=$(git branch --show-current)

# --- Check tag doesn't already exist ---
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo -e "${YELLOW}Tag ${TAG} already exists, waiting 1 minute...${NC}"
  sleep 60
  VERSION=$(date -u +'%Y%m%d.%H%M')
  TAG="operator-v${VERSION}"
fi

# --- Check working tree clean (for operator files) ---
if [[ -n $(git status --porcelain extra/paas-operator/ 2>/dev/null) ]]; then
  echo -e "${YELLOW}Warning: Uncommitted changes in extra/paas-operator/:${NC}"
  git status --short extra/paas-operator/
fi

# --- Check unpushed commits ---
AHEAD=$(git rev-list --count @{upstream}..HEAD 2>/dev/null || echo "0")
if [[ "$AHEAD" -gt 0 ]]; then
  echo -e "${YELLOW}Warning: ${AHEAD} unpushed commit(s) on ${BRANCH}:${NC}"
  git log --oneline "@{upstream}..HEAD"
  echo ""
fi

# --- Display configuration ---
echo ""
echo -e "${GREEN}Build Operator Configuration:${NC}"
echo "  Version:     ${VERSION}"
echo "  Git Tag:     ${TAG}"
echo "  Image Tag:   ${VERSION} + latest"
echo "  Branch:      ${BRANCH}"
if [[ "$SKIP_PUSH" == "true" ]]; then
  echo "  Push:        SKIPPED (--skip-push)"
fi
if [[ "$DRY_RUN" == "true" ]]; then
  echo -e "  Mode:        ${YELLOW}DRY RUN${NC}"
fi
echo ""

# --- Dry run stops here ---
if [[ "$DRY_RUN" == "true" ]]; then
  echo -e "${YELLOW}Dry run complete. No changes made.${NC}"
  exit 0
fi

# --- Create tag ---
echo -e "${BLUE}Creating tag: ${TAG}${NC}"
git tag "$TAG"

# --- Push ---
if [[ "$SKIP_PUSH" == "true" ]]; then
  echo -e "${YELLOW}Skipping push (--skip-push)${NC}"
  echo -e "${GREEN}Tag ${TAG} created locally.${NC}"
else
  echo -e "${BLUE}Pushing...${NC}"
  # Push unpushed commits if any
  if [[ "$AHEAD" -gt 0 ]]; then
    echo -e "${BLUE}Pushing ${AHEAD} unpushed commit(s)...${NC}"
    git push origin "$BRANCH"
  fi
  git push origin "$TAG"
  echo -e "${GREEN}Tag ${TAG} pushed. CI build triggered.${NC}"
  echo ""
  echo "Monitor CI: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/actions"
fi

echo ""
echo -e "${GREEN}Done!${NC}"
echo "  Tag:   ${TAG}"
echo "  Image: paas-operator:${VERSION}"
