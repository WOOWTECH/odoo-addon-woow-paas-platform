---
name: ai-assistant
status: backlog
created: 2026-02-09T16:30:02Z
progress: 0%
prd: .claude/prds/ai-assistant.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/21
---

# Epic: AI Assistant

## Overview

在 woow_paas_platform 的 standalone OWL app (`/woow`) 中實作 AI Assistant 功能。核心能力：

1. **AI Provider/Agent 後台管理** — 多組 OpenAI compatible provider 設定 + 可自訂 AI agent
2. **任務內嵌多人群組聊天** — 基於 `discuss.channel` + `mail.message` + `bus`，支援多人即時對話 + AI 串流回覆
3. **@ Mention AI Agent** — 輸入 `@` 呼叫特定 AI agent 加入討論
4. **Support Projects/Tasks** — 基於 `project.project` + `project.task`，完整任務管理 + Kanban

**架構核心決策**：用 `_inherit` 擴展 Odoo 原生 model（而非自建 6 個 `_name` model），重用 mail/discuss/bus 生態，大幅減少開發量。

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| 對話儲存 | `_inherit` 擴展 `discuss.channel` + `mail.message` | 天然多人群組聊天、bus 即時通知、附件系統；Woow_odoo_task_ai_solver 已驗證 |
| 任務管理 | `_inherit` 擴展 `project.project` + `project.task` | 重用 Odoo 專案/任務 CRUD、stage 管理、指派人 |
| AI 配置 | `_name` 新建 `ai_provider` + `ai_agent` | 獨立業務邏輯，無對應原生 model |
| API 風格 | JSON-RPC POST（同 `paas.py`） | 與現有 controller 一致 |
| AI 串流 | SSE via HTTP controller (`type='http'`) | API Key 不暴露前端，server-side proxy |
| @ Mention | 前端 `@` 偵測 + agent dropdown | 輕量實作，參考 UI 設計圖 |
| 新增依賴 | `project`, `mail`, `bus` | `_inherit` 擴展所需 |
| State Management | `ai_service.js` + `support_service.js` reactive | 同 `workspace_service.js` 模式 |

### Model 策略

| Model | 模式 | 說明 |
|-------|------|------|
| `woow_paas_platform.ai_provider` | `_name` 全新 | AI Provider 設定 |
| `woow_paas_platform.ai_agent` | `_name` 全新 | AI Agent 定義 |
| `project.project` | `_inherit` 擴展 | 新增 `workspace_id` |
| `project.task` | `_inherit` 擴展 | 新增 `chat_enabled`, `channel_id`, `ai_auto_reply` |
| `discuss.channel` | `_inherit` 擴展 | Override `message_post()` 觸發 AI + bus |

### 重用 Woow_odoo_task_ai_solver 模式

| Pattern | Source | Usage |
|---------|--------|-------|
| Lazy Channel Creation | `project_task.py` `_create_chat_channel()` | 任務首次開 Chat 自動建 channel |
| Bus Notifications | `discuss_channel.py` `message_post` override | 多人即時同步 |
| Access Validation | `portal.py` `_validate_portal_channel_access()` | 權限檢查 |
| Chat History API | `portal.py` 訊息歷史 + attachment enrichment | 聊天記錄 |
| File Upload | `portal.py` 附件上傳 + access token | 附件上傳 |

## Technical Approach

### Backend

**全新 Models（`_name`）：**
- `ai_provider` — name, api_base_url, api_key, model_name, is_active, max_tokens, temperature
- `ai_agent` — name, display_name, system_prompt, provider_id, avatar_color, is_default

**擴展 Models（`_inherit`）：**
- `project.project` — 新增 `workspace_id` (Many2one → workspace)
- `project.task` — 新增 `chat_enabled`, `channel_id`, `ai_auto_reply`；`_create_chat_channel()` lazy creation
- `discuss.channel` — Override `message_post()` 觸發 AI 回覆 + bus 通知

**AI Client：** `ai_client.py`
- OpenAI compatible HTTP client (`requests`)
- `chat_completion()` 一般回覆 + `chat_completion_stream()` SSE 串流
- Messages array 組裝（system prompt + history + user message）

**Controller：** `ai_assistant.py`
- `/api/ai/providers` — 列出 providers（GET）
- `/api/ai/agents` — 列出 agents（GET）
- `/api/ai/stream/<int:channel_id>` — SSE 串流（HTTP type）
- `/api/ai/chat/history` — 聊天歷史（JSON-RPC）
- `/api/ai/chat/post` — 發送訊息（JSON-RPC）
- `/api/ai/chat/upload` — 附件上傳（HTTP multipart）
- `/api/support/projects/<int:workspace_id>` — 專案 CRUD（JSON-RPC）
- `/api/support/tasks/<int:workspace_id>` — 任務 CRUD（JSON-RPC）
- `/api/support/tasks/<int:task_id>` — 任務詳情（JSON-RPC）

