---
status: archived
archived-from: docs/tracking/test-reports/2026-05-20-project-detail-page-issue-48-tdd-red.md
archived: 2026-07-19
archived-reason: "一次性測試報告，對應功能已上線"
---

# Issue #48 Test Coverage — Project Detail page (TDD red phase)

**Branch**: `feature/project-setting` **Base**: `main`
**Spec**: `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` @ `9033712`（後續若 amend，commit SHA 由 phase-leader 在 implementer hand-off 時更新）
**Predecessor**: PR #47（schema-level decouple of MCP from CloudService — `mcp_server` 從 `cloud_service_id` 改掛 `project_id`）
**Audience**: reviewer / stakeholder（30 秒內要能說出本 PR 守了什麼）
**狀態**: TDD red phase 完成 — **36 個新 test 全紅**（HTTP 404，endpoint not registered，符合 expected "feature missing" shape），**54 個既有 MCP test 全綠**（regression baseline OK）。

---

## Why this PR exists

PR #47 把 user-scope MCP 從 `cloud_service` 解耦到 `project.project`，但 `/woow` 前端 UI 沒同步：

- 既有 Project 列表頁卡片主點擊只進 task Kanban，**沒有 setting 入口**。
- 既有 MCP 管理 UI 仍掛在「CloudService detail 頁的一個 tab」下，**URL 與心智模型已與 schema 脫節**。
- 「project 是 MCP / Cloud Service / 基本資訊的擁有者」這個模型在 UI 上沒有對應的單一入口。

本 PR 補 UI 缺口：新增 `/woow#/projects/:id` Project Detail 頁，整合三段（Basic / Cloud Service binding / MCP servers），並把舊 CloudService MCP tab 改成 redirect placeholder。Backend 同步新增 project-centric endpoint family（`/api/support-projects/<pid>` + `.../mcp-servers`）取代既有 service-centric MCP endpoint（後者保留為 dead code，後續 issue deprecate）。

物理現實 invariants 沿用 PR #47：

- **D4**：`scope='user' iff project_id IS NOT NULL`
- **D5**：user-scope MCP 必須掛 bound project（`project.cloud_service_id IS NOT NULL`），rebind / unbind 必須通過此檢查

---

## What "red phase" means here

本 PR 採 TDD：tester 先把 spec 的 acceptance criteria 翻譯成 36 個 test 並讓它們全部紅燈，由 implementer 在 green 階段把 endpoint 寫到全綠。**紅的形狀是有意義的**：

- 每個 test 跑到 helper `_post()` 內 `assertEqual(response.status_code, 200, ...)` 就炸 `AssertionError: 404 != 200`。
- 失敗訊息明確點名「The new project-centric endpoint is likely not registered yet」，並且每個 test 的 docstring + `assertion.msg=` 串都寫明它要守的 AC 與期望 post-condition。
- 一旦 endpoint 寫好回 200 + JSON-RPC envelope，紅就會在「業務語意層」浮現（success/false、error 字串、ORM 副作用），不再是 routing 層噴 404。

簡言之：**這次 36 個紅 = "endpoint 還沒接"**；implementer 的 green 路徑很清楚，不存在「test 自己寫錯導致紅」的雜訊。

---

## 測試覆蓋總覽（spec AC → test method）

兩個 test file，36 個 test：

