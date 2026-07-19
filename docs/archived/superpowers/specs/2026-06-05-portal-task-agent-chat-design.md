---
title: "Design: Portal task/project 頁的 Agent chat 與 status（原生 share read-mode 擴充）"
category: archived
status: archived
created: 2026-06-05
archived: 2026-07-19
archived-from: docs/superpowers/specs/2026-06-05-portal-task-agent-chat-design.md
archived-reason: 對應 plan 已於 a2b3d914 歸檔至 docs/archived/superpowers/plans/，長期知識已完整轉入現行權威文件：架構與並發分層見 docs/guides/development/agent-runtime-chat-architecture.md，落地紀錄與偏差（F-PORTAL-1~3）見 docs/tracking/followups/follow-ups.md，ACL 紀律見 docs/decisions/patterns/two-layer-acl-lockstep.md；spec 自帶實作後記已對齊全部偏差，無殘留唯一權威內容
---

# Design: Portal task/project 頁的 Agent chat 與 status（原生 share read-mode 擴充）

> 狀態：design approved（brainstorming 2026-06-05），待 writing-plans。
> 來源：`docs/meeting/2026-06-03 paas-project-protal-hermes 會議報告.md`
> （Action #1 的 portal 呈現面 + Action #5「Task Portal 頁的 AI 對話頁」）。
> 研究依據：6-agent 平行研究（原生 portal 擴充點 / paas chat 架構 / realtime 可行性 /
> share ACL / 先行案例 `Woow_odoo_task_ai_solver` / docs invariants），結論已整合進本文。

## 背景

商業流程（會議 §F）：客人付費 → 我們後台用 Odoo 原生 **share project（read 模式）**
分享 → 客人以 portal user 身分在**原生 portal 頁**（`/my/projects/<id>`、`/my/tasks/<id>`）
看到被分享的 project/task，並直接與該 project 的 agent 對話 —— 不用進 /woow 完整平台頁。

研究確認的三個前提：

1. **可見性天然成立**：原生 portal ir.rule（`project_project_rule_portal` /
   `project_task_rule_portal`）只放行 follower / collaborator，本 addon 未動 portal
   rules、無額外洩漏面 → 「只有被分享的才看得到」在 portal 端已成立，本設計不需做
   可見性收緊。
2. **「portal 只能 polling」假設是錯的**：Odoo 18 bus 是純 WebSocket 且
   `auth="public"`、SSE 在 portal user（也是 `res.users`，過 `auth='user'`）同樣可行。
   真正的阻礙是 ACL / internal-user 假設，不是 transport。
3. **對話模型天生 per-task**：既有 chat 是 `discuss.channel` 綁 `task.channel_id`
   （1 task = 1 channel），與拍板的「per-task 共享串」吻合，資料模型零新增。

## 需求（已拍板）

| 項目 | 決定 |
|---|---|
| 範圍 | 原有 portal task/project QWeb 頁擴充（read-mode share） |
| portal task 頁（`/my/tasks/<id>`） | AI chat 區塊 + Agent status 卡片 |
| portal project 頁（`/my/projects/<id>`，無 collaborator 時的 QWeb 頁） | Agent status 卡片 |
| status 資訊量 | 同 /woow（Agent 名稱、pod 三態、AI Connection 資訊），唯讀 |
| 對話串歸屬 | per-task 共享串 = 既有 `task.channel_id`，portal 與 /woow 看同一條 |
| 串流 UX | 與 /woow 一致（SSE 逐字串流） |
| task description | 只簡單帶入（小 hook），完整 context enrichment 由 `feat-enrich-agent-context` branch 收編 |

## 非目標

- **edit-mode `project_sharing` 嵌入式 backend client**（collaborator 分享時
  `/my/projects/<id>` 會改 render iframe web client）— 另開 epic。
- 後台 task form view 的 chat 分頁 — 另開 epic。
- /woow 前台的可見性收緊 — 不在本次。
- bus WebSocket 推播（部署層 websocket upgrade 未驗證）— 未來演進路徑。
- portal 對話的新計費模型 — 沿用 per-project pod 掛 project 擁有者 workspace 的既有歸屬。

## 架構

### 檔案佈局

