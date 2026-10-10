-- fingerprint.sql — DB 內容指紋（standalone-odoo-migrate，#1604）
--
-- 每張 public 表一行：表名|筆數|內容 hash；另輸出 sequence 狀態與 extension 版本。
-- 內容 hash＝每列 md5(row::text) 排序後串接再取 md5：與實體順序、OID、vacuum 無關，只看資料本身。
-- 來源（凍結、odoo 停下後）與目標（restore 後、Odoo 啟動前）各跑一次，以 fingerprint_diff.py 比對。
--
-- 用法（全程唯讀；可在來源 odoo 容器或目標 postgres 容器內跑）：
--   psql -X -q -At -v ON_ERROR_STOP=1 -d <db> -f fingerprint.sql
-- 輸出格式穩定性：先固定所有影響 ::text 的 session 設定（時區、日期格式、浮點位數、bytea 輸出）。
SET TimeZone = 'UTC';
SET DateStyle = 'ISO, YMD';
SET IntervalStyle = 'postgres';
SET extra_float_digits = 1;
SET bytea_output = 'hex';
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

SELECT 'meta|server_major|' || current_setting('server_version_num')::int / 10000;
SELECT 'ext|' || extname || '|' || extversion FROM pg_extension ORDER BY extname;
SELECT 'seq|' || sequencename || '|' || coalesce(last_value::text, 'null')
  FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename;

SELECT format(
  $q$SELECT 'table|' || %L || '|' || count(*) || '|' || coalesce(md5(string_agg(h, '' ORDER BY h)), '-')
       FROM (SELECT md5(t::text) AS h FROM %I.%I AS t) AS s$q$,
  c.relname, n.nspname, c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY c.relname
\gexec

COMMIT;