| AC | 行為 | Test class | Test method | File |
|---|---|---|---|---|
| **#1** | `action=get` 完整 payload shape（10 鍵） | `TestProjectDetailGet` | `test_get_returns_full_project_data_for_owner` | endpoints |
| **#1 / D6** | payload **不含** `status` 欄位（D6 carve-out） | `TestProjectDetailGet` | `test_get_does_not_include_status_field` | endpoints |
| **#1** | `mcp_server_ids` 含 user-scope MCP | `TestProjectDetailGet` | `test_get_mcp_server_ids_reflects_user_scope_mcp` | endpoints |
| **#6** | admin 可讀 | `TestProjectDetailGet` | `test_get_admin_can_read` | endpoints |
| **#6** | member（role=user）可讀 | `TestProjectDetailGet` | `test_get_member_can_read` | endpoints |
| **#6 / #11** | non-member 回統一防-enum 訊息 | `TestProjectDetailGet` | `test_get_non_member_returns_unified_error` | endpoints |
| **#11** | 不存在 id 回**同訊息**（防 enumeration） | `TestProjectDetailGet` | `test_get_nonexistent_id_returns_unified_error` | endpoints |
| **#6 / #11** | 跨 workspace 讀回同訊息 | `TestProjectDetailGet` | `test_get_cross_workspace_project_returns_unified_error` | endpoints |
| **#2** | owner 可改 name | `TestProjectDetailUpdateName` | `test_update_name_by_owner_succeeds` | endpoints |
| **#2** | admin 可改 description | `TestProjectDetailUpdateName` | `test_update_description_by_admin_succeeds` | endpoints |
| **#6** | member 寫入回 `Permission denied`（**非**防-enum 訊息） | `TestProjectDetailUpdateName` | `test_update_by_member_returns_permission_denied` | endpoints |
| **#6 / #11** | non-member 寫入回防-enum 訊息（**不**洩 existence） | `TestProjectDetailUpdateName` | `test_update_by_non_member_returns_unified_error` | endpoints |
| **#3** | 同 workspace rebind 成功 | `TestProjectDetailRebind` | `test_rebind_to_same_workspace_service_succeeds` | endpoints |
| **#3** | 跨 workspace rebind 被擋，原 binding 不變 | `TestProjectDetailRebind` | `test_rebind_to_different_workspace_service_rejected` | endpoints |
| **#3** | unbind 無 user MCP 時成功 | `TestProjectDetailRebind` | `test_unbind_without_user_mcp_succeeds` | endpoints |
| **#3 / D5** | unbind 有 user MCP 時被 **endpoint 手動 query** 擋 | `TestProjectDetailRebind` | `test_unbind_with_user_mcp_rejected_via_endpoint_manual_query` | endpoints |
| **#3** | rebind 不重算既有 MCP URL | `TestProjectDetailRebind` | `test_rebind_with_user_mcp_to_same_workspace_succeeds` | endpoints |
| **D4 / OR-6** | hyphenated URL pattern 有掛上 | `TestProjectDetailUrlPattern` | `test_hyphenated_url_pattern_is_active` | endpoints |
| **#4** | owner 可 list user-scope MCP | `TestProjectMcpList` | `test_list_returns_user_scope_mcp_for_owner` | mcp |
| **#4** | row shape 與既有 `_format_mcp_server` 一致（10 鍵 lock） | `TestProjectMcpList` | `test_list_format_matches_existing_format_mcp_server` | mcp |
| **#4** | 無 MCP 的 project 回空 list（不是 error） | `TestProjectMcpList` | `test_list_returns_empty_for_project_without_mcp` | mcp |
| **#6** | member 可 list | `TestProjectMcpList` | `test_list_member_can_read` | mcp |
| **#6 / #11** | non-member list 回統一防-enum 訊息 | `TestProjectMcpList` | `test_list_non_member_returns_unified_error` | mcp |
| **#11** | 不存在 id list 回同訊息 | `TestProjectMcpList` | `test_list_nonexistent_id_returns_unified_error` | mcp |
| **#5** | owner create user-scope MCP（scope='user' + project_id 來自 path） | `TestProjectMcpCreate` | `test_create_user_mcp_by_owner_succeeds` | mcp |
| **#5 / #6** | admin create | `TestProjectMcpCreate` | `test_create_user_mcp_by_admin_succeeds` | mcp |
| **#6** | member create 回 `Permission denied` | `TestProjectMcpCreate` | `test_create_by_member_returns_permission_denied` | mcp |
| **#5** | 缺必填欄位被拒（mirror 既有 `_create_mcp_server` 驗證） | `TestProjectMcpCreate` | `test_create_missing_required_fields_rejected` | mcp |
| **#5** | owner update MCP attribute | `TestProjectMcpUpdateDelete` | `test_update_user_mcp_by_owner_succeeds` | mcp |
| **#5** | 跨 project 的 update 被拒（防 cross-project tamper） | `TestProjectMcpUpdateDelete` | `test_update_cross_project_mcp_rejected` | mcp |
| **#6** | member update 回 `Permission denied` | `TestProjectMcpUpdateDelete` | `test_update_by_member_returns_permission_denied` | mcp |
| **#5** | owner delete MCP | `TestProjectMcpUpdateDelete` | `test_delete_user_mcp_by_owner_succeeds` | mcp |
| **#6** | member delete 被拒，row 仍存在 | `TestProjectMcpUpdateDelete` | `test_delete_by_member_returns_permission_denied` | mcp |
| **#5** | `action=sync` 呼叫 `McpServer.action_sync_tools`（mocked） | `TestProjectMcpSyncAndTest` | `test_sync_user_mcp_invokes_action_sync_tools` | mcp |
| **#5** | `action=test` 呼叫 `McpServer.action_test_connection`（mocked） | `TestProjectMcpSyncAndTest` | `test_test_user_mcp_invokes_action_test_connection` | mcp |
| **#25 第二 file / D5** | unbound project create user-scope MCP 由 **ORM `@api.constrains`** 擋 | `TestProjectMcpD5OrmConstraint` | `test_create_user_mcp_on_unbound_project_rejected_by_orm_constraint` | mcp |

