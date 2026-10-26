#!/usr/bin/env bash
# fake-source.sh — 在 dev-sandbox-* 起一個「假 standalone Odoo 站」當搬遷來源（standalone-odoo-migrate，#1604）
#
# 形狀照抄 harumi-odoo：同一個 odoo-tenant chart（GitHub WOOWTECH/Woow_k3s_odoo feat/tenant-chart）、
# 同一份 values（deploy/woow-k3s/harumi-odoo.yaml，把 harumi 換成 <tenant>、拿掉 nodeSelector／tunnel／backup、
# 縮小 resources）、同一顆 odoo image digest、pgvector:pg17、nginx sidecar、執行時 pip install。
# 讓 skill 在白名單 ns 內完整開發與測試，不碰真正的 standalone 站。
#
# 用法：
#   fake-source.sh up   <slug> --context <ctx> [--tenant fake]   建 ns、Secret、ConfigMap、PVC，helm install
#   fake-source.sh seed <slug> --context <ctx> [--tenant fake]   放客製 addon、-i 模組、灌測試資料、vector 表
#        [--from-image <registry/repo:tag@sha256:…> --extra-addons a,b [--data-dir-addons c] [--pip "<specs>"]
#         [--pull-secret-from <ns>/<secret>]]
#   fake-source.sh down <slug> --context <ctx>                   helm uninstall＋刪 ns（PVC 是 longhorn-delete）
# ns 固定為 dev-sandbox-<slug>。密碼在本機以 openssl 產生、直接進 Secret，不印出。
#
# seed 的客製 addon 兩種來源：
#   預設：tests/fixtures/fake-source 的兩個假 addon——目標 cloud-odoo 沒有它們，用來測「image 缺 addon」（升級閘 exit 4）。
#   --from-image：從該 image 的 /all-addons 取（paas-odoo-ci 已經烘進去的 addon），--extra-addons 放 /mnt/extra-addons、
#     --data-dir-addons 放 data_dir/addons/18.0，--pip 是它們的 python 相依（在 odoo 容器內 pip install，落在 data PVC 的
#     ~/.local）。來源與目標的 addon 原始碼相同 ⇒ 用來測升級閘跑 -u all 的正向流程，不必另建 image。
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FIXTURES="$SCRIPT_DIR/../../tests/fixtures/fake-source"
CHART_REPO=https://github.com/WOOWTECH/Woow_k3s_odoo.git
CHART_REF=feat/tenant-chart
CACHE=${XDG_CACHE_HOME:-$HOME/.cache}/standalone-odoo-migrate
export PATH=$HOME/.local/bin:$PATH   # yq

CMD=${1:-}; shift || true
SLUG=${1:-}; shift || true
CTX="" T=fake FROM_IMAGE="" EXTRA_ADDONS="" DATADIR_ADDONS="" PIP_SPECS="" PULL_FROM=paas-ws-stg-4360e1f5/paas-registry
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --tenant) T=$2; shift 2 ;;
    --from-image) FROM_IMAGE=$2; shift 2 ;;
    --extra-addons) EXTRA_ADDONS=$2; shift 2 ;;
    --data-dir-addons) DATADIR_ADDONS=$2; shift 2 ;;
    --pip) PIP_SPECS=$2; shift 2 ;;
    --pull-secret-from) PULL_FROM=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
[[ $CMD =~ ^(up|seed|down)$ && $SLUG =~ ^[a-z0-9-]+$ && -n $CTX ]] || { usage >&2; exit 2; }
[[ $T =~ ^[a-z][a-z0-9]*$ ]] || { echo "--tenant must match ^[a-z][a-z0-9]*$" >&2; exit 2; }
MODLIST='^[a-z0-9_]+(,[a-z0-9_]+)*$'
if [[ -n $FROM_IMAGE ]]; then
  [[ $FROM_IMAGE =~ ^[^/]+/.+:[^@]+@sha256:[a-f0-9]{64}$ && $EXTRA_ADDONS =~ $MODLIST ]] \
    || { echo "--from-image needs registry/repo:tag@sha256:… and --extra-addons a,b" >&2; exit 2; }
  [[ -z $DATADIR_ADDONS || $DATADIR_ADDONS =~ $MODLIST ]] || { echo "bad --data-dir-addons" >&2; exit 2; }
