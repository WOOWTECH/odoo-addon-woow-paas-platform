# Test Coverage Report — MCP `cloud_service_id` → `project_id` 重構

**Date**: 2026-05-18
**Author**: refactor-test-explainer (draft；reporter phase 合併或 finalize)
**Audience**: reviewer + stakeholder + future onboarding
**Inputs**:
- Plan: `docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md` (25 AC, 6 decisions, 4 open risks)
- Decision: `docs/decisions/refactor/mcp-project-decouple.md` (7 invariants)
- Tester report: `docs/tracking/test-reports/test-scope-report-mcp-2026-05-18.md`
- 5 test files (**26 cases**，全 red，all `AssertionError`，0 fixture noise)

讀這份報告**不需要打開 test code**。每條 AC 都標清「測什麼、為什麼、不寫會炸什麼」。

## Scope

本次 refactor 把 user-scope MCP 從「service 的附屬」改成「project 的附屬」，並補上 pre-existing 的 ACL leak。

物理現實：user-scope MCP 是 **sidecar 的 wrapper**（URL 從 service helm release 算）。語意上歸 project（AI chat 真實消費 owner），物理上仍綁 service — 兩條 lifecycle 路徑因此要分別處理：
- `project.unlink()` → DB cascade（schema 層 `ondelete='cascade'`）
- `service.unlink()` → cleanup hook 主動 unlink user-scope MCP（schema 不夠，要 application-level hook）

測試矩陣覆蓋四個耦合面：**schema/migration**、**constraint**、**lifecycle**、**ACL**、**chat consumption**。System-scope MCP 路徑是「不能被 refactor 牽連」的 regression lock。

## Coverage summary

| 類別 | AC 數 | Test cases | 範圍 |
|---|---:|---:|---|
| ✅ 已測（unit / integration / ORM） | 19 | **26** | AC #1-7, #9-15, #17-21 |
| 🔍 Reviewer-phase grep 兜底（unit test 無法測 → 需 reviewer 抓） | **0** | — | （Round 2 後清空 — AC #14 已補 controller unit test） |
| ⏭ 結構性 / 間接覆蓋 + Inv-driven sanity | 4 | — | AC #8（Inv #7 sanity grep，由 AC #1 field 移除實質擋住漏改）、#22（XML view，install-time 暴露）、#23（action domain 不變）、#25（endpoint 同 chain） |
| 🧪 Staging smoke / 沙盒 manual | 2 | — | AC #16（SSE 端到端 + langgraph）、AC #24（OWL frontend） |

🔁 R2 標記：第 2 輪 tester 補的 2 條 case（AC #14 controller、AC #21 CRUD parametrized），詳見後續 AC 對應表。

## Acceptance criteria 對應（26 cases）

每條 test 連到 plan AC + decision Invariant + 「保護什麼」（不寫此 test 會炸的失敗模式）。

### A. Schema state — `TestMcpProjectDecoupleMigrationSchema` (6 cases)

| # | Test | AC | Invariant | 保護什麼（不寫會炸的失敗模式） |
|---|------|----|-----------|------|
| 1 | `test_ac1_mcp_server_has_project_id_field` | #1 | Inv #1 | `ondelete='cascade'` 是 D1 的根基 — 改成 `set null` 會讓 deleted project 留 MCP zombie，改成 `restrict` 會卡死刪 project。Test 同時 assert `comodel_name='project.project'` + `ondelete='cascade'`。 |
| 2 | `test_ac1_mcp_server_no_cloud_service_id_field` | #1 | Inv #2 | 防 D1=C 雙 FK 走私（舊欄位忘刪 → 雙 source of truth → consistency 死循環）。 |
| 3 | `test_ac1_project_id_column_exists` | #1 | — | ORM field 存在 ≠ DB column 存在。雙保險：抓到 `register_hook` 沒 sync 的詭異情境。 |
| 4 | `test_ac2_project_project_has_mcp_server_ids_inverse` | #2 | — | AI chat consumer chain `channel→task→project→mcp` 走不通，AC #13 沒這條 inverse 等於白測。 |
| 5 | `test_ac2_cloud_service_no_user_mcp_server_ids` | #2 | — | 防舊路徑復活 — 若 inverse 還在，consumer 走舊鏈仍能拿到 MCP，refactor 沒實際發生。 |
| 6 | `test_ac5_cloud_service_id_column_dropped` | #5 | — | D3=A 一次 drop 的物理驗證（`information_schema.columns` 查無）。D3=B 兩階段 deprecate 被 reject，這條 test 鎖住執行的是 D3=A。 |

