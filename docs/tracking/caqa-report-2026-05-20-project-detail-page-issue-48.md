---
name: caqa-report-2026-05-20-project-detail-page-issue-48
description: CAQA phase report for Issue #48 Project Detail page (commit 63f49c1) — quality / security / performance / architecture analysis with one HIGH BLOCKER finding (B1 fresh-install fix incomplete) and several medium caveats.
date: 2026-05-20
phase: caqa
issue: 48
branch: feature/project-setting
commit: 63f49c1
related:
  - docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md
  - docs/tracking/test-reports/2026-05-20-project-detail-page-issue-48-tdd-red.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md
---

# CAQA Report — Project Detail Page (Issue #48)

**Audit scope**: commit `63f49c1` on `feature/project-setting`, 27 files / +3657 / −314.
**Auditor**: phase-caqa
**Date**: 2026-05-20
**Spec**: `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` (D1-D10 + OR-1..OR-8)
**Upstream reports**: tester `/tmp/tester-report-issue-48.md`, test-explainer `docs/tracking/test-reports/2026-05-20-project-detail-page-issue-48-tdd-red.md`, implementer `/tmp/implementation-report-issue-48.md`

---

## Verdict

**blocked** — 1 HIGH blocker on AC #29 B1 fresh-install fix (incomplete deactivation reopens the project-side leak on fresh-install path; same root cause class as the original B1 incident the PR is trying to fix). Several medium caveats below that don't block but should be addressed at reviewer/follow-up.

---

## 四面向結果

### Quality

- [info] `src/static/src/paas/pages/project-detail/sections/BasicInfoSection.js:32-40` — `useEffect` 依賴 `() => [this.descEditorRef.el]` 會在每次 ref 變動時 reseed `el.innerHTML`，邏輯正確且註解清楚說明「不能 bind innerHTML 到 state（OWL re-render 會在每個 keystroke reset cursor）」。Idiomatic OWL 處理 contenteditable 的好範例。
- [info] `src/controllers/paas.py:1601-1626` — `_create_project_mcp_server` 用 `request.env.cr.savepoint()` 包 ORM create，正確 cover D5 ORM constraint 觸發時的 orphan row roll-back。Savepoint 釋放/rollback 在 with-block 結束時自動處理（standard Odoo idiom），不需手動 commit。
- [info] `src/controllers/paas.py:1638-1651` — `_update_project_mcp_server` 用 whitelist 過濾欄位（name / url / transport / api_key / headers_json / description），**故意排除** `scope` 與 `project_id` — 守住 D4/D5 invariant 不被 user 透過 update 路徑改寫成 system-scope 或跨 project 搬。乾淨的 invariant 防護。
- [caveat] `src/controllers/paas.py:1519-1527` — rebind 路徑回兩種訊息：「Cloud Service not found」（target.exists() 失敗）vs「Cloud Service must belong to the same workspace.」（不同 workspace）。前者洩漏 cloud_service id 存在性（可枚舉），後者不洩漏。建議統一改成同訊息「Cloud Service not found」或「Invalid cloud service target」，避免 enumeration（與 AC #11 防 id enumeration 一致原則）。
- [caveat] `src/controllers/paas.py:1672-1678` — `_test_project_mcp_server` 把 connection test 異常 message 直接回給 client（`f'Connection test failed: {e}'`）。`e` 是 langchain-mcp-adapters 拋的，可能包含內部 stack / endpoint 資訊。建議只回固定字串「Connection test failed」並 `_logger.warning(... %s, e)`。同樣的點在 `_sync_project_mcp_server:1666-1668`。低風險（user 已是 owner/admin，能看到的 endpoint 本來就是他/她設定的 url），但屬「不洩內部錯誤」最佳實踐。
- [caveat] `src/static/src/paas/pages/project-detail/sections/BasicInfoSection.js:90-103` — `applyFormat` 用 `document.execCommand`。此 API 為 legacy（W3C 已 deprecated），但 contenteditable 場景仍是事實標準（瀏覽器商 still ship）。短期沒問題，2-3 年後可能需要遷移到 ProseMirror / TipTap / Lexical 之類的現代 editor。建議在 follow-up 開 issue 追蹤；本 PR 不必做。
- [info] 本 PR 整體 code style 一致：endpoint dispatcher → action match → helper method 切分（與既有 `api_workspace`, `api_cloud_service_detail` pattern 一致）。Python type hint、docstring 完整、變數命名 idiomatic（`_PROJECT_NOT_FOUND_ERROR` 常數 + `_SyntheticAccess` 內部 class + `_resolve_*` helper pattern）。

### Security

- **[blocker]** `src/security/ir_rules.xml:230-233` — B1 fresh-install fix 不完整。詳細攻擊鏈在下方 §"Blocker 清單" + §"Known pitfalls 對照"。簡要：`<function>` 只 deactivate 2 條 std rule（`task_visibility_rule` + `task_visibility_rule_project_user`），**漏掉** `project.project_public_members_rule`（套 `base.group_user`、`project.project`、含 always-true leg `('privacy_visibility', '!=', 'followers')` 對 default `'employees'` 永真）。Fresh install 路徑不跑 migration，所以該 std rule 仍 active；自家 `project_rule_workspace_member` (line 65) 與其 OR-combined → cross-workspace `project.project` ORM read leak。同源 pattern [[cross-workspace-task-data-leak]] 重蹈。
- [caveat-medium] `src/controllers/paas.py:1452` — `_SyntheticAccess(role=ROLE_OWNER)` 對 unbound project（`workspace_id` is NULL）給 ANY 已認證 internal user。Spec OR-7 明示「不修既有 leak」是 documented carve-out，但實作攻擊面比 spec 預期更廣：
  - **獨立場景**（即使 B-1 blocker 修好，也仍存在）：任意 internal user 對 unbound project ID 做 brute-force guess（id ∈ [1, N]），所有 hit 的 unbound id 都拿到 `ROLE_OWNER` — 可讀整 project payload（name / description / mcp_server_ids / task_count / created_date）並 update name / description。Endpoint 對 unbound 路徑**沒有**防 enumeration（與 bound non-member 路徑的統一防護不對稱）。
  - **組合場景**（fresh-install + B-1 active）：ORM 層 enumerate 所有 project（含 unbound），不必 brute-force，攻擊面更明顯。
  - Rebind 路徑因 `current_ws` 為 None 會被 `_update_support_project:1521-1526` 擋下（無法 transition unbound → bound via endpoint），所以 update 範圍限 name / description（非 cloud_service_id）。
  - **建議**（不擋本 PR，但 reviewer 須評估）：把 OR-7 carve-out 收緊到 `create_uid == user.id OR user.partner_id in message_partner_ids`（與 `ir_rules.xml:68` 的 unbound leg 一致）— 不再給 ROLE_OWNER 給「無關 internal user」。spec OR-7 三 option 之一即為此（creator-only）。
  - 此 caveat 在 spec 標 「OR-7 deferred、不寫 test cover unbound」— 是 known 風險，但 sub-agent 獨立 audit 認為 brute-force 攻擊面值得獨立 caveat（不只 composite with B-1）。
