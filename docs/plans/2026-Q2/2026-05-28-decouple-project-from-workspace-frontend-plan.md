# Plan: 解除「Create New Project」對話框的 Workspace 強制要求（前端 + Controller 收尾）

**Date**: 2026-05-28
**Branch**: `fix/decuple-project`
**Status**: signed-off (2026-05-28) — ready for tester (TDD red) hand-off
**Related**: [[cloud-service-project-decouple]] §Invariants（既有 7 條，皆不衝突）

---

## Context

**現況**：使用者透過 maestri portal 進入 `/woow#/projects`（`SupportProjectsPage`），點「New Project」會跳出 `CreateProjectModal`，**強制要求選 Workspace**（必填驗證 + UI 永遠顯示 workspace dropdown）。

**目標**：讓「Create New Project」**不需要選 workspace** — Support project 應該能獨立存在（也可選擇性綁 cloud_service / workspace）。

**Refactor 背景**：[[cloud-service-project-decouple]]（2026-05-17 accepted）已決定 D3=A「unbound Project ACL 走 project member」，後端 model + ir.rule + test 已完整支援 unbound project。**本 plan 補的是該 refactor 漏掉的「Controller create path + Frontend Modal UX」收尾**，不開新 design 平面。

---

## 1. Root cause（檔案:行號）

### A. 前端強制（主要阻擋點）

| 位置 | 行為 |
|---|---|
| `src/static/src/paas/components/modal/CreateProjectModal.js:124-126` | `if (!this.state.workspaceId) { this.state.error = "Please select a workspace"; return; }` — 客戶端硬擋 |
| `src/static/src/paas/components/modal/CreateProjectModal.xml:41-63` | Workspace dropdown 永遠顯示（無 `t-if`），且 placeholder `"Select a workspace..."` 沒有「No workspace」/「Personal」選項 |
| `src/static/src/paas/components/modal/CreateProjectModal.xml:66` | Cloud Service dropdown `t-if="state.workspaceId"` — 必須先選 workspace 才能載 services |
| `src/static/src/paas/components/modal/CreateProjectModal.js:60-84` | `_loadCloudServices(workspaceId)` 必須有 `workspaceId` 才會 fetch |

### B. Controller create 路徑（語意不一致）

| 位置 | 行為 |
|---|---|
| `src/controllers/ai_assistant.py:994-1001` | endpoint 註冊三條 URL，**其中一條是 `/api/support/projects`（無 workspace、無 cloud_service）** — 設計上似乎支援完全 unbound |
| `src/controllers/ai_assistant.py:1038-1040` | 但 dispatcher 邏輯：`if not cloud_service and not workspace: return {'error': 'cloud_service_id is required for create'}` — **完全 unbound 被擋下** |
| `src/controllers/ai_assistant.py:1206-1242` | `_create_project()`：有 cloud_service → bind；有 workspace 但無 cloud_service → 建立 unbound（**workspace 沒有被 persist 到 project**）；**完全 unbound 路徑不存在** |
| `src/controllers/ai_assistant.py:1228` | `request.env['project.project'].sudo().create(vals)` — **sudo() 讓 create_uid = SUPERUSER**，違反 [[paas-workspace-member-acl-tightening]] 的 unbound dual-leg domain 預期 |

**Frontend service caller**（一致缺 unbound path）：

| 位置 | 行為 |
|---|---|
| `src/static/src/paas/services/support_service.js:247-264` | `createProject(workspaceId, data)` → `POST /api/support/projects/${workspaceId}` — 必填 workspaceId |
| `src/static/src/paas/services/support_service.js:275-293` | `createProjectForService(cloudServiceId, data)` → service path |
| ❌ 缺 | `createUnboundProject(data)` 走 `/api/support/projects`（無 workspace）— **不存在** |

### C. 後端已完整支援（**無需改動**）

| 位置 | 證據 |
|---|---|
| `src/models/project_project.py:43-48, 106-109` | `workspace_id` 為 computed-from-cloud_service_id 的 optional 欄位，**無 required=True**、**無 _sql_constraints** |
| `src/security/ir_rules.xml:65-78` `project_rule_workspace_member` | dual-leg domain：bound → workspace member；unbound → follower OR creator |
| `src/tests/test_project_decouple.py:46-59, 169-198` | AC#1/#4/#5/#6 既有 test 覆蓋 unbound create + ACL |
| `src/models/project_project.py:114-161` `create()` override | 為非 SUPERUSER create 自動 bootstrap `project_access_ids` 給 creator 當 owner（但**對 SUPERUSER create 不 bootstrap，除非有 `bootstrap_owner_partner_id` hint**） |