```
src/
├── controllers/
│   ├── portal.py                  # 新增：繼承 ProjectCustomerPortal
│   └── ai_assistant.py            # 修改：access helpers 加 portal leg
├── views/
│   └── portal_templates.xml       # 新增：QWeb xpath 繼承
├── static/src/
│   ├── common/ai_chat_core/       # 新增：抽出共用 SSE client + Markdown 渲染 + 訊息列表核心
│   └── portal/                    # 新增：portal 專用輕量 OWL 元件（chat + status）
└── __manifest__.py                # 修改：web.assets_frontend 加 portal bundle
```

### 元件一：Agent status 卡片（project 頁 + task 頁）

- **初始渲染**：override `_project_get_page_view_values` / `_task_get_page_view_values`
  （原生 hook，`odoo/addons/project/controllers/portal.py:33` / `:280`），server 端算好
  唯讀展示值。頁面進得來代表已過原生 `_document_check_access`，status 展示值不需
  另開 model 可見性。
- **動態更新**：OWL 小元件 polling 既有 `agent-pod-status` endpoint
  （`src/controllers/ai_assistant.py:1166`，三態 absent/active/reaped →
  未啟動/運行中/閒置），約 10s 間隔、分頁隱藏（`visibilitychange`）時暫停。

### 元件二：AI chat 區塊（task 頁）

- **資料模型零新增**：用既有 `task.channel_id`。channel 不存在時由 controller 在
  驗權後 lazy 建立（sudo、idempotent）。
- **Transport 重用**：`POST /api/ai/chat/post`（上行）+
  `GET /api/ai/stream/<channel_id>`（SSE 下行，csrf_token query）— 與 /woow 完全同
  管線（two-hop bridge，見
  [`agent-runtime-chat-architecture.md`](../../../guides/development/agent-runtime-chat-architecture.md)）。
- **前端共用核心**：從 /woow chat 邏輯抽出 `ai_chat_core`（EventSource 處理、
  Markdown 渲染、訊息列表核心），/woow 與 portal 兩個 bundle 共用；portal 端只新寫
  薄 OWL UI 殼（原生 portal 視覺）。**不重做 transport、不複製邏輯**。
- **Spawn 體驗**：portal user 可觸發 pod spawn（付費客戶對話即商模），重用 #123 的
  spawning 進度事件 + 90s timeout + retry。
- **Markdown 渲染為何不用 chatter**：先行案例 `Woow_odoo_task_ai_solver` 證實
  `mail.message.body` 是 sanitized HTML 非 Markdown，AI 輸出的 Markdown 走 chatter
  會壞排版且有 sanitize 不一致風險 → chat UI 自建渲染（= 會議 §G 的「需重做」結論）。

### Template 插入點（原生預留、不重做頁面）

| 頁面 | 繼承 template | 插入點 |
|---|---|---|
| task 詳情 | `project.portal_my_task` | status 卡片 → `div[name='portal_my_task_second_column']`（原生空欄）；chat → `#task_chat`（Communication history）之前 |
| project 詳情 | `project.portal_my_project` | status 卡片 → searchbar 下方 |

### Task description（簡單版 hook）

在 chat payload 組裝處（pod payload / legacy context）加一個小 hook：該 channel 對應
task 的 `name` + 純文字 `description` 帶入 context。刻意保持極小；
`feat-enrich-agent-context` branch 合併後由其完整機制收編（merge 協調點，plan 內標記）。

## 並發與資源分層（重要觀念）

兩個容易混淆的層，**分開計算容量**：

| 層 | 單位 | 隨什麼成長 | 同一 task 兩人同時對話 |
|---|---|---|---|
| agent pod（K8s） | 1 project = 1 pod，全 project 的 task/人共用；LangGraph `thread_id = channel_id` 區隔對話記憶 | project 數 | 同一個 pod |
| Odoo HTTP worker | 1 條開著的 SSE = 佔 1 個 worker（直到該次串流結束） | 同時收串流的連線數 | 2 個 worker |

→ portal 開放後「同時串流人數」上限受 Odoo worker 數限制（design doc 既有約束：
100 active = 100 worker slot）；**pod 資源完全不受人數影響**。此觀念已持久化進
[`agent-runtime-chat-architecture.md`](../../../guides/development/agent-runtime-chat-architecture.md) §3.1。

