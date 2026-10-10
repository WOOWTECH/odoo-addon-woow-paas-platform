#!/usr/bin/env bash
# fingerprint.sh — 來源 standalone Odoo 站的資料指紋（standalone-odoo-migrate skill，#1604）
#
# ⛔ 執行者＝woow-machines-operator（WMO）；理由同 inventory.sh。交付時連同 ../odoo_inspect.py、../fingerprint.sql、lib.sh。
#
# 用法：
#   fingerprint.sh --context <ctx> --ns <ns> [--odoo-workload <deploy/name>] [--db <name>] [--out-prefix <path>]
#
# 時機：最終 dump 的同一個凍結窗口內——POS 關帳、odoo 程序不再寫入之後、pg_dump 之前或之後（中間不能有寫入）。
#   odoo 工作負載必須還有 pod 在跑（exec 的落點）；凍結的作法是擋流量＋停 cron，不是 scale 0。
# 做什麼（唯讀）：odoo 容器內以 psql 跑 fingerprint.sql（REPEATABLE READ READ ONLY），並對 filestore/<db> 逐檔 sha256。
# 輸出：<prefix>.fp（DB 指紋）、<prefix>.fs（filestore 清單，sha256sum 格式）；預設 prefix ./fingerprint-<ns>-<UTC>。
#   兩個檔都要跟著 dump 一起交回，目標端還原後用 fingerprint_diff.py 比對。
# exit：0 成功、2 參數或探索失敗、3 exec 失敗或輸出解析失敗
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INSPECT="$SCRIPT_DIR/../odoo_inspect.py"
SQL="$SCRIPT_DIR/../fingerprint.sql"
CTX="" NS="" WORKLOAD="" DB="" PREFIX=""

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --odoo-workload) WORKLOAD=$2; shift 2 ;;
    --db) DB=$2; shift 2 ;;
    --out-prefix) PREFIX=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS ]] || { usage >&2; die 2 "--context and --ns are required"; }
command -v jq >/dev/null || die 2 "jq is required"
[[ -r $INSPECT && -r $SQL ]] || die 2 "missing odoo_inspect.py or fingerprint.sql next to source/"
PREFIX=${PREFIX:-./fingerprint-$NS-$(date -u +%Y%m%dT%H%M%SZ)}
K=(kubectl --context "$CTX" -n "$NS")

discover_odoo "$("${K[@]}" get deploy,sts -o json)" "$WORKLOAD"
echo "exec: $WORKLOAD -c $CONTAINER — fingerprint (read-only)" >&2
json=$(inspect_in_pod K fingerprint --sql-b64 "$(base64 -w0 < "$SQL")" ${DB:+--db "$DB"})
jq -r '.db' <<<"$json" > "$PREFIX.fp"
jq -r '.fs' <<<"$json" > "$PREFIX.fs"
grep -q '^table|' "$PREFIX.fp" || die 3 "$PREFIX.fp has no table lines"
echo "fingerprint: DB $(jq -r .db_name <<<"$json") — $(grep -c '^table|' "$PREFIX.fp") tables, $(grep -c . "$PREFIX.fs") filestore files" >&2
echo "$PREFIX.fp"
echo "$PREFIX.fs"