### B. Migration behavior — `TestMcpProjectDecoupleMigrationBehavior` (4 cases)

| # | Test | AC | Invariant | 保護什麼 |
|---|------|----|-----------|------|
| 7 | `test_ac3_migrate_backfill_user_scope_mcp` | #3 | — | 真正 happy path：舊 `cloud_service_id` 的 user-scope MCP 自動 backfill `project_id` 指向 service 的 support project。不寫 = 既有 row 變孤兒。 |
| 8 | `test_ac3_migrate_orphan_user_scope_archive` | Resolved Q4 | — | **設計選擇 lock**：orphan（service 無對應 project）→ `active=False` archive，**不 unlink**。Migration 不可逆 → archive 是 rollback 救命繩。Implementer 若直接 unlink，rollback 後無法救回任何 row。 |
| 9 | `test_ac3_migrate_system_scope_untouched` | #3 + Inv #2 | Inv #2 | 防 over-eager backfill：system-scope 沒 `cloud_service_id` 也沒 `project_id`，migration 動到任何 system row = 違反 D4 invariant（`scope='system' ⇔ project_id IS NULL`）。 |
| 10 | `test_ac4_migrate_idempotent` | #4 | — | `-u woow_paas_platform` 重跑常見（test runner、手動 upgrade）。第二次 raise / 雙寫 / archive 再活化 = blocker。Test 同時驗證「第二次完整 state snapshot 與第一次相同」。 |

### C. Constraint — `TestMcpProjectScopeConstraints`（in `test_mcp_integration.py`, 3 cases）

| # | Test | AC | Invariant | 保護什麼 |
|---|------|----|-----------|------|
| 11 | `test_ac6_constraint_user_scope_requires_project` | #6 | Inv #2 (user leg) | D4 雙向蘊含的 user direction：`scope='user'` 但 `project_id IS NULL` 必 raise。不寫 = AI chat 拿到無 workspace anchor 的 user MCP。 |
| 12 | `test_ac6_constraint_system_scope_excludes_project` | #6 | Inv #2 (system leg) | D4 雙向蘊含的 system direction：`scope='system'` 但 `project_id IS NOT NULL` 必 raise。不寫 = system MCP 被誤掛 project，破壞 D5 reasoning。 |
| 13 | `test_ac7_constraint_user_scope_requires_bound_project` | #7 | Inv #3 | D5：user-scope MCP 的 project 必須是 bound（`project.cloud_service_id IS NOT NULL`）— 從 schema 層拒絕，**不靠 ACL rule filter 兜底**（B1 pitfall 教訓）。Positive sanity 同時驗證「bound project 不會誤殺」（防 over-correction）。 |

### D. Lifecycle — `TestMcpProjectLifecycle` (4 cases)

