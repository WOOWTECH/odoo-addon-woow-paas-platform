#!/bin/bash
# Cloudflare Tunnel deploy script
# Applies Helm chart with kustomize patches (protocol=http2, relaxed liveness probe)
#
# Usage:
#   ./extra/cloudflare/post-renderer/render.sh [install|upgrade|template]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHART_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VALUES_FILE="$CHART_DIR/cloudflare-values.yml"
NAMESPACE="cloudflare-system"
RELEASE_NAME="cloudflare-tunnel"

if [[ ! -f "$VALUES_FILE" ]]; then
  echo "❌ $VALUES_FILE not found. Copy from cloudflare-values.example.yml and fill in your token."
  exit 1
fi

ACTION="${1:-upgrade}"

echo "Rendering Helm template..."
helm template "$RELEASE_NAME" cloudflare/cloudflare-tunnel-remote \
  --namespace "$NAMESPACE" \
  --values "$VALUES_FILE" \
  > "$SCRIPT_DIR/all.yaml"

echo "Applying kustomize patches..."
kubectl kustomize "$SCRIPT_DIR" > "$SCRIPT_DIR/rendered.yaml"

case "$ACTION" in
  template)
    cat "$SCRIPT_DIR/rendered.yaml"
    ;;
  install|upgrade)
    echo "Applying to cluster (namespace: $NAMESPACE)..."
    kubectl create namespace "$NAMESPACE" 2>/dev/null || true
    kubectl apply -n "$NAMESPACE" -f "$SCRIPT_DIR/rendered.yaml"
    echo "✅ Cloudflare Tunnel deployed"
    kubectl rollout status deployment -n "$NAMESPACE" -l pod=cloudflared --timeout=90s
    ;;
  *)
    echo "Usage: $0 [install|upgrade|template]"
    exit 1
    ;;
esac

# Clean up temp files
rm -f "$SCRIPT_DIR/all.yaml" "$SCRIPT_DIR/rendered.yaml"
