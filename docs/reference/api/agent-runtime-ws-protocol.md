---
title: Agent-Runtime Pod WS Protocol (Odoo ↔ Pod /v1/chat)
category: reference
status: implemented (Odoo side) / partial (image side)
audience: agent-runtime image team (B-series)
source-of-truth: src/controllers/ai_assistant.py `_stream_agent_runtime` (Odoo)
related:
  - docs/plans/2026-Q2/agent-runtime-pod-design.md §B / §8.2
  - docs/guides/paas-skill-system/02-conversation-streaming.md
  - extra/paas-operator/src/services/jwt_signer.py (ws_token)
  - src/controllers/agent_runtime_skill.py (§5 skill-bundle endpoint)
  - src/static/src/paas/components/ai-chat/AiChat.js (§4b browser frame consumer)
  - src/models/ai_client.py + src/models/ai_config.py (§6 LLM model config source)
  - extra/paas-operator/src/api/agent_runtimes.py + models/schemas.py (§6 LLM Secret provision)
---

# Agent-Runtime Pod WS Protocol — `/v1/chat`

This is the **implemented** contract between the Odoo controller (SSE↔WS bridge,
`ai_assistant._stream_agent_runtime`) and the per-project agent-runtime pod's
WebSocket endpoint. It is derived from the live Odoo code, which is the
source of truth for what Odoo **sends** and what frame shapes it **consumes**.

> **Why this doc exists (2026-05-25 pilot finding):** the agent-runtime e2e is
> green through transport+auth (pod spawns, WS upgrade accepted, `ws_token`
> verified `invoker_role=owner`, `user_message` received). But image
> `agent-runtime:1.0.0` **acks the message then closes the WS without emitting
> any reply frame** (no LLM call, no skill-bundle fetch, no `text_chunk`/`done`).
> The pod side must implement the **Outbound frames** below for the chat
> round-trip to complete. Everything else in the pipeline is verified working.

## 1. Connection

```
URL:     ws://{release}-svc.{namespace}.svc.cluster.local:8000/v1/chat
         e.g. ws://agent-pa-{hash}-svc.paas-ws-stg-pa-{hash}.svc.cluster.local:8000/v1/chat
Header:  Authorization: Bearer <ws_token>
Keepalive: client uses ping_interval=30s, ping_timeout=60s
```

`{release}` = `agent-pa-{project_hash}`, `{namespace}` = `{NAMESPACE_PREFIX}pa-{project_hash}`
(operator-derived; `paas-ws-pa-…` default, `paas-ws-stg-pa-…` env-scoped).

## 2. Auth — `ws_token` (HS256, pod MUST verify)

The pod verifies the bearer token with the shared HS256 secret mounted from the
per-namespace Secret `agent-runtime-auth-secret` (env
`AGENT_RUNTIME_AUTH_SECRET_CURRENT` / `_PREVIOUS`, dual-secret rotation window).
Same raw key the operator signs with (D-CAVEAT-1 three-way: operator signs /
Odoo + pod verify). Stdlib HS256 (no PyJWT).

`ws_token` claims:

| claim | value |
|-------|-------|
| `iss` | `paas-operator` |
| `aud` | `agent-runtime` |
| `token_use` | `ws` |
| `project_hash` | `<16-char base32>` |
| `channel_id` | `<int>` |
| `invoker_user_id` | `<int>` |
| `invoker_role` | `owner` \| `admin` \| `member` \| `guest` |
| `iat` / `exp` | unix; 1h TTL |
| `jti` | uuid hex (replay defence) |

A separate `SKILL_BUNDLE_TOKEN` (env, `token_use=skill_bundle`, carries the
authoritative `project_id` + `workspace_ids`) is what the pod presents back to
Odoo's skill-bundle endpoint — see §5.

## 3. Inbound — Odoo → Pod (`user_message`)

On each chat turn Odoo opens the WS, sends **exactly one** envelope, then
streams the pod's reply frames back to the browser as SSE:

```json
{
  "version": "1.0",
  "type": "user_message",
  "request_id": "<uuid4 hex>",
  "sequence_number": 0,
  "timestamp": "<ISO-8601>Z",
  "payload": {
    "channel_id": 16,
    "body": "<user message text/HTML>",
    "history": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ],
    "template_id": 2,
    "invoker_role": "owner",
    "invoker_user_id": 2,
    "system_context": "<Odoo 端組好的完整 system prompt>"
  }
}
```

- `history`: recent turns, oldest→newest, `role ∈ {user, assistant}`, plaintext.
- `template_id`: the resolved `agent_template`; the pod fetches its
  model config / bound skills+MCP via the skill-bundle endpoint
  (§5) using `SKILL_BUNDLE_TOKEN` (do **not** trust query params — token is
  authoritative).

| field | type | required | meaning |
|-------|------|----------|---------|
| `system_context` | string | optional（v1.1 additive） | Odoo 端組好的完整 system prompt（template system_prompt + project/task/cloud-service 背景）。缺席 = 維持 v1.0 行為（pod 不注入 SystemMessage）。 |

system prompt 由 Odoo 端 per-turn 組裝後經 `user_message.payload.system_context`
傳入（v1.1 additive 欄位，見上）；pod 不另行拉取 system_prompt（歷史上
skill-bundle endpoint 從未提供 system_prompt，舊描述有誤）。

## 4. Outbound — Pod → Odoo (reply frames) — **THIS IS WHAT 1.0.0 IS MISSING**

The pod streams zero or more frames, terminated by `done` (or a `fatal` `error`).
Echo each frame's `request_id` back from the inbound `user_message` (§3).
There are **two classes** of frame — the field placement differs and **matters**:

### 4a. Odoo-transformed frames (Odoo reads `payload.*`, re-emits a normalised SSE event)

Drain loop: `ai_assistant.py:687-720`.

| `type` | frame the pod sends | Odoo behaviour (code) |
|--------|--------------------|----------------------|
| `text_chunk` | `{"type":"text_chunk","request_id":"…","payload":{"content":"<delta>"}}` | reads **`payload.content`**, accumulates into `full_response`, emits SSE `{"chunk":"<delta>","done":false}` (`:688-693`) |
| `done` | `{"type":"done","request_id":"…","payload":{"full_response":"…"}}` (`full_response` optional) | terminal: persists the **Odoo-accumulated** text as the assistant's `mail.message` (`:701-712` → `_persist_ai_response :795-811`), emits SSE `{"chunk":"","done":true,"full_response":"<accumulated>","message_id":<int>}`. NB Odoo persists **its own** accumulated chunks, not your `payload.full_response` (that field is currently ignored for persistence). |
| `error` | `{"type":"error","request_id":"…","payload":{"error":"<msg>","fatal":true\|false}}` | reads **`payload.error`**, emits SSE `{"error":"<msg>","done":true}` (`:713-720`); the relay only **stops the stream** when **`payload.fatal`** is truthy (`:674-676`) — a non-fatal `error` surfaces but the stream continues. |

### 4b. Passthrough frames (Odoo forwards the WHOLE frame verbatim; the **browser** reads them)

Odoo forwards these unchanged with `done:false` merged in — `{**envelope, "done":false}`
(`ai_assistant.py:694-700`). The OWL browser client then reads fields at the
**TOP LEVEL** of the frame (`AiChat.js`), so **these fields go at the envelope top
level (siblings of `type`/`request_id`), NOT inside `payload`** — the single most
common mistake:

| `type` | frame the pod sends | browser consumer |
|--------|--------------------|------------------|
| `tool_call` | `{"type":"tool_call","request_id":"…","tool":"<name>","args":{…}}` | `AiChat.js:216-231` reads `data.tool`, `data.args` |
| `tool_result` | `{"type":"tool_result","request_id":"…","tool":"<name>","result":"<str>"}` | `AiChat.js:233-244` matches by `data.tool`, sets `data.result` |
| `tool_error` | `{"type":"tool_error","request_id":"…","tool":"<name>","error":"<msg>"}` | `AiChat.js:246-258` matches by `data.tool`, sets `data.error` |
| `status` / `tool_unavailable` | `{"type":"status",…}` | forwarded with `done:false`, but the browser has **no handler** → silently ignored. Safe to emit; no field contract; not rendered. |

> **Why top-level for tool frames:** §4a frames are *re-emitted* by Odoo (it pulls
> `payload.content` / `payload.error` out), so they live in `payload`. §4b frames
> are *spread* (`{**envelope}`) straight to the browser, which reads `data.tool`
> etc. — put them in `payload` and the UI renders nothing.

**Minimum viable reply** (to pass the e2e round-trip): emit ≥1 `text_chunk`
then a `done`. Example:

```json
{"version":"1.0","type":"text_chunk","request_id":"<same>","payload":{"content":"Hello"}}
{"version":"1.0","type":"done","request_id":"<same>","payload":{"full_response":"Hello"}}
```

Closing the WS **without** sending `done`/`error` (current 1.0.0 behaviour) ends
Odoo's `async for` cleanly but yields **no assistant reply** → the user sees a
connected-but-silent chat.

## 5. skill-bundle fetch — Pod → Odoo (during processing)

Source of truth: `src/controllers/agent_runtime_skill.py` (route `:86-90`).

```
GET {ODOO_BASE_URL}/api/agent-runtimes/skill-bundle?project_hash={hash}&skill_name={name}&version={ver}
Authorization: Bearer <SKILL_BUNDLE_TOKEN>
ODOO_BASE_URL = http://stg-odoo-dev-sandbox-odoo.dev-sandbox-stg.svc.cluster.local:8069
```

- **Response = the RAW bundle bytes** (`:125-133`) — **NOT base64, NOT a JSON
  wrapper**. `Content-Type` is the attachment mimetype (default `application/gzip`).
  The pod does `resp.read()` then `sha256`.
- **Auth** `SKILL_BUNDLE_TOKEN` (HS256, verified against `AGENT_RUNTIME_AUTH_SECRET_CURRENT`
  then `_PREVIOUS`, `:62-80`). The token's **`token_use` MUST be `skill_bundle`** or
  the endpoint returns **403** (`:105-106`) — a `ws` token can't be replayed here.
  The token's **`project_id` claim is authoritative** (`:108-111`); the query
  `project_hash` is **not** trusted for project resolution (HC-2).
- **Status codes**: missing/non-Bearer → 401 (`:93-94`); wrong `token_use` → 403
  (`:138`); skill not found / not `published` / no attachment → 404 (`:118-123`).

### What to fetch + how to verify — `skill_snapshot` (delivered at spawn)

The endpoint returns **only bytes**; the list of skills to fetch, each
**expected sha256**, and each skill's **`description`** ride the spawn request
as `skill_snapshot` (`ai_assistant.py:_build_skill_snapshot`):

```json
[ {"name": "<skill_name>", "version": "<ver>", "sha256": "<expected hex>",
   "description": "<host-owned skill description, may be empty>"}, … ]
```

- For each entry: GET the endpoint with `skill_name`+`version`, then assert
  `sha256(raw_bytes) == entry.sha256` (`bundle_sha256`).
- **`description`** is host-owned metadata for **tool registration** — register
  the StructuredTool as `(name, description, args_schema)` **without fetching
  the bundle**.
- **`parameters` (the tool `args_schema`) is deliberately NOT in the snapshot.**
  It is runtime-owned (SKILL.md frontmatter `parameters`, JSON Schema; "host 不檢",
  design §skill). At registration the pod uses the **single-string `input`
  fallback** (design: schema 來自 `parameters` 或預設 single-string `input`) and
  fetches the bundle **lazily on first invoke** for execution. Carrying the rich
  `parameters` in the snapshot (so the pod can register a structured schema
  without any fetch) is a **deferred enhancement** — see
  `docs/tracking/followups/follow-ups.md` §skill args_schema.