elif [[ -n $EXTRA_ADDONS$DATADIR_ADDONS$PIP_SPECS ]]; then
  echo "--extra-addons／--data-dir-addons／--pip only apply with --from-image" >&2; exit 2
fi
NS=dev-sandbox-$SLUG
K=(kubectl --context "$CTX" -n "$NS")
log() { echo "[fake-source] $*" >&2; }
# Odoo 啟動時會把 data_dir/addons/<series> 建成 0500（config.addons_data_dir：要手動 +w 才能放模組）
DATADIR_UNPACK='d=/var/lib/odoo/addons/18.0; mkdir -p $d && chmod u+w $d && tar -xf - --no-same-owner --no-same-permissions -m -C $d'

fetch_chart() {
  if [[ -d $CACHE/Woow_k3s_odoo/.git ]]; then
    git -C "$CACHE/Woow_k3s_odoo" fetch -q --depth 1 origin "$CHART_REF"
    git -C "$CACHE/Woow_k3s_odoo" checkout -q FETCH_HEAD
  else
    mkdir -p "$CACHE"
    git clone -q --depth 1 --branch "$CHART_REF" "$CHART_REPO" "$CACHE/Woow_k3s_odoo"
  fi
  log "odoo-tenant chart @ $(git -C "$CACHE/Woow_k3s_odoo" rev-parse --short HEAD)"
}

render_values() {
  # harumi 的 values 原樣改名：init container、command、volume 順序都不動（這正是要測的形狀）
  T=$T yq '
    del(.odoo.nodeSelector) | del(.postgres.nodeSelector) | del(.odoo.podAnnotations) |
    .tunnel.enabled = false | .extraTunnels = [] | .backup.enabled = false |
    .odoo.resources = {"requests": {"cpu": "100m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "2Gi"}} |
    .postgres.resources = {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}}
  ' "$CACHE/Woow_k3s_odoo/deploy/woow-k3s/harumi-odoo.yaml" | sed "s/harumi/$T/g"
}

wait_odoo_started() {
  # 等 odoo 容器進入 running（不能看 .started：那要 startupProbe 通過才會 true，DB 未初始化時永遠 false）。
  # kubectl wait 的 jsonpath 不支援 [?()] 過濾式 ⇒ 有上限的輪詢（最多 10 分鐘）
  local i running
  for i in $(seq 120); do
    running=$("${K[@]}" get pod -l "app=$T-odoo" --field-selector=status.phase=Running \
      -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="odoo")].state.running.startedAt}' 2>/dev/null || true)
    [[ -n $running ]] && return 0
    sleep 5
  done
  echo "odoo container did not start within 10 minutes" >&2
  return 1
}

up() {
  fetch_chart
  local expires; expires=$(date -u -d '+24 hours' +%Y-%m-%dT%H:%M:%SZ)
  "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: $NS
  labels:
    app.kubernetes.io/part-of: standalone-odoo-migrate
  annotations:
    dev-sandbox.io/expires: "$expires"
    standalone-odoo-migrate/role: fake-source
EOF
  if ! "${K[@]}" get secret "$T-postgres-secret" >/dev/null 2>&1; then
    "${K[@]}" create secret generic "$T-postgres-secret" \
      --from-literal=POSTGRES_USER="$T" --from-literal=POSTGRES_DB=odoo \
      --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 16)" \
      --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 16)" >/dev/null
  fi
  "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: $T-odoo-config
