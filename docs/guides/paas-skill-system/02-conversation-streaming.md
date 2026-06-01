---
title: Agent 對話與串流
category: guides
status: active
created: 2026-05-25
updated: 2026-05-25
tags: [paas, skill-system, conversation, streaming, langchain]
related:
  - README.md
  - 06-pod-internals.md
  - 08-webhook-ws-token.md
---

# Agent 對話與串流

> 使用者怎麼跟 LLM agent 對話、langchain 在哪被調用。關鍵洞察：**agent-runtime 不是新開一個對話入口，而是把既有 `/api/ai/stream` 這條 SSE 的「LLM 執行端」從 Odoo 內搬到 per-project 的 Pod**，前端 `EventSource` 合約完全不變。對應程式碼：`src/controllers/ai_assistant.py`、`src/models/ai_client.py`、`src/services/agent_runtime_client.py`。

## 🚪 單一前門 + 雙模式 gate

```mermaid
graph TB
    BROWSER["瀏覽器 EventSource"] -->|"GET /api/ai/stream/{channel_id}"| ENTRY["ai_assistant.api_ai_stream"]
    ENTRY --> CSRF["CSRF + channel 存取檢查"]
    CSRF --> GATE{"channel→task→project<br/>use_agent_runtime?"}
    GATE -->|"False（預設・legacy）"| LEGACY["LLM 跑在 Odoo<br/>AIClient（LangChain）"]
    GATE -->|"True（pilot 開啟）"| POD["_stream_agent_runtime<br/>橋接 Pod"]
    LEGACY --> SSE["text/event-stream 回瀏覽器"]
    POD --> SSE
    style GATE fill:#fff0b3,stroke:#e6a700
```

- 前門：`GET /api/ai/stream/{channel_id}`（SSE，`type=http`，`auth=user`）— `ai_assistant.py:360`
- gate：由 `channel → project.task → project.use_agent_runtime` 決定走哪邊，**預設 False**，Phase E 才對 pilot project 開 — `ai_assistant.py:384-396`

## 🅰️ Legacy 路徑：LLM 跑在 Odoo

```mermaid
sequenceDiagram
    participant B as 瀏覽器
    participant O as Odoo（ai_assistant）
    participant AC as AIClient（LangChain）
    participant LLM as LLM / MCP
    B->>O: GET /api/ai/stream/{channel_id}
    O->>O: 取最後 user message + history(20) + system prompt(+雲端服務 context)
    O->>AC: build_messages() + 收集 MCP tools（system + user scope）
    alt 有 MCP tools
        AC->>LLM: _async_agent_stream（langgraph + bind_tools）
    else 純文字
        AC->>LLM: chat_completion_stream
    end
    LLM-->>O: 串流 chunk / tool 事件
    O-->>B: SSE data:{chunk, done:false}
    O->>O: 把完整 AI 回覆存回 discuss.channel
    O-->>B: SSE data:{done:true, message_id}
```

落點：`AIClient.from_assistant()` → `build_messages()` → generator 內 `_async_agent_stream()` 或 `chat_completion_stream()`（`ai_assistant.py:403-565`）。

## 🅱️ Agent-runtime 路徑：LLM 跑在 Pod，Odoo 只橋接

```mermaid
sequenceDiagram
    participant B as 瀏覽器
    participant O as Odoo（_stream_agent_runtime）
    participant Op as paas-operator
    participant P as Pod（agent-runtime）
    B->>O: GET /api/ai/stream/{channel_id}（use_agent_runtime=True）
    O->>O: project_access gate + resolve agent_template<br/>build mcp_snapshot / skill_snapshot
    O->>Op: spawn_or_attach(project, channel, invoker, template, snapshots)
    Op-->>O: {pod_ws_url, auth_token, pod_state, spawn_age_ms}
    O->>P: WS connect /v1/chat（Authorization: Bearer auth_token）
    O->>P: envelope type=user_message<br/>{body, history, template_id, invoker_role, invoker_user_id}
    loop Pod 回串流
        P-->>O: envelope（text_chunk / tool_* / status）
        O-->>B: 重新包成 SSE data:{..., done:false}
    end
    P-->>O: envelope type=done
    O->>O: _persist_ai_response 存回 channel
    O-->>B: SSE data:{done:true, message_id}
```

落點：`_stream_agent_runtime()`（`ai_assistant.py:567`）→ `spawn_or_attach()`（`agent_runtime_client.py:60`，回 `{pod_ws_url, auth_token, pod_state, spawn_age_ms}`）→ WS 橋接（`ai_assistant.py:647-743`）。WS 的 `Bearer` 用的就是這裡的 `auth_token`（即 [Webhook 回呼與 ws_token](08-webhook-ws-token.md) 講的 ws_token）。Pod 端怎麼收這條 WS、怎麼跑，見 [agent-runtime Pod 內部](06-pod-internals.md)。

