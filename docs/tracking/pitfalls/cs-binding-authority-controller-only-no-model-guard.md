# Finding: project↔CloudService 綁定授權只在 controller 層 gated、model 層無守（defense-in-depth 缺口）（✅ 已解決 2026-08-13，#698；附原始風險評估的更正）

> 來源：#280/#279 租戶網路隔離 spec 過程，docs-leader 附帶請查「`cloud_service_ids` 寫入是否 ACL-gated 到成員 workspace」（2026-07-04）。**非即刻可利用洞**（主路徑已 gated），但屬 `constraint-input-trust-expansion` / `two-layer-acl-lockstep` family 的 defense-in-depth 缺口，值得 follow-up。**追蹤 issue：[#289](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/289)。**
>
> Pattern：[`constraint-input-trust-expansion`](../../decisions/patterns/constraint-input-trust-expansion.md)、[`two-layer-acl-lockstep`](../../decisions/patterns/two-layer-acl-lockstep.md)、[`orm-vs-endpoint-acl-test-coverage`](../../decisions/patterns/orm-vs-endpoint-acl-test-coverage.md)

## ✅ 已解決（2026-08-13 #698 落地，#705 / #715 / #716 三輪補洞；追蹤 issue #289 於 2026-08-23 結案）

> 這一段是**今天**的狀態。**下方原文一字未改**（含當時那張分佈表、那句「非即刻可利用洞」與那些絕對行號指標），保留作對照——刪掉就看不出當時誤判在哪。本段的 code 指標一律走「檔案.py:符號」形式，因為 `repo-lint.py` 第 20 條 `doc-symbol-anchor` 只驗這個形式（行號形式天然不匹配、完全不驗）。

### 守衛在哪

`src/models/cloud_service_binding_guard.py:CloudServiceBindingGuard`——一個 AbstractModel mixin，是**唯一實作**，`project.project` 與 `project.task` 共用。

- **攔截點**在 mixin 自己的 `src/models/cloud_service_binding_guard.py:create` 與 `src/models/cloud_service_binding_guard.py:write`。繼承者**不需要、也不應該**在自己的 `write` / `create` 裡另外呼叫守衛——#698 一口氣找到四個入口，逐一手動掛的漏接機率是 100%，所以攔截點放在 mixin 身上；新增第五個入口只要把欄位名加進該 model 的 `_cs_binding_fields`。
- **判準入口**是 `src/models/cloud_service.py:_assert_binding_authorized`（四個受保護欄位共用同一條線）：對「這次寫入實際**新增 ∪ 移除**」的每一顆 service，要求呼叫者是該 service 所屬 workspace 的 owner／admin。判準走 `workspace.check_user_access(user, required_role=ROLE_ADMIN)`（`ROLE_HIERARCHY` ⇒ admin 與 owner 通過），刻意不自己再寫一套 role 比較。
- **反向端**（從 service 側寫 `project_ids`）走另一個方法、而且是**前置**檢查：`src/models/cloud_service.py:_assert_reverse_binding_authorized`。這一端不能沿用 mixin 的 after-diff，理由是結構性的——綁定一旦寫進去，那個 project 就落進呼叫者的 bound 分支，事後檢查反而**通過**（實測過：接上 mixin 之後攻擊照樣成功）。自我實現的判準必須前置評估。
- **掛載宣告**：`src/models/project_project.py:_cs_binding_fields`、`src/models/project_project.py:_cs_binding_triggers`、`src/models/project_task.py:_cs_binding_fields`、`src/models/project_task.py:_cs_binding_write_triggers`。

### 守衛的形狀：比對實際差異，不重演指令語意

snapshot before → `super()` → snapshot after → 比對實際差異，實作在 `src/models/cloud_service_binding_guard.py:_cs_binding_snapshot` 與 `src/models/cloud_service_binding_guard.py:_cs_binding_assert`。

守衛**刻意不重演** x2many 的指令語意。理由寫在該檔檔頭：本 repo 有明確教訓（#657，同一行修了兩次）——逐一重演框架的解析規則，就是會漏掉下一個形態。改看「relation 實際變成什麼」之後，`(4)` / `(6)` / `(0)` 的新增與 `(3)` / `(5)` / `(6)` / `(2)` 的移除一律被看見，未來多出來的指令形態不需要回頭改守衛。