### Frontend

**Pages：**
- `AiAssistantPage` — Hub（統計卡片 + 導航）
- `SupportProjectsPage` — 專案列表（card grid + search + filter）
- `SupportTasksPage` — Kanban（按專案分組 + search + filter）
- `TaskDetailPage` — 詳情（info panel + tabs: Description / Sub-tasks / Chat）

**Components：**
- `AiChat` — 訊息列表 + 輸入框 + SSE 串流 + 附件上傳
- `AiMentionDropdown` — `@` 偵測 + agent 下拉

**Services：**
- `ai_service.js` — agents, streaming, chat history
- `support_service.js` — projects, tasks CRUD

**路由（hash-based，新增至 `router.js`）：**
```
ai-assistant                          → AiAssistantPage
ai-assistant/projects                 → SupportProjectsPage
ai-assistant/tasks                    → SupportTasksPage
ai-assistant/tasks/:taskId            → TaskDetailPage
ai-assistant/chat/:conversationId     → AiChat（全頁）
```

### Infrastructure

- **SSE**：Odoo 18 HTTP controller `type='http'` + `werkzeug.Response` with `text/event-stream`
- **API Key 安全**：所有 LLM 呼叫走 server-side，前端不接觸 API Key
- **附件**：使用 `ir.attachment`，前端 multipart upload
- **即時通訊**：Odoo bus 模組，多人同步

## Implementation Strategy

採用 **垂直切片** — 每個 task 交付一段可測試的端到端功能。

### Phase 1: Backend Foundation（Task 1-3）
建立所有 models、settings、AI client、discuss.channel 擴展和 controller API

### Phase 2: Frontend Foundation（Task 4-5）
建立 routing、Hub 頁面、AiChat 元件、@ mention

### Phase 3: Support System（Task 6-8）
Support Projects + Tasks UI + Task Detail（嵌入 Chat）

### Phase 4: Integration（Task 9-10）
全頁 Chat、workspace 整合、測試、打磨

## Task Breakdown (10 Tasks)

- [ ] **Task 1: AI Provider + Agent Models + Settings UI** — 建立 `ai_provider`、`ai_agent` models（`_name`），擴展 `res_config_settings`，建立 views + 預設 WoowBot data + ACL，新增 `project`/`mail`/`bus` 依賴到 `__manifest__.py`
- [ ] **Task 2: Extend project.project + project.task** — `_inherit` 擴展加入 `workspace_id`、`chat_enabled`、`channel_id`、`ai_auto_reply`，實作 `_create_chat_channel()` lazy creation + `write()` hook，建立 `project_task_views.xml`
- [ ] **Task 3: Extend discuss.channel + AI Client + Controller** — Override `message_post()` 觸發 AI 回覆 + bus 通知，建立 `ai_client.py` OpenAI HTTP client（含 streaming），建立 `ai_assistant.py` controller（全部 API endpoints）
- [ ] **Task 4: Frontend Routing + Hub + Services** — 擴展 `router.js` + `root.js/xml` + `Sidebar.js/xml`，建立 `AiAssistantPage`（Hub），建立 `ai_service.js` + `support_service.js` reactive services
- [ ] **Task 5: AiChat Component + @ Mention** — 建立 `AiChat` OWL 元件（訊息列表、輸入框、SSE EventSource、附件上傳），建立 `AiMentionDropdown`（`@` 偵測 + agent 下拉），建立 `AiChat.scss`
- [ ] **Task 6: Support Projects Page** — 建立 `SupportProjectsPage`（card grid），搜尋 + 篩選 + grid/list 切換，Apply for Project 功能，`_support_projects.scss`
- [ ] **Task 7: Support Tasks Page (Kanban)** — 建立 `SupportTasksPage`（Kanban by project），任務卡片 + My & Shared 篩選，`_support_tasks.scss`
- [ ] **Task 8: Task Detail Page + Embedded Chat** — 建立 `TaskDetailPage`（info panel + Description/Sub-tasks/Chat tabs），Chat tab 嵌入 `AiChat`，`_task_detail.scss`
- [ ] **Task 9: AI Chat Full Page + Workspace Integration** — 建立全頁 Chat 路由，Settings 連線測試按鈕，混合模式 auto_reply toggle UI
- [ ] **Task 10: Integration Testing + Polish** — 端到端測試（Provider → 訊息 → AI 串流），錯誤處理，UI 對照設計圖，安全審查

