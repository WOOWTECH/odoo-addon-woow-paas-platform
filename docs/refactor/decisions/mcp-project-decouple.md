# Decision: MCP `cloud_service_id` → `project_id` Refactor

**Date**: 2026-05-18
**Status**: draft (pending reporter finalize)
**Components**: `woow_paas_platform.mcp_server`, `woow_paas_platform.mcp_tool`, `project.project`, `cloud_service`, `ir.rule`

> Draft — finalize 在 reporter phase。此版本反映 plan 階段 leader 拍板的 6 個決策（D1/D2/D3/D4/D5/D6），實作後可能補充。

## Context

Refactor 前的耦合：`mcp_server` 有 `cloud_service_id` M2O 指向 `woow_paas_platform.cloud_service`。AI chat 的真實消費鏈是 `channel → task → project → cloud_service → user_mcp_server_ids → tools` — `cloud_service` 在這條鏈上只是中介，**真正擁有 AI chat context 的是 `project`**。

語意錯位的具體後果：
- 任何 AI chat 邏輯要拿 user-scope MCP 都得多走一跳 `project → cloud_service`
- 「Project 屬於 AI chat 場域」這個業務語意在 schema 沒被反映
- 配套缺陷：`mcp_server` / `mcp_tool` 完全沒有 `ir.rule`，整個 instance 上的所有 MCP（含 user-scope）對所有 `base.group_user` 可讀 — pre-existing leak，類似 [[cloud-service-project-decouple]] §Context 描述的 `cloud_service` ACL 缺口

兩個耦合面要在同一個 PR 處理：消費鏈改路徑 + ACL 從無到有。否則 ACL 補洞會被新消費鏈引入的新存取 path 繞過。

物理現實的張力：**user-scope MCP 語意上歸 project，物理上是 sidecar 的 wrapper**。`mcp_server.url` 來自 `service.helm_release_name + service.helm_namespace` — 即使 schema 改成 project_id，url 計算仍要回查 service。Lifecycle 因此必須在兩條路徑都對齊：
- `project.unlink()` → DB cascade（`project_id ondelete='cascade'`）
- `service.unlink()` → service 端 cleanup hook 主動 unlink user-scope MCP（service 真實 delete 前）

跨平面動作：Schema（drop `cloud_service_id`、加 `project_id`）+ Migration（backfill + drop column 一刀過）+ ACL（兩條新 ir.rule）+ Constraint（兩條 Python `_check_*`）+ Lifecycle（service `_do_cleanup` hook）+ Backend view + Frontend OWL（service tab API 微調）。

## Decision

採取「**純改名 + service 端 lifecycle hook**」的 surgical refactor：

- **Schema (D1=B)**：drop `mcp_server.cloud_service_id`，新增 `mcp_server.project_id` (M2O → `project.project`, `ondelete='cascade'`)。`cloud_service.user_mcp_server_ids` 反向 O2M 一併移除。`project.project` 新增 `mcp_server_ids` 反向 O2M。
- **Lifecycle (D1)**：`cloud_service._do_cleanup` 在 helm uninstall **之前**主動 `unlink` 該 service 對應的 user-scope MCP；project 不動。Project 端則靠 DB cascade。
- **Migration (D2/D3)**：`18.0.1.0.24/post-migrate.py` 做 defensive backfill SQL（idempotent） + orphan archive (active=False，**不 unlink** — 留 rollback 救命繩) + 一次 drop `cloud_service_id` column。**不**走兩階段 deprecate。
- **Constraint (D4/D5)**：保留 `scope` 欄位，加 Python `@api.constrains`：
  - `_check_scope_project_consistency`：`scope='user' ⇔ project_id IS NOT NULL`
  - `_check_user_scope_requires_bound_project`：user-scope MCP 的 project 必須是 bound（`project_id.cloud_service_id IS NOT NULL`）
- **ACL**：新增 `mcp_server_rule_workspace_member` + `mcp_tool_rule_workspace_member` 兩條 `ir.rule` 套 `base.group_user`，dual-leg domain：`'|', ('scope'='system'), (workspace member chain)`。CSV row 從 `1,0,0,0` 提升為 `1,1,1,1`，靠 rule 兜底範圍。
- **AI chat consumer**：`discuss_channel._get_user_mcp_tools` 與 `controllers/ai_assistant._get_user_mcp_tools_for_channel` 改走 `task.project_id.mcp_server_ids`。
- **Backend view (D6=A)**：`mcp_server_views.xml` form 上 `cloud_service_id` 欄位替換為 `project_id`。
- **Frontend (D6=A)**：`McpServersTab.xml` 保留在 service 頁，內部 query 改 `service.project_ids[0].mcp_server_ids`，**不**搬到 project 頁。

## Considered options

