---
name: ai-assistant
description: Workspace 內嵌 AI 助手功能，支援多人群組聊天 + AI agent @ mention、多組 OpenAI compatible provider 設定、任務管理與專案追蹤。採用 _inherit 擴展 project.task + discuss.channel 實現。
status: backlog
created: 2026-02-09T14:58:30Z
updated: 2026-02-09T15:57:18Z
---

# PRD: AI Assistant

## Executive Summary

在 woow_paas_platform 的 standalone OWL app (`/woow`) 中，為每個 Workspace 內嵌 AI 助手功能。使用者可以透過對話介面與 AI 互動，使用 `@` mention 機制呼叫特定 AI agent 加入對話。系統支援多組 OpenAI compatible AI provider 設定，整合任務管理（Support Tasks）、專案追蹤（Support Projects）等功能，提供完整的 AI 驅動支援體驗。

**核心價值**：讓 PaaS 平台使用者在 Workspace 內直接獲得 AI 協助，結合任務管理流程，提升問題解決效率。

**架構策略**：採用 `_inherit` 擴展 Odoo 原生的 `project.project`、`project.task`、`discuss.channel` model，而非自建獨立 model。這樣可以重用 Odoo 的 mail/discuss/bus 基礎設施，天然支援多人群組聊天和即時通知。

## Problem Statement

### 現狀問題
1. 目前 Workspace 缺乏內建的 AI 助手功能，使用者需要切換到外部工具進行 AI 對話
2. 支援工單和任務管理散落在不同系統中，無法與 AI 對話聯動
3. 缺乏統一的 AI provider 管理機制，無法靈活切換不同的 AI 服務

### 為什麼現在做
- AI 助手已成為 SaaS 平台標配功能
- 使用者期望在工作環境中直接獲得 AI 協助
- 整合 AI 能力可以顯著提升平台的競爭力和使用者黏著度

## User Stories

### Persona 1: Workspace 管理員
- **作為** Workspace 管理員，**我想要** 在 Workspace 內使用 AI 助手與團隊協作，**以便** 提升工作效率
- **作為** Workspace 管理員，**我想要** 管理支援專案和任務，**以便** 追蹤團隊的支援工作進度
- **作為** Workspace 管理員，**我想要** 選擇頻道的 AI 回覆模式（自動/手動），**以便** 根據場景調整 AI 互動方式
- **驗收標準**：
  - 可在任務中開啟 AI 對話（基於 discuss.channel）
  - 多個成員可在同一任務 Chat 中看到彼此和 AI 的訊息
  - 可管理支援專案與任務
  - 可切換頻道的 AI 回覆模式
  - AI Provider 由系統管理員在後台統一設定

### Persona 2: 一般使用者
- **作為** 一般使用者，**我想要** 在任務討論中與 AI 對話，**以便** 快速獲得問題解答
- **作為** 一般使用者，**我想要** 透過 `@` mention 呼叫特定 AI agent，**以便** 針對不同類型的問題獲得專業回答
- **作為** 一般使用者，**我想要** 查看和管理我的支援任務，**以便** 追蹤問題處理進度
- **驗收標準**：
  - 可在任務 Chat tab 中開啟 AI 對話
  - 輸入 `@` 時顯示可用 agent 清單
  - AI 回覆即時串流顯示
  - 其他成員的訊息透過 bus 即時推送
  - 可查看任務狀態（New / In Progress / Done）

### Persona 3: 系統管理員
- **作為** 系統管理員，**我想要** 在 Odoo 後台配置 AI provider，**以便** 全域管理 AI 服務連線
- **驗收標準**：
  - 在 Settings → Woow PaaS 中可設定多組 AI provider
  - 每組 provider 可設定名稱、API URL、API Key、Model
  - 可標記一組為 active（使用中）
  - 支援 OpenAI compatible API 格式

## Requirements

### Functional Requirements

