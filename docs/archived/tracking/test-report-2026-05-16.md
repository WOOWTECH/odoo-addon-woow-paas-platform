---
name: test-report-2026-05-16
phase: red-explained
team: refactor
test_explainer: refactor-test-explainer
plan_ref: docs/plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md
test_scope_ref: docs/archived/tracking/test-scope-report-2026-05-16.md
created: 2026-05-17T00:00:00Z
updated: 2026-05-17T00:00:00Z
status: archived
archived-from: docs/tracking/test-reports/test-report-2026-05-16.md
archived: 2026-07-19
archived-reason: "一次性測試報告，對應功能已上線"
---

# Test Report — CloudService / Project Decouple（人類可讀版）

> 給 reviewer / stakeholder 的「測試導讀」：不要把這份當 spec 重讀（spec 是 plan），把它當 **「為什麼這次 PR 這樣測、不那樣測」** 的對照地圖。讀完後你應該能對外說出「這次保護了哪些行為、放掉哪些行為、為什麼」。

---

## Overview — 這次 refactor 在做什麼

把 Odoo 端的 `project.project` 從「**必須**綁一個 `cloud_service`」鬆綁為「**可以**綁也可以不綁」，並補上 task 層級的多 service 參考（extras）與 AI prompt 注入時的 secret 遮罩。9 個決策一句帶過：

| # | 決策 | 一句重點 |
|---|---|---|
| D1 | Project ↔ Service 關係 | **B** — 留 `unique_cloud_service` constraint，只把 ACL 跟前端鬆綁；schema 完全可逆 |
| D2 | Task ↔ Service 連結 | **A** — Task 加 `extra_cloud_service_ids` Many2many + `primary_cloud_service_id` related，用 M2M 表 `project_task_cloud_service_rel` |
| D3 | unbound Project ACL | **A** — Project record rule 改為 project member 可見（拔掉 `cloud_service_id = False` 短路） |
| D4 | UI 入口 | **C** — `/woow#/ai-assistant` 與 `/woow#/projects` 雙路徑都指向 Projects 列表，保留書籤 |
| D5 | CreateProjectModal | **A** — service 下拉首項「No service」，移除必選 gate |
| D6 | Extras 候選範圍 | **C** — 只能同 workspace；跨 workspace 留 follow-up |
| D7 | AI prompt 注入 extras | **A** — Primary + Extras 合併注入，primary 在前、去重 |
| D8 | cloud_service ACL gap | **A** — 補 `cloud_service_rule_workspace_member` record rule（**新規 ACL，本次必補**） |
| D9 | Secret 遮罩策略 | **A → B** — 本次先 pattern sanitizer + 三欄位 blacklist，redact 字串 `***REDACTED***`；whitelist 模式留 follow-up |

**Tester 已交付的紅燈現況**（細節見 `test-scope-report-2026-05-16.md`）：

- **25 個 refactor-scope 紅燈** — 失敗模式正確（`AssertionError` / `KeyError` / `AttributeError`），不是 syntax / import / fixture broken
- **7 個 regression 綠燈** — 守住 D1=B 不能誤刪 constraint、D8=A 補規則不能 break 既有 controller 等
- **32 個 pre-existing infra 紅燈** — `Subscription journal is not configured`，**out of scope，本次不碰**

---

## AC × Test mapping（25 條）

下表把 plan §Acceptance criteria 的 25 條與 tester 的 test 一一對齊，並對每條解釋「**為何選這個 boundary**」。讀者可以照表逐條問「這個 boundary 有沒有過嚴 / 過寬」。

### 群組 A：Project 獨立性（AC #1-3）