## Implementation Order

```
Task 1 (Models + Settings)
  ↓
Task 2 (project extensions)  ←── Task 3 (discuss.channel + AI Client + Controller)
  ↓                                    ↓
Task 4 (Routing + Hub + Services)    Task 5 (AiChat Component)
  ↓                                    ↓
Task 6 (Projects Page)              Task 8 (Task Detail + Chat)
  ↓
Task 7 (Tasks Kanban)
  ↓
Task 9 (Full Page Chat + Integration)
  ↓
Task 10 (Testing + Polish)
```

## Dependencies

### Prerequisites（已完成）
- `woow_paas_platform.workspace` model ✓
- `woow_paas_platform.workspace_access` model ✓
- Standalone OWL app shell + router ✓
- AppShell / Sidebar / Header layout ✓
- WoowCard / WoowButton / WoowIcon components ✓
- `workspace_service.js` / `cloud_service.js` service pattern ✓

### External Dependencies
- OpenAI Compatible API 服務（使用者自行提供）
- Python `requests`（Odoo 已內建）
- Odoo 18 modules: `project`, `mail`, `bus`

### Internal Task Dependencies
- Task 2, 3 depend on Task 1
- Task 4 independent（可與 Task 2-3 平行）
- Task 5 depends on Task 3
- Task 6-7 depend on Task 4
- Task 8 depends on Task 2 + Task 5
- Task 9 depends on Task 5
- Task 10 depends on all

## Existing Code to Reuse

| Pattern | Source | Usage |
|---------|--------|-------|
| JSON-RPC `jsonRpc()` | `services/workspace_service.js` | ai_service / support_service |
| Reactive service | `workspace_service.js` / `cloud_service.js` | service pattern |
| Router | `core/router.js` | 新增路由 |
| Page component | `WorkspaceDetailPage.js` | TaskDetailPage 參考 |
| Card/Button/Icon | `WoowCard.js` / `WoowButton.js` / `WoowIcon.js` | 直接使用 |
| Modal | `CreateWorkspaceModal.js` | 各 modal 參考 |
| SCSS variables | `styles/00_variables.scss` | 樣式一致性 |
| Controller | `controllers/paas.py` | API response format |
| Settings | `models/res_config_settings.py` | AI settings 擴展 |
| Chat patterns | `Woow_odoo_task_ai_solver/` | channel creation / bus / history / upload |

## Success Criteria (Technical)

| Criteria | Target |
|----------|--------|
| AI 對話端到端 | Provider 設定 → 發訊息 → SSE 串流回覆 |
| 多人群組聊天 | 多成員同一 Chat 即時同步 |
| @ mention | 選 agent → 對應 system prompt 回覆 |
| Bus 通知 | 發訊息 → 其他成員即時收到 |
| Settings 連線測試 | 按鈕驗證 API 連線 |
| 任務 CRUD | 基於 project.project + project.task |
| API Key 安全 | 前端不接觸 API Key |
| ACL | ir.model.access.csv 覆蓋 ai_provider, ai_agent |
| UI | 對照設計圖驗證 |

## Estimated Effort

| Phase | Tasks | 估計 |
|-------|-------|------|
| Phase 1: Backend Foundation | Task 1-3 | 中等 |
| Phase 2: Frontend Foundation | Task 4-5 | 較高（SSE 串流技術難點） |
| Phase 3: Support System | Task 6-8 | 中等（CRUD + UI） |
| Phase 4: Integration | Task 9-10 | 中等 |

**Critical Path**: Task 1 → Task 3 → Task 5 → Task 8

## Tasks Created
- [ ] #22 (22.md) - AI Provider + Agent Models + Settings UI (parallel: false)
- [ ] #24 (24.md) - Extend project.project + project.task (parallel: true)
- [ ] #25 (25.md) - Extend discuss.channel + AI Client + Controller (parallel: true)
- [ ] #28 (28.md) - Frontend Routing + Hub + Services (parallel: true)
- [ ] #31 (31.md) - AiChat Component + @ Mention (parallel: false)
- [ ] #23 (23.md) - Support Projects Page (parallel: true)
- [ ] #26 (26.md) - Support Tasks Page / Kanban (parallel: true)
- [ ] #27 (27.md) - Task Detail Page + Embedded Chat (parallel: false)
- [ ] #29 (29.md) - AI Chat Full Page + Workspace Integration (parallel: true)
- [ ] #30 (30.md) - Integration Testing + Polish (parallel: false)

Total tasks: 10
Parallel tasks: 5 (#24, #25, #28, #23, #26, #29)
Sequential tasks: 5 (#22, #31, #27, #29, #30)
Estimated total effort: 42-60 hours
