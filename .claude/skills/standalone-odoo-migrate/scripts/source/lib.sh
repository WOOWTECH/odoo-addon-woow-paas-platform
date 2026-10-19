# shellcheck shell=bash
# lib.sh — source/ 腳本共用函式（inventory.sh、fingerprint.sh 以 source 載入；standalone-odoo-migrate，#1604）

die() { echo "ERROR: $2" >&2; exit "$1"; }

# discover_odoo <deploy,sts 的 JSON> [明確指定的 deploy/<name>|sts/<name>]
# 設定 WORKLOAD（kubectl exec 目標）、WL_NAME、CONTAINER（開 8069 的容器）。找不到或不唯一 ⇒ exit 2。
discover_odoo() {
  local workloads=$1 explicit=${2:-} found
  if [[ -n $explicit ]]; then
    WORKLOAD=$explicit
  else
    mapfile -t found < <(jq -r '.items[]
      | select([.spec.template.spec.containers[].ports[]?.containerPort] | index(8069))
      | "\(.kind | ascii_downcase)/\(.metadata.name)"' <<<"$workloads" | sed 's#^deployment/#deploy/#;s#^statefulset/#sts/#')
    (( ${#found[@]} == 1 )) \
      || die 2 "expected exactly one workload exposing 8069, found ${#found[@]} (${found[*]:-none}); pass --odoo-workload"
    WORKLOAD=${found[0]}
  fi
  WL_NAME=${WORKLOAD#*/}
  CONTAINER=$(jq -r --arg n "$WL_NAME" '.items[] | select(.metadata.name == $n)
    | .spec.template.spec.containers[] | select([.ports[]?.containerPort] | index(8069)) | .name' <<<"$workloads" | head -1)
  [[ -n $CONTAINER ]] || die 2 "workload $WORKLOAD has no container exposing 8069"
}

# inspect_in_pod <kubectl 前綴陣列名> <odoo_inspect.py 參數…>
# 把 odoo_inspect.py 從 stdin 送進 odoo 容器執行，印出標記之間的 JSON；失敗 ⇒ exit 3。
inspect_in_pod() {
  local -n kref=$1; shift
  local raw json
  raw=$(cat "$INSPECT" | "${kref[@]}" exec -i "$WORKLOAD" -c "$CONTAINER" -- python3 - "$@" 2>&1) \
    || { printf '%s\n' "$raw" | tail -20 >&2; die 3 "exec failed"; }
  json=$(printf '%s\n' "$raw" | sed -n '/^---ODOO-INSPECT-JSON-START---$/,/^---ODOO-INSPECT-JSON-END---$/p' | sed '1d;$d')
  jq -e . >/dev/null <<<"$json" 2>/dev/null || { printf '%s\n' "$raw" | tail -20 >&2; die 3 "cannot parse odoo_inspect output"; }
  printf '%s\n' "$raw" | grep -E '^(connect|receiver) retry ' >&2 || true   # 成功時也留下重試紀錄
  printf '%s\n' "$json"
}

# 導出 addon 原始碼的 tar 指令（export-addons.sh 用；tests/test_addons_tar_cmd.py 也呼叫同一組函式驗證）。
# 每個參數印一行，呼叫端用 mapfile 讀回陣列。排除的範圍與 odoo_inspect.dir_digest 一致。
ADDON_TAR_EXCLUDES=(--exclude=.git --exclude=__pycache__ --exclude='*.pyc' --exclude='*.pyo' --exclude=.DS_Store)

# addon_tar_cmd <addon 根目錄> <模組…>：根目錄是實體目錄的模組，一個 addon 根目錄打包成一顆 tar
addon_tar_cmd() { local dir=$1; shift; printf '%s\n' tar -cf - "${ADDON_TAR_EXCLUDES[@]}" -C "$dir" -- "$@"; }

# addon_tar_linked_cmd <addon 根目錄> <模組>：模組根目錄本身是 symlink 時用（#1844）。
# 直接 tar <模組> 只會打包連結本身（GNU tar 也會去掉結尾的 /），解出來是 0 個檔。
# 不能全面加 -h：模組內部的 symlink 要照原樣保留，內容 hash 才會與盤點一致。
# 做法：-C 進到連結指向的實際目錄打包「.」，再把成員名稱開頭的「.」換回模組名；
# transform 的 S／H 旗標讓它不套用到 symlink／hardlink 的目標字串。需要 GNU tar。
addon_tar_linked_cmd() {
  local dir=$1 name=$2
  printf '%s\n' tar -cf - "${ADDON_TAR_EXCLUDES[@]}" "--transform=s,^\\.,$name,SH" -C "$dir/$name" .
}
