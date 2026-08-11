# Refactor Plan: MCP `cloud_service_id` → `project_id`

**Date**: 2026-05-18
**Status**: signed-off (2026-05-18) → tester
**Components**: `woow_paas_platform.mcp_server`, `woow_paas_platform.mcp_tool`, `project.project`, `cloud_service`, `ir.rule`, AI chat consumer chain, MCP backend & frontend views

## Context

現況：`mcp_server.cloud_service_id` M2O 連到 `woow_paas_platform.cloud_service`。但 AI chat 真實消費鏈是 `channel → task → project → cloud_service → user_mcp_server_ids → tools` — `cloud_service` 在這條鏈上只是中介，**真正擁有 AI chat context 的是 `project`**。語意錯位。

目標：把 user-scope MCP 改為 **`project` 的附屬資源**，AI chat 直接從 `project.mcp_server_ids` 取。同時補上 `mcp_server` / `mcp_tool` 缺失的 `ir.rule`（pre-existing leak — 任何 base.group_user 可讀全 instance 的 system & user MCP）。

兩個耦合面要一起處理：
- `mcp_server` 的**消費**改路徑（project 直連）
- `mcp_server` 的 ACL 從無到有（套 `paas-workspace-member-acl-tightening` 三件套）

物理現實 invariant：user-scope MCP 是 **sidecar 的 wrapper**（`url` 來自 `service.helm_release_name + helm_namespace`）。語意上歸 project，物理上仍綁 service — 兩條 lifecycle 路徑要對齊：
- project 刪 → DB cascade 自動清 MCP（M2O ondelete='cascade'）
- service 刪 → service 端 cleanup hook 主動 unlink user-scope MCP（**在 service unlink 前**），project 不動

對等 sub-model：`mcp_tool` 走 `server_id` 反向，rule domain 必須穿透 `server_id.project_id...`，避免 sub-model leak（B1 教訓）。

## Decisions

D1 / D3 / D4 / D5 / D6 由 leader 拍板（2026-05-18 brainstorm）；D2 inventory 由 leader 接手 kubectl，plan 預設走 defensive backfill。

- **D1**: 純改名（drop `cloud_service_id`、新增 `project_id` M2O to `project.project`, `ondelete='cascade'`），加上 `cloud_service._do_cleanup` hook 主動 unlink user-scope MCP（schema 簡單 + lifecycle 對齊）。
- **D2**: Migration 走 defensive backfill SQL（idempotent；無 row → noop）。Inventory SQL 因 shared infra 授權問題 planner / leader 都被擋，已交由 user 跑；結果不擋 plan 進度（backfill 設計獨立於 inventory 結果）。
- **D3**: `cloud_service_id` 在同一個 migration（`18.0.1.0.24`）**一次 drop** — 不走兩階段 deprecate。本次 deploy target 只有 dev-sandbox + stg，**沒有正式 prod data**（假設待 leader inventory 驗證 → Open Risk #1）。
- **D4**: 保留 `scope` 欄位 + Python constraint `_check_scope_project_consistency`：`scope='user' ⇔ project_id IS NOT NULL`。
- **D5**: User-scope MCP 必須掛 **bound** project（project.cloud_service_id IS NOT NULL）。Python constraint `_check_user_scope_requires_bound_project` 禁止 unbound project 創 user-scope MCP。**與 D1 cleanup hook 對齊**：service 刪 → user-scope MCP 先被 unlink → constraint 不會 violate（row 已不存在）。
- **D6**: 前端 `McpServersTab.xml` **不搬**，繼續掛 service 詳情頁。API 入參改 `project_id`（前端拿 service 後再透過 service.project_ids 反向）。

## Rejected options

