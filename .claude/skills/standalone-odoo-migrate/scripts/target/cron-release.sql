-- cron-release.sql — 放開 cron-hold.sql 暫停的排程（standalone-odoo-migrate，#1604）。
--
-- 先移除 cron-hold.sql 掛的 function 與 trigger（standalone_odoo_migrate_cron_hold），再處理「目前仍停在暫停時點」的
-- 排程與 trigger（期間已經跑過、或被人改到別的時點的不碰）：
--   - 暫停當時就存在的：依 ir_config_parameter `standalone_odoo_migrate.cron_hold` 的原值還原；
--   - 暫停期間才被夾到暫停時點的（開機途中排的 trigger、-u all 改寫或重新啟用的排程）：原本就早於暫停時點、現在已到期
--     ⇒ 設成 now()。
-- 最後刪掉那個 key。重跑無害；沒有 hold 時只清掉殘留的 function／trigger。
-- 放開後一分鐘內這些排程會照原本的順序跑一輪（與沒有暫停時開機的行為相同，只是晚一點）。
-- 由 verify.sh（--release-crons／--cleanup，比對通過後）或 cron-hold.sh release 執行（postgres 角色）。
-- 輸出：cronrelease|<有無 hold：1／0>|<還原的排程數>|<還原的 trigger 數>|<暫停期間才夾住、改成 now 的排程數>|<同上的 trigger 數>
\set ON_ERROR_STOP on
\set QUIET on
SET client_min_messages = warning;
BEGIN;
DROP TRIGGER IF EXISTS standalone_odoo_migrate_cron_hold ON ir_cron;
DROP TRIGGER IF EXISTS standalone_odoo_migrate_cron_hold ON ir_cron_trigger;
DROP FUNCTION IF EXISTS standalone_odoo_migrate_cron_hold();
CREATE TEMP TABLE _cron_hold ON COMMIT DROP AS
  SELECT value::json AS j, (value::json->>'until')::timestamp AS u
    FROM ir_config_parameter WHERE key = 'standalone_odoo_migrate.cron_hold';
WITH upd AS (
  UPDATE ir_cron c SET nextcall = (e.value #>> '{}')::timestamp
    FROM _cron_hold h, json_each(h.j->'crons') e
   WHERE c.id = e.key::int AND c.nextcall = h.u
  RETURNING c.id)
SELECT count(*) AS crons_restored FROM upd \gset
WITH upd AS (
  UPDATE ir_cron_trigger g SET call_at = (e.value #>> '{}')::timestamp
    FROM _cron_hold h, json_each(h.j->'triggers') e
   WHERE g.id = e.key::int AND g.call_at = h.u
  RETURNING g.id)
SELECT count(*) AS triggers_restored FROM upd \gset
-- 上面還原過的列已經不在暫停時點（原值都早於它），剩下仍停在暫停時點的就是暫停期間才被夾住的
WITH upd AS (
  UPDATE ir_cron c SET nextcall = (now() AT TIME ZONE 'UTC')
    FROM _cron_hold h
   WHERE c.active AND c.nextcall = h.u
  RETURNING c.id)
SELECT count(*) AS crons_late FROM upd \gset
WITH upd AS (
  UPDATE ir_cron_trigger g SET call_at = (now() AT TIME ZONE 'UTC')
    FROM _cron_hold h
   WHERE g.call_at = h.u
  RETURNING g.id)
SELECT count(*) AS triggers_late FROM upd \gset
SELECT count(*) AS held FROM _cron_hold \gset
DELETE FROM ir_config_parameter WHERE key = 'standalone_odoo_migrate.cron_hold';
COMMIT;
SELECT 'cronrelease|' || :held || '|' || :crons_restored || '|' || :triggers_restored || '|' || :crons_late || '|' || :triggers_late;