| # | Criteria | Test | File:Line | Why this boundary |
|---|---|---|---|---|
| 1 | 可建 unbound project | `TestProjectDecouple.test_ac1_create_unbound_project_is_allowed` | `src/tests/test_project_decouple.py:48` | **Baseline lock**：今天 ORM 已允許 nullable，但若未來誰把欄位改 NOT NULL 立刻爆掉。順便 assert `workspace_id` computed 為 False — unbound 必然沒 workspace（plan §現況：workspace_id computed via cloud_service）|
| 2 | unique constraint 不退化 | `TestProjectDecouple.test_ac2_unique_cloud_service_constraint_preserved` | `src/tests/test_project_decouple.py:64` | **D1=B 的核心 guard**：plan 明說「不擴 1:N」；若 implementer 誤讀為「可以拔 constraint」，這條會擋住。用 `IntegrityError` + `mute_logger` 是因為 Odoo SQL constraint 真的會丟 DB-level 錯，這是最忠實的契約 |
| 3 | bound project workspace_id compute chain 不變 | `TestProjectDecouple.test_ac3_bound_project_workspace_id_unchanged` | `src/tests/test_project_decouple.py:79` | **Regression**：plan 把 D1=B 列為「零 schema migration」，意思是 `workspace_id` 仍走 `cloud_service_id.workspace_id` 關聯。**測 service 改 workspace 後 project 也跟著變**，這是 related compute 必須仍是 reactive 的 invariant；若有人改成 stored 不 recompute，stg 現有 row 就會錯 |

### 群組 B：Project ACL（unbound 隔離，AC #4-6）

| # | Criteria | Test | File:Line | Why this boundary |
|---|---|---|---|---|
| 4 | unbound project 對非 member 不可見 | `TestProjectUnboundACL.test_ac4_unbound_project_invisible_to_non_member` | `src/tests/test_project_decouple.py:178` | **這次最關鍵的安全 fix**：plan 指出 `ir_rules.xml:55` 的 `cloud_service_id = False` 短路是多租戶外洩；test 直接讓 Bob `search` 一個不屬於他的 unbound project，必須拿到空集合。**用 search 而非 read 是因為 record rule 在 search 層就會過濾**，這是 Odoo ACL 的真實 contract surface |
| 5 | 加 member 後 unbound project 可見 | `TestProjectUnboundACL.test_ac5_unbound_project_visible_after_member_add` | `src/tests/test_project_decouple.py:195` | **D3=A 的正面測**：拔掉短路會不會「全 unbound 都看不到」？這條保護「member 可見」這條腿。Tester 故意對 `user_ids` 和 `message_partner_ids` 都 add（plan D3=A 留兩種可能），讓 test portable — 見 Open Q1 處置 |
| 6 | bound project 對 workspace member 仍可見 | `TestProjectUnboundACL.test_ac6_bound_project_visible_to_workspace_member` | `src/tests/test_project_decouple.py:209` | **Regression**：D3=A 改寫 rule 後 bound leg 不能被誤殺。Bob 是 workspace member 卻不是 project member，bound project 仍要可見（這是 Odoo 的標準 paas 心智模型，不能因為改 unbound 規則連帶 break）|

### 群組 C：cloud_service ACL 補強（AC #7-8）

| # | Criteria | Test | File:Line | Why this boundary |
|---|---|---|---|---|
| 7 | 跨 workspace 不可 read / write / unlink | `TestCloudServiceWorkspaceRule.test_ac7_bob_cannot_read_w1_service_via_orm` 等 5 條 | `src/tests/test_cloud_service_acl.py:75-115` | **Plan §現況指出的 pre-existing gap**：ACL CSV 給 `base.group_user` RWCU，但完全沒 record rule。D2=A 引入 Task M2M widget 會把這 gap 浮上水面（widget 直查 ORM 不走 controller）。Test 拆成 5 條（search-empty / browse-raise / write-raise / unlink-raise / own-ws-positive）是因為 **Odoo 不同 ORM 路徑會走不同 enforcement**：search 過濾、browse+read 拋 `AccessError`，要分別 cover 才能保證所有 widget 都安全 |
| 8 | controller endpoint 對 member 不變、對 outsider 仍拒 | `TestCloudServiceControllerRegression.test_ac8_list_services_endpoint_unchanged_for_member` + `_denied_for_outsider` | `src/tests/test_cloud_service_acl.py:178-203` | **Regression** + plan R4 風險：D8=A 補 ir.rule 後，原本只靠 `workspace.check_user_access` 把關的 controller 行為**必須完全一樣**。Test 直接 patch `request` 走 controller method（非 HTTP），避免 routing / session 雜訊 — 純測 ACL 與 controller 的 ORM 互動 |

### 群組 D：Task ↔ Service extras（AC #9-13）

