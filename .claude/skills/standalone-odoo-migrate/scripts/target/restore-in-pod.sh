#!/usr/bin/env bash
# restore-in-pod.sh — 把 standalone 站的匯出組還原進 woow-odoo release（standalone-odoo-migrate，#1604）
#
# 由 restore.sh 以 ConfigMap 掛進目標 ns 的一次性 Job（image＝release 的 postgres image：PG17 client、tar、coreutils）。
# 底稿是 woow-e2e-fixtures/scripts/prod-clone/restore-in-pod.sh；差異：允許 vector、可取代 chart 首裝的空庫、
# 還原後在 pod 內比對指紋，一致才預寫 upgrade_done marker。
#
# env：SET_DIR（/incoming/<set>）、ODOO_DB_NAME、PGHOST、PGPORT、PGSUPERUSER_PASSWORD、ODOO_DB_PASSWORD、DATA_ROOT、
#      FINGERPRINT_SQL、REPLACE_DB（＝ODOO_DB_NAME 才允許 DROP 既有 DB）、WRITE_MARKER（1／0）
# 輸入（export.sh 經 receiver 送進來）：db.dump、filestore.tar、manifest.json、source.fp、source.fs、SHA256SUMS
# 輸出：SET_DIR/target.fp、SET_DIR/target.fs、log 最後一行 RESTORE-DONE
# 每一步失敗都 exit 1、不寫 marker；odoo 沒有 marker 開機時升級閘會 exit 3，不會碰資料。
set -euo pipefail
die() { echo "RESTORE-FAIL: $*" >&2; exit 1; }
: "${SET_DIR:?}" "${ODOO_DB_NAME:?}" "${PGHOST:?}" "${PGSUPERUSER_PASSWORD:?}" "${ODOO_DB_PASSWORD:?}" "${DATA_ROOT:?}" "${FINGERPRINT_SQL:?}"
export PGPORT="${PGPORT:-5432}" LC_ALL=C
REPLACE_DB=${REPLACE_DB:-} WRITE_MARKER=${WRITE_MARKER:-1}
sup() { PGPASSWORD="$PGSUPERUSER_PASSWORD" psql -h "$PGHOST" -U postgres -X -q -v ON_ERROR_STOP=1 -tA "$@"; }
odoo_sql() { PGPASSWORD="$ODOO_DB_PASSWORD" psql -h "$PGHOST" -U odoo -d "$ODOO_DB_NAME" -X -q -v ON_ERROR_STOP=1 -tA "$@"; }
for _i in $(seq 1 60); do pg_isready -q -h "$PGHOST" -p "$PGPORT" -t 2 && break; sleep 1; done
pg_isready -q -h "$PGHOST" -p "$PGPORT" -t 2 || die "連不到 ${PGHOST}:${PGPORT}"
cd "$SET_DIR"

# 1. 完整性
[[ -f SHA256SUMS ]] || die "缺 SHA256SUMS"
for f in db.dump filestore.tar manifest.json source.fp source.fs; do
    [[ -f "$f" ]] || die "缺 ${f}"
    awk '{print $2}' SHA256SUMS | sed 's/^\*//' | grep -qx "$f" || die "SHA256SUMS 沒有列 ${f}"
done
sha256sum --strict -c SHA256SUMS || die "sha256 不符（傳輸截斷或檔案不對）"
pg_restore -f /dev/null db.dump || die "pg_restore 全讀失敗（archive 損毀）"
if tar -tf filestore.tar | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then die "filestore.tar 含絕對路徑或 .."; fi
echo "RESTORE integrity ok: $(stat -c %s db.dump) bytes dump, $(tar -tf filestore.tar | grep -vc '/$') files in filestore.tar"

# 2. manifest（postgres image 沒有 jq／python ⇒ 用 psql 解析 JSON）
MANIFEST="$(cat manifest.json)"
IFS='|' read -r ENC COLL CTYPE PROVIDER SRC_DB SRC_DIGEST SRC_CORE < <(sup -d postgres -F '|' -v m="$MANIFEST" <<'SQL'
SELECT coalesce(j->>'encoding', ''), coalesce(j->>'datcollate', ''), coalesce(j->>'datctype', ''),
       coalesce(j->>'datlocprovider', ''), coalesce(j->>'db_name', ''),
       coalesce(j->>'source_image_digest', ''), coalesce(j->>'source_core_date', '')
FROM (SELECT :'m'::jsonb AS j) s;
SQL
)
[[ "$ENC" == UTF8 ]] || die "manifest encoding='${ENC}'（只收 UTF8）"
[[ -n "$COLL" && -n "$CTYPE" ]] || die "manifest 缺 datcollate／datctype"
[[ "$PROVIDER" == c ]] || die "manifest datlocprovider='${PROVIDER}'（只收 libc＝c）"
[[ "$SRC_DB" == "$ODOO_DB_NAME" ]] || echo "RESTORE note: 來源 DB '${SRC_DB}' 還原成 '${ODOO_DB_NAME}'（filestore 目錄跟著改名）"
EXTS="$(sup -d postgres -v m="$MANIFEST" <<'SQL'
SELECT coalesce(string_agg(k, ' ' ORDER BY k), '') FROM jsonb_object_keys(coalesce(:'m'::jsonb->'extensions', '{}'::jsonb)) AS k;
SQL
)"
for e in $EXTS; do
    case "$e" in
        plpgsql|pg_trgm|unaccent|vector) ;;
        *) die "未知 extension '${e}'，改走人工 runbook" ;;
    esac
