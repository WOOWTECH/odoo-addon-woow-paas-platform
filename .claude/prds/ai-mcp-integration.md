---
name: ai-mcp-integration
description: Enable AI Assistant to use external tools via MCP (Model Context Protocol) with system-level and user-level server management
status: backlog
created: 2026-02-26T15:49:19Z
---

# PRD: AI MCP Integration

## Executive Summary

為 AI Assistant 加入 MCP（Model Context Protocol）支援，讓 AI 能在對話中調用外部工具（查資料庫、搜文件、操作 API 等）。MCP Server 管理分為兩層：

1. **系統層級** — Admin 在 Odoo 後台為每個 `ai.assistant` 設定可用的 MCP servers + 開關個別 tools
2. **使用者層級** — 使用者可在自己的 Cloud Service 上新增自定義 MCP servers，讓該 service 的 AI 對話也能使用

技術上透過 LangChain 的 `langchain-mcp-adapters` 套件，將 MCP tools 轉成 LangChain tools，搭配 `ChatOpenAI.bind_tools()` 實現 function calling + streaming。

## Problem Statement

### 現況

AI Assistant 目前只能進行純文字對話（`chat_completion` / `chat_completion_stream`），無法執行任何外部操作。當使用者問 "我的 n8n 服務狀態如何？" 或 "幫我查一下這個客戶的訂單"，AI 只能根據 system prompt 中靜態注入的 context 回答，無法即時查詢。

### 為什麼現在做

- MCP 已成為 AI tool calling 的業界標準協議（Anthropic 主導，OpenAI/Google 跟進）
- LangChain 已有成熟的 MCP adapter（`langchain-mcp-adapters`），降低實作成本
- 平台已有完整的 AI 對話 + SSE 串流基礎設施
- 使用者開始要求 AI 能做更多事（不只是聊天）

## User Stories

### US-1: Admin 設定系統 MCP Server

**作為** Odoo Admin
**我想要** 在後台新增 MCP Server 設定（URL + API Key + 名稱）
**以便** AI Assistant 可以使用這些外部工具

**Acceptance Criteria:**
- 可在 Settings → Woow PaaS 或獨立選單中管理 MCP Server 列表
- 每個 MCP Server 設定包含：名稱、SSE endpoint URL、API Key（選填）、描述
- 可測試連線（驗證 URL 可達 + 取得 tool list）
- 新增後系統自動拉取該 server 提供的 tools 清單

### US-2: Admin 為 AI Assistant 分配 MCP Tools

**作為** Odoo Admin
**我想要** 為每個 AI Assistant 選擇可使用的 MCP servers 及個別 tools
**以便** 控制不同 Assistant 的能力範圍

**Acceptance Criteria:**
- AI Assistant form view 中新增 MCP 設定區塊
- 可勾選要啟用的 MCP servers（Many2many）
- 對每個啟用的 server，可進一步開關個別 tools
- 未啟用的 tools 不會傳給 LLM

### US-3: AI 在對話中使用 MCP Tools

**作為** 使用者
**我想要** AI 能在對話中自動判斷何時調用工具並回傳結果
**以便** 獲得即時、精確的資訊

**Acceptance Criteria:**
- AI 對話中能觸發 tool calling（function calling protocol）
- Tool 呼叫和結果在 SSE 串流中正確處理
- 使用者能看到 AI 正在調用工具的指示（loading/thinking 狀態）
- Tool 執行失敗時，AI 能優雅處理並回報錯誤
- 對話歷史中記錄 tool calls 和 results（可選顯示）

### US-4: 使用者自定義 MCP Server（Cloud Service Level）

**作為** Cloud Service 擁有者
**我想要** 在我的 Cloud Service 設定頁面中新增自己的 MCP Server
**以便** AI 在討論這個 service 相關的任務時，能使用我自定義的工具

**Acceptance Criteria:**
- Cloud Service 設定頁面（或新 tab）可新增 MCP Server
- 僅限該 Cloud Service 相關的對話中使用
- 使用者只需填入 SSE endpoint URL 和 API Key
- 系統自動 discover tools 並顯示可用列表
- 使用者可開關個別 tools

### US-5: Tool 執行可視化

**作為** 使用者
**我想要** 在 AI 回覆中看到工具調用的過程和結果
**以便** 理解 AI 如何得出答案

**Acceptance Criteria:**
- AI 回覆中包含 tool call indicator（折疊式）
- 展開可看到：tool 名稱、輸入參數、執行結果
- 多次 tool call 按順序顯示
- Tool call 在串流過程中即時顯示（不等全部完成）

## Requirements

### Functional Requirements

#### FR-1: MCP Server Model（`woow_paas_platform.mcp_server`）