| # | Test | AC | Invariant | 保護什麼 |
|---|------|----|-----------|------|
| 14 | `test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall` | #9, #10 | Inv #4 | **這次 refactor 最關鍵的 regression lock**。詳見下方專章 §D1 cleanup hook 順序。 |
| 15 | `test_ac9_cleanup_hook_does_not_touch_system_mcp` | #9 sanity | — | cleanup hook filter 必須 `.filtered(lambda s: s.scope=='user')` — 若漏 filter，cleanup 連帶把 system MCP 也刪掉（platform-level 資源消失）。 |
| 16 | `test_ac11_delete_project_cascades_user_mcp` | #11 | Inv #1 | DB-level cascade 行為驗證（`ondelete='cascade'`）— Test #1 已驗 ORM declaration，但 cascade 真實發生在 DB FK。雙重保險。 |
| 17 | `test_ac12_delete_project_does_not_cross_contaminate` | #12 | Inv #1 | cascade 必須只剷除 deleted project 的 MCP — 防 careless `mapped('mcp_server_ids').unlink()` walk 越界。兩個 service/project/MCP triple，刪 A 不能動 B。 |

### E. ACL — `TestMcpAclCrossWorkspaceIsolation` (5 cases, B1-style 對等)

| # | Test | AC | Invariant | 保護什麼 |
|---|------|----|-----------|------|
| 18 | `test_alice_cannot_read_bob_user_mcp_via_orm` | #17, #19 | Inv #5 | Main rule `mcp_server_rule_workspace_member` 缺失 → pre-existing leak 仍存（任何 internal user 讀全 instance MCP）。雙路徑驗證：`search()` 必須 silent 過濾、`browse().read()` 必須 raise `AccessError`（Odoo 兩條 ACL hook 不同）。 |
| 19 | `test_alice_cannot_read_bob_user_mcp_tool` | #18, #19 | Inv #5 (B1 sub-model) | 詳見下方專章 §B1 對等 deny。 |
| 20 | `test_bob_can_read_own_workspace_mcp` | #20 | — | **Over-correction lock**（read leg）：若 implementer 怕 leak 寫成 blanket deny（`1=0` 或 domain 寫錯），own-ws user 失去自家 MCP 讀取，AI chat 整個壞掉。Bob 讀自家 = 必須 green 全程。 |
| 21 | `test_system_mcp_readable_by_all_users` | #17 system leg | Inv #6 | system-leg 的 always-true 是**故意設計**（system MCP 是平台級工具）— 若 reviewer 因看似 B1 always-true bug「修」掉，整個 ai.assistant 路徑斷。Alice + Bob 都驗一次。 |
| 🔁 R2 · 25 | `test_ac21_bob_can_crud_own_workspace_mcp` | #21 | — | **CSV CRUD 4-leg lock**：原 #20 只驗 read，partial CSV flip（如 `1,1,0,0`）會放行 read/write 但卡 create/unlink，#20 抓不到。本 case 3 ops 串接（CREATE → WRITE → UNLINK），每 leg `try/except AccessError + self.fail` — 任何 leg 失敗具體紅燈（不是模糊的「Bob 沒權限」），同時驗 CSV bit 與 rule `perm_create`/`perm_write`/`perm_unlink` flag 全 True。 |

### F. AI chat consumption — `TestMcpChatConsumption` + `TestAiAssistantControllerMcpToolsForChannel` (4 cases)

