---
title: Skill Dual-Scope（system / user）+ 上傳 Pipeline 設計
category: plans
status: draft
created: 2026-05-30
updated: 2026-05-30
topic: skill-dual-scope
phase: design
related:
  - src/models/skill.py
  - src/models/mcp_server.py
  - src/controllers/agent_runtime_skill.py
  - src/controllers/ai_assistant.py
  - docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md
  - docs/decisions/patterns/invariant-recompute-on-orm-write-hook.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/guides/paas-skill-system/03-skill-design.md
  - docs/guides/paas-skill-system/10-roadmap-phases.md
---

# Skill Dual-Scope（system / user）+ 上傳 Pipeline 設計

## 背景

`woow_paas_platform.skill`（`src/models/skill.py`）目前是**嚴格 project-scoped**：

- `project_id` 為 `required=True`（`skill.py:33-38`），每筆 skill 必綁一個 project。
- **沒有 `scope` 欄位**，沒有「平台級共用 skill」的概念。
- 沒有任何上傳途徑：整個 repo **沒有**接收 tar.gz、解 `SKILL.md` frontmatter、算 sha256 的程式碼。skill record 目前只能透過 ORM（測試 fixture / migration）或外部 XML-RPC 建立。`agent_runtime_skill.py` 只有 GET download。
- 後台 Skills view（`src/views/skill_views.xml`）刻意 `create="false"`（見 `docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md`），只做唯讀檢視 + lifecycle transition。

對照組 `woow_paas_platform.mcp_server` 已是 **dual-scope**（`mcp_server.py:49-57`）：`scope` Selection（system/user）、`project_id` nullable、D4 `@api.constrains` 雙向（`mcp_server.py:118-132`）。system MCP 綁在 `ai.assistant` 層（`ai_assistant.py:10` `domain=[('scope','=','system')]`）→ **永遠載入、不靠 template 綁**；user MCP 經 channel/project 載入並與 system 聯集（`ai_assistant.py:455-460`）。

本設計讓 skill 跟進 dual-scope，並補上上傳 pipeline。**目標語意**（採用 user 的定義）：

- **system skill** — 平台級共用，**所有 project 的 pod 都載入**（對齊 system MCP「永遠可用」）。
- **user skill** — 綁 project，只有該 project 的 pod 載入。

## 跨 repo 去風險結論（關鍵）

對 `paas-agent-runtime`（frozen runtime contract）的唯讀探查確認：**支援 system skill 是純 Odoo 端改動，runtime 零改動、非 breaking。**

- pod 對 skill 的契約只有：讀一份 `skills.json` snapshot（`{name,version,sha256}` 清單，`runtime/langgraph_runtime.py:208`）+ lazy fetch `skill-bundle?project_hash&skill_name&version`（`runtime/skills/cache.py:87-115`，回裸 gzip、fetch 後驗 sha256）。
- pod 對 scope **零認知**，無 scope 分流，`SKILL_BUNDLE_TOKEN` 是不透明 passthrough（pod 不自驗 token_use，F-PC-7 在 skill_bundle 方向由 Odoo 守）。
- snapshot 結構 additive-tolerant：多塞欄位被忽略、缺欄 entry 被 skip。

→ 因此 system skill 靠兩個純 Odoo 端改動即可：**(A)** `_build_skill_snapshot` union system skill；**(B)** 下載端點加 `scope='system'` fallback leg。

> **團隊協定**：本 epic 所有改動落在 `woow-paas-platform`。若後續需動 `paas-agent-runtime` / `woow-paas-charts` / operator，須透過各 repo 的 leader agent 協調，不直接存取其工作目錄。

## 已核准的決策