### 🔴 Hidden trap：sudo create + ir.rule 的 `create_uid` leg 失效

這是本 fix **必須一起處理**的隱性問題，否則「Frontend 允許 unbound create」修完後使用者**依然看不到自己建的 project**：

- Controller line 1228 用 `sudo().create()` → 建出的 project `create_uid = SUPERUSER (1)`
- `project_rule_workspace_member` unbound leg 是 `('message_partner_ids', 'in', [user.partner_id.id]) OR ('create_uid', '=', user.id)`
- 後者對 sudo create 永遠 False（`SUPERUSER ≠ user.id`）
- 前者 `message_partner_ids` 是 Odoo `mail.thread` follower — **controller 沒有 subscribe 動作**（grep 確認）
- → 使用者建完 unbound project 後，**兩條 leg 都 False，ir.rule 拒讀**
- Model `create()` override 也不救：對 SUPERUSER + 無 hint 直接 `return projects`，**不 bootstrap project_access_ids**（但即使 bootstrap 也沒幫助 — `project_access_ids` 不在 project ir.rule domain 內）

**佐證**：`src/controllers/ai_assistant.py:43-58` `_check_project_access` 對 unbound 走的是 `user_partner in project.message_partner_ids`（follower），沒看 create_uid — 暗示 controller layer 整體預設 follower 是 unbound 的唯一信任途徑，所以 create 後必須 subscribe。

---

## 2. 修正範圍清單

### 後端 model / security

- ✅ **無需動**：`project_project.py`、`ir_rules.xml`、`ir.model.access.csv`

### Controller (`src/controllers/ai_assistant.py`)

- **必改 1**：line 1038-1040 移除「無 workspace + 無 cloud_service 拒絕」的 guard（或保留並依「方案選擇」決定）
- **必改 2**：`_create_project()` 在 create 後 subscribe self user 為 follower（修 hidden trap）
  - 形式：`project.sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])`
  - 或：改用 `with_user(request.env.user).create()` 讓 `create_uid` 對齊 → 與 `_check_project_access` follower-only 假設衝突，**不建議**
  - **建議走 follower subscribe 路徑**（與 controller idiom 一致）

### Frontend OWL

- **必改 1**：`CreateProjectModal.xml` workspace dropdown 加「— No workspace — (Personal project)」選項（與 cloud service dropdown 既有的「— No service —」對稱）
- **必改 2**：`CreateProjectModal.js:124-126` 移除 `!workspaceId` 必填檢查
- **必改 3**：`CreateProjectModal.xml:66` cloud service dropdown 改成 `t-if` 邏輯支援「無 workspace 也能顯示 service list」（細節依 D3 拍板）
- **必改 4**：`support_service.js` 加 `createUnboundProject(data)` method 走 `POST /api/support/projects`（無 workspace, 無 cloud_service）
- **可選**：`SupportProjectsPage` — 已有 `Unbound` badge（`SupportProjectsPage.xml:137-140`）+ unbound 樣式（`_support_projects.scss`）— **不需動**

### Test

- **新增 controller test**（HttpCase）：`POST /api/support/projects` `action=create` 無 workspace + 無 cloud_service → 應該 success 且 **創建者能用 with_user 查到該 project**（驗證 follower subscribe 有 work）
- **新增 OWL test**：CreateProjectModal 不選 workspace + 不選 service，submit 應該成功（call `createUnboundProject`）
- **regression**：`test_project_decouple.py` 既有 AC#4/#5/#6 不應該被 break
- **regression**：`ServiceDetailPage` 入口（pre-select workspace + cloud_service）仍 work

### Docs

- **新建本 plan doc**（即此檔）
- **可選**：在 [[cloud-service-project-decouple]] §Consequences 加 follow-up 註記：「Frontend Modal 強制 workspace 的限制已於 2026-05-28 plan 解除（見 [[2026-05-28-decouple-project-from-workspace-frontend-plan]]）」 — leader 拍板後再加
- **可能需要新 pattern doc**：若決定走「controller sudo create + follower subscribe」這條解 trap 路徑，可考慮歸納成 `docs/decisions/patterns/controller-sudo-create-follower-subscribe.md`，避免下個 unbound-class endpoint 再撞同坑（**escalate 給 leader 決定是否值得 promote 為 pattern**）

