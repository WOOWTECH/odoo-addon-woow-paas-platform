-- cron-release.sql — 放開 cron-hold.sql 暫停的排程（standalone-odoo-migrate，#1604）。
--
-- 依 ir_config_parameter `standalone_odoo_migrate.cron_hold` 的原值還原：只動「目前仍停在暫停時點」的排程與 trigger
-- （期間已經跑過、或被人改過的不碰），然後刪掉那個 key。重跑無害；沒有 hold 時什麼都不做。
-- 原值多半早已過期 ⇒ 放開後一分鐘內這些排程會照原本的順序跑一輪（與沒有暫停時開機的行為相同，只是晚一點）。
-- 由 verify.sh（--release-crons／--cleanup，比對通過後）或 cron-hold.sh release 執行。
-- 輸出：cronrelease|<有無 hold：1／0>|<還原的排程數>|<還原的 trigger 數>
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
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
SELECT count(*) AS held FROM _cron_hold \gset
DELETE FROM ir_config_parameter WHERE key = 'standalone_odoo_migrate.cron_hold';
COMMIT;
SELECT 'cronrelease|' || :held || '|' || :crons_restored || '|' || :triggers_restored;
