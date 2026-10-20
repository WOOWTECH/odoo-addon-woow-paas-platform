-- rename-module.sql — 把 DB 裡已安裝模組的技術名從舊名改成新名（standalone-odoo-migrate，#1878）
--
-- 用途：站上裝的模組技術名跟 repo 的正式名不同（例 well-101 的 cs_portal_discuss → woow_odoo_portal_discuss、
--   hsinpoli 的 woow_line_bridge → woow_odoo_line_liff）。先在 DB 改名，再用正式名的程式碼 -u；
--   xmlid 對得上，記錄（含 noupdate 的資料與設定）就不會被當成孤兒刪掉重建。
-- 用法（odoo 停著；以 DB owner 或 superuser 連進 Odoo 的 DB）：
--   psql -X -q -tA -v ON_ERROR_STOP=1 -v old=<舊名> -v new=<新名> [-v apply=1] [-v scan=0] -f rename-module.sql
--   apply 沒給或 0＝dry-run：照樣全部執行、印出筆數，最後 ROLLBACK；apply=1 才 COMMIT。
--   scan=0 跳過最後的殘留掃描（大庫省時間；預設掃）。
-- 輸出（-tA）：
--   rename|<表.欄>|<筆數>   每一項改了幾列（dry-run 時是「會改」幾列）
--   rscan|<表.欄>|<筆數>    改完之後仍含舊名（整詞比對）的欄位，只列筆數；逐條看是不是要另外處理
--   rename-ok|commit 或 rename-ok|dry-run
-- 冪等：已經改過再跑一次，每項都是 0。全部在一個交易內，任何一步失敗整筆回滾。
--
-- 處理的欄位（依 Odoo 18 原始碼 odoo/addons/base/models 核對）：
--   ir_module_module.name、.icon（/<舊>/static/…）
--   ir_module_module_dependency.name、ir_module_module_exclusion.name（別的模組相依／互斥它）
--   ir_model_data.module（整個模組的 xmlid）、base.module_<舊>（模組本身的 xmlid，odoo/modules/db.py）
--   ir_ui_view.key（<舊>.xxx，含 website 的 COW 複本）、.arch_db（jsonb，整詞取代：t-call、inherit、/舊/static 路徑、
--     data-key 等）、.arch_prev、.arch_fs（<舊>/views/…）
--   ir_ui_menu.web_icon（<舊>,static/…）
--   ir_asset.path、.target（<舊>/… 或 /<舊>/…）、.bundle（<舊>.xxx）
--   ir_attachment.url（/<舊>/…）
--   ir_config_parameter.key（<舊>.xxx：模組自己的設定，例 woow_line_bridge.liff_id_member）
-- 不需要處理：ir_model_constraint.module、ir_model_relation.module 是指向 ir_module_module 的 FK；
--   ir_model.modules、ir_model_fields.modules 是 compute、不存 DB。
-- 新名已經有一列 ir_module_module（例 update_list 掃到新程式碼）：狀態是 uninstalled 而且沒有任何資料才刪掉它再改名，
--   否則中止。舊名、新名都找不到也中止。
\if :{?old}
\else
\echo 'rename-module.sql: 要給 -v old=<舊名> -v new=<新名>'
\quit 2
\endif
\if :{?new}
\else
\echo 'rename-module.sql: 要給 -v old=<舊名> -v new=<新名>'
\quit 2
\endif
\if :{?apply}
\else
\set apply 0
\endif
\if :{?scan}
\else
\set scan 1
\endif

BEGIN;

CREATE FUNCTION pg_temp.woow_rename_module(o text, n text)
RETURNS TABLE (item text, cnt bigint) LANGUAGE plpgsql AS $fn$
DECLARE
    old_id int; old_state text; new_id int; new_state text;
    -- 整詞比對：前後不是 [A-Za-z0-9_]（模組名只含 [a-z0-9_]，不用跳脫；lookaround 不吃掉邊界字元，
    -- 相鄰兩次出現也一次換完，重跑才會是 0）
    word text := '(?<![A-Za-z0-9_])' || o || '(?![A-Za-z0-9_])';
    c bigint;