| # | Test | AC | Invariant | 保護什麼 |
|---|------|----|-----------|------|
| 22 | `test_ac13_channel_gets_user_mcp_via_project` | #13 | — | 消費鏈改路徑（channel 層）：`channel → task → project.mcp_server_ids → tool_ids`。Fixture 故意只設 `project_id`（無 `cloud_service_id` 反向 — 在新 schema 也不存在），若 implementer 走舊鏈 `task.project_id.cloud_service_id.user_mcp_server_ids`，會拿到 empty → test 抓到。 |
| 23 | `test_ac13_channel_no_mcp_for_unbound_project` | #13 negative | Inv #3 配套 | D5 constraint 阻止 unbound project 上有 user-scope MCP，但 consumer 仍須 graceful degrade（unbound project 是合法存在的、只是還沒接 service）— 回 empty `mcp_tool` recordset，而非 raise / 回 None / 回 list。 |
| 24 | `test_ac15_assistant_system_mcp_unchanged` | #15 | — | system-MCP M2M 路徑（`ai.assistant.mcp_server_ids`）是 pre-existing 行為，不能被本次 refactor 牽連。Regression lock — 若 implementer 改 M2M domain 或 `get_enabled_mcp_tools`，test 抓到。 |
| 🔁 R2 · 26 | `test_ac14_controller_get_user_mcp_tools_for_channel_walks_project` | #14 | — | **Controller chain behavioural lock**：原方案靠 reviewer grep 兜底，但若 implementer 把 chain walk 包成 helper method（如 `_walk_channel_to_project_mcps`），grep `cloud_service_id.user_mcp_server_ids` 在 controller 文件 0 hits 仍可能漏抓 — helper 內部走舊鏈。本 case 用 `MagicMock(env=env)` + `patch('...controllers.ai_assistant.request')` 直接 call 方法，linked-by-`project_id`-only 的 MCP 拿到 = chain 走對；空 recordset = chain 走錯。Pattern 同 `test_cloud_service_acl.py::TestCloudServiceControllerRegression`。 |

## Regression 保護點（既有行為 lock）

不是新行為，但本次 refactor 容易誤傷：

| # | 既有行為 | 對應 test | 為何要守 |
|---|---------|----------|---------|
| R1 | system-scope MCP 對所有 `base.group_user` 可讀 | test #21 | 設計上 system MCP 是平台工具，所有 ai.assistant 用。若 ACL 寫成「any user-scope chain」單腿，system MCP 也被擋 → AI chat 全壞。 |
| R2 | `ai.assistant.mcp_server_ids` M2M domain `[('scope', '=', 'system'), ('active', '=', True)]` | test #24 | 後台 admin 設定 AI 助手用哪些 system MCP，這條路徑與 user-scope 完全獨立，不能被 refactor 拉下水。 |
| R3 | cleanup hook 不能誤刪 system MCP | test #15 | 同 R1 — refactor 引入新 cleanup hook，必須 filter scope，否則 service.unlink() 帶走 platform-level 資源。 |
| R4 | Migration 不能洗到 system-scope row | test #9 | system row 沒 `cloud_service_id` 也沒 `project_id`，over-eager backfill 把 NULL ` cloud_service_id` 視為 0 / 隨意 join，可能誤改 `active` 或誤掛 project。 |
| R5 | Own-workspace user 可 read 自家 user MCP | test #20 | 加 ACL ≠ blanket deny。Bob 是 ws_bob 的 owner（auto-加入 access_ids），domain chain 必須 evaluate true。 |

## 專章 1：B1-style 對等 deny — 為何同時測 `mcp_server` + `mcp_tool`

**B1 pitfall 出處**：`docs/tracking/pitfalls/cross-workspace-task-data-leak.md`（CAQA B1 incident）。前次 refactor `project.task` 加 workspace-member rule 時漏掉 sub-model `project.task` 自己的 inherited std rule，導致 task 的 description / chatter 跨 workspace 可讀（main rule 鎖 task 但 sub-model 級不對等）。

**本次的等效情境**：
```
mcp_server  ← server_id ← mcp_tool
   (main)                  (sub-model)
```

`mcp_tool` 不持有 `project_id` — schema 上只透過 `server_id` 連到 `mcp_server`。若只加 `mcp_server_rule_workspace_member` 而漏 `mcp_tool_rule_workspace_member`：
- Alice `search` mcp_server → 過濾乾淨（main rule 生效）
- Alice `search` mcp_tool → **拿到 Bob 的所有 tools**（sub-model 無 rule → ACL CSV `read=1` 直接放行）

Test #19 (`test_alice_cannot_read_bob_user_mcp_tool`) 強制 implementer 在 `ir_rules.xml` 兩條 rule 一起加。Rule domain 必走 `server_id` 反向：
```python
['|', ('server_id.scope', '=', 'system'),
      ('server_id.project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]
```

