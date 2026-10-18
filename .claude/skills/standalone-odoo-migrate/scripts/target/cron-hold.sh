#!/usr/bin/env bash
# cron-hold.sh — 查看或手動放開 restore.sh 暫停的排程（standalone-odoo-migrate skill，#1604）
#
# 執行者＝本 repo agent（目標 ns 必須在白名單；prod 的 release 是寫入，人工放行）。
#
# 用法：
#   cron-hold.sh status  --context <ctx> --ns <target-ns> --release <R>
#   cron-hold.sh release --context <ctx> --ns <target-ns> --release <R>
#
# 平常由 verify.sh --release-crons／--cleanup 在比對通過後放開；這支給比對沒過、但差異已查明可以接受，
# 或要提早恢復排程的時候用。走 R-postgresql-0 容器內的 psql（127.0.0.1，postgres 角色），SQL 見 cron-release.sql。
# exit：0 成功、2 參數或前置條件不符、3 psql 失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CMD=${1:-}; shift || true
CTX="" NS="" RELEASE=""

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ERROR: $2" >&2; exit "$1"; }
while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --release) RELEASE=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ $CMD =~ ^(status|release)$ && -n $CTX && -n $NS && -n $RELEASE ]] \
  || { usage >&2; die 2 "status|release, --context, --ns and --release are required"; }
K=(kubectl --context "$CTX" -n "$NS")
dep=$("${K[@]}" get deploy "$RELEASE" -o json) || die 2 "deployment $RELEASE not found"
db=$(jq -r '[.. | objects | select(.name? == "ODOO_DB_NAME") | .value] | first // empty' <<<"$dep")
[[ -n $db ]] || die 2 "cannot find ODOO_DB_NAME in deploy/$RELEASE"
PSQL=("${K[@]}" exec -i "$RELEASE-postgresql-0" -c postgresql -- psql -h 127.0.0.1 -U postgres -d "$db" -X -q -tA -v ON_ERROR_STOP=1)

if [[ $CMD == status ]]; then
  out=$("${PSQL[@]}" -c "SELECT coalesce((SELECT value::json->>'until' FROM ir_config_parameter
                                         WHERE key = 'standalone_odoo_migrate.cron_hold'), '')") || die 3 "psql failed"
  guards=$("${PSQL[@]}" -c "SELECT count(*) FROM pg_trigger WHERE tgname = 'standalone_odoo_migrate_cron_hold'") \
    || die 3 "psql failed"
  if [[ -z $out ]]; then
    echo "no cron hold on $RELEASE/$db"
  else
    held=$("${PSQL[@]}" -c "SELECT count(*) FROM ir_cron WHERE active AND nextcall = '$out'::timestamp") || die 3 "psql failed"
    parked=$("${PSQL[@]}" -c "SELECT count(*) FROM ir_cron_trigger WHERE call_at = '$out'::timestamp") || die 3 "psql failed"
    expired=$("${PSQL[@]}" -c "SELECT (now() AT TIME ZONE 'UTC') >= '$out'::timestamp") || die 3 "psql failed"
    if [[ $expired == t ]]; then
      echo "cron hold on $RELEASE/$db expired at $out UTC (crons are running again)"
    else
      echo "crons held until $out UTC on $RELEASE/$db ($held cron(s), $parked trigger(s) still parked)"
    fi
  fi
  # 暫停過期或沒有 hold 時 trigger 已經不起作用，但還掛在客戶 DB 上 ⇒ 提示用 release 清掉
  (( guards == 0 )) || echo "hold guard triggers present on ir_cron/ir_cron_trigger: $guards (release removes them)"
  exit 0
fi
out=$("${PSQL[@]}" -f - < "$SCRIPT_DIR/cron-release.sql") || die 3 "psql failed"
IFS='|' read -r _ had n_crons n_triggers n_late_crons n_late_triggers <<<"$out"
if [[ $had == 0 ]]; then
  echo "no cron hold on $RELEASE/$db (hold guard triggers removed if any)"
else
  echo "released on $RELEASE/$db: $n_crons cron(s), $n_triggers trigger(s) restored to their original nextcall;" \
    "$n_late_crons cron(s), $n_late_triggers trigger(s) parked during the hold set to now"
fi
