-- fingerprint_business.sql — 「客戶資料列」指紋：升級閘跑 -u all 前後比對用（standalone-odoo-migrate，#1604）
--
-- fingerprint.sql 是整張表逐位元；-u all 會重寫模組自己的資料列（XML data 的 write_date、view、menu…），
-- 整表 hash 一定會變，分不出「模組資料被重寫」與「客戶資料被動到」。本檔只看後者：
--   列：排除模組可更新的資料列（ir_model_data 有登記且 noupdate 不為 true）；noupdate 的列（公司、admin 使用者…）
--       與沒有對應 model 的表（關聯表、非 ORM 表）照算。升級後只算基準當時就存在的列（id <= 基準的 max id），
--       之後新增的列另外計數——Odoo 一啟動 cron 就會寫新資料，那不是升級動到舊資料。
--   欄：排除 write_date／write_uid；升級後只取基準當時就有的欄（升級新增的欄不算）。
--       可翻譯欄位（jsonb，一個語言一個 key）在「模組擁有的列」（ir_model_data 有登記；這裡剩下的都是 noupdate）只比
--       en_US：-u all 會替模組資料補上新版 .po 多出來的翻譯（實測：harumi 升級後 uom、信件範本多了 zh_TW），那不是
--       客戶資料被動到。客戶自己建的列（沒有 ir_model_data）照樣比整個 jsonb。
--   ir_attachment 另外排除 /web/assets/ 的 bundle（-u all 會清掉重產）與模組可更新資料列的欄位附件（例：語言旗幟圖）。
--
-- 用法（全程唯讀；temp table 只活在這個 session）：
--   基準（還原後、Odoo 啟動前）：psql -X -q -At -v ON_ERROR_STOP=1 -d <db> -f fingerprint_business.sql > baseline.bfp
--   升級後：BFP_BASELINE=baseline.bfp psql -X -q -At -v ON_ERROR_STOP=1 -v baseline=1 -d <db> -f fingerprint_business.sql
-- 輸出：bcol|表|欄|序（採用的欄）、bmax|表|基準 max id、bmissing|表|欄（基準有、現在沒有的欄）、
--       btable|表|列數|hash、badded|表|基準之後新增的列數（只在升級後、且大於 0 時輸出）、
--       btransient|表（TransientModel 的表：Odoo 的 autovacuum 會清掉一小時前的列，比對時當成執行期表）。
SET TimeZone = 'UTC';
SET DateStyle = 'ISO, YMD';
SET IntervalStyle = 'postgres';
SET extra_float_digits = 1;
SET bytea_output = 'hex';
SET client_min_messages = warning;

CREATE TEMP TABLE _bcol (tbl name NOT NULL, col name NOT NULL, pos int NOT NULL);
CREATE TEMP TABLE _bmax (tbl name NOT NULL, maxid bigint NOT NULL);
\if :{?baseline}
\copy _bcol FROM PROGRAM 'sed -n "s/^bcol|//p" "$BFP_BASELINE"' WITH (FORMAT csv, DELIMITER '|')
\copy _bmax FROM PROGRAM 'sed -n "s/^bmax|//p" "$BFP_BASELINE"' WITH (FORMAT csv, DELIMITER '|')
\else
INSERT INTO _bcol
SELECT c.relname, a.attname, a.attnum
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE c.relkind = 'r' AND n.nspname = 'public' AND a.attname NOT IN ('write_date', 'write_uid');
\endif

-- 現在還在的欄
CREATE TEMP VIEW _blive AS
SELECT b.tbl, b.col, b.pos, a.atttypid
  FROM _bcol b
  JOIN pg_class c ON c.relname = b.tbl AND c.relkind = 'r'
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = b.col AND a.attnum > 0 AND NOT a.attisdropped;

-- 表 → model：一般是把 . 換成 _；ir.actions.* 的表名是例外
CREATE TEMP VIEW _bmodel AS
SELECT tbl, array_agg(model ORDER BY model) AS models
  FROM (SELECT replace(model, '.', '_')::name AS tbl, model FROM ir_model
        UNION
        SELECT v.tbl::name, v.model FROM (VALUES
          ('ir_act_window', 'ir.actions.act_window'), ('ir_act_window_view', 'ir.actions.act_window.view'),
          ('ir_act_server', 'ir.actions.server'), ('ir_act_report_xml', 'ir.actions.report'),
          ('ir_act_url', 'ir.actions.act_url'), ('ir_act_client', 'ir.actions.client')) AS v(tbl, model)) q
 GROUP BY tbl;