| 選項 | 為什麼拒絕 |
|---|---|
| D1=A 純改名無 cleanup hook | service 刪後 user-scope MCP zombie（project 還在、URL 失效）— 真實 bug |
| D1=C 雙 FK（cloud_service_id + project_id 都留） | schema 變胖、cascade 順序衝突、rollback 多 column、雙 source of truth |
| D1=D UI/邏輯層改、不動 schema | 沒滿足 brief 「附屬於 project」的字面語意；query path 變複雜 |
| D3=B 兩階段 drop（deprecate → drop） | rollback 顆粒度是整包退版，不會用個別 column rollback；surgical refactor 不該留 dead column 兩個版本 |
| D5=B Dual-leg rule（unbound project follower / creator） | ACL 補丁、不解 root cause；違反「user-scope MCP 是 sidecar wrapper」物理現實；B1 pitfall 警示不該靠 rule 兜底 |
| D6=B/C 前端搬到 project 頁 / 兩邊都列 | 擴 UX scope；service 頁列 MCP 仍符合「物理綁 sidecar」心智模型；本次只動 schema/ACL，不擴前端 |

## Acceptance criteria

每條都對應一個可被 test 抓住的行為。

### Schema & 資料

1. `mcp_server` 有 `project_id` 欄位（M2O → `project.project`, `ondelete='cascade'`, indexed），無 `cloud_service_id` 欄位。
2. `project.project` 有 `mcp_server_ids` 反向 O2M（inverse `project_id`），無 `cloud_service.user_mcp_server_ids` 反向（一併移除）。
3. Migration `18.0.1.0.24/post-migrate.py` 對既有 `cloud_service_id IS NOT NULL` 的 row 自動 backfill `project_id`（透過 `project.project WHERE cloud_service_id = X LIMIT 1`）。
4. Migration **idempotent**：重跑 `-u woow_paas_platform` 不會 raise、不會雙寫、不會留孤兒。
5. Migration 跑完 `cloud_service_id` column 已 drop（驗證：`information_schema.columns` 查無此欄位）。

### Constraint

6. **D4 invariant**：scope='user' iff project_id IS NOT NULL；scope='system' iff project_id IS NULL — 任一條違反 raise `ValidationError`。
7. **D5 invariant**：scope='user' 的 mcp_server，`project_id.cloud_service_id` 必須 IS NOT NULL — 違反 raise `ValidationError`。
8. `mcp_server.url` 計算保持原邏輯（從 backing service 取 `helm_release_name + helm_namespace`），但取 service 的路徑改成 `self.project_id.cloud_service_id`（不再走 `self.cloud_service_id`）。

### Lifecycle

9. **D1 cleanup hook**：`cloud_service._do_cleanup` 在 `helm uninstall` **之前**主動 unlink 該 service 的所有 user-scope MCP（透過 `self.project_ids.mapped('mcp_server_ids').filtered(lambda s: s.scope == 'user').sudo().unlink()`）。順序：**unlink user-scope MCP → `client.uninstall_release(...)` → `self.write({'state': 'deleting'})`**（理由見 [[mcp-project-decouple]] Invariant #4 的時間窗註解）。Operator 後續回 404 → cron 觸發 `service.unlink()`。
10. Delete service → user-scope MCP 被清空、project 仍存在（project 不跟著刪）。
11. Delete project → DB cascade 自動清該 project 的所有 mcp_server（user + system 都會跟著掉，但 system scope 不會綁 project，所以實際只清 user-scope）。
12. Delete project 不會誤刪別 project 的 MCP、不會誤刪 system-scope MCP。

### AI chat 消費鏈

13. `discuss_channel._get_user_mcp_tools` 走 `task.project_id.mcp_server_ids`（不再經 `cloud_service_id.user_mcp_server_ids`）。
14. `controllers/ai_assistant._get_user_mcp_tools_for_channel` 同上路徑。
15. `ai.assistant.mcp_server_ids` M2M domain 維持 `[('scope', '=', 'system'), ('active', '=', True)]` — 不變。
16. SSE streaming 與 message_post 路徑都能正確拿到 user-scope MCP tools（regression：n8n MCP tool 透過 chat 能被呼叫成功）。

### ACL（mcp_server / mcp_tool 從無 rule 到有 rule — rule split per MAJOR 1 fix）