#### FR-1: AI Provider 設定（後台）
- **FR-1.1**: 在 Odoo 後台 Settings → Woow PaaS 中新增「AI Providers」區塊
- **FR-1.2**: 支援新增多組 AI Provider 設定，每組包含：
  - Provider 名稱（如 OpenAI、Azure OpenAI、Local LLM）
  - API Base URL（OpenAI compatible endpoint）
  - API Key（加密儲存）
  - Model 名稱（如 gpt-4、claude-3-sonnet）
  - 啟用/停用開關
- **FR-1.3**: 可選擇一組作為 active provider
- **FR-1.4**: 提供連線測試按鈕，驗證 API 連線是否正常

#### FR-2: AI 對話功能（基於 discuss.channel + mail.message）
- **FR-2.1**: 每個任務可啟用 Chat 頻道（Lazy creation，首次使用時自動建立 discuss.channel）
- **FR-2.2**: 對話介面支援即時文字訊息發送（透過 mail.message）
- **FR-2.3**: AI 回覆支援串流式顯示（SSE/streaming）
- **FR-2.4**: 對話歷史持久化儲存（mail.message），重新開啟時可載入歷史
- **FR-2.5**: 支援 Enter 發送訊息、Shift+Enter 換行
- **FR-2.6**: 訊息顯示發送者名稱、頭像、時間戳
- **FR-2.7**: 多人即時同步（透過 bus 通知，一個用戶發送訊息，其他成員即時收到更新）

#### FR-3: @ Mention AI Agent 機制
- **FR-3.1**: 輸入 `@` 時顯示可用 AI agent 下拉清單
- **FR-3.2**: 預設提供 `@WoowBot` 通用 AI agent
- **FR-3.3**: 混合模式：
  - 頻道可設定「自動回覆」模式（每則訊息自動由預設 agent 回覆）
  - 頻道可設定「手動模式」（僅在 @ mention 時 AI 才回覆）
- **FR-3.4**: @ mention 特定 agent 時，該 agent 以其角色設定回覆
- **FR-3.5**: 支援自訂 AI agent（名稱、system prompt、model 偏好）

#### FR-4: AI Assistant Support Hub（主頁面）
依據 UI 設計圖，AI Assistant 主頁面包含：
- **FR-4.1**: 統計概覽卡片（Total 支援項目數、Active 數、完成度）
- **FR-4.2**: Support Projects 卡片 - 連結到支援專案列表
- **FR-4.3**: My Support Tasks 卡片 - 連結到個人支援任務列表
- **FR-4.4**: AI Connection & Docs 卡片 - 顯示 AI 連線狀態與設定入口

#### FR-5: Support Projects（支援專案，基於 project.project）
- **FR-5.1**: 專案列表頁面，卡片式 grid 展示
- **FR-5.2**: 每個專案卡片顯示：名稱、狀態徽章、日期範圍、任務數、團隊成員頭像
- **FR-5.3**: 支援搜尋與篩選
- **FR-5.4**: 支援 grid/list 視圖切換
- **FR-5.5**: 「Apply for Project」申請加入專案功能

#### FR-6: My Support Tasks（支援任務，基於 project.task）
- **FR-6.1**: 任務列表頁面，按專案分組的 Kanban 佈局
- **FR-6.2**: 任務卡片顯示：標題、優先級星號、截止日期、任務 ID、完成度、指派人頭像
- **FR-6.3**: 「My & Shared Tasks」篩選按鈕
- **FR-6.4**: 支援搜尋與多種視圖（list、board、calendar 等）

#### FR-7: Task Detail（任務詳情）
- **FR-7.1**: 任務標題、狀態徽章（New / In Progress / Done）
- **FR-7.2**: 任務資訊面板：Milestone、Assignees、Tags、Deadline、Customer、Company
- **FR-7.3**: 分頁：Description、Sub-tasks、Chat
- **FR-7.4**: Sub-tasks 表格：標題、指派人、截止日、狀態
- **FR-7.5**: Chat 分頁整合 AI 對話功能（基於 discuss.channel），顯示對話歷史與系統訊息（如狀態變更）
- **FR-7.6**: 附件支援（圖片預覽、檔案下載）