- **An empty `skill_snapshot` (template has no published skills) is valid** —
  fetch nothing; that still counts as a working path.

### MCP servers — `mcp_snapshot` (delivered at spawn)

`ai_assistant.py:778-783`:

```json
{"servers": {"<mcp_name>": {"url": "<url>", "transport": "<transport>"}}}
```

NetworkPolicy for the egress (pod→Odoo) + the WS ingress (Odoo→pod:8000) is in
place (Odoo side: `paas-managed=true` label match; pod side: `odooNamespace`).

## 6. LLM model config — pod env (`LLM_API_KEY` / `LLM_MODEL_CONFIG`)

The tenant's LLM credential + model config reach the pod as **env vars**, not
over the WS. Source chain (design §8.2):

```
agent_template.model_config_id → ai.config            (agent_template.py:23-27)
        + agent_template.model_overrides (JSON merge)  (agent_template.py:28-32)
   ↓ Odoo client puts api_key + llm_model_config on the spawn request
operator SpawnOrAttachRequest.api_key / .llm_model_config   (operator schemas.py:378-405)
   ↓ operator cold-start provisions a per-release Secret  agent-pa-{hash}-llm
Secret stringData = {LLM_API_KEY, LLM_MODEL_CONFIG(json)}   (operator agent_runtimes.py:160-185)
   ↓ chart (agent-runtime ≥0.1.2) secretKeyRef, optional:true
pod env  LLM_API_KEY  (+ LLM_MODEL_CONFIG)  →  init LangGraph ChatOpenAI
```

### `LLM_MODEL_CONFIG` schema (JSON string in env)

Built by the Odoo client mirroring the existing `ai_client.from_assistant`
read of `ai.config` (`src/models/ai_client.py:114-126`; `api_base_url` is this
repo's extension of `ai.config`, `src/models/ai_config.py:11`):

```json
{
  "model":       "gpt-4o-mini",                 // ai.config.model (default gpt-4o-mini)
  "api_base":    "https://api.openai.com/v1",   // ai.config.api_base_url (default OpenAI)
  "temperature": 0.7,                            // ai.config.temperature (default 0.7)
  "max_tokens":  4096                            // ai.config.max_tokens (default 4096)
}
```

- `agent_template.model_overrides` (a JSON dict) is **merged on top** of the
  above (per-template override of `model` / `temperature` / `max_tokens` / …).
- **`api_base` runs through the pod's `url_validator`** before constructing
  `ChatOpenAI(base_url=…)` (design §3.x). Treat any field as possibly-absent and
  fall back to the defaults shown.

### Fallback-echo (拍板)

If `LLM_API_KEY` is **absent**, the operator provisioned **no** LLM Secret
(`agent_runtimes.py:170-171` — `if not request.api_key: return`). This happens
when the bound `ai.config` has no `api_key`, or no template config is bound.
**The pod MUST fall back to echo** in that case (do not crash) — `LLM_MODEL_CONFIG`
may likewise be absent. Only initialise the real LLM when `LLM_API_KEY` is present.

## 7. e2e status snapshot (2026-05-25 pilot)

| stage | status |
|-------|--------|
| pod spawn / chart install / image pull / probe / NetworkPolicy | ✅ |
| WS upgrade `/v1/chat` accepted | ✅ |
| `ws_token` verified (`invoker_role` extracted) | ✅ |
| `user_message` received by pod | ✅ |
| **pod emits `text_chunk` … `done`** | ❌ **image team to implement** |
| skill-bundle fetch (pod→Odoo) | ❌ not reached (pod closes before processing) |
| assistant reply rendered in UI | ❌ blocked on the above |