| Field | Type | Description |
|-------|------|-------------|
| `name` | Char | Server 名稱（required） |
| `url` | Char | SSE endpoint URL（required） |
| `api_key` | Char | API Key（optional, password field） |
| `headers_json` | Text | 自訂 HTTP headers（JSON，optional） |
| `description` | Text | 說明 |
| `active` | Boolean | 啟用/停用 |
| `scope` | Selection | `system`（admin 設定）/ `user`（使用者自定義） |
| `cloud_service_id` | Many2one | 關聯的 Cloud Service（scope=user 時） |
| `tool_ids` | One2many | 自動 discover 的 tools |
| `last_sync` | Datetime | 上次同步 tools 時間 |
| `state` | Selection | `draft` / `connected` / `error` |

#### FR-2: MCP Tool Model（`woow_paas_platform.mcp_tool`）

| Field | Type | Description |
|-------|------|-------------|
| `name` | Char | Tool 名稱（from MCP discovery） |
| `description` | Text | Tool 描述（from MCP discovery） |
| `input_schema` | Text | JSON Schema（from MCP discovery） |
| `server_id` | Many2one | 所屬 MCP Server |
| `active` | Boolean | 啟用/停用個別 tool |

#### FR-3: AI Assistant ↔ MCP Server 關聯

- `ai.assistant` 擴充：新增 `mcp_server_ids`（Many2many → `mcp_server`，scope=system）
- `ai.assistant` 擴充：新增 `mcp_tool_disabled_ids`（Many2many → `mcp_tool`，反向黑名單）
- 邏輯：assistant 啟用的 server 中所有 active tools，扣除 disabled_ids = 可用 tools

#### FR-4: Cloud Service ↔ MCP Server 關聯

- `cloud_service` 擴充：新增 `user_mcp_server_ids`（One2many → `mcp_server`，scope=user）
- 對話時合併：assistant 的 system MCP tools + 當前 cloud service 的 user MCP tools

#### FR-5: LangChain Tool Calling 整合

修改 `ai_client.py`：

```python
# 1. 透過 langchain-mcp-adapters 取得 tools
from langchain_mcp_adapters.client import MultiServerMCPClient

# 2. bind_tools 到 ChatOpenAI
llm_with_tools = self.llm.bind_tools(mcp_tools)

# 3. 使用 LangGraph 的 ReAct agent 或手動 loop 處理 tool calls
```

**串流中的 tool call 處理：**
1. LLM 回傳 `tool_calls` chunk → 發送 SSE event `{"type": "tool_call", "tool": "...", "args": {...}}`
2. 執行 MCP tool → 發送 SSE event `{"type": "tool_result", "tool": "...", "result": "..."}`
3. 將 tool result 送回 LLM 繼續生成 → 回到正常串流

#### FR-6: MCP Server Discovery / Sync

- `action_sync_tools()` 方法：連接 MCP server → 取得 `tools/list` → 建立/更新 `mcp_tool` records
- 可從 form view button 手動觸發
- 可選：定時排程自動同步（`ir.cron`）

#### FR-7: 前端 Tool Call 顯示

- AiChat component 擴充：解析 SSE 中的 `tool_call` / `tool_result` events
- 顯示折疊式 tool call block（工具名稱 + 輸入 + 結果）
- 串流中顯示 "正在調用工具..." 動畫

### Non-Functional Requirements

#### NFR-1: Performance
- MCP tool discovery（sync）不超過 10 秒
- 單次 tool call 執行 timeout 設定為 30 秒
- 多次 tool calls 的總 timeout 設定為 120 秒
- MCP 連線使用 connection pooling 或快取

#### NFR-2: Security
- MCP Server API Key 存儲加密（Odoo `config_parameter` 或 model field with `groups`）
- User-defined MCP servers 只能在其 Cloud Service scope 內使用
- Tool 執行結果需經過 sanitization 後才顯示在前端
- Admin 可隨時停用任何 MCP server/tool

#### NFR-3: Reliability
- MCP Server 連線失敗不影響正常對話（graceful fallback）
- Tool 執行失敗時 AI 需告知使用者而非靜默忽略
- 斷線重連機制（MCP SSE 連線）

#### NFR-4: Scalability
- 支援每個 assistant 最多 10 個 MCP servers
- 支援每個 server 最多 50 個 tools
- Tool calling loop 最多迭代 5 次（防止無限循環）

## Architecture

### 資料流

```
User Message
    ↓
AI Assistant (get enabled tools)
    ↓
├── System MCP Servers (admin-defined, per assistant)
└── User MCP Servers (user-defined, per cloud service)
    ↓
LangChain ChatOpenAI.bind_tools(all_tools)
    ↓
LLM decides: text response or tool_call
    ↓
[If tool_call]
    → MCP Client executes tool via SSE
    → Result sent back to LLM
    → LLM continues (may call more tools or generate text)
    ↓
SSE Stream to Frontend
    → text chunks
    → tool_call events
    → tool_result events
    → final message
```

