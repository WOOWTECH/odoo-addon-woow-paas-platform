---
name: project-kanban-and-chat-fix
description: 為專案任務新增看板視圖，並修復 AI 聊天連線問題
status: backlog
created: 2026-02-10T08:53:30Z
---

# PRD：專案看板視圖 & 聊天連線修復

## 摘要

本 PRD 解決 AI Assistant 模組中的兩個使用者端問題：

1. **專案看板視圖** — 點擊 support project 時，以看板（Kanban）方式顯示任務，依階段（stage）分欄，並支援拖曳更新任務狀態。目前點擊專案會導航到依專案名稱分組的平面任務列表，無法提供預期的看板級視覺化。

2. **AI 聊天連線修復** — 任務詳情頁的聊天功能顯示「Connection to AI was lost. Please try sending your message again.」，源於 SSE 串流管線中的多個根本原因。需要修復 bug 並改善使用體驗（自動重連、連線狀態指示器）。

## 問題陳述

### 問題 1：專案缺少看板視圖

使用者進入特定專案（`#/ai-assistant/projects` → 點擊專案）後被導向 `#/ai-assistant/tasks?project={id}`，呈現依專案名稱分組的平面任務列表。這造成困擾：

- 使用者期望進入專案時看到**看板視圖**，以狀態欄（New、In Progress、Done 等）分列
- 目前依專案分組的視圖重複顯示使用者已選擇的資訊
- 無法視覺化追蹤任務在各階段的進展
- 無法透過拖曳在階段間移動任務以更新狀態

### 問題 2：聊天連線失敗

任務詳情頁（`#/ai-assistant/tasks/{taskId}` → Chat 分頁）的 AI 聊天持續失敗，顯示「Connection to AI was lost.」。根因分析揭示多個失敗點：

1. **Agent 偵測回傳 None**（`ai_assistant.py:391-411`）— 若任務未啟用 `ai_auto_reply=True` 或不存在預設 AI agent，SSE 端點回傳 400
2. **Provider 未設定**（`ai_assistant.py:305-311`）— 若 AI provider 未啟用或缺失，端點靜默失敗
3. **channelId 可能為 null**（`TaskDetailPage.xml:195`）— 若任務 `chat_enabled=false`，則無 channel 存在，`channelId` 以 `null` 傳入 AiChat，導致無效 URL（`/api/ai/stream/null`）
4. **無自動重連機制** — SSE 連線中斷時無重試邏輯
5. **無連線狀態回饋** — 使用者無法得知連線失敗的原因

## 使用者故事

### 看板視圖

**US-1：以看板方式檢視專案任務**
> 身為專案成員，我希望看到專案中所有任務依階段分欄排列，以便一目了然地掌握專案進度。

驗收標準：
- 點擊專案卡片導航至看板視圖
- 任務依 `stage_id`（Odoo 預設階段）分組為欄
- 每欄標題顯示階段名稱與任務數量
- 任務卡片顯示：標題、優先級、負責人頭像、截止日期（若有）
- 空欄仍顯示，帶有佔位提示

**US-2：拖曳任務在階段間移動**
> 身為專案成員，我希望將任務卡片從一個階段欄拖曳到另一個，以便快速更新任務狀態而不需開啟任務詳情。

驗收標準：
- 任務可從一欄拖曳到另一欄
- 放下任務時透過 API 更新其 `stage_id`
- 拖曳過程有視覺回饋（幽靈卡片、放置區高亮）
- 樂觀式 UI 更新，API 失敗時回滾
- 成功/錯誤的 toast 通知

**US-3：從看板導航至任務詳情**
> 身為專案成員，我希望在看板上點擊任務卡片以開啟其詳情頁。

驗收標準：
- 點擊任務卡片導航至 `#/ai-assistant/tasks/{taskId}`
- 返回導航回到看板（而非專案列表）

### 聊天連線修復

**US-4：可靠的 AI 聊天連線**
> 身為使用者，我希望 AI 聊天能可靠連線，並在出錯時顯示明確的回饋，讓我知道能否使用聊天功能。

驗收標準：
- 聊天顯示連線狀態指示器（連線中、已連線、已斷線）
- 若 channel 尚未建立，提示使用者先啟用聊天
- 若 AI provider 未設定，顯示描述性錯誤（非通用「連線中斷」）
- 連線中斷時以指數退避自動重連（最多 3 次）
- 各失敗場景顯示明確的錯誤訊息與操作建議

**US-5：優雅處理缺失的設定**
> 身為使用者，我希望了解聊天無法運作的原因，而非看到通用錯誤。

驗收標準：
- Provider 缺失 → 「AI 供應商尚未設定，請聯繫管理員。」
- 無預設 agent → 「目前沒有可用的 AI 助理，請在設定中配置 AI agent。」
- Channel 未建立 → 「此任務尚未啟用聊天功能，請點擊「啟用聊天」開始。」
- 自動回覆已停用 → 聊天仍可檢視歷史紀錄，並附註 AI 不會自動回覆

## 需求

### 功能需求

#### FR-1：專案看板頁面

- **新路由**：`#/ai-assistant/projects/{projectId}` → `ProjectKanbanPage`
- **API 增強**：`GET /api/ai/tasks` 回傳需包含 `stage_id`（目前只有 `stage_name`）
- **新 API**：`POST /api/support/projects/{project_id}/stages` 取得專案的階段列表
- **看板欄位**：從 Odoo `project.task.type`（階段模型）取得特定專案的階段
- **欄位排序**：遵循 Odoo 階段的 `sequence` 欄位