本節記錄「為什麼這類選擇對這類情境是對的」，逐項對比細節在 `plan-mcp-project-decouple.md §Rejected options`。

### 1. 純改名 + cleanup hook vs 雙 FK vs UI-only（D1）

**為什麼純改名 + cleanup hook 對這類 refactor 對**：「MCP 是 project 的附屬資源」是 brief 字面語意。Schema 必須反映語意，否則 maintainer 看 schema 還是會以為 MCP 屬於 service。雙 FK（cloud_service_id + project_id 都留）會留兩個 source of truth — 任何寫入時都要 sync，consistency invariant 變難守。UI-only（schema 不動）等於把語意決定塞在邏輯層，違反「schema is the most durable contract」。

**為什麼 cleanup hook 是配套必需**：user-scope MCP 物理上是 sidecar 的 wrapper（url 從 service 算）。Schema 上歸 project 後，service.unlink() 不會觸發 project_id cascade（service 跟 project 是 `ondelete='set null'` 雙向解耦，[[cloud-service-project-decouple]] Invariant #2）。沒 cleanup hook = service 刪後 MCP 變 zombie（指向已不存在的 sidecar URL）。

**對未來而言**：若 MCP 改成完全跟 service 同生共死（罕見），可把 cleanup hook 拔掉，改 `cloud_service.project_ids` cascade — 但要走獨立 plan + Invariant #2 review。

### 2. 一次 drop column vs 兩階段 deprecate（D3）

**為什麼一次 drop 對這類 surgical refactor 對**：
- 本次 deploy target 只有 dev-sandbox + stg，無正式 prod data（inventory 未由實環境 SQL 驗證，但 backfill 採 defensive 設計 — see `plan-mcp-project-decouple.md` Open Risk #1）
- Rollback 顆粒度是「整包退版」，不會用個別 column rollback
- 兩階段會留 dead column 兩個版本，是 cognitive load + 增加 mistake surface

**對未來而言**：若未來這類 refactor 涉及 production data + 有實際停機成本，兩階段 deprecate 才是合理選擇（先改 application code 不讀該 column → 一段時間後再 drop）。本次不適用。

### 3. Scope 欄位保留 + Python constraint（D4）

**為什麼保留 scope 而非用 project_id NULL 反推**：
- `scope` 是顯式 enum，比「`project_id IS NULL` ⇒ system」這種 implicit semantics 更 self-documenting
- 未來若要加第三種 scope（如 `org`），保留 enum 可擴
- Constraint 用 Python `@api.constrains` 而非 DB constraint：因為要驗證雙向蘊含（`scope='user' ⇔ project_id NOT NULL` 兩個方向）— DB constraint 寫起來會比較囉嗦

### 4. User-scope MCP 必須掛 bound project（D5）

**為什麼禁止 unbound project 有 user-scope MCP**：
- User-scope MCP 物理上是 sidecar wrapper — sidecar 屬於 service，service 屬於 workspace
- Unbound project（無 cloud_service_id）= 沒 service = 沒 sidecar = 沒 MCP 該存在的物理基礎
- Constraint 跟物理現實對齊；違反現實的狀態根本不該被允許 create
- B1 pitfall 提醒：靠 ACL rule 兜底「不該存在的狀態」是補丁、不解 root cause。從 schema 層拒絕創建才是正解

**對 ACL rule 的 implication**：D5 ACL rule domain `('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)` — 看起來怕「unbound project 的 user-scope MCP」會 leak（cloud_service_id IS NULL → 整條 chain False → 看不到 → 等於 cross-user invisible）。但 constraint 已禁止這種 row 存在，所以 ACL domain 不需要 dual-leg fallback。

### 5. ACL 自家 model，無 std rule 對等問題（D5/ACL）

**為什麼不需要 [[odoo-addon-std-rule-symmetric-deactivation]] checklist**：
- `mcp_server` / `mcp_tool` 是自家 model（`_name = 'woow_paas_platform.*'`），Odoo `addons/` 內無同名 std rule
- 加 ACL 不會 trigger 任何被 OR-bypass 的 std rule
- 不需要 migration deactivate SQL，也不需要對等 sub-model rule deactivation

**Verify SQL**（可選 sanity，但非必要）：
```sql
SELECT d.module, d.name FROM ir_model_data d
JOIN ir_rule r ON r.id = d.res_id AND d.model = 'ir.rule'
WHERE r.model_id IN (
  SELECT id FROM ir_model WHERE model IN (
    'woow_paas_platform.mcp_server', 'woow_paas_platform.mcp_tool'
  )
) AND d.module != 'woow_paas_platform';
-- expect: 0 rows
```

### 6. 前端不搬（D6）

