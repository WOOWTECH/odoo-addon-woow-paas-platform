---
name: plan-cloud-service-project-decouple
status: signed-off
created: 2026-05-16T13:13:43Z
updated: 2026-05-16T13:23:21Z
phase: planning
team: refactor
planner: refactor-planner
---

# Refactor Plan: 解耦 CloudService 與 Project

## Context

### 現況（基於 codebase 校準）

模組初始任務描述為「Service 1:1 Project、Project 不能單獨存在」，但實際探索 codebase 結果與描述存在落差，本 plan 以下列校準後的事實為前提：

- **DB schema**：`Project.cloud_service_id` 已是 nullable `Many2one('woow_paas_platform.cloud_service', ondelete='set null')`，定義於 `src/models/project_project.py:7-13`。
- **1:1 限制來源**：是 `src/models/project_project.py:27-33` 的 SQL constraint `UNIQUE(cloud_service_id)`，加上 `src/static/src/paas/components/modal/CreateProjectModal.js:124-132` 前端強制使用者必選 service，並於 `:68-71` 過濾掉「已有 project」的 service。
- **Task discussion ↔ Service 連結**：機制已部分存在，`src/models/discuss_channel.py:216-274` 透過鏈式查詢 `channel → task → project → cloud_service`，把 service 資訊注入 AI 系統 prompt（含 helm_values 全文）。但是**隱性**：使用者在 UI 上看不到 service chip / 卡片，且僅支援 task 所屬 project 綁的單一 service。
- **Project 在 UI 上的入口**：唯一在 `/woow#/ai-assistant` 進入 `SupportProjectsPage`，名稱與 navigation 路徑反映「support project bound to cloud service」舊心智模型。
- **🔴 既有 ACL 漏洞**：`src/security/ir_rules.xml:55` 的 `project_rule_workspace_member` record rule 短路 `'|', ('cloud_service_id', '=', False), ...`，凡 `cloud_service_id = False` 的 Project 對所有 `base.group_user` 都可見。今天靠 unique constraint + 前端強制綁 service 才沒爆發，**一旦放開「Project 可獨立」這條 rule 立刻變多租戶資料外洩**。
- **🔴 既有 cloud_service ACL gap**：`woow_paas_platform.cloud_service` 在 `src/security/ir.model.access.csv:7` 給 `base.group_user` 全 RWC，但 `src/security/ir_rules.xml` **沒有任何 record rule**。隔離靠 `src/controllers/paas.py` endpoint 內以 `workspace_id` 過濾。Odoo 標準 form 的 Many2many widget 走 ORM 直查，會繞過 controller — 本次 D2=A 引入「Task UI 列 cloud_service 候選清單」會把這個 pre-existing gap 浮上水面。
- **🔴 既有 secret 洩漏面**：`src/models/discuss_channel.py:248-249` 將 `service.helm_values` JSON 整段 dump 進 AI prompt，且取 service 用 `sudo()` 繞過 ACL；helm_values 可能含 password / api_key / token（如 `mcp_api_key_helm_path` 注入到 helm_values 的 n8n API key）。本次 D2=A 透過 extras 引入「使用者可參考別 service」後，此風險面積擴大。

### 目標

- Project **可以**獨立於 Service 存在（不綁也能完整運作）。
- Project **若**綁 Service，繼續維持 1:1 關係（本次不擴張為 1:N，留作後續 feature）。
- Task 可參考多個 Service：
  - **Primary**：由 `task.project_id.cloud_service_id` 自動推導（隨 project 綁定變動）。
  - **Extras**：使用者明確選的額外 references（Many2many）。
  - Chatter 區顯示 service chip（可連結、可加可刪）。
  - AI prompt 注入 primary + extras 合併內容，但 secret 欄位必須遮罩。
- 既有 stg 環境 Service / Project 操作不能 break，零資料遷移。

---

## Decisions