data:
  odoo.conf: |
    [options]
    addons_path = /mnt/extra-addons
    data_dir = /var/lib/odoo
    db_host = $T-postgres-svc
    db_port = 5432
    db_user = $T
    db_password = replaced-at-runtime
    db_name = odoo
    dbfilter = ^odoo$
    admin_passwd = replaced-at-runtime
    list_db = True
    proxy_mode = True
    workers = 2
    max_cron_threads = 1
    limit_time_cpu = 1200
    limit_time_real = 2400
    limit_memory_soft = 2147483648
    limit_memory_hard = 2684354560
    smtp_server = smtp.example.invalid
    smtp_port = 465
    smtp_ssl = True
    without_demo = True
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: $T-nginx-config
data:
  nginx.conf: |
    worker_processes 1;
    events { worker_connections 1024; }
    http {
      client_max_body_size 100m;
      proxy_cache_path /var/cache/nginx/odoo levels=1:2 keys_zone=odoo:10m max_size=200m;
      map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
      server {
        listen 8080;
        location /websocket {
          proxy_pass http://127.0.0.1:8072;
          proxy_http_version 1.1;
          proxy_set_header Upgrade \$http_upgrade;
          proxy_set_header Connection \$connection_upgrade;
        }
        location /web/static/ { proxy_cache odoo; proxy_pass http://127.0.0.1:8069; }
        location / {
          proxy_set_header X-Forwarded-Host \$host;
          proxy_set_header X-Forwarded-Proto https;
          proxy_pass http://127.0.0.1:8069;
        }
      }
    }
EOF
  local pvc size
  for pvc in odoo-data:2Gi odoo-addons:1Gi db-pvc:2Gi nginx-cache-pvc:1Gi; do
    size=${pvc#*:}; pvc=$T-${pvc%%:*}
    "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $pvc
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: longhorn-delete
  resources:
    requests:
      storage: $size
EOF
  done
  local values; values=$(mktemp)
  render_values > "$values"
  # 不加 --wait：DB 還沒初始化前 /web/health 一律 500，odoo 永遠不會 Ready；
  # startupProbe 給 60s＋60×15s，seed 要在容器啟動後約 15 分鐘內跑完 -i（實測 point_of_sale 約數分鐘）
  log "helm upgrade --install odoo-tenant -n $NS"
  helm --kube-context "$CTX" -n "$NS" upgrade --install odoo-tenant "$CACHE/Woow_k3s_odoo/charts/odoo-tenant" \
    -f "$values" >/dev/null
  rm -f "$values"
  "${K[@]}" rollout status "deploy/$T-postgres" --timeout=10m >/dev/null
  wait_odoo_started
  "${K[@]}" get pods
  log "odoo container started; run 'seed' now (startup probe restarts it after ~15 min without a DB)"
}

copy_from_image() {
  # 起一顆只 sleep 的 pod 跑該 image，把 /all-addons 裡指定的模組用 tar 管線直接送進來源站的 volume（不落地本機）
  local pod=fake-source-addon-src
  if ! "${K[@]}" get secret paas-registry >/dev/null 2>&1; then
    # The NotFound guard makes this create-only. Client-side apply would copy
    # the complete docker credential into last-applied-configuration as a
    # second readable Secret value.
    kubectl --context "$CTX" -n "${PULL_FROM%%/*}" get secret "${PULL_FROM#*/}" -o json \
      | jq '{apiVersion, kind, type, data, metadata: {name: "paas-registry"}}' | "${K[@]}" create -f - >/dev/null
  fi
  "${K[@]}" delete pod "$pod" --ignore-not-found --wait >/dev/null
  "${K[@]}" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $pod
spec:
  restartPolicy: Never
  imagePullSecrets: [{name: paas-registry}]
  containers:
    - name: src
      image: $FROM_IMAGE
      command: ["sleep", "900"]
      resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: 500m, memory: 256Mi}}
