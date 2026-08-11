#!/usr/bin/env bash
#
# deploy-stg post-check gate（#665）
# 驗證 sandbox DB 裡的 addon 版號真的追上了「pod 正在跑的那份 code」。
#
# ---------------------------------------------------------------------------
# 為什麼需要這道 gate
# ---------------------------------------------------------------------------
# deploy-stg 換 image **不會**升 addon：
#   - plugin create.sh 只下 `--set odoo.updateModules=web`（deployment args 恆為
#     `-u web`，不含 woow_paas_platform）
#   - create.sh 的 `-i <modules>` install 分支只在 DB **未初始化**時跑，既有 stg DB
#     整段被 skip
#   - `.sandbox/post-create.sh` 是純 psql seed，完全不碰 ir_module_module
# ⇒ 對既有 sandbox 而言，整條路徑沒有任何一步對 woow_paas_platform 下 `-u`。
#
# 過去幾次「換 image 就升上去了」是**僥倖**：前一輪的 stg-upgrade Job 把模組留在 DB 的
# `state='to upgrade'`，而 Odoo 只要看到這個狀態，任何後續啟動的 registry load 都會把它
# 撿起來跑完（不論該次啟動帶不帶 `-u`）。前一輪若乾淨收在 `installed`，同樣的 deploy
# 就會**靜默不升**——helm 綠燈、pod 1/1 Running、kubectl logs 沒有任何錯誤訊號，
# DB 版號卻停在舊版。這正是本 gate 要讓它變紅的東西。
#
# ---------------------------------------------------------------------------
# 判準
# ---------------------------------------------------------------------------
#   期望值 = **pod 正在跑的 image 內** __manifest__.py 的 version
#   實際值 = DB `ir_module_module.latest_version`，且 `state` 必須是 `installed`
#
# 🔴 期望值刻意取自 image 而非本機 repo working tree。理由：
#   (1) 判準要問的是「跑著的 code 與 DB schema 有沒有對上」——真相源是 pod 裡那份
#       code，不是我手上的 checkout。
#   (2) deploy-stg 的 CI 一律 clone **develop**（paas-odoo-ci build-image.yml 對
#       `stg-*` tag 的 fallback），與本機所在的 branch / worktree 無關。拿 working
#       tree 當期望值，在 feature branch 上會天天誤報 ⇒ gate 很快就會被無視。
#   (3) `--image` 直接指定既有 image 時，working tree 更是完全無關的第三方。
#   本機 repo 的版號仍會印出來當**參考值**（不是判準），因為「你部到的不是你手上的
#   code」也值得看見——但它不會讓本 gate 變紅，見 main() 尾段的說明。
#
# ---------------------------------------------------------------------------
# 失敗時**不**自動補跑升級
# ---------------------------------------------------------------------------
# #665 的事實鏈第 6 點記過一個危險窗口：odoo pod 已 1/1 Running 跑新 image 對外服務，
# DB 卻還在舊 schema。此時若 gate 自己去跑 `odoo -u ... --stop-after-init`，等於讓第二
# 個 registry 對同一顆 DB 做 migration，與正在服務的 pod 打架。而且既有的手動 upgrade
# Job recipe 有兩個非顯然的陷阱（ResourceQuota `limits.cpu`、filestore RWO PVC 需
# nodeSelector 同 node），不是一行指令能安全複製的。
# ⇒ 本 gate 只負責**讓失效可見**，修復手段指向
#    docs/guides/deployment/stg-db-manual-upgrade-recipe.md。
#
# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
#   verify-addon-version.sh [OPTIONS]
#
#   --slug <name>         sandbox slug（預設 stg）；namespace = dev-sandbox-<slug>
#   --namespace <ns>      直接指定 namespace（未同時給 --slug 時，slug 由 ns 反推）
#   --module <name>       Odoo 模組技術名（預設 woow_paas_platform）
#   --db <name>           DB 名（預設 <db-prefix> + slug 的 '-' 換成 '_'）
#   --db-prefix <p>       DB 名前綴（預設 woow_，與 deploy-stg 傳給 create.sh 的一致）
#   --db-user <u>         psql 使用者（預設 odoo）
#   --context <ctx>       kubectl context（預設 $KUBE_CONTEXT，再預設 woow-k3s）
#   --pod-image <image>   只認正在跑這顆 image 的 odoo pod（deploy-stg 會帶）
#   --addons-path <path>  容器內 addons 目錄（預設 /opt/bitnami/odoo/custom-addons）
#   --manifest <path>     改用本機 manifest 檔當期望值（不讀 image）
#   --expected <version>  直接指定期望版號（最高優先，不做任何取樣）
#   --repo <path>         本機 repo 根（只用來印參考版號；預設由本檔位置往上推）
#   --timeout <sec>       等待 DB 收斂的秒數（預設 300；0 = 只查一次不輪詢）
#   --interval <sec>      輪詢間隔秒數（預設 10）
#   --no-self-test        跳過判定層自測（除錯用；正常路徑不要用）
#   -h, --help            顯示用法
#
# Exit codes:
#   0  一致（state=installed 且版號吻合）
#   1  版號判定失敗（不一致 / state 不對 / 逾時未收斂）
#   2  取樣或環境錯誤（找不到 pod / 讀不到 manifest / 參數錯 / 自測紅燈）
#      —— 刻意與 1 分開，但**兩者都必須讓呼叫端失敗**：取樣不到 ≠ 沒問題。
#
# 🔴 fail-closed：查不到模組列、psql 無輸出、manifest 讀不出版號，一律當失敗處理，
#    絕不因為「查不到」就放行。壞掉的探針最愛回答「零／空／找不到」，而那個回答與
#    「一切正常」在寬鬆的判準下長得一模一樣。