-- 可翻譯欄位（表要有 id 才能對 ir_model_data）
CREATE TEMP VIEW _btrans AS
SELECT DISTINCT m.tbl, f.name::name AS col, m.models
  FROM _bmodel m
  JOIN ir_model_fields f ON f.model = ANY (m.models) AND f.translate
 WHERE EXISTS (SELECT 1 FROM _blive i WHERE i.tbl = m.tbl AND i.col = 'id');

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

\if :{?baseline}
\else
SELECT format('INSERT INTO _bmax SELECT %L, coalesce(max(id), 0) FROM ONLY public.%I', l.tbl, l.tbl)
  FROM _blive l
 WHERE l.col = 'id' AND l.atttypid IN ('int2'::regtype, 'int4'::regtype, 'int8'::regtype)
 ORDER BY l.tbl
\gexec
\endif

SELECT 'bcol|' || tbl || '|' || col || '|' || pos FROM _bcol ORDER BY tbl, pos;
SELECT 'bmax|' || tbl || '|' || maxid FROM _bmax ORDER BY tbl;
SELECT 'btransient|' || replace(model, '.', '_') FROM ir_model WHERE transient ORDER BY 1;
SELECT 'bmissing|' || b.tbl || '|' || b.col
  FROM _bcol b
 WHERE EXISTS (SELECT 1 FROM _blive l WHERE l.tbl = b.tbl)
   AND NOT EXISTS (SELECT 1 FROM _blive l WHERE l.tbl = b.tbl AND l.col = b.col)
 ORDER BY 1;

SELECT format(
  $q$SELECT 'btable|' || %L || '|' || count(*) || '|' || coalesce(md5(string_agg(h, '' ORDER BY h)), '-')
       FROM (SELECT md5(ROW(%s)::text) AS h FROM ONLY public.%I AS t WHERE true%s%s%s) AS s$q$,
  l.tbl,
  string_agg(
    CASE WHEN tr.col IS NOT NULL AND l.atttypid = 'jsonb'::regtype
         THEN format('CASE WHEN EXISTS (SELECT 1 FROM ir_model_data d WHERE d.model = ANY (%L::varchar[])'
                     ' AND d.res_id = t.id) THEN t.%I -> ''en_US'' ELSE t.%I END', tr.models, l.col, l.col)
         ELSE format('t.%I', l.col) END,
    ', ' ORDER BY l.pos),
  l.tbl,
  CASE WHEN x.maxid IS NOT NULL THEN format(' AND t.id <= %s', x.maxid) ELSE '' END,
  CASE WHEN bool_or(l.col = 'id') AND m.models IS NOT NULL
       THEN format(' AND NOT EXISTS (SELECT 1 FROM ir_model_data d WHERE d.model = ANY (%L::varchar[])'
                   ' AND d.res_id = t.id AND d.noupdate IS NOT TRUE)', m.models)
       ELSE '' END,
  CASE WHEN l.tbl = 'ir_attachment'
       THEN $f$ AND (t.url IS NULL OR t.url NOT LIKE '/web/assets/%')
                AND NOT (t.res_field IS NOT NULL AND EXISTS (
                  SELECT 1 FROM ir_model_data d
                   WHERE d.model = t.res_model AND d.res_id = t.res_id AND d.noupdate IS NOT TRUE))$f$
       ELSE '' END)
FROM _blive l
LEFT JOIN _bmodel m ON m.tbl = l.tbl
LEFT JOIN _bmax x ON x.tbl = l.tbl
LEFT JOIN _btrans tr ON tr.tbl = l.tbl AND tr.col = l.col
GROUP BY l.tbl, m.models, x.maxid
ORDER BY l.tbl
\gexec

\if :{?baseline}
SELECT format($q$SELECT 'badded|' || %L || '|' || count(*) FROM ONLY public.%I AS t WHERE t.id > %s HAVING count(*) > 0$q$,
              x.tbl, x.tbl, x.maxid)
  FROM _bmax x
 WHERE EXISTS (SELECT 1 FROM _blive l WHERE l.tbl = x.tbl AND l.col = 'id')
 ORDER BY x.tbl
\gexec
\endif

COMMIT;
