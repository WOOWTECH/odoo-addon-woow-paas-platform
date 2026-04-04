#!/usr/bin/env bash
#
# Build & Publish paas-operator
# 建立 operator-v{version} git tag 觸發 CI 建置 paas-operator image。
#
# Usage:
#   .claude/skills/build-operator/scripts/build-operator.sh [OPTIONS] [VERSION]
#
# Options:
#   --bump <level>    Auto bump version (patch|minor|major), updates Chart.yaml
#   --skip-push       Create tag locally only, don't push
#   --dry-run         Show what would happen without executing
#   -h, --help        Show usage
#
# Examples:
#   .claude/skills/build-operator/scripts/build-operator.sh
#   .claude/skills/build-operator/scripts/build-operator.sh 0.2.0
#   .claude/skills/build-operator/scripts/build-operator.sh --bump patch
#   .claude/skills/build-operator/scripts/build-operator.sh --bump minor --dry-run

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

# --- Chart.yaml path ---
CHART_YAML="${PROJECT_ROOT}/extra/paas-operator/helm/Chart.yaml"

# --- Defaults ---
VERSION_ARG=""
BUMP_LEVEL=""
SKIP_PUSH=false
DRY_RUN=false

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bump)
      BUMP_LEVEL="$2"
      if [[ ! "$BUMP_LEVEL" =~ ^(patch|minor|major)$ ]]; then
        echo -e "${RED}Error: --bump must be patch, minor, or major${NC}"
        exit 1
      fi
      shift 2
      ;;
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
      VERSION_ARG="$1"
      shift
      ;;
  esac
done

cd "$PROJECT_ROOT"

# --- Read current version from Chart.yaml ---
if [[ ! -f "$CHART_YAML" ]]; then
  echo -e "${RED}Error: Chart.yaml not found at ${CHART_YAML}${NC}"
  exit 1
fi

CURRENT_VERSION=$(grep '^version:' "$CHART_YAML" | head -1 | awk '{print $2}' | tr -d '"')
echo -e "${BLUE}Current Chart.yaml version: ${CURRENT_VERSION}${NC}"

# --- Determine target version ---
bump_version() {
  local version="$1"
  local level="$2"
  local major minor patch
  IFS='.' read -r major minor patch <<< "$version"

  case "$level" in
    major) echo "$((major + 1)).0.0" ;;
    minor) echo "${major}.$((minor + 1)).0" ;;
    patch) echo "${major}.${minor}.$((patch + 1))" ;;
  esac
}

if [[ -n "$BUMP_LEVEL" ]]; then
  VERSION=$(bump_version "$CURRENT_VERSION" "$BUMP_LEVEL")
  NEED_UPDATE_CHART=true
elif [[ -n "$VERSION_ARG" ]]; then
  VERSION="$VERSION_ARG"
  NEED_UPDATE_CHART=false
else
  VERSION="$CURRENT_VERSION"
  NEED_UPDATE_CHART=false
fi

TAG="operator-v${VERSION}"
BRANCH=$(git branch --show-current)

# --- Check tag doesn't already exist ---
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo -e "${RED}Error: Tag ${TAG} already exists${NC}"
  echo "Use --bump to create a new version, or specify a different version."
  exit 1
fi

# --- Check working tree clean (for operator files) ---
if [[ -n $(git status --porcelain extra/paas-operator/ 2>/dev/null) ]]; then
  echo -e "${YELLOW}Warning: Uncommitted changes in extra/paas-operator/:${NC}"
  git status --short extra/paas-operator/
  if [[ "$NEED_UPDATE_CHART" == "true" ]]; then
    echo -e "${RED}Error: Cannot bump version with dirty working tree.${NC}"
    echo "Please commit or stash your changes first."
    exit 1
  fi
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
if [[ "$NEED_UPDATE_CHART" == "true" ]]; then
  echo "  Bump:        ${CURRENT_VERSION} → ${VERSION} (${BUMP_LEVEL})"
fi
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

# --- Update Chart.yaml if bumping ---
if [[ "$NEED_UPDATE_CHART" == "true" ]]; then
  echo -e "${BLUE}Updating Chart.yaml version: ${CURRENT_VERSION} → ${VERSION}${NC}"
  sed -i.bak "s/^version: .*/version: ${VERSION}/" "$CHART_YAML"
  sed -i.bak "s/^appVersion: .*/appVersion: \"${VERSION}\"/" "$CHART_YAML"
  rm -f "${CHART_YAML}.bak"

  git add "$CHART_YAML"
  git commit -m "release: paas-operator ${VERSION}"
  echo -e "${GREEN}Committed Chart.yaml update${NC}"
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
  if [[ "$NEED_UPDATE_CHART" == "true" ]]; then
    git push origin "$BRANCH"
  fi
  # Push unpushed commits if any
  if [[ "$AHEAD" -gt 0 && "$NEED_UPDATE_CHART" != "true" ]]; then
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