**為什麼 service 頁列 MCP 仍合理**：
- User-scope MCP 是 sidecar wrapper — user 在 service 頁看「該 service 的 MCP」符合心智模型
- 搬到 project 頁等於改 UX 路徑，比本次 schema/ACL 動作 surface 大
- Brief 字面要求是「AI chat 從 project 拿 MCP server」（消費鏈），不是「前端入口從 service 搬到 project」（UX 入口）

**對未來而言**：若使用者實際 feedback「我都從 project 進 chat，找不到 MCP 設定」，再評估搬 — 屬於 UX feature 而非 schema refactor。

## Consequences

### Positive

- **語意對齊**：MCP 在 schema 層直接歸 project；maintainer 讀 model 即可理解 AI chat 消費鏈
- **ACL 從無到有**：補上 pre-existing leak（mcp_server / mcp_tool 對 cross-workspace user 不再可讀）
- **Lifecycle 對齊**：service 刪、project 刪兩條路徑都不會留 zombie
- **Schema 簡單**：一個 column 改名 + 一個反向 O2M 移到 project，無雙 FK / sync overhead
- **Rollback 顆粒度清晰**：整包退版 = 完整 rollback；不需要個別 column 復原
- **配合既有 invariant**：D5 constraint 與 [[cloud-service-project-decouple]] Invariant #2（service↔project `ondelete='set null'`）天然一致，cleanup hook 是必要配套而非衝突補丁

### Negative / 要持續注意

- **D1 cleanup hook 必須維持「unlink MCP → helm uninstall」順序**。任何把 hook 移到 helm uninstall 後 / 漏 filter scope='user' 的修改 = MCP zombie。Test #8 鎖此順序，PR review 必看 hook 位置。
- **D5 constraint 與 cleanup hook 互依**：constraint 假設「user-scope MCP 必有 bound project」；cleanup hook 必須在 project unbind 或 service unlink 前清掉 user-scope MCP，否則 constraint 在 unbind 動作中 violate（無法寫成）。
- **`scope='system'` 仍對所有 `base.group_user` 可讀**（Risk #3）。本次不收緊 system-scope ACL — 假設 system MCP 是平台級工具、含 admin-curated 配置。若未來引入 sensitive system MCP（含 API key 等），需重新 review。
- **`mcp_server.url` 仍依賴 backing service**（Risk #4）。Schema 上 mcp_server 看不到 service，但 url 計算邏輯仍走 `self.project_id.cloud_service_id` 拿 helm namespace。Project unbind 後 url 失效 — 這個情境靠 D5 constraint 阻止（user-scope MCP 在 unbind 動作中已被 cleanup hook 清掉）。
- **`_model_auto_create_mcp_server` 對「service running 但無 project」情境無 retry**。目前實作 skip create — 後續 user 建 project 不會自動補建 MCP。屬於 follow-up，本次 plan 暫不解決（Open Q #3）。
- **Migration 一次 drop column 不可逆**（Risk #1）。dev-sandbox / stg 的 inventory SQL 因 shared infra 授權問題未由 planner / leader 直接驗證，已交由 user 跑；plan 採 defensive backfill 設計（idempotent + 0 row noop + orphan cleanup），不依賴 inventory 結果即可安全 deploy。Production deploy 流程未來必須加 `SELECT COUNT(*) FROM woow_paas_platform_mcp_server WHERE scope='user'` sanity check 作為 deploy gate。

## Invariants（後人改本區前必看）

對應 [[cloud-service-project-decouple]] §Invariants 1-7 的格式 — 改本 plan 落地的程式碼前必看：

1. **`mcp_server.project_id` 必須維持 `ondelete='cascade'`**。改成 `set null` 會把 deleted project 的 user-scope MCP 變 zombie；改成 `restrict` 會把刪 project 卡死（user-scope MCP 還在）。Project cascade 行為是 D1 的根基。

2. **`scope='user' ⇔ project_id IS NOT NULL`（D4 invariant）**。任何寫入路徑（ORM create/write、migration backfill、direct SQL）破壞此關係 = `_check_scope_project_consistency` raise。`scope` 與 `project_id` 必須同步遷移 — 不可只改一個。

3. **User-scope MCP 必須掛 bound project（D5 invariant）**。`scope='user' AND project_id.cloud_service_id IS NULL` 是禁止狀態。Cleanup hook（D1）負責在「project unbind」與「service unlink」場景下先清掉 user-scope MCP，防止這個狀態出現。任何拔 cleanup hook / 改 unbind 邏輯的動作 = 違反 invariant。