| # | 議題 | 選項 | Tradeoff | Planner 推薦 | 使用者拍板 |
|---|---|---|---|---|---|
| **D1** | Project ↔ Service 拆 1:1 後的關係形式 | A. 拆 unique constraint → 1:N<br/>B. 留 unique constraint，僅 nullable | A 一勞永逸但 schema 變更不可逆；B 簡單可逆，1:N 留作後續 | **B** | **B** ✅ |
| **D2** | Task ↔ Service 連結機制 | A. Task 加 Many2many extras + primary related from project<br/>B. 只加 UI chip 顯示，schema 不擴<br/>C. mail.message reference | A 支援 task 多 service 參考（含跨 project 副 reference），是使用者真實需求 | A | **A** ✅ |
| **D3** | unbound Project ACL 修補方式 | A. 改 record rule 為 project member 可見<br/>B. Project 加必填 workspace_id 後改 rule | A 貼齊 Odoo 原生 project 隔離邏輯；B 需先補 workspace_id 寫死，工作量大 | A | **A** ✅ |
| **D4** | unbound Project 在 PaaS UI 的入口 | A. SupportProjectsPage 加 filter<br/>B. 新增獨立 menu<br/>C. 現有 `/woow#/ai-assistant` 入口重新命名為 Projects，列表混合 bound + unbound | C 最小改動，UX 統一 | C | **C** ✅（沿用 `/woow#/ai-assistant` 為 URL alias 不破書籤，正規路徑改 `/woow#/projects`） |
| **D5** | CreateProjectModal 行為 | A. Service 變 optional 下拉（含「— No service —」）<br/>B. 分兩個按鈕 | A 一致 UX | A | **A** ✅ |
| **D6** | Extras 候選 service 範圍 | A. 只限 task 所在 workspace<br/>B. user 可 view 的所有 workspace<br/>C. 先 A，後續放寬 | A 收緊安全，跨 workspace 屬於另一個需求 | C | **C** ✅ |
| **D7** | AI prompt 注入時 extras 是否合併 | A. Primary + Extras 全併入<br/>B. 只注入 Primary | A 符合 D2=A 引入 extras 的初衷 — 讓 AI 知道 task 涉及哪些 service | A | **A** ✅（**含 password / secret 遮罩**，見 D9） |
| **D8** | cloud_service pre-existing ACL gap | A. 本次補 ir.rule<br/>B. 只在 UI domain / endpoint 加 filter<br/>C. 列 follow-up | A 從根源堵住；D2=A 引入 Task form M2M widget 後不補幾乎一定漏 | A | **A** ✅ |
| **D9** | AI prompt 注入時的 secret 遮罩策略 | A. Sanitizer 遞迴遮罩 known sensitive keys（pattern match）<br/>B. Whitelist 白名單模式（只放安全欄位）<br/>C. 不注入 helm_values | A 彈性、不破壞既有資訊量；B 最安全但每增 chart 要維護白名單；C 喪失 helm_values 對 AI debug 的價值 | A → B（先 A 上線、telemetry 觀察後升級到 B） | **A → B** ✅（先 A，明確 redact `n8n_api_key` / `n8n_owner_password` / `mcp_auth_token`，redacted value 顯示為 `***REDACTED***`；後續升級到 B 由 reporter 寫入 follow-up） |

---

## Rejected options

以下選項在本次討論中被否決，記錄理由供未來 reporter 持久化：