- [caveat] `src/static/src/paas/services/html_sanitize.js:15` — `ALLOWED_ATTR` 包含 `target` 但**未強制** `rel`。Description 內 `<a target="_blank">...</a>` 沒有 `rel="noopener noreferrer"` 時，open 出去的新分頁可透過 `window.opener` 對原頁做 tabnabbing（改 URL、釣魚）。建議：DOMPurify 加 `hook('afterSanitizeAttributes')`，對含 `target="_blank"` 的 anchor 自動補 `rel="noopener noreferrer"`。Odoo std `fields.Html(sanitize=True)` 的後端 sanitize 也不會自動補 rel（檢視 `odoo/tools/mail.py:html_sanitize`），所以前後端兩層都有此 gap。Medium severity — description 不是 user-generated mass content，但 owner/admin 寫的內容會被其他 member read（描述 read 路徑無 ACL 細分）。
- [info] D10 XSS surface 整體 OK：
  - 後端：`project.description` 為 std `fields.Html(sanitize=True)` — 寫入時自動 bleach-based sanitize（strip `<script>`、`on*`-handlers、`javascript:` URIs）。
  - 前端 read：`safeHtml(this.props.project.description)` 走 DOMPurify + 白名單（ALLOWED_TAGS 14 個 + ALLOWED_ATTR 4 個），t-out 渲染為 markup。
  - 前端 edit：contenteditable 的 `el.innerHTML` 在 save 時送回後端 raw — 由後端 sanitize 把關（不在前端 sanitize，因 contenteditable 內容多次 edit 應該都先入後端再回吐）。
  - **Defense-in-depth 兩層都有，沒發現 XSS 攻擊 vector** survive 兩層的（除上面 `target="_blank"` tabnabbing caveat）。
  - `safeHtml` fallback（DOMPurify 沒載入時）用 text escape — 安全（會把所有 HTML 視為純文字）。
- [info] `_resolve_project_access:1437-1456` 對「不存在 id」「workspace 不存在但 project 存在（unbound）」「project + workspace 都存在但 user 非 member」三條失敗路徑全部回 `(None, None)` → 統一 `Project not found or access denied`。**防 id enumeration 設計正確** — 攻擊者無法用訊息差異判定 project 是否存在、workspace 是否存在、自己是否為 member。實作與 AC #6 / #11 一致。
- [info] `_update_project_mcp_server` 用 sudo env（line 1632 `McpServer.sudo()`）所以後續 write 也跑 sudo — 跳過 ir.rule。ACL 純由 controller-level role check（owner/admin gate at line 1577）守。**這是設計選擇而非 bug**（role check 已先 gate），但意味著「ir.rule 不是 defense-in-depth」— 若未來 controller 邏輯有 regression 就直接 leak。Low severity，但建議在 follow-up 評估是否改成 with_user(env.user) 跑 ORM 讓 ir.rule 做第二層守護。
- [info] `api_key` 欄位在 mcp_server 上有 `groups='base.group_system'` (mcp_server.py:37) 限制存取。`_create_project_mcp_server` 與 `_update_project_mcp_server` 都允許 workspace owner/admin write `api_key`（透過 sudo）— 等於跳過 group_system 守護。**但語境上合理**：user-scope MCP 是 workspace member 自己的；他/她本來就應該能設 api_key。**System-scope MCP** 的 api_key 不受影響（endpoint 只能 update user-scope MCP，因為 _resolve_project_mcp 限定 server.project_id == project.id 而 system-scope project_id 永為 NULL）。Info-level — 是 ACL 設計的 expected behavior，但值得在後續 doc 標明。
- [caveat-medium] **TOCTOU race in `_create_project_mcp_server`**（sub-agent F4）— `_resolve_project_access(id)` 完成（返回 access record）→ controller 開 savepoint → `McpServer.create({scope='user', project_id=P.id})`。在 resolve 與 create 之間的時間窗內，concurrent transaction 可能改 `project.cloud_service_id`（unbind 或 rebind 到不同 workspace）。Create 看到 P 此刻 bound 狀態 → D5 constraint pass。但 access record 的 workspace 是 stale 的：若 P 已被 rebind 到 attacker 無 access 的 workspace，create 仍以 attacker 的舊 role 通過。攻擊面：需 2 user + 精準時序，且 attacker 要先有 P 的 access record。**Severity medium** — 低 exploitability、潛在 MCP cross-workspace leak。**Suggested fix direction**：在 savepoint 內 re-fetch project 並 re-validate workspace access（或對 project row 加 `FOR UPDATE` lock）。
- [caveat-low] **F1 spillover** to related-field reads（sub-agent F2 sub-finding）— B-1 blocker 不只 leak `Project.search()` 直接路徑；任何 M2O / O2M / related field 指向 `project.project` 的 ORM walk 同樣套此 ir.rule。具體：`mcp_server.project_id` 是 M2O，base.group_user 透過 `mcp_server.sudo(False).project_id.name` 走 ORM read 會踩 std rule OR-bypass，read 到別 workspace 的 project name。當前 system-scope MCP 對所有 user 可讀（mcp_server_rule_system_readonly），且 system MCP 的 `project_id` 應為 NULL（D4 invariant 守），所以**直接踩這條 path 機率低**。但任何 future addon 加 model M2O 到 `project.project` 都會 affected。記為 F1 fix 後的「對等性 check 範圍」提示。
- [caveat-low] **F6 ROLE_USER permission dict 不一致**（sub-agent F6）— `src/models/workspace_access.py:137-144` 在 `get_role_permissions` 宣告 `ROLE_USER.can_edit = True`，但 `src/controllers/paas.py:1481` + `:1577` 對 ROLE_USER 拒絕 update（回 `Permission denied`）。Dict 描述與實際 controller 行為脫鉤 — 任何讀 `get_role_permissions(ROLE_USER)` 的 caller（前端 UI hide/show 按鈕、其他 controller）會誤判 member 有 edit 權限。**Severity low** — 無 security impact（controller 才是 source of truth、不會讓 user 真的 edit），但 code smell。**Fix direction**：dict 改成 `can_edit: False` 對齊 controller，或在 spec 文件化「member 對 project 唯讀、對 workspace 部分可寫」差異。Plan AC #2 / #6 明示 member RWCU policy 是「Read OK / Write 拒」，dict 應對齊。
- [info] CSRF：所有新 endpoint 用 `type="json"`，Odoo `type="json"` route 有 built-in CSRF protection（透過 session_id check + JSON-RPC envelope）。**OK**。
- [info] SQL injection：所有 ORM 走 Odoo recordset / domain，無原生 SQL。**OK**。
- [info] api_key sudo bypass（C-8 升級註記）— sub-agent 獨立評估後認為 by-design 合理：api_key 是 write-only（未在 `_format_mcp_server` 回吐到 response），且 endpoint 限 user-scope MCP，user 為 owner/admin 本有正當理由設此值。**降級為 info-only**，不需 caveat。
- [info] `headers_json` 自訂 `Authorization` header override（sub-agent F9）— user 對自家 user-scope MCP 已 control api_key + headers_json 兩條，header override 不是 privilege escalation。**OK**。