### Non-Functional Requirements

#### NFR-1: 效能
- AI 回覆首 token 延遲 < 2 秒
- 對話歷史載入 < 1 秒（前 50 筆訊息）
- @ mention 下拉清單回應 < 200ms

#### NFR-2: 安全
- API Key 加密儲存，不以明文顯示
- AI 對話內容依 Workspace 權限隔離
- API 請求走 server-side proxy，前端不直接接觸 API Key

#### NFR-3: 可擴展性
- AI Provider 介面使用 OpenAI compatible 標準，方便整合各種 LLM
- Agent 系統設計允許未來擴展（如 RAG、工具呼叫、多模態）
- 對話基於 mail.message，天然支援分頁載入

#### NFR-4: 可用性
- 符合現有 `/woow` app 的設計語言（Manrope/Outfit 字體、Material Symbols）
- 響應式佈局，支援桌面和平板
- 鍵盤快捷鍵支援（Enter 發送、Shift+Enter 換行）

## Technical Architecture

### 架構決策：`_inherit` 擴展 vs 全新 `_name`

經過討論，本功能採用 **`_inherit` 擴展 Odoo 原生 model** 的方式，而非自建 6 個全新 `_name` model。

**理由**：
1. **多人群組聊天**：`discuss.channel` + `mail.message` + `bus` 天然支援多人即時通訊
2. **減少重複造輪**：不需自行實作對話、訊息、專案、任務的 CRUD 邏輯
3. **生態整合**：可直接使用 Odoo 的附件系統、訊息通知、活動記錄等功能
4. **參考成功案例**：`Woow_odoo_task_ai_solver` 專案已驗證此模式可行

### 後端模型

#### Model 策略

| Model | 模式 | 說明 |
|-------|------|------|
| `woow_paas_platform.ai_provider` | `_name`（全新） | AI Provider 設定 |
| `woow_paas_platform.ai_agent` | `_name`（全新） | AI Agent 定義 |
| `project.project` | `_inherit`（擴展） | 新增 `workspace_id` 欄位 |
| `project.task` | `_inherit`（擴展） | 新增 `chat_enabled`, `channel_id`, `ai_auto_reply` 等 |
| `discuss.channel` | `_inherit`（擴展） | Override `message_post()` 觸發 AI 回覆 + bus 通知 |

#### 原方案 → 修訂方案 對照

| 原方案 (6 個 `_name`) | 修訂方案 | 說明 |
|---|---|---|
| `woow_paas_platform.ai_provider` | `woow_paas_platform.ai_provider` | 不變 |
| `woow_paas_platform.ai_agent` | `woow_paas_platform.ai_agent` | 不變 |
| `woow_paas_platform.ai_conversation` | 移除，用 `discuss.channel` | `_inherit` 擴展 |
| `woow_paas_platform.ai_message` | 移除，用 `mail.message` | Odoo 內建 |
| `woow_paas_platform.support_project` | 移除，用 `project.project` | `_inherit` 擴展 |
| `woow_paas_platform.support_task` | 移除，用 `project.task` | `_inherit` 擴展 |

#### 新增模組依賴

原依賴：`base`, `web`
新增依賴：**`project`**, **`mail`**, **`bus`**

#### AI Provider Model
```python
class WoowAiProvider(models.Model):
    _name = 'woow_paas_platform.ai_provider'
    name = fields.Char(required=True)           # Provider 名稱
    api_base_url = fields.Char(required=True)   # OpenAI compatible API URL
    api_key = fields.Char(required=True)        # API Key（加密）
    model_name = fields.Char(required=True)     # Model 名稱
    is_active = fields.Boolean(default=False)   # 是否啟用
    max_tokens = fields.Integer(default=4096)   # 最大 token 數
    temperature = fields.Float(default=0.7)     # 溫度參數
```

