#!/usr/bin/env bash
#
# Build the development Docker image for CI/CD sandbox deployments.
#
# Usage:
#   .claude/skills/k8s-dev-sandbox/scripts/build.sh [--registry REGISTRY] [--push]
#
# Options:
#   --registry REGISTRY  Registry prefix (e.g., registry.example.com/)
#   --push               Push the image after building
#
# The image is tagged as: {registry}woow-odoo-dev:{branch-slug}-{commit-short}

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

# --- Defaults ---
REGISTRY=""
PUSH=false

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --registry)
            REGISTRY="$2"
            shift 2
            ;;
        --push)
            PUSH=true
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Usage: $0 [--registry REGISTRY] [--push]" >&2
            exit 1
            ;;
    esac
done

# --- Resolve branch slug ---
BRANCH=$(git -C "${PROJECT_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)
if [[ -z "${BRANCH}" ]]; then
    echo "Error: Unable to determine current git branch." >&2
    exit 1
fi

# Convert branch name to a URL/DNS-safe slug:
#   - lowercase
#   - replace non-alphanumeric characters with -
#   - collapse consecutive dashes
#   - trim leading/trailing dashes
#   - truncate to 40 characters
BRANCH_SLUG=$(echo "${BRANCH}" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]/-/g' \
    | sed 's/-\{2,\}/-/g' \
    | sed 's/^-//;s/-$//' \
    | cut -c1-40)

# --- Resolve short commit hash ---
COMMIT_SHORT=$(git -C "${PROJECT_ROOT}" rev-parse --short HEAD 2>/dev/null)
if [[ -z "${COMMIT_SHORT}" ]]; then
    echo "Error: Unable to determine current commit hash." >&2
    exit 1
fi

# --- Build image tag ---
IMAGE_NAME="${REGISTRY}woow-odoo-dev"
IMAGE_TAG="${BRANCH_SLUG}-${COMMIT_SHORT}"
FULL_TAG="${IMAGE_NAME}:${IMAGE_TAG}"

echo "==> Building image: ${FULL_TAG}"
docker build \
    -f "${PROJECT_ROOT}/Dockerfile.dev" \
    -t "${FULL_TAG}" \
    "${PROJECT_ROOT}"

# --- Optionally push ---
if [[ "${PUSH}" == true ]]; then
    echo "==> Pushing image: ${FULL_TAG}"
    docker push "${FULL_TAG}"
fi

echo ""
echo "Image built successfully."
echo "  Tag: ${FULL_TAG}"