### Performance

- [caveat] `src/controllers/paas.py:1487-1489` + `1593-1595` — `project.mcp_server_ids.filtered(lambda s: s.scope == 'user' and s.active)` 為 Python-level filter。若 project 有大量 MCP server（理論上 user-scope MCP 不會很多，per project 數量上限應該是個位數到十位數），效能足夠。若 future 有 use case 是 100+ MCP per project，可改用 `self.env['woow_paas_platform.mcp_server'].search([('project_id', '=', project.id), ('scope', '=', 'user'), ('active', '=', True)])` 走 DB index。**Info-level**，目前 scale 沒問題。
- [caveat] `_format_project_detail` (paas.py:1486-1502) 取 `project.task_count` + `project.open_task_count`。`task_count` 是 Odoo std computed field（`@api.depends('task_ids')` non-stored），每次存取會 recompute。Page load 一次 ok，但 list page 大量 project 時這個 helper 不會被 list call（list page 走別的 endpoint），所以**目前 OK**。Worth noting 若 future 有 batch fetch_project_detail use case，task_count 會 N+1。
- [info] `_resolve_project_access:1446-1456` 路徑：browse(id) → cloud_service_id walk → workspace.check_user_access 內又 search workspace_access。Search query 用 `[('workspace_id', '=', ...), ('user_id', '=', ...)]` 有 index（workspace_id + user_id 在 model 預設 indexed）。**單次 RTT，無 N+1**。
- [info] Endpoint 每次 read 都 `Project.sudo().browse(id)` — sudo 是 env switch 不是 query，輕量。`_format_mcp_server` 對每個 MCP server 也是 record access，Odoo 自動 prefetch。**OK**。
- [info] Frontend `support_service.js` 的 `updateProject` 對 `this.projects[]` 做 optimistic local merge（line 467-470）— 避免 list page binding badge stale，省一次 reload。**好的 UX 設計**，無 perf 問題。

### Architecture

- [info] 新 endpoint family `/api/support-projects/<id>` + `.../mcp-servers` 命名與既有 `/api/cloud-services/...` 對齊（hyphenated resource）。**避開** 既有 `/api/support/projects/<wid>`（slash pattern + workspace-scoped）的 routing 衝突，與 D4 末段論證一致。OR-6 已記 follow-up（全 repo URL 統一）。
- [info] OWL 元件結構 (`ProjectDetailPage` → `BasicInfoSection` + `CloudServiceBindingSection` + `McpServersSection`) 對應 D1 「單頁 3 卡片」設計，與既有 `ServiceDetailPage` 3-tab 不對稱（spec D1 已論證 project 段落少不必切 tab）。**對齊 spec**。
- [info] McpServersTab 重寫為 redirect placeholder（D2）— forked template，內部邏輯切換到 `supportService.fetchProjectForService(serviceId)` 反查 project；bound vs unbound 兩 branch 文案 measured（不暗示 user-scope MCP 與 cloud_service 有 ownership 關係）。**與 D2 + AC #22 + OR-5 一致**，未引入新耦合。
- [info] `support_service.js` 8 個新 method 全部走 `operationLoading + {success, data?, error?}` envelope — 與既有 method（createProject / updateTask / 等）一致。`updateProject` 對 `this.projects[]` 樂觀更新 — 與 list page binding badge 即時反映需求（AC #17 / #21）一致。
- [info] Router pattern `projects/:id`（hyphenated, exact-match `projects` 在前以保 list page route 優先）— 對齊既有 `services/:id` / `workspaces/:id` 風格。Root.js / root.xml 增 3 行 wire。**乾淨**。
- [caveat] `_SyntheticAccess` 內部 class — 雖然 docstring 明寫 OR-7 carve-out，但實作上是用 duck-typed `role` attribute 模仿 workspace_access record。Future maintainer 若擴 workspace_access 加新欄位（如 `last_active_at`、`role_history` 等），這個 stand-in 只有 `role` 不會跟上。**長期維護債** — 建議改 follow-up 評估是否乾脆對 unbound project 不要 grant access（OR-7 三 option 之一），不要保留 stand-in。
- [caveat] `_update_support_project:1553` `project.sudo().write(vals)` — sudo 跳 ir.rule。整個 endpoint 都跑 sudo（包括 browse、write）。如同 Security 段註記，**這是設計選擇而非 bug**，但意味著 ir.rule 對 endpoint 路徑無 defense-in-depth。**整 PR 的 endpoint 全跑 sudo**（每個 helper 開頭都 `.sudo()`），與 PR #47 後的 mcp_server 路徑 idiom 一致。Info-level，但值得 follow-up 評估「endpoint sudo + role check」是否該演進為「endpoint with_user(env.user) + role check + ir.rule defense-in-depth」。

---

## 重點 focus areas 結果

### D10 description rich-text editor（剛 re-loop 過）