> "endpoints" = `src/tests/test_project_detail_endpoints.py`（18 test）
> "mcp" = `src/tests/test_project_mcp_project_centric.py`（18 test）

---

## 兩個 test file 的測試組織解說

### File 1：`test_project_detail_endpoints.py`（18 test）

聚焦 **project-as-resource** 的 detail / update endpoint（`POST /api/support-projects/<int:project_id>`），4 個 class 把行為切成可獨立 mental-model 的子集：

1. **`TestProjectDetailGet`（8 test）— Read 路徑與 ACL**
   - 守 AC #1 的 payload shape（包含 D6 carve-out：response **不含** `status` 欄位）。
   - 守 AC #6 / #11 的「**統一防-enum 訊息**」設計（見下方 §ACL 設計重點）。
   - 為什麼測 8 個：read 是攻擊面最大、最常被 enumerate 的點；分 owner / admin / member / non-member / 不存在 id / 跨 workspace 6 條 ACL 分支 + 2 條 payload shape lock，每條都要單獨 assertion 才能在 green 階段抓住分支 regression。

2. **`TestProjectDetailUpdateName`（4 test）— Write 路徑的 ACL 雙訊息設計**
   - 守 AC #2（owner / admin 可寫）+ AC #6 / #11 的 write 路徑訊息規則。
   - **關鍵設計**：member（已是 workspace 成員）寫入回 `Permission denied`；non-member（不是 workspace 成員）寫入回 `Project not found or access denied`。**訊息刻意不同**——non-member 拿到的訊息與「不存在 id」一致，防止攻擊者用 `Permission denied` vs `not found` 的差異 enumerate 出 project id 範圍。

3. **`TestProjectDetailRebind`（5 test）— Cloud Service 綁定 invariant**
   - 守 AC #3 的三條子規則：（a）同 workspace rebind OK；（b）跨 workspace rebind 拒（原綁定不變）；（c）unbind 受 D5 保護 + rebind 不重算 MCP URL。
   - **`test_unbind_with_user_mcp_rejected_via_endpoint_manual_query` 是 D5 第一條觸發路徑**（詳見下方 §D5 兩條觸發路徑）。

4. **`TestProjectDetailUrlPattern`（1 test）— URL pattern lock**
   - 守 D4 / OR-6：endpoint 走 hyphenated `/api/support-projects/<pid>` 而非沿用既有 slash pattern `/api/support/projects/<wid>`。Spec D4 末段解釋：既有 slash pattern 的 `wid` 與本 endpoint 的 `pid` 會 routing 衝突，hyphenated 也與既有 `/api/cloud-services/...` 風格一致。OR-6 已記 follow-up issue 評估全 repo URL 統一到 workspace nesting。