**Reviewer MAJOR 1 fix**：原 dual-leg single-rule 設計允許 base.group_user 透過 ORM `create({'scope': 'system', ...})` 創建 system-scope MCP（domain leg 1 `('scope', '=', 'system')` 對 new row 永真 → rule allow create → row created → 全 instance 可讀）。攻擊 vector：phishing URL、tool call interception、mcp_tool 偽造定義。Rule 必須拆成兩條，分別 enforce read 與 create direction。

17. `mcp_server_rule_system_readonly` 套 `base.group_user`：
    - domain: `[('scope', '=', 'system')]`
    - `perm_read=True, perm_write=False, perm_create=False, perm_unlink=False`
    - 效果：所有 base.group_user 可讀 system-scope MCP，但**不可** create/write/unlink。
18. `mcp_server_rule_workspace_user` 套 `base.group_user`：
    - domain: `[('scope', '=', 'user'), ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]`
    - `perm_read=True, perm_write=True, perm_create=True, perm_unlink=True`
    - 效果：workspace 成員對自家 user-scope MCP 全 CRUD；不可在此 rule 下 create scope='system'（domain match 失敗）。
19. `mcp_tool_rule_system_readonly` 對等 sub-model rule（B1 對等性原則）：
    - domain: `[('server_id.scope', '=', 'system')]`
    - `perm_read=True, perm_write=False, perm_create=False, perm_unlink=False`
    - 防：user 對既有 system MCP 偽造 tool 定義。
20. `mcp_tool_rule_workspace_user` 對等 sub-model rule：
    - domain: `[('server_id.scope', '=', 'user'), ('server_id.project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]`
    - `perm_read=True, perm_write=True, perm_create=True, perm_unlink=True`
21. **MAJOR 1 invariant**：base.group_user 透過 ORM `create({'scope': 'system', ...})` 必須 raise AccessError；只有 sudo() context（auto-create user-scope 用，跟 system create 無關）、`access_mcp_server_admin` (group_system) row、demo data XML 可以建 system-scope MCP。
22. Cross-workspace user 不能 search / browse.read 別 workspace 的 user-scope mcp_server / mcp_tool（不論 ORM 路徑或 controller 路徑）。
23. Own-workspace user **可以** read / write / create / unlink 自己 workspace 內的 user-scope mcp_server（防 over-correction）。
24. `ir.model.access.csv` row：`mcp_server` / `mcp_tool` 給 `base.group_user` 的 RWCU 維持 `1,1,1,1`（**CSV 為 hard cap，rule 細控 row-level**）。Admin row（`group_system`）不變。

**為何 CSV 維持 1,1,1,1 而非降 perm_create=0**：CSV access right 是 model-level binary cap，無法表達「user 可建 scope=user / 不可建 scope=system」這種 row-level 區分。若 CSV 改 perm_create=0 會把所有 create 都堵掉（含合法的 user-scope create path），等於把語意決定塞錯層。Rule 層 enforce 才是 Odoo idiom（見 `workspace_rule_member_access` + `workspace_rule_create` 既有 split pattern）。<br>⚠ #658 後記（此段保留當時原文）：那兩個 xmlid 已由 `src/migrations/18.0.1.0.127/post-migrate.py` 退役，現名 `workspace_rule_member_read` / `workspace_rule_anyone_create`；且當時稱它為 split pattern 並不準確——`workspace_rule_member_access` 的 `perm_read` 與 `perm_write` 都是 True。

### Backend / Frontend view

22. `mcp_server_views.xml` 的 form view：`cloud_service_id` 欄位替換為 `project_id`（invisible="scope != 'user'"），其餘 layout 不變。
23. `mcp_server` action（後台 menu）domain 維持 `[('scope', '=', 'system')]` — 不變。
24. 前端 `McpServersTab.xml` 留在 service 頁，內部 query 改成 `service.project_ids[0].mcp_server_ids`（無 project 時 graceful empty list）。
25. `/api/cloud-services/...` 跟 MCP 相關的 endpoint（如果有）入參或回傳改用 `project_id` 為 PK；list 仍透過 service → project 反向。