BEGIN
    IF o !~ '^[a-z0-9_]+$' OR n !~ '^[a-z0-9_]+$' OR o = n THEN
        RAISE EXCEPTION 'rename-module: 模組名不合法或相同（old=%, new=%）', o, n;
    END IF;
    SELECT id, state INTO old_id, old_state FROM ir_module_module WHERE name = o;
    SELECT id, state INTO new_id, new_state FROM ir_module_module WHERE name = n;
    IF old_id IS NULL AND new_id IS NULL THEN
        RAISE EXCEPTION 'rename-module: ir_module_module 找不到 % 也找不到 %', o, n;
    END IF;

    -- 新名已有一列：只接受「update_list 掃到、從沒裝過」的空殼
    IF old_id IS NOT NULL AND new_id IS NOT NULL THEN
        IF new_state <> 'uninstalled' THEN
            RAISE EXCEPTION 'rename-module: % 與 % 都在 ir_module_module，而且 % 的狀態是 %', o, n, n, new_state;
        END IF;
        IF EXISTS (SELECT 1 FROM ir_model_data WHERE module = n) THEN
            RAISE EXCEPTION 'rename-module: % 是 uninstalled，但 ir_model_data 已有它的資料', n;
        END IF;
        DELETE FROM ir_model_data WHERE module = 'base' AND model = 'ir.module.module' AND res_id = new_id;
        DELETE FROM ir_module_module WHERE id = new_id;   -- dependency／exclusion 是 ON DELETE CASCADE
        item := 'ir_module_module.placeholder_dropped'; cnt := 1; RETURN NEXT;
    END IF;

    IF old_id IS NOT NULL THEN
        RAISE NOTICE 'rename-module: % (id %, %) → %', o, old_id, old_state, n;
        UPDATE ir_module_module SET name = n WHERE id = old_id;
        GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_module_module.name'; cnt := c; RETURN NEXT;
    ELSE
        item := 'ir_module_module.name'; cnt := 0; RETURN NEXT;   -- 已改過
    END IF;

    UPDATE ir_module_module SET icon = '/' || n || substr(icon, length(o) + 2)
     WHERE starts_with(icon, '/' || o || '/');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_module_module.icon'; cnt := c; RETURN NEXT;

    UPDATE ir_module_module_dependency SET name = n WHERE name = o;
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_module_module_dependency.name'; cnt := c; RETURN NEXT;

    UPDATE ir_module_module_exclusion SET name = n WHERE name = o;
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_module_module_exclusion.name'; cnt := c; RETURN NEXT;

    -- 模組本身的 xmlid：base.module_<名>
    UPDATE ir_model_data SET name = 'module_' || n
     WHERE module = 'base' AND model = 'ir.module.module' AND name = 'module_' || o;
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_model_data.name(base.module_)'; cnt := c; RETURN NEXT;

    -- 整個模組的 xmlid；(module, name) 有 unique，撞到就整筆失敗
    UPDATE ir_model_data SET module = n WHERE module = o;
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_model_data.module'; cnt := c; RETURN NEXT;

    UPDATE ir_ui_view SET key = n || substr(key, length(o) + 1) WHERE starts_with(key, o || '.');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_ui_view.key'; cnt := c; RETURN NEXT;

    UPDATE ir_ui_view SET arch_db = regexp_replace(arch_db::text, word, n, 'g')::jsonb
     WHERE arch_db::text ~ word;
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_ui_view.arch_db'; cnt := c; RETURN NEXT;

    UPDATE ir_ui_view SET arch_prev = regexp_replace(arch_prev, word, n, 'g')
     WHERE arch_prev ~ word;
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_ui_view.arch_prev'; cnt := c; RETURN NEXT;

    UPDATE ir_ui_view SET arch_fs = n || substr(arch_fs, length(o) + 1) WHERE starts_with(arch_fs, o || '/');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_ui_view.arch_fs'; cnt := c; RETURN NEXT;

    UPDATE ir_ui_menu SET web_icon = n || substr(web_icon, length(o) + 1) WHERE starts_with(web_icon, o || ',');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_ui_menu.web_icon'; cnt := c; RETURN NEXT;

    UPDATE ir_asset SET path = CASE
            WHEN starts_with(path, o || '/') THEN n || substr(path, length(o) + 1)
            ELSE '/' || n || substr(path, length(o) + 2) END
     WHERE starts_with(path, o || '/') OR starts_with(path, '/' || o || '/');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_asset.path'; cnt := c; RETURN NEXT;

    UPDATE ir_asset SET target = CASE
            WHEN starts_with(target, o || '/') THEN n || substr(target, length(o) + 1)
            ELSE '/' || n || substr(target, length(o) + 2) END
     WHERE starts_with(target, o || '/') OR starts_with(target, '/' || o || '/');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_asset.target'; cnt := c; RETURN NEXT;

    UPDATE ir_asset SET bundle = n || substr(bundle, length(o) + 1) WHERE starts_with(bundle, o || '.');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_asset.bundle'; cnt := c; RETURN NEXT;

    UPDATE ir_attachment SET url = '/' || n || substr(url, length(o) + 2) WHERE starts_with(url, '/' || o || '/');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_attachment.url'; cnt := c; RETURN NEXT;

    -- 模組自己的設定 <舊>.xxx：新 key 已存在而且值不同 ⇒ 中止（不猜哪一份對）；值相同 ⇒ 刪舊的
    IF EXISTS (SELECT 1 FROM ir_config_parameter p JOIN ir_config_parameter q
                 ON q.key = n || substr(p.key, length(o) + 1)
                WHERE starts_with(p.key, o || '.') AND q.value IS DISTINCT FROM p.value) THEN
        RAISE EXCEPTION 'rename-module: ir_config_parameter 的 %.* 與 %.* 同名 key 值不同', o, n;
    END IF;
    DELETE FROM ir_config_parameter p USING ir_config_parameter q
     WHERE starts_with(p.key, o || '.') AND q.key = n || substr(p.key, length(o) + 1);
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_config_parameter.key(duplicate_dropped)'; cnt := c; RETURN NEXT;
    UPDATE ir_config_parameter SET key = n || substr(key, length(o) + 1) WHERE starts_with(key, o || '.');
    GET DIAGNOSTICS c = ROW_COUNT; item := 'ir_config_parameter.key'; cnt := c; RETURN NEXT;

    -- 收尾檢查
    IF EXISTS (SELECT 1 FROM ir_module_module WHERE name = o) OR EXISTS (SELECT 1 FROM ir_model_data WHERE module = o) THEN
        RAISE EXCEPTION 'rename-module: 改完仍有 % 的 ir_module_module／ir_model_data', o;
    END IF;