| # | Criteria | Test | File:Line | Why this boundary |
|---|---|---|---|---|
| 9 | `extra_cloud_service_ids` Many2many 欄位存在且可空 | `TestTaskExtrasFieldsExist.test_ac9_extra_cloud_service_ids_field_exists` + `_extras_can_be_empty` | `src/tests/test_task_service_extras.py:89-110` | **Schema contract**：assert `_fields` dict 而非 attribute，因為實作可能 lazy-define；同時 assert `type == 'many2many'` 與 `comodel_name == 'woow_paas_platform.cloud_service'`，避免實作誤用 Many2one / 指錯 model。`_extras_can_be_empty` 守 D2=A 「可空」的語意 |
| 10 | `primary_cloud_service_id` 是 `task.project_id.cloud_service_id` 的 `store=True` related | `TestTaskExtrasFieldsExist.test_ac10_primary_cloud_service_related_to_project` + `_updates_when_project_rebound` | `src/tests/test_task_service_extras.py:114-141` | **`store=True` 語義保護**：plan 講「store=True，project 改綁 service 後 task 此欄位同步更新」。Test 跑兩段：(a) 初始值等於 project 的 service；(b) 改 project 綁定後 invalidate + 重讀仍正確。**不直接測 related 本身**（那是 framework），而是測 propagation 的 timeline，因為這直接影響 AC #14 的「primary 從哪來」 |
| 11 | 候選清單只列同 workspace 的 service | `TestTaskExtrasDomain.test_ac11_extras_field_has_workspace_scoped_domain` | `src/tests/test_task_service_extras.py:151` | **D6=C 的最弱形式契約**：test 只 assert `'workspace_id'` 出現在 `field.domain` 的 `repr()`，刻意不約束 static / dynamic domain — 見 Open Q2 處置。**為何不直接測「candidate list 渲染出來只有同 ws」**？因為那需要起 OWL，太 heavy；domain string 比對是最便宜又能擋住「忘了加 domain」的方式 |
| 12 | 跨 workspace ORM write 被擋 | `TestTaskExtrasRecordRuleEnforcement.test_ac12_cross_workspace_extra_invisible_or_rejected` + `_same_workspace_extra_accepted` | `src/tests/test_task_service_extras.py:190-223` | **D8=A 連帶 enforce**：cloud_service record rule 上線後，跨 ws service 對 Alice 不可見，M2M write 應該失敗或被靜默 drop。Test 對三種實作都包容：`AccessError` / `ValidationError` / silent-drop（讀回 ids 不含跨 ws id）。**為何容許三種**：plan 沒拍板 enforcement 通道，先讓 implementation 自選，安全性等價 |
| 13 | 跨 project 移動後 extras 保留，UI 顯示警告 chip | `TestTaskCrossProjectMove.test_ac13_extras_preserved_on_cross_project_move` + `_incompatible_extras_flag_exposed` | `src/tests/test_task_service_extras.py:245-277` | **AC #13 拆兩部分**：(a) server 不能 silent-drop（不然警告 chip 沒得顯示）；(b) model 必須有一個欄位告訴 UI「哪幾個 extras 現在不相容」，否則前端無從畫 chip。Test 對 warning 欄位名三選一接受（`incompatible_extra_cloud_service_ids` / `has_incompatible_extras` / `extra_cloud_service_warning`）— 見 Open Q3 處置 |

### 群組 E：AI prompt 注入 + secret 遮罩（AC #14-19）

