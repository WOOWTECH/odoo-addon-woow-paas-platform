# Feature Plan: Project Detail Page (UI for MCP / Cloud Service / Basic Settings)

**Date**: 2026-05-19
**Status**: design — awaiting user sign-off
**Issue**: [#48 UI: 新增 Project Detail 頁](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/48)
**Predecessor**: PR #47 (`feature/project-mcp-skill` merged @ ebe8d67) — schema-level decouple of MCP from CloudService
**Branch**: `feature/project-setting`
**Components**: `project.project`, `woow_paas_platform.mcp_server`, `woow_paas_platform.cloud_service`, `controllers/paas.py` (support endpoints), `static/src/paas/pages/` (new project-detail page + service McpServersTab redirect), `static/src/paas/services/support_service.js`, `static/src/paas/core/router.js`

> **Superseded（partial）2026-06-05**：D10 / AC#1-3 / #14 / #16 的前台編輯行為改由 `woow_paas_platform.portal_project_settings_editable` flag gate，預設 off（唯讀）。flag on 時本設計行為完整保留。見 `docs/superpowers/specs/2026-06-05-skill-binding-ux-design.md`。

## Context

PR #47 完成 schema 解耦：`mcp.server` 從 `cloud_service_id` 改掛 `project_id`，`CloudService` 成為 `project.project` 的 optional binding（unbound / bound to 1 cloud service，1:1 with UNIQUE constraint）。AI chat 消費鏈也已遷移走 `task → project → mcp_server_ids`。Staging（stg-20260519.0115）已驗證 schema 正確。

但 `/woow` 前端 UI 沒有同步：

- 既有 `pages/support-projects/SupportProjectsPage` 是 **列表頁**（卡片顯示 Unbound / cloud `<service-name>` 標籤）— 但卡片主點擊現在會進 `pages/project-kanban/ProjectKanbanPage`（task 看板），**沒有 setting 入口**。
- 既有 MCP 管理 UI 在 `pages/service/tabs/McpServersTab`（CloudService detail 頁的一個 tab），內部 query 已被 PR #47 改成 `service.project_ids[:1]` 反查 project（D6 註解保留），但 URL 路徑仍 service-centric，**入口語意錯位**。
- 「project 是 MCP / Cloud Service / 基本資訊的擁有者」這個心智模型在 UI 上沒有對應的單一入口。

本 plan 補上 UI 缺口：新增 `/woow#/projects/:id` Project Detail 頁，整合三個 section（MCP / Cloud Service 綁定 / 基本設定），並把舊的 CloudService MCP tab 改成 redirect placeholder。

物理現實 invariant 沿用 PR #47：

- D4: `scope='user' iff project_id IS NOT NULL`
- D5: user-scope MCP 必須掛 bound project（`project.cloud_service_id IS NOT NULL`），rebind 與 unbind 操作必須通過此檢查

## Decisions

D1 / D2 / D3 / D4 / D5 由 leader 拍板（2026-05-19 brainstorm）；D6 / D7 / D8 由 user 拍板（2026-05-19 OQ resolution）；D9 由 user 拍板（2026-05-20 tester TDD red phase surface 後決定 scope creep 收乾）；D10 由 user 拍板（2026-05-20 implementation report 後 reject 改 std 欄位型態的做法）。

- **D1 Layout = 單頁 3 卡片**（非 3-tab）— Project Detail 內容比 ServiceDetail 少（沒 helm values、addons、versioning），三段（Basic / Cloud Service / MCP）一頁可控；減少 tab 路由樣板與 deep-link 維護成本。URL 只一個：`/woow#/projects/:id`。
- **D2 舊 McpServersTab = redirect placeholder**（非純移除、非雙寫）— 當 service 已綁 project 時，tab 內容換成「MCP servers 現由 Project Detail 集中管理（PR #47 後 MCP 改掛 `project_id`，project 是 single source of truth）→ 前往」CTA，點擊 `router.navigate('projects/' + projectId)`；service 未綁 project 時，顯示「此 service 暫無 bound project，請從 Projects 列表建立或綁定 project 後再從 Project Detail 管理 MCP」說明。**不暗示 user-scope MCP 與 `cloud_service` 有 ownership 關係**（PR #47 已解耦 — MCP 掛 project，不掛 service）；目前的 sidecar auto-create 機制是物理 backing 細節，由 OR-5 follow-up issue 處理。保留使用者從舊入口的探路 affordance。
- **D3 Project list 卡片導航 = 主點擊保留進 Kanban + 加 `⚙ 設定` icon 進 Detail** — 不改既有「卡片 = 看 task」直覺，最低侵入；Detail 入口透過明確的 settings icon 暴露（icon name: `settings`，與 sidebar 的 Settings icon 對齊）。Cross-link：Project Detail 頁右上加「View Tasks」按鈕跳回 Kanban。
- **D4 API = 新增 project-centric endpoint，service-centric MCP endpoint 暫保留但前端不再呼叫** — 新 `POST /api/support-projects/<int:project_id>`（actions: `get`, `update`）+ `POST /api/support-projects/<int:project_id>/mcp-servers`（actions: `list`, `create`, `update`, `delete`, `sync`, `test`）。舊 service-centric MCP endpoint 短期保留為 dead code（後續另開 deprecation issue 收尾，避免本次 scope 擴張）。URL 採 hyphenated `support-projects` resource name（與既有 `cloud-services` pattern 一致），**不沿用**既有 `support/projects/<wid>` slash pattern；理由是 `support/projects/<wid>`（list / create 在 workspace 下）的 `wid` 與 detail 的 `pid` 會 routing 衝突，需要 path segment 區隔。Future issue（見 OR-6）評估全 repo URL 重構到 cloud_service nesting 結構（`/api/workspaces/<wid>/projects/<pid>/...`）以收乾兩 pattern 並存的 inconsistency。
- **D5 ACL gate = reuse `workspace.check_user_access` 結果做 RBAC** — 不重做 ir.rule（PR #47 已收乾 mcp_server / mcp_tool 6-rule split）；本次 endpoint 內 if-check `access.role in [ROLE_OWNER, ROLE_ADMIN]` for write/delete，member 等級可讀。
- **D6 Status 子段 = 不實作（issue 字面誤會澄清）**（OQ-1 拍板 2026-05-19 + 後續 2026-05-19 user 二度澄清）— Odoo 18 std `project.project`（`addons/project/models/project_project.py`）**沒有 `status` 單欄位**。與 status 概念相關的 std 欄位有 3 個，但**沒有單一欄位直接對應** issue #48 寫的「Active / Completed / 其他既有狀態」：
  - `stage_id`（M2O to `project.project.stage`）：seed 4 個 stage（To Do / In Progress / Done / Cancelled，後兩個 folded）— 定義在 `addons/project/data/project_data.xml`
  - `last_update_status`（Selection 6 值）：on_track / at_risk / off_track / on_hold / to_define / done — 是「最近一次 status update」紀錄欄位，配 `project.update` model 使用
  - `active`（Boolean）：archive flag，True=active / False=archived（**不是** "Completed"）

  因此 issue #48「### C. 基本設定」中的「Status（Active / Completed / 其他既有狀態）」**為字面誤會**（issue 寫的選項不存在於 std `project.project`）。本 PR Project Detail 頁 BasicInfoSection 維持只實作 name / description / workspace（read-only）顯示與編輯，**不實作 status 子段**。Backend 對 `status` payload **不主動防禦**（前端 D6 不送 status key；backend 即使收到 `status` 也不處理 — 無 reject、無 silent write）。

  若後續要 UI 顯示 / 編輯 project 狀態，建議走 `stage_id`（4 個 seed stage 語意最接近 issue 描述），可另開 issue 評估 — **此非「字面誤會的 follow-up」而是「新增功能的決策 issue」**，與本 PR 無依賴關係。
- **D7 rebind cloud service = confirm modal**（OQ-2 拍板 2026-05-19）— CloudServiceBindingSection 選 dropdown 不立即 commit，先彈出 confirm modal：「Rebind this project to <new service>?」或「Unbind this project from <current service>?」訊息，含 D5 invariant 警示（unbind 時若 project 仍有 user-scope MCP 會被 backend 阻擋）。Modal 含 Confirm / Cancel，Cancel 還原 dropdown selection。Reuse 既有 `components/modal` Modal 元件。
- **D8 CloudService dropdown filter = 排除 deleted / error / deleting state**（OQ-3 拍板 2026-05-19）— CloudServiceBindingSection 透過 `cloudService.fetchServices(workspaceId)` 拿列表後，前端 filter `service.state ∈ {error, deleted, deleting}` 的項目（具體 state 字串由 implementation 階段對齊既有 `cloud_service.state` enum）。被 filter 的 service 不出現在 dropdown；只列可健康綁定的目標。「Unbound」選項永遠出現在 dropdown 第一位（讓 unbind 動作走相同 UI 路徑）。
- **D9 B1 fresh-install fix scope creep**（user 拍板擴 scope 2026-05-20；CAQA B-1 blocker 補正 2026-05-20）— Tester TDD red phase 發現既有 B1 task ACL test (`test_alice_cannot_read_bob_task_via_orm`) 在 fresh install 紅；root cause 是 B1 fix 邏輯**只放在 migration**（`src/migrations/18.0.1.0.23/post-migrate.py` 開頭 `if not version: return` — 只在 upgrade 跑），fresh install 不會跑該 migration。本 PR 順便修：把 std **project + task rule deactivation**（**3 條** rule）搬到 `src/security/ir_rules.xml` declaration data（fresh install + upgrade 兩條路徑都 cover）；migration 既有邏輯**保留向後相容**但變成幾乎 noop（declaration 已 deactivate，migration 再跑也 idempotent）。三條 std rule：
  - `project.project_public_members_rule` — 套 `base.group_user`、目標 model `project.project`；含 always-true leg `privacy_visibility != 'followers'`（對 default `employees` 永真，是 [[cross-workspace-task-data-leak]] B1 pitfall pattern）
  - `project.task_visibility_rule`
  - `project.task_visibility_rule_project_user`

  **3 條對齊 `migrations/18.0.1.0.23/post-migrate.py:101` 既有邏輯**；declaration data 與 migration 必須 **1:1 對稱**（spec patch 已驗證），否則 fresh-install 路徑會復活 [[cross-workspace-task-data-leak]] pitfall pattern（CAQA B-1 blocker 原因即是 spec 漏寫 project rule、migration 拔 3 條但 declaration 只拔 2 條 → fresh install 上 project rule 仍 active → cross-workspace `project.project` ORM leak）。Scope 擴張屬「相鄰問題順手收乾」— B1 leak 在 fresh install 復活與本 PR「project 相關 ACL」主題密切相關，不擴反而會留 false-fixed 假象，未來 fresh-install 部署直接受影響。AC #29 對應本變動；OR-9 記錄 declaration / migration 對稱 hardening 給 reporter。
- **D10 Description 子段 = 用 std HTML field + rich-text editor**（user 拍板 2026-05-20 implementation report 後）— Odoo std `project.project.description` 為 `fields.Html(sanitize=True)`。Implementation 階段一度把欄位 override 為 `fields.Text` 讓 plain-text assertion 過 — user reject，理由是「能用原生最好，要擴充再 inherit 加新欄位」。本 PR 採 std HTML：BasicInfoSection 改用 **rich-text editor**（OWL `HtmlField` 或 contenteditable + sanitize，對齊既有 `TaskDetailPage` `safeHtml` idiom），endpoint 回傳 / 接受 `description` 為 HTML 字串。Backend **不做** strip / wrap hack — 讓 std `fields.Html` sanitize 自然發揮。既有 task description 也是 `fields.Html`（read-only 顯示 with `safeHtml`），本 PR 對 project description 加 inline edit 介面 → 跟 task pattern 一致 read 路徑、額外加 write 路徑。**未來 task inline edit description 若有需求**，可採同 pattern；本 PR 不 cover（見 Out of scope）。

## Rejected options

| 選項 | 為什麼拒絕 |
|---|---|
| D1=B 3-tab 對稱 ServiceDetail | tab 路由樣板（router pattern + tab param parsing + xml tab nav）成本高；Project 段落少不必切；deep-link 收益對「先看再改」的 Project 場景不大 |
| D2=A 純移除 McpServersTab | 使用者從 CloudService 找 MCP 入口會迷路；service 頁本來就是「看部署/狀態」的合理位置，留個 redirect 簡介比沒入口好 |
| D2=C 雙寫（兩邊都可改） | 違反 single source of truth；rebind / unbind project 時要同步兩 UI 狀態，bug 風險高；違反 PR #47 的解耦本意 |
| D3=B 卡片主點擊改進 Detail，Kanban 走 cross-link | 改變既有「卡片進 Kanban」直覺；本 issue 不是改 list 頁主流程的時機 |
| D4=B 直接擴 service-centric endpoint（不新增 project endpoint） | URL 語意持續錯位；前端要繼續 walk service→project，反向 query 在 unbound project 無法成立（unbound 沒 service 可掛） |
| D4=C 廢除 service-centric MCP endpoint（這次就刪） | 擴 scope（要清 deprecation cycle、call site 確認、可能影響 API consumer）；本 issue 只動 UI |
| D5=B 新增 project-level ir.rule（rebind 走 rule 拒絕） | PR #47 剛收乾 ACL，再動 rule 風險高；rebind 是 write 動作，controller-level check 就足夠 |
| D6 各替代方案（前 D6=A 接 `last_update_status` / D6=B derived from `task_count` read-only / D6=D 自製 selection 欄位） | 三個方案的前提都是「issue 確實要做 status」；user 二度確認 Odoo std `project.project` 無 status 單欄位後拍板 issue 字面是誤會（見 D6 段詳列 `stage_id` / `last_update_status` / `active` 三 std 欄位與其語意），三方案均不適用本 PR。若後續產品團隊有 stage_id-based UI 的需求，由另開 issue 處理（與本 PR 無依賴關係） |
| D7=B Dropdown 變動即 commit（無 confirm modal） | rebind / unbind 屬不可逆動作（unbind 後 user-scope MCP 會被 D5 擋；rebind 後 MCP URL 重算）；意外點擊代價高 |
| D8=B Dropdown 列所有 services（含 deleted/error） | 使用者誤選 unhealthy service → backend 接受但 deploy 鏈會失敗；UX 不應暴露無法達成的選項 |
| D10=A implementation override `description = fields.Text`（讓 plain-text assertion 過） | 改 std 欄位型態屬 invasive 修改：(a) Odoo std project form view 仍 expect HTML widget，可能 break；(b) 既有 row 含 HTML 會直接顯示 raw tags；(c) Odoo 升級時 std 改 Html 又要 maintain override；(d) 其他 module 依賴 `description` 為 Html 也可能 break；(e) 違反 user 原則「能用原生最好，要擴充再 inherit 加新欄位」 |
| D10=B 加 `paas_description` 新欄位（與 std description 並存） | Schema +1 column；雖無破壞但 PaaS-specific 概念（為什麼這個 module 要自己一個 description？）需先決定 design，本 PR 不該扛此設計題；rich-text editor 跟既有 `TaskDetailPage` description idiom 一致更乾淨，無需另開 column |
| D10=C endpoint 層 strip / wrap HTML（讓 plain-text test 過但 schema 不動） | 違反 single source of truth — endpoint 與 std form view 看到的 description 形式不同；可能 break std backend view；維護成本高（每次 std 升級都要 review strip 邏輯是否還合用） |

## Acceptance criteria

每條對應一個可被 test 抓住的行為。

### Backend endpoints

1. `POST /api/support-projects/<int:project_id>` `action=get` 對有 access 的 user 回 `{success: True, data: {id, name, description, cloud_service_id, cloud_service_name, workspace_id, workspace_name, mcp_server_ids: [{id, name, url, transport, state, ...}], task_count, open_task_count, created_date}}`。`description` 為 **HTML 字串**（std `fields.Html` sanitized output，例如 `<p>...</p>` wrapped — D10）。**不含 status 欄位**（D6：本 PR 不實作 status；前端若需 Active/Completed 顯示，list 頁繼續用 `_getProjectStatus` 從 `task_count` derive，detail 頁不顯示 status）。
2. `POST /api/support-projects/<int:project_id>` `action=update` 接受 `{name?, description?, cloud_service_id?}`；`description` 為 **HTML 字串**（frontend rich-text editor 送過來，std `fields.Html` 自動 sanitize — D10；backend 不做 strip / wrap hack）。只允許 workspace `owner`/`admin` 寫入，回 updated project data。（D6 carve-out 已在 Decisions 段說明 backend 不主動 reject `status`；前端 D6 保證不送 status key，spec 此處不重複該防禦性需求。）
3. `action=update` 對 `cloud_service_id` 變更（rebind / unbind）執行：
   - 變更前驗證新 cloud_service 屬於相同 workspace（隔離不變式）
   - **Unbind**（`cloud_service_id=False`）時，endpoint **手動** query `project.mcp_server_ids.filtered(lambda m: m.scope == 'user')`；若非空，endpoint 自行 `raise ValidationError` with D5 violation message（內容對齊 `mcp_server.py:_check_user_scope_requires_bound_project` 的訊息措辭），回 `{success: False, error: <D5 violation message>}`。**不依賴** `@api.constrains` auto-trigger — `_check_user_scope_requires_bound_project` 是 `@api.constrains('scope', 'project_id')` on `mcp_server`，改 `project.cloud_service_id` **不會** 觸發該 callback（見 OR-4，未來拆 D5 時一併回收此手動 query 邏輯）
   - rebind 不改既有 MCP 的 URL（URL 由 MCP 自己 compute；只變 owning service）
4. `POST /api/support-projects/<int:project_id>/mcp-servers` `action=list` 回該 project 的 user-scope MCP 列表（filter `scope='user'`），sub-fields 同既有 `_format_mcp_server`。
5. 同 endpoint 的 `create` / `update` / `delete` / `sync` / `test` 行為與既有 service-centric `_create_mcp_server` 等對齊，差異只在 owning 物件是 `project` 而非 `service`（即 `project_id` 從 path 取代 `service.project_ids[:1]`）。
6. ACL：endpoint 對非 workspace member 與**不存在 project id** **統一回** `{success: False, error: 'Project not found or access denied'}`（同訊息防 id enumeration — 與 AC #11 對齊，不洩漏存在性）；對 member but non-admin 的 write 動作回 `{success: False, error: 'Permission denied'}`。
7. 既有 service-centric MCP endpoint（`/api/workspaces/<wid>/services/<sid>/mcp-servers`）行為**不變**；前端不再呼叫但 backend 仍接受（後續另開 issue deprecate）。

### Frontend — routing

8. `router.js` 新增 pattern `/^projects\/(\d+)$/` → routeName `project-detail`，`this.params.id` = project id。
9. `root.js` / `root.xml` 把 `project-detail` 路由綁到新元件 `ProjectDetailPage`，props `{ projectId: Number }`。
10. 未綁定 project 的 service detail 頁，點 mcp-servers tab → 進入 redirect placeholder。
11. 直接 navigate to `/projects/<不存在 id>` → endpoint 回 `{success: False, error: 'Project not found or access denied'}`（**與 AC #6 non-member case 統一同訊息**防 id enumeration）；ProjectDetailPage 收到該 error 後顯示 NotFoundPage 風格 error state，不 silent fallback、不分流區隔「不存在」與「無權限」case（避免攻擊者 brute force `id ∈ [1, N]` 拿到差異）。

### Frontend — Project Detail page

12. `pages/project-detail/ProjectDetailPage.{js,xml,scss}` 元件 mount 後呼叫 `supportService.fetchProjectDetail(projectId)` 載入。
13. 頁面 layout：上方 header（project name + back button + View Tasks CTA），下方三張 `WoowCard`：BasicInfoSection、CloudServiceBindingSection、McpServersSection。
14. **BasicInfoSection** 顯示 name（inline edit text input + save button）/ description（**rich-text editor** + save button — 用 OWL `HtmlField` 或 contenteditable + sanitize，對齊既有 `TaskDetailPage` `safeHtml` idiom；**不**改 std `project.project.description` 為 `fields.Text`，見 D10）/ workspace（read-only 顯示）。Save 後呼叫 `supportService.updateProject(id, payload)` 並樂觀更新 local state（description 送 / 收皆為 HTML 字串）。**不顯示 status 子段**（D6）。
15. **CloudServiceBindingSection** 顯示目前綁定（badge：cloud icon + service name，或 cloud_off + "Unbound"）。下拉選單組成：(a) "Unbound" 永遠在第一位、(b) 同 workspace 內所有 cloud services 中 `state ∉ {error, deleted, deleting}` 的項目（D8 filter，透過 `cloudService.fetchServices(workspaceId)` 後前端過濾）。Dropdown 變動時 **不立即 commit**，彈出 confirm modal（D7），訊息依動作而異：rebind「Rebind this project from <current> to <new>?」/ bind from unbound「Bind this project to <new>?」/ unbind「Unbind this project from <current>? This will block if user-scope MCP servers exist.」。Modal 有 Confirm / Cancel；Cancel 把 dropdown selection 還原成 current binding；Confirm 後呼叫 update endpoint。Unbind 若 backend 回 D5 violation error，顯示 toast：「This project has user-scope MCP servers. Remove them first.」並把 dropdown selection 還原。
16. **McpServersSection** fork 自 `McpServersTab`，內部邏輯一致（add / edit / delete / sync / test form），差異：
    - props 從 `{service, workspaceId}` 改 `{projectId}`
    - service 層 method 改 project-centric（見 D17）
    - 若 project unbound（`cloud_service_id == null`），顯示「Bind a Cloud Service first to manage user-scope MCP」說明 + disable Add button（D5 invariant 前端 echo）

### Frontend — service layer

17. `services/support_service.js` 新增：
    - `fetchProjectDetail(projectId)` → `Promise<{success, data?, error?}>`，呼叫 `POST /api/support-projects/<id>` `action=get`
    - `updateProject(projectId, payload)` → 同 endpoint `action=update`，成功時本地同步更新 `this.projects[idx]`（讓 list 頁的 binding badge 即時反映）
    - `fetchProjectMcpServers(projectId)`
    - `createProjectMcpServer(projectId, payload)`
    - `updateProjectMcpServer(projectId, serverId, payload)`
    - `deleteProjectMcpServer(projectId, serverId)`
    - `syncProjectMcpServer(projectId, serverId)`
    - `testProjectMcpServer(projectId, serverId)`
18. 舊 `cloudService.fetchMcpServers` 等 method 保留但加 deprecation JSDoc 標記；本 PR 不刪。

### Frontend — list page integration

19. `pages/support-projects/SupportProjectsPage` 的卡片新增 `⚙` settings icon（右上角，hover 顯示），點擊呼叫新方法 `navigateToProjectSettings(id)` → `router.navigate('projects/' + id)`。
20. 卡片主點擊 navigation **不變**（仍進 Kanban）。
21. 從 Project Detail 回 list 頁時，binding badge（Unbound / cloud `<name>`）必須反映剛剛的 rebind / unbind（D17 樂觀更新 local state 即可達成；不需強制 reload）。

### Service Detail MCP tab redirect

22. `pages/service/tabs/McpServersTab` 簡化為 placeholder：
    - 若 service 有 bound project（`service.project_ids[:1]` 非空），顯示「MCP servers are now managed in Project Detail. [Go to Project Settings →]」CTA → `router.navigate('projects/' + projectId)`
    - 若無 bound project，顯示「This service has no bound project. Create or bind a project from the Projects list, then manage MCP servers from Project Detail.」說明（與 D2 對齊 — 不暗示「user-scope MCP 綁 cloud service」；ownership 在 project，sidecar auto-create 是物理細節，見 OR-5）
23. 既有 McpServersTab 的 add / edit form 等 UI 全部刪除（避免雙寫面板再被誤用）。
24. ServiceDetailPage 的 tab nav 仍保留 "MCP Servers" 標籤，不改 URL pattern（`/workspace/:id/services/:serviceId/mcp-servers`），避免 bookmark 斷裂。

### Testing

25. Backend pytest：
    - `tests/test_project_detail_endpoints.py`：cover endpoint actions（get / update name / update description / rebind / unbind 含 D5 violation — **透過 endpoint 內手動 query 觸發**，非 ORM `@api.constrains` auto-trigger，見 AC #3）+ ACL（non-member / member read-only / admin write，含 AC #6/#11 統一 error message 驗證 — non-member 與不存在 id 都回 `Project not found or access denied`）
    - `tests/test_project_mcp_project_centric.py`：cover project-centric MCP CRUD + scope/project_id constraint + unbound project create MCP 應 D5 violation（此 case 由 `mcp_server` 既有 `@api.constrains` 觸發，**不依賴** endpoint 手動 query）
26. Frontend：repo 無 OWL unit test infra → 不寫 frontend unit test；以 staging E2E smoke 為主（issue acceptance #6）。
27. 既有 `tests/test_mcp_*.py` 全部須繼續 pass（regression check）。
28. Staging smoke 步驟（issue acceptance「至少跑過一輪 smoke」）：
    1. 建立 project（透過 list 頁 + Create modal）
    2. 進 Project Detail（從 list 點 ⚙ icon）
    3. 綁 Cloud Service：dropdown 選 service → confirm modal「Bind this project to <name>?」→ Confirm → badge 變 cloud icon
    4. 加 MCP server（McpServersSection add form）
    5. Edit project name（BasicInfoSection inline edit）→ save 後 reload 仍生效
    6. 嘗試 Unbind Cloud Service：dropdown 選 Unbound → confirm modal → Confirm → 預期 backend 回 D5 violation error → 前端 toast「This project has user-scope MCP servers. Remove them first.」、dropdown selection 還原
    7. 刪 MCP server
    8. 再 Unbind Cloud Service：dropdown 選 Unbound → confirm modal → Confirm → 預期成功，badge 變 cloud_off + "Unbound"
    9. 回 list 頁 → 確認 badge 變 "Unbound"
    10. 進舊 ServiceDetail 的 MCP tab → 確認 redirect placeholder（service 已 unbind project，應顯示「無 bound project」說明變體）
    11. 把另一個 service 重新綁回 project：dropdown 選 service → confirm modal「Rebind」用詞或「Bind from unbound」用詞→ Confirm → 預期成功

29. **B1 fresh-install fix**（D9）：移植 `src/migrations/18.0.1.0.23/post-migrate.py` 內的 std **project + task rule deactivation** 邏輯到 `src/security/ir_rules.xml`（declaration data — `noupdate=0` 區塊，讓 module update 也能 re-apply）。具體：對 **3 條** std rule 加 `active=False` 邏輯，透過 `<function model="ir.rule" name="write">` ORM write（**bypass** `ir_model_data noupdate` flag — `<record>` 寫 `active=False` 在 std rule 上會被 noupdate 機制拒絕）：
    - `project.project_public_members_rule`
    - `project.task_visibility_rule`
    - `project.task_visibility_rule_project_user`

    Migration 既有邏輯**保留**（向後相容，但變成幾乎 noop — declaration 已 deactivate，migration 再跑也 idempotent，不會出 race / double-deactivate 錯誤）。**Acceptance**：
    - (a) 既有 `test_alice_cannot_read_bob_task_via_orm` 必須在 **fresh install**（worktree docker compose 新建 DB 跑 `-i woow_paas_platform`）與 **upgrade**（既有 18.0.1.0.23 sandbox 升到本 PR 版跑 `-u woow_paas_platform`）兩種路徑都綠
    - (b) **新增** `test_alice_cannot_read_bob_project_via_orm` regression test in `src/tests/test_cross_workspace_task_isolation.py`（mirror 既有 task 版本但對 `project.project` 模型 — Alice / Bob 雙 workspace fixture，Alice with_user `search([('id', '=', bob_project.id)])` → 空 recordset，`browse(bob_project.id).read()` → raise AccessError）；同樣須在 fresh install + upgrade 兩 path 都綠

    caqa phase 須在報告中列出兩 path × 兩 test 共 4 個執行結果。

## Out of scope

- 舊 service-centric MCP endpoint 的最終 deprecation cycle（另開 issue：scan call site、加 deprecation header、移除）
- Project member 管理 / 多人協作 UI（project 的 ACL 仍透過 workspace.check_user_access 推導，不在本次 scope）
- Project status 子段（D6 — issue 字面誤會澄清，**非延後決策**；Odoo std `project.project` 無 status 單欄位，若後續要做 stage_id-based UI 由另開 issue 處理，與本 PR 無依賴）
- Frontend unit test infra 引入（spec 已標註以 staging E2E 為主）
- Project Kanban 頁的 MCP / Cloud Service section 整合（Kanban 仍只看 task）
- 未來 `project.task` description 的 inline edit 介面（D10 pattern 可複用：std `fields.Html` + rich-text editor + `safeHtml`；本 PR 只 cover project description 的 inline edit，task 維持既有 read-only 顯示）
- Unbound project ACL 行為決策（OR-7 deferred — 預設與既有 `ai_assistant.py:_check_project_access` 一致，**不寫 test cover unbound**、**不修既有 leak**；另開 follow-up issue 評估三方案：reject all / creator-only / message_partner_ids walk 跟 `_PROJECT_RULE_DOMAIN` 對齊）

## Open questions / risks

### Resolved (2026-05-19)

| ID | Question | 拍板結論 |
|---|---|---|
| OQ-1 | Status 欄位要接 Odoo std 哪個欄位？ | **本 PR 不實作 status 子段**（D6）。Resolution（**2026-05-19 user 二度澄清後最終結論**）：Issue acceptance「Status（Active / Completed / 其他既有狀態）」**為字面誤會** — Odoo std `project.project` 無 status 單欄位（D6 段詳列 `stage_id` / `last_update_status` / `active` 三 std 欄位與其語意）。**不另建 schema-decision follow-up issue**；reporter 須在 issue #48 留 comment 澄清誤會（見 OR-3）。若後續要 UI 顯示 / 編輯 project 狀態，建議走 `stage_id`，由產品團隊另開「新功能」issue 決定，與本 PR 無依賴關係。PR 描述須明示此 carve-out。 |
| OQ-2 | rebind 是否要 confirm modal？ | **要**（D7）。Dropdown 變動 → confirm modal → Confirm 才 commit；Cancel 還原 selection。 |
| OQ-3 | CloudService dropdown 是否 filter unhealthy state？ | **要**（D8）。前端 filter `state ∈ {error, deleted, deleting}` 的 services；只顯示可健康綁定的目標。 |

### Open risks

| ID | Risk | 對應 owner |
|---|---|---|
| OR-1 | 若舊 service-centric MCP endpoint 仍被外部呼叫（API consumer / scripts）→ deprecation issue 須先盤點 call site | 未來 issue owner |
| OR-2 | rebind 操作可能 race condition：A 正在 rebind project to service X，B 同時把 X delete → 後端應 lock-and-validate；本 spec 假設 endpoint 內 transaction 足夠，但 caqa 階段需 review | caqa |
| OR-3 | D6 carve-out 須在 issue #48 留 comment **澄清 status 子段為字面誤會**（Odoo 18 std `project.project` 無 status 單欄位 — D6 段已詳列 `stage_id` / `last_update_status` / `active` 三個相關 std 欄位與其語意）；**不另建 schema-decision follow-up issue**（前提變了 — issue 字面已澄清為誤會，非「延後決策」）。若後續產品團隊有新增 `stage_id`-based UI 的需求，由產品團隊另開 issue 決定；reporter phase 須驗證 issue #48 已留 comment | reporter |
| OR-4 | 未來若拆 D5 invariant（移除 `mcp_server.py:_check_user_scope_requires_bound_project` constraint + 重新設計 user-scope MCP URL 機制），本 PR 的 endpoint 手動 query 邏輯（AC #3 unbind 防護）一起回收；reporter phase 須在 [[follow-ups]] 記錄此 cleanup debt | future issue owner |
| OR-5 | 本 PR **不處理** sidecar MCP auto-create 機制（`controllers/paas.py` / `models/cloud_service.py` 的 `_auto_create_mcp_server` + `_model_auto_create_mcp_server`，6 個 call site；`cloud_app_template.py` 7 個 `mcp_*` fields；Helm chart sidecar 定義）。當前 user-scope MCP 在 `cloud_service` deploy 時自動建立、物理 backing 來自 sidecar — 這是 PR #47 後**唯一**仍把「user-scope MCP 與 cloud_service」綁在物理層的耦合面。Auto-create 機制與 D5 invariant 應在 follow-up issue 一起重新審視（是否拆 sidecar / 是否拆 D5 / user-scope MCP URL 機制重新設計）。D2 / AC #22 措辭已中性化避免暗示此耦合，但實作層仍存在。reporter phase 須在 [[follow-ups]] 記錄此 architectural debt 並引用 follow-up issue link（與 OR-4 同一 future issue 可合併處理） | future issue owner / reporter |
| OR-6 | URL pattern 採 hyphenated `/api/support-projects/<pid>` 與既有 `/api/support/projects/<wid>`（list / create 在 workspace 下）**兩 pattern 並存**。本 PR 因 `wid` vs `pid` routing 衝突無法直接沿用既有 slash pattern（見 D4 末段），故選 hyphenated 與 `cloud-services` 對齊。Future issue 評估全 repo URL pattern 統一到 **cloud_service nesting**（`/api/workspaces/<wid>/projects/<pid>/...`），收乾兩 pattern 並存的 inconsistency。本 PR 不重構（避免擴 scope）；reporter phase 須在 [[follow-ups]] 記錄此 URL pattern debt（可與 OR-1 service-centric MCP endpoint deprecation 合併同一 follow-up issue 處理） | future issue owner / reporter |
| OR-7 | Unbound project（`cloud_service_id IS NULL` → `workspace_id` computed NULL）的 ACL 行為 **spec gap** — 本 PR endpoint 預設與既有 `ai_assistant.py:_check_project_access` 一致（unbound project 對 internal user **global access**），**不寫 test cover unbound case**、**不修既有 leak**。Tester TDD red phase surface 三 options：(a) reject all unbound access、(b) creator (`create_uid`) only、(c) walk `message_partner_ids` 跟 `_PROJECT_RULE_DOMAIN` 對齊。本 PR deferred（避免擴 scope，且 unbound project 在 PaaS 流程中為非常態 — 通常 project 一建立即 bind cloud_service）；另開 follow-up issue 評估三方案。User-scope MCP 在 unbound project 仍受 D5 ORM constraint 保護（spec AC #25 第二 file `test_project_mcp_project_centric.py` 已 cover：unbound project 無法 create user-scope MCP），所以 OR-7 deferred **不影響** MCP path 安全性（unbound 沒 user-scope MCP 可洩漏） | future issue owner / reporter |
| OR-8 | Worktree dev env 3 個 setup gaps（tester OQ-2 surface）— (a) `PAAS_CI_ADDONS_PATH` override 未在 `scripts/start-dev.sh` 自動設定；(b) `requirements.txt` 沒 pre-install 在 docker image，每次容器啟動才裝；(c) `src/hooks.py:7-12` 的 `_IMPORT_NAME_OVERRIDES` dict 缺 `pycryptodomex → Cryptodome` mapping（某些 addon dependency 會踩到）。tester 已 in-flight fix（不動 production code，只調 worktree script）；reporter phase 須寫到 `docs/tracking/pitfalls/` 提示後續開發者。**`hooks.py` override map 修補**（一行 dict entry，無風險）可由本 PR implementer 順手加入；其餘兩條（PAAS_CI_ADDONS_PATH override / requirements pre-install）留 worktree script / docker image 後續另議，不擴本 PR scope | reporter / implementation |
| OR-9 | Declaration data (`ir_rules.xml`) 與 migration (`post-migrate.py`) 對 std rule deactivate 必須 **1:1 對稱**。CAQA B-1 blocker root cause：spec D9 / AC #29 抽 migration 邏輯到 declaration data 時，沒對「**3 條 lockstep**」做 mental check，漏寫 `project.project_public_members_rule` → migration 拔 3 條但 declaration 只拔 2 條 → fresh install 上 project rule 仍 active → cross-workspace `project.project` ORM leak（[[cross-workspace-task-data-leak]] pitfall pattern fresh-install 路徑復活）。本 PR 已 fix（spec + xml + test 同步補）。Reporter phase 須在 `docs/tracking/pitfalls/cross-workspace-task-data-leak.md` 加 hardening 段：「未來把 migration 邏輯搬到 declaration data 時，先 inventory migration 內所有 std rule write 並 cross-check declaration list；single UPDATE + IN clause 在 declaration form 等同 single `<function>` + ref list — 兩邊 list 必須同步」 | reporter |

## Components (file map)

### New files

- `src/static/src/paas/pages/project-detail/ProjectDetailPage.js`
- `src/static/src/paas/pages/project-detail/ProjectDetailPage.xml`
- `src/static/src/paas/pages/project-detail/ProjectDetailPage.scss`
- `src/static/src/paas/pages/project-detail/sections/BasicInfoSection.js`
- `src/static/src/paas/pages/project-detail/sections/BasicInfoSection.xml`
- `src/static/src/paas/pages/project-detail/sections/CloudServiceBindingSection.js`
- `src/static/src/paas/pages/project-detail/sections/CloudServiceBindingSection.xml`
- `src/static/src/paas/pages/project-detail/sections/McpServersSection.js`
- `src/static/src/paas/pages/project-detail/sections/McpServersSection.xml`
- `src/static/src/paas/styles/pages/_project_detail.scss`
- `src/tests/test_project_detail_endpoints.py`
- `src/tests/test_project_mcp_project_centric.py`

### Modified files

- `src/controllers/paas.py` — 新增 endpoint handler（D4）
- `src/security/ir_rules.xml` — 加 std **project + task rule** deactivation declaration data（B1 fresh-install fix，D9 / AC #29；對 **3 條** std rule deactivate：`project.project_public_members_rule` / `project.task_visibility_rule` / `project.task_visibility_rule_project_user`，與 `migrations/18.0.1.0.23/post-migrate.py:101` 既有邏輯 **1:1 對稱**，fresh install path 也 cover）
- `src/static/src/paas/core/router.js` — 新增 pattern
- `src/static/src/paas/root.js` / `root.xml` — 路由綁元件
- `src/static/src/paas/services/support_service.js` — 新增 method（D17）
- `src/static/src/paas/pages/support-projects/SupportProjectsPage.js` / `.xml` — 卡片加 ⚙ icon（D19）
- `src/static/src/paas/pages/service/tabs/McpServersTab.js` / `.xml` / `.scss` — 改 placeholder（D22）
- `src/__manifest__.py` — 註冊新 OWL 元件到 assets bundle
- `src/__manifest__.py` 版號 bump（議題：是否 bump，若 deploy 要走 release flow 則 bump）

## Implementation phasing

預期 phase 順序（由 phase-leader 在 recruit 後啟動）：

1. **planner** — sign-off 本 design（含 OQ-1/OQ-2/OQ-3 與 user 對焦）
2. **tester (TDD red)** — 寫 backend endpoint test（acceptance #1-7）+ ACL test（#6）+ MCP project-centric test（#25 第二個 file）
3. **test-explainer** — 把 test cases 寫成 human-readable test report
4. **implementation (TDD green)** — backend endpoint → support_service.js method → ProjectDetailPage + sections → router → list 頁 icon → McpServersTab placeholder
5. **caqa** — analyze quality / security / performance / architecture，重點查 OR-2 rebind race + D5 invariant 邊界 + ACL gate
6. **reviewer** — `/pr-review-toolkit:review-pr` 對 branch 做完整 review
7. **reporter** — durable docs：把這份 design 的 final state + 任何意外 pattern 寫進 `docs/decisions/` 或 `docs/tracking/`（若有踩坑）

## References

- Predecessor refactor plan: `docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md`
- D4/D5 invariant source: `src/models/mcp_server.py:111-139` (`_check_scope_project_consistency`, `_check_user_scope_requires_bound_project`)
- Existing McpServersTab to be replaced: `src/static/src/paas/pages/service/tabs/McpServersTab.js`
- ACL split decision (referenced for context): `docs/decisions/patterns/paas-workspace-member-acl-tightening.md`