比對的**範圍**由 `src/models/cloud_service_binding_guard.py:_cs_binding_diff_fields` 決定（只看呼叫者這次指名的載體欄位），而且那個閘門看的是**框架解析後**的 vals（`_add_missing_default_values`）——只看呼叫者送進來的 raw vals 的話，`default_<field>` context 與 `ir.default` 整族來源都繞得過去（#705 F1）。

### 與本 finding「建議」段的差異

下方「建議」段要的是 **`@api.constrains('cloud_service_ids')`**。實作**刻意沒有採用 constrains**，改成上面那個 snapshot-diff 形狀，理由同上（#657）。

但 Eugene 拍板的 **Model B** 被完整保留：只做 authority check、**不加** same-workspace 限制、**不用** `len(workspace_ids) <= 1`，跨 workspace 綁定仍然合法（前提是呼叫者在每個目標 workspace 都夠格）。

### 涵蓋範圍比本 finding 大

本 finding 只講 `project.cloud_service_ids` 一側。實際守住的是：

| 受保護的面 | 落點 |
| --- | --- |
| `project.project` 的 `cloud_service_ids` | `src/models/project_project.py:_cs_binding_fields` |
| `project.project` 的 `cloud_service_id`（legacy M2O；只觸發、不參與 diff，否則多綁一顆會讓 primary 位移、看起來像移除）| `src/models/project_project.py:_cs_binding_triggers` |
| `project.task` 的 `extra_cloud_service_ids` 與 `primary_cloud_service_id`（**本 finding 未提**）| `src/models/project_task.py:_cs_binding_fields` |
| `task.project_id` 搬動造成的靜默改綁（#715；**只掛 write 側**，掛兩側會讓 #705 F2 復發）| `src/models/project_task.py:_cs_binding_write_triggers` |
| 反向端 `cloud_service.project_ids`（**本 finding 未提**）| `src/models/cloud_service.py:_assert_reverse_binding_authorized` |

而且**新增與移除兩個方向**都套同一判準。移除方向不是對稱性潔癖：#698 E7——workspace 的 guest 解掉某 project 的最後一個關聯，`project_rule_workspace_member` 會從 bound 分支翻到 unbound 分支 ⇒ 整個 workspace 連 admin 都讀不到那個 project，一般路徑救不回。只守一半等於沒守。

### 🔴 更正：本 finding「非即刻可利用洞（主路徑已 gated）」那句是錯的

**原句保留在上方引言裡，不刪。** 那是 2026-07-04 當時的判斷；刪掉就看不出誤判在哪。

#698 實測：任何 `base.group_user`（**不需要屬於任何 workspace**）建一個自己的 project、對它下 `(4, <別的租戶的 service id>)` 就綁得上，只要知道那個整數 id。

根因是本 finding 沒有考慮到的框架層事實：Odoo 的 `Many2many.write_real` 對 `(3)` / `(4)` / `(5)` / `(6)` **只改 relation 表**（直接 SQL `INSERT` / `DELETE FROM project_cloud_service_rel`），程式碼路徑上**沒有任何 comodel 權限檢查——連 `read` 都不檢查** ⇒ 對 comodel 收 ir.rule（#676）對這幾個指令**完全無效**。

「controller 已 gated，所以只是 defense-in-depth」這個推論的前提是攻擊者必須經過 controller。ORM 直呼（`/web/dataset/call_kw`）不必。

### 三輪後續補洞

| Issue | 內容 |
| --- | --- |
| #705 F1 | create 閘門改看框架解析後的 vals；否則 `default_<field>` context ／ `ir.default` 整族來源都繞得過去（實測：payload 從 `args` 搬到 `kwargs.context`，#698 的主攻擊完整復原）|
| #705 F2 | 比對範圍收窄到「呼叫者這次指名的載體欄位」；否則 `task.primary_cloud_service_id` 這種 compute 導出值會被算成呼叫者新增的綁定，**誤擋合法操作** |
| #705 F3 | 反向端候選集：舊版只撈指令裡的 id ⇒ `(5, 0, 0)` 與 `(6, 0, [])` 的候選集是空的、**移除方向整條沒被檢查**，卻在同一份 docstring 裡宣稱自己是超集 |
| #715 | `task` move 旁路：guest 把 task 從 P1 搬到 P2，`primary_cloud_service_id` 靜默改綁，而守衛連 snapshot 都沒做（呼叫者一個載體欄位都沒指名）|
| #716 ① | 批次 create 的 key **聯集**誤擋：授權範圍必須跟著 vals 走，不跟著 batch 走 |
| #716 ② | 反向端候選集加 `active_test=False`，讓「候選集 ⊇ 實際變動」自己成立，不再掛在框架某個沒寫下來的行為上 |