### 不做的事（明文 OUT OF SCOPE）

- 前端不新增 project 頁的 MCP 入口（D6=A）
- system-scope MCP 不改 ACL 模型（仍對所有 base.group_user 可讀 — 透過 ai.assistant.mcp_server_ids consume）
- `mcp_tool` 不加 `project_id` 欄位（透過 `server_id` 取，避免冗餘）
- `cloud_app_templates.xml` data 不動（沒寫死 mcp_server row）
- mcp_server.url 算法不改（仍從 backing service 取 namespace）

## Affected files

### Models（5 個）
- `src/models/mcp_server.py` — 欄位改名（`cloud_service_id` → `project_id`），加 `@api.constrains` D4 & D5、改 `_model_build_mcp_endpoint_url` 路徑（從 `self.project_id.cloud_service_id` 取）
- `src/models/mcp_tool.py` — 不動 schema（透過 server_id 走）；只是被新 rule 影響
- `src/models/project_project.py` — 加 `mcp_server_ids = fields.One2many('woow_paas_platform.mcp_server', 'project_id', ...)`
- `src/models/cloud_service.py` — 移除 `user_mcp_server_ids` 反向 O2M；`_model_auto_create_mcp_server` 改成填 `project_id`（透過 `self.project_ids[:1]`，沒 project 時 skip 或建 project）；`_do_cleanup` 加 cleanup hook（unlink user-scope MCP 在 helm uninstall 前）
- `src/models/discuss_channel.py` — `_get_user_mcp_tools` 路徑改 `task.project_id.mcp_server_ids`

### Controllers（1 個）
- `src/controllers/ai_assistant.py` — `_get_user_mcp_tools_for_channel` 路徑同上；如有 MCP CRUD endpoint 同步改

### Security（2 個）
- `src/security/ir.model.access.csv` — `mcp_server` / `mcp_tool` user row RWCU 從 `1,0,0,0` → `1,1,1,1`（admin row 不變）
- `src/security/ir_rules.xml` — **MAJOR 1 fix**：把既有 `mcp_server_rule_workspace_member` + `mcp_tool_rule_workspace_member` 兩條 dual-leg rule **拆成 4 條**（`noupdate="0"` data 區塊）：
  - `mcp_server_rule_system_readonly`（domain `[('scope', '=', 'system')]`, perm_read only）
  - `mcp_server_rule_workspace_user`（domain `[('scope', '=', 'user'), (workspace chain)]`, 全 perm）
  - `mcp_tool_rule_system_readonly`（domain `[('server_id.scope', '=', 'system')]`, perm_read only）
  - `mcp_tool_rule_workspace_user`（domain `[('server_id.scope', '=', 'user'), (workspace chain)]`, 全 perm）

  既有兩條 rule 的 xmlid 一併 retire（migration 18.0.1.0.24 加 SQL `DELETE FROM ir_rule WHERE id IN (SELECT res_id FROM ir_model_data WHERE module='woow_paas_platform' AND name IN ('mcp_server_rule_workspace_member', 'mcp_tool_rule_workspace_member'))` 防 noupdate=0 模組更新時殘留）。

### Views（1 個）
- `src/views/mcp_server_views.xml` — form view 欄位改名

### Frontend（1 個 + 可能 service 頁 props/API 微調）
- `src/static/src/paas/pages/service/tabs/McpServersTab.xml` — 內部 query 改 `service.project_ids[0].mcp_server_ids`
- 對應 service_service.js / API helper（如果有 MCP CRUD client side）：傳入參數 key 改 `project_id`

