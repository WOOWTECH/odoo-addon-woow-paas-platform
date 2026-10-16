#!/usr/bin/env bash
# inventory.sh — 來源 standalone Odoo 站的唯讀盤點（standalone-odoo-migrate skill，#1604）
#
# ⛔ 執行者＝woow-machines-operator（WMO）。來源 ns（例 harumi-odoo）不在 woow_paas_platform 的
#    kubectl 白名單，本 repo agent 不得執行本腳本；把本檔與 ../odoo_inspect.py 一起交給 WMO。
#
# 用法：
#   inventory.sh --context <ctx> --ns <ns> [--odoo-workload <deploy/name|sts/name>] [--db <name>] [--out <file>]
#
# 做什麼（全部唯讀）：
#   1. get deploy／sts／pod／pvc／svc／cronjob／configmap（只取名稱）——**不讀 Secret**。
#   2. 找出 odoo 工作負載（容器開 8069）與 postgres（容器開 5432），抽 image、resources、PVC、
#      執行時 pip install 指令片段。
#   3. 一次 exec：odoo 容器內跑 `python3 - source`（odoo_inspect.py；DB 走 READ ONLY 交易）。
#   4. helm list（有 helm 才跑；不取 values，values 內可能有密碼）。
# 輸出：JSON 檔（預設 ./inventory-<ns>-<UTC>.json）；stderr 印摘要。密碼、token、SMTP 帳號、email 不會出現在輸出。
# exit：0 成功、2 參數或探索失敗、3 exec 失敗或輸出解析失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSPECT="$SCRIPT_DIR/../odoo_inspect.py"
CTX="" NS="" WORKLOAD="" DB="" OUT=""

usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --odoo-workload) WORKLOAD=$2; shift 2 ;;
    --db) DB=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS ]] || { usage >&2; die 2 "--context and --ns are required"; }
command -v jq >/dev/null || die 2 "jq is required"
[[ -r $INSPECT ]] || die 2 "missing $INSPECT (copy odoo_inspect.py next to source/)"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT=${OUT:-./inventory-$NS-$TS.json}
K=(kubectl --context "$CTX" -n "$NS")

workloads=$("${K[@]}" get deploy,sts -o json)
discover_odoo "$workloads" "$WORKLOAD"

