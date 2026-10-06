#!/usr/bin/env bash
#
# seed-from-prod.sh — prod clone 灌進 platform lockdown 沙盒的「沙盒段」（#1206；裁決 #1162 留言 14057 D8）
#
# 取資料段（從 paas-system 讀 prod、push 進沙盒）由人執行，不在本腳本——見
# docs/guides/deployment/prod-clone-neutralization-runbook.md §B。本腳本只碰 dev-sandbox-<slug>。
#
# 前置：沙盒以 lockdown 建立（cc/woowprd#5／#8）：
#   SANDBOX_NEUTRALIZE_LOCKDOWN=1 /woow-dev:k8s-dev-sandbox create --mode platform --branch <slug> …
#
# Subcommands（<slug> 必填、不從 branch 推導；--context 必填）：
#   receive <slug> --context <ctx> --set <name> [--size 40Gi] [--storage-class longhorn-delete]
#       開搬運洞：建 incoming PVC、receiver pod（HTTP PUT）、只放行 paas-system→receiver 的 NP。
#       web／cron 必須是 0 副本。印出 push 時要用的 URL 與 token 取法。
#   restore <slug> --context <ctx> --set <name> [--db woow_prod] [--no-upgrade-marker] [--keep-incoming]
#       關搬運洞 → lockdown NP gate（內容＋標記＋探針）→ 一次性 Job restore（sha256、locale 取 manifest、
#       odoo 角色 pg_restore、filestore）→ 刪 incoming PVC → Stage A：neutralize-clone.sh（預設預寫 upgrade_done）
#       → 斷言 gate。全綠才印下一步。
#   stage-b <slug> --context <ctx> [--db woow_prod]
#       Odoo 起來之後再中和＋gate（＝neutralize-clone.sh，不寫 marker）。
#   gate <slug> --context <ctx> [--db woow_prod]
#       只跑斷言 gate（每次有 Odoo 程序啟動後都要跑：#1162 不變式）。
#   close <slug> --context <ctx>
#       只關搬運洞（receiver pod／Service／NP），不動 incoming PVC。
set -euo pipefail

CMD="${1:-}"; shift || true
SLUG="" CTX="" SET="" DB="woow_prod" SIZE="40Gi" SC="longhorn-delete" MARKER=1 KEEP_INCOMING=0
usage() { sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --context) CTX="$2"; shift 2 ;;
        --set) SET="$2"; shift 2 ;;
        --db) DB="$2"; shift 2 ;;
        --size) SIZE="$2"; shift 2 ;;
        --storage-class) SC="$2"; shift 2 ;;
        --no-upgrade-marker) MARKER=0; shift ;;
        --keep-incoming) KEEP_INCOMING=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 1 ;;
        *) [[ -z "$SLUG" ]] || { echo "多餘的參數：$1" >&2; exit 1; }; SLUG="$1"; shift ;;
    esac
done
case "$CMD" in receive|restore|stage-b|gate|close) ;; -h|--help|"") usage; exit 0 ;; *) echo "未知子命令：${CMD}" >&2; exit 1 ;; esac
[[ "$DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || { echo "--db '${DB}' 不合法" >&2; exit 1; }
if [[ "$CMD" == receive || "$CMD" == restore ]]; then
    [[ "$SET" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo "--set 必填（[A-Za-z0-9._-]）" >&2; exit 1; }
fi
[[ "$SIZE" =~ ^[1-9][0-9]*Gi$ ]] || { echo "--size 要 <N>Gi" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=prod-clone/lib.sh
source "${SCRIPT_DIR}/prod-clone/lib.sh"
PC_WORK="$(mktemp -d)"; chmod 700 "$PC_WORK"
trap 'rm -rf "$PC_WORK"' EXIT
pc_init "$SLUG" "$CTX"

close_hole() {
    kubectl delete pod -n "$NAMESPACE" "$PC_RECEIVER" --ignore-not-found --wait=true >/dev/null
    kubectl delete -n "$NAMESPACE" \
        "service/${PC_RECEIVER}" "networkpolicy/${PC_HOLE_NP}" "configmap/${PC_RECEIVER}" --ignore-not-found >/dev/null
    [[ "$(pc_check_transfer_hole)" == absent ]] || pc_die "搬運洞沒有關乾淨"
    pc_ok "搬運洞已關（receiver pod／Service／NP 都不在）"
}

odoo_image() {
    kubectl get deploy -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=server" \
        -o jsonpath='{.items[0].spec.template.spec.containers[?(@.name=="odoo")].image}'
}

cmd_receive() {
    pc_require_odoo_down
    pc_apply_marker_np
    if ! kubectl get secret -n "$NAMESPACE" prod-clone-upload-token >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(24), end="")' > "$PC_WORK/token"
        kubectl create secret generic prod-clone-upload-token -n "$NAMESPACE" --from-file=token="$PC_WORK/token" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
        rm -f "$PC_WORK/token"
    fi
    kubectl create configmap "$PC_RECEIVER" -n "$NAMESPACE" --from-file=receiver.py="${PC_DIR}/receiver.py" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl apply -n "$NAMESPACE" -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${PC_INCOMING_PVC}
  labels: {app.kubernetes.io/managed-by: prod-clone}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: ${SC}
  resources: {requests: {storage: ${SIZE}}}
---
apiVersion: v1
kind: Service
metadata:
  name: ${PC_RECEIVER}
  labels: {app.kubernetes.io/managed-by: prod-clone}
spec:
  selector: {app.kubernetes.io/component: prod-clone-receiver}
  ports: [{name: http, port: 8080, targetPort: 8080}]
---
# 搬運洞：只在 web／cron 0 副本期間存在；restore 前刪除並進 gate（K04）。
# egress 鏡射 paas-system：K3s 的 NP enforcer 會把部分回程封包當成新的 egress（同 plugin chart 的註解）。
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${PC_HOLE_NP}
  labels: {app.kubernetes.io/managed-by: prod-clone}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/component: prod-clone-receiver}}
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-system}}}]
      ports: [{protocol: TCP, port: 8080}]
  egress:
    - to: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: paas-system}}}]