set -euo pipefail

# --- Colors ---
VAV_RED='\033[0;31m'
VAV_GREEN='\033[0;32m'
VAV_YELLOW='\033[1;33m'
VAV_BLUE='\033[0;34m'
VAV_NC='\033[0m'

VAV_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 修復指引的單一出處（失敗訊息與 SKILL.md 都指向它）
VAV_REMEDY_DOC="docs/guides/deployment/stg-db-manual-upgrade-recipe.md"

# =============================================================================
# 純函式層（可被 source 進來單獨測；不碰 kubectl、不碰檔案系統以外的東西）
# =============================================================================

# extract_manifest_version —— 從 stdin 讀 __manifest__.py 內容，印出 version 值。
#
# 刻意不用 python3 + ast.literal_eval：deploy-stg 的前置工具清單沒有 python3，
# 為了一個取值加一條硬相依不划算。代價是要自己擋掉 regex 的兩種假答案，所以下面
# 三個判定缺一不可：
#   - 命中 0 個  → 失敗（不是「當作沒版號然後放行」）
#   - 命中 >1 個 → 失敗（無從得知哪個才是模組版號；寧可紅也不要猜）
#   - 值不是版號形態 → 失敗（擋掉抓到別的鍵或抓到半截字串）
#
# 失敗原因同時寫進 EXTRACT_REASON **並印到 stderr**。兩者都要的理由：呼叫端幾乎都寫成
# `v="$(extract_manifest_version ...)"`，那是 subshell，函式裡設的 EXTRACT_REASON 傳不
# 回去 —— 只靠變數的話，真正的原因會在 main 印出來時變成空字串（形態上像「沒有原因」）。
# shellcheck disable=SC2034  # 由 tests/test-verify-addon-version.sh source 進去讀
EXTRACT_REASON=""
_vav_extract_fail() {
    EXTRACT_REASON="$1"
    printf 'manifest 解析失敗：%s\n' "$1" >&2
    return 1
}
extract_manifest_version() {
    # 字元類寫成變數：manifest 的引號可能是 " 或 '，兩種都要吃。
    local q="[\"']"
    local re="^[[:space:]]*${q}version${q}[[:space:]]*:[[:space:]]*${q}([^\"']+)${q}"
    local line value="" count=0
    EXTRACT_REASON=""
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        if [[ "$line" =~ $re ]]; then
            count=$((count + 1))
            value="${BASH_REMATCH[1]}"
        fi
    done
    if [ "$count" -eq 0 ]; then
        _vav_extract_fail "manifest 內找不到 version 鍵"
        return 1
    fi
    if [ "$count" -gt 1 ]; then
        _vav_extract_fail "manifest 內有 ${count} 個 version 鍵，無法判定哪個是模組版號"
        return 1
    fi
    if [[ ! "$value" =~ ^[0-9]+(\.[0-9]+)+$ ]]; then
        _vav_extract_fail "version 值不是版號形態：'${value}'"
        return 1
    fi
    printf '%s\n' "$value"
    return 0
}