| # | Criteria | Test | File:Line | Why this boundary |
|---|---|---|---|---|
| 14 | primary + extras 合併注入，primary 在前、去重 | `TestPrimaryAndExtrasMerge.test_ac14_primary_then_extras_merged_with_dedup` | `src/tests/test_chatter_service_injection.py:91` | **D7=A 的順序 + dedup 契約**：test 故意把 primary 也塞進 extras，要 assert 它只出現一次（dedup）；再用 `context.index('Primary Svc') < context.index('Extra Svc')` assert 順序。**沒有測「兩段之間的精準 delimiter」**，因為 plan 沒約束 prompt 模板長相 — 給 implementation 空間決定排版 |
| 15 | helm_values 中符合 `(?i)(password\|secret\|token\|api[_-]?key\|credential)` 的 value 被 `***REDACTED***` 取代 | `TestHelmValuesSanitizer.test_ac15_helm_values_password_redacted` + `_token_and_api_key_patterns_redacted` | `src/tests/test_chatter_service_injection.py:186-228` | **Plan D9=A 的核心 unit test**：兩條測涵蓋兩個面向 — (a) `password` key 被 redact 且 redacted 字串為 `***REDACTED***` literal；(b) `token` / `api_key` / `client_secret` / `credential` 四個 pattern 都生效。**為何同時測 sentinel literal 與 raw value 不存在**：implementation 可能光 strip 不替換（讀者看不出有 secret 存在）或替換成空字串（破壞 JSON）；雙 assertion 同時防呆 |
| 16 | `n8n_api_key` / `n8n_owner_password` / `mcp_auth_token` 即使透過 `sudo()` 也不注入 | `TestHelmValuesSanitizer.test_ac16_blacklisted_fields_never_injected` | `src/tests/test_chatter_service_injection.py:231` | **Plan D9 明確 blacklist 三欄位**：把三個欄位名直接塞進 `helm_values` JSON，模擬「未來 chart 把這些欄位也順手寫進 values」。Test 不檢查 sentinel（不要求一定要 redact，這三個欄位可以**完全略過**），只要保證 value literal 不出現。**為何**：blacklist 比 pattern 強 — pattern 只擋 key 名匹配，blacklist 是「就算 key 不匹配 pattern 也不能注入」的硬截止 |
| 17 | unbound + extras → 只注入 extras 段落 | `TestPrimaryAndExtrasMerge.test_ac17_unbound_with_extras_injects_extras_only` | `src/tests/test_chatter_service_injection.py:125` | **修補 plan §現況 `discuss_channel.py:225-226` 的早 return**：今天 unbound project 直接 return `''`，extras 拿不到。Test assert (a) context 非空、(b) 出現 `'Extra Svc'`、(c) 不出現 `'Primary Svc'`。**第三條最重要**：implementer 不可以為了過第一條就硬塞 "Primary: None" 之類的 placeholder，那會誤導 AI |
| 18 | bound + 無 extras → 只注入 primary | `TestBoundNoExtrasRegression.test_ac18_bound_no_extras_only_primary` | `src/tests/test_chatter_service_injection.py:164` | **Regression**：今天的 happy path 不能被改壞。Plan §現況指出這就是目前 `_get_cloud_service_context` 唯一能跑的分支 |
| 19 | unbound + 無 extras → 完全沒有 service 段落 | `TestPrimaryAndExtrasMerge.test_ac19_unbound_no_extras_no_service_section` | `src/tests/test_chatter_service_injection.py:147` | **Regression** + 防呆：今天 return `''`，明天可能被 implementer 改成 `"=== Cloud Services ===\n(none)"` 之類「友善但是多餘」的輸出，浪費 AI token + 誤導模型。Assert `context` falsy 是最嚴格 contract |

### 群組 F：UX 前端（AC #20-25）

| # | Criteria | Test | File:Line | Why this boundary |
|---|---|---|---|---|
| 20 | `/projects` 與 `/ai-assistant` 路徑共存 | `TestRouterAlias.test_ac20_router_declares_projects_path` + `_keeps_ai_assistant_for_bookmarks` | `src/tests/test_ux_routing_views.py:39-64` | **D4=C 雙路徑 alias**：test 用 regex 對 `router.js` 原始檔比對 `path: "projects"` 與 `path: "ai-assistant"` 都存在。**為何用 file-content 而非起 OWL**：plan §E2E 明說「browser hash round-trip 留 playwright-cli」；file-content 是最便宜且足以擋「忘了加 alias / 誤刪舊路徑」這兩種錯 |
| 21 | Sidebar 顯示 `Projects` literal | `TestSidebarEntry.test_ac21_sidebar_navitem_renamed_to_projects` | `src/tests/test_ux_routing_views.py:70` | **Open Q4 拍板「保留英文 Projects」**：test regex 比對 `name: "Projects"`。**沒測「移除 AI Assistant 文字」**：Sidebar 可能保留它做其它用途，過嚴會誤殺 |
| 22 | CreateProjectModal 首項「No service」 | `TestCreateProjectModal.test_ac22_modal_offers_no_service_option` | `src/tests/test_ux_routing_views.py:91` | **D5=A 必要 UI hook**：assert `"no service"` 或 `"unbound"` 字串出現於 modal 的 JS 或 XML（lowercase 比對）。**為何容許兩種文字**：plan 沒拍板 literal 是 "No service" / "— No service —" / "Unbound"，給 i18n 空間 |
| 23 | 移除 `has_project` filter 與必選 gate | `TestCreateProjectModal.test_ac23_modal_removes_has_project_filter` + `_no_longer_requires_service_selection` | `src/tests/test_ux_routing_views.py:112-144` | **D5=A 的兩個前端代碼除錯**：(a) regex 拒絕 `!svc.has_project` 寫法；(b) literal 拒絕 `'Please select a cloud service'`。**為何分兩條**：分別對應 plan §現況講的 `CreateProjectModal.js:68-71` 過濾與 `:124-132` 必選 — 一條過了另一條沒過，意義完全不同 |
| 24 | Projects 列表頁顯示 bound/unbound badge | `TestProjectsListBadge.test_ac24_projects_page_shows_binding_state` | `src/tests/test_ux_routing_views.py:150` | **D4=C 的 UX 必要可見性**：assert `'unbound'` literal 出現在 SupportProjectsPage 的 JS 或 XML。**沒測 grid layout / column 排序**：那屬於視覺打磨，不在 AC #24 的契約裡 |
| 25 | Task form 顯示 Linked Services panel（含 primary + extras 兩欄位） | `TestTaskFormLinkedServicesPanel.test_ac25_task_form_xml_declares_extras_panel` | `src/tests/test_ux_routing_views.py:173` | **D2=A 的 UI binding 契約**：assert `project_task_views.xml` 同時包含 `extra_cloud_service_ids` 與 `primary_cloud_service_id`。**沒測 widget 細節**：plan Open Q3 已說「panel 細部 UX 留給 implementation」 |

