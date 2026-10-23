#!/usr/bin/env bash
# cloud-service.sh — 在平台 Odoo pod 以 odoo shell 操作一台 cloud_service（standalone-odoo-migrate skill，#1604）
#
# 執行者＝人。prod 平台 pod 的 odoo shell 屬 prod 寫入；agent 在 auto mode 下會被擋，也不要繞過——把整行指令交給人用 `!` 跑。
#
# 用法：
#   cloud-service.sh status|suspend|resume|redeploy --context <ctx> --id <cloud_service id> --name <服務名稱> --release <svc-…>
#   cloud-service.sh conf ...（同上）[--limit-time-cpu N] [--limit-time-real N] [--server-wide-modules <csv>]
#                    [--platform-ns paas-system] [--deploy paas-odoo-platform-cron] [--db woow_prod]
#
# 先比對 --id 那台服務的名稱與 release，有一個不符就什麼都不做。動作（括號內是允許的前一個 state）：
#   status    唯讀：state、訂閱狀態、conf 三個鍵、每日訂閱生命週期排程的下次執行時間（停權到復權不要跨過它）
#   suspend   _do_suspend()：web 與 postgres 縮到 0、對外網址改指停權頁（running）
#   resume    _do_resume()：全部開回 1、網址改回服務（suspended）
#   redeploy  action_redeploy()：重新套用範本釘的 chart 版本與 helm_values，觸發部署後初始化（running）
#   conf      把給定的 conf.limitTimeCpu／limitTimeReal／serverWideModules 寫進 helm_values，再 redeploy（running）
# redeploy／conf 是非同步的：之後用 status 等 state 回到 running。
# stg 平台（cd#289 起由 Fleet 管理）：--platform-ns paas-stg-system --deploy paas-odoo-platform-stg --db woow_stg
# 走 deploy/<name> 而不是 pod 名稱：平台升版後 pod 會換名。實作在同目錄 cloud_service_ops.py。
# 輸出平台回的 JSON（conf 只印三個鍵，不印 helm_values 裡的密碼）。
# exit：0 成功、1 動作失敗或 state=error（JSON 的 error／error_message）、2 參數錯、3 kubectl 或 odoo shell 沒有回 JSON
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
usage() { sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
case ${1:-} in -h|--help) usage; exit 0 ;; esac
CMD=${1:-}; shift || true
CTX="" ID="" NAME="" RELEASE="" PNS=paas-system DEPLOY=paas-odoo-platform-cron DB=woow_prod
CPU="" REAL="" SWM=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --id) ID=$2; shift 2 ;;
    --name) NAME=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    --platform-ns) PNS=$2; shift 2 ;;
    --deploy) DEPLOY=$2; shift 2 ;;
    --db) DB=$2; shift 2 ;;
    --limit-time-cpu) CPU=$2; shift 2 ;;
    --limit-time-real) REAL=$2; shift 2 ;;
    --server-wide-modules) SWM=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ $CMD =~ ^(status|suspend|resume|redeploy|conf)$ ]] || { usage >&2; die 2 "action must be status|suspend|resume|redeploy|conf"; }
[[ -n $CTX && -n $NAME && -n $RELEASE ]] || die 2 "--context, --id, --name and --release are required"
[[ $ID =~ ^[0-9]+$ ]] || die 2 "--id must be a cloud_service id (integer)"
for v in "$CPU" "$REAL"; do [[ -z $v || $v =~ ^[0-9]+$ ]] || die 2 "--limit-time-cpu／--limit-time-real must be integers (seconds)"; done
if [[ $CMD == conf ]]; then
  [[ -n $CPU || -n $REAL || -n $SWM ]] || die 2 "conf needs --limit-time-cpu, --limit-time-real or --server-wide-modules"
elif [[ -n $CPU || -n $REAL || -n $SWM ]]; then
  die 2 "--limit-time-*／--server-wide-modules only apply to conf"
fi

args=$(jq -cn --arg action "$CMD" --argjson id "$ID" --arg name "$NAME" --arg release "$RELEASE" \
  --arg cpu "$CPU" --arg real "$REAL" --arg swm "$SWM" \
  '{action: $action, id: $id, name: $name, release: $release,
    conf: ({} + (if $cpu != "" then {limitTimeCpu: ($cpu | tonumber)} else {} end)
              + (if $real != "" then {limitTimeReal: ($real | tonumber)} else {} end)
              + (if $swm != "" then {serverWideModules: $swm} else {} end))}')
# 參數用 base64 帶進 odoo shell，名稱裡有引號或中文也不會壞
b64=$(printf '%s' "$args" | base64 | tr -d '\n')
rc=0
raw=$({ cat "$SCRIPT_DIR/cloud_service_ops.py"
        printf '\nimport base64 as _b64\nmain(env, json.loads(_b64.b64decode("%s").decode("utf-8")))\n' "$b64"; } \
  | kubectl --context "$CTX" -n "$PNS" exec -i "deploy/$DEPLOY" -c odoo -- \
      env USER=odoo odoo shell -c /etc/odoo/odoo.conf -d "$DB" --shell-interface=python --no-http 2>&1) || rc=$?
out=$(sed -n '/^---JSON-START---$/,/^---JSON-END---$/p' <<<"$raw" | sed '1d;$d')
if [[ -z $out ]]; then
  echo "kubectl／odoo shell exit $rc, no JSON result; last lines:" >&2
  tail -n 20 <<<"$raw" >&2
  exit 3
fi
jq . <<<"$out"
[[ $(jq -r '.ok' <<<"$out") == true ]] || exit 1