**結論：pass（一條 medium caveat 在 tabnabbing）**

- `src/models/project_project.py` ✓ 確認 revert（沒有 override `description`），純加 `cloud_service_id` / `workspace_id` / `mcp_server_ids` 三欄位 + UNIQUE constraint。Std `fields.Html(sanitize=True)` 自然發揮。
- `BasicInfoSection.js` contenteditable + execCommand + safeHtml chain 設計合理：
  - **入** `el.innerHTML = description` — 描述已是後端 sanitized 過的 HTML，re-seed 安全。
  - **編** `document.execCommand('bold' / 'italic' / 'insertUnorderedList')` — legacy API 但 contenteditable 場景仍是事實標準。vendor 差異（Firefox `<i>` vs Chrome `<em>`）兩種都在 ALLOWED_TAGS 內。
  - **送** `el.innerHTML` raw 給後端，std `fields.Html` sanitize 把關。
  - **讀** `safeHtml(description)` → DOMPurify 白名單。
- Defense-in-depth 兩層 sanitize，沒 vector survive 兩層。
- **medium caveat**：`<a target="_blank">` 沒強制 `rel="noopener noreferrer"`（前後端 sanitize 都沒這個 hook）。Tabnabbing 風險低（description 不是 user-generated mass content）但不為零。建議：DOMPurify 加 `afterSanitizeAttributes` hook 補 rel。

### OR-2 rebind race condition

**結論：caveat（不擋）**

Spec §"Open risks" 標 defer to caqa。實作用 Odoo default transaction（READ COMMITTED）。

評估後 3 個 race scenarios：

| Scenario | 後果 | 防線 | 評估 |
|---|---|---|---|
| **A 改 cloud_service_id=X**，**B 同時 unlink X** | `cloud_service` 對 project 是 `ondelete='set null'` — B unlink 後 FK 自動 set NULL on 此 project；A write `X.id` 後競爭 — DB FK constraint 會 raise `IntegrityError`（X.id 已不存在） | DB FK 自動防（隱性）；錯誤訊息不友善但 invariant 不破 | **OK**（醜但不洩） |
| **A 與 B 同時 rebind 到不同 service** | last writer wins，project.cloud_service_id 一致；UI 反映 stale 狀態但 fetch refresh 後自動修正 | 無顯式防護，依賴 Odoo write atomicity（單 row write 是 atomic） | **OK** |
| **A unbind project P**，**B 同時 create user-scope MCP on P** | A `filtered(scope=='user')` 看到 empty → write `cloud_service_id=False` 通過。B `create({scope='user', project_id=P.id})` 在自己 snapshot 看到 P 仍 bound → `_check_user_scope_requires_bound_project` 通過 → row INSERT。**兩 tx commit 後：P unbound + user-scope MCP exists on P → D5 invariant 在 DB 層破** | 無顯式防護；ORM `@api.constrains` 只在 mcp_server 寫入時 fire | **medium caveat** — D5 真實 race，發生機率低（同 project 同步操作），但 invariant 破裂代價高 |

**建議 fix direction（不擋本 PR）**：
- 短期：accept the race（罕見，且 D5 invariant 破在 DB 層而非執行流程 — 可由 cron job 定期 scan 修正：找出 `scope='user' AND project.cloud_service_id IS NULL` 的 MCP 並 archive）
- 中期：在 unbind 路徑加 `cr.execute("SELECT id FROM project_project WHERE id = %s FOR UPDATE", [project.id])` row lock，確保 unbind 與 concurrent MCP create 互斥
- 長期：D5 invariant 移到 DB CHECK constraint 或 trigger（OR-4 已記，未來 D5 重設計時一併回收）

### D5 invariant edge cases

**結論：pass（一條 caveat 已合併到 OR-2 段）**

- **Endpoint manual query 觸發 D5（AC #3 unbind）** — `_update_support_project:1534-1546` `lingering = project.mcp_server_ids.filtered(scope='user')`。實作對齊 spec AC #3 第三 bullet。錯誤訊息含「Cloud Service」字串（line 1542-1545），對齊 `mcp_server.py:_check_user_scope_requires_bound_project` wording — 與 D5 第二觸發路徑（ORM constraint）的訊息一致，前端與測試可以用同樣的字串 match。
  - **race condition 上面 OR-2 段已 cover** — medium caveat。
- **ORM constraint 觸發 D5（AC #25 second file create）** — `_create_project_mcp_server:1612-1623` 用 `with request.env.cr.savepoint()` 包 ORM create。savepoint 在 with-block 出口處理：例外時 rollback、無例外時 release（合併入外層 tx）。**正確 cover** orphan row（constraint fires after INSERT，沒 savepoint 的話 row 會 persist 到 request tx commit）。
- **Update path 不能改 scope / project_id** — `_update_project_mcp_server:1643` whitelist 排除這兩欄位，守住 D4/D5 不被透過 update 路徑繞過。
- **Delete path 用 unlink** — `cascade='cascade'` on `project_id` (mcp_server.py:61)，delete project 會 cascade delete MCP，無 orphan 風險。Endpoint 的 delete 只刪 MCP 本身，不影響 project。

### ACL gate 完整性

**結論：pass（一條 caveat：composite leak 已合併到 Security blocker）**

| 路徑 | Owner | Admin | Member | Non-member | Nonexistent id | Cross-workspace |
|---|---|---|---|---|---|---|
| `action=get` | ✓ data | ✓ data | ✓ data | 統一防 enum | 統一防 enum | 統一防 enum |
| `action=update` | ✓ write | ✓ write | `Permission denied` | 統一防 enum | 統一防 enum | 統一防 enum |
| `action=list` mcp | ✓ list | ✓ list | ✓ list | 統一防 enum | 統一防 enum | 統一防 enum |
| `action=create/update/delete/sync/test` mcp | ✓ | ✓ | `Permission denied` | 統一防 enum | 統一防 enum | 統一防 enum |

- 三種 error 訊息設計（success/Permission denied/Project not found or access denied）對齊 AC #6 / #11。
- `_resolve_project_access` (line 1437-1456) 對「不存在 id」「workspace=None」「member 不存在」三條失敗路徑全部 return `(None, None)`，由 caller 一律回統一訊息。
- **唯一 carve-out**：unbound project 給 `_SyntheticAccess(ROLE_OWNER)`（line 1452）— spec OR-7 documented，不擋。但與 §"Blocker" 的 fresh-install ORM leak **組合放大**為 composite caveat（fresh install 上任何 user 都能 enumerate unbound project id 後做 update）。修了 blocker 即同時消除 composite caveat。

