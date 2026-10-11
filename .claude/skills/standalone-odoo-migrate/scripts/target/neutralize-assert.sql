-- neutralize-assert.sql — 彩排中和的斷言與報告（standalone-odoo-migrate，#1604）。唯讀。
--
-- 兩個時點各跑一次：
--   還原 Job 內、中和 SQL 套用之後（給 -v pwhash=…：連登入密碼一起驗）——有 nfail 就不寫 marker，Odoo 開不了機。
--   升級閘跑完、web 起來之後（verify.sh --stage upgrade，不給 pwhash）——-u all 或新版模組若把東西開回來，這裡會抓到。
-- 輸出（-At，一行一筆）：
--   nfail|<項目>|<筆數>   中和沒有生效（或被改回去）；有任何一行就算失敗
--   ninfo|<項目>|<數值>   供報告用的計數
--   nscan|<表.欄>|<筆數>  欄名像對外憑證、仍有值的欄位——只列名字與筆數，不輸出內容；要人看過，
--                         是對外憑證就用 restore.sh --extra-neutralize-sql 補一段再重跑
\set QUIET on
BEGIN TRANSACTION READ ONLY;

SELECT 'nfail|database.is_neutralized 未設定|1'
 WHERE NOT EXISTS (SELECT 1 FROM ir_config_parameter WHERE key = 'database.is_neutralized' AND lower(value) = 'true');

SELECT 'nfail|web.base.url 仍被凍結在來源站網址|1'
 WHERE EXISTS (SELECT 1 FROM ir_config_parameter WHERE key = 'web.base.url.freeze' AND lower(value) = 'true');

SELECT 'nfail|仍啟用的排程（autovacuum 以外）|' || count(*) FROM ir_cron c
 WHERE c.active AND c.id NOT IN (SELECT res_id FROM ir_model_data
                                  WHERE model = 'ir.cron' AND module = 'base' AND name = 'autovacuum_job')
HAVING count(*) > 0;
SELECT 'ninfo|已停用的排程|' || count(*) FROM ir_cron WHERE NOT active;

SELECT 'nfail|仍啟用的寄信主機|' || count(*) FROM ir_mail_server
 WHERE active AND name <> 'neutralization - disable emails' HAVING count(*) > 0;
SELECT 'nfail|缺少擋信用的假寄信主機|1'
 WHERE NOT EXISTS (SELECT 1 FROM ir_mail_server WHERE active AND name = 'neutralization - disable emails');

SELECT to_regclass('public.fetchmail_server') IS NOT NULL AS has_fetchmail \gset
\if :has_fetchmail
SELECT 'nfail|仍啟用的收信主機|' || count(*) FROM fetchmail_server WHERE active HAVING count(*) > 0;
\endif

SELECT to_regclass('public.payment_provider') IS NOT NULL AS has_payment \gset
\if :has_payment
SELECT 'nfail|沒有停用的金流供應商（含測試模式）|' || count(*) FROM payment_provider WHERE state <> 'disabled' HAVING count(*) > 0;
\endif

SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'ir_act_server' AND column_name = 'webhook_url') AS has_webhook \gset
\if :has_webhook
SELECT 'nfail|仍會打出去的 webhook 動作|' || count(*) FROM ir_act_server
 WHERE state = 'webhook' AND coalesce(webhook_url, '') NOT LIKE 'neutralization%' HAVING count(*) > 0;
\endif

-- 登入只在還原當下驗：web 起來之後測試的人可以改密碼、平台也可以重設管理員密碼
\if :{?pwhash}
SELECT 'nfail|密碼沒有換成彩排密碼的使用者|' || count(*) FROM res_users
 WHERE password IS NOT NULL AND password <> '' AND password <> :'pwhash' HAVING count(*) > 0;
SELECT 'ninfo|換成彩排密碼的使用者|' || count(*) FROM res_users WHERE password = :'pwhash';
SELECT to_regclass('public.res_users_apikeys') IS NOT NULL AS has_apikeys \gset
\if :has_apikeys
SELECT 'nfail|還在的 API key|' || count(*) FROM res_users_apikeys HAVING count(*) > 0;
\endif
\endif

SELECT count(*) = 3 AS has_ecpay_company FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'res_company'
   AND column_name IN ('ecpay_demo_mode', 'ecpay_MerchantID', 'auto_invoice') \gset
\if :has_ecpay_company
SELECT 'nfail|綠界電子發票不在測試商店／手動開立的公司|' || count(*) FROM res_company
 WHERE ecpay_demo_mode IS NOT TRUE OR "ecpay_MerchantID" IS DISTINCT FROM '2000132' OR auto_invoice <> 'manual'
HAVING count(*) > 0;
\endif

SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'pos_config' AND column_name = 'uber_direct_enabled') AS has_pos_uber \gset
\if :has_pos_uber
SELECT 'nfail|仍啟用 Uber Direct 或留有憑證的 POS|' || count(*) FROM pos_config
 WHERE uber_direct_enabled OR uber_direct_client_secret IS NOT NULL HAVING count(*) > 0;
\endif

SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'pos_printer' AND column_name = 'escpos_proxy_url') AS has_pos_escpos \gset
\if :has_pos_escpos
SELECT 'nfail|仍指向出單代理的印表機|' || count(*) FROM pos_printer
 WHERE escpos_proxy_url IS NOT NULL OR escpos_proxy_api_key IS NOT NULL HAVING count(*) > 0;
\endif

-- 欄名像對外憑證、仍有值的欄位（只列表.欄與筆數）。不列的：
--   access_token（分享連結用的進站 token，欄位多、不是對外憑證）；
--   已由官方或補充 SQL 處理、處理後仍有值的欄位——res_users.password（彩排密碼）、ir_act_server.webhook_url（中和字樣）、
--   iap_account.account_token（加了 +disabled）、pos_config.kds_access_token（已換新）、res_company.ecpay_*（測試商店，上面有斷言）。
SELECT format('SELECT %L || count(*) FROM ONLY %I.%I WHERE %I IS NOT NULL AND %I::text NOT IN (%L, %L) HAVING count(*) > 0',
              'nscan|' || c.table_name || '.' || c.column_name || '|',
              c.table_schema, c.table_name, c.column_name, c.column_name, '', 'false')
  FROM information_schema.columns c
  JOIN information_schema.tables t USING (table_schema, table_name)
 WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
   AND c.data_type IN ('character varying', 'text', 'jsonb')
   AND c.column_name ~* '(api_?key|secret|client_id|hash_?key|hash_?iv|merchant_?id|passw|_pass$|private_key|webhook|proxy_url|_token$|^token$)'
   AND c.column_name <> 'access_token'
   AND (c.table_name, c.column_name) NOT IN (
         ('res_users', 'password'), ('ir_act_server', 'webhook_url'), ('iap_account', 'account_token'),
         ('pos_config', 'kds_access_token'),
         ('res_company', 'ecpay_MerchantID'), ('res_company', 'ecpay_HashKey'), ('res_company', 'ecpay_HashIV'))
 ORDER BY c.table_name, c.column_name \gexec

ROLLBACK;