### Model 關係

```
ai.assistant
    ├── mcp_server_ids (M2M) ──→ mcp_server (scope=system)
    └── mcp_tool_disabled_ids (M2M) ──→ mcp_tool

cloud_service
    └── user_mcp_server_ids (O2M) ──→ mcp_server (scope=user)

mcp_server
    └── tool_ids (O2M) ──→ mcp_tool
```

### 技術選型

| Component | Choice | Reason |
|-----------|--------|--------|
| MCP Client | `langchain-mcp-adapters` | 官方 LangChain MCP bridge，直接產生 LangChain tools |
| Tool Calling | `ChatOpenAI.bind_tools()` | LangChain 原生支援，與現有架構一致 |
| Agent Loop | 手動 tool-call loop | 比 LangGraph ReAct agent 更輕量，控制更精細 |
| Transport | SSE (HTTP) | 使用者需求，適合遠端 MCP server |

## Success Criteria

| Metric | Target |
|--------|--------|
| Admin 能在 5 分鐘內設定一個新 MCP Server | ✅ |
| AI 能在對話中自動調用 MCP tool 並回傳正確結果 | ✅ |
| Tool call 失敗不影響正常對話 | ✅ |
| 使用者能在 Cloud Service 頁面自定義 MCP server | ✅ |
| SSE 串流中 tool call 過程即時可見 | ✅ |
| 端到端 tool call 延遲 < 5 秒（不含 tool 本身執行時間）| ✅ |

## Constraints & Assumptions

### Constraints
- 僅支援 SSE transport 的 MCP Server（不支援 stdio）
- 依賴 LangChain `langchain-mcp-adapters` 套件的成熟度
- Tool calling 需要 LLM 支援 function calling（OpenAI compatible API）
- Odoo server 需能連通 MCP server endpoint（網路可達性）

### Assumptions
- 目標 LLM（GPT-4o, Claude 等）都支援 function calling
- MCP Server 遵循標準 MCP protocol（`tools/list`, `tools/call`）
- `langchain-mcp-adapters` 支援 SSE transport

## Out of Scope

- **stdio 類型 MCP Server** — Odoo server 環境不適合管理本地子進程
- **MCP Resources / Prompts** — 本期只做 Tools，Resources 和 Prompts 未來再考慮
- **Tool 執行權限的細粒度控制**（如限制特定使用者只能用特定 tool） — 本期以 assistant 層級控制
- **MCP Server 的建立/託管** — 本系統只負責連接和使用，不負責建立 MCP server
- **Billing / Usage tracking** — Tool 調用的計費追蹤
- **Tool result caching** — Tool 結果快取機制

## Dependencies

### External
- `langchain-mcp-adapters` Python package
- `langchain-openai` >= 0.3（已安裝，需確認 tool calling 支援版本）
- MCP Server endpoints（使用者自備或系統預設）

### Internal
- `ai_base_gt` module（已整合）
- `ai_client.py` — 需大幅修改以支援 tool calling
- `ai_assistant.py` controller — SSE generator 需擴充 tool call events
- `discuss_channel.py` — 非 SSE 路徑也需支援 tool calling
- AiChat 前端元件 — 需新增 tool call 顯示邏輯

## Implementation Notes

### Phase 建議

**Phase A: 基礎 MCP 模型 + Admin 管理**
- 建立 `mcp_server` + `mcp_tool` models
- Admin form views + settings 整合
- MCP Server discovery（sync tools）
- 預估：3-4 tasks

**Phase B: LangChain Tool Calling 整合**
- 修改 `ai_client.py` 支援 `bind_tools()`
- 實作 tool-call loop（invoke → tool_call → execute → re-invoke）
- 修改 SSE controller 支援 tool call events
- 預估：3-4 tasks

**Phase C: 前端 Tool Call 顯示**
- AiChat 解析 tool_call / tool_result SSE events
- Tool call 折疊式 UI 元件
- Loading / error 狀態
- 預估：2-3 tasks

**Phase D: 使用者自定義 MCP（Cloud Service Level）**
- Cloud Service detail page 新增 MCP tab
- 使用者 MCP server CRUD UI
- 對話中合併 system + user tools
- 預估：2-3 tasks

### LangChain MCP Adapter 使用範例

```python
from langchain_mcp_adapters.client import MultiServerMCPClient

async with MultiServerMCPClient(
    {
        "server_name": {
            "url": "http://mcp-server:8080/sse",
            "transport": "sse",
        }
    }
) as client:
    tools = client.get_tools()
    # tools is a list of LangChain BaseTool instances
    llm_with_tools = llm.bind_tools(tools)
    response = await llm_with_tools.ainvoke(messages)
```
