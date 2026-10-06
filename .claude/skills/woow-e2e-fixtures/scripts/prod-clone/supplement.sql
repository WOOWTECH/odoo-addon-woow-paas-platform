-- prod clone 中和的補差 SQL（#1206；裁決 #1162 留言 14057 D3-A～D3-E、D6，#1200 量測）
--
-- 由 neutralize-clone.sh 併進同一個交易，順序：
--   刪 dummy mail server → 官方 `odoo neutralize --stdout`（含 #1201 的 src/data/neutralize.sql）
--   → 本檔 → 腳本產生的動態段（沙盒密碼 hash、選配的 upgrade_done marker）。
-- 原則：
--   - 只放不需要執行期參數的靜態 SQL；重跑冪等（Stage B 會在 Odoo 起來後再跑一次整批）。
--   - 官方 SQL 已做的項目在這裡再做一次無害（image 的 Odoo core 可能比 18.0 HEAD 舊，少了幾條）。
--   - vendored addon 的表與欄位一律經 pg_temp.exec_if 檢查存在才執行；不存在就 RAISE NOTICE 留痕、
--     不中斷。漏網（欄位改名）由斷言 fixture 的 fail-closed 檢查接住。
--   - ⛔ 不做：mail.catchall.domain／mail.default.from（Odoo 18 死參數）、mail_alias_domain、
--     mail_message.email_from 與 filestore 內 PDF 的遮罩（明寫接受，靠銷毀與產物規則補償）。

CREATE OR REPLACE FUNCTION pg_temp.has_col(t text, c text) RETURNS boolean
LANGUAGE sql STABLE AS $f$
    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = t AND column_name = c)
$f$;

-- exec_if(<table>, <columns>, <statement>)：表與所有欄位都存在才執行
CREATE OR REPLACE FUNCTION pg_temp.exec_if(t text, cols text[], stmt text) RETURNS void
LANGUAGE plpgsql AS $f$
DECLARE
    c text;
BEGIN
    IF to_regclass('public.' || quote_ident(t)) IS NULL THEN
        RAISE NOTICE 'prod-clone neutralize: skip (table % absent): %', t, left(stmt, 80);
        RETURN;
    END IF;
    FOREACH c IN ARRAY cols LOOP
        IF NOT pg_temp.has_col(t, c) THEN
            RAISE NOTICE 'prod-clone neutralize: skip (column %.% absent): %', t, c, left(stmt, 80);
            RETURN;
        END IF;
    END LOOP;
    EXECUTE stmt;
END
$f$;

-- ───── A 排程 ─────
-- 官方 SQL 已停 cron（只留 base.autovacuum_job 當探針）並設 database.is_neutralized；
-- nextcall 推後與 ir_cron_trigger 由 #1201 的平台段負責。這裡只停 base_automation（#1200：兩筆都是 n8n webhook）。
SELECT pg_temp.exec_if('base_automation', ARRAY['active'],
    $q$UPDATE base_automation SET active = false WHERE active$q$);

-- ───── B 外連 ─────
-- SMTP：較新的 18.0 官方 SQL 才清憑證；webhook 同。停用不刪（刪了會 fallback 到 conf 的 localhost:25）。
UPDATE ir_mail_server SET smtp_user = NULL, smtp_pass = NULL
 WHERE smtp_user IS NOT NULL OR smtp_pass IS NOT NULL;
SELECT pg_temp.exec_if('ir_act_server', ARRAY['webhook_url'],
    $q$UPDATE ir_act_server SET webhook_url = 'neutralization - disable webhook'
        WHERE state = 'webhook' AND webhook_url IS DISTINCT FROM 'neutralization - disable webhook'$q$);
SELECT pg_temp.exec_if('ir_mail_server',
    ARRAY['google_gmail_authorization_code', 'google_gmail_refresh_token',
          'google_gmail_access_token', 'google_gmail_access_token_expiration'],
    $q$UPDATE ir_mail_server
          SET google_gmail_authorization_code = NULL, google_gmail_refresh_token = NULL,
              google_gmail_access_token = NULL, google_gmail_access_token_expiration = NULL$q$);