4. **`cloud_service._do_cleanup` 必須在 helm uninstall 之前主動 unlink user-scope MCP（D1 invariant）**。順序：先 `self.project_ids.mapped('mcp_server_ids').filtered(lambda s: s.scope == 'user').sudo().unlink()` → 再 `client.uninstall_release(...)` → 再 `self.write({'state': 'deleting'})`。test #8 是這個 invariant 的 regression lock。

   **為何這個順序（後人不要重排）**：cleanup 三步驟之間的時間窗存在的「中介狀態」是 race condition 來源。順序選擇明確避開兩個風險：
   - **若先 uninstall 再 unlink**：時間窗 1 內 helm 已拆 sidecar、url 無效，但 ORM 還看得到 mcp_server 與 mcp_tool 的 active row — 另一個 process（cron / SSE 串流 / channel.message_post）此刻拿到 mcp_tool 引用發起 RPC，連線到不存在的 endpoint → uncaught NetworkError / silent fail。AI chat 回覆會看起來「執行 tool 但無 output」— 比 fast-fail 更難 debug。
   - **若先 write state='deleting' 再 unlink**：時間窗 2 內 state 已是 deleting（front-end 假設 service 在拆），但 mcp_server 還 active — UI 可能同時觸發 cleanup retry / cron 觸發 auto-create 重建 MCP，造成 ORM 狀態打架。
   - **先 unlink → 再 uninstall → 再 state='deleting'** 的理由：ORM 層先一致（mcp_server / mcp_tool 即刻看不到，後續路徑全 fail-fast），再讓 helm 物理清理（時間窗內物件已不可被 reference），最後 state 收尾標示 deleting 給其他 process 看。**三步 atomicity 不夠**（Odoo transaction 內 helm RPC 不能 rollback），但時間窗風險最小。

   改順序會讓 service 刪後留 MCP zombie（url 失效但 row 還在），最終由 D5 constraint 在 user 動作中 raise — 但 user-facing error 比 quiet cleanup 差。

5. **`mcp_server` ACL 必須有 `mcp_server_rule_workspace_member` + `mcp_tool_rule_workspace_member` 兩條 rule 對等存在**。Tool 走 `server_id` 反向 — 任何加 `mcp_server` ACL 但漏 `mcp_tool` 對等 rule = sub-model leak（B1-style）。雖然兩個 model 都是自家、無 std rule 對等問題，但 ACL 對等性原則一樣適用 — see [[odoo-addon-std-rule-symmetric-deactivation]] §Why pattern。

6. **`mcp_server_rule_workspace_member` 的 system-scope leg 是故意的 always-true，非 OR-bypass bug**。Domain `'|', ('scope', '=', 'system'), (workspace chain)` — system 那條腿對任何 `base.group_user` 永真。這是設計：system MCP 是平台級工具，所有 internal user 透過 ai.assistant 用。**不要**因為看起來像 [[cross-workspace-task-data-leak]] 的 always-true leg 就「修」掉它 — 它跟 B1 的 std rule always-true 性質不同（B1 是 std rule 對 employees-privacy project 的隱含放行；這裡是明確 scope='system' 的顯式設計）。

7. **`mcp_server.url` 算法仍走 `self.project_id.cloud_service_id` 取 backing service**。Schema 上看不到 service 但邏輯上依賴 service — 任何 inline `self.cloud_service_id` 改寫漏掉 `_model_build_mcp_endpoint_url` 會炸（NoneType chain）。Migration 後 grep `self.cloud_service_id` 在 `mcp_server.py` 內必須 0 hit。

## See also

- Patterns:
  - [[paas-workspace-member-acl-tightening]] — `mcp_server` / `mcp_tool` ACL 的三件套
  - [[odoo-addon-std-rule-symmetric-deactivation]] — 雖然本次 model 是自家、無 std rule 對等，但「ACL 對等」原則仍適用（mcp_server ↔ mcp_tool）
- Pitfalls:
  - [[cross-workspace-task-data-leak]] — B1 pitfall，sub-model 對等 leak 範例（mcp_tool 必須跟 mcp_server 一起加 rule）
- Plan:
  - `docs/refactor/plan-mcp-project-decouple.md`（本 decision 的 implementation plan）
- Inputs:
  - Brief from refactor-leader, 2026-05-18
  - Brainstorm with refactor-leader, 2026-05-18（D1-D6 拍板）
- 關鍵 code（plan 落地後填）:
  - `src/migrations/18.0.1.0.24/post-migrate.py:?`（backfill + orphan cleanup + drop column）
  - `src/security/ir_rules.xml:?`（兩條新 rule）
  - `src/models/mcp_server.py:?`（`project_id` 欄位 + 兩條 constraint + `_model_build_mcp_endpoint_url` 路徑改）
  - `src/models/cloud_service.py:?`（`_do_cleanup` hook + 移除 `user_mcp_server_ids` O2M）
  - `src/models/project_project.py:?`（新增 `mcp_server_ids` O2M）
  - `src/tests/test_mcp_acl_isolation.py`（B1-style cross-workspace regression）
