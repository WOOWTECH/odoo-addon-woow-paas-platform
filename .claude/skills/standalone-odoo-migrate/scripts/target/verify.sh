#!/usr/bin/env bash
# verify.sh — 目標端資料指紋比對（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent（目標 ns 必須在白名單）。
#
# 用法：
#   verify.sh --context <ctx> --ns <target-ns> --release <R> --set <name> --stage restore|upgrade [--cleanup] [--workdir <dir>]
#
# restore：restore Job 已在 incoming PVC 留下 target.fp／target.fs；取回 source.*／target.* 在本機跑
#   fingerprint_diff.py --stage restore（逐位元）。web 可以還是 0 副本。
# upgrade：web 已開回 1 副本、升級閘跑完 -u all 且 Ready；在 web pod 所在 node 起一顆唯讀 pod，重算 DB 指紋與
#   filestore 清單（upgraded.*），取回後跑 fingerprint_diff.py --stage upgrade。判準是客戶資料列：restore Job 留下的
#   baseline.bfp（還原後、Odoo 啟動前）對 upgraded.bfp——基準當時就存在的客戶資料列必須原封不動；模組資料列被重寫、
#   啟動後新增的列、registry 表列為預期內。set 裡沒有 baseline.bfp（舊版 restore）時退回整表比對（只容許 metadata 表）。
#   filestore：同時取目標 DB 仍引用的 store_fname（upgraded.refs）——仍被引用的檔不能少；已無引用而被 Odoo 回收的
#   來源檔（模組附件換了內容）列為預期內。
#   彩排（restore.sh --neutralize，set 裡有 neutralized 標記）：restore 階段比的仍是中和**之前**取的指紋；
#   upgrade 階段另外重跑 neutralize-assert.sql——-u all 或新版模組若把排程、寄信、金流等開回來，這裡不通過。
# --cleanup：比對通過後刪跑完的 restore Job 與 incoming PVC（含客戶資料的 dump）。
# 取回的檔案放 --workdir（預設 ./verify-<set>）；檔案都小（指紋與清單），用 kubectl exec cat 取回不會撞截斷。
# exit：0 通過、1 不通過、2 參數或前置條件不符、3 pod 失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PVC=standalone-odoo-incoming
POD=standalone-odoo-verify
CTX="" NS="" RELEASE="" SET="" STAGE="" CLEANUP=0 WORKDIR=""

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --set) SET=$2; shift 2 ;;
    --stage) STAGE=$2; shift 2 ;;
    --cleanup) CLEANUP=1; shift ;;
    --workdir) WORKDIR=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS && -n $RELEASE && -n $SET && $STAGE =~ ^(restore|upgrade)$ ]] \
  || { usage >&2; die 2 "--context, --ns, --release, --set and --stage restore|upgrade are required"; }
K=(kubectl --context "$CTX" -n "$NS")
WORKDIR=${WORKDIR:-./verify-$SET}
mkdir -p "$WORKDIR"

