#!/usr/bin/env bash
# restore.sh — 在目標 ns 跑一次性 restore Job，把匯出組還原進 woow-odoo release（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent（目標 ns 必須在白名單；prod 每步人工放行）。
#
# 用法：
#   restore.sh --context <ctx> --ns <target-ns> --release <R> --set <name> [--replace-db] [--no-marker]
#
# 前置：export.sh 已把整組檔送進 incoming PVC；receive.sh close 已關洞（incoming PVC 是 RWO，receiver 還掛著就跑不了）；
#   release R 的 odoo web 已經 0 副本（升級閘、bootstrap 都在 web pod 的 initContainer，web 停了才能安全換 DB）。
#   平台服務怎麼停到 0 副本見 SKILL.md；dev 沙盒直接 kubectl scale。
# Job：image＝R-postgresql 的 image，掛 incoming PVC 與 R-data，跑 restore-in-pod.sh——完整性、建庫（locale 取來源）、
#   pg_restore、filestore、指紋逐位元比對，一致才預寫 upgrade_done 與 tenant_localized 兩個 marker。
#   --replace-db：允許 DROP chart 首裝建的空庫（平台買服務時一定先有一個）。
#   --no-marker：只還原＋比對，不寫 marker（odoo 開機時升級閘會 exit 3）。
# incoming PVC 保留（升級後的指紋比對要用 source.fp／source.fs），由 verify.sh --stage upgrade --cleanup 刪除。印出 Job log 尾段。
# exit：0 成功、2 參數或前置條件不符、3 Job 失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PVC=standalone-odoo-incoming
RECEIVER=standalone-odoo-receiver
CTX="" NS="" RELEASE="" SET="" REPLACE=0 MARKER=1

usage() { sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --set) SET=$2; shift 2 ;;
    --replace-db) REPLACE=1; shift ;;
    --no-marker) MARKER=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS && -n $RELEASE && $SET =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
  || { usage >&2; die 2 "--context, --ns, --release and --set are required"; }
K=(kubectl --context "$CTX" -n "$NS")
JOB=standalone-odoo-restore-$(date -u +%H%M%S)

dep=$("${K[@]}" get deploy "$RELEASE" -o json) || die 2 "deployment $RELEASE not found"
[[ $(jq '.spec.replicas' <<<"$dep") == 0 ]] || die 2 "deploy/$RELEASE replicas must be 0 before restore"
pods=$("${K[@]}" get pod -l "app.kubernetes.io/instance=$RELEASE,app.kubernetes.io/component=server" -o name)
[[ -z $pods ]] || die 2 "web pod still present: $pods"
! "${K[@]}" get pod "$RECEIVER" >/dev/null 2>&1 || die 2 "receiver still running; run receive.sh close first"
"${K[@]}" get pvc "$PVC" >/dev/null || die 2 "incoming PVC $PVC not found (receive.sh open + export.sh first)"
db=$(jq -r '[.. | objects | select(.name? == "ODOO_DB_NAME") | .value] | first // empty' <<<"$dep")
[[ -n $db ]] || die 2 "cannot find ODOO_DB_NAME in deploy/$RELEASE"
sts=$("${K[@]}" get sts "$RELEASE-postgresql" -o json) || die 2 "statefulset $RELEASE-postgresql not found"
pg_image=$(jq -r '.spec.template.spec.containers[] | select(.name == "postgresql") | .image' <<<"$sts")
pull_secrets=$(jq -c '.spec.template.spec.imagePullSecrets // []' <<<"$sts")
replace_db=""; (( REPLACE )) && replace_db=$db

"${K[@]}" create configmap "$JOB" --from-file=restore-in-pod.sh="$SCRIPT_DIR/restore-in-pod.sh" \
  --from-file=fingerprint.sql="$SCRIPT_DIR/../fingerprint.sql" \
  --from-file=fingerprint_business.sql="$SCRIPT_DIR/../fingerprint_business.sql" >/dev/null
"${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
  labels: {app.kubernetes.io/part-of: standalone-odoo-migrate, app.kubernetes.io/component: restore}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 7200
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      # tenant NP（若有）放行同 release 的 pod 連 postgres
      labels: {app.kubernetes.io/part-of: standalone-odoo-migrate, app.kubernetes.io/instance: $RELEASE, app.kubernetes.io/component: restore}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      imagePullSecrets: $pull_secrets
      containers:
        - name: restore
          image: $pg_image
          command: ["bash", "/opt/restore/restore-in-pod.sh"]
          securityContext: {runAsUser: 0}
          env:
            - {name: SET_DIR, value: /incoming/$SET}
            - {name: ODOO_DB_NAME, value: "$db"}
            - {name: PGHOST, value: $RELEASE-postgresql}
            - {name: PGPORT, value: "5432"}
            - {name: DATA_ROOT, value: /var/lib/odoo}
            - {name: FINGERPRINT_SQL, value: /opt/restore/fingerprint.sql}
            - {name: BUSINESS_SQL, value: /opt/restore/fingerprint_business.sql}
            - {name: REPLACE_DB, value: "$replace_db"}
            - {name: WRITE_MARKER, value: "$MARKER"}
            - name: PGSUPERUSER_PASSWORD
              valueFrom: {secretKeyRef: {name: $RELEASE-credentials, key: postgres-password}}
            - name: ODOO_DB_PASSWORD
              valueFrom: {secretKeyRef: {name: $RELEASE-credentials, key: odoo-password}}
          volumeMounts:
            - {name: incoming, mountPath: /incoming}
            - {name: data, mountPath: /var/lib/odoo}
            - {name: code, mountPath: /opt/restore, readOnly: true}
          resources:
            requests: {cpu: 250m, memory: 512Mi}
            limits: {cpu: "2", memory: 2Gi}
      volumes:
        - name: incoming
          persistentVolumeClaim: {claimName: $PVC}
        - name: data
          persistentVolumeClaim: {claimName: $RELEASE-data}
        - name: code
          configMap: {name: $JOB}
EOF
echo "restore job $JOB started (db $db, replace=$REPLACE, marker=$MARKER)" >&2
status=""
for _ in $(seq 1 720); do
  status=$("${K[@]}" get job "$JOB" -o jsonpath='{.status.succeeded}/{.status.failed}')
  [[ $status == 1/* || $status == */1 ]] && break
  sleep 5
done
"${K[@]}" logs "job/$JOB" --tail=40 >&2 || true
"${K[@]}" delete configmap "$JOB" --ignore-not-found >/dev/null
[[ $status == 1/* ]] || die 3 "restore job $JOB failed (status $status); incoming PVC kept for inspection"
echo "RESTORE OK: $RELEASE/$db（incoming PVC 保留；下一步 verify.sh --stage restore，再把 web 開回 1 副本）"