| 決策 | 結果 | 來源 |
|------|------|------|
| 方向 | skill 跟進 dual-scope（system/user） | user |
| 切分 | 分兩期：Phase 1 地基、Phase 2 上傳 | user |
| `scope` 預設 | `'user'`（現有 100% 為 project-scoped，保既有 fixture 綠） | research 一致收斂 |
| scope 不可偽造 | `scope`/`project_id` 對非 admin 由 `write()` guard 擋 flip（非 ACL） | `constraint-input-trust-expansion` |
| UNIQUE 修法 | 兩條 partial unique index（`init()` 建 + migration drop 舊約束） | research（唯一健全解） |
| is_latest（system） | 全平台每個 name 一個 latest | 設計推導 |
| system bundle 下載 | **pods-only**，租戶不可經 `/web/content` 下載（維持 attachment deny-all） | 安全預設 |
| HC-2 放寬 | 接受（dual-scope 的必然）；bound leg 仍嚴格；guide 補跨 repo 註記 | 安全預設 |
| system skill 建立/發布 | **僅 `base.group_system`** | 安全預設 |
| tar.gz 解析 | **絕不 `extractall`** + 解壓前 size cap + `SKILL.md` 讀取 byte cap | 安全預設（Phase 2） |

---

## Phase 1：dual-scope skill 地基（不含上傳 UI）

> 目標：system skill 端到端可跑（先以 demo/ORM 種子建立），把 schema / ACL / 不變式這些**安全關鍵**在乾淨 PR 裡做對。Phase 1 完成後，system skill 已能被所有 project 的 pod 發現＋下載＋執行。

### ① Model（`src/models/skill.py`）

**新增欄位：**

- `scope = fields.Selection([('system','System'),('user','User')], required=True, default='user', index=True)` — 鏡像 `mcp_server.scope`，但 default 為 `'user'`（非 MCP 的 `'system'`）。
- `project_id`：移除 `required=True`，改為 nullable（保留 `ondelete='cascade'`、`index=True`）。

**D4 不變式（新增 `@api.constrains('scope','project_id')`）** — 逐字鏡像 `mcp_server.py:118-132`，**雙向**：

- `scope='user'` 而 `project_id` 為空 → `ValidationError`
- `scope='system'` 而 `project_id` 非空 → `ValidationError`

這條讓 scope 與 project_id 綁死，是下方 partial index 健全性的前提（避免 system skill 帶 stray project_id 落入兩個 index 的 WHERE 縫隙）。

**UNIQUE 修法（取代 `_sql_constraints`）：**

`UNIQUE(project_id, name, version)`（`skill.py:92-98`）在 `project_id IS NULL` 時失效（PostgreSQL NULL 互異）。移除該 `_sql_constraints` tuple，改在 `init(self)` override 建兩條 partial unique index（`CREATE UNIQUE INDEX IF NOT EXISTS`，fresh install 也生效）：

```sql
-- user leg
CREATE UNIQUE INDEX skill_uniq_user_proj_name_version
  ON woow_paas_platform_skill (project_id, name, version)
  WHERE project_id IS NOT NULL;
-- system leg
CREATE UNIQUE INDEX skill_uniq_system_name_version
  ON woow_paas_platform_skill (name, version)
  WHERE project_id IS NULL;
```

舊約束 `woow_paas_platform_skill_unique_name_version_per_project` 由 migration（§⑤）drop。

**`_recompute_is_latest` 重構為 scope-aware：**

現簽名 `(project_id, name)`（`skill.py:202`）positional，無法表達 system 的 NULL grouping。改為從 record 自推 group domain（新增 helper `_is_latest_group_domain(self)`，`ensure_one`）：

- `scope='system'` → `[('scope','=','system'),('project_id','=',False),('name','=',name)]`（全平台同 name 一桶）
- `scope='user'` → `[('scope','=','user'),('project_id','=',project_id),('name','=',name)]`

所有 call site（create branch `skill.py:114`、write branch `:129`、三個 transition `:172/:184/:200`）改傳 record／呼叫新 helper。`_anchor_bundle_attachment`（`skill.py:132-153`）不變——system bundle 同樣需要 anchor（HC-4 side-channel 對 system bundle 更適用，因其廣泛可讀 metadata）。

