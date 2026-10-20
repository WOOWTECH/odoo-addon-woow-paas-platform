#!/usr/bin/env bash
# restore-in-pod.sh — 把 standalone 站的匯出組還原進 woow-odoo release（standalone-odoo-migrate，#1604）
#
# 由 restore.sh 以 ConfigMap 掛進目標 ns 的一次性 Job（image＝release 的 postgres image：PG17 client、tar、coreutils）。
# 底稿是 woow-e2e-fixtures/scripts/prod-clone/restore-in-pod.sh；差異：允許 vector、可取代 chart 首裝的空庫、
# 還原後在 pod 內比對指紋，一致才預寫 upgrade_done marker。
#
# env：SET_DIR（/incoming/<set>）、ODOO_DB_NAME、PGHOST、PGPORT、PGSUPERUSER_PASSWORD、ODOO_DB_PASSWORD、DATA_ROOT、
#      FINGERPRINT_SQL、BUSINESS_SQL、REPLACE_DB（＝ODOO_DB_NAME 才允許 DROP 既有 DB）、WRITE_MARKER（1／0）
#      PHASE：all（預設）／restore（步驟 1～7）／finalize（步驟 7a～8）。彩排中和時 restore.sh 把一個 Job 拆成
#        restore →（release 的 odoo image 產官方中和 SQL）→ finalize 三個容器，所以要能分段跑。
#      NEUTRALIZE（1／0）、NEUTRALIZE_SQL、NEUTRALIZE_ASSERT_SQL、NEUTRALIZE_EXTRA_SQL（選配）：見步驟 7a
#      CRON_HOLD_MINUTES（0＝不暫停）、CRON_HOLD_SQL：見步驟 8c
#      RENAME_MODULES（選配，空白分隔的「舊名:新名」）、RENAME_SQL：見步驟 7r
# 輸入（export.sh 經 receiver 送進來）：db.dump、filestore.tar、manifest.json、source.fp、source.fs、SHA256SUMS
# 輸出：SET_DIR/target.fp、SET_DIR/target.fs、SET_DIR/baseline.bfp（客戶資料列基準，verify.sh --stage upgrade 用）、
#      中和時另有 SET_DIR/neutralize-report.txt 與空檔 SET_DIR/neutralized（標記這組是彩排）、
#      log 最後一行 RESTORE-DONE（PHASE=restore 時是 RESTORE-PHASE-RESTORE-DONE）
# 每一步失敗都 exit 1、不寫 marker；odoo 沒有 marker 開機時升級閘會 exit 3，不會碰資料。
# WRITE_MARKER=1 另寫 woow_paas_platform.tenant_localized，擋掉平台部署後初始化的台灣在地化（見步驟 8b）。
set -euo pipefail
die() { echo "RESTORE-FAIL: $*" >&2; exit 1; }
: "${SET_DIR:?}" "${ODOO_DB_NAME:?}" "${PGHOST:?}" "${PGSUPERUSER_PASSWORD:?}" "${ODOO_DB_PASSWORD:?}" "${DATA_ROOT:?}" "${FINGERPRINT_SQL:?}" "${BUSINESS_SQL:?}"
export PGPORT="${PGPORT:-5432}" LC_ALL=C
REPLACE_DB=${REPLACE_DB:-} WRITE_MARKER=${WRITE_MARKER:-1} PHASE=${PHASE:-all} NEUTRALIZE=${NEUTRALIZE:-0}
CRON_HOLD_MINUTES=${CRON_HOLD_MINUTES:-0} RENAME_MODULES=${RENAME_MODULES:-}
[[ "$CRON_HOLD_MINUTES" =~ ^[0-9]+$ ]] || die "CRON_HOLD_MINUTES='${CRON_HOLD_MINUTES}' 不是整數"
case "$PHASE" in all|restore|finalize) ;; *) die "PHASE='${PHASE}' 不合法" ;; esac
sup() { PGPASSWORD="$PGSUPERUSER_PASSWORD" psql -h "$PGHOST" -U postgres -X -q -v ON_ERROR_STOP=1 -tA "$@"; }
odoo_sql() { PGPASSWORD="$ODOO_DB_PASSWORD" psql -h "$PGHOST" -U odoo -d "$ODOO_DB_NAME" -X -q -v ON_ERROR_STOP=1 -tA "$@"; }
for _i in $(seq 1 60); do pg_isready -q -h "$PGHOST" -p "$PGPORT" -t 2 && break; sleep 1; done
pg_isready -q -h "$PGHOST" -p "$PGPORT" -t 2 || die "連不到 ${PGHOST}:${PGPORT}"
cd "$SET_DIR"