---
apiVersion: v1
kind: Pod
metadata:
  name: ${PC_RECEIVER}
  labels: {app.kubernetes.io/component: prod-clone-receiver, app.kubernetes.io/managed-by: prod-clone}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: {runAsUser: 100, runAsGroup: 101, fsGroup: 101}
$(pc_pull_secret_yaml)
  containers:
    - name: receiver
      image: $(odoo_image)
      command: ["python3", "/opt/prod-clone/receiver.py"]
      env:
        - name: UPLOAD_TOKEN
          valueFrom: {secretKeyRef: {name: prod-clone-upload-token, key: token}}
      ports: [{containerPort: 8080}]
      readinessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 5}
      volumeMounts:
        - {name: incoming, mountPath: /incoming}
        - {name: code, mountPath: /opt/prod-clone, readOnly: true}
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: "1", memory: 512Mi}
  volumes:
    - name: incoming
      persistentVolumeClaim: {claimName: ${PC_INCOMING_PVC}}
    - name: code
      configMap: {name: ${PC_RECEIVER}}
EOF
    kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/${PC_RECEIVER}" --timeout=300s >/dev/null \
        || pc_die "receiver pod 沒有 Ready（kubectl -n ${NAMESPACE} describe pod ${PC_RECEIVER}）"
    pc_ok "搬運洞已開：receiver Ready，只放行 paas-system → ${PC_RECEIVER}:8080"
    cat <<EOF

【人類】在 paas-system 端把備份組 push 進來（runbook §B；⛔ 不用 kubectl exec … > file）：
  URL：   http://${PC_RECEIVER}.${NAMESPACE}.svc.cluster.local:8080/${SET}/<file>
  Header：X-Upload-Token: <token>   # token：kubectl --context ${CTX} -n ${NAMESPACE} get secret prod-clone-upload-token -o jsonpath='{.data.token}' | base64 -d
  必要檔：db.dump、filestore.tar、manifest.json、SHA256SUMS（＋選配 attachments.txt）；SHA256SUMS 最後送。
  每次 PUT 回應「<bytes> <sha256>」，與來源端比對。

push 完：seed-from-prod.sh restore ${SLUG} --context ${CTX} --set ${SET}
EOF
}