# parse_manifest_version <path> —— 檔案版的 extract_manifest_version。
parse_manifest_version() {
    local path="${1:-}"
    EXTRACT_REASON=""
    if [ -z "$path" ]; then
        _vav_extract_fail "parse_manifest_version：未提供路徑"
        return 1
    fi
    if [ ! -f "$path" ]; then
        _vav_extract_fail "manifest 檔不存在：${path}"
        return 1
    fi
    extract_manifest_version < "$path"
}

# evaluate_module_row <psql_row> <expected_version> —— 判定層。
#
# psql_row 是 `psql -tA` 的單列輸出，形態 `<state>|<latest_version>`；模組不存在時
# 是空字串。判定結果的人話寫進 EVAL_REASON（成功時也寫，讓綠燈也有可讀輸出）。
#   0 = 一致（可以放行）
#   1 = 尚未一致（呼叫端決定要繼續等還是判紅）
EVAL_REASON=""
evaluate_module_row() {
    local row="${1:-}" expected="${2:-}" state actual
    # 🔴 換行字元一定要用 $'\n' 這種**字面**寫法。寫成 "$(printf '\n')" 會被 command
    #    substitution 剝掉尾端換行變成空字串 ⇒ 底下的 pattern 退化成 `**`，於是**每一列**
    #    都被判成「多列」而永遠紅。假紅與假綠一樣糟，都會讓 gate 被繞過。
    local nl=$'\n'
    EVAL_REASON=""
    if [ -z "$expected" ]; then
        # 期望值取樣失敗卻還走到判定層 —— 一定要紅，不能拿空字串去比對「相等」。
        EVAL_REASON="期望版號為空（取樣失敗）—— 拒絕以空值當判準"
        return 1
    fi
    if [ -z "$row" ]; then
        EVAL_REASON="DB 查不到該模組的 ir_module_module 記錄（或查詢無輸出）"
        return 1
    fi
    case "$row" in
        *"$nl"*)
            EVAL_REASON="psql 回了多列（模組名應唯一，這代表查詢或 DB 不符預期）：${row}"
            return 1
            ;;
    esac
    case "$row" in
        *"|"*) ;;
        *)
            EVAL_REASON="無法解析 psql 輸出（缺少欄位分隔符 '|'）：'${row}'"
            return 1
            ;;
    esac
    state="${row%%|*}"
    actual="${row#*|}"
    if [ "$state" != "installed" ]; then
        # 'to upgrade' 代表升級還沒跑完（或卡住）；'uninstalled' 是 #85 那個家族。
        # 兩者都不是「已經升好」，一律不放行。
        EVAL_REASON="state='${state}'（不是 installed；latest_version='${actual}'）"
        return 1
    fi
    if [ -z "$actual" ]; then
        EVAL_REASON="state=installed 但 latest_version 為空"
        return 1
    fi
    if [ "$actual" != "$expected" ]; then
        EVAL_REASON="版號不一致：DB='${actual}' vs 期望='${expected}'"
        return 1
    fi
    EVAL_REASON="state=installed、latest_version=${actual}"
    return 0
}

# compute_db_name <slug> <prefix> —— 與 woow-dev plugin lib/common.sh 同一套規則。
compute_db_name_local() {
    local slug="${1:-}" prefix="${2:-}"
    printf '%s\n' "${prefix}$(printf '%s' "$slug" | sed 's/-/_/g')"
}

# =============================================================================
# 以下是取樣層 + main；被 source 時不執行（讓測試可以只拿上面的純函式）
# =============================================================================

vav_run_self_test() {
    local test_file="${VAV_SCRIPT_DIR}/../tests/test-verify-addon-version.sh"
    if [ ! -f "$test_file" ]; then
        # fail-closed：自測檔不見了 ⇒ 比對器處於「沒人證明它還會動」的狀態。
        # 一道從沒被看過會失敗的檢查，與一道什麼都沒檢查的檢查，輸出長得一模一樣。
        echo -e "${VAV_RED}找不到判定層自測檔：${test_file}${VAV_NC}" >&2
        echo -e "${VAV_YELLOW}（自測是本 gate 可信度的前提；真的要跳過請顯式加 --no-self-test）${VAV_NC}" >&2
        return 1
    fi
    if ! bash "$test_file" >/dev/null 2>&1; then
        echo -e "${VAV_RED}判定層自測未通過 —— 本 gate 的比對器本身有問題，其判定不可信。${VAV_NC}" >&2
        echo -e "${VAV_YELLOW}重跑看細節：bash ${test_file}${VAV_NC}" >&2
        return 1
    fi
    return 0
}