### Migration（1 個新檔）
- `src/migrations/18.0.1.0.24/post-migrate.py` — 五件事：
  1. Backfill：`UPDATE woow_paas_platform_mcp_server SET project_id = (SELECT id FROM project_project WHERE cloud_service_id = woow_paas_platform_mcp_server.cloud_service_id LIMIT 1) WHERE cloud_service_id IS NOT NULL AND scope = 'user'`
  2. 對 `scope='user' AND project_id IS NULL`（backfill 失敗 — service 沒對應 project）的 row：**archive (active=False) + log warning**（**不 unlink** — migration 是不可逆動作，archive 留 rollback 救命繩；後續可另寫清理 job 把長期 archived 的 row 刪除）
  3. Drop `cloud_service_id` column
  4. `ir_model_data.noupdate=false` 對新 ir.rule xmlid（同 18.0.1.0.23 的 noupdate flip pattern）
  5. **MAJOR 1 fix**：DELETE 舊兩條 dual-leg rule（`mcp_server_rule_workspace_member` / `mcp_tool_rule_workspace_member`）的 row + 對應 ir_model_data — 防 module update 殘留與新 4 條 rule OR-combine 重新打開漏洞

### Manifest
- `src/__manifest__.py` — version bump `18.0.1.0.23` → `18.0.1.0.24`

## Test scope 建議（給 tester）

### 必加 regression（unit + integration）

依賴環境：`docker-desktop` k8s context（與既有 integration test setup 相同），不需要實際 helm install — 只測 ORM / ACL 邏輯。

**A. Migration**（`test_mcp_project_decouple_migration.py` 新檔）

1. **migrate_backfill_user_scope_mcp**: 先用 sudo 建一個 cloud_service + 對應 project + 用舊 schema 模擬 user-scope mcp_server（直接 SQL INSERT，bypassing ORM constraint）；跑 migration；assert mcp_server.project_id 正確指向 project；cloud_service_id column 已 drop。
2. **migrate_idempotent**: 連跑兩次 migration；第二次無 error、無重複寫入。
3. **migrate_orphan_user_scope_archive**: 模擬 cloud_service 沒對應 project 的 user-scope MCP；migration 後該 row 已 archive (active=False)、log warning；row 本身**仍存在**（不 unlink — 留 rollback 救命繩）。
4. **migrate_system_scope_untouched**: system-scope MCP（cloud_service_id IS NULL）migration 前後 project_id 都是 NULL，row 未受影響。

**B. Constraint**（補入既有 `test_mcp_integration.py`）

5. **constraint_user_scope_requires_project**: scope='user' + project_id IS NULL → raise ValidationError（D4）
6. **constraint_system_scope_excludes_project**: scope='system' + project_id IS NOT NULL → raise ValidationError（D4）
7. **constraint_user_scope_requires_bound_project**: scope='user' + project_id 指向 unbound project (cloud_service_id IS NULL) → raise ValidationError（D5）

**C. Lifecycle**（`test_mcp_project_lifecycle.py` 新檔）

8. **delete_service_unlinks_user_mcp_keeps_project**: 建 service + project + user MCP；service.unlink()；assert user MCP 已 unlink（被 cleanup hook 清）、project 仍 exists。
9. **delete_service_does_not_touch_system_mcp**: 同上 setup + 額外建 system MCP；service.unlink() 後 system MCP 仍 exists。
10. **delete_project_cascades_user_mcp**: 建 project + user MCP；project.unlink()；assert mcp_server 已 cascade unlink。
11. **delete_project_does_not_cross_contaminate**: 建兩個 project 各帶 user MCP；刪 project A；assert project B 的 MCP 仍 exists。

**D. AI chat consumption**（`test_mcp_chat_consumption.py` 新檔或 extend `test_chatter_service_injection.py`）

12. **channel_gets_user_mcp_via_project**: 建 channel → task → project（bound）→ user MCP；call `channel._get_user_mcp_tools()`；assert 拿到該 MCP 的 tools。
13. **channel_no_mcp_for_unbound_project**: project unbound（無 cloud_service_id）→ `_get_user_mcp_tools()` 回 empty recordset（D5 constraint 同時保證這個狀態不會 exist）。
14. **assistant_system_mcp_unchanged**: ai.assistant.mcp_server_ids M2M 仍可拿 system MCP，行為與 refactor 前一致。

