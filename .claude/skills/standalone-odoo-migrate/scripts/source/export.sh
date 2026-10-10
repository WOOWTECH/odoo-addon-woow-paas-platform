#!/usr/bin/env bash
# export.sh — 來源 standalone Odoo 站的搬遷匯出（standalone-odoo-migrate skill，#1604）
#
# ⛔ 執行者＝woow-machines-operator（WMO）；理由同 inventory.sh。交付時連同 lib.sh、../odoo_inspect.py、../fingerprint.sql。
#
# 用法：
#   export.sh --context <ctx> --ns <ns> --url http://<receiver>.<target-ns>.svc.cluster.local:8080/<set> \
#             --token-file <file> [--odoo-workload deploy/<name>] [--db <name>] [--live]
#
# 前置：目標端已開好 receiver（target/receive.sh open），--url 與 token 檔由目標端交來。
# 凍結（預設）：odoo 工作負載必須已 scale 0、沒有 pod——匯出的就是最終資料。
#   --live：不凍結（彩排用）。DB 指紋與 dump 仍是同一個 snapshot、彼此一致；filestore 可能多出匯出途中的新檔。
# 做什麼：起一顆 helper pod——image、env、volume 掛載都照抄 odoo 容器（掛載一律唯讀，env 裡的 secretKeyRef 由
#   kubelet 解析，本腳本不讀任何 Secret 值）——在 pod 內跑 `odoo_inspect.py export`：同一個 REPEATABLE READ snapshot
#   內採 DB 指紋＋pg_dump，filestore 逐檔 sha256＋tar，全部 HTTP PUT 給 receiver，每個檔都比對 receiver 回報的 sha256。
#   結束（含失敗）時刪除 helper pod 與上傳 token Secret。
# 輸出：stdout 印匯出摘要 JSON（manifest＋各檔 bytes／sha256），並存成 ./export-<ns>-<UTC>.json。
# exit：0 成功、2 參數或前置條件不符、3 helper pod 或匯出失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSPECT="$SCRIPT_DIR/../odoo_inspect.py"
SQL="$SCRIPT_DIR/../fingerprint.sql"
CTX="" NS="" URL="" TOKEN_FILE="" WORKLOAD="" DB="" LIVE=0
SECRET=standalone-odoo-migrate-upload

usage() { sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --url) URL=$2; shift 2 ;;
    --token-file) TOKEN_FILE=$2; shift 2 ;;
    --odoo-workload) WORKLOAD=$2; shift 2 ;;
    --db) DB=$2; shift 2 ;;
    --live) LIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS && -n $URL && -n $TOKEN_FILE ]] || { usage >&2; die 2 "--context, --ns, --url and --token-file are required"; }
[[ $URL =~ ^http://[a-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die 2 "--url must be http://<host>[:port]/<set>"
[[ -s $TOKEN_FILE ]] || die 2 "token file $TOKEN_FILE is empty or missing"
command -v jq >/dev/null || die 2 "jq is required"
[[ -r $INSPECT && -r $SQL ]] || die 2 "missing odoo_inspect.py or fingerprint.sql next to source/"
K=(kubectl --context "$CTX" -n "$NS")
TS=$(date -u +%Y%m%dT%H%M%SZ)
POD=standalone-odoo-export-$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')

workloads=$("${K[@]}" get deploy,sts -o json)
discover_odoo "$workloads" "$WORKLOAD"
wl=$(jq --arg n "$WL_NAME" '.items[] | select(.metadata.name == $n)' <<<"$workloads")
selector=$(jq -r '.spec.selector.matchLabels | to_entries | map("\(.key)=\(.value)") | join(",")' <<<"$wl")
running=$("${K[@]}" get pod -l "$selector" -o json | jq '[.items[] | select(.status.phase == "Running")]')
node_name=null
if (( LIVE )); then
  (( $(jq length <<<"$running") >= 1 )) || die 2 "--live needs a running odoo pod (selector $selector)"
  # RWO volume：helper 必須跟 odoo pod 在同一個 node 才掛得上
  node_name=$(jq '.[0].spec.nodeName' <<<"$running")
else
  [[ $(jq '.spec.replicas' <<<"$wl") == 0 && $(jq length <<<"$running") == 0 ]] \
    || die 2 "$WORKLOAD is not frozen (replicas must be 0 and no running pod); scale it to 0 first, or pass --live for a rehearsal"
fi

cleanup() {
  "${K[@]}" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  "${K[@]}" delete secret "$SECRET" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT
"${K[@]}" create secret generic "$SECRET" --from-file=token="$TOKEN_FILE" >/dev/null

jq --arg pod "$POD" --arg c "$CONTAINER" --arg secret "$SECRET" --argjson node "$node_name" '
  .spec.template.spec as $ps | ($ps.containers[] | select(.name == $c)) as $oc
  | ([$oc.volumeMounts[]?.name]) as $names
  | {
      apiVersion: "v1", kind: "Pod",
      metadata: {name: $pod, labels: {"app.kubernetes.io/part-of": "standalone-odoo-migrate",
                                      "app.kubernetes.io/component": "export"}},
      spec: ({
        restartPolicy: "Never", automountServiceAccountToken: false, activeDeadlineSeconds: 7200,
        securityContext: ($ps.securityContext // {}), nodeSelector: ($ps.nodeSelector // {}),
        imagePullSecrets: ($ps.imagePullSecrets // []), tolerations: ($ps.tolerations // []),
        volumes: [$ps.volumes[]? | select(.name as $n | $names | index($n))],
        containers: [{
          name: "export", image: $oc.image, command: ["sleep", "7200"],
          env: (($oc.env // []) + [{name: "UPLOAD_TOKEN", valueFrom: {secretKeyRef: {name: $secret, key: "token"}}}]),
          envFrom: ($oc.envFrom // []),
          volumeMounts: [$oc.volumeMounts[]? | . + {readOnly: true}],
          resources: {requests: {cpu: "200m", memory: "512Mi"}, limits: {cpu: "2", memory: "2Gi"}}
        }]
      } + (if $node then {nodeName: $node} else {} end))
    }' <<<"$wl" | "${K[@]}" apply -f - >/dev/null
"${K[@]}" wait --for=condition=Ready "pod/$POD" --timeout=600s >/dev/null \
  || { "${K[@]}" describe pod "$POD" | tail -20 >&2; die 3 "helper pod $POD not ready"; }
image_id=$("${K[@]}" get pod "$POD" -o jsonpath='{.status.containerStatuses[0].imageID}')
digest=${image_id##*@}
[[ $digest == sha256:* ]] || die 3 "cannot resolve source image digest from imageID '$image_id'"

echo "export: $WORKLOAD → $URL (helper $POD, image $digest, $( ((LIVE)) && echo live || echo frozen))" >&2
WORKLOAD=pod/$POD CONTAINER=export
json=$(inspect_in_pod K export --sql-b64 "$(base64 -w0 < "$SQL")" --url "$URL" --image-digest "$digest" ${DB:+--db "$DB"})
out=./export-$NS-$TS.json
jq --argjson live "$LIVE" '. + {live: ($live == 1)}' <<<"$json" | tee "$out"
echo "saved $out" >&2
