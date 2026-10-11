#!/usr/bin/env bash
# receive.sh — 目標 ns 的搬運洞：incoming PVC＋HTTP PUT receiver（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent（目標 ns 必須在白名單：dev-sandbox-*、paas-ws-stg-*、paas-ws-*；prod 每步人工放行）。
#
# 用法：
#   receive.sh open  --context <ctx> --ns <target-ns> --release <R> --source-ns <ns> --set <name> [--size 20Gi] [--token-out <file>]
#   receive.sh close --context <ctx> --ns <target-ns>
#
# open：建 upload token Secret、incoming PVC（longhorn-delete）、receiver pod（image＝release R 的 odoo image，
#   程式＝woow-e2e-fixtures/scripts/prod-clone/receiver.py）、Service、NP（只放行 --source-ns → receiver:8080）。
#   token 寫到 --token-out（預設 ./upload-token-<set>，權限 600）交給 WMO 的 export.sh；印出 --url。
# close：刪 receiver pod／Service／NP／token Secret；**保留** incoming PVC（restore.sh 要讀，restore 完自己刪）。
# exit：0 成功、2 參數錯誤、3 incoming PVC 建不出來（例：租戶 ns 儲存配額不足）或 receiver 起不來
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RECEIVER_PY="$SCRIPT_DIR/../../../woow-e2e-fixtures/scripts/prod-clone/receiver.py"
NAME=standalone-odoo-receiver
PVC=standalone-odoo-incoming
TOKEN_SECRET=standalone-odoo-upload-token
CMD=${1:-}; shift || true
CTX="" NS="" RELEASE="" SRC_NS="" SET="" SIZE=20Gi TOKEN_OUT=""

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --source-ns) SRC_NS=$2; shift 2 ;;
    --set) SET=$2; shift 2 ;;
    --size) SIZE=$2; shift 2 ;;
    --token-out) TOKEN_OUT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ $CMD =~ ^(open|close)$ && -n $CTX && -n $NS ]] || { usage >&2; die 2 "subcommand, --context and --ns are required"; }
K=(kubectl --context "$CTX" -n "$NS")

close() {
  "${K[@]}" delete pod,service,networkpolicy "$NAME" --ignore-not-found --wait=true >/dev/null
  "${K[@]}" delete configmap "$NAME" --ignore-not-found >/dev/null
  "${K[@]}" delete secret "$TOKEN_SECRET" --ignore-not-found >/dev/null
  echo "receiver closed in $NS (PVC $PVC kept)" >&2
}

open() {
  [[ -n $RELEASE && -n $SRC_NS && $SET =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
    || die 2 "open needs --release, --source-ns and --set (^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$)"
  [[ -r $RECEIVER_PY ]] || die 2 "missing $RECEIVER_PY"
  local dep image pull_secrets token_out
  dep=$("${K[@]}" get deploy "$RELEASE" -o json) || die 2 "deployment $RELEASE not found in $NS"
  image=$(jq -r '.spec.template.spec.containers[] | select(.name == "odoo") | .image' <<<"$dep")
  pull_secrets=$(jq -c '.spec.template.spec.imagePullSecrets // []' <<<"$dep")
  token_out=${TOKEN_OUT:-./upload-token-$SET}
  if ! "${K[@]}" get secret "$TOKEN_SECRET" >/dev/null 2>&1; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_hex(24), end="")' > "$token_out")
    "${K[@]}" create secret generic "$TOKEN_SECRET" --from-file=token="$token_out" >/dev/null
  else
    (umask 077; "${K[@]}" get secret "$TOKEN_SECRET" -o jsonpath='{.data.token}' | base64 -d > "$token_out")
  fi
  "${K[@]}" create configmap "$NAME" --from-file=receiver.py="$RECEIVER_PY" --dry-run=client -o yaml \
    | "${K[@]}" apply -f - >/dev/null
  # PVC 先單獨建：租戶 ns 有儲存配額（一台 Odoo 就佔 48Gi，刪掉的服務在 7 天寬限期內仍然計入），超過時要在這裡
  # 明確失敗，不要留下一顆永遠 Pending 的 receiver。
  local err
  if "${K[@]}" get pvc "$PVC" >/dev/null 2>&1; then
    echo "incoming PVC $PVC already exists; reusing it (--size ignored)" >&2
  elif ! err=$("${K[@]}" apply -f - 2>&1 >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $PVC
  labels: {app.kubernetes.io/part-of: standalone-odoo-migrate}
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-delete
  resources: {requests: {storage: $SIZE}}
EOF
  ); then
    die 3 "cannot create incoming PVC $PVC ($SIZE): ${err##*$'\n'} — 配額不足時用 --size 調小（dump＋filestore 的兩倍就夠）後重跑"
  fi
  "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $NAME
  labels: {app.kubernetes.io/part-of: standalone-odoo-migrate}
spec:
  selector: {app.kubernetes.io/component: standalone-odoo-receiver}
  ports: [{name: http, port: 8080, targetPort: 8080}]
---
# 搬運洞：只放行來源 ns 進 receiver:8080；egress 鏡射來源 ns（K3s NP enforcer 會把部分回程封包當成新的 egress）
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: $NAME
  labels: {app.kubernetes.io/part-of: standalone-odoo-migrate}
spec:
  podSelector: {matchLabels: {app.kubernetes.io/component: standalone-odoo-receiver}}
  policyTypes: [Ingress, Egress]
  ingress:
    - from: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: $SRC_NS}}}]
      ports: [{protocol: TCP, port: 8080}]
  egress:
    - to: [{namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: $SRC_NS}}}]
---
apiVersion: v1
kind: Pod
metadata:
  name: $NAME
  labels: {app.kubernetes.io/part-of: standalone-odoo-migrate, app.kubernetes.io/component: standalone-odoo-receiver}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: {runAsUser: 100, runAsGroup: 101, runAsNonRoot: true, fsGroup: 101, seccompProfile: {type: RuntimeDefault}}
  imagePullSecrets: $pull_secrets
  containers:
    - name: receiver
      image: $image
      command: ["python3", "/opt/receiver/receiver.py"]
      env:
        - name: UPLOAD_TOKEN
          valueFrom: {secretKeyRef: {name: $TOKEN_SECRET, key: token}}
      ports: [{containerPort: 8080}]
      readinessProbe: {httpGet: {path: /healthz, port: 8080}, periodSeconds: 5}
      securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
      volumeMounts:
        - {name: incoming, mountPath: /incoming}
        - {name: code, mountPath: /opt/receiver, readOnly: true}
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: "1", memory: 512Mi}
  volumes:
    - name: incoming
      persistentVolumeClaim: {claimName: $PVC}
    - name: code
      configMap: {name: $NAME}
EOF
  "${K[@]}" wait --for=condition=Ready "pod/$NAME" --timeout=300s >/dev/null \
    || { "${K[@]}" describe pod "$NAME" | tail -15 >&2; die 3 "receiver pod not ready"; }
  echo "receiver ready in $NS; only $SRC_NS may connect" >&2
  echo "token file: $token_out" >&2
  echo "http://$NAME.$NS.svc.cluster.local:8080/$SET"
}

"$CMD"
