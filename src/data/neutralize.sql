-- woow_paas_platform neutralize (#1201)
--
-- 平台 addon 的中和補差（#1162 裁決 R2-Q1／R2-Q2）。第一層是 Odoo 官方
-- `odoo neutralize`：它對每個已安裝模組讀 `<module>/data/neutralize.sql`，
-- 所以本檔不登記 manifest。`--stdout` 把各模組這支檔 `.strip()` 後
-- `rstrip(";") + ";"` 串成一個 `BEGIN; … COMMIT;` 交易 ⇒ 本檔最後一行必須是以
-- `;` 結尾的 statement，不可留只有註解的尾段（尾段註解會吃掉補上的 `;`）。
--
-- ⚠ 第一行是固定 marker：#1206 的 neutralize-clone.sh 用它判斷 image 內的 addon
-- 是否已含本段，不要改字。
--
-- 範圍：只放不需要執行期參數的靜態 SQL，且只引用本 addon 與 manifest `depends`
-- 模組的表。⛔ 不放：密碼 hash（等於隨 addon 發佈已知密碼）、`web.base.url`、
-- PII 遮罩、任何需要沙盒 host 的值——那些在 neutralize-clone.sh（#1206）。
-- 憑證欄位一律只改「有值」的列：空值保持空值，「沒設定」的語意不變；斷言以
-- 「有值且 ≠ 'neutralized'」的筆數 = 0 判定。
-- 本檔可重複執行（彩排 Stage A／Stage B 各跑一次）。

-- ---------------------------------------------------------------------------
-- D4 operator：黑洞 sentinel（不清空——`src/controllers/paas.py` 與
-- `src/models/cloud_service.py` 對 None 靜默 return，其餘呼叫點會報錯；清空反而
-- 讓「是否已中和」看不出來）。127.0.0.1:9 連線立即被拒，不經 DNS。
-- D3-C 身分：deploy_env／namespace_prefix 改 stg；D3-B：新專案不自動開 agent
-- runtime。鍵可能不存在（res.config.settings 沒存過）⇒ upsert。
-- ---------------------------------------------------------------------------
INSERT INTO ir_config_parameter (key, value)
VALUES ('woow_paas_platform.operator_url', 'http://127.0.0.1:9'),
       ('woow_paas_platform.operator_api_key', 'neutralized'),
       ('woow_paas_platform.deploy_env', 'stg'),
       ('woow_paas_platform.namespace_prefix', 'paas-ws-stg-'),
       ('woow_paas_platform.default_enable_agent_runtime', 'False')
    ON CONFLICT (key) DO
       UPDATE SET value = EXCLUDED.value;

-- ---------------------------------------------------------------------------
-- D3-B agent runtime：有 pod 的 session 一律標 reaped（維持 model 的
-- 「reaped_since 有值 ⟺ state='reaped'」invariant）。'absent' 本來就沒有 pod，
-- 不改（改成 reaped 會讓 GC 把一個不存在的 namespace 排進回收）。
-- ---------------------------------------------------------------------------
UPDATE woow_paas_platform_agent_session
   SET state = 'reaped',
       reaped_since = COALESCE(reaped_since, NOW() AT TIME ZONE 'UTC')
 WHERE state NOT IN ('reaped', 'absent');

-- ---------------------------------------------------------------------------
-- D3-B MCP：停用全部 server（不再對租戶 sidecar URL 同步 tools）；api_key 換
-- sentinel。headers_json 可夾帶 `Authorization: Bearer …`（欄位說明即此用途），
-- 與 api_key 同屬「拿到就能在沙盒外用」的憑證，一併清掉。
-- ---------------------------------------------------------------------------
UPDATE woow_paas_platform_mcp_server
   SET active = false;
UPDATE woow_paas_platform_mcp_server
   SET api_key = 'neutralized'
 WHERE api_key IS NOT NULL AND api_key <> '';
UPDATE woow_paas_platform_mcp_server
   SET headers_json = NULL
 WHERE headers_json IS NOT NULL;

-- ---------------------------------------------------------------------------
-- D3-B AI：清 LLM API key。ai_config 屬 ai_base_gt（manifest depends 內），照理
-- 一定在；仍以 to_regclass() guard，避免上游換掉 model 時整批中和交易炸掉。
-- PL/pgSQL 的 statement 在執行到時才 plan ⇒ 表不存在時 IF 分支內的 UPDATE
-- 不會被解析。
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('ai_config') IS NOT NULL THEN
        UPDATE ai_config
           SET api_key = NULL
         WHERE api_key IS NOT NULL;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- D3-B 郵件：佇列中待送的信改 cancel（官方 SQL 只換 mail server，不動佇列）。
-- ---------------------------------------------------------------------------
UPDATE mail_mail
   SET state = 'cancel'
 WHERE state = 'outgoing';

-- ---------------------------------------------------------------------------
-- D3-E 平台 DB 內的 prod 租戶 live 憑證：拿到就能在沙盒外用，NP 擋不住。
-- ---------------------------------------------------------------------------
UPDATE woow_paas_platform_security_access
   SET tunnel_token = 'neutralized'
 WHERE tunnel_token IS NOT NULL AND tunnel_token <> '';
UPDATE woow_paas_platform_smart_home
   SET tunnel_token = 'neutralized'
 WHERE tunnel_token IS NOT NULL AND tunnel_token <> '';
UPDATE woow_paas_platform_cloud_service_secret
   SET admin_password = 'neutralized'
 WHERE admin_password IS NOT NULL AND admin_password <> '';
UPDATE woow_paas_platform_cloud_service_secret
   SET gateway_api_key = 'neutralized'
 WHERE gateway_api_key IS NOT NULL AND gateway_api_key <> '';
UPDATE woow_paas_platform_cloud_service
   SET mcp_auth_token = 'neutralized'
 WHERE mcp_auth_token IS NOT NULL AND mcp_auth_token <> '';
UPDATE woow_paas_platform_cloud_service
   SET n8n_api_key = 'neutralized'
 WHERE n8n_api_key IS NOT NULL AND n8n_api_key <> '';
UPDATE woow_paas_platform_cloud_service
   SET n8n_owner_password = 'neutralized'
 WHERE n8n_owner_password IS NOT NULL AND n8n_owner_password <> '';

-- ---------------------------------------------------------------------------
-- D3-D 進入面：本 addon 的 OAuth token／code／device code 是明碼、refresh 30 天、
-- 端點 auth='none' ⇒ 全刪。oauth_client 不動（client secret 已 hash）。三表之間
-- 沒有 FK，刪除順序無關。
-- ---------------------------------------------------------------------------
DELETE FROM woow_paas_platform_oauth_token;
DELETE FROM woow_paas_platform_oauth_code;
DELETE FROM woow_paas_platform_oauth_device_code;

-- ---------------------------------------------------------------------------
-- D3-A 排程（第二保險）：官方 SQL 已停掉 autovacuum 以外的全部 cron；這裡再把
-- nextcall 推到 100 年後，萬一有 cron 被開回來也不會跑。寫成 GREATEST(…) 而
-- 不是 `nextcall + 100 years`：重複執行不會一直往後疊，且結果恆 ≥ 現在＋100 年
-- （斷言可直接比 now）。base.autovacuum_job 與官方 SQL 同樣排除——它是彩排的
-- cron 探針（#1162 D3-A），推到 100 年後探針就失效。
-- ir_cron_trigger 全刪：已排定的立即觸發不經 nextcall。
-- ---------------------------------------------------------------------------
UPDATE ir_cron
   SET nextcall = GREATEST(nextcall, (NOW() AT TIME ZONE 'UTC') + INTERVAL '100 years')
 WHERE id NOT IN (
       SELECT res_id
         FROM ir_model_data
        WHERE model = 'ir.cron'
          AND name = 'autovacuum_job'
          AND module = 'base'
);
DELETE FROM ir_cron_trigger;