# k8s 中繼資料：只取形狀與 image，不取 env 值（env 可能是字面密碼）
k8s=$(jq --arg odoo "$WL_NAME" '
  # 執行時 pip install 片段（標明在 initContainer 或主容器：initContainer 裝的套件主容器看不到，除非裝進共用 volume）
  def pipcmds: [((.spec.template.spec.initContainers // []) | map(. + {init: true}))
      + (.spec.template.spec.containers | map(. + {init: false})) | .[]
    | {container: .name, init} + ({cmd: ((.command // []) + (.args // [])) | join(" ")})
    | .cmd as $c | ($c | [scan("pip3? install[^;&|>\n]*")] | map(sub(" +[0-9]*$"; ""))) as $m
    | select($m | length > 0) | {container, init, commands: $m}];
  {
    workloads: [.items[] | {
      kind, name: .metadata.name, replicas: .spec.replicas,
      role: (if .metadata.name == $odoo then "odoo"
             elif ([.spec.template.spec.containers[].ports[]?.containerPort] | index(5432)) then "postgres"
             else "other" end),
      containers: [.spec.template.spec.containers[] | {name, image, ports: [.ports[]?.containerPort], resources,
                   mounts: [.volumeMounts[]? | {name, mountPath, subPath}]}],
      initContainers: [.spec.template.spec.initContainers[]? | {name, image}],
      volumes: [.spec.template.spec.volumes[]? | {name, pvc: .persistentVolumeClaim.claimName,
                configMap: .configMap.name, emptyDir: (.emptyDir != null)}],
      pip_install_commands: pipcmds
    }]
  }' <<<"$workloads")

pods=$("${K[@]}" get pod -o json | jq '[.items[] | {name: .metadata.name, phase: .status.phase,
  node: .spec.nodeName, images: [.status.containerStatuses[]? | {name, image, imageID, restartCount,
  lastTerminated: .lastState.terminated.reason}]}]')
pvcs=$("${K[@]}" get pvc -o json | jq '[.items[] | {name: .metadata.name, storageClass: .spec.storageClassName,
  requested: .spec.resources.requests.storage, capacity: .status.capacity.storage, phase: .status.phase,
  volume: .spec.volumeName}]')
svcs=$("${K[@]}" get svc -o json | jq '[.items[] | {name: .metadata.name, ports: [.spec.ports[] | {name, port, targetPort}]}]')
cronjobs=$("${K[@]}" get cronjob -o json | jq '[.items[] | {name: .metadata.name, schedule: .spec.schedule, suspend: .spec.suspend}]')
configmaps=$("${K[@]}" get configmap -o json | jq '[.items[].metadata.name]')
helm_releases=null
if command -v helm >/dev/null; then
  helm_releases=$(helm --kube-context "$CTX" -n "$NS" list -o json 2>/dev/null \
    | jq '[.[] | {name, chart, revision, updated, status}]' || echo null)
fi

echo "exec: $WORKLOAD -c $CONTAINER — odoo_inspect.py source (read-only)" >&2
pod_json=$(inspect_in_pod K source ${DB:+--db "$DB"})

# 各段先落暫存檔再合併：inspect JSON（pip import_map 等）會超過 argv 上限
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
printf '%s\n' "$pod_json" > "$TMP/pod.json"
jq -n --argjson k8s "$k8s" --argjson pods "$pods" --argjson pvcs "$pvcs" --argjson svcs "$svcs" \
  --argjson cronjobs "$cronjobs" --argjson cms "$configmaps" --argjson helm "$helm_releases" \
  '$k8s + {pods: $pods, pvcs: $pvcs, services: $svcs, cronjobs: $cronjobs, configmaps: $cms, helm_releases: $helm}' \
  > "$TMP/k8s.json"
jq -n --arg ts "$TS" --arg ctx "$CTX" --arg ns "$NS" --arg wl "$WORKLOAD" --arg c "$CONTAINER" \
  --slurpfile k8s "$TMP/k8s.json" --slurpfile pod "$TMP/pod.json" '{
    schema_version: 1, kind: "standalone-odoo-inventory", generated_at: $ts,
    source: {context: $ctx, namespace: $ns, odoo_workload: $wl, container: $c},
    k8s: $k8s[0], inspect: $pod[0]
  }' > "$OUT"

jq -r '.inspect as $i | [
  "inventory: \(input_filename)",
  "  core_date        \($i.core_date)    PG \($i.db.server_version)    DB \($i.db.name) \($i.db.size_bytes / 1048576 | floor) MiB  \($i.db.encoding)/\($i.db.datcollate)",
  "  modules          \([$i.db.modules[] | select(.state == "installed")] | length) installed; states \($i.db.module_states)",
  "  non-core addons  \([$i.addons.modules | to_entries[] | select(.value.digest) | .key] | join(", "))",
  "  filestore        \($i.filestore.files) files, \(($i.filestore.bytes // 0) / 1048576 | floor) MiB, missing referenced \($i.filestore.missing_referenced)",
  "  extensions       \($i.db.extensions | keys | join(", "))  vector columns \($i.db.vector_columns | length)",
  "  users            internal \($i.db.users.internal_active), portal \($i.db.users.portal_active)",
  "  pos              open sessions \($i.db.pos_sessions_open // "-"), draft orders \($i.db.pos_orders_draft // "-")\(
      ($i.db.pos_sessions_idle_opening // []) as $idle
      | if ($idle | length) > 0 then "; idle opening_control \($idle | length) (\([$idle[] | "\(.id) \(.name)"] | join(", ")); 0 orders, OK to freeze)" else "" end)",
  "  runtime pip      \([.k8s.workloads[].pip_install_commands[] | "\(if .init then "init" else "main" end):\(.container) \(.commands | join("; "))"] | join(" | "))"
] | .[]' "$OUT" >&2
echo "$OUT"