1. **D1 選 A（1:N）** — 否決原因：本次目標只是「解綁」不是「擴張」；1:N 的 schema 變更不可逆（rollback 需處理已存在的 1:N 資料），與 success criteria「不能 break stg」風險過高；保留 unique constraint 不阻擋未來改 1:N。
2. **D2 選 B（只 UI chip 不擴 schema）** — 否決原因：使用者需求是「task 可額外參考其它 service」，純 UI 無法表達使用者意圖（無法持久化選擇）；且 AI prompt 注入 extras 需要欄位來源。
3. **D2 選 C（mail.message reference）** — 否決原因：可發現性差（reference 藏在 message body），不易做 UI chip / domain filter；對 AI prompt 注入而言要再解 message body 找 reference，工程量更大。
4. **D3 選 B（改 Project workspace_id 必填）** — 否決原因：workspace_id 目前是 computed via cloud_service，改成必填要先寫資料回填 + 改 form view + 多處依賴調整，scope 暴漲。
5. **D4 選 A / B（保留 SupportProjects 名稱 / 新增獨立 menu）** — 否決原因：與既有 UX 心智模型矛盾（命名暗示「support」、新增 menu 造成重複入口）。
6. **D6 選 B（user 可 view 全 workspace）** — 否決原因：違反 multi-tenant 隔離直覺，且目前沒有實際 cross-workspace task 用例；先收緊、有需求再放寬比反向操作便宜。
7. **D7 選 B（只注入 Primary）** — 否決原因：使用者主動掛 extras 的核心目的就是讓 AI 知道這些 service 存在；不注入等同 extras 退化為純跳轉鏈接。
8. **D8 選 B（僅 UI / endpoint filter）** — 否決原因：ORM 是真實 source of truth，UI domain 是 hint 可被繞過；root-cause 修補本次處理一勞永逸。
9. **D9 選 C（不注入 helm_values）** — 否決原因：helm_values 對 AI debug 有實質價值（chart 版本、resource、特定 chart 配置）；遮罩比完全捨棄 better tradeoff。

---

## Acceptance criteria

以下子句設計為可被 tester 直接轉為 red test cases。

### Project 獨立性
1. 使用者可建立一個 `project.project` 而不設 `cloud_service_id`，系統不阻擋。
2. 已綁定 service 的 Project 仍然每個 service 至多一個 Project（`unique_cloud_service` constraint 仍有效）。
3. 既有 stg 環境的 bound Project 升級後 `cloud_service_id` 不變、`workspace_id` computed 結果不變。

### Project ACL（unbound 隔離）
4. 使用者 A 建立 unbound Project P1（未綁 service），使用者 B（與 A 沒有同 project membership）查詢 `project.project` 不會看到 P1。
5. 使用者 A 把 P1 加 B 為 member 後，B 查詢 `project.project` 看得到 P1。
6. 既有 bound Project 對同 workspace 成員仍可見（regression）。

### cloud_service ACL（補強）
7. 使用者 A 在 workspace W1，使用者 B 在 workspace W2（且 A、B 不互為 workspace 成員）：A 查詢 `cloud_service` 不會看到 W2 的 service（無論 read / write / unlink）。
8. 既有 controller `/woow` 系列 endpoint 對 service 的列表 / 操作行為不變（regression）。

### Task ↔ Service extras
9. `project.task` 有 `extra_cloud_service_ids` Many2many 欄位，可空。
10. `project.task` 有 `primary_cloud_service_id` computed/related 欄位，值等於 `task.project_id.cloud_service_id`（store=True，project 改綁 service 後 task 此欄位同步更新）。
11. 在 task form 上選 extras：候選清單**只列**該 task 所在 project 的 workspace 內的 service（domain filter）。
12. 直接以 ORM 嘗試 write `extra_cloud_service_ids` 為其他 workspace 的 service id：被 record rule（D8 修補後）拒絕。
13. Task 跨 project 移動後，extras 保留；若 extras 中有「對新 project 的 workspace 不可見」的 service，UI 顯示警告 chip（不阻擋儲存）。