**Reviewer 對照點**：看 `src/security/ir_rules.xml` diff 必須**同時看到兩條 `<record id="mcp_*_rule_workspace_member">`**。少一條 = sub-model leak（B1 復現），合併前必須 hold。

**Mcp_server / mcp_tool 是自家 model**（無 std rule 對等問題），所以**不需要** `odoo-addon-std-rule-symmetric-deactivation` 三件套的「deactivate std rule」步驟（decision §5 已明標）— 但「對等性」概念仍適用。

## 專章 2：D1 cleanup hook 順序 — Regression lock 的意義

**Test #14** (`test_ac9_cleanup_hook_unlinks_user_mcp_before_helm_uninstall`) 是這次 refactor 最關鍵的 regression lock。它捕捉的不是「最終 state」，而是「**中間時間窗**」。

### Test 怎麼鎖順序

```python
def fake_uninstall_release(namespace, release_name):
    captured['user_mcp_count_at_uninstall'] = McpServer.search_count([
        ('id', '=', user_mcp_id)
    ])
```

`uninstall_release` 被 mock，side_effect 在**被呼叫的那一刻**捕捉 mcp_server 的存活數。
- 順序正確（unlink → uninstall）：side_effect 時 mcp 已 unlink，count=0
- 順序錯誤（uninstall 在前）：side_effect 時 mcp 還在 ORM 中，count≥1

### 為何順序錯誤會炸（兩個時間窗 race condition）

來自 decision §Invariant #4 的長註解（這個 test 就是這段論證的可執行 expression）：

**錯誤順序 1：`uninstall → unlink → write state='deleting'`**
- 時間窗 1：helm 已拆 sidecar、URL 失效
- 但 ORM 還看得到 `mcp_server` 與 `mcp_tool` 的 active row
- 另一 process（cron / SSE 串流 / `channel.message_post`）此刻拿到 mcp_tool 引用發 RPC
- → 連到不存在的 endpoint → uncaught `NetworkError` / silent fail
- AI chat 回覆「執行 tool 但無 output」— 比 fast-fail 更難 debug

**錯誤順序 2：`write state='deleting' → unlink → uninstall`**
- 時間窗 2：state 已是 deleting（前端假設 service 在拆）
- 但 mcp_server 還 active — UI 同時觸發 cleanup retry / cron 觸發 auto-create 重建 MCP
- → ORM 狀態打架，可能在 helm uninstall 前先重建一個 zombie MCP

**正確順序：`unlink → uninstall → write state='deleting'`**
- ORM 層先一致（mcp_server / mcp_tool 即刻看不到，後續路徑全 fail-fast）
- 再讓 helm 物理清理（時間窗內物件已不可被 reference）
- 最後 state 收尾標示 deleting 給其他 process 看
- 三步不能 atomic（helm RPC 在 Odoo transaction 內不能 rollback），但**時間窗風險最小**

### Reviewer 對照點

看 `src/models/cloud_service.py` 的 `_do_cleanup`（function-scoped import `get_paas_operator_client` 在 1124 行附近）：
- 必須先看到 `self.project_ids.mapped('mcp_server_ids').filtered(lambda s: s.scope == 'user').sudo().unlink()`
- 再看到 `client.uninstall_release(...)`
- 再看到 `self.write({'state': 'deleting'})`

順序錯 = test #14 抓到 + 違反 Invariant #4。

## 專章 3：D5 constraint violation — 物理意義

**Test #13** (`test_ac7_constraint_user_scope_requires_bound_project`) 看似只是「一條 Python `@api.constrains`」，但物理意義很重：

### 為何「user-scope MCP 必須掛 bound project」

User-scope MCP 是 **sidecar wrapper**：
```
mcp_server.url  =  service.helm_release_name + "-mcp." +
                   service.helm_namespace + ".svc.cluster.local:" +
                   sidecar_port + endpoint_path
```

