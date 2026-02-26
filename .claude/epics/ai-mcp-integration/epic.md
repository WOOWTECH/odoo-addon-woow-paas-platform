---
name: ai-mcp-integration
status: backlog
created: 2026-02-26T16:08:20Z
progress: 0%
prd: .claude/prds/ai-mcp-integration.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/96
---

# Epic: ai-mcp-integration

## Overview

為 AI Assistant 加入 MCP（Model Context Protocol）tool calling 支援。透過 `langchain-mcp-adapters` 將遠端 MCP Server 的 tools 轉成 LangChain tools，搭配 `ChatOpenAI.bind_tools()` 實現 function calling。管理分兩層：Admin 在 Odoo 後台為每個 `ai.assistant` 配置 MCP servers（系統層級），使用者可在 Cloud Service 上自定義 MCP servers（使用者層級）。

## Architecture Decisions

### AD-1: 使用 `langchain-mcp-adapters` + LangGraph 輕量 agent

**決定**：不手動實作 tool-call loop，改用 LangGraph 的 `StateGraph` + `ToolNode` + `tools_condition` 建立 agent。

**理由**：
- `langchain-mcp-adapters` 的 `MultiServerMCPClient` 直接產生 LangChain `BaseTool` 實例
- LangGraph 的 `ToolNode` 自動處理 tool execution + result injection
- 比手動 loop 更可靠（自動處理 parallel tool calls、retry、max iterations）
- `tools_condition` 自動判斷是否需要繼續執行或回傳 text

### AD-2: Async Bridge 策略

**決定**：在 Odoo sync context 中用 `asyncio.run()` 包裝 MCP 操作。

**理由**：
- `langchain-mcp-adapters` 的 API 全部是 async（`await client.get_tools()`）
- Odoo HTTP controllers 是 sync（WSGI）
- Tool discovery（sync tools）是一次性操作，用 `asyncio.run()` 即可
- 串流對話中的 tool calling 需要在 generator 內用 `asyncio.run()` 逐步執行

### AD-3: Transport 支援 SSE + Streamable HTTP

**決定**：同時支援 `sse` 和 `streamable_http` 兩種 transport。

**理由**：
- `sse` 是舊版 MCP transport（仍廣泛使用）
- `streamable_http` 是新版標準（2025+ MCP servers 推薦）
- `langchain-mcp-adapters` 兩者都支援，只需在 model field 中加 selection

### AD-4: Tool 管理用黑名單（disabled）而非白名單（enabled）

**決定**：`ai.assistant` 的 MCP tool 控制用 `mcp_tool_disabled_ids`（Many2many 黑名單）。

**理由**：
- 新 server 同步後，所有 tools 預設可用（減少 admin 操作）
- Admin 只需停用不想要的 tools
- 新增 tool 時不需要手動啟用

### AD-5: 串流中的 tool call 呈現

**決定**：SSE 新增 `tool_call` 和 `tool_result` event types，前端用折疊式 UI 顯示。

**理由**：
- 與現有 `{"chunk": "...", "done": false}` 格式共存
- Tool call 過程對使用者透明，可展開查看細節
- 不打斷正常的 text streaming 體驗

## Technical Approach

### Backend Models

**新增 models：**
- `mcp_server.py` — `woow_paas_platform.mcp_server` model
  - Fields: name, url, transport(sse/streamable_http), api_key, headers_json, description, active, scope(system/user), cloud_service_id, tool_ids, last_sync, state(draft/connected/error)
  - Methods: `action_sync_tools()`, `action_test_connection()`, `_get_mcp_client_config()`
- `mcp_tool.py` — `woow_paas_platform.mcp_tool` model
  - Fields: name, description, input_schema, server_id, active

**擴充 models：**
- `ai_assistant.py` — 擴充 `ai.assistant`
  - 新增: `mcp_server_ids` (M2M), `mcp_tool_disabled_ids` (M2M)
  - Method: `get_enabled_mcp_tools()` → 回傳可用的 LangChain tools
- `cloud_service.py` — 擴充
  - 新增: `user_mcp_server_ids` (O2M → mcp_server, scope=user)

### Backend Services

**修改 `ai_client.py`：**
- 新增 `chat_completion_with_tools()` 和 `chat_completion_stream_with_tools()` 方法
- 內部使用 LangGraph `StateGraph` 建立輕量 agent
- async bridge: `asyncio.run()` 包裝 MCP tool execution

**修改 `ai_assistant.py` controller：**
- SSE generator 擴充：在 tool call 時 yield `{"type": "tool_call", ...}` 和 `{"type": "tool_result", ...}` events
- 呼叫路徑：取得 assistant → 取得 enabled tools → 傳入 ai_client → stream with tools

**修改 `discuss_channel.py`：**
- 非 SSE 路徑（`message_post` AI reply）也需支援 tool calling

### Frontend Components

**修改 AiChat component：**
- 解析新 SSE event types：`tool_call`, `tool_result`
- 新增 `ToolCallBlock` 子元件：折疊式顯示 tool name + args + result
- 串流中 tool call 即時顯示（loading spinner → result）