### File 2：`test_project_mcp_project_centric.py`（18 test）

聚焦 **MCP-as-project-subresource** 的 CRUD endpoint（`POST /api/support-projects/<int:project_id>/mcp-servers`），5 個 class：

1. **`TestProjectMcpList`（6 test）** — list payload shape lock（10 鍵與 `_format_mcp_server` 對齊，讓前端 OWL row 元件可重用）+ list ACL（與 File 1 同設計：member 可讀、non-member 與不存在 id 同訊息）。
2. **`TestProjectMcpCreate`（4 test）** — owner / admin / member create 三條 ACL + 缺必填欄位驗證（mirror 既有 `_create_mcp_server` `controllers/paas.py:1302-1326`）。
3. **`TestProjectMcpUpdateDelete`（5 test）** — owner update / delete 成功 + member 都被擋 + **跨 project tampering 被拒**（用 path 上的 `project_id` 守住，mirror 既有 service-centric guard `paas.py:1334-1336`）。
4. **`TestProjectMcpSyncAndTest`（2 test）** — sync / test action 委派到 `McpServer.action_sync_tools` / `action_test_connection`，**外部呼叫 patch 掉**（mirror 既有 `test_mcp_integration.py:TestMCPServerCronRetry` 的 patch style），保持 suite hermetic。
5. **`TestProjectMcpD5OrmConstraint`（1 test）— D5 第二條觸發路徑**（詳見下節）。

---

## D5 invariant 的兩條觸發路徑（一個 invariant，兩個攻擊面）

D5 規定：**user-scope MCP 必須掛 bound project**。但「破壞此 invariant」可以從兩個方向發動，所以要兩條獨立的 test path：

| 攻擊面 | 場景 | 防線 | Test |
|---|---|---|---|
| **A：unbind 既有 bound project** | project 已有 user-scope MCP，呼叫 `update cloud_service_id=False`。如果沒檢查，project 變成 unbound 但 MCP 還 ORM-visible，等於 D5 在 ORM 層被繞過。 | **Endpoint 內手動 query** `project.mcp_server_ids.filtered(lambda m: m.scope=='user')`，非空就 `raise ValidationError`。**不能靠** `mcp_server.py:_check_user_scope_requires_bound_project`，因為它是 `@api.constrains('scope', 'project_id')`，**只在改 mcp_server 時 fire，不會在改 `project.cloud_service_id` 時 fire**。 | File 1 `TestProjectDetailRebind.test_unbind_with_user_mcp_rejected_via_endpoint_manual_query` |
| **B：在 unbound project 新建 user-scope MCP** | project 一開始就 unbound（`cloud_service_id IS NULL`），呼叫 `create scope='user' project_id=<unbound>`。 | **既有 ORM `@api.constrains`** 自動 fire（這是改 `mcp_server` 的寫入，constraint 範圍內），endpoint 只需把 `ValidationError` 正確轉成 `{success: False, error: ...}` 給 JSON-RPC caller（不能讓它噴 HTTP 500）。 | File 2 `TestProjectMcpD5OrmConstraint.test_create_user_mcp_on_unbound_project_rejected_by_orm_constraint` |

兩條 test 都 assert error 訊息包含 `cloud service` 字串（case-insensitive），以對齊 `mcp_server.py:_check_user_scope_requires_bound_project` 的措辭，讓 D5 violation 在 log / 前端 toast 都可被識別為「同一條 invariant 守住」。

**為什麼不合併成一條 test**：兩條路徑在 production code 是**不同的程式分支**（endpoint 手動 query vs ORM constraint），合併測就拿不到「哪條防線壞了」的訊號。OR-4 記了 future cleanup：若 D5 invariant 整個重新設計（例如改 user-scope MCP URL 機制），這兩條測試的覆蓋角度都需要重新評估。