# 解析 odoo pod：印出 `<pod名>`。有 --pod-image 時只認跑那顆 image 的 Ready pod
# （rollout 中途 exec 到舊 pod 會讀到舊 image 的 manifest ⇒ 期望值取樣被舊 pod 汙染，
# 那正好會讓「沒升成」誤判成綠燈）。
vav_resolve_odoo_pod() {
    local rows row name images readies
    rows="$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get pods \
        -l "app.kubernetes.io/instance=${INSTANCE},app.kubernetes.io/component=odoo" \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.containers[*].image}{"\t"}{.status.containerStatuses[*].ready}{"\n"}{end}' \
        2>/dev/null || true)"
    [ -n "$rows" ] || return 1
    while IFS=$'\t' read -r name images readies; do
        [ -n "$name" ] || continue
        # 全部 container 都要 ready；readies 為空代表容器狀態還沒出來。
        [ -n "$readies" ] || continue
        case "$readies" in *false*) continue ;; esac
        if [ -n "$POD_IMAGE" ]; then
            local matched=0 img
            for img in $images; do
                [ "$img" = "$POD_IMAGE" ] && matched=1
            done
            [ "$matched" -eq 1 ] || continue
        fi
        printf '%s\n' "$name"
        return 0
    done <<< "$rows"
    return 1
}

vav_resolve_pg_workload() {
    local name
    name="$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" get statefulset \
        -l "app.kubernetes.io/instance=${INSTANCE},app.kubernetes.io/component=postgres" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
    [ -n "$name" ] || return 1
    printf '%s\n' "$name"
}

vav_query_module_row() {
    # psql -tA 的欄位分隔符預設就是 '|'，與 evaluate_module_row 的解析對齊。
    kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" exec "statefulset/${PG_WORKLOAD}" -- \
        psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        "SELECT state, latest_version FROM ir_module_module WHERE name='${MODULE}';" \
        2>/dev/null || true
}