## 安全模型（核心風險面）

**原則：portal leg 是「正向判定」，絕不落入既有 fail-open 邊界。**

1. `_check_project_access` / `_check_channel_access` 加 portal 分支，放在所有
   internal 邏輯**之前**：

   ```python
   if user.share:  # portal user 走獨立正向判定，不進 internal fail-open 路徑
       return self._check_portal_task_access(task)  # 不符合就 deny
   ```

2. **two-layer-acl-lockstep 配對**（明文記錄，任一層改動必須同步）：

   | Layer | 位置 | 述詞 |
   |---|---|---|
   | Layer 1（ir.rule，原生、未改動） | `project.project_task_rule_portal` | `project_id.privacy_visibility == 'portal'` AND active AND（partner 是 task follower OR 該 project 的 collaborator 且 `limited_access=False`） |
   | Layer 2（Python carve-out，新增） | `_check_portal_task_access` | 同上述詞的 Python 鏡射 |

3. **F4 fail-open 不可達**：既有 `_check_project_access`（unbound project return True）
   / `_check_task_access`（無 project return True）的 fail-open 是 internal 假設；
   portal 分支獨立先行，保證 portal user 永遠走不到。F4 修復歸原 follow-up，不擴 scope。
4. **不為 portal 開 project/task/agent model 的新 ir.rule**：status 由 controller 算好；
   chat 歷史走既有 endpoint + portal leg。`discuss.channel` / `mail.message` 若原生
   member 規則不足，才加「membership-scoped」最小 portal rule（先行案例驗證過的形狀）
   — plan 階段以程式碼實證決定。
5. **統一錯誤訊息**：deny 與 not-found 回同樣錯誤，防 ID 列舉。
6. **channel membership**：驗權通過後自動把 portal partner 加入 task channel
   （sudo、idempotent，借鏡先行案例 `_validate_portal_channel_access` 形狀）。
7. **被取消分享**：follower/collaborator 撤銷後下一個請求 portal leg 即 deny
   （前端顯示「已無存取權」），無需額外清理機制；channel membership 殘留不構成
   可見性（每次請求都重新驗 task access）。

## 資料流

```
載入歷史：頁面開啟 → 既有 chat 歷史 endpoint（portal leg 驗權）→ 渲染既往訊息
送訊息：portal user 輸入 → POST /api/ai/chat/post {channel_id, body}
  → portal leg 驗權 → message_post（user 訊息落 channel）
收回覆：GET /api/ai/stream/<channel_id>?csrf_token=...（EventSource）
  → portal leg 驗權 → spawn_or_attach（必要時，含 spawning 進度）
  → pod WS 串流 → 逐筆轉 SSE → 前端漸進渲染 Markdown → done 落 channel
status：頁面 render 帶初始值 → OWL polling agent-pod-status（10s、隱藏暫停）
```

## 錯誤處理

| 情境 | 行為 |
|---|---|
| Agent 未設定（無 template / `use_agent_runtime` 關） | status 卡片顯示「未設定」，chat 輸入框 disabled + 原因說明（不隱藏區塊） |
| Pod spawn 失敗 / timeout | 重用 #123：spawning 進度、90s timeout、retry 按鈕 |
| SSE 中斷 | 與 /woow 同行為（`ai_chat_core` 共用，行為自然一致） |
| channel 不存在 | 驗權後 lazy 建立（sudo、idempotent） |
| 對話中被取消分享 | 下一請求 deny → 前端顯示「已無存取權」 |

## 測試策略（TDD）

1. **ORM 層（先寫先紅）**：portal leg allow/deny 矩陣 — follower ✓ / collaborator ✓ /
   非 follower ✗ / `privacy_visibility != 'portal'` ✗ / unbound project 對 portal ✗。
2. **HttpCase endpoint 層（pattern 強制，「ORM 綠 ≠ endpoint secure」）**：chat post /
   history / stream / status 每個 endpoint 補 cross-user negative（portal A 不可及
   portal B 的 task、未分享者 deny），與 ORM 測試成套。