**E. ACL（cross-workspace isolation + MAJOR 1 create-direction）**（`test_mcp_acl_isolation.py` 新檔，套 `test_cloud_service_acl.py` 樣板）

15. **alice_cannot_read_bob_user_mcp_via_orm**: Alice (W1) 與 Bob (W2) 是不同 workspace 的 internal user；Bob 的 project 有 user MCP；Alice with_user 走 `search([('id', '=', bob_mcp.id)])` → 空 recordset；走 `browse(bob_mcp.id).read()` → raise AccessError。
16. **alice_cannot_read_bob_user_mcp_tool**: 同上對 mcp_tool sub-model（B1-style 對等檢查）。
17. **bob_can_read_own_workspace_mcp**: Bob with_user 拿自家 MCP search/read 正常（防 over-correction）。
18. **all_users_can_read_system_mcp**: scope='system' 對 Alice 與 Bob 都可讀（system MCP 是公共資源）。
19. **MAJOR 1: user_cannot_create_system_mcp_via_orm**: Bob with_user `env['woow_paas_platform.mcp_server'].create({'scope': 'system', 'name': 'evil', 'url': '...', 'transport': 'sse'})` → raise AccessError（rule split create direction enforcement）。
20. **MAJOR 1: user_cannot_create_mcp_tool_for_system_server**: Bob with_user 對既有 system MCP `env['woow_paas_platform.mcp_tool'].create({'server_id': system_mcp.id, ...})` → raise AccessError（sub-model 對等 create deny）。
21. **MAJOR 1: user_cannot_write_or_unlink_system_mcp**: Bob with_user `system_mcp.with_user(bob).write({...})` 或 `.unlink()` → raise AccessError（rule split write/unlink direction）。
22. **MAJOR 1: sudo_can_create_system_mcp**: sudo() context 仍可 create scope='system' MCP（為 demo data / admin 維護路徑保留 — 不被 rule split 卡到）。
23. **MAJOR 1: admin_can_create_system_mcp_via_backend**: group_system user 走 admin CSV row（access_mcp_server_admin）可 create system MCP — 後台維護路徑通暢。
24. **MAJOR 1: user_can_still_create_user_scope_in_own_workspace**: Bob with_user 在自家 workspace 創 scope='user' MCP（透過 sudo bypass D5 constraint 模擬 service auto-create 後的 update 場景）→ 成功（防止 rule split over-correction 把合法 user-scope create 一起堵）。

**F. View regression**（既有 `test_ux_routing_views.py` extend）

19. **mcp_server_form_view_loads**: form view 載入無 error（欄位改名後）。
20. **service_page_mcp_tab_renders**: McpServersTab 載入時 project_ids 為空也不 crash。

### 刻意不測（避免假陽性）

- 不測 mcp_server.url 算法（沒改）
- 不測 ai.assistant 後台 view（沒動）
- 不測 system-scope MCP 的 cross-user 可見性（保持 pre-existing 行為）
- 不測前端 OWL 元件渲染（scope 過大，本次只動 schema/ACL）

### 給 tester 的 test data fixture 建議

- 復用既有 `test_mcp_integration.py` 的 `mcp_enabled=True` template fixture
- ACL fixtures 套 `test_cross_workspace_task_isolation.py` 的 Alice/Bob 雙 workspace 樣板
- Migration test 用 `cr.execute()` 直接 SQL INSERT 模擬舊 schema（bypass new constraint）

## Migration path

### Forward（18.0.1.0.23 → 18.0.1.0.24）