EOF
  "${K[@]}" wait --for=condition=Ready "pod/$pod" --timeout=5m >/dev/null
  # shellcheck disable=SC2086  # 模組清單已用 MODLIST 驗過，刻意展開成多個參數
  "${K[@]}" exec "$pod" -- tar -C /all-addons -cf - ${EXTRA_ADDONS//,/ } | "${K[@]}" exec -i "deploy/$T-odoo" -c odoo -- \
    tar -xf - --no-same-owner --no-same-permissions -m -C /mnt/extra-addons
  if [[ -n $DATADIR_ADDONS ]]; then
    # shellcheck disable=SC2086
    "${K[@]}" exec "$pod" -- tar -C /all-addons -cf - ${DATADIR_ADDONS//,/ } | "${K[@]}" exec -i "deploy/$T-odoo" -c odoo -- \
      sh -c "$DATADIR_UNPACK"
  fi
  "${K[@]}" delete pod "$pod" --wait=false >/dev/null
}

seed() {
  local odoo=(exec "deploy/$T-odoo" -c odoo --) modules
  if [[ -n $FROM_IMAGE ]]; then
    log "copy $EXTRA_ADDONS${DATADIR_ADDONS:+ and $DATADIR_ADDONS} from /all-addons of $FROM_IMAGE"
    copy_from_image
    if [[ -n $PIP_SPECS ]]; then
      log "pip install $PIP_SPECS"
      # shellcheck disable=SC2086  # 多個 requirement spec，刻意斷詞；noglob 讓 >= 之類不被當 glob
      (set -f; "${K[@]}" "${odoo[@]}" pip install --quiet --break-system-packages $PIP_SPECS)
    fi
    modules=point_of_sale,$EXTRA_ADDONS${DATADIR_ADDONS:+,$DATADIR_ADDONS}
  else
    log "copy fixture addons into /mnt/extra-addons and data_dir/addons/18.0"
    # 只打包模組目錄（不含 "."）：PVC 根目錄屬 root，odoo 使用者改不了它的 mode／mtime
    tar -C "$FIXTURES/extra-addons" -cf - fake_pos_enhancement | "${K[@]}" exec -i "deploy/$T-odoo" -c odoo -- \
      tar -xf - --no-same-owner --no-same-permissions -m -C /mnt/extra-addons
    tar -C "$FIXTURES/data-dir-addons" -cf - fake_apps_module | "${K[@]}" exec -i "deploy/$T-odoo" -c odoo -- \
      sh -c "$DATADIR_UNPACK"
    modules=point_of_sale,fake_pos_enhancement,fake_apps_module
  fi
  log "odoo -i $modules（首次會順便初始化 base）"
  "${K[@]}" "${odoo[@]}" odoo --config=/tmp/odoo.conf -d odoo -i "$modules" \
    --without-demo=all --stop-after-init --no-http --log-level=warn
  log "seed business data via odoo shell"
  cat <<'PY' | "${K[@]}" exec -i "deploy/$T-odoo" -c odoo -- odoo shell --config=/tmp/odoo.conf -d odoo --no-http --log-level=warn
import base64
import random

rnd = random.Random(1604)
partners = env['res.partner'].create([
    {'name': '測試客戶 %d Ünïcode' % i, 'email': 'c%d@example.invalid' % i, 'phone': '09%08d' % i}
    for i in range(200)])
env['product.product'].create([
    {'name': '商品 %d' % i, 'list_price': i * 10 + 0.5, 'available_in_pos': True} for i in range(50)])
atts = env['ir.attachment'].create([
    {'name': 'file%d.bin' % i, 'datas': base64.b64encode(rnd.randbytes(1024 * (i + 1))),
     'res_model': 'res.partner', 'res_id': partners[i].id} for i in range(30)])
env['ir.attachment'].create({'name': 'big.bin', 'datas': base64.b64encode(rnd.randbytes(3 * 1024 * 1024))})
if 'fake.pos.note' in env:
    env['fake.pos.note'].create([
        {'name': 'note %d' % i, 'body_md': '# 標題 %d\n\n- item' % i, 'amount': i / 7,
         'partner_id': partners[i].id, 'attachment_ids': [(6, 0, atts[i:i + 2].ids)]} for i in range(20)])
env['ir.config_parameter'].sudo().set_param('web.base.url', 'https://fake-odoo.example.invalid')
env.cr.commit()
print('SEED-DONE')
PY
  log "vector extension＋非 ORM 表（驗證 restore 會帶到 ORM 以外的資料）"
  "${K[@]}" exec "deploy/$T-postgres" -- psql -v ON_ERROR_STOP=1 -U "$T" -d odoo -q -c \
    "CREATE EXTENSION IF NOT EXISTS vector;
     CREATE TABLE IF NOT EXISTS fake_embedding (id serial PRIMARY KEY, label text, emb vector(3));
     INSERT INTO fake_embedding (label, emb) SELECT 'v' || g, ('[' || g || ',' || g * 2 || ',' || g * 3 || ']')::vector
       FROM generate_series(1, 25) g WHERE NOT EXISTS (SELECT 1 FROM fake_embedding);"
  "${K[@]}" rollout restart "deploy/$T-odoo" >/dev/null
  "${K[@]}" rollout status "deploy/$T-odoo" --timeout=10m >/dev/null
  log "seed done"
}

down() {
  helm --kube-context "$CTX" -n "$NS" uninstall odoo-tenant --wait >/dev/null 2>&1 || true
  [[ $("${K[@]}" get ns "$NS" -o jsonpath='{.metadata.annotations.standalone-odoo-migrate/role}' 2>/dev/null) == fake-source ]] \
    || { echo "refuse: $NS is not a fake-source namespace" >&2; exit 2; }
  "${K[@]}" delete ns "$NS" --wait=false
}

"$CMD"