### AI prompt 注入 + secret 遮罩
14. Task chatter 觸發 AI 自動回覆時：AI 系統 prompt 包含 primary 與所有 extras 的 service 資訊段落，順序為 primary 在前；primary 同時出現在 extras 時去重。
15. Service 的 `helm_values` 注入前經 sanitizer 遞迴遮罩：key 符合 pattern `(?i)(password|passwd|secret|token|api[_-]?key|credential)` 的 value 替換為 `***REDACTED***`。
16. Service 的 `n8n_api_key`、`n8n_owner_password`、`mcp_auth_token` 即使被 `sudo()` 讀到也**不**注入 AI prompt（明確 blacklist）。
17. Task chatter on unbound project（primary 空）+ 有 extras → AI prompt 只注入 extras 段落（不出現「Primary」標籤）。
18. Task chatter on bound project + 無 extras → AI prompt 只注入 primary 段落（regression for 現有行為）。
19. Task chatter on unbound project + 無 extras → AI prompt 不出現任何 service 段落（不爆錯）。

### UX 前端
20. `/woow#/ai-assistant` 與 `/woow#/projects` 兩個路徑都導到同一個 Projects 列表頁（avoid bookmark 破裂）。
21. Sidebar 對應 entry label 顯示 `Projects`（zh-tw：依使用者後續決議）。
22. CreateProjectModal 的 service 下拉首項為「— No service —」（或對應 zh-tw 文字），選此項可建立 unbound project。
23. CreateProjectModal 移除「過濾掉已有 project 的 service」的前端邏輯（D1 仍保留 unique constraint 由 server-side 把關）。
24. Projects 列表頁 grid/list view 新增一欄/badge 顯示綁定狀態（service name 或「Unbound」）。
25. Task form 在 chatter 區域上方顯示 Linked services panel：primary chip（readonly，可點擊跳轉到 service detail）+ extras chips（可加可刪）+ `+ Add reference` 按鈕。

---

## Affected files

### Schema / Model
- `src/models/project_project.py` — **保留** `unique_cloud_service` constraint（D1=B）；無變動或補註解說明為何保留。
- `src/models/project_task.py` — 新增 `extra_cloud_service_ids = Many2many`、`primary_cloud_service_id = Many2one(related=..., store=True)`。
- `src/models/cloud_service.py:184-188` — 保留 `project_ids = One2many`（與 D1=B 一致）。
- `src/models/discuss_channel.py:216-274` — 改寫 `_get_cloud_service_context()`：
  - 接收 task，分別收集 primary（`task.project_id.cloud_service_id`）+ extras（`task.extra_cloud_service_ids`）
  - 新增 helper `_sanitize_helm_values(helm_values_json)` 做遞迴 secret 遮罩
  - 對 `n8n_api_key` / `n8n_owner_password` / `mcp_auth_token` 不注入
  - 同 service 出現在 primary 與 extras 時去重

### Security
- `src/security/ir_rules.xml`：
  - 改寫 `project_rule_workspace_member`：移除 `'|', ('cloud_service_id', '=', False), ...` 短路，改為：
    - bound project → 同 workspace 成員可見
    - unbound project → project member 可見（`('user_ids', 'in', user.id)` 或 `('message_partner_ids', 'in', user.partner_id.id)`）
  - 新增 `cloud_service_rule_workspace_member`：限同 workspace 成員可讀寫 service。

### Controllers
- `src/controllers/paas.py`：
  - line 2422 `'has_project': bool(service.project_ids)` — 保留（CreateProjectModal 改 service 為 optional 後此 flag 仍用於 UI 顯示）
  - 視 D5 修改 project 建立 endpoint：放寬 `cloud_service_id` 為 optional（伺服器端驗證；唯 D1=B 仍維持 unique constraint）
  - 新增 endpoints 支援 task extras CRUD（如果前端走 RPC 而非標準 ORM）；若 task form 已走 Odoo 標準 form RPC 則無需新增 endpoint

### Frontend (OWL)
- `src/static/src/paas/components/modal/CreateProjectModal.js`：
  - 移除 `:68-71` 的 `has_project` filter
  - 移除 `:124-132` 的「service required」驗證
  - service 下拉新增「— No service —」首項