if [[ "$PHASE" != finalize ]]; then
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
fi

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

DEST="${DATA_ROOT}/filestore/${ODOO_DB_NAME}"
if [[ "$PHASE" != finalize ]]; then
# 3. 既有 DB：chart 首裝會先建一個空庫（-i base），只有 REPLACE_DB 明確指名時才取代
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
rm -f neutralized neutralize-report.txt neutralize-official.sql rehearsal.pwhash   # 重新還原過：上一輪彩排的產物作廢

# 7r. 模組改名（restore.sh --rename-module）：站上的技術名與 image 裡的正式名不同時，在 DB 改名（rename-module.sql）。
#     放在逐位元比對之後（target.fp 仍是改名前、與來源相同）、中和與取基準之前：官方中和 SQL 依已安裝模組產生、
#     升級閘只認 image 裡的模組名，兩者都要看到新名；基準取在改名之後，升級後的比對不會把改名算成差異。
if [[ -n "$RENAME_MODULES" ]]; then
    : "${RENAME_SQL:?}"
    : > rename-report.txt
    for pair in $RENAME_MODULES; do
        [[ "$pair" =~ ^([a-z0-9_]+):([a-z0-9_]+)$ ]] || die "RENAME_MODULES 的 '${pair}' 不是 舊名:新名"
        out=$(odoo_sql -v old="${BASH_REMATCH[1]}" -v new="${BASH_REMATCH[2]}" -v apply=1 -f "$RENAME_SQL") \
            || die "模組改名 ${pair} 失敗（交易已回滾）"
        printf '%s\n' "$out" | sed "s/^/${pair}|/" >> rename-report.txt
        grep -qx 'rename-ok|commit' <<<"$out" || die "模組改名 ${pair} 沒有 commit"
    done
    echo "RESTORE rename report（rscan＝改完仍含舊名的欄位，逐條看）："; grep -v '|0$' rename-report.txt | sed 's/^/  /'
    echo "RESTORE rename ok: ${RENAME_MODULES}"
fi
fi
if [[ "$PHASE" == restore ]]; then
    echo "RESTORE-PHASE-RESTORE-DONE"
    exit 0
fi

# 7a. 彩排中和（只給彩排；正式切換不走這段）。此刻資料已證明與來源逐位元相同，中和之後才取客戶資料列基準，
#     所以升級後的比對同時證明「中和過的設定沒有被 -u all 改回去」。
#     官方 SQL＝release 的 odoo image 跑 `odoo neutralize --stdout`（依已安裝模組組出，restore.sh 的中間容器產生）；
#     補充 SQL 見 neutralize-supplement.sql 檔頭。全部在同一個交易內；之後跑斷言，有任何 nfail 就停在這裡不寫 marker。
if [[ "$NEUTRALIZE" == 1 ]]; then
    : "${NEUTRALIZE_SQL:?}" "${NEUTRALIZE_ASSERT_SQL:?}"
    [[ -s neutralize-official.sql ]] || die "缺 neutralize-official.sql（odoo neutralize --stdout 的輸出）"
    [[ -s rehearsal.pwhash ]] || die "缺 rehearsal.pwhash（彩排密碼的 hash）"
    PWHASH="$(cat rehearsal.pwhash)"
    [[ "$PWHASH" == '$pbkdf2-sha512$'* ]] || die "rehearsal.pwhash 不是 pbkdf2_sha512 hash"
    {
        echo 'BEGIN;'
        grep -vxE '(BEGIN|COMMIT);' neutralize-official.sql
        cat "$NEUTRALIZE_SQL"
        if [[ -n "${NEUTRALIZE_EXTRA_SQL:-}" ]]; then cat "$NEUTRALIZE_EXTRA_SQL"; fi
        echo 'COMMIT;'
    } > /tmp/neutralize.sql
    odoo_sql -v pwhash="$PWHASH" -f /tmp/neutralize.sql || die "中和 SQL 失敗（交易已回滾，資料仍與來源相同）"
    odoo_sql -v pwhash="$PWHASH" -f "$NEUTRALIZE_ASSERT_SQL" > neutralize-report.txt || die "中和斷言 SQL 執行失敗"
    rm -f rehearsal.pwhash /tmp/neutralize.sql
    echo "RESTORE neutralize report："; sed 's/^/  /' neutralize-report.txt
    if grep -q '^nfail|' neutralize-report.txt; then die "中和斷言未通過（見上方 nfail 行）：不寫 marker"; fi
    : > neutralized
    echo "RESTORE neutralized ok: 官方 SQL $(grep -c . neutralize-official.sql) 行、$(grep -c '^nscan|' neutralize-report.txt || true) 個欄位待人檢查（nscan）"