cmd_restore() {
    pc_require_odoo_down
    kubectl get pvc -n "$NAMESPACE" "$PC_INCOMING_PVC" >/dev/null 2>&1 || pc_die "沒有 ${PC_INCOMING_PVC}（先 receive＋push）"
    close_hole
    pc_apply_marker_np
    pc_info "restore 前 lockdown gate（NP 內容＋標記＋行為探針）..."
    local c m p
    c="$(pc_check_np_content)"; m="$(pc_check_np_marker)"; p="$(pc_check_np_probe)"
    [[ "$c" == PASS && "$m" == present && "$p" == blocked ]] \
        || pc_die "lockdown gate 不通過（np_content=${c} np_marker=${m} np_probe=${p}）——拒絕 restore"
    pc_ok "lockdown gate：np_content=${c} np_marker=${m} np_probe=${p}"
    if pc_db_exists "$DB"; then pc_die "DB ${DB} 已存在——不覆蓋（要重來請 destroy 沙盒重建）"; fi

    local job="${RELEASE}-prod-clone-restore" cred="${RELEASE}-credentials"
    kubectl get secret -n "$NAMESPACE" "$cred" >/dev/null || pc_die "找不到 ${cred}"
    kubectl create configmap "${job}" -n "$NAMESPACE" --from-file=restore.sh="${PC_DIR}/restore-in-pod.sh" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl delete job -n "$NAMESPACE" "$job" --ignore-not-found --wait=true >/dev/null
    kubectl apply -n "$NAMESPACE" -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  labels: {app.kubernetes.io/instance: ${RELEASE}, app.kubernetes.io/component: prod-clone-restore}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: {app.kubernetes.io/instance: ${RELEASE}, app.kubernetes.io/component: prod-clone-restore}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      # chown filestore 成 odoo（100:101）需要 root；只在沙盒內、一次性
      securityContext: {runAsUser: 0}
$(pc_pull_secret_yaml | sed 's/^/    /')
      containers:
        - name: restore
          image: $(pc_pg_image)
          command: ["/bin/bash", "/opt/prod-clone/restore.sh"]
          env:
            - {name: SET_DIR, value: /incoming/${SET}}
            - {name: ODOO_DB_NAME, value: ${DB}}
            - {name: PGHOST, value: ${RELEASE}-postgresql}
            - {name: PGPORT, value: "5432"}
            - {name: DATA_ROOT, value: /var/lib/odoo}
            - name: PGSUPERUSER_PASSWORD
              valueFrom: {secretKeyRef: {name: ${cred}, key: postgres-password}}
            - name: ODOO_DB_PASSWORD
              valueFrom: {secretKeyRef: {name: ${cred}, key: odoo-password}}
          volumeMounts:
            - {name: incoming, mountPath: /incoming, readOnly: true}
            - {name: data, mountPath: /var/lib/odoo}
            - {name: code, mountPath: /opt/prod-clone, readOnly: true}
          resources:
            requests: {cpu: 500m, memory: 512Mi}
            limits: {cpu: "2", memory: 2Gi}
      volumes:
        - name: incoming
          persistentVolumeClaim: {claimName: ${PC_INCOMING_PVC}, readOnly: true}
        - name: data
          persistentVolumeClaim: {claimName: ${RELEASE}-data}
        - name: code
          configMap: {name: ${job}}
EOF
    pc_info "restore Job 執行中（大 dump 需要時間；上限 2h）..."
    if ! kubectl wait -n "$NAMESPACE" --for=condition=complete "job/${job}" --timeout=7200s >/dev/null 2>&1; then
        kubectl logs -n "$NAMESPACE" "job/${job}" --tail=80 >&2 || true
        pc_die "restore Job 失敗（incoming 保留；看上方 RESTORE-FAIL）"
    fi
    kubectl logs -n "$NAMESPACE" "job/${job}" | grep -E '^RESTORE' | sed 's/^/    /'
    kubectl logs -n "$NAMESPACE" "job/${job}" | grep -qx 'RESTORE-DONE' || pc_die "restore Job 沒有印 RESTORE-DONE"
    kubectl delete -n "$NAMESPACE" "job/${job}" "configmap/${job}" --wait=false >/dev/null
    pc_ok "restore 完成"

    if [[ "$KEEP_INCOMING" == 1 ]]; then
        pc_warn "--keep-incoming：incoming PVC 保留（K05 斷言會 FAIL，直到刪除）"
    else
        kubectl delete pvc -n "$NAMESPACE" "$PC_INCOMING_PVC" --wait=true --timeout=300s >/dev/null \
            && kubectl delete secret -n "$NAMESPACE" prod-clone-upload-token --ignore-not-found >/dev/null
        pc_ok "incoming 原始檔已刪（PVC ${PC_INCOMING_PVC} 已刪除）"
    fi

    pc_info "Stage A：中和＋gate..."
    local args=("$SLUG" --context "$CTX" --db "$DB")
    [[ "$MARKER" == 1 ]] && args+=(--write-upgrade-marker)
    bash "${SCRIPT_DIR}/neutralize-clone.sh" "${args[@]}" || pc_die "Stage A 未通過——⛔ 不得把 web 拉起"
    pc_ok "Stage A 完成"
    cat <<EOF

下一步（#1231 彩排順序；每次 Odoo 程序啟動後都要再過 gate）：
  1. /woow-dev:k8s-dev-sandbox scale ${SLUG} --context ${CTX} --component server --replicas 1
     kubectl --context ${CTX} -n ${NAMESPACE} logs deploy/${RELEASE} -c upgrade-gate   # 預期 marker 相等 skip
  2. seed-from-prod.sh stage-b ${SLUG} --context ${CTX}       # Stage B：再中和＋gate
  3. /woow-dev:k8s-dev-sandbox scale ${SLUG} --context ${CTX} --component cron --replicas 1
  4. seed-from-prod.sh gate ${SLUG} --context ${CTX}          # cron 起來後再過一次
EOF
}

case "$CMD" in
    receive) cmd_receive ;;
    restore) cmd_restore ;;
    stage-b) exec bash "${SCRIPT_DIR}/neutralize-clone.sh" "$SLUG" --context "$CTX" --db "$DB" ;;
    gate)    pc_apply_marker_np; pc_gate "$DB" ;;
    close)   close_hole ;;
esac