- `src/static/src/paas/services/support_service.js`：
  - 新增 / 調整 createProject API call 支援 `cloud_service_id = null`
- `src/static/src/paas/pages/support-projects/SupportProjectsPage.js`：
  - 新增「Service」欄位 / badge（顯示 bound service name 或「Unbound」）
  - filter 加「all / bound / unbound」
  - 頁面 title 改為「Projects」
- `src/static/src/paas/components/sidebar/*`（探索後確定路徑）— sidebar entry label `ai-assistant` → `Projects`
- `src/static/src/paas/core/router.js` — 加 `/projects` 路由 alias，導向同一 page；保留 `/ai-assistant`
- Task chatter 區的 Linked Services panel（新 OWL component，或擴 existing task widget — 由 implementation phase 決定具體做法）

### Views
- `src/views/project_task_views.xml`：在 form 內加 Linked Services panel widget；可能需擴 chatter container 或 sheet
- 視前端決議是否需要新 view（如獨立 service picker dialog）

### Tests
- `src/tests/test_cloud_service.py` — 補：cloud_service ACL（D8）regression / 新增
- `src/tests/test_cloud_api.py` — 補：unbound project 建立 endpoint regression
- `src/tests/`（新檔，由 tester 決定）：
  - `test_project_decouple.py` — Project unbound 建立、ACL 隔離（AC #1-6）
  - `test_task_service_extras.py` — Task extras CRUD、primary related、domain filter、cross-project 移動（AC #9-13）
  - `test_chatter_service_injection.py` — AI prompt 注入 primary/extras 合併、secret 遮罩、blacklist 欄位（AC #14-19）

### Data / Migration
- 無 data 變動；ORM auto-create `project_task_cloud_service_rel` M2M table
- `src/migrations/`：無需新增 pre-migrate（D1=B 無 schema drop）；若要 hygiene 補 post-uninstall 清 M2M 表可選

### Hooks
- `src/hooks.py` — 無 config_parameter 新增，無需更新

---

## Test scope 建議（給 tester）

### Unit / Model tests
- AC #1-3, #7-13, #15-16（純 model 層行為）
- 用 Odoo TransactionCase（既有 test_cloud_service.py 風格）

### Integration tests
- AC #4-6（ACL 隔離，需多 user）
- AC #14, #17-19（discuss_channel 觸發 AI prompt 注入，可 mock `AIClient.chat_completion_with_tools` 驗證注入內容）
- 既有 `test_cloud_api.py` HttpCase 風格延伸

### E2E / UI tests（playwright-cli 或 agent-browser）
- AC #20-25（前端 Projects 列表、CreateProjectModal、Task chatter Linked Services panel）
- 建議涵蓋：
  - 建 unbound Project 流程
  - Bound / Unbound badge 顯示正確
  - Task 加 extra service chip + 觸發 AI 訊息確認 service 資訊出現
  - URL `/woow#/ai-assistant` 與 `/woow#/projects` 都能正確進入

### 刻意不測（明列理由）
- 1:N Service-Project（D1=B 不允許）
- Task 直接 FK 到 Service 跨 project（D2=A 用 extras Many2many 取代，非直接 FK）
- cross-workspace extras 候選（D6=C 收緊到同 workspace；若未來放寬，補測）
- 已被遮罩的 secret 是否正確「重建」AI 回覆（測 sanitizer 輸入輸出即可；不測 AI 回覆品質）
- Sanitizer 對 nested dict / array 超過 N 層深度（用 reasonable depth limit；對 helm_values 一般是 2-3 層即足）

---

## Migration plan

### 主要 schema 動作
- **D1=B → 零 schema migration**：保留 `unique_cloud_service` constraint，僅放寬前端與 ACL。✅ 完全可逆。
- **D2=A → ORM auto-create M2M table `project_task_cloud_service_rel`**：對既有 task 零影響（新欄位、新表，舊資料無 default）；rollback = drop table（資料若有則丟失）。⚠️ 半可逆（如 production 已有 extras 資料，rollback 會丟失 extras 連結）。

