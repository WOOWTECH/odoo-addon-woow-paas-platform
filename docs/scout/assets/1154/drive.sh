#!/usr/bin/env bash
# ⚠ PROTOTYPE（#1154）— 拋棄式驅動腳本：用平台自己的 JSON-RPC API 走 odoo cloud_service 生命週期。
# 只量測、不修。每次呼叫都把 request／response 連同時間戳追加到 $LOG，事後當證據。
#
# 前置：另一個 shell 先開 port-forward 到沙盒 nginx：
#   kubectl --context woow-k3s -n dev-sandbox-odoo1154 port-forward svc/<nginx svc> 18069:80
# 用法：
#   BASE=http://127.0.0.1:18069 DB=<db> LOGIN=admin PASSWORD=<pw> ./drive.sh login
#   ./drive.sh rpc /api/workspaces '{"action":"list"}'
#   ./drive.sh create <ws_id> <template_id> <plan_id> <name>
#   ./drive.sh get <ws_id> <service_id>            # 同時會觸發平台端的 status poll／post-deploy init
#   ./drive.sh watch <ws_id> <service_id> [秒]      # 每 10s get 一次，印 state／init_retries／error
#   ./drive.sh update <ws_id> <service_id> '<json>' # 例：'{"values":{"smtpHost":"smtp.example.com"}}'
#   ./drive.sh check-removal <service_id> '[<addon_id>,...]'
#   ./drive.sh change-plan <service_id> <plan_id>
#   ./drive.sh delete <ws_id> <service_id>
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18069}"
HERE="$(cd "$(dirname "$0")" && pwd)"
JAR="${JAR:-$HERE/.cookies.txt}"
LOG="${LOG:-$HERE/run-log.jsonl}"

rpc() { # rpc <path> <params-json>
    local path="$1" params="${2:-{\}}" t0 t1 resp
    t0=$(date +%s.%N)
    resp=$(curl -sS -m 200 -b "$JAR" -c "$JAR" -H 'Content-Type: application/json' \
        -d "{\"jsonrpc\":\"2.0\",\"method\":\"call\",\"params\":${params}}" "${BASE}${path}")
    t1=$(date +%s.%N)
    python3 -W ignore - "$path" "$params" "$resp" "$t0" "$t1" >> "$LOG" <<'PY'
import json, sys, datetime
path, params, resp, t0, t1 = sys.argv[1:6]
try: body = json.loads(resp)
except Exception: body = {"_raw": resp[:4000]}
try: p = json.loads(params)
except Exception: p = params
for k in ("password",):  # 不把登入密碼寫進 log
    if isinstance(p, dict) and k in p: p[k] = "***"
print(json.dumps({"ts": datetime.datetime.utcnow().isoformat() + "Z", "path": path, "params": p,
                  "elapsed_s": round(float(t1) - float(t0), 2), "response": body}, ensure_ascii=False))
PY
    printf '%s\n' "$resp"
}

cmd="${1:?subcommand}"; shift || true
case "$cmd" in
    login)
        rpc /web/session/authenticate "{\"db\":\"${DB:?}\",\"login\":\"${LOGIN:?}\",\"password\":\"${PASSWORD:?}\"}" \
            | python3 -c 'import json,sys; r=json.load(sys.stdin).get("result",{}); print("uid:", r.get("uid"), "db:", r.get("db"))' ;;
    rpc)            rpc "$1" "${2:-{\}}" ;;
    create)         rpc "/api/workspaces/$1/services" "{\"action\":\"create\",\"template_id\":$2,\"plan_id\":$3,\"name\":\"$4\",\"values\":{\"odooEmail\":\"proto1154@example.com\",\"odooPassword\":\"${SVC_PASSWORD:-Prototype1154x}\"}}" ;;
    get)            rpc "/api/workspaces/$1/services/$2" '{"action":"get"}' ;;
    update)         rpc "/api/workspaces/$1/services/$2" "$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); d["action"]="update"; print(json.dumps(d))' "$3")" ;;
    check-removal)  rpc "/api/cloud-services/services/$1/check-addon-removal" "{\"removed_addon_ids\":$2}" ;;
    change-plan)    rpc "/api/cloud-services/services/$1/change-plan" "{\"plan_id\":$2}" ;;
    delete)         rpc "/api/workspaces/$1/services/$2" '{"action":"delete"}' ;;
    watch)
        end=$(( $(date +%s) + ${3:-1800} ))
        while [ "$(date +%s)" -lt "$end" ]; do
            rpc "/api/workspaces/$1/services/$2" '{"action":"get"}' | python3 -W ignore -c '
import json,sys,datetime
r=json.load(sys.stdin).get("result",{}); s=r.get("data",r) if isinstance(r,dict) else {}
s=s.get("service",s) if isinstance(s,dict) else {}
print(datetime.datetime.utcnow().strftime("%H:%M:%S"), {k:s.get(k) for k in ("state","init_retries","error_message","helm_revision")})'
            sleep 10
        done ;;
    *) echo "unknown subcommand: $cmd" >&2; exit 2 ;;
esac