### 測試

三個檔、**17 個 class／64 支測試方法**，**全部走真 HTTP RPC**（`/web/dataset/call_kw`）——ir.rule 與 `env.su` 豁免對 `TransactionCase` 的 superuser env 不生效，直呼 model 會系統性假綠。下方「驗證/測試要求」段要的「測 model constraint 本身、避免 `orm-vs-endpoint-acl-test-coverage` 假綠」因此被滿足，而且比它要求的 `with_user()` 更強（不是沒做，是做了更強的）。

- `src/tests/test_project_cs_m2m_authz_698.py:TestBindingGuardPositiveControls`（陽性對照：owner／admin 綁得上、解得掉）
- `src/tests/test_project_cs_m2m_authz_698.py:TestBindingGuardAddDirection`、`src/tests/test_project_cs_m2m_authz_698.py:TestBindingGuardRemoveDirection`、`src/tests/test_project_cs_m2m_authz_698.py:TestBindingGuardCreatePath`、`src/tests/test_project_cs_m2m_authz_698.py:TestBindingGuardOtherEntries`
- `src/tests/test_project_cs_m2m_authz_698.py:TestBindingGuardDownstream`（下游不再被污染）、`src/tests/test_project_cs_m2m_authz_698.py:TestCloudServiceRuleStillGuards676`（#676 那三格仍由 cloud_service 的 rule 擋、沒被本修法取代）、`src/tests/test_project_cs_m2m_authz_698.py:TestReverseEntryFromServiceSide`
- `src/tests/test_project_cs_m2m_authz_705.py:TestCreateValueSourcesF1`、`src/tests/test_project_cs_m2m_authz_705.py:TestCreateOtherValueSourcesF1`、`src/tests/test_project_cs_m2m_authz_705.py:TestCreateComputeFalsePositiveF2`、`src/tests/test_project_cs_m2m_authz_705.py:TestReverseCandidateSetF3`、`src/tests/test_project_cs_m2m_authz_705.py:TestCrossProjectMoveReasonF6`
- `src/tests/test_project_cs_m2m_authz_715_716.py:TestTaskMoveRebindsPrimary715`、`src/tests/test_project_cs_m2m_authz_715_716.py:TestBatchCreateUnion716`、`src/tests/test_project_cs_m2m_authz_715_716.py:TestReverseCandidateArchivedProject716`、`src/tests/test_project_cs_m2m_authz_715_716.py:TestNestedCreateCommand0Claim716`

### 殘留（**不**在本 finding 內解決）

- **守衛豁免 `env.su`**（`src/models/cloud_service_binding_guard.py:_cs_binding_assert` 開頭那個 early return，反向端同形）。檔頭有論證：seed／migration／cron／controller 的 sudo 產品路徑（例如 `ai_assistant._create_project`）照常，守衛堵的是 ORM 直呼那條旁路。但本 finding 原文把 `.sudo()` 明列為風險路徑之一 ⇒ 這一格**併入 #870 討論**（該單留言的「第二個實例」段），不另開單、也不在這裡解決。
- **create 側還有兩條 leg 沒有測試守著**（#777，open；**test-gap 不是 security bug**）：守衛的 create 閘門看的是 `_add_missing_default_values` 的產物，而 `default_get` 的第三個來源「欄位自身的 `default=`」推導上被涵蓋、卻從未被任何測試證實（唯一一次嘗試證實的探針沒命中 RPC 走的 registry class 實例）；`precompute=True` 的 stored compute 更是**根本不在那份 vals 裡**——`odoo/models.py:_prepare_create_values` 先呼叫 `_add_missing_default_values`、之後才呼叫 `_add_precomputed_values`（Odoo 18 原始碼順序，本次對過）⇒ 閘門整道不觸發，與 #698 原始缺陷、#705 F1 是同一個形狀，只是換了載體。今天不可利用：五個受保護／觸發欄位（`cloud_service_ids`／`cloud_service_id`／`extra_cloud_service_ids`／`primary_cloud_service_id`／`project_ids`）都沒有 `default=`、也沒有 `precompute=True`。⚠ 但 `src/models/project_task.py:primary_cloud_service_id` 已經是 stored compute，離 `precompute=True` 只差一個關鍵字——效能調優時很自然會有人加上去，而加的人不會知道他同時關掉一道授權閘。修法（補測試／改守衛／repo-lint 加一條）未裁決。
- ⚠ 連帶要小心讀：守衛檔頭那句界線「guest／user 連自己 workspace 的 service 也不能綁」**在產品主路徑上不成立**——`POST /api/support/cloud-services/<id>/project` 走 sudo，它的閘只驗成員資格、不驗 role ⇒ 同一件事兩條路兩個界線。追蹤在 **#711**（待裁，不是本 finding 的功課）。