SELECT pg_temp.exec_if('mail_template', ARRAY['mail_server_id'],
    $q$UPDATE mail_template SET mail_server_id = NULL WHERE mail_server_id IS NOT NULL$q$);
SELECT pg_temp.exec_if('mail_mail', ARRAY['state'],
    $q$UPDATE mail_mail SET state = 'cancel' WHERE state = 'outgoing'$q$);
SELECT pg_temp.exec_if('fetchmail_server', ARRAY['active'],
    $q$UPDATE fetchmail_server SET active = false WHERE active$q$);
DELETE FROM ir_config_parameter
 WHERE key IN ('mail.web_push_vapid_private_key', 'mail.web_push_vapid_public_key', 'mail.sfu_server_key');
SELECT pg_temp.exec_if('mail_push_device', ARRAY['id'], $q$DELETE FROM mail_push_device$q$);
SELECT pg_temp.exec_if('mail_push', ARRAY['id'], $q$DELETE FROM mail_push$q$);
-- IAP：後綴 +disabled（清空會讓 Odoo 自建新的 live token；database.is_neutralized 已由官方 SQL 先設）
SELECT pg_temp.exec_if('iap_account', ARRAY['account_token'],
    $q$UPDATE iap_account SET account_token = REGEXP_REPLACE(account_token, '(\+.*)?$', '+disabled')
        WHERE account_token IS NOT NULL AND account_token NOT LIKE '%+disabled' AND LENGTH(account_token) <= 33$q$);
SELECT pg_temp.exec_if('iap_account', ARRAY['account_token'],
    $q$UPDATE iap_account SET account_token = 'dummy_value+disabled'
        WHERE account_token IS NOT NULL AND account_token NOT LIKE '%+disabled'$q$);
-- 金流：provider 停用只是改打 stage 站台、仍帶正式 HashKey；真正阻斷是 cron 停＋NP
SELECT pg_temp.exec_if('payment_provider', ARRAY['state'],
    $q$UPDATE payment_provider SET state = 'disabled' WHERE state NOT IN ('test', 'disabled')$q$);
-- 電子發票（ecpay_invoice_tw；憑證在 res_company、action_post 同步開票不經 cron）：
-- 測試模式＋ECPay 官方公開測試商店碼（與該 addon 欄位 default 相同）
SELECT pg_temp.exec_if('res_company',
    ARRAY['ecpay_demo_mode', 'ecpay_MerchantID', 'ecpay_HashKey', 'ecpay_HashIV'],
    $q$UPDATE res_company
          SET ecpay_demo_mode = true, "ecpay_MerchantID" = '2000132',
              "ecpay_HashKey" = 'ejCk326UnaZWKisg', "ecpay_HashIV" = 'q9jcZX8Ib9LM8wYk'$q$);
SELECT pg_temp.exec_if('res_company', ARRAY['auto_invoice'],
    $q$UPDATE res_company SET auto_invoice = 'manual' WHERE auto_invoice IS DISTINCT FROM 'manual'$q$);
-- LINE 三處：line_liff_config 5 欄＋ir_config_parameter woow_line_base.* 5 key（ORM 鏡像，兩邊都要清）
-- ＋im_livechat_channel.line_channel_secret
SELECT pg_temp.exec_if('line_liff_config',
    ARRAY['messaging_channel_id', 'messaging_channel_secret', 'messaging_access_token',
          'login_channel_id', 'login_channel_secret'],
    $q$UPDATE line_liff_config
          SET messaging_channel_id = NULL, messaging_channel_secret = NULL, messaging_access_token = NULL,
              login_channel_id = NULL, login_channel_secret = NULL$q$);
DELETE FROM ir_config_parameter
 WHERE key IN ('woow_line_base.messaging_access_token', 'woow_line_base.messaging_channel_id',
               'woow_line_base.messaging_channel_secret', 'woow_line_base.login_channel_id',
               'woow_line_base.login_channel_secret');