URL 的每個 component 都從 `cloud_service` 拿。**沒 service = URL 算不出來 = MCP 無物理 endpoint**。

`project` 與 `cloud_service` 是 1:0..1（`project.cloud_service_id` 可以 NULL，[[cloud-service-project-decouple]] §Invariant #2）。**Unbound project**（無 service）的 user-scope MCP 就是「指向 nothing 的 wrapper」— 物理上不該存在。

### 為何用 schema constraint 而非 ACL rule

[[cross-workspace-task-data-leak]] B1 教訓：**靠 ACL rule 兜底「不該存在的狀態」是補丁、不解 root cause**。

- ACL rule filter 走 chain `project_id.cloud_service_id.workspace_id.access_ids...` — unbound project 的 row 在 chain 第二跳 NULL → search 拿不到 → 表面像被擋
- 但 row **仍 commit 到 DB**，仍佔 PK，仍可能因為其他 path（admin / sudo / direct SQL）被讀回
- 從 `@api.constrains` 在 create/write 時直接 raise = 根本不讓這個 row 進 DB

D5 + D1 cleanup hook 互依：
- D5 constraint 假設「user-scope MCP 必有 bound project」
- D1 cleanup hook 在「service unlink」場景下先清掉 user-scope MCP — 否則 service.unlink 過程中 project 失去 service 時，constraint 在 unbind 動作中 violate（rollback 死循環）

### Reviewer 對照點

看 `src/models/mcp_server.py` 必須看到兩條 `@api.constrains`：
- `_check_scope_project_consistency`（D4，雙向：user ⇔ project_id NOT NULL）
- `_check_user_scope_requires_bound_project`（D5：user → project.cloud_service_id NOT NULL）

兩條一起才完整。

## 刻意不測（with reason）

| 未測項目 | 理由 | 替代覆蓋路徑 |
|---------|------|------------|
| **AC #8** `_model_build_mcp_endpoint_url` 路徑改 `self.project_id.cloud_service_id` | 演算法未改，只 walk path 換。寫 ORM test 無法分辨「URL 算出 vs URL 路徑對」 — 不是行為層 assertion。 | **Inv #7 sanity grep**（非兜底）：Test #2 (`no_cloud_service_id_field`) 已強制 field 移除，implementer 漏改 `_model_build_mcp_endpoint_url` 中 `self.cloud_service_id` 任何引用會直接 `AttributeError`。Reviewer 額外 grep `self.cloud_service_id` 在 `mcp_server.py` 必 0 hits（雙保險）。 |
| **AC #16** SSE streaming + message_post 端到端 MCP tool exec | 需 real n8n sidecar + langgraph runtime — unit test 無法涵蓋。Plan 標為 "regression" 但本質是 integration class。 | k8s-dev-sandbox manual smoke（staging）。 |
| **AC #22** `mcp_server_views.xml` form view 欄位改名 | XML view load failure 在 `-u woow_paas_platform` 時直接 deploy error，不需 unit test。 | Module install / 沙盒部署即可暴露。 |
| **AC #23** action domain `[('scope', '=', 'system')]` 不變 | 無行為改動，純 regression。 | Test #24 (system MCP 路徑) 間接守護。 |
| **AC #24** 前端 `McpServersTab.xml` query 改 `service.project_ids[0].mcp_server_ids` | OWL 前端，無 Python test infra。 | Dev sandbox manual smoke。 |
| **AC #25** `/api/cloud-services/...` MCP endpoint | 若存在，行為與 AC #13/#14 chain 同。 | Test #22 (channel) + Test #26 (controller) walk chain 同源覆蓋。 |
| **mcp_server.url 算法** | AC #8 只改 walk path，演算法本身不變。 | 無對應 invariant 變動，無 regression 風險。 |
| **scope='system' 對 cross-user 可見性** | Pre-existing 行為，明文 OUT OF SCOPE。 | Test #21 鎖 always-true 設計（防被誤改）。 |
| **Helm CLI 真實呼叫** | Plan 明標：本次不引入 helm CLI mock 層。Lifecycle test 用 Python `patch` 函式級 mock 即可驗 ORM state 與順序。 | k8s-dev-sandbox 真實 helm 驗證（staging only）。 |