done
if [[ "$WRITE_MARKER" == 1 ]]; then
    [[ "$SRC_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || die "manifest source_image_digest='${SRC_DIGEST}' 不是 sha256 digest"
    [[ "$SRC_CORE" =~ ^[0-9]+\.[0-9]+\.[0-9]{8}$ ]] || die "manifest source_core_date='${SRC_CORE}' 不是 18.0.YYYYMMDD"
fi
echo "RESTORE manifest ok: ${ENC} ${COLL}/${CTYPE} extensions=[${EXTS}] source=${SRC_DIGEST:-?} core=${SRC_CORE:-?}"

# 3. 既有 DB：chart 首裝會先建一個空庫（-i base），只有 REPLACE_DB 明確指名時才取代
DEST="${DATA_ROOT}/filestore/${ODOO_DB_NAME}"
EXISTS=$(sup -d postgres -v db="$ODOO_DB_NAME" <<'SQL'
SELECT count(*) FROM pg_database WHERE datname = :'db';
SQL
)
if [[ "$EXISTS" != 0 ]]; then
    [[ "$REPLACE_DB" == "$ODOO_DB_NAME" ]] || die "DB ${ODOO_DB_NAME} 已存在；要取代 chart 首裝的空庫請明確給 REPLACE_DB=${ODOO_DB_NAME}"
    echo "RESTORE replacing existing DB ${ODOO_DB_NAME}: $(sup -d "$ODOO_DB_NAME" -c "SELECT 'partners=' || count(*) FROM res_partner" 2>/dev/null || echo 'not an odoo db')"
    sup -d postgres -c "DROP DATABASE \"${ODOO_DB_NAME}\" WITH (FORCE)"
    rm -rf "$DEST"
fi
if [[ -d "$DEST" ]] && [[ -n "$(ls -A "$DEST")" ]]; then die "${DEST} 已有檔案——不覆蓋（DB 不存在卻有 filestore，先人工確認）"; fi

# 4. 角色、DB、extension（superuser）；odoo 角色屬性同 chart bootstrap ②
sup -d postgres -v pw="$ODOO_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE odoo LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'odoo') \gexec
SQL
sup -d postgres -v db="$ODOO_DB_NAME" -v coll="$COLL" -v ctype="$CTYPE" <<'SQL'
SELECT format('CREATE DATABASE %I ENCODING %L LC_COLLATE %L LC_CTYPE %L TEMPLATE template0 OWNER odoo',
              :'db', 'UTF8', :'coll', :'ctype') \gexec
SQL
for e in $EXTS; do
    [[ "$e" == plpgsql ]] && continue
    sup -d "$ODOO_DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS ${e}"
done

# 5. 資料（odoo 角色、單一交易；TOC 濾掉 EXTENSION 與 COMMENT - EXTENSION）
pg_restore -l db.dump | awk '!/ EXTENSION /' > /tmp/toc
PGPASSWORD="$ODOO_DB_PASSWORD" pg_restore -h "$PGHOST" -U odoo -d "$ODOO_DB_NAME" -1 --exit-on-error \
    --no-owner --no-privileges --role=odoo -L /tmp/toc db.dump || die "pg_restore 失敗（單一交易已回滾）"
echo "RESTORE db ok: $(sup -d "$ODOO_DB_NAME" -c "SELECT pg_size_pretty(pg_database_size(current_database()))")"

# 6. filestore（odoo uid 100／gid 101，與 woow-odoo chart podSecurityContext 一致）
mkdir -p "$DEST"
tar -xf filestore.tar -C "$DEST" --no-same-owner --no-same-permissions
chown -R 100:101 "${DATA_ROOT}/filestore"
echo "RESTORE filestore ok: $(find "$DEST" -type f | wc -l) files"

# 7. 指紋：與來源逐位元比對（meta／ext 行不比：PG minor、extension 版本可以不同）
sup -d "$ODOO_DB_NAME" -f "$FINGERPRINT_SQL" > target.fp
( cd "$DEST" && find . -type f -printf '%P\0' | sort -z | xargs -0 -r sha256sum ) > target.fs
if ! diff <(grep -E '^(table|seq)\|' source.fp) <(grep -E '^(table|seq)\|' target.fp) > /tmp/fp.diff; then
    echo "RESTORE fingerprint DB mismatch（前 20 行，< 來源 > 目標）："; head -20 /tmp/fp.diff
    die "DB 指紋不一致：不寫 marker"
fi
if ! diff source.fs target.fs > /tmp/fs.diff; then
    echo "RESTORE fingerprint filestore mismatch（前 20 行）："; head -20 /tmp/fs.diff
    die "filestore 指紋不一致：不寫 marker"
fi
echo "RESTORE fingerprint ok: $(grep -c '^table|' target.fp) tables, $(grep -c '^seq|' target.fp) sequences, $(grep -c . target.fs) files identical"

# 8. 預寫升級閘 marker：來源 digest＋來源 core 日期 ⇒ 開機時升級閘看到 digest 不同，自動跑一次 -u all＋post-verify
if [[ "$WRITE_MARKER" == 1 ]]; then
    odoo_sql -v v="{\"digest\":\"${SRC_DIGEST}\",\"core_date\":\"${SRC_CORE}\"}" <<'SQL'
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES ('woow_paas_platform.upgrade_done', :'v', 1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;
SQL
    echo "RESTORE marker written: digest=${SRC_DIGEST} core_date=${SRC_CORE}"
else
    echo "RESTORE marker skipped (WRITE_MARKER=0)：odoo 開機時升級閘會 exit 3"
fi
echo "RESTORE-DONE"