dep=$("${K[@]}" get deploy "$RELEASE" -o json) || die 2 "deployment $RELEASE not found"
db=$(jq -r '[.. | objects | select(.name? == "ODOO_DB_NAME") | .value] | first // empty' <<<"$dep")
sts=$("${K[@]}" get sts "$RELEASE-postgresql" -o json) || die 2 "statefulset $RELEASE-postgresql not found"
pg_image=$(jq -r '.spec.template.spec.containers[] | select(.name == "postgresql") | .image' <<<"$sts")
pull_secrets=$(jq -c '.spec.template.spec.imagePullSecrets // []' <<<"$sts")
node=null
if [[ $STAGE == upgrade ]]; then
  web=$("${K[@]}" get pod -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=server" -o json \
    | jq '[.items[] | select(.status.phase == "Running" and ([.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length > 0))]')
  (( $(jq length <<<"$web") >= 1 )) || die 2 "upgrade stage needs a Ready web pod (upgrade gate finished)"
  node=$(jq '.[0].spec.nodeName' <<<"$web")
fi

cleanup_pod() { "${K[@]}" delete pod "$POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup_pod EXIT
# 上一輪的 verify pod 可能還在終止中（收尾不等刪除完成）；同名 apply 會打到正在刪的那顆
if "${K[@]}" get pod "$POD" >/dev/null 2>&1; then
  "${K[@]}" wait --for=delete "pod/$POD" --timeout=120s >/dev/null 2>&1 || die 3 "previous verify pod still terminating"
fi
"${K[@]}" create configmap "$POD" --from-file=fingerprint.sql="$SCRIPT_DIR/../fingerprint.sql" \
  --from-file=fingerprint_business.sql="$SCRIPT_DIR/../fingerprint_business.sql" \
  --from-file=neutralize-assert.sql="$SCRIPT_DIR/neutralize-assert.sql" \
  --dry-run=client -o yaml | "${K[@]}" apply -f - >/dev/null
jq -n --arg pod "$POD" --arg image "$pg_image" --argjson ps "$pull_secrets" --argjson node "$node" \
  --arg rel "$RELEASE" --arg pvc "$PVC" --arg stage "$STAGE" '{
  apiVersion: "v1", kind: "Pod",
  metadata: {name: $pod, labels: {"app.kubernetes.io/part-of": "standalone-odoo-migrate",
                                  "app.kubernetes.io/instance": $rel, "app.kubernetes.io/component": "verify"}},
  spec: ({
    restartPolicy: "Never", automountServiceAccountToken: false, imagePullSecrets: $ps, activeDeadlineSeconds: 3600,
    containers: [{name: "verify", image: $image, command: ["sleep", "3600"], securityContext: {runAsUser: 0},
      env: [{name: "PGPASSWORD", valueFrom: {secretKeyRef: {name: ($rel + "-credentials"), key: "postgres-password"}}}],
      volumeMounts: ([{name: "incoming", mountPath: "/incoming"}, {name: "code", mountPath: "/opt/verify", readOnly: true}]
                     + (if $stage == "upgrade" then [{name: "data", mountPath: "/var/lib/odoo", readOnly: true}] else [] end))}],
    volumes: ([{name: "incoming", persistentVolumeClaim: {claimName: $pvc}}, {name: "code", configMap: {name: $pod}}]
              + (if $stage == "upgrade" then [{name: "data", persistentVolumeClaim: {claimName: ($rel + "-data"), readOnly: true}}] else [] end))
  } + (if $node then {nodeName: $node} else {} end))}' | "${K[@]}" apply -f - >/dev/null
"${K[@]}" wait --for=condition=Ready "pod/$POD" --timeout=300s >/dev/null \
  || { "${K[@]}" describe pod "$POD" | tail -15 >&2; die 3 "verify pod not ready"; }

S=/incoming/$SET
if [[ $STAGE == upgrade ]]; then
  # 平台租戶 ns 有 baseline NetworkPolicy，新 pod 的規則約 1 秒後才生效，立刻連 PG 會被拒（stg 實測）⇒ 先等到連得上
  "${K[@]}" exec "$POD" -- bash -c "set -euo pipefail; export LC_ALL=C
    for _i in \$(seq 1 60); do pg_isready -q -h '$RELEASE-postgresql' -t 2 && break; sleep 1; done
    pg_isready -q -h '$RELEASE-postgresql' -t 2 || { echo 'cannot reach $RELEASE-postgresql:5432' >&2; exit 1; }
    psql -h '$RELEASE-postgresql' -U postgres -d '$db' -X -q -tA -v ON_ERROR_STOP=1 -f /opt/verify/fingerprint.sql > $S/upgraded.fp
    if [[ -s $S/baseline.bfp ]]; then
      BFP_BASELINE=$S/baseline.bfp psql -h '$RELEASE-postgresql' -U postgres -d '$db' -X -q -tA -v ON_ERROR_STOP=1 -v baseline=1 \
        -f /opt/verify/fingerprint_business.sql > $S/upgraded.bfp
    fi
    psql -h '$RELEASE-postgresql' -U postgres -d '$db' -X -q -tA -v ON_ERROR_STOP=1 \
      -c 'SELECT DISTINCT store_fname FROM ir_attachment WHERE store_fname IS NOT NULL ORDER BY 1' > $S/upgraded.refs
    if [[ -e $S/neutralized ]]; then
      psql -h '$RELEASE-postgresql' -U postgres -d '$db' -X -q -tA -v ON_ERROR_STOP=1 \
        -f /opt/verify/neutralize-assert.sql > $S/neutralize-upgraded.txt
    fi
    cd /var/lib/odoo/filestore/'$db' && find . -type f -printf '%P\0' | sort -z | xargs -0 -r sha256sum > $S/upgraded.fs" \
    || die 3 "fingerprint in verify pod failed"
  tgt=upgraded
else
  tgt=target
fi
for f in source.fp source.fs "$tgt.fp" "$tgt.fs"; do
  "${K[@]}" exec "$POD" -- cat "$S/$f" > "$WORKDIR/$f" || die 3 "cannot read $S/$f"
done
business=()
if [[ $STAGE == upgrade ]]; then
  "${K[@]}" exec "$POD" -- cat "$S/upgraded.refs" > "$WORKDIR/upgraded.refs" || die 3 "cannot read $S/upgraded.refs"
  business+=(--target-refs "$WORKDIR/upgraded.refs")
  if "${K[@]}" exec "$POD" -- test -s "$S/upgraded.bfp"; then
    for f in baseline.bfp upgraded.bfp; do
      "${K[@]}" exec "$POD" -- cat "$S/$f" > "$WORKDIR/$f" || die 3 "cannot read $S/$f"
    done
    business+=(--source-business "$WORKDIR/baseline.bfp" --target-business "$WORKDIR/upgraded.bfp")
  else
    echo "note: $S/baseline.bfp missing — falling back to whole-table comparison" >&2
  fi
fi
neutralized=0
if "${K[@]}" exec "$POD" -- test -e "$S/neutralized"; then
  neutralized=1
  if [[ $STAGE == upgrade ]]; then
    "${K[@]}" exec "$POD" -- cat "$S/neutralize-upgraded.txt" > "$WORKDIR/neutralize-upgraded.txt" \
      || die 3 "cannot read $S/neutralize-upgraded.txt"
  fi
fi
"${K[@]}" delete configmap "$POD" --ignore-not-found >/dev/null

rc=0
python3 "$SCRIPT_DIR/../fingerprint_diff.py" --stage "$STAGE" \
  --source-db "$WORKDIR/source.fp" --target-db "$WORKDIR/$tgt.fp" \
  --source-fs "$WORKDIR/source.fs" --target-fs "$WORKDIR/$tgt.fs" "${business[@]}" \
  --json "$WORKDIR/diff-$STAGE.json" || rc=$?
if (( neutralized )); then
  echo
  if [[ $STAGE == restore ]]; then
    echo "彩排（已中和）：上面比的是中和之前取的指紋；現在資料庫裡的排程、寄信、金流、登入密碼等已被改掉。"
  else
    fails=$(grep -c '^nfail|' "$WORKDIR/neutralize-upgraded.txt" || true)
    if (( fails > 0 )); then
      echo "彩排中和斷言：不通過（升級或開機後有 $fails 項被開回來）——先把 web 停掉再處理："
      grep '^nfail|' "$WORKDIR/neutralize-upgraded.txt" | awk -F'|' '{printf "- %s：%s\n", $2, $3}'
      (( rc != 0 )) || rc=1
    else
      echo "彩排中和斷言：通過（升級與開機後仍然是中和狀態）"
    fi
  fi
fi
if (( rc == 0 && CLEANUP )); then
  cleanup_pod
  "${K[@]}" wait --for=delete "pod/$POD" --timeout=120s >/dev/null 2>&1 || true
  # 跑完的 restore Job（ttl 一天）的 pod 還掛著 incoming PVC，不先刪掉 PVC 會一直卡在 Terminating（stg 實測）
  "${K[@]}" delete job -l app.kubernetes.io/part-of=standalone-odoo-migrate,app.kubernetes.io/component=restore \
    --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1 || true
  "${K[@]}" delete pvc "$PVC" --wait=false >/dev/null && echo "incoming PVC $PVC deleted" >&2
fi
exit "$rc"