## Test fixture / 隱性假設（reviewer 必看）

不寫 test，但 test 依賴這些條件 — implementer 改 production code 時碰到這些假設要警覺：

1. **All tests `@tagged('post_install', '-at_install')`**：addon 載完才跑（new constraint / ACL 已套）。Migration test 用 `importlib.util.spec_from_file_location` 動態 load `18.0.1.0.24/post-migrate.py`（dot-separated 不是 Python identifier）。

2. **Two-workspace Alice/Bob fixture**：刻意 mirror `test_cross_workspace_task_isolation.py`（B1 regression 樣板）— 視覺上 reviewer 能直接對照前次 incident。`workspace.create({'owner_id': X})` 自動把 X 加進 `access_ids`，這是 ACL chain `access_ids.user_id == bob` evaluate true 的前提。

3. **Lifecycle test 用 `with_context(active_test=False)`**：捕捉 cleanup 時 mcp_server 是否真的 unlink。若 implementer 把 cleanup 改成 archive（active=False）而非 unlink，這個 context 仍會看到 row → test 會誤判為「cleanup 失敗」。**這是 tester 對 D1 設計選擇的鎖**：cleanup 必須 unlink、不是 archive（與 migration orphan archive 不同，因為 migration 不可逆而 cleanup 是 runtime 可重建）。

4. **Migration test 用 raw SQL seed**（`_seed_user_scope_row_via_sql`）：introspect `information_schema.columns` 動態組 INSERT — 同一 helper 在 pre-migration / post-migration / 半途 schema 都能跑，避免 fixture-broken 失敗汙染紅相 signal。

5. **Cleanup hook patch target**：
   ```python
   patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client')
   ```
   `_do_cleanup` 內 function-scoped import — patch 源 module 屬性才能命中。Implementer 若改成 lazy import / move import 位置，這個 patch 失效，test 會誤綠。

6. **Workspace chain 可 search**：`project.project.workspace_id` 是 `compute='_compute_workspace_id', store=True`（`models/project_project.py:14-25`）— stored 才能被 ir.rule domain 用。Implementer 改 compute / 去掉 store = ACL chain 斷。

## 對 reviewer 的提醒（PR 對照導讀）

讀 PR diff 時對照本報告：

1. **`src/models/mcp_server.py`** 改動
   - Field: `cloud_service_id` 必須整段拿掉 → 對 Test #2、#5、#6
   - Field: `project_id` 必須有 `ondelete='cascade'` → 對 Test #1
   - 兩條 `@api.constrains`（D4 + D5）→ 對 Test #11、#12、#13
   - `_model_build_mcp_endpoint_url` 內 grep `self.cloud_service_id` 必 0 hits → AC #8 reviewer 檢查

2. **`src/models/project_project.py`** 改動
   - 新 `mcp_server_ids = fields.One2many(..., 'project_id', ...)` → 對 Test #4

3. **`src/models/cloud_service.py`** 改動
   - 移除 `user_mcp_server_ids` 反向 O2M → 對 Test #5
   - `_do_cleanup` hook 三步順序 → 對 Test #14（**順序錯就違反 Inv #4**）
   - cleanup hook 必須 `.filtered(lambda s: s.scope == 'user')` → 對 Test #15

4. **`src/models/discuss_channel.py`** + **`src/controllers/ai_assistant.py`** 改動
   - `_get_user_mcp_tools` (channel) 必須走 `task.project_id.mcp_server_ids` → 對 Test #22、#23
   - `_get_user_mcp_tools_for_channel` (controller) 同上 → 對 🔁 R2 Test #26
   - 兩條 chain 應 identical rebar — reviewer 看 diff 時兩處寫法須對齊。**helper method 抽取也安全**（Test #26 是 behavioural lock，helper 內部走舊鏈仍會被抓）。

