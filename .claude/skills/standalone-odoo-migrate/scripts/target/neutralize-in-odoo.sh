#!/usr/bin/env bash
# neutralize-in-odoo.sh — 在 release 的 odoo image 內產生彩排中和的輸入（standalone-odoo-migrate，#1604）
#
# 由 restore.sh --neutralize 掛進 restore Job 的中間容器（image＝release 的 odoo image；此時 DB 已還原、還沒中和）。
# 本腳本**不改 DB**，只寫兩個檔到 SET_DIR，交給後面的 finalize 容器（postgres image）在單一交易內套用：
#   neutralize-official.sql  `odoo neutralize --stdout`：Odoo 依「已安裝模組」組出的官方中和 SQL
#                            （各模組的 data/neutralize.sql；不載入 registry，升級前的資料庫也能跑）
#   rehearsal.pwhash         彩排密碼的 pbkdf2_sha512 hash（明文只在 Secret 與本容器的 env，不落檔、不進 log）
# env：SET_DIR、ODOO_DB_NAME、PGHOST、PGPORT、PGPASSWORD（odoo 角色）、REHEARSAL_PASSWORD
set -euo pipefail
: "${SET_DIR:?}" "${ODOO_DB_NAME:?}" "${PGHOST:?}" "${PGPASSWORD:?}" "${REHEARSAL_PASSWORD:?}"
cd "$SET_DIR"
rm -f neutralize-official.sql rehearsal.pwhash

# addons_path：core＋image 內全部候選 addon（/all-addons）。只有已安裝模組的 neutralize.sql 會被取用。
odoo neutralize --stdout -d "$ODOO_DB_NAME" --db_host "$PGHOST" --db_port "${PGPORT:-5432}" --db_user odoo \
    --addons-path=/usr/lib/python3/dist-packages/odoo/addons,/all-addons > /tmp/neutralize.out
# stdout 可能混進別的訊息：只取 BEGIN; 到 COMMIT; 之間
sed -n '/^BEGIN;$/,/^COMMIT;$/p' /tmp/neutralize.out > neutralize-official.sql
[[ $(grep -cx 'BEGIN;' neutralize-official.sql) == 1 && $(grep -cx 'COMMIT;' neutralize-official.sql) == 1 ]] \
    || { echo "NEUTRALIZE-FAIL: odoo neutralize --stdout 的輸出不是一段 BEGIN…COMMIT" >&2; head -5 /tmp/neutralize.out >&2; exit 1; }
grep -q 'ir_cron' neutralize-official.sql \
    || { echo "NEUTRALIZE-FAIL: 官方 SQL 沒有停用排程的語句（base 的 neutralize.sql 沒被讀到？）" >&2; exit 1; }

python3 - > rehearsal.pwhash <<'PY'
import os
from passlib.context import CryptContext
# 與 Odoo 18 res.users 相同的雜湊（pbkdf2_sha512、600000 rounds）
print(CryptContext(schemes=["pbkdf2_sha512"], pbkdf2_sha512__rounds=600_000).hash(os.environ["REHEARSAL_PASSWORD"]), end="")
PY
echo "NEUTRALIZE-SQL ok: $(grep -c . neutralize-official.sql) lines from odoo neutralize --stdout"
