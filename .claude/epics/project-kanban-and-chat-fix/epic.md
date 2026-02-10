---
name: project-kanban-and-chat-fix
status: backlog
created: 2026-02-10T09:00:00Z
progress: 0%
prd: .claude/prds/project-kanban-and-chat-fix.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/39
---

# Epic：專案看板視圖 & 聊天連線修復

## 概述

AI Assistant 模組的兩項平行改善：

1. **看板視圖** — 點擊專案時，將任務列表導航替換為看板視圖，使用 Odoo 的 `project.task.type` 階段作為欄位，支援拖曳功能。
2. **聊天修復** — 修正 SSE 串流錯誤處理鏈（後端回傳非 200 狀態碼 → EventSource 觸發 `onerror` → 使用者看到通用錯誤），並新增自動重連 + 連線狀態回饋。

## 架構決策

### 1. 複用 `updateTask()` 處理階段變更
現有的 `supportService.updateTask(taskId, { stage_id })` 已透過 `POST /api/support/tasks/detail/{taskId}` 搭配 `action: "update"` 支援階段更新。拖曳功能不需要新增端點 — 只需在序列化回傳中加入 `stage_id`。

### 2. 原生 HTML5 Drag & Drop
使用瀏覽器原生 DnD API 搭配 OWL 事件處理器（`t-on-dragstart`、`t-on-dragover`、`t-on-drop`）。不需外部套件，維持打包體積小。

### 3. 修正 SSE 錯誤傳播（根本原因）
**關鍵 Bug**：後端對「No AI agent available」和「AI provider not configured」等錯誤回傳 HTTP 400。當 `EventSource` 收到非 200 回應時，觸發 `onerror`（而非 `onmessage`），因此結構化錯誤 JSON **永遠不會被前端接收**。修正方式：SSE 端點永遠回傳 HTTP 200，將錯誤作為 SSE data 事件發送。

### 4. SSE 前新增預檢
建立 `EventSource` 前，前端應驗證 `channelId` 為有效數字。若 `channelId` 為 null/undefined（聊天未啟用），顯示啟用聊天的提示，而非嘗試注定失敗的連線。

## 技術方案

### 後端變更

**`src/controllers/ai_assistant.py`**：
- `_serialize_task()`：新增 `stage_id`、`priority`、`date_deadline`、`user_name` 欄位（修正 JSDoc/API 不一致）
- 新方法 `_get_project_stages(project_id)`：回傳專案的排序階段
- 新端點：`POST /api/support/projects/{project_id}/stages` → 回傳 `[{id, name, sequence}]`
- `api_ai_stream()`：將所有錯誤回傳從 HTTP 400/404 改為 HTTP 200，搭配錯誤 SSE data + `error_code` 欄位

**`src/models/project_task.py`**（不需修改 — `stage_id` 繼承自 Odoo）

### 前端變更

**新檔案**：
- `src/static/src/paas/pages/project-kanban/ProjectKanbanPage.js` — 看板頁面，含欄位 + 拖曳功能
- `src/static/src/paas/pages/project-kanban/ProjectKanbanPage.xml` — 模板
- `src/static/src/paas/styles/pages/_project_kanban.scss` — 樣式

**修改檔案**：
- `src/static/src/paas/core/router.js` — 新增路由 `ai-assistant/projects/:id` → `ProjectKanbanPage`
- `src/static/src/paas/pages/support-projects/SupportProjectsPage.js` — 修改 `navigateToProject()` 導航目標
- `src/static/src/paas/services/support_service.js` — 新增 `fetchProjectStages(projectId)` 方法
- `src/static/src/paas/components/ai-chat/AiChat.js` — 預檢、自動重連、連線狀態、錯誤碼處理
- `src/static/src/paas/components/ai-chat/AiChat.xml` — 連線狀態 UI
- `src/__manifest__.py` — 註冊新資源檔

## 實作策略

工作分為兩個獨立開發流，可平行進行：

**開發流 A：看板視圖**（任務 1-4）
- 後端 API 增強 → 看板頁面元件 → 拖曳功能 → 路由接線

**開發流 B：聊天修復**（任務 5-8）
- 後端 SSE 錯誤處理修正 → 前端連線改善 → UI 回饋

### 測試方式
- 透過瀏覽器手動測試 `http://localhost:8616/woow#/ai-assistant/projects`
- 驗證看板渲染階段、拖曳更新正常運作
- 驗證聊天在 provider 已設定時正常連線，未設定時顯示正確錯誤

## 任務拆解預覽

- [ ] 任務 1：後端 — 補齊 `_serialize_task()` 缺失欄位，新增專案階段列表端點
- [ ] 任務 2：前端 — 建立 `ProjectKanbanPage` 元件（JS + XML + SCSS）
- [ ] 任務 3：前端 — 實作看板上的拖曳階段更新
- [ ] 任務 4：前端 — 路由接線與導航修改（router.js + SupportProjectsPage.js + manifest）
- [ ] 任務 5：後端 — 修正 SSE 端點，永遠回傳 HTTP 200 搭配結構化錯誤碼
- [ ] 任務 6：前端 — AiChat 新增預檢驗證與錯誤碼處理
- [ ] 任務 7：前端 — AiChat 新增指數退避自動重連
- [ ] 任務 8：前端 — AiChat 新增連線狀態指示器 UI

## 依賴項

- Odoo `project.task.type` 階段需存在於專案中（Odoo 預設建立：New、In Progress、Done）
- AI provider + agent 需已設定才能使聊天運作（既有需求）
- 不需要新的外部套件

## 成功標準（技術面）

| 標準 | 衡量方式 |
|------|----------|
| 看板載入階段 | 所有 `project.task.type` 階段顯示為欄位 |
| 拖曳運作正常 | 放下後 DB 中 `stage_id` 已更新，UI 樂觀式更新 |
| 聊天已設定時可連線 | provider + agent 啟用時不觸發 `onerror` |
| 聊天顯示特定錯誤 | 後端錯誤碼對應至使用者友善訊息 |
| 自動重連運作正常 | 短暫斷線在 3 次重試內恢復 |

## 預估工作量

- **總任務數**：8
- **開發流 A（看板）**：4 個任務 — 中等複雜度
- **開發流 B（聊天）**：4 個任務 — 中等複雜度（1 後端 + 3 前端）
- **關鍵路徑**：各開發流內任務循序執行，兩個開發流可平行進行

## 已建立任務

### 開發流 A：看板視圖
- [ ] #40 - 後端 — 補齊任務序列化欄位與新增專案階段端點（parallel: true, S）
- [ ] #41 - 前端 — 建立 ProjectKanbanPage 看板元件（depends: #40, L）
- [ ] #42 - 前端 — 實作看板拖曳階段更新（depends: #41, M）
- [ ] #46 - 前端 — 路由接線與導航修改（depends: #41, S）

### 開發流 B：聊天修復
- [ ] #43 - 後端 — 修正 SSE 端點錯誤處理（parallel: true, S）
- [ ] #44 - 前端 — AiChat 預檢驗證與錯誤碼處理（depends: #43, S）
- [ ] #45 - 前端 — AiChat 指數退避自動重連（depends: #44, M）
- [ ] #47 - 前端 — AiChat 連線狀態指示器 UI（depends: #45, M）

**總任務數**：8
**可平行任務**：2（#40 + #43 可同時開始）
**循序任務**：6
**預估總工時**：19-28 小時