### AC #29 B1 fresh-install fix

**結論：BLOCKED**

詳細在下方 §"Blocker 清單" + §"AC #29 兩 path test 結果"。

簡述：implementation report 的 verification SQL **只 query 2 條 rule**（task pair），所以 implementation 自己沒測到 `project_public_members_rule` 在 fresh install 上的 active 狀態。Migration upgrade 路徑拔 3 條、`<function>` fresh-install 路徑只拔 2 條 — 對稱破壞，重蹈 [[cross-workspace-task-data-leak]] 的「漏一條」pattern。

### AC #28 staging E2E smoke

**Status: deferred to reviewer / leader**

我的 sandbox：kubectl woow-k3s context、helm、deploy-stg.sh 都可用，理論上能跑 deploy。但 caqa 角色定位是 code-level analysis（role 明訂「你是 analyzer, 不是 fixer」+ 「不修 code」），15-30 min 的 CI build + sandbox lifecycle + 11-step UI 操作超出 analyze scope。

**建議路徑**：
- (a) leader 指派 reviewer phase 跑 staging deploy + 11-step smoke（reviewer 本來就會跑 ultrareview / 各種 PR 級檢查）；
- (b) 或 leader 在本 phase 後另開 staging E2E task 給可執行階段；
- (c) Blocker 修完後一起跑（避免 deploy 跑了 11 步測完後又因 fix 重 deploy）。

**若 leader 要我跑，請明示**：本人有能力，只是預設不跑（職責分工）。

---

## Plan alignment check

- ✓ **D1 layout 3 卡片** — `ProjectDetailPage` mount 3 個 `WoowCard` section，未切 tab。對齊 spec。
- ✓ **D2 McpServersTab redirect** — fork from `McpServersTab.xml`，bound/unbound 兩 branch 文案 measured，未引入新耦合。
- ✓ **D3 list 卡片加 ⚙ icon** — `SupportProjectsPage.xml` `o_woow_sp_settings_btn` + factory handler pattern，stopPropagation 不衝突卡片主點擊。
- ✓ **D4 hyphenated URL** — `/api/support-projects/<int:project_id>` + `.../mcp-servers`，與 cloud-services 對齊；既有 slash pattern 不動。
- ✓ **D5 ACL gate** — workspace.check_user_access + role enum gate；未動 ir.rule（PR #47 已收乾）。
- ✓ **D6 status 不實作** — `_format_project_detail` 無 `status` key；BasicInfoSection 無 status field。
- ✓ **D7 confirm modal** — CloudServiceBindingSection 用 `components/modal`（implementation report mention，本 audit 沒個別讀 CloudServiceBindingSection.xml 細節但 spec 對齊有信心）。
- ✓ **D8 dropdown filter unhealthy** — implementation report mention過濾 `state ∈ {error, deleted, deleting}`。
- ⚠ **D9 / AC #29 B1 fresh-install fix** — **alignment partial**：spec D9 + AC #29 字面只寫「task rule」兩條，implementation 跟著只拔 2 條。**Spec 本身漏了第 3 條** `project_public_members_rule`，與 §migration `18.0.1.0.23/post-migrate.py` line 101 的 3-rule deactivation 不對稱。**Blocker** — 詳見下方。
- ✓ **D10 description rich-text** — std `fields.Html` + contenteditable + execCommand + DOMPurify safeHtml，符合 spec 拍板的「能用原生最好」原則。

**Plan vs implementation divergence**：D9 / AC #29 之間 spec 與 migration 不對稱（spec 漏寫第 3 條 std rule）— 這是 **spec 缺陷**而非 implementation 錯誤。Implementer 完全按 spec 做。修法是 spec 與 implementation 同步補 `project.project_public_members_rule` 到 `<function>` 的 ref list。

---

## 同源 code path 檢查

`grep -rn "project_public_members_rule\|task_visibility_rule"` 結果（已執行）：

- `src/security/ir_rules.xml:106` — 自家 task rule comment 明寫「the three-way deactivation (project_public_members_rule + the two task rules)」。Comment 知 3 條，function 只拔 2 條 — **comment 與 code 不一致**，明顯是 copy-paste 但漏更新。
- `src/security/ir_rules.xml:230-233` — `<function>` 的 `eval` 只 list 2 條。
- `src/migrations/18.0.1.0.23/post-migrate.py:101-103` — migration 拔 3 條（含 `project_public_members_rule`）。
- `src/tests/test_cross_workspace_task_isolation.py:10` — docstring 寫「Implementation deactivated `project.project_public_members_rule` in the `18.0.1.0.22` migration」— 確認 3 條 lockstep 認知存在。
- 沒有任何 `test_cross_workspace_project_via_orm` 測試在 src/tests/ 下 — **project.project 跨 workspace ORM read 沒 regression cover**。`test_get_cross_workspace_project_returns_unified_error` 在 `test_project_detail_endpoints.py:436` 但只測 endpoint 路徑（已 `_resolve_project_access` 守住）— 不測 ORM。

**結論**：3 條 lockstep 認知存在於 comment + migration + task pitfall doc，**但 fresh-install path 的 `<function>` 漏 1 條 + 沒對應 project.project ORM regression test**。同源 pattern 還有沒有沒修的 path：

- ✓ Migration upgrade 路徑：拔 3 條，OK。
- ✗ Fresh-install 路徑：拔 2 條，漏 1 條 → **blocker**。
- ⚠ Test 缺 `project.project` cross-workspace regression — 即使修了 blocker 也要補測，否則未來再次 regression 沒人擋。

---

## Known pitfalls 對照