---

## ACL 設計重點：三角色 + 兩種訊息（防 enumeration）

Spec AC #6 / AC #11 的 ACL 設計**比直覺多一層**，所以測試覆蓋也跟著拆細：

**三角色**（W1 在 fixture seed 內）：
- **owner**（Alice）— full RWCU
- **admin**（Carol，`role='admin'`）— full RWCU
- **member**（Dave，`role='user'`）— **R**（read）✓，**WCU**（write/create/update/delete）✗

**兩種訊息**（write/create/update/delete 被拒時）：
- **member**（已知是 workspace 成員）→ `Permission denied`（明確告訴你「你進得來但權限不夠」）
- **non-member**（Eve，沒任何 W1 access record）→ `Project not found or access denied`（**同**「不存在 id」訊息，**不**洩 existence）
- **cross-workspace**（Carol 是 W1 admin，去讀 W2 project）→ 也用 `Project not found or access denied`（同訊息，防 cross-ws enumeration）

兩個 file 各自鋪了一遍這套覆蓋——File 1 在 detail endpoint 鋪，File 2 在 MCP endpoint 鋪——確保 implementer 不會在某個 endpoint 漏掉某條分支。**這是這次測試陣容裡最容易在 green 階段被「直覺式 ACL 寫法」破功的地方**，reviewer 看 PR diff 時請特別對照這段。

---

## B1 fresh-install red 由既有 test 證明（不在新 36 內）

Tester 跑既有 `tests/test_cross_workspace_task_isolation.py:TestTaskCrossWorkspaceACL.test_alice_cannot_read_bob_task_via_orm` 時發現**它在 fresh install 紅**：

```
AssertionError: project.task(5,) is not false : B1 leak: Alice (W1) can search() Bob's W2 task.
Need to deactivate std `project.task_visibility_rule` + `project.task_visibility_rule_project_user`
symmetrically with the project.project fix in migrations/18.0.1.0.22.
```

**Root cause**：B1 fix 邏輯只在 `src/migrations/18.0.1.0.23/post-migrate.py` 跑（開頭 `if not version: return`，只 upgrade 路徑 fire），fresh install 不 fire migration，std task ACL 從未被 deactivate。Dev sandbox / production upgrade 路徑都綠是因為**它們不是 fresh install**。

User 在 2026-05-20 拍板 **D9 scope creep**（spec §Decisions D9 / §AC #29）：本 PR 順便修，把 std task rule deactivation 從 migration 搬到 `src/security/ir_rules.xml` declaration data，fresh install + upgrade 兩條路徑都 cover。Migration 既有邏輯保留作向後相容（idempotent，再跑也 noop）。

**這條 red 不在新 36 test 內**——它是**既有的 `test_alice_cannot_read_bob_task_via_orm` 本身就是 red proof**。Implementer 在 green 階段把 `ir_rules.xml` 改好後，本 test 自然轉綠。Spec AC #29 額外要求 caqa 階段在 report 列出 **fresh install + upgrade 兩條 path 的 test 執行結果**，確保兩條路徑對稱。

---

## Regression baseline — 既有 54 個 MCP test 全綠

Spec AC #27 把 regression scope 收斂到「既有 `tests/test_mcp_*.py` 全部須繼續 pass」。Tester 跑 targeted regression：

```
0 failed, 0 error(s) of 54 tests when loading database 'woow_develop'
```

涵蓋 class：`TestMcpAclCrossWorkspaceIsolation` / `TestMcpProjectLifecycle` / `TestMcpProjectScopeConstraints` / `TestCloudAppTemplateMCPFields` / `TestBuildMCPSidecarConfig` / `TestAutoCreateMCPServer` / `TestMCPServerCronRetry` / `TestMCPServerSyncSafe` / `TestMCPServerCascadeDelete` / `TestMCPServerModel`。

換句話說：**PR #47 解耦後立下的 MCP-on-project schema 行為基線，本 PR 一條都不會破**。新 36 test 只新增 endpoint 表面，不重塑 model 行為。