### 授權分佈表：翻轉的是哪一列

下方那張表**保留原值**（歷史）。今天翻轉的是 **model 那一列**：原文寫 `❌ 無 @api.constrains / write() guard（只有 compute）`，今天是 ✅ 有守衛——但形狀不是 `@api.constrains`，是 mixin 的 `create` / `write` override 加 snapshot-diff。同列後半「`cloud_service.py` 亦無 workspace 一致性 guard」也翻了，但翻成的是**反向端的前置授權檢查**，不是一致性 guard（Model B 刻意不加 same-workspace 限制）。

其餘三列（**controller create**／**controller update**／**ir.model.access.csv**）**沒有翻轉**，維持原文所述。

---

## 事實

`project.workspace_id(s)` 從 `cloud_service_ids` 綁定推導（`project_project.py:141-149`：`workspace_ids = cloud_service_ids.mapped('workspace_id')`）。綁定授權目前分佈：

| 層 | 現況 |
| --- | --- |
| **controller create**（`ai_assistant.py:1523 _create_project` via `api_support_projects:1300`）| ✅ gated：`1329 sudo().browse(cloud_service_id)`（繞 CS ir.rule）緊接 **`1332 _check_workspace_access(cloud_service.workspace_id)`** → 驗寫入者是該 CS workspace 成員才綁 |
| **controller update**（`_update_project:1578`）| `vals` 只收 name/description、**不 rebind CS** → 無 gap（也無 rebind UX）|
| **model**（`project.cloud_service_ids`）| ❌ **無 `@api.constrains` / `write()` guard**（只有 compute）；`cloud_service.py` 亦無 workspace 一致性 guard |
| **ir.model.access.csv** | project M2M 對 `base.group_user` 全開（授權靠 ir.rule + controller，非 model guard）|

## 風險

授權是 **controller-only**、無 model-level defense-in-depth：任何**其他/未來寫 `cloud_service_ids` 的路徑**（新 endpoint、data import、`.sudo()` 內部流程、admin action、XML-RPC）**沒複製 `_check_workspace_access` 就開跨租戶綁定洞**。諷刺：`_update_project:1583` 註解自標「two-layer-acl-lockstep invariant #4」守 name/description editable gate，卻**沒有對等 model-level 守綁定授權本身**——正是該 pattern 警告的「單層 ACL、他層 silent divergence」。

一旦跨 workspace 綁定成立，`project.workspace_ids` 變多值 → agent 的 `egressWorkspaceIds`（leg-1 NP）跨 workspace 放行 → **NP 租戶隔離被資料層繞過**（NP 正確、但輸入不可信）。

## stg 現況（2026-07-04 雙查收斂）

12 agent project **全部 `cloud_service_ids=[]`、0 跨-workspace**（leader `multi_ws=0/12` + docs-leader）。→ 跨-ws 綁定**零使用** → 加 constraint **零回歸**。

## 建議

加 model-level **`@api.constrains('cloud_service_ids')`**（defense-in-depth，authority check）：

**✅ Eugene 拍板 Model B（允許跨 workspace 綁定，保留產品功能）**（2026-07-04）→ constraint **只做 authority check、不加 same-workspace 限制**：綁的**每個** CS 的 workspace 都必須有寫入者為成員（`workspace_access` 查核）。跨 workspace 綁定合法，前提是寫入者是**每個**目標 workspace 的成員。**不可**用 `len(workspace_ids) <= 1`（會砍「support project 同時達多 workspace CS」產品功能）。

> 早期曾因 stg 零使用傾向 Model A（same-workspace constraint），Eugene 2026-07-04 否決：support project 須能同時達多 workspace 的 CS。故 leg-2 走 per-reachable-workspace label `woow.tw/reach-ws-<W>`（見 #279 spec §7）。

## 驗證/測試要求

- backend `TestCase` + `with_user()`：非成員 user 綁跨 workspace CS → `ValidationError`（不可只測 controller，須測 model constraint 本身，避免 [[orm-vs-endpoint-acl-test-coverage]] 假綠）。
- Model B：寫入者為成員的跨 workspace 綁定**放行**；綁到寫入者**非成員** workspace 的 CS → `ValidationError` fail-loud。