#### AI Agent Model
```python
class WoowAiAgent(models.Model):
    _name = 'woow_paas_platform.ai_agent'
    name = fields.Char(required=True)           # Agent 名稱（如 WoowBot）
    display_name = fields.Char()                # 顯示名稱
    system_prompt = fields.Text()               # System Prompt
    provider_id = fields.Many2one('woow_paas_platform.ai_provider')
    avatar_color = fields.Char()                # 頭像顏色
    is_default = fields.Boolean(default=False)  # 是否為預設 agent
```

#### project.task 擴展
```python
class ProjectTask(models.Model):
    _inherit = 'project.task'
    chat_enabled = fields.Boolean(default=False)
    channel_id = fields.Many2one('discuss.channel')
    ai_auto_reply = fields.Boolean(default=True)

    def _create_chat_channel(self):
        """Lazy channel creation — 參考 Woow_odoo_task_ai_solver 模式"""
        ...
```

#### discuss.channel 擴展
```python
class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    def message_post(self, **kwargs):
        """Override: 發送訊息後觸發 AI 回覆 + bus 通知"""
        message = super().message_post(**kwargs)
        # 偵測 @ mention → 觸發 AI 回覆
        # 發送 bus notification
        return message
```

### API Endpoints（新增）

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/ai/providers` | 列出 AI Providers（前台用，隱藏 API Key） |
| GET | `/api/ai/agents` | 列出可用 AI Agents |
| GET | `/api/ai/stream/<int:channel_id>` | SSE 串流端點（type='http'） |
| POST | `/api/ai/chat/history` | 聊天歷史（JSON-RPC） |
| POST | `/api/ai/chat/post` | 發送訊息（JSON-RPC） |
| POST | `/api/ai/chat/upload` | 附件上傳（HTTP multipart） |
| POST | `/api/support/projects/<int:workspace_id>` | 支援專案 CRUD（JSON-RPC） |
| POST | `/api/support/tasks/<int:workspace_id>` | 支援任務 CRUD（JSON-RPC） |
| POST | `/api/support/tasks/<int:task_id>` | 任務詳情（JSON-RPC） |

### 重用 Woow_odoo_task_ai_solver 的模式

| Pattern | Source | Usage |
|---------|--------|-------|
| Lazy Channel Creation | `project_task.py:25-56` 的 `_create_chat_channel()` | 任務首次開啟 Chat 時自動建立 channel |
| Bus Notifications | `discuss_channel.py:11-25` 的 `message_post` override | 多人即時同步 |
| Access Validation | `portal.py:25-98` 的 `_validate_portal_channel_access()` | 權限檢查 |
| Chat History API | `portal.py:126-167` 的訊息歷史 + attachment enrichment | 聊天記錄 API |
| File Upload | `portal.py:169-217` 的附件上傳 + access token | 附件上傳 |

### 前端元件架構

```
paas/
├── pages/
│   ├── ai-assistant/              # AI Assistant Hub 主頁面
│   │   ├── AiAssistantPage.js
│   │   └── AiAssistantPage.xml
│   ├── support-projects/          # 支援專案列表
│   │   ├── SupportProjectsPage.js
│   │   └── SupportProjectsPage.xml
│   ├── support-tasks/             # 支援任務列表（Kanban）
│   │   ├── SupportTasksPage.js
│   │   └── SupportTasksPage.xml
│   └── task-detail/               # 任務詳情（含 Chat）
│       ├── TaskDetailPage.js
│       └── TaskDetailPage.xml
├── components/
│   ├── ai-chat/                   # AI 對話元件
│   │   ├── AiChat.js
│   │   ├── AiChat.xml
│   │   └── AiChat.scss
│   └── ai-mention/                # @ mention 下拉選單
│       ├── AiMentionDropdown.js
│       └── AiMentionDropdown.xml
├── services/
│   ├── ai_service.js              # AI API 客戶端
│   └── support_service.js         # 支援專案/任務 API 客戶端
└── styles/
    └── pages/
        ├── _ai_assistant.scss
        ├── _support_projects.scss
        ├── _support_tasks.scss
        └── _task_detail.scss