---

## 刻意不測（with reason）

| 不測範圍 | 理由 |
|---|---|
| **Frontend OWL 元件 unit test**（`ProjectDetailPage` / 3 sections） | Repo 無 OWL unit test infra（spec AC #26）。前端品質改走 staging E2E smoke（spec AC #28 列了 11 步流程）。Reviewer 對前端應走 staging UI 親手點。 |
| **D6 — `status` 欄位行為**（除 "GET 不該回 status" lock 外） | Spec §D6 + OQ-1（2026-05-19 user 二度澄清）拍板「issue 字面誤會」：Odoo 18 std `project.project` **無 `status` 單欄位**（與 status 概念相關的是 `stage_id` / `last_update_status` / `active` 三欄位但都不對應 issue #48 寫的 "Active / Completed"）。**本 PR 不做 status 子段**；BasicInfoSection 只給 name / description / workspace。Backend 也不主動 reject `status` payload（前端不送）。後續若要 UI status 由另開 issue 走 `stage_id`，與本 PR 無依賴。 |
| **OR-7 — unbound project 的 ACL 語意** | Spec out of scope。Unbound project（`cloud_service_id IS NULL` → `workspace_id` computed NULL）的存取控制 spec gap；既有 `ai_assistant.py:_check_project_access` 對 unbound 回 `True`（global access），是潛在 leak 但本 PR 不修。三選項（reject all / creator-only / message_partner walk）由 follow-up issue 評估。**MCP path 不受影響**——unbound project 無法建立 user-scope MCP（File 2 D5 ORM constraint 已守），所以 OR-7 deferred 不影響 MCP 安全性。 |
| **OQ-2 — Worktree dev env 3 個 setup gaps** | 三條 infra gap（`PAAS_CI_ADDONS_PATH` 未自動 set / `requirements.txt` 不預 install / `hooks.py` 缺 `pycryptodomex → Cryptodome` import-name override）由 tester in-flight workaround，僅 doc 記在 OR-8。Implementer 可順手修 `hooks.py` override map（一行 dict entry），其餘兩條留 worktree script / docker image 後續另議。**不寫 test** —— infra setup 本來就不該 enforce by Odoo test。 |
| **Rebind race condition**（OR-2） | Spec 假設 endpoint 內 transaction 足夠；race scenario（A rebind 到 X，B 同時 delete X）由 caqa 階段 review，不寫 unit test cover（race 用 ORM TransactionCase 很難可靠重現）。 |
| **舊 service-centric MCP endpoint 行為** | Spec AC #7 明定「行為不變、前端不再呼叫、後端仍接受、後續另開 issue deprecate」。既有 54 MCP test 已 cover；新 PR 不重複測。 |

---

## 隱性假設 / Fixture 條件

兩個 file 各自獨立 fixture（無共用 `_fixtures.py` module，~80 行 setup 重複），讀者要知道的隱性 setup：

1. **Workspace 拓樸**：W1 (Alice owner) + W2 (Bob owner)，W1 內 Carol=admin、Dave=user、Eve=internal user only（**沒有** W1 access record）。Owner 的 workspace_access 由 `workspace.py:73-86` 自動建立，fixture 不顯式 create。
2. **每 project 一 service**：`project.project` 對 `cloud_service_id` 有 `UNIQUE` constraint（`project_project.py:32-38`），所以 rebind / cross-ws rebind 測試要事先 seed 額外「free」service（`service_w1_b` / `service_w2_b`）作為 rebind target。
3. **File 2 多一個 `unbound_project_w1`**：`cloud_service_id` 留空 → `workspace_id` computed NULL（`project_project.py:14-20`）。**D5 第二條觸發路徑專用**。
4. **Mock 範圍極小**：只 patch `McpServer.action_sync_tools` / `action_test_connection` 在 sync/test test 內，避免打到真實 sidecar。**D5 / ACL / ORM 行為一律 real ORM**（planner 指引：「不要 mock model layer」）。
5. **HTTP auth**：用 `HttpCase` + `self.authenticate(login, login)`（password == login），這是 Odoo test runner 標準做法。所有 endpoint 都走 JSON-RPC envelope（`{jsonrpc, method='call', params}` → `{result: {success, data?, error?}}`）。