### ACL XML 變動
- `ir_rules.xml` 改寫 `project_rule_workspace_member`、新增 `cloud_service_rule_workspace_member`
- 載入 noupdate="1" — 既有 stg 環境若已有同 ID 的 rule，需確認 stg 是否手動改過（先 export 既有 rule）
- Rollback = revert XML + module update

### 既有資料處理
- 既有 bound Project（有 `cloud_service_id`）→ 行為不變，繼續 1:1
- 既有 unbound Project（如果有，理論上前端阻擋下應為零）→ ACL 變嚴後，**之前能看到的 user 可能看不到了**（必須是 project member）。需在升級前 query 確認：

```sql
SELECT p.id, p.name, p.create_uid
FROM project_project p
WHERE p.cloud_service_id IS NULL;
```

- 若查詢結果非空，需逐筆確認是否該 user 已是 member；不是則手動補 member（或在 plan 中標 `Open Q2`）。

### Rollback strategy
| 變動 | Rollback |
|---|---|
| `extra_cloud_service_ids` M2M 欄位 | Revert code → ORM 留下空 column（移除欄位定義時 Odoo 不會自動 drop column；如需徹底清，補 post-uninstall migration `DROP TABLE project_task_cloud_service_rel`）|
| `primary_cloud_service_id` related 欄位 | 同上；related 欄位無實體 column 可 drop（store=True 才有；revert 時補 drop column 或留著無害） |
| ir_rules 變動 | Revert XML + module update |
| 前端 OWL / view 變動 | Revert code |
| `discuss_channel._get_cloud_service_context` 改寫 + sanitizer | Revert code（無持久化影響） |

整體 rollback time-to-recover：< 10 分鐘（code revert + module update）。

---

## Risks & rollback strategy

### Risks
| ID | 風險 | 影響 | 機率 | 緩解 |
|---|---|---|---|---|
| R1 | ACL `project_rule_workspace_member` 改寫後，**既有 unbound project 對某些 user 變不可見** | Medium（既有 unbound project 理論為零，但若有則使用者「東西不見了」） | Low | 升級前執行上述 SQL 查詢；非空時逐筆處理 |
| R2 | M2M `project_task_cloud_service_rel` rollback 會丟失使用者已設定的 extras 連結 | Medium | Medium（D2=A 上線後使用者會設） | rollback 前 dump 該表；標註 D2=A 動作為「half-reversible」 |
| R3 | Secret sanitizer 漏網（新 chart 引入未知 sensitive key pattern） | High（secret 洩漏到 AI prompt → AI provider → 可能 logs / 訓練資料） | Medium | 採 deny-by-pattern + 明確 blacklist 雙保險；新 chart 上 PR review 時必檢查 helm values keys；長期建議補 whitelist 模式（Open Q1） |
| R4 | cloud_service 新增 ir.rule 後，**既有 controller endpoint 行為改變**（之前可看的 service 變看不到） | High（stg 環境若有 user 跨 workspace 操作會 break） | Low | regression test 覆蓋 controller endpoint；stg 上線前先以 stg 環境 user 名單 dry-run rule 邏輯 |
| R5 | Task 跨 project 移動後 extras 殘留無權限 service | Low（UI 標警告即可，不阻擋） | Low | AC #13 明確規範，tester 測 |
| R6 | `/woow#/ai-assistant` 既有書籤未轉移 → 使用者輸入 `/projects` 找不到 | Low | Low | router alias 雙路徑，AC #20 規範 |

### Rollback procedure（緊急）
1. Revert merge commit on feature/decouple-cs-projects → main
2. Restart Odoo（stg 與 prod）
3. 若 production 已有 task extras 資料 → dump `project_task_cloud_service_rel` 到 S3 / backup 後再 drop（標 R2）
4. ir_rules.xml revert 後執行 `odoo-bin -u woow_paas_platform --stop-after-init` 重新載入