1. `__manifest__.py` version bump `18.0.1.0.24`
2. `-u woow_paas_platform` 觸發 `src/migrations/18.0.1.0.24/post-migrate.py`
3. Migration 順序（**重要**：必須在 ORM update 之前跑 SQL，否則新 ir.rule 套上後 backfill 看不到該看的 row）
   - **pre-migrate**（可選）：sanity check `cloud_service_id` column 還在；若不在，跳過所有 backfill
   - **post-migrate**：
     a. Backfill SQL（user-scope only；system-scope 不動）
     b. Orphan cleanup SQL（user-scope 但無對應 project 的 row → **archive (active=False)** + log warning，**不 unlink**）
     c. Drop column SQL：`ALTER TABLE woow_paas_platform_mcp_server DROP COLUMN cloud_service_id`
     d. `ir_model_data.noupdate=false` for new rule xmlids
4. ORM load 後：新 constraint 自動套（既有 row 應全部已合法因為 b 步驟清掉 orphan）
5. View load 後：mcp_server_views.xml 用新欄位 — 跑得起來

### Rollback

**單一動作**：整包 revert addon 到 18.0.1.0.23（git checkout 上版 tag + `-u`）。**不支援個別 column rollback**（D3=A 假設）。

Rollback 限制（明文 surface 給 user）：
- 18.0.1.0.24 跑過後，backfill 之前的舊 `cloud_service_id` 值已不可恢復（column drop）
- 任何在 18.0.1.0.24 之後新建的 mcp_server row 有 `project_id` 但無 `cloud_service_id`；rollback 後這些 row 變 NULL `cloud_service_id` → 舊邏輯把這些 row 視為 system-scope（scope 欄位實際是 'user'）→ behavioural drift
- 結論：**rollback 前要先停寫**，或接受 rollback 後的 user-scope MCP 變 zombie

## Open Risks

### Risk #1：D3=A 一次 drop 假設未由實環境 inventory 驗證

**Status**: kubectl exec 進 dev-sandbox / stg postgres 被 auto-mode 擋（shared infra 未明確授權）— planner 與 leader **都被擋**。Inventory SQL 已丟給 user 自己跑：

```sql
SELECT COUNT(*) FROM woow_paas_platform_mcp_server WHERE cloud_service_id IS NOT NULL;
SELECT COUNT(*) FROM woow_paas_platform_mcp_server WHERE scope = 'user';
```

**保守假設**：dev-sandbox / stg **可能有** user-scope mcp_server row（n8n template `is_active=True` + `mcp_enabled=True`，任何部署過 n8n service 的 sandbox 都會 auto-create）。

**為什麼 drop 仍安全**：plan 採 **defensive backfill** 路徑 — backfill SQL idempotent + 無 row 自動 noop + 有 orphan cleanup branch。三種 inventory 結果都涵蓋：

- **若有 user-scope row**：backfill SQL 自動處理（已 cover），cleanup hook 處理對不到 project 的 row。
- **若無 row**：backfill SQL 0 update，drop column 直接生效。
- **若有 row 但對應 project 不存在**：被 orphan cleanup archive (active=False)、log warning。Row 仍存在於 DB（不 unlink），是 dangling state 但保留 rollback 救命繩；AI chat 因 active=False filter 拿不到。

**Caveat lifecycle**:
- 現狀（2026-05-18，inventory 未驗證）：caveat = 「保守假設可能有資料，但 backfill defensive、drop 安全」
- User 跑 inventory 結果回來後 → 若 ≥1 row：caveat 升級為「已驗證有資料，backfill 已涵蓋」；若 0 row：caveat 降級為「理論風險已排除」
- Reporter phase finalize 時依實況更新此 section

**Caveat 給 user**：production 升 18.0.1.0.24 前必須**確認**無 user-scope mcp_server 在 production 流量上活躍 — 雖然這個 module 還沒上 production，但若未來有，必須在 deploy 流程加 `SELECT COUNT(*) FROM woow_paas_platform_mcp_server WHERE scope='user'` sanity check。

### Risk #2：cascade vs cleanup hook 順序錯誤可能讓 D1 hook bypass

**Status**: AC #9 與 #10 明定順序；test #8/#9 鎖住行為。