---

## Red proof 與失敗 shape

```bash
docker compose exec -T web odoo -d woow_develop --stop-after-init --http-port=8070 \
  --test-enable \
  --test-tags 'woow_paas_platform:TestProjectDetailGet,...,woow_paas_platform:TestProjectMcpD5OrmConstraint' \
  --log-level=test
```

結果：

```
36 failed, 0 error(s) of 36 tests when loading database 'woow_develop'
```

代表性失敗（36 條都是同一根因）：

```
FAIL: TestProjectMcpUpdateDelete.test_update_cross_project_mcp_rejected
  File "..../test_project_mcp_project_centric.py", line 193
    self.assertEqual(response.status_code, 200, ...)
AssertionError: 404 != 200 : Endpoint '/api/support-projects/94/mcp-servers'
  did not respond with HTTP 200 (status=404). The project-centric MCP endpoint
  is likely not registered yet. Body excerpt: '<!DOCTYPE html>...'
```

每條失敗的 `assertion.msg=` 都點名它要守的 AC + 期望 post-condition，implementer 在 green 階段對著訊息寫就可以。

---

## 對 reviewer 的提醒

對著 PR diff 讀本報告時建議的 check list：

1. **`controllers/paas.py` 新增的 endpoint handler** —— 對 AC #1 / #6 / #11 的「兩種訊息設計」要逐字檢查：member write 必須 `'Permission denied'`，non-member（含 cross-workspace、含不存在 id）必須 `'Project not found or access denied'`。**訊息字串硬編碼**是這次設計的一部分（防 enumeration），不是 typo。
2. **AC #3 unbind 路徑的「手動 query」段** —— 對照 File 1 `test_unbind_with_user_mcp_rejected_via_endpoint_manual_query`。Endpoint 必須自己 `filtered(lambda m: m.scope=='user')`，**不能依賴** `@api.constrains` auto-fire。OR-4 記了「未來 D5 重設計時這段邏輯一併回收」。
3. **`mcp_server.py:_check_user_scope_requires_bound_project` 行為不能改** —— File 2 `TestProjectMcpD5OrmConstraint` 直接依賴它的 wording（"Cloud Service" 字串）。
4. **`security/ir_rules.xml` 新增 std task rule deactivation**（D9 / AC #29） —— 對照既有 `migrations/18.0.1.0.23/post-migrate.py` 邏輯；fresh install 路徑必須讓既有 `test_alice_cannot_read_bob_task_via_orm` 變綠。
5. **URL pattern 一致性** —— 所有新 endpoint 用 hyphenated `support-projects`（與 `cloud-services` 同 family）；既有 slash pattern `/api/support/projects/<wid>` 不動。OR-6 是 future issue。
6. **Service-centric MCP endpoint 不刪**（AC #7） —— 保留為 dead code，前端不再呼叫但 backend 仍接受；既有 54 MCP test 仍然會跑到它。

---

## 報告外的 follow-up（不在本 PR scope，但 reviewer 知道有助理解 spec 的取捨）

- OR-1 / OR-6 → 「service-centric MCP endpoint deprecation + URL pattern 統一」可合併成同一 follow-up issue。
- OR-3 → D6 carve-out 需在 issue #48 留 comment 澄清「status 子段為字面誤會」（reporter phase 任務）。
- OR-4 / OR-5 → 「D5 invariant 重設計 + sidecar auto-create 機制」可合併。本 PR endpoint 手動 query 邏輯與 D5 ORM constraint 是當前防線，未來重設計時兩處要一起回收。
- OR-7 → unbound project ACL 三選項 future issue。
- OR-8 → worktree env infra 三條 gap，僅 `hooks.py` 一行可由本 PR implementer 順手修，其餘留 docker / worktree script 後續另議。
