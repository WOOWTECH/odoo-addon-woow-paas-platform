#!/usr/bin/env bash
# restore.sh — 在目標 ns 跑一次性 restore Job，把匯出組還原進 woow-odoo release（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent（目標 ns 必須在白名單；prod 每步人工放行）。
#
# 用法：
#   restore.sh --context <ctx> --ns <target-ns> --release <R> --set <name> [--replace-db] [--no-marker]
#              [--neutralize [--extra-neutralize-sql <file>]]
#
# 前置：export.sh 已把整組檔送進 incoming PVC；receive.sh close 已關洞（incoming PVC 是 RWO，receiver 還掛著就跑不了）；
#   release R 的 odoo web 已經 0 副本（升級閘、bootstrap 都在 web pod 的 initContainer，web 停了才能安全換 DB）。
#   平台服務怎麼停到 0 副本見 SKILL.md；dev 沙盒直接 kubectl scale。
# Job：image＝R-postgresql 的 image，掛 incoming PVC 與 R-data，跑 restore-in-pod.sh——完整性、建庫（locale 取來源）、
#   pg_restore、filestore、指紋逐位元比對，一致才預寫 upgrade_done 與 tenant_localized 兩個 marker。
#   --replace-db：允許 DROP chart 首裝建的空庫（平台買服務時一定先有一個）。
#   --no-marker：只還原＋比對，不寫 marker（odoo 開機時升級閘會 exit 3）。
#   --neutralize：**彩排用**（來源還在營運、目標站不能對外動作）。逐位元比對通過後、取基準之前把資料庫中和：
#     Job 多一個 release odoo image 的容器跑 `odoo neutralize --stdout`（官方 SQL：排程、寄信、收信、金流、webhook…），
#     再套 neutralize-supplement.sql（登入密碼／API key、WoowTech addon 的對外整合）與 --extra-neutralize-sql
#     （站別特有的設定，選配），最後跑 neutralize-assert.sql——有任何一項沒中和就失敗、不寫 marker。
#     彩排密碼在本機產生、存進 Secret standalone-odoo-rehearsal-login（key password），不印出。
#     ⛔ 中和過的資料庫不得用於正式切換；正式切換不要帶這個旗標。
# incoming PVC 保留（升級後的指紋比對要用 source.fp／source.fs），由 verify.sh --stage upgrade --cleanup 刪除。印出 Job log 尾段。
# exit：0 成功、2 參數或前置條件不符、3 Job 失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PVC=standalone-odoo-incoming
RECEIVER=standalone-odoo-receiver
LOGIN_SECRET=standalone-odoo-rehearsal-login
CTX="" NS="" RELEASE="" SET="" REPLACE=0 MARKER=1 NEUTRALIZE=0 EXTRA_SQL=""

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --set) SET=$2; shift 2 ;;
    --replace-db) REPLACE=1; shift ;;
    --no-marker) MARKER=0; shift ;;
    --neutralize) NEUTRALIZE=1; shift ;;
    --extra-neutralize-sql) EXTRA_SQL=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS && -n $RELEASE && $SET =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
  || { usage >&2; die 2 "--context, --ns, --release and --set are required"; }
if [[ -n $EXTRA_SQL ]]; then
  (( NEUTRALIZE )) || die 2 "--extra-neutralize-sql needs --neutralize"
  [[ -r $EXTRA_SQL ]] || die 2 "cannot read $EXTRA_SQL"
fi
(( !NEUTRALIZE || MARKER )) || die 2 "--neutralize and --no-marker do not go together"
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

odoo_image=$(jq -r '.spec.template.spec.containers[] | select(.name == "odoo") | .image' <<<"$dep")
pull_secrets=$(jq -c --argjson a "$pull_secrets" '($a + (.spec.template.spec.imagePullSecrets // [])) | unique' <<<"$dep")
cm_files=(--from-file=restore-in-pod.sh="$SCRIPT_DIR/restore-in-pod.sh"
  --from-file=fingerprint.sql="$SCRIPT_DIR/../fingerprint.sql"
  --from-file=fingerprint_business.sql="$SCRIPT_DIR/../fingerprint_business.sql")