## 📨 WS Envelope 協定（Pod ↔ Odoo）

去程 Odoo 送一筆 `user_message` envelope（version 1.0，含 `request_id` / `sequence_number` / `timestamp` + payload）。回程 Pod 串流多筆 envelope，Odoo 逐筆轉成 SSE：

| Pod envelope `type` | Odoo 動作 | 送出的 SSE |
|---|---|---|
| `text_chunk` | 累加 `payload.content` | `{chunk, done:false}` |
| `tool_call` / `tool_result` / `tool_error` / `tool_unavailable` / `status` | 原樣轉發 | `{...envelope, done:false}` |
| `done` | `_persist_ai_response()` 存訊息 | `{chunk:'', done:true, full_response, message_id?}` |
| `error` | 結束（fatal 立即斷） | `{error, done:true}` |

> 🔑 前端只看得到 SSE 那一層，**兩條路徑的 SSE 事件格式一致**，所以切換 `use_agent_runtime` 對瀏覽器無感。完整 15 種 envelope type 見 [agent-runtime Pod 內部](06-pod-internals.md)。

## 📦 Odoo 傳給 Pod 的快照（snapshot）

| 傳給 Pod | 來源（`ai_assistant.py`） | 內容 |
|---|---|---|
| `agent_template` | `_resolve_agent_template`（優先 `state=published`）`:755` | 該 project 綁定的 agent 範本；無自綁時取平台預設（config_parameter `default_agent_template_id` / 內建 seed `agent_template_default`）|
| `mcp_snapshot` | `_build_mcp_snapshot` `:778` | `{servers:{name:{url, transport}}}` |
| `skill_snapshot` | `_build_skill_snapshot` `:785` | 範本已發布 skills 的 `name`/`version`/`sha` |
| `invoker_role` | `_get_project_access_role`（預設 `guest`）`:770` | 觸發者在該 project 的角色 |

> 這些快照進 Pod 的通道是 **ConfigMap mount**（`/etc/agent-runtime/*.json`）+ 熱重載，不是走 WS payload，細節見 [agent-runtime Pod 內部](06-pod-internals.md)。skill_snapshot 怎麼變成 Pod 真的下載的 bundle，見 [Skill Bundle 與 JWT 認證](04-skill-bundle-jwt.md)；skill 怎麼來，見 [Skill 設計與發布](03-skill-design.md)。

## 🧬 langchain 到底在哪被調用

| 路徑 | langchain 在哪 | 技術棧 / 狀態 |
|---|---|---|
| **Legacy**（`use_agent_runtime=False`） | **Odoo** `src/models/ai_client.py` | `langchain-openai` 的 `ChatOpenAI`（`:88`）+ `langchain-mcp-adapters` 的 `MultiServerMCPClient`（`:271`）+ `langgraph` agent graph（`bind_tools` / `StateGraph` / `ToolNode`，`:373`）— **可運作** |
| **Agent-runtime**（`True`） | **Pod** `runtime/langgraph_runtime.py`（`woow-paas-agent-runtime`） | 同樣是 langchain/langgraph stack（已是依賴）；⚠️ **agent loop 目前是 Phase B stub**（`stream()` 只回 `done`），真正 graph 待後續 phase。詳見 [agent-runtime Pod 內部](06-pod-internals.md) |

## 🧹 連線清理（F-PC-6 / P-2）

`done` / `error` 跳出 drain loop 時，relay generator 仍掛在 `async with websockets.connect()` 裡，必須 `agen.aclose()` 收掉才會觸發 WS 關閉；否則**每輪對話漏一條 pod 連線**（`ai_assistant.py:728-743`）。

## 📁 相關文件

- [Skill 端到端調用流程](01-end-to-end-flow.md) — Pod 怎麼被 spawn 起來（本篇是對話那一段的放大）
- [agent-runtime Pod 內部](06-pod-internals.md) — 這條 WS 在 Pod 端怎麼被處理
- [PaaS Skill 系統總覽](README.md)
- [Webhook 回呼與 ws_token](08-webhook-ws-token.md) — `auth_token`/ws_token 與 WebSocket 認證
- [Agent Runtime Pod 部署機制](07-deployment.md) — `spawn_or_attach` 的 operator 側
- [Skill 設計與發布](03-skill-design.md) · [Skill 系統安全模型](09-security-model.md)