vav_usage() {
    sed -n '/^# Usage$/,/^# 🔴 fail-closed/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

vav_main() {
    # 絆線：自測會斷言「source 進來時這個變數仍是 no」。若哪天檔尾那道
    # `[ "${BASH_SOURCE[0]}" = "${0}" ]` 守衛被改壞，測試會在**打叢集之前**先紅，
    # 而不是變成一支會偷偷連線的單元測試。
    # shellcheck disable=SC2034  # 由 tests/test-verify-addon-version.sh 斷言
    VAV_MAIN_RAN=yes

    # --- Defaults ---
    SLUG=""
    NAMESPACE=""
    INSTANCE=""
    MODULE="woow_paas_platform"
    DB_NAME=""
    DB_PREFIX="woow_"
    # psql 使用者是 odoo（不是 woow 也不是 postgres）。POSTGRES_USER 由 secret 注入，
    # `kubectl get sts -o jsonpath='{...env[*].value}'` 讀出來是空的（值在 valueFrom），
    # 要 `exec ... sh -c 'echo $POSTGRES_USER'` 才拿得到 —— 別再用前者去「確認」。
    DB_USER="odoo"
    KUBE_CONTEXT="${KUBE_CONTEXT:-woow-k3s}"
    POD_IMAGE=""
    ADDONS_PATH="/opt/bitnami/odoo/custom-addons"
    MANIFEST_OVERRIDE=""
    EXPECTED_OVERRIDE=""
    REPO_ROOT=""
    TIMEOUT=300
    INTERVAL=10
    SELF_TEST=true

    while [ $# -gt 0 ]; do
        case "$1" in
            --slug)         SLUG="$2"; shift 2 ;;
            --namespace)    NAMESPACE="$2"; shift 2 ;;
            --module)       MODULE="$2"; shift 2 ;;
            --db)           DB_NAME="$2"; shift 2 ;;
            --db-prefix)    DB_PREFIX="$2"; shift 2 ;;
            --db-user)      DB_USER="$2"; shift 2 ;;
            --context)      KUBE_CONTEXT="$2"; shift 2 ;;
            --pod-image)    POD_IMAGE="$2"; shift 2 ;;
            --addons-path)  ADDONS_PATH="$2"; shift 2 ;;
            --manifest)     MANIFEST_OVERRIDE="$2"; shift 2 ;;
            --expected)     EXPECTED_OVERRIDE="$2"; shift 2 ;;
            --repo)         REPO_ROOT="$2"; shift 2 ;;
            --timeout)      TIMEOUT="$2"; shift 2 ;;
            --interval)     INTERVAL="$2"; shift 2 ;;
            --no-self-test) SELF_TEST=false; shift ;;
            -h|--help)      vav_usage; return 0 ;;
            *)
                echo -e "${VAV_RED}Unknown option: $1${VAV_NC}" >&2
                return 2
                ;;
        esac
    done

    # --- 名稱推導 ---
    if [ -z "$SLUG" ] && [ -n "$NAMESPACE" ]; then
        # 只給 namespace 時反推 slug（create.sh 的慣例：ns = dev-sandbox-<slug>，
        # 且 helm instance label = slug）。推不出來就讓後面的 pod 解析 fail-loud。
        SLUG="${NAMESPACE#dev-sandbox-}"
    fi
    [ -n "$SLUG" ] || SLUG="stg"
    [ -n "$NAMESPACE" ] || NAMESPACE="dev-sandbox-${SLUG}"
    INSTANCE="$SLUG"
    [ -n "$DB_NAME" ] || DB_NAME="$(compute_db_name_local "$SLUG" "$DB_PREFIX")"
    if [ -z "$REPO_ROOT" ]; then
        REPO_ROOT="$(cd "${VAV_SCRIPT_DIR}/../../../.." && pwd)"
    fi

    echo ""
    echo -e "${VAV_BLUE}Post-check：addon 版號（#665）${VAV_NC}"
    echo -e "  Namespace: ${NAMESPACE}"
    echo -e "  Module:    ${MODULE}"
    echo -e "  Database:  ${DB_NAME}"

    # --- 判定層自測（每次跑都重新證明比較器還活著）---
    if [ "$SELF_TEST" = true ]; then
        if ! vav_run_self_test; then
            return 2
        fi
        echo -e "  Self-test: ${VAV_GREEN}判定層自測通過${VAV_NC}"
    else
        echo -e "  Self-test: ${VAV_YELLOW}已跳過（--no-self-test）${VAV_NC}"
    fi

    # --- 期望值取樣 ---
    local expected="" expected_src=""
    if [ -n "$EXPECTED_OVERRIDE" ]; then
        expected="$EXPECTED_OVERRIDE"
        expected_src="--expected（人工指定）"
    elif [ -n "$MANIFEST_OVERRIDE" ]; then
        # 失敗原因由 parse_manifest_version 自己印到 stderr（見該函式的註解：
        # 這裡是 subshell，靠 EXTRACT_REASON 傳回來會拿到空字串）。
        if ! expected="$(parse_manifest_version "$MANIFEST_OVERRIDE")"; then
            echo -e "${VAV_RED}讀不出期望版號（來源：${MANIFEST_OVERRIDE}）${VAV_NC}" >&2
            return 2
        fi
        expected_src="本機 manifest ${MANIFEST_OVERRIDE}"
    else
        local pod manifest_path content
        if ! pod="$(vav_resolve_odoo_pod)"; then
            echo -e "${VAV_RED}找不到可用的 odoo pod（ns=${NAMESPACE}, instance=${INSTANCE}${POD_IMAGE:+, image=${POD_IMAGE}}）${VAV_NC}" >&2
            echo -e "${VAV_YELLOW}期望版號取樣不到就沒有判準 —— 這是失敗，不是「沒問題」。${VAV_NC}" >&2
            return 2
        fi
        manifest_path="${ADDONS_PATH}/${MODULE}/__manifest__.py"
        if ! content="$(kubectl --context "$KUBE_CONTEXT" -n "$NAMESPACE" exec "$pod" -- \
                cat "$manifest_path" 2>/dev/null)"; then
            echo -e "${VAV_RED}讀不到 image 內的 manifest：${pod}:${manifest_path}${VAV_NC}" >&2
            echo -e "${VAV_YELLOW}（addons 路徑不同請用 --addons-path 指定；讀不到一律當失敗，不 fallback。）${VAV_NC}" >&2
            return 2
        fi
        if ! expected="$(printf '%s\n' "$content" | extract_manifest_version)"; then
            echo -e "${VAV_RED}image 內 manifest 解析失敗（來源：${pod}:${manifest_path}）${VAV_NC}" >&2
            return 2
        fi
        expected_src="image 內 ${pod}:${manifest_path}"
    fi
    echo -e "  Expected:  ${VAV_GREEN}${expected}${VAV_NC}  ← ${expected_src}"

    # --- 本機 repo 版號：**參考值，不是判準** ---
    # 印它是因為「你部到的不是你手上的 code」值得看見；不拿它當判準是因為 deploy-stg
    # 的 CI 一律 clone develop，在 feature branch 上與 working tree 天然不同 ⇒ 拿它
    # 判紅就是天天誤報，而會誤報的 gate 三次之後就沒人看了。
    local repo_manifest="${REPO_ROOT}/src/__manifest__.py" repo_version=""
    if repo_version="$(parse_manifest_version "$repo_manifest" 2>/dev/null)"; then
        if [ "$repo_version" != "$expected" ]; then
            echo -e "  ${VAV_YELLOW}參考：本機 repo manifest = ${repo_version}（與部署版本不同；非判準）${VAV_NC}"
        else
            echo -e "  參考：本機 repo manifest = ${repo_version}（相同）"
        fi
    fi

    # --- 實際值取樣（輪詢到收斂或逾時）---
    if ! PG_WORKLOAD="$(vav_resolve_pg_workload)"; then
        echo -e "${VAV_RED}找不到 postgres statefulset（ns=${NAMESPACE}, instance=${INSTANCE}）${VAV_NC}" >&2
        return 2
    fi

    local start_ts=$SECONDS attempt=0 row elapsed
    while :; do
        attempt=$((attempt + 1))
        row="$(vav_query_module_row)"
        row="${row%$'\r'}"
        if evaluate_module_row "$row" "$expected"; then
            echo -e "  Actual:    ${VAV_GREEN}${EVAL_REASON}${VAV_NC}"
            echo ""
            echo -e "${VAV_GREEN}✓ addon 版號 post-check 通過：DB 與 image 都是 ${expected}${VAV_NC}"
            return 0
        fi
        elapsed=$((SECONDS - start_ts))
        if [ "$elapsed" -ge "$TIMEOUT" ]; then
            break
        fi
        echo -e "  [${elapsed}s/${TIMEOUT}s] 尚未收斂：${EVAL_REASON}，${INTERVAL}s 後重查..."
        sleep "$INTERVAL"
    done

    # --- 紅燈 ---
    echo -e "  Actual:    ${VAV_RED}${EVAL_REASON}${VAV_NC}"
    echo ""
    echo -e "${VAV_RED}========================================${VAV_NC}"
    echo -e "${VAV_RED}  ✗ addon 版號 post-check 失敗（#665）${VAV_NC}"
    echo -e "${VAV_RED}========================================${VAV_NC}"
    echo -e "  期望（${expected_src}）：${expected}"
    echo -e "  實際（DB ${DB_NAME}.ir_module_module）：${EVAL_REASON}"
    echo ""
    echo -e "${VAV_YELLOW}deploy-stg 換 image **不會**對 ${MODULE} 下 -u —— 這不是這次才壞的，${VAV_NC}"
    echo -e "${VAV_YELLOW}是本來就沒有升級步驟。helm 綠燈與 pod Running 都不代表 addon 升了。${VAV_NC}"
    echo ""
    echo -e "${VAV_YELLOW}手動查證：${VAV_NC}"
    echo -e "  kubectl --context ${KUBE_CONTEXT} -n ${NAMESPACE} exec statefulset/${PG_WORKLOAD} -- \\"
    echo -e "    psql -U ${DB_USER} -d ${DB_NAME} -tAc \\"
    echo -e "    \"SELECT state, latest_version FROM ir_module_module WHERE name='${MODULE}';\""
    echo ""
    echo -e "${VAV_YELLOW}補升級（有 ResourceQuota 與 filestore RWO nodeSelector 兩個陷阱，照 recipe 走）：${VAV_NC}"
    echo -e "  ${VAV_REMEDY_DOC}"
    echo ""
    return 1
}

# source 進來時只提供純函式，不跑 main（測試靠這個）
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    vav_main "$@"
fi