if (( NEUTRALIZE )); then
  [[ -n $odoo_image ]] || die 2 "cannot find the odoo container image in deploy/$RELEASE"
  cm_files+=(--from-file=neutralize-in-odoo.sh="$SCRIPT_DIR/neutralize-in-odoo.sh"
    --from-file=neutralize-supplement.sql="$SCRIPT_DIR/neutralize-supplement.sql"
    --from-file=neutralize-assert.sql="$SCRIPT_DIR/neutralize-assert.sql")
  [[ -z $EXTRA_SQL ]] || cm_files+=(--from-file=neutralize-extra.sql="$EXTRA_SQL")
  # 彩排密碼：本機產生、經 stdin 進 Secret，不上 argv、不印出；已存在就沿用（重跑彩排密碼不變）
  if ! "${K[@]}" get secret "$LOGIN_SECRET" >/dev/null 2>&1; then
    openssl rand -base64 18 | tr -d '\n' | "${K[@]}" create secret generic "$LOGIN_SECRET" \
      --from-file=password=/dev/stdin >/dev/null
    "${K[@]}" label secret "$LOGIN_SECRET" app.kubernetes.io/part-of=standalone-odoo-migrate >/dev/null
  fi
fi
"${K[@]}" create configmap "$JOB" "${cm_files[@]}" >/dev/null

# Job：平常一個容器跑完（PHASE=all）。--neutralize 時拆三段，依序：
#   restore（postgres image：還原＋逐位元比對）→ neutralize-sql（odoo image：產官方中和 SQL 與密碼 hash，不改 DB）
#   → finalize（postgres image：套用中和＋斷言、取基準、寫 marker）
jq -n --arg job "$JOB" --arg rel "$RELEASE" --arg set "$SET" --arg db "$db" --arg pvc "$PVC" \
  --arg pg "$pg_image" --arg odoo "$odoo_image" --arg replace "$replace_db" --arg marker "$MARKER" \
  --arg login "$LOGIN_SECRET" --argjson ps "$pull_secrets" --argjson neutralize "$NEUTRALIZE" \
  --argjson extra "$([[ -n $EXTRA_SQL ]] && echo true || echo false)" '
  def secret($key): {valueFrom: {secretKeyRef: {name: ($rel + "-credentials"), key: $key}}};
  def mounts: [{name: "incoming", mountPath: "/incoming"}, {name: "data", mountPath: "/var/lib/odoo"},
               {name: "code", mountPath: "/opt/restore", readOnly: true}];
  def pg($name; $phase): {
    name: $name, image: $pg, command: ["bash", "/opt/restore/restore-in-pod.sh"], securityContext: {runAsUser: 0},
    env: ([
      {name: "SET_DIR", value: ("/incoming/" + $set)}, {name: "ODOO_DB_NAME", value: $db},
      {name: "PGHOST", value: ($rel + "-postgresql")}, {name: "PGPORT", value: "5432"},
      {name: "DATA_ROOT", value: "/var/lib/odoo"},
      {name: "FINGERPRINT_SQL", value: "/opt/restore/fingerprint.sql"},
      {name: "BUSINESS_SQL", value: "/opt/restore/fingerprint_business.sql"},
      {name: "REPLACE_DB", value: $replace}, {name: "WRITE_MARKER", value: $marker},
      {name: "PHASE", value: $phase}, {name: "NEUTRALIZE", value: ($neutralize | tostring)},
      ({name: "PGSUPERUSER_PASSWORD"} + secret("postgres-password")),
      ({name: "ODOO_DB_PASSWORD"} + secret("odoo-password"))
    ] + (if $neutralize == 1 then [
      {name: "NEUTRALIZE_SQL", value: "/opt/restore/neutralize-supplement.sql"},
      {name: "NEUTRALIZE_ASSERT_SQL", value: "/opt/restore/neutralize-assert.sql"}
    ] + (if $extra then [{name: "NEUTRALIZE_EXTRA_SQL", value: "/opt/restore/neutralize-extra.sql"}] else [] end)
    else [] end)),
    volumeMounts: mounts,
    resources: {requests: {cpu: "250m", memory: "512Mi"}, limits: {cpu: "2", memory: "2Gi"}}
  };
  def odoo_sql: {
    name: "neutralize-sql", image: $odoo, command: ["bash", "/opt/restore/neutralize-in-odoo.sh"],
    securityContext: {runAsUser: 0},
    env: [
      {name: "SET_DIR", value: ("/incoming/" + $set)}, {name: "ODOO_DB_NAME", value: $db},
      {name: "PGHOST", value: ($rel + "-postgresql")}, {name: "PGPORT", value: "5432"},
      ({name: "PGPASSWORD"} + secret("odoo-password")),
      {name: "REHEARSAL_PASSWORD", valueFrom: {secretKeyRef: {name: $login, key: "password"}}}
    ],
    volumeMounts: [{name: "incoming", mountPath: "/incoming"}, {name: "code", mountPath: "/opt/restore", readOnly: true}],
    resources: {requests: {cpu: "250m", memory: "512Mi"}, limits: {cpu: "2", memory: "2Gi"}}
  };
  {
    apiVersion: "batch/v1", kind: "Job",
    metadata: {name: $job, labels: {"app.kubernetes.io/part-of": "standalone-odoo-migrate",
                                    "app.kubernetes.io/component": "restore"}},
    spec: {
      backoffLimit: 0, activeDeadlineSeconds: 7200, ttlSecondsAfterFinished: 86400,
      template: {
        # tenant NP（若有）放行同 release 的 pod 連 postgres
        metadata: {labels: {"app.kubernetes.io/part-of": "standalone-odoo-migrate",
                            "app.kubernetes.io/instance": $rel, "app.kubernetes.io/component": "restore"}},
        spec: ({
          restartPolicy: "Never", automountServiceAccountToken: false, imagePullSecrets: $ps,
          volumes: [
            {name: "incoming", persistentVolumeClaim: {claimName: $pvc}},
            {name: "data", persistentVolumeClaim: {claimName: ($rel + "-data")}},
            {name: "code", configMap: {name: $job}}
          ]
        } + (if $neutralize == 1
             then {initContainers: [pg("restore"; "restore"), odoo_sql], containers: [pg("finalize"; "finalize")]}
             else {containers: [pg("restore"; "all")]} end))
      }
    }
  }' | "${K[@]}" apply -f - >/dev/null