---

## 3. Invariants 衝突檢查

對照 [[cloud-service-project-decouple]] §Invariants 7 條：

| # | Invariant | 本 plan 是否衝突 |
|---|---|---|
| 1 | `unique_cloud_service` SQL constraint 不可拔 | ✅ 不動 |
| 2 | `cloud_service_id` ondelete='set null' | ✅ 不動 |
| 3 | `workspace_id` compute/store/related | ✅ 不動 |
| 4 | 3 條 Odoo std rule active=false | ✅ 不動 |
| 5 | `base.group_user` Project/Task ACL + ir.rule | ✅ 不動（且**驗證了** controller create 後 subscribe 的修法不會 leak — 因為 follower 本身就是 unbound leg 的合法 entry） |
| 6 | AI prompt 注入 helm_values 經 sanitizer | ✅ 不動 |
| 7 | `***REDACTED***` literal | ✅ 不動 |

對照其他 pattern docs：

- [[paas-workspace-member-acl-tightening]] §Bound vs unbound 雙腿 — **本 plan 的 controller subscribe 動作恰好對齊** unbound leg 對 follower 的依賴；如果走 follower 路徑而非 create_uid 路徑，與 pattern 一致
- [[odoo-addon-std-rule-symmetric-deactivation]] — ✅ 不適用（本 plan 不動 Odoo std rule）
- [[odoo-std-field-no-override]] — ✅ 不適用
- [[constraint-input-trust-expansion]] — ⚠️ **需注意**：controller 在 unbound create 後 subscribe `request.env.user.partner_id` — 這是 server-authoritative 來源（不是 attacker-controllable kw/context），符合 pattern 的「authority 來自可信來源」原則。**不違反**

---

## 4. 下一步建議

### ⚠️ STOP — 有 3 個設計決策需 escalate 給使用者拍板，plan 不能直接推進到 tester

#### D1：是否允許「完全 unbound」project（無 workspace、無 cloud_service）？

| 選項 | 描述 | 影響面 |
|---|---|---|
| **A**（建議）| 允許完全 unbound — `POST /api/support/projects` 無 workspace + 無 service → 直接 create unbound project | Controller dispatcher guard 拿掉；Frontend modal 完全可不選 workspace；UX 上呈現為「Personal Project」 |
| **B** | 仍要求 workspace，但 UI 隱藏 selector，predefault 給「使用者第一個 workspace」 | 後端不動；Frontend 簡化；但**沒解開「Support project 可獨立存在」的初衷** |
| **C** | 仍要求至少一個（workspace OR cloud_service），但允許 unbound 條件下 workspace 為空 | Controller 邏輯不變；Frontend 改 dropdown UX；**矛盾**：使用者選 workspace 但後端不 persist 任何關聯，等同沒選 |

**推薦 A**。理由：與後端 ir.rule + 既有 test 已支援的 unbound 模型一致；UX 上 `SupportProjectsPage` 已有 `Unbound` badge ready；後端 `_check_project_access` follower-only 對齊 plug-and-play。

#### D2：unbound creator authority 處理方式

| 選項 | 描述 | Tradeoff |
|---|---|---|
| **A**（建議）| Controller create 後 `project.message_subscribe(partner_ids=[self.partner_id])` | 與 controller layer 既有 `_check_project_access` follower-only 假設對齊；最小改動；複用 Odoo `mail.thread` 原生機制 |
| **B** | Controller 改用 `with_user(request.env.user).create()` 代替 sudo() | `create_uid` 對齊；但 model `create()` override 走非 SUPERUSER 分支會 bootstrap `project_access_ids` — 與 ir.rule domain 不一致（不靠 `project_access_ids` leg），等於 redundant；**且 sudo→non-sudo 改可能引入新 ACL 紅 — 需更廣 regression** |
| **C** | 改 ir.rule 加 `project_access_ids` leg | 跨層改動：ir.rule + controller create 後 bootstrap；違反「最小可逆」refactor 哲學；需重新 verify B1 隔離 |