**新增 Cloud Service MCP Tab：**
- ServiceDetailPage 新增 `McpServersTab`
- MCP Server CRUD UI（新增/編輯/刪除）
- Tool list 顯示 + 開關

### API Endpoints

- `POST /api/mcp-servers` — CRUD MCP servers (admin)
- `POST /api/mcp-servers/<id>/sync` — Trigger tool discovery
- `GET /api/cloud-services/<id>/mcp-servers` — List user MCP servers
- `POST /api/cloud-services/<id>/mcp-servers` — Create user MCP server

## Implementation Strategy

### 開發順序

嚴格按 task 順序，每個 task 都 self-contained 且可獨立驗證：

1. **Models + Security + Admin Views** → 可在 Odoo 後台驗證 CRUD
2. **AI Assistant MCP 關聯** → 可在 assistant form 驗證勾選
3. **LangChain Tool Calling 核心** → 可用 Python shell 驗證 tool execution
4. **SSE + Controller 整合** → 可用 curl 驗證 SSE events
5. **Frontend Tool Call UI** → 可在瀏覽器驗證完整流程
6. **User MCP (Cloud Service)** → 可在前端驗證使用者自定義
7. **E2E Testing + Polish** → 完整流程驗證

### 風險緩解

| Risk | Mitigation |
|------|------------|
| `langchain-mcp-adapters` async 與 Odoo sync 衝突 | Task #101 先驗證 async bridge 方案 |
| LLM 不支援 function calling | 在 system prompt 中加入 tool usage 引導 |
| MCP Server 不可達 | Graceful fallback: tool calling 失敗時降級為純文字對話 |
| Tool calling loop 無限迴圈 | LangGraph `recursion_limit` 設為 5 |

### 測試策略

- Unit tests: MCP Server model CRUD, tool sync mock, tool filtering logic
- Integration: ai_client + mock MCP server → 驗證 tool-call loop
- E2E: 前端完整 tool call 流程（需要 test MCP server）

## Task Breakdown Preview

- [ ] #97: MCP Server + Tool models, security, admin views, tool discovery
- [ ] #99: AI Assistant ↔ MCP Server relation + assistant form view
- [ ] #101: ai_client.py tool calling (LangChain bind_tools + async bridge + LangGraph agent)
- [ ] #103: SSE controller tool call events + discuss_channel integration
- [ ] #98: Frontend AiChat tool call visualization
- [ ] #100: Cloud Service user MCP servers (model extension + API + frontend tab)
- [ ] #102: E2E testing + error handling + graceful fallback

## Dependencies

### External
- `langchain-mcp-adapters` — pip install, 需加入 `requirements.txt`
- `langgraph` — pip install（for StateGraph + ToolNode）
- MCP Server endpoint（測試用，可用 `@modelcontextprotocol/server-*` 系列）

### Internal
- `ai_base_gt` module — 已整合，需擴充 `ai.assistant`
- `ai_client.py` — 核心修改（新增 tool calling 方法）
- `ai_assistant.py` controller — SSE generator 擴充
- `discuss_channel.py` — tool calling 整合
- AiChat component — 前端 tool call UI

### Prerequisites
- 確認 `langchain-mcp-adapters` 在 Odoo Docker 環境中可安裝
- 確認目標 LLM 支援 function calling

## Success Criteria (Technical)

| Criteria | Measurement |
|----------|-------------|
| MCP Server CRUD + tool sync 正常 | Admin 可新增 server 並看到 discovered tools |
| Tool calling 端到端成功 | AI 在對話中調用 tool 並回傳結果 |
| SSE tool events 正確 | 前端顯示 tool call 過程（name + args + result）|
| Graceful fallback | MCP 連線失敗時降級為純文字對話 |
| User MCP 隔離 | Cloud Service A 的 MCP 不出現在 B 的對話中 |
| Loop 保護 | Tool calling 最多 5 次迭代 |

## Estimated Effort

- **Total Tasks**: 7
- **Critical Path**: #97 → #99 → #101 → #103 → #98（前後依賴）
- **可並行**: #100 可與 #98 並行；#102 最後
- **最大風險**: #101（async bridge + LangGraph 整合）

## Tasks Created

- [ ] #97 - MCP Server + Tool models, security, admin views, tool discovery (parallel: false)
- [ ] #99 - AI Assistant MCP Server relation + form view (parallel: false)
- [ ] #101 - ai_client.py tool calling with LangGraph agent + async bridge (parallel: false)
- [ ] #103 - SSE controller tool call events + discuss_channel integration (parallel: false)
- [ ] #98 - Frontend AiChat tool call visualization (parallel: false)
- [ ] #100 - Cloud Service user MCP servers (model + API + frontend) (parallel: true)
- [ ] #102 - E2E testing + error handling + graceful fallback (parallel: false)

Total tasks: 7
Parallel tasks: 1 (#100)
Sequential tasks: 6
Estimated total effort: 51-66 hours