5. **`src/security/ir.model.access.csv`**
   - `mcp_server` + `mcp_tool` 兩 row RWCU `1,0,0,0` → `1,1,1,1`（user）。**Admin row 不變**。
   - Read leg 由 Test #20 守、CREATE/WRITE/UNLINK 3 legs 由 🔁 R2 Test #25 守（每 leg 失敗具體紅燈，partial flip 如 `1,1,0,0` 會在具體 leg 顯示）。CSV 4-bit 與對應 `ir.rule` 的 `perm_*` flag 須一致設 True。

6. **`src/security/ir_rules.xml`**
   - **必須兩條一起**：`mcp_server_rule_workspace_member` + `mcp_tool_rule_workspace_member`（B1 對等，§專章 1）。
   - 兩條 domain 都是 `'|', ('scope', '=', 'system'), (workspace chain)` — system leg 是故意 always-true（§Inv #6）。**不要當 B1 bug 修掉**。

7. **`src/migrations/18.0.1.0.24/post-migrate.py`**（新檔）
   - 四步：backfill → orphan archive (active=False, NOT unlink) → DROP COLUMN → `ir_model_data.noupdate=false`。
   - Idempotent（Test #10 第二次跑必 state 一致）。
   - 用 `cr.execute` raw SQL（這時 ORM 尚未載入新 schema）。

8. **`src/__manifest__.py`** 版本 bump `18.0.1.0.23` → `18.0.1.0.24`。

## See also

- [[mcp-project-decouple]] — 本 refactor 的 decision doc（7 invariants）
- [[cloud-service-project-decouple]] — service/project 解耦的 invariants（本次依賴 §Invariant #2）
- [[paas-workspace-member-acl-tightening]] — workspace-member ACL 三件套 pattern
- [[odoo-addon-std-rule-symmetric-deactivation]] — std rule 對等 deactivation pattern（本次 model 是自家、不需要 deactivate，但「ACL 對等」概念仍適用）
- [[cross-workspace-task-data-leak]] — B1 pitfall（sub-model 對等 leak 的根源案例）
- `docs/tracking/test-reports/test-scope-report-mcp-2026-05-18.md` — tester 的 AC mapping + red proof + 6 open Q

## Hand-off

**Audience**:
- **Implementer**：本報告告訴你「每條 test 對應的 invariant 是什麼」— 動 production code 時對照本檔的「reviewer 對照點」與「fixture 隱性假設」。
- **Reviewer**：本報告告訴你「每條 test 守的失敗模式是什麼」+ 「PR diff 該對照本檔哪幾段」。
- **Stakeholder / future onboarding**：讀 §Scope + §專章 1/2/3，5 分鐘理解本次 refactor 在驗什麼。

**Reasoning gap 收尾**（Round 2 後狀態）：

- ~~AC #14 controller 路徑無獨立 unit test~~ → **🔁 R2 Test #26 已補**（`MagicMock(env=env)` + `patch(request)`，helper-wrap-safe）
- ~~AC #21 CSV bit 變更只有 read leg 行為驗證~~ → **🔁 R2 Test #25 已補**（CRUD 4-leg parametrized，partial flip 具體紅燈）
- **AC #19 雙路徑 deny 散在 #18/#19 case 內**（沒拆獨立 test class）— **accepted by leader**，視覺對等明顯（兩個 ACL hook 在同 case 內驗反而對等性可讀），不補。

Round 2 後**無 outstanding reasoning gap**。Coverage 邊界已收攏到 §刻意不測表內列出的「結構性 + Inv-driven sanity + staging smoke」三類，每類都有對應的非 unit-test 覆蓋路徑。
