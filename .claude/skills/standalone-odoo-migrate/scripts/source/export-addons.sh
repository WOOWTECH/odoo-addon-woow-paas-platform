#!/usr/bin/env bash
# export-addons.sh — 把來源 standalone Odoo 站的非 core addon 原始碼導出到本機（standalone-odoo-migrate skill，#1604）
#
# ⛔ 執行者＝woow-machines-operator（WMO）；理由同 inventory.sh。交付時連同 lib.sh、../odoo_inspect.py、../addons_unpack.py。
#
# 用法：
#   export-addons.sh --context <ctx> --ns <ns> --inventory <inventory-<ns>-….json> --out-dir <新目錄>
#                    [--modules a,b] [--odoo-workload <deploy/name|sts/name>]
#
# 前置：inventory.sh 剛跑過（列出非 core 模組、內容 hash、root_symlink；#1844 以前的盤點檔遇 symlink 模組會 exit 4）。
# 做什麼（全部唯讀，來源 ns 不建立任何資源）：
#   1. 從盤點檔取非 core 模組（預設全部；--modules 只取指定的），依 addon 根目錄分組。
#   2. 每個根目錄一次 exec 串流 tar 到本機（不含 .git／__pycache__／*.pyc）；根目錄本身是 symlink 的模組各自一次 exec（lib.sh）。
#   3. 本機解開（../addons_unpack.py：拒收不安全路徑），用與盤點同一支函式重算每個模組的內容 hash 並逐一比對。
#      不一致＝串流被截斷或盤點後原始碼被改 ⇒ 失敗，不留半套結果。
# 輸出：<out-dir>/addons/<模組>/…、<out-dir>/addons-export.json；stdout 最後一行印 <out-dir>。
#   原始碼是客戶的程式碼：留在本機交給 agent，不要貼進 issue／PR 留言。
# exit：0 成功、2 參數或前置條件不符、3 exec 或解包失敗、4 內容 hash 與盤點不一致
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNPACK="$SCRIPT_DIR/../addons_unpack.py"
CTX="" NS="" INVENTORY="" OUT_DIR="" MODULES="" WORKLOAD=""

usage() { sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

while [[ $# -gt 0 ]]; do
  case $1 in
    --context) CTX=$2; shift 2 ;;
    --ns) NS=$2; shift 2 ;;
    --inventory) INVENTORY=$2; shift 2 ;;
    --out-dir) OUT_DIR=$2; shift 2 ;;
    --modules) MODULES=$2; shift 2 ;;
    --odoo-workload) WORKLOAD=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die 2 "unknown argument: $1" ;;
  esac
done
[[ -n $CTX && -n $NS && -n $INVENTORY && -n $OUT_DIR ]] \
  || { usage >&2; die 2 "--context, --ns, --inventory and --out-dir are required"; }
command -v jq >/dev/null || die 2 "jq is required"
[[ -r $UNPACK ]] || die 2 "missing $UNPACK (copy addons_unpack.py and odoo_inspect.py next to source/)"
[[ -r $INVENTORY ]] || die 2 "cannot read $INVENTORY"
[[ $(jq -r '.source.namespace // empty' "$INVENTORY") == "$NS" ]] \
  || die 2 "$INVENTORY is not an inventory of namespace $NS"
if [[ -e $OUT_DIR ]] && [[ -n $(ls -A "$OUT_DIR") ]]; then die 2 "$OUT_DIR is not empty (use a new directory)"; fi
K=(kubectl --context "$CTX" -n "$NS")

discover_odoo "$("${K[@]}" get deploy,sts -o json)" "$WORKLOAD"

# 模組清單：盤點檔內有 digest 的（非 core）；每行 <根目錄>\t<模組>\t<root_symlink 或空字串>
mapfile -t rows < <(jq -r --arg only "$MODULES" '
  ($only | split(",") | map(select(length > 0))) as $want
  | .inspect.addons.modules | to_entries[] | select(.value.digest)
  | select(($want | length) == 0 or (.key as $k | $want | index($k)))
  | "\(.value.dir)\t\(.key)\t\(.value.root_symlink // "")"' "$INVENTORY" | sort)
(( ${#rows[@]} > 0 )) || die 2 "no non-core module to export (check --modules against the inventory)"
mapfile -t dirs < <(printf '%s\n' "${rows[@]}" | cut -f1 | sort -u)

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
pairs=()
n=0
for dir in "${dirs[@]}"; do
  # 根目錄是實體目錄的模組：一個 addon 根目錄一次 exec
  mapfile -t names < <(printf '%s\n' "${rows[@]}" | awk -F'\t' -v d="$dir" '$1 == d && $3 == "" {print $2}')
  if (( ${#names[@]} > 0 )); then
    echo "exec: $WORKLOAD -c $CONTAINER — tar ${#names[@]} module(s) under $dir (read-only)" >&2
    mapfile -t cmd < <(addon_tar_cmd "$dir" "${names[@]}")
    "${K[@]}" exec "$WORKLOAD" -c "$CONTAINER" -- "${cmd[@]}" > "$TMP/$n.tar" || die 3 "tar in $WORKLOAD failed for $dir"
    pairs+=("$dir=$TMP/$n.tar"); n=$((n + 1))
  fi
  # 根目錄本身是 symlink 的模組（#1844）：各自一次 exec，打包連結指向的內容
  while IFS=$'\t' read -r _ name target; do
    echo "exec: $WORKLOAD -c $CONTAINER — tar $dir/$name (symlink -> $target, read-only)" >&2
    mapfile -t cmd < <(addon_tar_linked_cmd "$dir" "$name")
    "${K[@]}" exec "$WORKLOAD" -c "$CONTAINER" -- "${cmd[@]}" > "$TMP/$n.tar" \
      || die 3 "tar in $WORKLOAD failed for $dir/$name (needs GNU tar for --transform)"
    pairs+=("$dir=$TMP/$n.tar"); n=$((n + 1))
  done < <(printf '%s\n' "${rows[@]}" | awk -F'\t' -v d="$dir" '$1 == d && $3 != ""')
done

mkdir -p "$OUT_DIR"
rc=0
python3 "$UNPACK" --inventory "$INVENTORY" --out "$OUT_DIR" ${MODULES:+--modules "$MODULES"} "${pairs[@]}" || rc=$?
if (( rc != 0 )); then
  rm -rf "$OUT_DIR/addons"   # 不留沒驗過的原始碼
  exit "$rc"
fi
echo "$OUT_DIR"