---

## Regression 守線（7 條，明示要守的舊行為）

這 7 條 test 在紅燈階段就是綠的，**它們的價值不在「現在過」**，而在「**implementation 動完之後仍然要過**」。reviewer 在最終 diff 應該特別注意這些路徑沒有被誤改。

| # | 守住的既有行為 | Test | 為何 break 會有問題 |
|---|---|---|---|
| R1 | `_sql_constraints['unique_cloud_service']` 不被誤刪 | AC #2 `test_ac2_unique_cloud_service_constraint_preserved` | D1=B 的承諾就是「不擴 1:N」；constraint 一掉，service 多綁 project 變合法、未來 rollback 變不可逆（資料污染） |
| R2 | `cloud_service_id.workspace_id` related compute 鏈仍 reactive | AC #3 `test_ac3_bound_project_workspace_id_unchanged` | 若改 stored 不 recompute，stg 既有 service 換 workspace 時 project 的 `workspace_id` 不會同步，ACL 立刻錯亂 |
| R3 | bound project 對 workspace member 仍可見 | AC #6 `test_ac6_bound_project_visible_to_workspace_member` | D3=A 改寫 rule 是高風險動作；若 unbound leg 改錯把 bound leg 也吃掉，stg 既有使用者「東西不見」 |
| R4 | controller list endpoint shape 不變（member 路徑） | AC #8 `test_ac8_list_services_endpoint_unchanged_for_member` | D8=A 新增 ir.rule 後，原本 controller 走的 ORM read 必須仍能拿到 own ws service；plan R4 風險點 |
| R5 | controller 對 outsider 仍拒絕 | AC #8 `test_ac8_list_services_denied_for_outsider` | `workspace.check_user_access` 既有 barrier 不能因為「反正 ir.rule 會擋」就拔掉 — 雙保險 |
| R6 | bound + 無 extras → AI prompt 只注入 primary | AC #18 `test_ac18_bound_no_extras_only_primary` | 目前唯一能跑的分支；rewrite `_get_cloud_service_context` 後若 happy path 退化，所有現有 Task chatter 都受影響 |
| R7 | unbound + 無 extras → AI prompt 不出現 service 段落 | AC #19 `test_ac19_unbound_no_extras_no_service_section` | implementer 修 AC #17 時很容易順手塞 `"=== Cloud Services ===\n(none)"`，浪費 token + 誤導模型 |

---

## 刻意不測（with reason）

以下範圍 tester **故意沒測**，每條附理由。reviewer 看到這些動作出現在 diff 內請特別 push 一下 — 不是不該做，而是該被搬到後續 PR / E2E layer。

