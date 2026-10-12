-- neutralize-supplement.sql — 彩排中和的補充 SQL（standalone-odoo-migrate，#1604）
--
-- 由 restore-in-pod.sh 接在官方中和 SQL（`odoo neutralize --stdout`：排程、寄信、收信、金流供應商、webhook、
-- 網站網域…）之後、同一個交易內執行。這裡補官方不管的三類：
--   1. 登入與信箱密碼：彩排站有對外網址，真實使用者的密碼、API key、2FA、寄收信主機的密碼不能原樣留著。
--   2. 升級不得把中和改回去：-u all 會重載沒有 noupdate 的模組資料。
--   3. WoowTech 自家 addon 的對外整合（它們沒有附 data/neutralize.sql）：綠界發票／金流、POS 自助點餐的
--      Uber Direct 與出單代理。每段都先看欄位在不在，沒裝那個模組就跳過。
-- 其他站別特有的對外設定用 restore.sh --extra-neutralize-sql 追加；中和後的 neutralize-report.txt 會列出
-- 名字像憑證、仍有值的欄位（nscan 行）供人檢查。
--
-- psql 變數：pwhash＝彩排密碼的 pbkdf2_sha512 hash（由 release 的 odoo image 算出）。
-- 只給彩排用：中和過的資料庫不得拿去做正式切換。

-- ── 1. 登入 ──────────────────────────────────────────────────────────────
UPDATE res_users SET password = :'pwhash' WHERE password IS NOT NULL AND password <> '';

SELECT to_regclass('public.res_users_apikeys') IS NOT NULL AS has_apikeys \gset
\if :has_apikeys
DELETE FROM res_users_apikeys;
\endif

SELECT to_regclass('public.auth_totp_device') IS NOT NULL AS has_totp_device \gset
\if :has_totp_device
DELETE FROM auth_totp_device;
\endif

SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'res_users' AND column_name = 'totp_secret') AS has_totp \gset
\if :has_totp
UPDATE res_users SET totp_secret = NULL WHERE totp_secret IS NOT NULL;
\endif

-- 寄信／收信主機已被官方 SQL 停用，但帳號密碼還在：彩排站不需要，清掉
UPDATE ir_mail_server SET smtp_pass = NULL WHERE smtp_pass IS NOT NULL;
SELECT to_regclass('public.fetchmail_server') IS NOT NULL AS has_fetchmail \gset
\if :has_fetchmail
UPDATE fetchmail_server SET password = NULL WHERE password IS NOT NULL;
\endif

-- web.base.url 還是來源站的正式網址：彩排站產生的連結（入口網站、QR code、金流回呼）會指回正式站。
-- 拿掉凍結旗標，第一個管理員從彩排站網址登入時 Odoo 會自動把它改成彩排站的網址。
DELETE FROM ir_config_parameter WHERE key = 'web.base.url.freeze';

-- ── 2. 排程的模組資料改成 noupdate：-u all 不會把 active 設回 true ────────────────
UPDATE ir_model_data SET noupdate = true WHERE model = 'ir.cron' AND noupdate IS NOT TRUE;

-- ── 2b. 金流供應商全部停用 ──────────────────────────────────────────
-- 官方 SQL 只停用 enabled 的，測試模式（test）的留著。但 payment 模組每次升級都依「有沒有非停用的供應商」重設
-- 「交易後處理」排程 ⇒ 只要留一個 test 的供應商（harumi 留著 Odoo 內建的示範金流），升級後那顆排程就被開回來。
-- 彩排站不需要任何金流。color 是 state 的 stored compute，SQL 改 state 不會連動；不一起改的話，升級時 ORM 重算
-- 會讓這幾列在升級後比對顯示成「被升級動到」（2＝test 的橘色、7＝enabled 的綠色、3＝disabled 的黃色）。
SELECT count(*) = 2 AS has_payment_provider FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'payment_provider' AND column_name IN ('state', 'color') \gset
\if :has_payment_provider
UPDATE payment_provider SET state = 'disabled' WHERE state <> 'disabled';
UPDATE payment_provider SET color = 3 WHERE state = 'disabled' AND color IN (2, 7);
\endif

-- ── 3a. ecpay_invoice_tw：公司層的綠界電子發票憑證 ─────────────────────────────
-- 換成模組內建的綠界公開測試商店＋測試模式＋手動開立，彩排站不會對真實商店開發票。
SELECT count(*) = 5 AS has_ecpay_company FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'res_company'
   AND column_name IN ('ecpay_demo_mode', 'ecpay_MerchantID', 'ecpay_HashKey', 'ecpay_HashIV', 'auto_invoice') \gset
\if :has_ecpay_company
UPDATE res_company
   SET ecpay_demo_mode = true, "ecpay_MerchantID" = '2000132', "ecpay_HashKey" = 'ejCk326UnaZWKisg',
       "ecpay_HashIV" = 'q9jcZX8Ib9LM8wYk', auto_invoice = 'manual';
\endif

-- ── 3b. payment_ecpay：金流供應商的商店憑證（供應商本身已在上面停用）──────────────
SELECT count(*) = 3 AS has_ecpay_provider FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'payment_provider'
   AND column_name IN ('MerchantID', 'HashKey', 'HashIV') \gset
\if :has_ecpay_provider
UPDATE payment_provider SET "MerchantID" = NULL, "HashKey" = NULL, "HashIV" = NULL
 WHERE "MerchantID" IS NOT NULL OR "HashKey" IS NOT NULL OR "HashIV" IS NOT NULL;
\endif

-- ── 3c. pos_self_order_enhancement：Uber Direct、POS 電子發票、廚房顯示的存取 token ──────
SELECT count(*) = 4 AS has_pos_uber FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'pos_config'
   AND column_name IN ('uber_direct_enabled', 'uber_direct_client_id', 'uber_direct_client_secret',
                       'uber_direct_customer_id') \gset
\if :has_pos_uber
UPDATE pos_config
   SET uber_direct_enabled = false, uber_direct_client_id = NULL, uber_direct_client_secret = NULL,
       uber_direct_customer_id = NULL;
\endif

SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'pos_config' AND column_name = 'ecpay_einvoice_enabled') AS has_pos_einvoice \gset
\if :has_pos_einvoice
UPDATE pos_config SET ecpay_einvoice_enabled = false WHERE ecpay_einvoice_enabled;
\endif

SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
               AND table_name = 'pos_config' AND column_name = 'kds_access_token') AS has_pos_kds \gset
\if :has_pos_kds
-- 廚房顯示頁用這個 token 進來：換掉，正式站在用的連結打不進彩排站
UPDATE pos_config SET kds_access_token = md5(random()::text || id::text) WHERE kds_access_token IS NOT NULL;
\endif

-- ── 3d. pos_self_order_enhancement：出單代理（伺服器端把訂單送到店內印表機）──────────
SELECT count(*) = 3 AS has_pos_escpos FROM information_schema.columns
 WHERE table_schema = 'public' AND table_name = 'pos_printer'
   AND column_name IN ('escpos_proxy_url', 'escpos_proxy_api_key', 'escpos_printer_ip') \gset
\if :has_pos_escpos
UPDATE pos_printer SET escpos_proxy_url = NULL, escpos_proxy_api_key = NULL, escpos_printer_ip = NULL
 WHERE escpos_proxy_url IS NOT NULL OR escpos_proxy_api_key IS NOT NULL OR escpos_printer_ip IS NOT NULL;
\endif