**底層原理**：
- `mcp_server.project_id` 設 `ondelete='cascade'`
- `cloud_service.project_ids` 是 O2M 反向（`project.project.cloud_service_id` 的 inverse），ondelete='set null'（[[cloud-service-project-decouple]] Invariant #2）
- 所以 `service.unlink()` **不會**連坐刪 project，**不會**觸發 project_id cascade
- 唯一清掉 user-scope MCP 的路徑是 cleanup hook
- 若 cleanup hook 寫錯（例如放在 helm uninstall 後 / 漏 filter scope='user' / 漏 sudo）→ MCP 變 zombie

**Mitigation**：tester 寫 case 鎖死順序（test #8）；hook 加註解明確「先 unlink → 後 helm uninstall」。

### Risk #3：scope='system' 仍對所有 base.group_user 可讀

**Status**: 本次不改 system-scope ACL（明文 OUT OF SCOPE）。

**現實**：system MCP 設計上是「平台級工具」，admin 在後台統一管理，給所有 ai.assistant 用。對所有 internal user 可讀**符合語意**。但若未來 system MCP 含 sensitive 配置（如 API key），這個假設要重新 review。

**Caveat**：本次 plan 的 D5 rule 用 `'|', ('scope', '=', 'system'), (user-scope domain)` — system 那條腿是 always-true（對任何 base.group_user 都放行），這是**故意設計**，不是 [[cross-workspace-task-data-leak]] 講的 OR-bypass 失誤（B1 的 always-true leg 是 std rule 設計時對「員工專案預設可見」的業務假設；這裡的 always-true leg 是 system-scope 的明確設計選擇）。

### Risk #4：`mcp_server.url` 計算仍依賴 backing service

**Status**: AC #8 鎖住路徑改動；不改算法。

**含義**：即使 schema 上 mcp_server 不再有 `cloud_service_id`，邏輯上 `_model_build_mcp_endpoint_url` 仍要走 `self.project_id.cloud_service_id` 拿 helm 資訊。Project 必須是 bound 才能算 URL — 這也是 D5 constraint 強制 user-scope MCP 掛 bound project 的物理理由。

## Resolved open questions（leader sign-off 2026-05-18）

1. **Inventory 結果**: kubectl exec 兩端被擋 → user 自己跑 SQL。plan 已採 defensive backfill 不依賴結果。Reporter phase 收到結果後更新 [[mcp-project-decouple]] Risk #1 caveat。
2. **service 端 cleanup hook 順序**: **Resolved → unlink user-scope MCP → `uninstall_release` → state='deleting'**。時間窗風險分析寫在 [[mcp-project-decouple]] Invariant #4。Tester 必須鎖此順序（test #8）。
3. **`_model_auto_create_mcp_server` 無 project 處理**: **Resolved → skip + log info**（不擴 scope 自動建 project）。Tester 寫 case 鎖「service running 但無 support project → skip auto-create + log info（不 raise）」。Future retry 在 user 建 project 時觸發是 follow-up，本次不解。
4. **Orphan migration 行為**: **Resolved → archive (active=False) + log warning，不 unlink**。Migration 不可逆，archive 留 rollback 救命繩。後續清理 job 是 follow-up。

## Hand-off 給 tester

實作前先讀：
- [[mcp-project-decouple]] §Invariants（draft 在 `docs/decisions/refactor/`）
- [[paas-workspace-member-acl-tightening]]
- [[cross-workspace-task-data-leak]]（B1 pitfall — 對等 sub-model rule 邏輯）

新增 test 文件（5 個）：
- `src/tests/test_mcp_project_decouple_migration.py`
- `src/tests/test_mcp_project_lifecycle.py`
- `src/tests/test_mcp_acl_isolation.py`
- `src/tests/test_mcp_chat_consumption.py`（或 extend 既有）
- 既有 `test_mcp_integration.py` + `test_ux_routing_views.py` extend

Test 對 AC 的映射：每條 AC 應該至少對應一條 test case。Cross-workspace isolation 是 B1-style hard line — 沒寫測 = blocker。