| 未測範圍 | 為何先不測 |
|---|---|
| **Service 1:N Project** | plan §Rejected#1 已否決；測「能不能 1:N」與本次 D1=B「不能 1:N」契約**自相矛盾** |
| **Task 直接 FK 到 Service（跨 project）** | plan §Rejected#2-3：D2=A 用 M2M extras 取代；直接 FK 變成另一條 schema 路徑，不在本次 surface |
| **跨 workspace extras 候選** | D6=C 明確收緊到同 workspace；未來放寬時是另一條 AC，本次不預埋反向 test 製造混亂 |
| **Project audit log** | plan §Out of scope；本次無此需求，加 test 等於默許 scope 蔓延 |
| **Sidebar i18n / zh-tw label** | Open Q4 = 保留英文 literal；無 translation entry 可測 |
| **helm_values 嵌套 >5 層 deep recursion** | plan §刻意不測：reasonable depth limit；實務上 helm_values 通常 2-3 層；deep recursion 屬 unit test 邊角，不在 acceptance surface |
| **AI 回覆品質**（sanitize 後 AI 還能不能回答正確） | sanitizer 的 contract 是「輸入 → 輸出」，AI 回覆品質牽涉 model 行為，本次測穩定 |
| **`/woow#/ai-assistant` ↔ `/woow#/projects` browser hash round-trip** | plan §E2E：headed browser 成本太高；file-content test 已守住 router declaration，hash 行為留 playwright-cli |
| **Linked Services panel headed render** | 同上，plan §E2E；本次測 xml binding 已足以擋「忘了把欄位放進 view」 |
| **paas-operator service 端任何變動** | plan §Out of scope：本次純 Odoo 模組層 |
| **`has_project` controller flag 移除** | plan §Affected files 明示「保留」做 UI 顯示用，**不**測「被移除」（過嚴會誤殺 `has_project` 的合法使用） |
| **AI prompt 模板字串 / delimiter / 排版** | 給 implementation 空間；AC #14 只測「primary 在前、dedup、extras 也出現」，不測 prompt 長相 |

---

## Reviewer 對照指引（test → expected diff hotspot）

當 implementation 階段交付 PR，reviewer 拿這份 report 對照 diff，可以照下表逐區檢查每個 AC 對應的 src 變動是不是落在「**該動的檔**」。檔案路徑來自 plan §Affected files。

| AC | 預期 diff 落點（依 plan 推測） | Reviewer 對照重點 |
|---|---|---|
| #1-3 | `src/models/project_project.py`（可能僅補註解，constraint 不動） | constraint 不能被移除；若有改動需保留 `_sql_constraints['unique_cloud_service']` |
| #4-6 | `src/security/ir_rules.xml`（改寫 `project_rule_workspace_member`） | **`('cloud_service_id', '=', False)` 短路必須移除**；新規則需處理 bound leg + unbound leg 兩段 |
| #7-8 | `src/security/ir_rules.xml`（**新增** `cloud_service_rule_workspace_member`） | XML 新節點 + record rule 載入 noupdate；plan §Migration 講「需確認 stg 是否手動改過既有 rule」|
| #9-10 | `src/models/project_task.py`（新增 `extra_cloud_service_ids` + `primary_cloud_service_id`） | `primary` 必須是 `related='project_id.cloud_service_id', store=True`；M2M `comodel='woow_paas_platform.cloud_service'`，預設關聯表 `project_task_cloud_service_rel` |
| #11 | `src/models/project_task.py`（欄位 `domain=` 參數 / `@api.onchange`） | domain 必須引用 `workspace_id`；見 Open Q2 — implementation 須拍板 static / dynamic |
| #12 | 隱性依賴 AC #7-8 的 cloud_service record rule + ORM enforcement | 不需要新 code（除非 implementation 另外加 `@api.constrains`）；若 implementation 加了 constraint，要對照 test 容許的三種 enforcement 路徑 |
| #13 | `src/models/project_task.py`（cross-project move 不 strip extras + warning 欄位） | warning 欄位名見 Open Q3；reviewer 須確認 PR description 解釋「為何選 X 不選 Y」 |
| #14, #17-19 | `src/models/discuss_channel.py:216-274`（改寫 `_get_cloud_service_context`） | 改寫後需 cover primary / extras / unbound+extras / unbound+empty 四條分支；dedup 邏輯需明確（用 `set` 或 `ids in primary` skip） |
| #15-16 | `src/models/discuss_channel.py` 內新 helper `_sanitize_helm_values` + blacklist | sanitizer 應該是純函式（傳 dict / json string 進、傳 redacted dict / string 出），無 side effect；blacklist 三欄位 hardcode |
| #20 | `src/static/src/paas/core/router.js`（新增 `projects` 路由 alias） | 兩個 `path:` literal 都要存在，導向同一個 component |
| #21 | `src/static/src/paas/layout/sidebar/Sidebar.js`（label literal `Projects`） | 若舊 `AI Assistant` 字串還要保留請說明用途 |
| #22-23 | `src/static/src/paas/components/modal/CreateProjectModal.{js,xml}` | (a) 移除 `:68-71` 的 `has_project` filter；(b) 移除 `:124-132` 的必選 gate；(c) 下拉首項 literal 「No service」/「Unbound」 |
| #24 | `src/static/src/paas/pages/support-projects/SupportProjectsPage.{js,xml}` | `unbound` literal 出現（badge / label 任一） |
| #25 | `src/views/project_task_views.xml` | 同時出現 `extra_cloud_service_ids` 與 `primary_cloud_service_id` 兩個欄位名 |