### ⚠️ 不可逆動作清單
- 無嚴格不可逆動作（D1=B 確保 schema 不變）。
- 半可逆：M2M extras 資料（R2）— 已標註，rollback 前必 dump。

---

## Out of scope（明列排除，避免擴）

| 議題 | 為什麼排除 |
|---|---|
| Project 1:N Service | 本次 D1=B 明確留待後續 feature |
| Task 直接 FK 到 Service（取代或補充 extras） | D2=A extras Many2many 已涵蓋使用者描述的「Task 多參考其它額外 cloud services」需求 |
| Cross-workspace extras 候選 | D6=C 收緊到同 workspace，後續視實際需求放寬 |
| Project audit log | 本次無此需求 |
| Service 列表頁顯示綁定的 project 數 | 雙向資訊揭露不在本次目標 |
| Project 自身 workspace_id 改為手動必填（取代 computed） | 涉及大量 view / 既有資料處理；當前 computed via cloud_service 仍可用（unbound 時為 False，UI 處理） |
| 大規模 sidebar / menu 結構重整 | D4=C 只動單一 entry，不擴 |
| AI prompt 品質優化（多 service context 如何排序、token 預算管理） | 屬於 AI feature 範疇，與解耦無關 |
| paas-operator service 端任何變動 | 本次純 Odoo 模組層 |

---

## Open questions（已 resolve）

### Open Q1 — D9 secret 遮罩策略 ✅
**決議：A → B**（先 A pattern sanitizer 上線，後續升級到 B whitelist）。

- 第一階段（本次 PR）：實作 A pattern sanitizer + 明確 blacklist 三欄位（`n8n_api_key` / `n8n_owner_password` / `mcp_auth_token`）；redacted value 顯示為 `***REDACTED***`。
- 第二階段（follow-up）：reporter 在 phase 結尾把「升級到 whitelist 模式」寫入 `docs/tracking/followups/follow-ups.md` 或對應 follow-up issue，搭配 telemetry / pen-test 結果判斷時機。

### Open Q2 — 升級前 SQL dry-run ✅
**決議：採 planner 推薦** — 列為 implementation phase 的 deployment checklist 一部分：
- implementation agent 在 PR description 標註必要 SQL 檢查語句（Risk R1 的 query 為基準）
- 使用者 deploy 前手動執行（涉及 stg / prod 連線）
- caqa 在 PR 上抽查 deployment checklist 是否完整

### Open Q3 — Linked Services panel 的細部 UX
**決議：留給 implementation phase 決定**，plan 不過度約束視覺細節。本 plan 規範的 UX invariant 已列於 AC #25。

### Open Q4 — Sidebar entry zh-tw 文字 ✅
**決議：保留英文「Projects」**。implementation phase 不需新增 translation entry；如 sidebar 其它 entry 已 i18n，保持「Projects」literal 即可。

---

## Hand-off checklist（Planner → Tester）

使用者已 sign-off（2026-05-16T13:23:21Z）。refactor-planner 用：
`maestri ask "refactor-phase-leader" "plan-signed: docs/plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md ; D1=B(留 unique constraint, Project 可 unbound); D2=A(Task M2M extras + related primary); D3=A(unbound project ACL 改 project member); D4=C(ai-assistant→Projects, 路徑 alias); D5=A; D6=C(限同 workspace); D7=A(primary+extras 注入); D8=A(補 cloud_service record rule); D9=A→B(本次 pattern sanitizer + blacklist 三欄位, follow-up 升級 whitelist); sidebar 保留英文 Projects; deploy SQL 由 implementation 寫進 PR description, 使用者手動跑"`
回 leader。

Tester 開始 TDD red 階段時，建議 acceptance criteria #1-25 逐條轉 test case；本 plan 不列具體 test function 名稱 / fixture 細節（由 tester 決定）。