fi

# 7b. 客戶資料列基準：此刻的資料已證明與來源相同；升級閘跑完 -u all 後 verify.sh 拿它比對（marker 還沒寫，不會算進基準）
sup -d "$ODOO_DB_NAME" -f "$BUSINESS_SQL" > baseline.bfp
echo "RESTORE business baseline ok: $(grep -c '^btable|' baseline.bfp) tables"

# 8. 預寫升級閘 marker：來源 digest＋來源 core 日期 ⇒ 開機時升級閘看到 digest 不同，自動跑一次 -u all＋post-verify
if [[ "$WRITE_MARKER" == 1 ]]; then
    odoo_sql -v v="{\"digest\":\"${SRC_DIGEST}\",\"core_date\":\"${SRC_CORE}\"}" <<'SQL'
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES ('woow_paas_platform.upgrade_done', :'v', 1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;
SQL
    echo "RESTORE marker written: digest=${SRC_DIGEST} core_date=${SRC_CORE}"
    # 8b. 平台的部署後初始化（Redeploy、改 addon 都會觸發 operator POST /init/odoo）帶一段台灣在地化
    #     （extra/paas-operator/src/services/odoo_scripts/localize_tenant.py：裝 zh_TW、改管理員語言／時區、公司國家／幣別）。
    #     它靠這個標記判斷「已處理過」；匯入的 DB 沒有標記、又剛好沒有登入紀錄時會被當成新開的站改掉（stg 實測）。
    #     先寫標記 ⇒ 匯入的資料一律不被在地化。來源本來就有這個 key 的話保留原值。
    odoo_sql <<'SQL'
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES ('woow_paas_platform.tenant_localized', 'skipped-imported', 1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
ON CONFLICT (key) DO NOTHING;
SQL
    echo "RESTORE localization marker written: woow_paas_platform.tenant_localized"
    # 8c. 暫停排程到比對做完：web 開機後過期的排程會立刻跑、改到客戶資料列，升級後的比對就不準了。
    #     原值存在 ir_config_parameter，verify.sh --release-crons／--cleanup（或 cron-hold.sh release）還原；
    #     時間到了沒人放開，排程照常恢復。細節見 cron-hold.sql 檔頭。
    if (( CRON_HOLD_MINUTES > 0 )); then
        : "${CRON_HOLD_SQL:?}"
        hold=$(odoo_sql -v minutes="$CRON_HOLD_MINUTES" -f "$CRON_HOLD_SQL") || die "暫停排程失敗"
        IFS='|' read -r _ HOLD_UNTIL HOLD_CRONS HOLD_TRIGGERS <<<"$hold"
        echo "RESTORE crons held until ${HOLD_UNTIL} UTC: ${HOLD_CRONS} 個排程、${HOLD_TRIGGERS} 個 trigger（verify.sh --release-crons 放開）"
    fi
else
    echo "RESTORE marker skipped (WRITE_MARKER=0)：odoo 開機時升級閘會 exit 3"
fi
echo "RESTORE-DONE"