---

## Open Qs 處置（4 條）

每條 open Q 都給：(a) test-explainer 的解讀、(b) implementation 階段須拍板的事項、(c) 是否需要回退 planner 重定。

### Open Q1 — AC #5 enforcement choice：`user_ids` vs `message_partner_ids`

**Plan D3=A 留兩個可能性**：unbound project record rule 的「member 可見」可走 `user_ids` 或 `message_partner_ids`；test 對兩個 axis 都 add 以保持 portable。

- **(a) 解讀**：tester 做對了 — dual-add 不會 over-specify，無論 implementation 選哪一個 test 都能通過。**不算 ambiguity，是刻意的彈性**。
- **(b) implementation 階段須拍板**：選一個（建議 `user_ids`，貼近 Odoo 原生 `project.project` 的 follower model）。PR description 須講明選擇理由。
- **(c) 是否回退 planner**：**不需要**。Plan 已明確列為可選；test 已保證安全網。

### Open Q2 — AC #11 domain form：static 或 dynamic

**Plan 沒指定** `extra_cloud_service_ids` 的 domain 是 static literal（如 `[('workspace_id', '=', project_id.workspace_id.id)]`）或 dynamic（透過 `@api.onchange` / `_compute_domain`）。Test 只 assert `'workspace_id'` 出現在 `field.domain` 的 `repr()`。

- **(a) 解讀**：兩種實作對 UI 候選清單的 UX 一樣；差別在「domain 是欄位定義時固定的」vs「runtime 算出」。Static domain 在 task 沒 project 時會壞掉（`project_id.workspace_id` 是 NoneType），dynamic 較 robust 但複雜。**Test contract 是最弱形式：domain 不能為空、必須提到 workspace_id**。
- **(b) implementation 階段須拍板**：建議 dynamic — task 可能 unbound（project 沒綁 service ⇒ workspace_id 為 False），static domain 在那情況下要 evaluate `False.id` 會 AttributeError。Implementation 應在 PR 標記「domain 走 dynamic / onchange，理由：unbound project 的 NoneType safety」。
- **(c) 是否回退 planner**：**不需要**。Plan 給的彈性是合理的；test 已守住最小契約。

### Open Q3 — AC #13 warning-field naming

Test 接受三選一：`incompatible_extra_cloud_service_ids` / `has_incompatible_extras` / `extra_cloud_service_warning`。

- **(a) 解讀**：三個候選代表三種設計：(i) M2M 列出不相容的 service ids；(ii) Boolean 旗標僅標「有不相容」；(iii) 字串訊息直接給 UI 用。哪個適合 UI 要看 panel 想顯示多少資訊（chip 一個 vs 多個 vs 文字 banner）。
- **(b) implementation 階段須拍板**：建議 `incompatible_extra_cloud_service_ids`（computed M2M）— 對應 panel 可以對每個不相容的 chip 加紅框，最豐富的 UI 資訊量；其餘兩個是退化形式。Implementation 須在 PR description 寫明選擇與 UI rendering 對應關係。
- **(c) 是否回退 planner**：**不需要**。Plan §Open Q3 已說「Linked Services panel 細部 UX 留給 implementation」，這條屬於 panel 細節。

### Open Q4 — AC #15 redaction sentinel：`***REDACTED***`

