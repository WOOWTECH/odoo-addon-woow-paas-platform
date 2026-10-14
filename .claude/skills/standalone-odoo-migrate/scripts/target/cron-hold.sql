-- cron-hold.sql — 還原後、Odoo 開機前暫停排程（standalone-odoo-migrate，#1604）。
--
-- 為什麼：還原進來的 DB 帶著來源的 nextcall，大多早就過期。web 一開機（升級閘跑完 -u all 之後），過期的排程立刻
--   輪流跑——寄信、收信、POS、autovacuum……都會改到客戶資料列，verify.sh --stage upgrade 就比不過
--   （harumi prod 彩排實測：autovacuum 開機約 3 分鐘後刪了 2 筆 ir_sequence）。
-- 做什麼：把啟用中、nextcall 早於 now()+:minutes 的排程，以及 call_at 早於那個時點的 trigger，一律延到那個時點；
--   原值存在 ir_config_parameter `standalone_odoo_migrate.cron_hold`（JSON），cron-release.sql 依此還原。
--   時間到了沒人放開，排程照常恢復（漏放不會讓站一直停擺）。
-- 比對不受影響：ir_cron*、在基準之後新增的 ir_config_parameter 列都屬「預期內」。
-- 由 restore-in-pod.sh 在取完客戶資料列基準、寫完 marker 之後執行（odoo 角色）。參數：-v minutes=<分鐘>。
-- 輸出：cronhold|<到期 UTC>|<暫停的排程數>|<暫停的 trigger 數>
\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
SELECT to_char((now() AT TIME ZONE 'UTC') + make_interval(mins => :minutes), 'YYYY-MM-DD HH24:MI:SS') AS hold_until \gset
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
SELECT 'standalone_odoo_migrate.cron_hold',
       json_build_object(
         'until', :'hold_until',
         'crons', (SELECT coalesce(json_object_agg(id, to_char(nextcall, 'YYYY-MM-DD HH24:MI:SS.US')), '{}'::json)
                     FROM ir_cron WHERE active AND nextcall < :'hold_until'::timestamp),
         'triggers', (SELECT coalesce(json_object_agg(id, to_char(call_at, 'YYYY-MM-DD HH24:MI:SS.US')), '{}'::json)
                        FROM ir_cron_trigger WHERE call_at < :'hold_until'::timestamp))::text,
       1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;
UPDATE ir_cron SET nextcall = :'hold_until'::timestamp WHERE active AND nextcall < :'hold_until'::timestamp;
UPDATE ir_cron_trigger SET call_at = :'hold_until'::timestamp WHERE call_at < :'hold_until'::timestamp;
COMMIT;
SELECT 'cronhold|' || (j->>'until') || '|' || (SELECT count(*) FROM json_object_keys(j->'crons'))
       || '|' || (SELECT count(*) FROM json_object_keys(j->'triggers'))
  FROM (SELECT value::json AS j FROM ir_config_parameter WHERE key = 'standalone_odoo_migrate.cron_hold') s;
