-- cron-hold.sql — 還原後、Odoo 開機前暫停排程（standalone-odoo-migrate，#1604）。
--
-- 為什麼：web 一開機（升級閘跑完 -u all 之後），到期的排程就會跑——寄信、收信、POS、autovacuum……都會改到客戶資料列，
--   verify.sh --stage upgrade 就比不過。到期有兩個來源：
--   1. 還原進來的 nextcall 帶著來源的值，常常早就過期。
--   2. 開機途中才排的 trigger：Odoo core 每次更新模組（odoo/modules/loading.py，-u all 結束時）都會
--      `base.autovacuum_job._trigger(at=now+1 分鐘)`。這個 trigger 在本檔執行之後才建立，只改既有列擋不到。
--      harumi prod 彩排實測（第二輪）：-u all 結束約 1 分鐘後 autovacuum 被這個 trigger 叫起來，跑 point_of_sale 的
--      _gc_session_sequences 刪掉 2 筆已關帳 POS session 的 ir_sequence（第一輪 Redeploy 後比對不過的也是這 2 筆）；
--      verify 取指紋剛好早於它，第一次比對因此「剛好通過」。
-- 做什麼：
--   a. 啟用中、nextcall 早於 now()+:minutes 的排程，以及 call_at 早於那個時點的 trigger，一律延到那個時點；
--      原值與暫停起點存在 ir_config_parameter `standalone_odoo_migrate.cron_hold`（JSON：until／since／crons／triggers）。
--   b. 在 ir_cron 與 ir_cron_trigger 掛 BEFORE INSERT OR UPDATE trigger（standalone_odoo_migrate_cron_hold）：
--      暫停期間新建或被改寫的 trigger call_at、啟用中排程的 nextcall，早於暫停時點的一律夾到暫停時點——
--      涵蓋 loading.py 的 autovacuum trigger、-u all 依 XML 改寫的 nextcall、被 XML 重新啟用的排程。
--   時間到了沒人放開，function 自己失效（每次觸發都重讀 until），排程照常恢復（漏放不會讓站一直停擺）；
--   function 與 trigger 由 cron-release.sql 移除（verify.sh --release-crons／--cleanup 或 cron-hold.sh release）。
-- 比對不受影響：ir_cron*、在基準之後新增的 ir_config_parameter 列都屬「預期內」；指紋不含 function／trigger。
-- 由 restore-in-pod.sh 在取完客戶資料列基準、寫完 marker 之後執行（odoo 角色：DB 與這幾張表的擁有者）。參數：-v minutes=<分鐘>。
-- 輸出：cronhold|<到期 UTC>|<暫停的排程數>|<暫停的 trigger 數>
\set ON_ERROR_STOP on
\set QUIET on
SET client_min_messages = warning;
BEGIN;
SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS hold_since,
       to_char((now() AT TIME ZONE 'UTC') + make_interval(mins => :minutes), 'YYYY-MM-DD HH24:MI:SS') AS hold_until \gset
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
SELECT 'standalone_odoo_migrate.cron_hold',
       json_build_object(
         'until', :'hold_until',
         'since', :'hold_since',
         'crons', (SELECT coalesce(json_object_agg(id, to_char(nextcall, 'YYYY-MM-DD HH24:MI:SS.US')), '{}'::json)
                     FROM ir_cron WHERE active AND nextcall < :'hold_until'::timestamp),
         'triggers', (SELECT coalesce(json_object_agg(id, to_char(call_at, 'YYYY-MM-DD HH24:MI:SS.US')), '{}'::json)
                        FROM ir_cron_trigger WHERE call_at < :'hold_until'::timestamp))::text,
       1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;
UPDATE ir_cron SET nextcall = :'hold_until'::timestamp WHERE active AND nextcall < :'hold_until'::timestamp;
UPDATE ir_cron_trigger SET call_at = :'hold_until'::timestamp WHERE call_at < :'hold_until'::timestamp;
CREATE OR REPLACE FUNCTION standalone_odoo_migrate_cron_hold() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
  u timestamp;
BEGIN
  SELECT (value::json->>'until')::timestamp INTO u
    FROM ir_config_parameter WHERE key = 'standalone_odoo_migrate.cron_hold';
  IF u IS NULL OR (now() AT TIME ZONE 'UTC') >= u THEN
    RETURN NEW;
  END IF;
  IF TG_TABLE_NAME = 'ir_cron_trigger' THEN
    IF NEW.call_at < u THEN
      NEW.call_at := u;
    END IF;
  ELSIF NEW.active AND NEW.nextcall < u THEN
    NEW.nextcall := u;
  END IF;
  RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS standalone_odoo_migrate_cron_hold ON ir_cron;
CREATE TRIGGER standalone_odoo_migrate_cron_hold BEFORE INSERT OR UPDATE ON ir_cron
  FOR EACH ROW EXECUTE FUNCTION standalone_odoo_migrate_cron_hold();
DROP TRIGGER IF EXISTS standalone_odoo_migrate_cron_hold ON ir_cron_trigger;
CREATE TRIGGER standalone_odoo_migrate_cron_hold BEFORE INSERT OR UPDATE ON ir_cron_trigger
  FOR EACH ROW EXECUTE FUNCTION standalone_odoo_migrate_cron_hold();
COMMIT;
SELECT 'cronhold|' || (j->>'until') || '|' || (SELECT count(*) FROM json_object_keys(j->'crons'))
       || '|' || (SELECT count(*) FROM json_object_keys(j->'triggers'))
  FROM (SELECT value::json AS j FROM ir_config_parameter WHERE key = 'standalone_odoo_migrate.cron_hold') s;