| Pitfall doc | 對照本 PR | 結果 |
|---|---|---|
| [[cross-workspace-task-data-leak]] | task rule fresh-install 補修；**但同源 project rule 沒補** | ⚠ **重蹈 root cause class**（漏一條 std rule deactivation，與 B1 原 incident 同 pattern） |
| [[odoo-addon-std-rule-symmetric-deactivation]] | Pattern §"(c) Migration: 對等 deactivate Odoo std rules" 明確規定「single UPDATE + `IN` clause」+「不對稱 = anti-pattern」 | ✗ **違反 pattern** — `<function>` ref list 不含 project rule，等同分 2 UPDATE，與 migration 3 條不對稱 |
| [[paas-workspace-member-acl-tightening]] | 三件套（CSV + ir.rule + symmetric deactivation） | ⚠ 第三件「對等 deactivate std rule」在 fresh-install path 不完整 — 同上 |
| [[system-mcp-create-acl-bypass]] (MAJOR 1) | always-true leg × perm direction matrix | ✓ 本 PR 未動 mcp_server / mcp_tool 的 6-rule split，無 regression |
| [[plan-vs-odoo-schema-mismatch]] | plan 引用不存在欄位 | ✓ 沒發現新引用不存在欄位（D6 status 已澄清字面誤會，spec D10 description 也已 verify std `fields.Html` 存在） |

**根因評估**：本次 fresh-install 漏 1 條是「spec 漏寫 + implementer 跟 spec」的 chain。**Spec 階段沒走 [[odoo-addon-std-rule-symmetric-deactivation]] §"(c)" 的 "single UPDATE + IN clause" invariant 檢查** — 從 migration（3 條）抽 task rule 兩條到 ir_rules.xml 時，沒對「3 條 lockstep」做 mental check。Implementer 對 spec 做了忠實實作但沒對 [[cross-workspace-task-data-leak]] 做 inventory cross-ref。

**這條 finding 本身應該補進 [[cross-workspace-task-data-leak]] 的 Hardening 段** — pitfall doc 警告 migration row count，但**沒警告「把 migration 邏輯搬到 declaration data 時要 1:1 對照」**。Reporter phase 應該更新 pitfall doc 加這條。

---

## Blocker 清單（給 leader / implementation）

### B-1 (HIGH SEVERITY): B1 fresh-install fix 漏 1 條 std rule，重蹈 B1 leak pattern

**File / Line**: `src/security/ir_rules.xml:230-233`
**Spec / AC**: D9 / AC #29

**Issue**:
```xml
<function model="ir.rule" name="write" eval="[
    [ref('project.task_visibility_rule'), ref('project.task_visibility_rule_project_user')],
    {'active': False}
]"/>
```

`<function>` 的 eval 只 list 2 條 std rule（兩條都是 `project.task` 上的）— **漏掉** `project.project_public_members_rule`（套 `base.group_user`、`project.project`、含 always-true leg `('privacy_visibility', '!=', 'followers')` 對 Odoo default `privacy_visibility='employees'` 永真）。

**攻擊鏈**（fresh install 路徑，從零跑 `-i woow_paas_platform`）：
1. Migration `18.0.1.0.23/post-migrate.py:34-36` 開頭 `if not version: return` — fresh install 不跑。
2. 因此 `project.project_public_members_rule` 在 fresh install 上 **active = True**（Odoo std 預設 active）。
3. `ir.model.access.csv:26` 給 `base.group_user` `project.project` 的 R/W/C/U 權限。
4. `ir_rules.xml:65-78` 自家 `project_rule_workspace_member` 套 `base.group_user`，restrictive workspace gate。
5. Odoo `ir_rule.py:140-168`：同 group 多 rule = **OR**。
6. → 有效 read domain：`(privacy_visibility != 'followers' OR follower)` **OR** `(workspace member condition)` ≡ 對 default-privacy='employees' project 永真。
7. → 任何 `base.group_user` 透過 ORM `search` / `browse().read()` / M2M widget / search domain 都能 read 全 instance 所有 project.project 的 name / description / message / 任意欄位。

**Impact**:
- Fresh-install 部署直接 leak（任何新 dev sandbox 都 affected）。
- 同源 B1 incident（[[cross-workspace-task-data-leak]]）的 pattern 重現 — 「漏一條 std rule deactivation」就破工程不對稱。
- Endpoint 路徑（`_resolve_project_access` + sudo）不受影響，所以前端 UI 看起來正常，但 **ORM 任何路徑 leak**（包括 Odoo 標準 form view、search wizard、M2M widget — 同 [[cross-workspace-task-data-leak]] §"Why it happens" 講的攻擊面）。
- 與 OR-7 (synthetic ROLE_OWNER for unbound) 組合：fresh install 上攻擊者可 ORM enumerate 任何 project 後，對 unbound project 走 `/api/support-projects/<id>` 拿 update 權限。
- Verification SQL in `/tmp/implementation-report-issue-48.md` 也只 query 2 條 rule — implementer 自己也沒測到第 3 條的 active 狀態。

**Suggested fix direction**（不寫具體 patch，由 implementation phase 落地）：
- 在 `<function>` 的 ref list 加上 `ref('project.project_public_members_rule')`，與 migration 3-rule 對稱：
  ```xml
  <function model="ir.rule" name="write" eval="[
      [ref('project.project_public_members_rule'),
       ref('project.task_visibility_rule'),
       ref('project.task_visibility_rule_project_user')],
      {'active': False}
  ]"/>
  ```
- Spec D9 / AC #29 同步補正：明寫「拔 3 條 std rule」+ 列出全部 xmlid。
- 補一條 `test_alice_cannot_read_bob_project_via_orm` regression（mirror 既有 task 版本，但對 `project.project`）— 否則修了第 3 條也無 lockstep 守線。
- 更新 implementation report verification SQL：query 3 條 rule 的 active 狀態。
- Reporter phase 補強 [[cross-workspace-task-data-leak]] pitfall doc：加「declaration-data 與 migration 對稱」hardening 段。

**Note for reviewer**：這條 finding 不是 implementation 錯誤而是 **spec 漏寫**；implementer 100% 按 spec 做。Fix 落點在 spec 與 ir_rules.xml 兩處同步補。

---

## Caveat 清單（給 leader 決定是否帶入 reviewer）

