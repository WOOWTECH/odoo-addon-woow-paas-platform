#!/usr/bin/env bash
# prod clone restore（在沙盒內的一次性 Job 執行；image＝沙盒 postgres image：PG17 clients、tar、coreutils）。
# 由 seed-from-prod.sh restore 以 ConfigMap 掛進來（#1206；做法對齊 woow-odoo chart files/restore.sh 的 stage 段）。
#
# env：SET_DIR（/incoming/<set>）、ODOO_DB_NAME、PGHOST、PGPORT、PGSUPERUSER_PASSWORD、ODOO_DB_PASSWORD、DATA_ROOT
# 輸入（#1145 備份組形狀，經 HTTP PUT 送進來）：db.dump（pg_dump -Fc）、filestore.tar、manifest.json、SHA256SUMS、
#   attachments.txt（選配；有就驗引用存在）
# 不變：只建新 DB（已存在就拒絕）；建庫 locale 取 manifest 的來源實測值（charts#81 R2-Q4）；
#   資料以 odoo 角色 pg_restore -1 --no-owner --no-privileges，TOC 只排除 EXTENSION 類（不用 --no-comments）。
set -euo pipefail
die() { echo "RESTORE-FAIL: $*" >&2; exit 1; }
: "${SET_DIR:?}" "${ODOO_DB_NAME:?}" "${PGHOST:?}" "${PGSUPERUSER_PASSWORD:?}" "${ODOO_DB_PASSWORD:?}" "${DATA_ROOT:?}"
export PGPORT="${PGPORT:-5432}"
sup() { PGPASSWORD="$PGSUPERUSER_PASSWORD" psql -h "$PGHOST" -U postgres -X -q -v ON_ERROR_STOP=1 -tA "$@"; }
# K3s 對新 pod 套 NP 有 1～2 秒延遲（這段期間連同 ns postgres 也被拒）⇒ 先等 DB 可連
for _i in $(seq 1 60); do pg_isready -q -h "$PGHOST" -p "$PGPORT" -t 2 && break; sleep 1; done
pg_isready -q -h "$PGHOST" -p "$PGPORT" -t 2 || die "連不到 ${PGHOST}:${PGPORT}"
cd "$SET_DIR"

# 1. 完整性：SHA256SUMS 必須涵蓋必要檔案，且全部相符（--strict：格式錯也算失敗）
[[ -f SHA256SUMS ]] || die "缺 SHA256SUMS"
for f in db.dump filestore.tar manifest.json; do
    [[ -f "$f" ]] || die "缺 ${f}"
    awk '{print $2}' SHA256SUMS | sed 's/^\*//' | grep -qx "$f" || die "SHA256SUMS 沒有列 ${f}"
done
sha256sum --strict -c SHA256SUMS || die "sha256 不符（傳輸截斷或檔案不對）"
pg_restore -f /dev/null db.dump || die "pg_restore 全讀失敗（archive 損毀）"
if tar -tf filestore.tar | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then die "filestore.tar 含絕對路徑或 .."; fi
echo "RESTORE integrity ok: $(stat -c %s db.dump) bytes dump, $(tar -tf filestore.tar | grep -vc '/$') files in filestore.tar"

# 2. manifest：encoding／locale／extension（用 psql 解析 JSON，postgres image 沒有 jq／python）
MANIFEST="$(cat manifest.json)"
read -r ENC COLL CTYPE PROVIDER SRC_DB < <(sup -d postgres -F ' ' -v m="$MANIFEST" <<'SQL'
SELECT coalesce(:'m'::jsonb->>'encoding', ''), coalesce(:'m'::jsonb->>'datcollate', ''), coalesce(:'m'::jsonb->>'datctype', ''),
       coalesce(:'m'::jsonb->>'datlocprovider', 'c'), coalesce(:'m'::jsonb->>'db_name', '');
SQL
)
[[ "$ENC" == UTF8 ]] || die "manifest encoding='${ENC}'（只收 UTF8）"
[[ -n "$COLL" && -n "$CTYPE" ]] || die "manifest 缺 datcollate／datctype"
[[ "$PROVIDER" == c ]] || die "manifest datlocprovider='${PROVIDER}'（只收 libc＝c）"
[[ "$SRC_DB" == "$ODOO_DB_NAME" ]] || echo "RESTORE note: manifest db_name='${SRC_DB}'，還原成 '${ODOO_DB_NAME}'"
EXTS="$(sup -d postgres -v m="$MANIFEST" <<'SQL'
SELECT string_agg(k, ' ' ORDER BY k) FROM jsonb_object_keys(coalesce(:'m'::jsonb->'extensions', '{}'::jsonb)) AS k;
SQL
)"
for e in $EXTS; do
    case "$e" in
        plpgsql|pg_trgm|unaccent) ;;
        vector) die "來源有 vector extension；平台 Step 1 期間 postgres.extensions.vector=false（charts#81 R2-Q6），改走人工處理" ;;
        *) die "未知 extension '${e}'，改走人工 runbook" ;;
    esac
done
echo "RESTORE manifest ok: ${ENC} ${COLL}/${CTYPE} extensions=[${EXTS}]"

# 3. 角色與 DB（superuser）。odoo 角色屬性同 chart bootstrap ②；密碼＝chart credentials（之後 bootstrap ② 每次同步）
sup -d postgres -v db="$ODOO_DB_NAME" <<'SQL' | grep -qx 0 || die "DB ${ODOO_DB_NAME} 已存在——不覆蓋（要重來請 destroy 沙盒）"
SELECT count(*) FROM pg_database WHERE datname = :'db';
SQL
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

# 4. 資料（odoo 角色、單一交易）
pg_restore -l db.dump | awk '!/ EXTENSION /' > /tmp/toc
PGPASSWORD="$ODOO_DB_PASSWORD" pg_restore -h "$PGHOST" -U odoo -d "$ODOO_DB_NAME" -1 --exit-on-error \
    --no-owner --no-privileges --role=odoo -L /tmp/toc db.dump || die "pg_restore 失敗（單一交易已回滾；DB 是空的，可 DROP 後重跑）"
echo "RESTORE db ok: $(sup -d "$ODOO_DB_NAME" -c "SELECT pg_size_pretty(pg_database_size(current_database()))")"

# 5. filestore（odoo uid 100／gid 101，與 woow-odoo chart podSecurityContext 一致）
DEST="${DATA_ROOT}/filestore/${ODOO_DB_NAME}"
if [[ -d "$DEST" ]] && [[ -n "$(ls -A "$DEST")" ]]; then die "${DEST} 已有檔案——不覆蓋"; fi
mkdir -p "$DEST"
tar -xf filestore.tar -C "$DEST" --no-same-owner --no-same-permissions
chown -R 100:101 "${DATA_ROOT}/filestore"
echo "RESTORE filestore ok: $(find "$DEST" -type f | wc -l) files, $(du -sh "$DEST" | cut -f1)"
if [[ -f attachments.txt ]]; then
    missing=0
    while IFS= read -r ref; do
        [[ -z "$ref" || -f "${DEST}/${ref}" ]] || missing=$((missing + 1))
    done < attachments.txt
    [[ "$missing" == 0 ]] || die "attachments.txt 有 ${missing} 個引用在 filestore 找不到"
    echo "RESTORE attachments ok: $(grep -c . attachments.txt) references"
fi
echo "RESTORE-DONE"