**`_order`（`skill.py:10`）**：`project_id, name, version desc` 改為 `scope, project_id, name, version desc`（避免混 scope 排序錯亂；NULL project 排序仍合法）。

### ② ACL（`src/security/ir_rules.xml` + `ir.model.access.csv`）

> skill **無 FK 子 model**（只有 `agent_template` 的 M2M `agent_template_skill_rel`），所以不是 MCP 那種 server+tool 各 3 條的字面 6-rule；對應的「子資源」是 bundle `ir.attachment`（已有 HC-4 兩層防護）。實際形狀是 **3 條 user-scope rule（補 scope leg）+ 1 條 system_readonly + 1 條 admin_full + 既有 attachment pair**。

- **新增 `skill_rule_system_readonly`**：domain `[('scope','=','system')]`，`base.group_user`，`perm_read=1, perm_create=0, perm_write=0, perm_unlink=0`。→ 所有內部 user 可讀 system skill metadata，但**不能偽造／改／刪** system skill。
- **既有 3 條 project rule**（`skill_rule_project_admin/user/guest`，`ir_rules.xml:381-412`，走 `project_id.project_access_ids` walk）：各加 `('scope','=','user')` AND-leg（不誤吃 NULL-project〔system〕row）。
- **scope-flip 防護靠 `skill.write()` guard，不是 ir.rule**（實測修正 commit `8bbe1c2`）：Odoo 寫入權限檢查的是**寫入前**狀態（user skill + owner → 通過），改完才變 system，ir.rule 擋不住 flip。故 `write()` 內：非 `env.su`、非 `base.group_system` 變更 `scope`/`project_id` → `AccessError`。create 走 `scope='user'` 不受影響；admin / sudo（upload pipeline、demo seed）放行。
- **`skill_rule_admin_full`**（`base.group_system`，full bypass）保留——對齊 `cross-workspace-task-data-leak` B1 root cause「加 group_user rule 必對等加 group_system admin_full」。
- **bundle `ir.attachment` rule**（`ir_attachment_rule_skill_bundle_user_scope`，`ir_rules.xml:491` deny-all group_user on `res_model='woow_paas_platform.skill'` attachment）**不動**：system bundle 同樣 pods-only，租戶可讀 metadata、不可下載 binary。
- **`ir.model.access.csv`**：**不需改動**（已驗證）。既有 `access_skill_user`（`base.group_user`，`1,1,1,1`）+ `access_skill_admin`（`base.group_system`，`1,1,1,1`），與 `mcp_server` 同 pattern：model-level 開 RWCU，**實際 scope gating 全在 record-rule（ir.rule）層**。其中 model-level `create=1` 是 Phase 2「租戶自助建立 user skill」所需；**system skill 的建立在 record-rule 層被擋**（base.group_user 的 create-applicable rule 只有 user-scope leg `scope='user'`，建立 `scope='system'` row 無任何 create-permitting rule 命中 → 拒），無需另設 model-level 限制。

### ③ Pod 載入（純 Odoo，三條 leg 必須一起上）

少任一條，system skill 不是「死」（snapshot 沒有 → LLM 看不到）就是「404」（snapshot 有但 download project_id 鎖死）。

- **`_build_skill_snapshot`（`ai_assistant.py:785`）**：回傳 `published(template.skill_ids)` ∪ `published(scope='system')`。
  - 去重：依 `(name, version)`；**同 `name` 衝突時 user(project) skill 蓋過 system skill**（project 優先，每個 name 在 snapshot 只出現一次，避免 pod `_TOOL_CACHE` key `(name,version,role)` 撞與 LLM 看到重複 tool 名）。
- **下載端點（`agent_runtime_skill.py:113`）**：search domain 改 dual-leg
  ```python
  ['&', ('name','=',skill_name), ('version','=',version),
   '|', ('project_id','=',project_id), ('scope','=','system')]
  ```
  token 的 `project_id` 對 bound leg 仍 authoritative（HC-2 維持）；system leg 是刻意放寬（platform-curated、設計為跨 project 可讀）。若同 (name,version) 同時命中 project row 與 system row，**取 project row**（與 snapshot 去重一致）。端點 header docstring（`agent_runtime_skill.py:14-16`）更新說明此 OR-leg。