#### FR-2：看板卡片元件

- 顯示：任務名稱、優先級星號、負責人頭像、截止日期徽章
- 截止日期顏色編碼（逾期 = 紅色、即將到期 = 橘色）
- 點擊導航至任務詳情
- 拖曳手柄

#### FR-3：拖曳功能

- HTML5 Drag and Drop API（OWL 不需外部套件）
- 樂觀式 UI：立即移動卡片，API 失敗時回滾
- 無寫入權限的使用者停用拖曳（未來考量）

#### FR-4：聊天連線改善

- **預檢**：在開啟 EventSource 前驗證 `channelId` 為有效值
- **後端錯誤區分**：回傳結構化 error code，而非僅字串
- **自動重連**：實作指數退避（1s、2s、4s），最多 3 次重試
- **連線狀態機**：`idle → connecting → connected → streaming → error → reconnecting`
- **UI 指示器**：在聊天標題區域顯示連線狀態

#### FR-5：序列化修正

- 在 `ai_assistant.py` 的 `_serialize_task()` 中新增 `stage_id`
- 確保 JSDoc 型別與實際 API 回傳一致

### 非功能需求

- **效能**：看板頁面在 100 個任務內需 2 秒內載入
- **響應式**：看板需在平板螢幕上正常運作（欄位可水平捲動）
- **無障礙**：拖曳應有鍵盤替代方案（未來考量）
- **瀏覽器支援**：EventSource（SSE）在所有現代瀏覽器中均支援

## 成功標準

| 指標 | 目標 |
|------|------|
| 看板正確渲染 | 所有專案任務顯示在正確的階段欄中 |
| 拖曳成功率 | 100% 的有效拖曳操作成功更新階段 |
| 聊天連線成功率 | provider 已設定時 > 95% 首次連線成功 |
| 聊天錯誤清晰度 | 可辨識根因時 0 個通用「連線中斷」錯誤 |
| 自動重連效果 | 短暫網路問題在 10 秒內恢復 |

## 技術設計備註

### 需建立的檔案

| 檔案 | 用途 |
|------|------|
| `src/static/src/paas/pages/project-kanban/ProjectKanbanPage.js` | 看板頁面元件 |
| `src/static/src/paas/pages/project-kanban/ProjectKanbanPage.xml` | 看板模板 |
| `src/static/src/paas/styles/pages/_project_kanban.scss` | 看板樣式 |

### 需修改的檔案

| 檔案 | 變更 |
|------|------|
| `src/static/src/paas/core/router.js` | 新增路由 `ai-assistant/projects/{id}` → `ProjectKanbanPage` |
| `src/static/src/paas/pages/support-projects/SupportProjectsPage.js` | 修改 `navigateToProject()` 導航至看板 |
| `src/controllers/ai_assistant.py` | `_serialize_task()` 新增 `stage_id`；新增階段列表端點；修復 SSE 錯誤處理 |
| `src/static/src/paas/services/support_service.js` | 新增 `fetchProjectStages()` 方法 |
| `src/static/src/paas/components/ai-chat/AiChat.js` | 新增自動重連、連線狀態、預檢查 |
| `src/static/src/paas/components/ai-chat/AiChat.xml` | 新增連線狀態指示器 UI |
| `src/__manifest__.py` | 註冊新資源檔 |

### 關鍵架構決策

1. **不引入外部拖曳套件** — 使用 HTML5 原生 DnD API 搭配 OWL 事件處理器，維持打包體積小
2. **階段來自 Odoo** — 使用 `project.task.type` 模型（Odoo 內建階段模型），而非硬編碼
3. **SSE 錯誤碼** — 後端回傳結構化 JSON 錯誤並附帶 `code` 欄位，供前端區分失敗類型

## 限制與假設

### 限制
- 必須在獨立 OWL 應用架構中運作（不依賴 Odoo web client）
- 拖曳必須使用原生 HTML5 DnD（不使用 React DnD 或類似套件）
- SSE 端點必須維持對現有聊天整合的向後相容

### 假設
- Odoo 的 `project.task.type` 模型已為各專案設定了階段
- 使用者至少擁有專案任務的讀取權限
- AI provider 由管理員在預期使用聊天前完成設定

## 不在範圍內

- 看板 WIP 限制（限制每欄卡片數）
- 看板卡片行內編輯
- 從看板 UI 自訂建立階段
- 僅鍵盤操作的拖曳無障礙支援
- 聊天訊息回應或串接
- 從聊天 UI 內選擇多個 agent
- 離線聊天訊息佇列

## 依賴項

### 內部
- Odoo `project.task.type` 模型（階段）需為專案填充資料
- AI provider 和 agent 需已設定才能使聊天運作
- 現有 `support_service.js` 和 `ai_service.js` API

### 外部
- 無需外部依賴
- HTML5 Drag and Drop API（瀏覽器原生）
- EventSource API（瀏覽器原生）

## 風險

| 風險 | 可能性 | 影響 | 緩解措施 |
|------|--------|------|----------|
| Odoo 階段未為專案設定 | 中 | 高 | 若無階段則新增預設備用階段 |
| 大量任務時拖曳效能問題 | 低 | 中 | 每欄超過 50 個任務時虛擬化渲染 |
| SSE 被 proxy/防火牆阻擋 | 低 | 高 | 記錄反向代理 SSE 設定文件 |
