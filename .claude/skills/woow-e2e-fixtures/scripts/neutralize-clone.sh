#!/usr/bin/env bash
#
# neutralize-clone.sh — 對 platform lockdown 沙盒裡的 prod clone 做中和（#1206；裁決 #1162 留言 14057）
#
# 純 psql、冪等、同一個交易：
#   刪 dummy mail server（保冪等）
#   → 官方 SQL：同一張 image 起一次性 Job 跑 `odoo neutralize --stdout`（含 #1201 的 src/data/neutralize.sql；
#     image 若早於 #1201 就補 repo 的版本）
#   → 補差 SQL（prod-clone/supplement.sql）
#   → 沙盒密碼：明文只存在沙盒 Secret <slug>-prod-clone-login；hash 在叢集內用 image 的 passlib 算
#     （pbkdf2_sha512、rounds 600000），只換掉原本有密碼的使用者
#   →（選配）預寫 woow_paas_platform.upgrade_done（charts#81 R2-Q1；屬 Stage A）
# 結尾跑斷言 gate（prod-clone/assertions.toml），未通過 exit 1。
#
# Usage:
#   neutralize-clone.sh <slug> --context <ctx> [--db woow_prod] [--write-upgrade-marker] [--skip-gate]
#
# 不變式（#1162）：任何會啟動 Odoo 程序的步驟之後都要再過一次 gate ⇒ Odoo 起來後再跑一次本腳本（Stage B）。
# web 已起時可以跑：SQL 結尾會推進 base_*_signaling sequence，讓 worker 丟掉快取。
set -euo pipefail

SLUG="" CTX="" DB="woow_prod" WRITE_MARKER=0 SKIP_GATE=0
usage() { sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --context) CTX="$2"; shift 2 ;;
        --db) DB="$2"; shift 2 ;;
        --write-upgrade-marker) WRITE_MARKER=1; shift ;;
        --skip-gate) SKIP_GATE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 1 ;;
        *) [[ -z "$SLUG" ]] || { echo "多餘的參數：$1" >&2; exit 1; }; SLUG="$1"; shift ;;
    esac