echo "restore job $JOB started (db $db, replace=$REPLACE, marker=$MARKER, neutralize=$NEUTRALIZE)" >&2
status=""
for _ in $(seq 1 720); do
  status=$("${K[@]}" get job "$JOB" -o jsonpath='{.status.succeeded}/{.status.failed}')
  [[ $status == 1/* || $status == */1 ]] && break
  sleep 5
done
if (( NEUTRALIZE )); then
  "${K[@]}" logs "job/$JOB" -c restore --tail=12 >&2 || true
  "${K[@]}" logs "job/$JOB" -c neutralize-sql --tail=8 >&2 || true
  "${K[@]}" logs "job/$JOB" -c finalize --tail=80 >&2 || true
else
  "${K[@]}" logs "job/$JOB" --tail=40 >&2 || true
fi
"${K[@]}" delete configmap "$JOB" --ignore-not-found >/dev/null
[[ $status == 1/* ]] || die 3 "restore job $JOB failed (status $status); incoming PVC kept for inspection"
if (( NEUTRALIZE )); then
  echo "RESTORE OK（彩排，已中和）: $RELEASE/$db。上面 nscan 行列出的欄位要看過；全部使用者的密碼已換成彩排密碼（Secret $LOGIN_SECRET 的 password）。" \
    "下一步 verify.sh --stage restore，再把 web 開回 1 副本。⛔ 這個資料庫不得用於正式切換。"
else
  echo "RESTORE OK: $RELEASE/$db（incoming PVC 保留；下一步 verify.sh --stage restore，再把 web 開回 1 副本）"
fi