SELECT pg_temp.exec_if('im_livechat_channel', ARRAY['line_channel_secret'],
    $q$UPDATE im_livechat_channel SET line_channel_secret = NULL WHERE line_channel_secret IS NOT NULL$q$);
-- livechat AI（im_livechat_ai）
SELECT pg_temp.exec_if('im_livechat_channel', ARRAY['ai_enabled', 'ai_api_key'],
    $q$UPDATE im_livechat_channel SET ai_enabled = false, ai_api_key = NULL
        WHERE ai_enabled OR ai_api_key IS NOT NULL$q$);
-- Google Drive（cloud_link_gdrive）
SELECT pg_temp.exec_if('cl_link_drive_gdrive',
    ARRAY['gdrive_service_account_info', 'gdrive_client_secret_file', 'gdrive_refresh_token'],
    $q$UPDATE cl_link_drive_gdrive
          SET gdrive_service_account_info = NULL, gdrive_client_secret_file = NULL, gdrive_refresh_token = NULL$q$);
-- ai_base_gt（#1201 平台段也做；這裡保底，image 較舊時同樣生效）
SELECT pg_temp.exec_if('ai_config', ARRAY['api_key'],
    $q$UPDATE ai_config SET api_key = NULL WHERE api_key IS NOT NULL$q$);
-- Google Calendar（官方 google_calendar SQL 已做；保留給斷言對照）
SELECT pg_temp.exec_if('res_users_settings', ARRAY['google_calendar_rtoken', 'google_calendar_token'],
    $q$UPDATE res_users_settings SET google_calendar_rtoken = NULL, google_calendar_token = NULL
        WHERE google_calendar_rtoken IS NOT NULL OR google_calendar_token IS NOT NULL$q$);