**推薦 A**。理由：modify scope 最窄（只動 controller 一行 `message_subscribe`），與既有 idiom 對齊（`_check_project_access` line 53-55），且 follower 機制是 Odoo 原生、整個 project chatter 體驗都受惠。

#### D3：UI 上 cloud_service dropdown 在「無 workspace」狀態怎麼呈現？

| 選項 | 描述 | UX 連動 |
|---|---|---|
| **A**（建議）| 「無 workspace」時隱藏 cloud_service dropdown（與目前 `t-if="state.workspaceId"` 一致） | 簡單；明確告訴使用者「unbound project 就是 unbound」 |
| **B** | 「無 workspace」時改顯示「所有 workspace 的 services」聚合清單 | 違反 multi-tenant 隔離直覺；且 `cloud_service.workspace_id` 是 ir.rule scope 點 — 跨 workspace 取 services 走 sudo() 才能拿，UX 露出 cross-workspace 語意，**不推薦** |
| **C** | 「無 workspace」時 dropdown 灰掉並顯示 hint「Please select a workspace to bind a cloud service」 | 視覺乾淨但隱性教導使用者「想綁 service 就得選 workspace」 — 與 D1=A 解耦初衷半矛盾 |

**推薦 A**。理由：與 [[cloud-service-project-decouple]] D6=A「cross-workspace task 隔離收緊」一致；不暴露 cross-workspace 語意；簡化心智模型。

---

### 推薦組合（給 leader 拍板）

**D1=A + D2=A + D3=A**：
- Controller：移 dispatcher guard + 在 `_create_project` create 後 subscribe self user 為 follower
- Frontend Modal：加「— No workspace — (Personal project)」option + 移 workspace 必填驗證 + cloud service dropdown 維持 `t-if="state.workspaceId"`
- service_support.js：加 `createUnboundProject(data)`
- Test：補 controller HttpCase（unbound create + self can read）+ OWL test（modal flow）

**Plan 拍板後 hand-off 順序**：
1. tester (TDD red) — 寫上述 4 條新 test（含 hidden trap 的 follower regression）
2. implementation — 改 controller + modal + service caller
3. caqa — 重點 audit unbound project 是否 leak 給其他 workspace user
4. reviewer — 對照 [[cloud-service-project-decouple]] §Invariants + [[paas-workspace-member-acl-tightening]]
5. reporter — 把本 plan 升級為 [[cloud-service-project-decouple]] follow-up entry，視情況 promote pattern doc

### 不推進到 tester 的情境

- 若使用者選 D1=B（仍要求 workspace）→ scope 退化為 frontend only，後端 controller 完全不動，hidden trap 也不需處理（但**沒達成解耦目標**），需 leader 重新評估
- 若使用者要求 D2=B / D2=C → 必須先寫 ACL regression scope（受影響 model 更廣），本 plan 不展開

---

## 5. Browser 驗證計畫

**工具**：playwright-cli（使用者指定 `--headed` 模式）。

### 前置

1. 啟動本機 Odoo dev 環境
   ```bash
   ./scripts/start-dev.sh
   ```
2. 確認 worktree 對應的 port（`.env` 內 `ODOO_PORT_EXTERNAL`）
3. 登入到 `http://localhost/web` 後切到 `/woow#/projects`
4. 透過 maestri portal 觀察結果（leader hand-off 提到的「project-view」portal）

### Test Cases

| # | 場景 | 操作 | 預期（After fix） |
|---|---|---|---|
| TC1 | **完全 unbound create** | `/woow#/projects` → 「New Project」→ name only → submit | Modal 關閉；列表多一張 card 顯示 `Unbound` badge；reload 後仍可見 |
| TC2 | **selectworkspace + 無 service** | 同上 modal 選 workspace、不選 service → submit | 後端 cloud_service_ids 空（workspace 選擇實際不 persist）；ACL 預期：creator 為 follower 可見 |
| TC3 | **bound regression** | `/woow#/cloud-services/<id>` → 「New Project」（workspace、service 預填）→ submit | 仍 work；card 顯示 cloud_service name |
| TC4 | **跨 user ACL 隔離** | TC1 建完 → 切到不同 workspace 的 user → `/woow#/projects` | 看不到 TC1 的 unbound project（ACL 應該擋） |
| TC5 | **modal UI 樣態** | 打開 modal → 不選 workspace | cloud_service dropdown 隱藏（D3=A）；workspace dropdown 第一個 option 是 `— No workspace — (Personal project)` |