| ID | File / Line | Severity | Issue | 建議方向 |
|---|---|---|---|---|
| C-1 | `paas.py:1519-1527` | medium | Cloud Service `target.exists()` 失敗回「Cloud Service not found」洩漏 id 存在性，與 same-workspace 失敗訊息不同 | 統一成 `Invalid cloud service target` 或 `Cloud Service not found`，與 AC #11 防 enumeration 一致原則 |
| C-2 | `paas.py:1534-1553` | medium | D5 unbind 路徑 `filtered()` + `write()` 非 atomic — 並發 create user-scope MCP 可破 D5 invariant in DB | 短期 accept + cron clean；中期 row lock；長期 D5 改 DB-level constraint (OR-4) |
| **C-2b** | `paas.py:1601-1626` | medium | **TOCTOU race in create**（sub-agent F4）— `_resolve_project_access` 與 savepoint INSERT 間 concurrent rebind 可讓 stale access record 通過。低 exploitability，但潛在 MCP cross-workspace leak | savepoint 內 re-fetch + re-validate workspace access，或對 project row 加 `FOR UPDATE` lock |
| C-3 | `html_sanitize.js:15` + `BasicInfoSection.xml:91-93` | medium | DOMPurify ALLOWED_ATTR 含 `target` 但沒強制 `rel`，`<a target="_blank">` tabnabbing 風險 | DOMPurify `afterSanitizeAttributes` hook 補 `rel="noopener noreferrer"` |
| **C-3b** | `paas.py:1427-1483` (OR-7 carve-out) | medium | **OR-7 unbound brute-force surface**（sub-agent F3）— 任意 internal user brute-force unbound id 拿 synthetic ROLE_OWNER，可讀整 payload + update name/description。Endpoint 對 unbound 路徑無防 enumeration，與 bound non-member 路徑不對稱 | 收緊 carve-out 到 `create_uid OR message_partner_ids`（與 ir_rules.xml:68 unbound leg 一致），或評估 OR-7 三 option |
| C-4 | `paas.py:1666-1678` | low | `_sync_/test_project_mcp_server` 把內部 exception message 直接回 client（可能含 stack / endpoint 細節） | 固定字串 + `_logger.warning` 細節 |
| C-5 | `BasicInfoSection.js:90-103` | low | `document.execCommand` 為 W3C deprecated API | Follow-up 評估 ProseMirror / TipTap 等現代 editor |
| C-6 | `paas.py:1427-1456` | low | `_SyntheticAccess` duck-typed stand-in（只實作 role attribute）— future workspace_access 加欄位不會跟上 | Follow-up 評估 OR-7 三 option（不要保留 stand-in） |
| C-7 | `paas.py` 整體 endpoint | info | endpoint 全部走 `.sudo()` — ir.rule 對此路徑無 defense-in-depth | Follow-up 評估「endpoint with_user + ir.rule 二層守護」 |
| **C-9** | `workspace_access.py:137-144` vs `paas.py:1481,1577` | low | **ROLE_USER permission dict 不一致**（sub-agent F6）— `get_role_permissions(ROLE_USER).can_edit = True` 但 controller 對 ROLE_USER 拒絕 update。Dict 描述與 controller 行為脫鉤 — 可能誤導前端 UI / 其他 caller | dict 改 `can_edit: False` 對齊 controller，或文件化「workspace member 對 project 唯讀」 |
| **F1-spillover** | M2O / related fields → `project.project` | info | F1 blocker 不只 leak `Project.search()`；任何 M2O / O2M / related field 走 ORM read 同樣套 std rule OR-bypass。當前直接觸發機率低（system MCP `project_id` 永為 NULL），但任何 future addon 加 M2O 都會 affected | F1 fix 後再 sweep 確認 — 對等性 check 範圍 |

C-3 修了 blocker B-1 也建議一起補（前端 sanitize hook）— 雖然 caveat 等級，但 1-2 行 code 即可關閉。C-2b 與 C-2 同源（D5/OR-2 race family），可一併納入 D5 重設計（OR-4）。

**C-8（api_key sudo bypass）** 經 sub-agent 二次評估降級為 info-only：api_key 是 write-only 欄位，未在 response 回吐；user 為 owner/admin 本有正當理由設此值。從 caveat 表移除。

---

## AC #29 兩 path test 結果

依 spec AC #29 要求列出 fresh install + upgrade 兩 path test 結果：

| Path | Implementation report cite | Caqa verify | 結論 |
|---|---|---|---|
| **Upgrade** (woow_develop DB) | `92/92 ✅`（includes `TestTaskCrossWorkspaceACL`）| Verified by implementation; verification SQL 顯示 `task_visibility_rule.active = false` + `task_visibility_rule_project_user.active = false` | ✓ Task leak fix passes |
| **Fresh install** (woow_fresh_b1 DB) | `92/92 ✅`（`TestTaskCrossWorkspaceACL` + 91 others）| Implementation report verification SQL **只 query 2 條 task rule** — `project_public_members_rule` 沒被 query | ⚠ Task leak fix passes BUT project rule status **NOT verified** in implementation report |

**Caqa cross-check**：
- 從 `src/security/ir_rules.xml:230-233` 直接 read：`<function>` ref list **不含** `project.project_public_members_rule`。
- 從 `src/migrations/18.0.1.0.23/post-migrate.py:101` 直接 read：migration 拔 3 條（含 `project_public_members_rule`）。
- → fresh install 上 `project_public_members_rule.active = true`（推論 — 沒實機跑 SQL 確認，但 logic 對 follow trail：std rule 預設 active；XML loader 不 reload noupdate=1 record；`<function>` 沒拔 → active stay true）。
- 沒有 `test_alice_cannot_read_bob_project_via_orm`（grep 確認）→ 此 leak **沒 regression cover**。

**Verification cmd for implementation phase fix**（建議落地後跑）：
```sql
SELECT d.name AS xmlid, r.name, r.active
FROM ir_rule r
JOIN ir_model_data d ON d.res_id = r.id AND d.model = 'ir.rule'
WHERE d.module = 'project'
  AND d.name IN (
    'project_public_members_rule',
    'task_visibility_rule',
    'task_visibility_rule_project_user'
  );
-- expect: 3 rows, all active = false
```

跑 in **fresh-install DB**（不只 upgrade DB），確保補完整。

---

## AC #28 staging E2E smoke status

**Status: deferred (capability exists, scope decision)**

詳見上方 §"AC #28 staging E2E smoke" 段。摘要：

- 本人有能力（kubectl woow-k3s + helm + deploy-stg.sh 全可用）
- 預設不跑：caqa 角色是 code-level analysis；15-30 min deploy + 11-step UI smoke 出 caqa scope
- Blocker 修完前跑也不划算（修完還要重 deploy）
- **建議**：leader 在 blocker fix 後指派 reviewer 跑，或視 sandbox lifecycle 一起帶

---

## Open items / risks