done
[[ "$DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || { echo "--db '${DB}' 不合法" >&2; exit 1; }

# shellcheck source=prod-clone/lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prod-clone/lib.sh"
PC_WORK="$(mktemp -d)"; chmod 700 "$PC_WORK"
trap 'rm -rf "$PC_WORK"' EXIT
pc_init "$SLUG" "$CTX"
pc_db_exists "$DB" || pc_die "DB ${DB} 不存在於 ${PG_POD}"
pc_info "web／cron 狀態（replicas pods）：server=$(pc_component_state server) cron=$(pc_component_state cron)"

# --- 沙盒登入密碼 Secret（明文只在這裡；不上 argv、不進 log）-----------------------------------
LOGIN_SECRET="$(pc_login_secret)"
if ! kubectl get secret -n "$NAMESPACE" "$LOGIN_SECRET" >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(24), end="")' > "$PC_WORK/pw"
    kubectl create secret generic "$LOGIN_SECRET" -n "$NAMESPACE" --from-file=password="$PC_WORK/pw" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl label secret -n "$NAMESPACE" "$LOGIN_SECRET" app.kubernetes.io/managed-by=prod-clone --overwrite >/dev/null
    rm -f "$PC_WORK/pw"
    pc_ok "已建沙盒登入密碼 Secret ${LOGIN_SECRET}（明文只在 Secret）"
fi

# --- 官方中和 SQL：同 image 的一次性 Job -------------------------------------------------------
JOB="${RELEASE}-prod-clone-neutralize"
kubectl delete job -n "$NAMESPACE" "$JOB" --ignore-not-found --wait=true >/dev/null
kubectl get deploy -n "$NAMESPACE" -l "app.kubernetes.io/instance=${RELEASE},app.kubernetes.io/component=server" \
    -o json | python3 -c 'import json,sys; d=json.load(sys.stdin)["items"]; assert len(d)==1, "server Deployment 不是剛好一個"; json.dump(d[0], sys.stdout)' \
    > "$PC_WORK/deploy.json" || pc_die "找不到 server Deployment"
python3 "$PC_TOOL" neutralize-job --name "$JOB" --db "$DB" --login-secret "$LOGIN_SECRET" < "$PC_WORK/deploy.json" \
    | kubectl apply -f - >/dev/null
pc_info "等待 ${JOB}（odoo neutralize --stdout）..."
if ! kubectl wait -n "$NAMESPACE" --for=condition=complete "job/${JOB}" --timeout=600s >/dev/null 2>&1; then
    kubectl logs -n "$NAMESPACE" "job/${JOB}" --tail=50 >&2 || true
    pc_die "${JOB} 沒有成功完成（DB 未變動）"
fi
kubectl logs -n "$NAMESPACE" "job/${JOB}" > "$PC_WORK/job.log"
kubectl delete job -n "$NAMESPACE" "$JOB" --wait=false >/dev/null
python3 "$PC_TOOL" extract --sql-out "$PC_WORK/official.sql" --pwhash-out "$PC_WORK/pwhash" < "$PC_WORK/job.log" \
    > "$PC_WORK/extract.txt" || pc_die "解析 Job 輸出失敗（DB 未變動）"
rm -f "$PC_WORK/job.log"
CORE_DATE="$(sed -n 's/^CORE_DATE=//p' "$PC_WORK/extract.txt")"
HAS_PLATFORM="$(sed -n 's/^HAS_PLATFORM_SQL=//p' "$PC_WORK/extract.txt")"
pc_ok "官方 SQL $(wc -l < "$PC_WORK/official.sql") 行（sha256 $(sha256sum "$PC_WORK/official.sql" | cut -c1-12)），image core ${CORE_DATE:-?}"

# Job 第一次算出 hash ⇒ 存回 Secret（之後 Stage B 沿用，hash 不再變）
if [[ -s "$PC_WORK/pwhash" ]]; then
    kubectl get secret -n "$NAMESPACE" "$LOGIN_SECRET" -o jsonpath='{.data.password}' | base64 -d > "$PC_WORK/pw"
    kubectl create secret generic "$LOGIN_SECRET" -n "$NAMESPACE" --from-file=password="$PC_WORK/pw" \
        --from-file=password-hash="$PC_WORK/pwhash" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl label secret -n "$NAMESPACE" "$LOGIN_SECRET" app.kubernetes.io/managed-by=prod-clone --overwrite >/dev/null
    rm -f "$PC_WORK/pw" "$PC_WORK/pwhash"
fi
PWHASH="$(kubectl get secret -n "$NAMESPACE" "$LOGIN_SECRET" -o jsonpath='{.data.password-hash}' | base64 -d)"
[[ "$PWHASH" == '$pbkdf2-sha512$'* ]] || pc_die "沙盒密碼 hash 不是 pbkdf2-sha512（Secret ${LOGIN_SECRET}）"

# --- 平台段：image 早於 #1201 就補 repo 的 src/data/neutralize.sql --------------------------------
PLATFORM_SQL=""
if [[ "$HAS_PLATFORM" != 1 ]]; then
    PLATFORM_SQL="${PC_PROJECT_ROOT}/src/data/neutralize.sql"
    [[ -f "$PLATFORM_SQL" ]] && head -1 "$PLATFORM_SQL" | grep -qF -- '-- woow_paas_platform neutralize (#1201)' \
        || pc_die "image 的官方 SQL 沒有平台段，repo 也沒有 #1201 的 src/data/neutralize.sql —— 不能只做一半"
    pc_warn "image 早於 #1201：平台段改用 repo 的 ${PLATFORM_SQL#"$PC_PROJECT_ROOT"/}"
fi

# --- upgrade_done marker（選配；charts#81 R2-Q1：digest＝image digest、core_date＝image 內 dpkg 版本）-----
MARKER_SQL=""
if [[ "$WRITE_MARKER" == 1 ]]; then
    DIGEST="$(python3 -c 'import json,sys; c=[x for x in json.load(open(sys.argv[1]))["spec"]["template"]["spec"]["containers"] if x["name"]=="odoo"][0]; print(c["image"].split("@",1)[1] if "@" in c["image"] else "")' "$PC_WORK/deploy.json")"
    [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || pc_die "server Deployment 的 odoo image 沒有 @sha256 digest，無法預寫 marker"
    [[ "$CORE_DATE" =~ ^[0-9]+\.[0-9]+\.[0-9]{8}$ ]] || pc_die "image core_date '${CORE_DATE}' 不是 18.0.YYYYMMDD"
    MARKER_VALUE="{\"digest\":\"${DIGEST}\",\"core_date\":\"${CORE_DATE}\"}"
    MARKER_SQL="INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES ('woow_paas_platform.upgrade_done', '${MARKER_VALUE}', 1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;"
    pc_info "預寫 upgrade_done＝${MARKER_VALUE}（不寫 upgrade_attempt：缺席視為等於 done）"
fi

# --- 組成單一交易並套用 --------------------------------------------------------------------------
{
    echo "BEGIN;"
    echo "DELETE FROM ir_mail_server WHERE name = 'neutralization - disable emails';"
    echo "-- ===== official: odoo neutralize --stdout ====="
    cat "$PC_WORK/official.sql"
    if [[ -n "$PLATFORM_SQL" ]]; then
        echo "-- ===== platform (repo, image predates #1201) ====="
        cat "$PLATFORM_SQL"; echo
    fi
    echo "-- ===== supplement ====="
    cat "${PC_DIR}/supplement.sql"
    echo "-- ===== sandbox password（只換原本有密碼的使用者；id 1＝__system__ 不動）====="
    printf 'UPDATE res_users SET password = $pw$%s$pw$ WHERE id <> 1 AND password IS NOT NULL AND password <> $pw$%s$pw$;\n' "$PWHASH" "$PWHASH"
    [[ -z "$MARKER_SQL" ]] || printf '%s\n' "$MARKER_SQL"
    echo "COMMIT;"
} > "$PC_WORK/neutralize.sql"
pc_info "套用中和 SQL 到 ${DB}（單一交易；sha256 $(sha256sum "$PC_WORK/neutralize.sql" | cut -c1-12)）..."
if ! pc_psql "$DB" < "$PC_WORK/neutralize.sql" > /dev/null 2> "$PC_WORK/apply.err"; then
    sed 's/^/    /' "$PC_WORK/apply.err" >&2
    pc_die "中和 SQL 失敗，交易已回滾（DB 維持原狀）"
fi
grep -o 'prod-clone neutralize: skip.*' "$PC_WORK/apply.err" | sort | uniq -c | sed 's/^/    /' || true
rm -f "$PC_WORK/neutralize.sql"
pc_ok "中和 SQL 已提交"

[[ "$SKIP_GATE" == 1 ]] && { pc_warn "--skip-gate：沒有跑斷言 gate（不得據此起 Odoo）"; exit 0; }
pc_apply_marker_np
pc_gate "$DB"