-- ───── C 身分 ─────
-- web.base.url 指 localhost（預設禁 --ingress）＋freeze；website.domain 清空（#1200：prod 為 https://aiot.woowtech.io）
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES ('web.base.url', 'http://localhost:8069', 1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC')),
       ('web.base.url.freeze', 'True', 1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = 1, write_date = EXCLUDED.write_date;
SELECT pg_temp.exec_if('website', ARRAY['domain'],
    $q$UPDATE website SET domain = NULL WHERE domain IS NOT NULL$q$);
-- database.secret → 每次換新的 dummy；database.uuid 輪替成 v4（Odoo 自己產的是 uuid1 ⇒ 版本位 '4' 即已輪替，重跑不再換）。
-- ⚠ 正式切換必須維持原值（#1142 留言 14050 ②）——這兩條只屬於彩排。
INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
VALUES ('database.secret', 'neutralized-' || gen_random_uuid()::text, 1,
        (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = 1, write_date = EXCLUDED.write_date;
UPDATE ir_config_parameter SET value = gen_random_uuid()::text, write_uid = 1, write_date = (now() AT TIME ZONE 'UTC')
 WHERE key = 'database.uuid' AND substr(value, 15, 1) <> '4';

-- ───── D 進入面（DB 層；conf 層的 admin_passwd／list_db 由 chart／plugin 處理）─────
SELECT pg_temp.exec_if('res_users', ARRAY['oauth_access_token'],
    $q$UPDATE res_users SET oauth_access_token = NULL WHERE oauth_access_token IS NOT NULL$q$);
SELECT pg_temp.exec_if('res_users', ARRAY['totp_secret'],
    $q$UPDATE res_users SET totp_secret = NULL WHERE totp_secret IS NOT NULL$q$);
SELECT pg_temp.exec_if('res_users_apikeys', ARRAY['id'], $q$DELETE FROM res_users_apikeys$q$);
SELECT pg_temp.exec_if('auth_totp_device', ARRAY['id'], $q$DELETE FROM auth_totp_device$q$);
SELECT pg_temp.exec_if('auth_oauth_provider', ARRAY['enabled'],
    $q$UPDATE auth_oauth_provider SET enabled = false WHERE enabled$q$);
-- vendored mcp_server 的 OAuth token（#1200：prod 未安裝、表不存在；保留 guard 給日後）
SELECT pg_temp.exec_if('mcp_oauth_token', ARRAY['revoked'],
    $q$UPDATE mcp_oauth_token SET revoked = true WHERE NOT revoked$q$);

-- ───── PII 遮罩（D6：預設一律遮罩；res_users.login 不動）─────
-- email_normalized／phone_sanitized 是 stored compute，SQL 改 email／phone 不會連動，一併改。
SELECT pg_temp.exec_if('res_partner', ARRAY['email'],
    $q$UPDATE res_partner SET email = 'p' || id || '@sandbox.invalid'
        WHERE email IS NOT NULL AND email NOT LIKE '%@sandbox.invalid'$q$);
SELECT pg_temp.exec_if('res_partner', ARRAY['email_normalized'],
    $q$UPDATE res_partner SET email_normalized = lower(email)
        WHERE email_normalized IS DISTINCT FROM lower(email)$q$);
SELECT pg_temp.exec_if('res_partner', ARRAY['phone'],
    $q$UPDATE res_partner SET phone = '+0-000-' || id WHERE phone IS NOT NULL AND phone NOT LIKE '+0-000-%'$q$);
SELECT pg_temp.exec_if('res_partner', ARRAY['mobile'],
    $q$UPDATE res_partner SET mobile = '+0-000-' || id WHERE mobile IS NOT NULL AND mobile NOT LIKE '+0-000-%'$q$);
SELECT pg_temp.exec_if('res_partner', ARRAY['phone_sanitized'],
    $q$UPDATE res_partner SET phone_sanitized = NULL WHERE phone_sanitized IS NOT NULL$q$);
-- hr_employee 的 work_email／mobile_phone／work_phone 是 stored compute（來自 work_contact），另下 UPDATE
SELECT pg_temp.exec_if('hr_employee', ARRAY['private_email'],
    $q$UPDATE hr_employee SET private_email = 'e' || id || '@sandbox.invalid'
        WHERE private_email IS NOT NULL AND private_email NOT LIKE '%@sandbox.invalid'$q$);
SELECT pg_temp.exec_if('hr_employee', ARRAY['work_email'],
    $q$UPDATE hr_employee SET work_email = 'w' || id || '@sandbox.invalid'
        WHERE work_email IS NOT NULL AND work_email NOT LIKE '%@sandbox.invalid'$q$);
SELECT pg_temp.exec_if('hr_employee', ARRAY['private_phone'],
    $q$UPDATE hr_employee SET private_phone = '+0-000-' || id
        WHERE private_phone IS NOT NULL AND private_phone NOT LIKE '+0-000-%'$q$);
SELECT pg_temp.exec_if('hr_employee', ARRAY['mobile_phone'],
    $q$UPDATE hr_employee SET mobile_phone = '+0-000-' || id
        WHERE mobile_phone IS NOT NULL AND mobile_phone NOT LIKE '+0-000-%'$q$);
SELECT pg_temp.exec_if('hr_employee', ARRAY['work_phone'],
    $q$UPDATE hr_employee SET work_phone = '+0-000-' || id
        WHERE work_phone IS NOT NULL AND work_phone NOT LIKE '+0-000-%'$q$);

-- ───── 讓已起來的 Odoo worker 丟掉快取（Stage B 在 web 已起時跑；ir.config_parameter 是 ormcache）─────
DO $f$
DECLARE
    s text;
BEGIN
    FOR s IN SELECT sequence_name FROM information_schema.sequences
              WHERE sequence_schema = 'public'
                AND (sequence_name = 'base_registry_signaling' OR sequence_name LIKE 'base\_cache\_signaling%')
    LOOP
        EXECUTE format('SELECT nextval(%L)', s);
    END LOOP;
END
$f$;