| ID | Item | Owner |
|---|---|---|
| **B-1** | B1 fresh-install fix 漏拔 `project_public_members_rule` | leader → 退回 planner（spec 補正）+ implementation（XML 補 ref） |
| **B-1-test** | 補 `test_alice_cannot_read_bob_project_via_orm` regression | tester (補 red) → implementation 修了 blocker 自然轉綠 |
| **C-1** | Cloud Service id enumeration via rebind error | reviewer / 帶入下個 PR |
| **C-2** | D5 unbind race condition (OR-2 / OR-4) | reviewer caveat + future D5 重設計 owner（OR-4 已記） |
| **C-2b** | TOCTOU race in `_create_project_mcp_server` (sub-agent F4) | reviewer caveat + D5 重設計 owner（OR-4） |
| **C-3** | `<a target="_blank">` tabnabbing | reviewer caveat / 可隨 B-1 fix 一起補（1-2 行） |
| **C-3b** | OR-7 unbound brute-force surface (sub-agent F3 sharpened) | reviewer + planner 評估 OR-7 三 option |
| **C-4** | Exception message leak from sync/test endpoints | reviewer caveat |
| **C-5** | execCommand deprecation | future issue（無時間壓力） |
| **C-6** | `_SyntheticAccess` stand-in 維護債 | future issue（OR-7 三 option 評估，與 C-3b 同 thread） |
| **C-7** | endpoint 全 sudo，ir.rule 無 defense-in-depth | future issue（架構演進） |
| **C-9** | ROLE_USER permission dict 與 controller 不一致 (sub-agent F6) | reviewer / 同步 dict 或文件 |
| **F1-spillover** | F1 leak 對 M2O/related field 的對等性 | F1 fix 後 sweep |
| **Pitfall doc** | [[cross-workspace-task-data-leak]] 補 hardening 段：「declaration-data 與 migration 必須對稱」 | reporter |
| **AC #28** | Staging E2E smoke 11 步 | reviewer 或 leader 指派 |

---

## 工具 raw 輸出摘要

| 工具 | 用途 | 重點輸出 |
|---|---|---|
| `git log -1 --stat 63f49c1` | commit 範圍 | 27 files, +3657/-314；新增 9 OWL files + 2 test files + ir_rules.xml `<function>`；不動 manifest（glob 自動拾） |
| `Read` 上游 reports | tester / test-explainer / implementer | 36 new tests + 54 regression + 2 B1 test = 92/92 green；implementation 完整對齊 spec D1-D10 +OR-1..OR-8（D10 已 re-loop revert） |
| `grep -rn "project_public_members_rule\|task_visibility_rule"` | 同源 pattern check | 5 處 reference（migration 3 條 / ir_rules.xml comment 3 條 / `<function>` 2 條 — 不對稱） |
| `Read src/security/ir_rules.xml` | B1 fresh-install fix 落點 | `<function>` ref list 漏 `project_public_members_rule` — blocker source |
| `Read /Users/eugene/.../odoo-18.0/addons/project/security/project_security.xml` | Odoo std rule 來源 | 3 條 rule (line 72/88/155) 均存在，皆套 base.group_user / group_project_user，皆含 always-true leg |
| `Read src/controllers/paas.py:1420-1685` | 新 endpoint family | 4 helper（_resolve_project_access / _format_project_detail / _update_support_project / _create_project_mcp_server）+ 6 MCP CRUD helpers；endpoint 全跑 sudo + role check + savepoint 保護 D5 create |
| `Read BasicInfoSection.js + .xml` | D10 XSS surface | contenteditable + execCommand + safeHtml 三層防護；ALLOWED_ATTR `target` 無 rel 強制（caveat C-3） |
| `Read html_sanitize.js` | 前端 sanitize 機制 | DOMPurify + 白名單 14 tag / 4 attr；fallback 為 text escape；無 tabnabbing hook |
| `Agent security-auditor` (background) | 獨立 audit 二次驗證 | **完成**。獨立確認 B-1 blocker（同 verdict BLOCKED）。新增 4 finding（OR-7 brute-force F3 → C-3b、TOCTOU create F4 → C-2b、ROLE_USER dict F6 → C-9、F1 M2O spillover），已合入本 report。降級 C-8 api_key 為 info-only |

---

**Sub-agent 補錄**（security-auditor，獨立 audit 完成）：

Sub-agent **獨立確認 B-1 blocker（F1）** — verdict 與 caqa 主審一致為 **BLOCKED**。透過 `<function>` ref list + Odoo std `project_security.xml:72-80` + migration `18.0.1.0.23/post-migrate.py:34-35` + ir_rules.xml:101-107 comment 的完整 chain 獨立追證，與 caqa 主審結論相同。

**Sub-agent 新增 / 升級 4 條 finding**（已合入本 report）：

| Sub-agent ID | Finding | 落點 | 動作 |
|---|---|---|---|
| F3 | OR-7 unbound brute-force surface（不只 documented carve-out，而是 medium 攻擊面）| Security § + caveat C-3b | 升級為獨立 medium caveat，建議收緊 carve-out |
| F4 | TOCTOU race in `_create_project_mcp_server` — `_resolve_project_access` 與 savepoint INSERT 間 concurrent rebind | Security § + caveat C-2b | 新增 medium caveat，C-2 family 的 create direction 對等 |
| F6 | ROLE_USER permission dict 與 controller 不一致 — `get_role_permissions(ROLE_USER).can_edit = True` 但 controller 拒絕 | Security § + caveat C-9 | 新增 low caveat (code smell) |
| F2 sub-finding | F1 spillover：B-1 leak 對 M2O / related field 的 ORM walk 同樣 affected | Security § + Caveat 表 F1-spillover | 提示 F1 fix 後對等性 check 範圍 |

**Sub-agent 確認與 caqa 主審一致 4 條**：

- F1 = B-1 ✓
- F2 = "missing project.project regression test" — caqa 已在 B-1-test 列出
- F5 = caqa C-3（tabnabbing），同 finding
- F7 = caqa F7 info（/tmp/implementation-report-issue-48.md §Spec ambiguity 段 stale；project_project.py 已 revert）— Reporter phase 須清理 implementation report 或開 ticket

**Sub-agent 降級的 caqa caveat**：

- C-8（api_key sudo bypass）— sub-agent F8 評估 by-design（api_key write-only 不回吐），降為 info-only。從 caveat 表移除。

---

**END CAQA REPORT**