3. **Controller view-values**：被分享 task 頁渲染出 status 區塊；未分享者進不了頁
   （原生行為迴歸）。
4. **斷言紀律**：存在性 / `>=` 斷言，不硬編 schema 快照值
   （[`phase-versioned-test-assertion`](../../../decisions/patterns/phase-versioned-test-assertion.md)）。
5. **E2E**：playwright 走 k8s sandbox（分享 → portal 登入 → 看 status → 對話收串流）；
   正式 e2e 等 merge 後在 develop 跑 `/woow:deploy-stg`。

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| SSE 每串流連線佔 1 Odoo worker | 付費客戶量級可接受；量大時演進 bus 推播（方案三路徑保留，見研究結論矩陣） |
| 與 `feat-enrich-agent-context` merge 順序 | task description hook 刻意極小、明確標記；該 branch 落地後由其收編 |
| portal frontend bundle 首次引入 OWL chat 元件 | k8s sandbox 實測 asset 載入；`ai_chat_core` 抽取不可帶入 backend-only 依賴 |
| access helper 改動波及 /woow internal 路徑 | portal 分支以 `user.share` 隔離、internal 路徑零改動；HttpCase 雙向（internal + portal）迴歸 |

## 相關文件

- 會議報告：`docs/meeting/2026-06-03 paas-project-protal-hermes 會議報告.md`
- Chat 架構鳥瞰：[`docs/guides/development/agent-runtime-chat-architecture.md`](../../../guides/development/agent-runtime-chat-architecture.md)
- ACL patterns：[`orm-vs-endpoint-acl-test-coverage`](../../../decisions/patterns/orm-vs-endpoint-acl-test-coverage.md)、[`two-layer-acl-lockstep`](../../../decisions/patterns/two-layer-acl-lockstep.md)
- 先行案例：`/Users/eugene/Documents/woow/AREA-odoo/woow-addons/Woow_odoo_task_ai_solver`（repo 外部參考）

---

## 實作後記（2026-06-05，實作完成後對齊）

實作全數落地（commits `8eba9ea..7edd749`，902 tests 全綠），與本 spec 的偏差如下（含理由，追蹤位置見括號）：

1. **§安全模型 5「deny / not-found 統一錯誤訊息」未實作**：本節寫「與現有 /api/ai/* 慣例一致」是**對現況的誤述**——既有 endpoint 實際區分 `Channel not found` / `Access denied`。統一化波及 /woow 前端對映與既有測試，維持現狀（F-PORTAL-2 列冊）。
2. **§錯誤處理「SSE 中斷與 /woow 同行為」縮減**：portal 端有 partial-text salvage 與錯誤提示，但**無 /woow 的自動重連**（指數退避）；composer 立即恢復可重送。刻意的極簡 scope（F-PORTAL-3 連動）。
3. **`ai_chat_core` 抽取範圍**：落地為「`ai_stream.js` SSE 協議 consumer + 既有 service 檔直接進 frontend bundle 共用」（rpc / ai_service / markdown_parser / html_sanitize / agent_pod_state）；訊息列表 UI 核心未抽（rule-of-three，F-PORTAL-3）。
4. **安全模型新增一條 spec 未預見的硬規則**：portal leg 放行的 access helper 同時是 mutation endpoint 的閘 → 新增 `_portal_write_denied()` fail-closed guard（share user 一律拒寫；對抗式審查 critical 發現，`2cece77` + `47a1773`）。
5. **lockstep 配對表修正**：原生 task rule 的 collaborator leg 是 **partner 精確比對**（無 child_of 家族展開），predicate 已對齊（`63eec6b`）；follower leg 維持 child_of（原生語意）。child_of 展開未帶 `active_test=False`（原生含 archived partner），分歧方向為多 deny（safe），已接受並註記於 model docstring。
6. **§Task description hook 已如約被收編**：merge develop 時 `feat-enrich-agent-context`（PR #145）的統一 context builder 已落地（`agent_session._build_system_context` / `_build_runtime_system_context`，task description 含 `CTX_DESC_CAP=1500`），本 epic 的極簡 hook（`_agent_task_context_for_channel` + 兩處接線 + 測試）**整組撤除**，避免雙重注入 — 正是 plan §風險 1 預載的劇本。