END
$fn$;

-- 殘留掃描：public 底下所有文字欄位（text／varchar／jsonb）仍含舊名（整詞）的列數；只列 > 0 的
CREATE FUNCTION pg_temp.woow_rename_scan(o text)
RETURNS TABLE (item text, cnt bigint) LANGUAGE plpgsql AS $fn$
DECLARE
    r record;
    word text := '(?<![A-Za-z0-9_])' || o || '(?![A-Za-z0-9_])';
BEGIN
    FOR r IN
        SELECT c.table_name, c.column_name
          FROM information_schema.columns c
          JOIN information_schema.tables t
            ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'
         WHERE c.table_schema = 'public' AND c.data_type IN ('text', 'character varying', 'jsonb')
         ORDER BY 1, 2
    LOOP
        EXECUTE format('SELECT count(*) FROM %I WHERE %I::text ~ $1', r.table_name, r.column_name) INTO cnt USING word;
        IF cnt > 0 THEN
            item := r.table_name || '.' || r.column_name; RETURN NEXT;
        END IF;
    END LOOP;
END
$fn$;

SELECT 'rename|' || item || '|' || cnt FROM pg_temp.woow_rename_module(:'old', :'new');
\if :scan
SELECT 'rscan|' || item || '|' || cnt FROM pg_temp.woow_rename_scan(:'old');
\endif

\if :apply
COMMIT;
SELECT 'rename-ok|commit';
\else
ROLLBACK;
SELECT 'rename-ok|dry-run';
\endif
