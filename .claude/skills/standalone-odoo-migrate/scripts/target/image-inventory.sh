#!/usr/bin/env bash
# image-inventory.sh — 盤點目標 odoo image 內有哪些模組（版本＋內容 hash）與 pip（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent。只能指向白名單 ns（建議 dev-sandbox-*；paas-ws-stg-* 也可），不要用 prod 租戶 ns。
#
# 用法：
#   image-inventory.sh --context <ctx> --ns <ns> --image <registry/repo@sha256:…> [--pull-secret <name>]
#                      [--dirs /all-addons,/opt/woow/addons] [--out <file>]
#
# 做什麼：在 <ns> 起一顆一次性 pod（command sleep，不連 DB、不需要網路），exec `python3 - image`
#   （odoo_inspect.py）後刪除 pod。image 必須是官方 odoo:18.0 lineage（cloud-odoo／paas-odoo-official）。
# 輸出：JSON 檔（預設 ./image-inventory-<digest 前 12 碼>.json）；stdout 最後一行印檔名。
# exit：0 成功、2 參數錯誤、3 pod 起不來或 exec 失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSPECT="$SCRIPT_DIR/../odoo_inspect.py"
CTX="" NS="" IMAGE="" PULL_SECRET="" DIRS="" OUT=""

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }

while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --image) IMAGE=$2; shift 2 ;;
    --pull-secret) PULL_SECRET=$2; shift 2 ;;
    --dirs) DIRS=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS && -n $IMAGE ]] || { usage >&2; die 2 "--context, --ns and --image are required"; }
command -v jq >/dev/null || die 2 "jq is required"
DIGEST=""
[[ $IMAGE == *@sha256:* ]] && DIGEST=${IMAGE##*@}
OUT=${OUT:-./image-inventory-${DIGEST:7:12}.json}
[[ $OUT != ./image-inventory-.json ]] || OUT=./image-inventory-$(date -u +%Y%m%dT%H%M%SZ).json
POD=odoo-image-inventory-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
K=(kubectl --context "$CTX" -n "$NS")

cleanup() { "${K[@]}" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

pull_secrets='[]'
[[ -z $PULL_SECRET ]] || pull_secrets=$(jq -nc --arg s "$PULL_SECRET" '[{name: $s}]')
jq -n --arg pod "$POD" --arg image "$IMAGE" --argjson ps "$pull_secrets" '{
  apiVersion: "v1", kind: "Pod",
  metadata: {name: $pod, labels: {"app.kubernetes.io/part-of": "standalone-odoo-migrate",
                                  "app.kubernetes.io/component": "image-inventory"}},
  spec: {
    restartPolicy: "Never", automountServiceAccountToken: false, imagePullSecrets: $ps,
    activeDeadlineSeconds: 900,
    securityContext: {runAsUser: 100, runAsGroup: 101, runAsNonRoot: true, seccompProfile: {type: "RuntimeDefault"}},
    containers: [{name: "inspect", image: $image, command: ["sleep", "900"],
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}},
      resources: {requests: {cpu: "100m", memory: "256Mi"}, limits: {cpu: "1", memory: "1Gi"}}}]
  }}' | "${K[@]}" apply -f - >/dev/null
"${K[@]}" wait --for=condition=Ready "pod/$POD" --timeout=300s >/dev/null \
  || { "${K[@]}" describe pod "$POD" | tail -15 >&2; die 3 "pod $POD not ready"; }

raw=$(cat "$INSPECT" | "${K[@]}" exec -i "$POD" -- python3 - image ${DIRS:+--dirs "$DIRS"} ${DIGEST:+--digest "$DIGEST"} 2>&1) \
  || { printf '%s\n' "$raw" | tail -20 >&2; die 3 "exec failed"; }
json=$(printf '%s\n' "$raw" | sed -n '/^---ODOO-INSPECT-JSON-START---$/,/^---ODOO-INSPECT-JSON-END---$/p' | sed '1d;$d')
jq -e . >/dev/null <<<"$json" 2>/dev/null || { printf '%s\n' "$raw" | tail -20 >&2; die 3 "cannot parse odoo_inspect output"; }
jq --arg image "$IMAGE" '. + {image: $image, kind: "odoo-image-inventory", schema_version: 1}' <<<"$json" > "$OUT"
jq -r '"image-inventory: core_date \(.core_date), \(.addons.modules | length) modules (\([.addons.modules[] | select(.digest)] | length) non-core), \(.pip.distributions | length) pip dists"' "$OUT" >&2
echo "$OUT"