- **跨 repo 註記**：在 `docs/guides/paas-skill-system/` 補一條：skills.json 現可能含 bundle 解析為 system-scope（project_id NULL）的 entry；契約 wire format 不變。（必要時經 `paas-agent-runtime` leader 知會。）

### ④ Template 子集約束（`src/models/agent_template.py` + `src/models/project_project.py`）

- **`project.project` 補 `skill_ids` 反向 O2M**（鏡像 `project_project.py:50` 的 `mcp_server_ids`），讓子集約束與 view 乾淨。
- **新增 `_check_skill_subset`**（`@api.constrains('skill_ids')`，鏡像 `agent_template.py:124-145` 的 `_check_mcp_subset`）：綁定的 **user-scope** skill ⊆ template 各 project 的 skill；**system-scope** skill 一律允許（平台級）。防跨 project 越權綁定。
- system skill **自動載入**（不需在 `skill_ids` 顯式綁）；`skill_ids` 用於 user skill 綁定。

### ⑤ Migration（manifest `18.0.1.0.28` → `18.0.1.0.29`）

新增 `src/migrations/18.0.1.0.29/`：

- **`pre-migrate.py`**：`ALTER TABLE ... ADD COLUMN scope`（若不存在）+ `UPDATE ... SET scope='user' WHERE scope IS NULL`。**必須在 pre**（ORM 對 required 欄位做 NOT NULL 註冊前），否則既有 row scope 為 NULL → upgrade abort。
- **`post-migrate.py`**：
  1. `DROP` 舊 UNIQUE 約束 `woow_paas_platform_skill_unique_name_version_per_project`。
  2. `CREATE UNIQUE INDEX IF NOT EXISTS` 兩條 partial index（與 `init()` 同義，冪等）。
  3. **two-DELETE 退役舊 rule xmlid**（沿用 `src/migrations/18.0.1.0.24/post-migrate.py:148-184` precedent）：`DELETE FROM ir_rule WHERE id IN (SELECT res_id FROM ir_model_data WHERE module=... AND name IN ('skill_rule_project_admin/user/guest'))` + `DELETE FROM ir_model_data ...`。因為這 3 條 rule 的 domain 變了（加 scope leg），noupdate XML 不會自動覆蓋 → 退役後由新 XML 重建，避免舊 domain 與新 domain OR-combine（MAJOR-1 重炸）。
  4. is_latest **不需重算**：既有 row 全 backfill 為 `scope='user'`，其 grouping `(project_id, name)` 與舊邏輯相同 → 既有 is_latest 值不變。
- **`init(self)` override**：建兩條 partial index（fresh install 不跑 migration，需這條）。

### ⑥ Tests

**修既有 forward-coupled 測試（會紅）：**

- `test_agent_pod_phase_a_new_models.py`（N.7/N.8 schema lock）— scope 欄位 + project_id nullable
- `test_agent_pod_phase_a_acl_new_models.py`（B1.SK-1..8；`:615` 斷言一個**不存在**的 `group_skill_admin`，順帶修正訊息為實際的 field-level `groups='base.group_system'` mask）
- `test_agent_pod_phase_a_acl.py:434`
- `test_agent_pod_phase_c_sse_gateway.py`（SSE.6 `len(snapshot)==1`：若 union system skill 改變 count，需更新；或測試資料無 system skill 則不變——實作時確認）

**新增測試：**