```

### 路由（Hash-based）

```
#/ai-assistant                      → AiAssistantPage（Hub）
#/ai-assistant/projects             → SupportProjectsPage
#/ai-assistant/tasks                → SupportTasksPage
#/ai-assistant/tasks/:id            → TaskDetailPage
#/ai-assistant/chat                 → AI 對話頁面（獨立）
#/ai-assistant/chat/:conversationId → 特定對話
```

### AI Provider 整合架構

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────────┐
│   OWL App    │────▶│  Odoo Controller  │────▶│  OpenAI          │
│  (Frontend)  │ API │  (Server-side     │HTTP │  Compatible API  │
│              │◀────│   Proxy)          │◀────│  (Any Provider)  │
│              │ SSE │                   │     │                  │
└──────────────┘     └───────────────────┘     └──────────────────┘
                            │
                     ┌──────┴──────┐
                     │  discuss +  │
                     │  mail + bus │
                     └─────────────┘
```

**Server-side Proxy 的好處**：
- API Key 不暴露到前端
- 可以進行 rate limiting 和 usage tracking
- 統一處理不同 provider 的差異
- 支援 SSE 串流轉發

## Success Criteria

| Metric | Target |
|--------|--------|
| AI 對話可用性 | 99% uptime（不含 provider 問題） |
| 首 token 延遲 | < 2 秒 |
| 對話歷史載入 | < 1 秒 |
| 多人即時同步 | 一個用戶發訊息，其他成員 < 1 秒內收到 |
| Provider 設定成功率 | 連線測試通過率 > 95% |
| 使用者滿意度 | AI 回覆有幫助率 > 70% |

## Constraints & Assumptions

### 技術限制
- AI Provider 必須相容 OpenAI Chat Completions API 格式
- 串流回覆依賴 SSE（Server-Sent Events），需確認 Odoo HTTP 框架支援
- 前端為 standalone OWL app，聊天 UI 自行實作（但後端使用 discuss.channel + mail.message）
- 圖片/附件大小限制 10MB
- 新增 `project`、`mail`、`bus` 模組依賴

### 假設
- 使用者已有可用的 OpenAI compatible AI 服務
- Odoo 18 的 HTTP controller 支援 SSE 串流回應
- 現有的 Workspace 和 WorkspaceAccess 模型可正常運作
- Woow_odoo_task_ai_solver 的 discuss.channel + bus 模式可直接參考

### Timeline
- 依據 Epic 分解後決定

## Out of Scope

以下功能不在本 PRD 範圍內：
1. **多模態 AI**（圖片理解、語音輸入）- 未來版本
2. **RAG（Retrieval-Augmented Generation）** - 未來版本
3. **AI 工具呼叫（Function Calling）** - 未來版本
4. **AI 用量計費** - 未來版本
5. **Portal 使用者 AI 對話** - 未來版本
6. **Mobile app 原生支援** - 依賴響應式設計
7. **多語言 AI 回覆偏好設定** - 未來版本

## Dependencies

### Internal Dependencies
- `woow_paas_platform.workspace` model（已完成）
- `woow_paas_platform.workspace_access` model（已完成）
- Standalone OWL app shell（已完成）
- Router、Layout、Components 基礎元件（已完成）

### External Dependencies
- OpenAI Compatible API 服務（使用者自行提供）
- Odoo 18 modules: `base`, `web`, `project`, `mail`, `bus`

### 參考資源
- UI 設計圖：`resource/stitch_saas_web_app_ai_assistant_2026-02-09/`（9 張設計稿）
- 聊天架構參考：`Woow_odoo_task_ai_solver` 專案（discuss.channel + mail.message + bus 模式，**本功能直接採用此架構**）