### 異常驗證（重要）

- **Hidden trap 回歸測**：TC1 reload 後若 card 不見了 → 確認 controller `message_subscribe` 未生效；屬於 implementation 階段需 catch 的紅
- **既有 unbound badge regression**：對照 `SupportProjectsPage.xml:137-140`，新建的 unbound project 應該 trigger `o_woow_sp_card_unbound` 樣式

### 何時執行

- **不在 plan 階段執行**（本 phase 不寫 code、不跑 browser）
- 由 implementation 階段在本機完成功能後，依此 5 條 TC 走 playwright-cli verify；caqa 階段再對 ACL 隔離（TC4）獨立 audit

---

## Decision Sign-off

- **Date**: 2026-05-28
- **Decided by**: user (via decuple-leader)
- **D1 = A**: 允許完全 unbound project（無 workspace + 無 cloud_service）
- **D2 = A**: controller create 後 `message_subscribe` self user 為 follower
- **D3 = A**: UI `cloud_service` dropdown 在「無 workspace」狀態時隱藏
- **Open Q #4（Pattern doc promote）**: 留待 reporter 階段評估，不阻擋本 plan 推進
- **Next phase**: tester (TDD red, 2 條新 test — controller HttpCase + OWL component) → implementation → caqa → reviewer → reporter

---

## Open questions（已回答 — 保留以利追溯）

1. **D1**（完全 unbound 是否允許）— ✅ A（允許）
2. **D2**（creator authority 處理）— ✅ A（subscribe follower）
3. **D3**（cloud_service dropdown 無 workspace 狀態）— ✅ A（隱藏）
4. **Pattern doc promote？** — ⏸ 推遲到 reporter 階段判斷

---

## Affected files（推薦組合 A+A+A 範圍）

| 檔案 | 動作 |
|---|---|
| `src/controllers/ai_assistant.py:1038-1040` | 移除「無 workspace + 無 service 拒絕」guard |
| `src/controllers/ai_assistant.py:1206-1242` `_create_project` | 加 `project.message_subscribe(partner_ids=[request.env.user.partner_id.id])`；接受完全 unbound vals |
| `src/static/src/paas/components/modal/CreateProjectModal.js:124-126` | 移除 workspaceId 必填檢查 |
| `src/static/src/paas/components/modal/CreateProjectModal.xml:41-63` | 加「— No workspace — (Personal project)」option |
| `src/static/src/paas/services/support_service.js` | 加 `createUnboundProject(data)` method |
| `src/tests/test_project_decouple.py` 或新檔 | 加 controller HttpCase: unbound create + self read regression |
| `src/tests/` 新檔 | OWL test：CreateProjectModal unbound submit |
| `docs/decisions/refactor/cloud-service-project-decouple.md` §Consequences | 加 follow-up 連結（reporter 階段） |

---

## Test scope 建議（給後續 tester）

### 既有 regression（不可破）

- `test_project_decouple.py` AC#1/#4/#5/#6
- `test_mcp_acl_unbound_isolation.py` AC#42（unbound update 非 follower 拒絕）
- `test_cross_workspace_task_isolation.py`（B1 leak regression）

### 新行為（必須覆蓋）

- Controller HttpCase：`POST /api/support/projects` action=create 無 workspace + 無 cloud_service → success + creator with_user can read
- OWL component test：CreateProjectModal 不選 workspace → submit 不報錯 + 呼叫 `createUnboundProject`
- ACL：unbound project 建立後，**非 follower 的同 workspace user 看不到**（驗證 follower 確實是 trust boundary）
- ServiceDetailPage 入口 regression：modal 預填 workspace + cloud_service 仍 work

### 刻意不測（理由）

- 「user 選 workspace 但後端不 persist」的語意對齊 — UI behavior + 後端 model 解耦在這個 plan 之外有獨立決策（D1=B 才會碰），這個 plan 走 D1=A 不處理
- 跨 workspace 移動 unbound project — 不在 scope，未來 feature

---

## Migration

**無需 schema migration**。後端 model + ir.rule 已在 [[cloud-service-project-decouple]] 落地；本 plan 只動 controller 邏輯 + frontend 行為，rollback 即 revert PR。
