#!/usr/bin/env bash
# fake-target.sh — 在 dev-sandbox-* 直接裝 woow-odoo chart 當搬遷目標（standalone-odoo-migrate，#1604）
#
# 模擬「平台買好服務」的狀態：tenant profile、首裝 bootstrap 跑 -i base 建空庫（restore.sh 要 --replace-db 取代它）。
# 不經平台（沒有 cloud_service／helm_values／Redeploy）；平台端流程在 stg 另外驗。
#
# 用法：
#   fake-target.sh up   <slug> --context <ctx> [--release migtest] [--image <registry/repo:tag@sha256:…>]
#                       [--pull-secret-from <ns>/<secret>] [--chart-ref origin/main] [--addons a,b]
#   fake-target.sh down <slug> --context <ctx> [--release migtest]
#
# ns 固定 dev-sandbox-<slug>（要先存在、annotation standalone-odoo-migrate/role=fake-target）。
# --image 預設＝範本 4 的 cloud-odoo；JCR image 需要 pull secret：從 --pull-secret-from（預設
#   paas-ws-stg-4360e1f5/paas-registry）複製一份 paas-registry 進來，內容不落地、不印出。
# chart 取自本機 woow-paas-charts checkout 的 --chart-ref（git archive，不動工作目錄）。
set -euo pipefail

CHARTS_REPO=${CHARTS_REPO:-$HOME/woow-repos/paas/woow-paas-charts}
CMD=${1:-}; shift || true
SLUG=${1:-}; shift || true
CTX="" RELEASE=migtest CHART_REF=origin/main ADDONS=""
IMAGE="jcr-prod.woowtech.io/woow-paas-docker-local/cloud-odoo:20261010.1604@sha256:c0c849aac39773ba13dc81ed781da8d7622780a296c2d94affdafe164f5743dc"
PULL_FROM=paas-ws-stg-4360e1f5/paas-registry
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --image) IMAGE=$2; shift 2 ;;
    --pull-secret-from) PULL_FROM=$2; shift 2 ;;
    --chart-ref) CHART_REF=$2; shift 2 ;;
    --addons) ADDONS=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
[[ $CMD =~ ^(up|down)$ && $SLUG =~ ^[a-z0-9-]+$ && -n $CTX ]] || { usage >&2; exit 2; }
NS=dev-sandbox-$SLUG
K=(kubectl --context "$CTX" -n "$NS")
log() { echo "[fake-target] $*" >&2; }
[[ $("${K[@]}" get ns "$NS" -o jsonpath='{.metadata.annotations.standalone-odoo-migrate/role}' 2>/dev/null) == fake-target ]] \
  || { echo "refuse: $NS missing or not annotated standalone-odoo-migrate/role=fake-target" >&2; exit 2; }

up() {
  [[ $IMAGE =~ ^([^/]+)/(.+):([^@]+)@(sha256:[a-f0-9]{64})$ ]] || { echo "--image must be registry/repo:tag@sha256:…" >&2; exit 2; }
  local registry=${BASH_REMATCH[1]} repo=${BASH_REMATCH[2]} tag=${BASH_REMATCH[3]} digest=${BASH_REMATCH[4]}
  if ! "${K[@]}" get secret paas-registry >/dev/null 2>&1; then
    # The NotFound guard makes this create-only. Client-side apply would copy
    # the complete docker credential into last-applied-configuration as a
    # second readable Secret value.
    kubectl --context "$CTX" -n "${PULL_FROM%%/*}" get secret "${PULL_FROM#*/}" -o json \
      | jq '{apiVersion, kind, type, data, metadata: {name: "paas-registry"}}' | "${K[@]}" create -f - >/dev/null
    log "copied pull secret from $PULL_FROM"
  fi
  local tmp chart values addons_json
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' RETURN
  git -C "$CHARTS_REPO" fetch -q origin
  git -C "$CHARTS_REPO" archive "$CHART_REF" charts/woow-odoo | tar -x -C "$tmp"
  chart=$tmp/charts/woow-odoo
  addons_json=$(jq -cn --arg a "$ADDONS" '$a | split(",") | map(select(length > 0))')
  values=$tmp/values.yaml
  cat > "$values" <<EOF
profile: tenant
imagePullSecrets: [{name: paas-registry}]
image: {registry: $registry, repository: $repo, tag: "$tag", digest: "$digest"}
conf:
  adminEmail: admin@example.invalid
  adminPassword: "$(openssl rand -hex 12)"
  workers: 2
  maxCronThreads: 1
addons: {selected: $addons_json}
postgres: {database: odoo}
persistence: {size: 5Gi}
backup: {enabled: false}
restore: {enabled: false}
identity: {cloudServiceId: dev, workspaceId: dev}
EOF
  log "helm upgrade --install $RELEASE (woow-odoo $(grep '^version:' "$chart/Chart.yaml" | cut -d' ' -f2) @ $CHART_REF) image $tag"
  helm --kube-context "$CTX" -n "$NS" upgrade --install "$RELEASE" "$chart" -f "$values" --wait --timeout 15m >/dev/null
  "${K[@]}" get pods
}

down() {
  helm --kube-context "$CTX" -n "$NS" uninstall "$RELEASE" --wait >/dev/null 2>&1 || true
  "${K[@]}" delete pvc -l "app.kubernetes.io/instance=$RELEASE" --ignore-not-found --wait=false >/dev/null
  log "release $RELEASE removed (ns $NS kept; delete it with kubectl when done)"
}

"$CMD"