- D4 constraint 雙向（user 缺 project / system 帶 project 各 raise）
- partial unique index：system 同 (name,version) 第二筆拒；user 同 (project,name,version) 第二筆拒；**跨 scope 同 name 允許**
- is_latest system grouping（全平台同 name 一個 latest；多 project 不互相干擾 system bucket）
- `skill_rule_system_readonly` ACL 矩陣：group_user 可讀 system、不能 create/write/unlink system；cross-user user-scope deny；group_user scope-flip（user→system）被擋
- download OR-leg：pod token 可抓自己 project 的 user skill + 任意 system skill，**不能**抓別 project 的 user skill
- `_build_skill_snapshot` union + 同 name 衝突 project 優先
- `_check_skill_subset`：跨 project user skill 綁定拒、system skill 綁定允許

### ⑦ Demo 種子資料（讓 stg 後台立刻看到效果）

Phase 1 無上傳 pipeline，system skill 以 **demo 資料**種子建立，讓 stg 後台不再是 `No skills yet`：

- 在模組內 commit 一個**極小 demo skill bundle**（`hello-world` system skill）的 tar.gz（root 含 `SKILL.md` + 最小 `src/`，對齊 `03-skill-design.md` 格式），存於 `src/data/demo_skills/`。
- 新增 XML（`src/data/demo_system_skill.xml`，`<odoo><data noupdate="1">`，註冊在 manifest **`data` 清單**，緊接 `data/agent_template_demo.xml`）：
  1. 建 `ir.attachment`：`datas` = 該 tar.gz 的 **base64**（impl 時產生並 inline）、`mimetype='application/gzip'`。
  2. 建 `woow_paas_platform.skill`：`scope='system'`、`project_id` 留空、`state='published'`、`name='hello-world'`、`version='0.1.0'`、`bundle_sha256` = **預算好的 sha256**（對 tar.gz raw bytes）、`bundle_attachment_id` 指向 ①。
- 由 `skill.create()` hook 自動處理 attachment **anchor**（`res_model/res_id/public=False`）+ is_latest。
- **放 `data` + `noupdate="1"`**（對齊既有 `data/agent_template_demo.xml` 慣例）：可靠地在 stg/dev-sandbox 顯示，不依賴 demo-data 開關；admins 可刪。**Trade-off**：prod 也會種一個 `hello-world` 範例 skill（可刪）。若要 prod 完全乾淨，改成新增 manifest `demo` key 並改放那裡（dev-sandbox 安裝需開 demo data）。

> 此 demo skill 同時是 §⑥ 測試與 §③ pod-loading 端到端驗證的天然 fixture：published system skill 應出現在**任一** project 的 `_build_skill_snapshot`，且可被 download OR-leg 抓取。

---

## Phase 2：上傳 Pipeline（之後另開 spec）

> 在 Phase 1 穩固地基上加 UI/pipeline。下列為 outline，細節決策留 Phase 2 spec。

- **共用解析 helper**（skill model method，backend wizard 與 frontend controller 共用）：
  - 接 tar.gz → **解壓前 size cap**（reuse ai_assistant 10MB 或另定）→ **絕不 `extractall`**（zip-bomb / path-traversal / symlink 防護）→ `extractfile(SKILL.md)` 設 byte cap → parse frontmatter（**極簡手寫 parser**，零依賴；契約只要 `name`/`version`，其餘原樣存 `skill_metadata` JSON；PyYAML 在 Odoo image 是否可用待確認，預設不依賴）→ 算 sha256（**對實際被服務的 raw bytes**，須與 pod `resp.read()` 一致）+ size_kb → 建 `ir.attachment`（**sudo**，因 frontend 走 base.group_user 而 attachment 受 HC-4 rule 管）→ anchor → create skill（draft）。
- **Backend wizard**（`TransientModel` + binary 欄位）：建 **system** skill，gated `base.group_system`；掛在 Skills list header button。後台 list action 加 `domain=[('scope','=','system')]`（鏡像 `mcp_server_views.xml:98`，admin 不看每個租戶的 user skill）。
- **/woow 前台**（multipart endpoint，`/api/.../skills`）：建 **user** skill，gated project role（owner/admin，reuse `_resolve_project_access`）。鏡像 MCP 的前台 section pattern（如 `McpServersSection`）。
- **Phase 2 待決**：frontmatter schema / SKILL.md 位置慣例（top-level only vs single-root-dir）/ version 是否強制 semver / 重複上傳行為（hard error vs auto-bump）/ 上傳後 draft vs auto-publish / 前台 CSRF / max size。