Test 用 `***REDACTED***` literal exact match；plan D9 同寫死。

- **(a) 解讀**：當前無歧義；若未來 i18n 化，sentinel 變成 translation key，test 和 plan 都需同步調整。
- **(b) implementation 階段須拍板**：照 plan 直接 hardcode `***REDACTED***`；**不要**為了「未來 i18n」預先抽出常數 — 那屬於 D9 階段 2（whitelist 升級）才會碰到的事。
- **(c) 是否回退 planner**：**不需要**。reporter 階段把「未來 i18n 化 sentinel 時需同步 plan + test」寫入 follow-up 即可。

---

## 隱性假設 / Fixture 條件

讀者讀 test code 時容易忽略以下隱性條件，影響 reviewer 對 test 的可信度判斷：

- **Test runner**：`./.claude/skills/k8s-dev-sandbox/scripts/test.sh feat-decouple-cs-projects --module woow_paas_platform`，sandbox `dev-sandbox-feat-decouple-cs-projects`（docker-desktop, local hostPath，TTL +24h）。
- **DB 隔離**：所有 test 用 `TransactionCase`，每個 test 自動 rollback；不留 persistent state。
- **無外部網路**：無 helm / k8s / paas-operator / AI provider 呼叫。AI 注入測試直接 call `_get_cloud_service_context()` string-producing helper，**不**走 `_post_ai_reply` → AI provider。
- **使用者建立**：所有 test 自建 `res.users` + `base.group_user`；不依賴 demo data，故 `--demo` 不需要。
- **Workspace / Service / Template fixture**：每個 test class 自建一組（不共用），避免 cross-test 污染。
- **Frontend 檔案內容 test**：用 `odoo.modules.get_module_path('woow_paas_platform')` 取 addon 路徑（**不要寫死 `src/...` 前綴**，plan §Migration 內也有此 gotcha），讀原始檔不啟動 JS toolchain。
- **`AC #11 domain` 比對 `repr(field.domain)`**：對 list-of-tuples 與 string lambda 兩種 domain 都生效。
- **`AC #12` 包容三種 enforcement**：read-back 不見 / `AccessError` / `ValidationError` 任一通過。

---

## 對 reviewer 的提醒

讀完此 report 後，建議 reviewer 在 PR 上特別對照以下幾段 diff：

1. **`src/security/ir_rules.xml`**：是這次 refactor 最關鍵的兩處改動（D3=A 改寫、D8=A 新增）。請對照 §Regression R3-R5 看 bound leg + outsider 兩條腿沒有被誤殺。
2. **`src/models/discuss_channel.py:216-274`**：原本只有單 service 注入，改寫後有四條分支（bound+extras / bound+no extras / unbound+extras / unbound+no extras）。對照 §AC mapping group E 的 AC #14, #17-19 三條，看 4 個分支都覆蓋。
3. **`src/models/project_task.py`**：新欄位三件套（`extra_cloud_service_ids` + `primary_cloud_service_id` + warning 欄位）。對照 §Open Qs 第 3 條看 implementation 是否在 PR description 解釋 warning 欄位選名理由。
4. **`src/migrations/`**：plan §Migration 講 D1=B 零 schema migration、D2=A 半可逆（M2M 表 rollback 會丟 extras）。**reviewer 須確認沒有意外引入 schema drop**。
5. **Pre-existing infra 紅燈（32 條）**：`Subscription journal is not configured` 與本次 PR 無關，**reviewer 不應 push 要求修這些**；若 implementation 順手修了反而要 push 移到另一個 PR。

---

## Hand-off 條件

- ✅ 25 條 AC 每條皆有對應 test，且每條都能回答「為何測這、為何不測那」。
- ✅ 7 條 regression 守線明列「守住什麼 + why」。
- ✅ 12 條刻意不測項目附理由，與 plan §Out of scope / §Rejected options / §刻意不測 對齊。
- ✅ Reviewer 對照指引 map test → expected diff hotspot 完整。
- ✅ 4 個 Open Qs 均給 (a)/(b)/(c) 三段處置；**0 條需回退 planner**。

下一步：交付 implementation phase（refactor-implementation）動工讓 25 條 red 變 green，過程中對照本 report 的 §Open Qs 拍板 4 條細節，並在 PR description 中保留決策理由供 reviewer / reporter 取用。