---

## 非目標（Non-goals / YAGNI）

- Phase 1 **不含**任何上傳 UI / pipeline（system skill 以 §⑦ demo 種子建立）。
- 不做 semver 精確排序（is_latest 以 `published_at` 近似，沿用既有 follow-up）。
- 不做 `archived` → unarchive（沿用既有 follow-up）。
- 不動 `paas-agent-runtime` / charts / operator 程式碼（除必要的 guide 註記）。
- 不放寬 bundle binary 對租戶的可下載性（維持 pods-only）。

## 風險登記

| 風險 | 等級 | 緩解 |
|------|------|------|
| `UNIQUE` 在 NULL project 失效 → system skill 重複 | 🔴 blocker | 兩條 partial unique index（§① + §⑤），含 `init()` 涵蓋 fresh install |
| 三條 project rule 對 NULL project 全 False → system skill 對非 admin 隱形 | 🔴 blocker | `skill_rule_system_readonly`（§②） |
| 三條 pod-loading leg 未同時上 → system skill 死/404 | 🟠 high | §③ 三條一起 ship + 對應測試 |
| scope 可被 group_user 偽造（flip project_id→NULL）→ 跨租戶外洩 | 🟠 high | `write()` guard 擋非 admin 改 scope/project_id（ir.rule 擋不住，只檢查 pre-write 狀態）+ D4 constraint |
| 舊 rule xmlid 與新 domain OR-combine（MAJOR-1 重炸） | 🟠 high | post-migrate two-DELETE 退役（§⑤） |
| 4 個既有測試 forward-coupled 假綠/紅 | 🟠 high | §⑥ 明列更新 |
| required scope 欄位 backfill 在 post → upgrade abort | 🟠 high | 在 **pre**-migrate ADD COLUMN + UPDATE（§⑤） |

## 檔案異動清單（Phase 1）

| 檔案 | 動作 |
|------|------|
| `src/models/skill.py` | 修改：`scope` 欄位、`project_id` nullable、D4 constraint、移除 `_sql_constraints`、`init()` partial index、`_recompute_is_latest` 重構、`_order` |
| `src/models/agent_template.py` | 修改：`_check_skill_subset` |
| `src/models/project_project.py` | 修改：`skill_ids` 反向 O2M |
| `src/controllers/ai_assistant.py` | 修改：`_build_skill_snapshot` union system + 去重 |
| `src/controllers/agent_runtime_skill.py` | 修改：download dual-leg + docstring |
| `src/security/ir_rules.xml` | 修改：system_readonly rule + 3 條 project rule 加 scope leg |
| `src/security/ir.model.access.csv` | **不需改動**（既有 `access_skill_user` 1,1,1,1 + 記錄規則層 gating，同 mcp_server） |
| `src/migrations/18.0.1.0.29/pre-migrate.py` | 新增 |
| `src/migrations/18.0.1.0.29/post-migrate.py` | 新增 |
| `src/__manifest__.py` | 修改：version → 18.0.1.0.29、`demo` 清單註冊 demo_system_skill.xml |
| `src/data/demo_system_skill.xml` | 新增：demo system skill（attachment + skill record，`data`+noupdate） |
| `src/data/demo_skills/hello-world-0.1.0.tar.gz` | 新增：demo skill bundle（base64 inline 於 XML，原檔留存供參） |
| `src/tests/test_skill_*.py` 等 | 修改既有 + 新增 dual-scope 測試 |
| `docs/guides/paas-skill-system/*` | 修改：system skill 載入 + 跨 repo 註記 |
| `docs/guides/paas-skill-system/10-roadmap-phases.md` | 修改：記錄 dual-scope 演進 |
