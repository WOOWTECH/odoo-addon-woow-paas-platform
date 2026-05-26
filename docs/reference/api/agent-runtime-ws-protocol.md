---
title: Agent-Runtime Pod WS Protocol (Odoo ↔ Pod /v1/chat)
category: reference
status: implemented (Odoo side) / partial (image side)
audience: agent-runtime image team (B-series)
source-of-truth: src/controllers/ai_assistant.py `_stream_agent_runtime` (Odoo)
related:
  - docs/plans/2026-Q2/agent-runtime-pod-design.md §B
  - docs/guides/paas-skill-system/02-conversation-streaming.md
  - extra/paas-operator/src/services/jwt_signer.py (ws_token)
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
    "invoker_user_id": 2
  }
}
```

- `history`: recent turns, oldest→newest, `role ∈ {user, assistant}`, plaintext.
- `template_id`: the resolved `agent_template`; the pod fetches its
  system_prompt / model config / bound skills+MCP via the skill-bundle endpoint
  (§5) using `SKILL_BUNDLE_TOKEN` (do **not** trust query params — token is
  authoritative).

## 4. Outbound — Pod → Odoo (reply frames) — **THIS IS WHAT 1.0.0 IS MISSING**

The pod streams zero or more frames, terminated by `done` (or `error`). Odoo's
drain loop (`ai_assistant.py:687-720`) recognises these `type`s:

| `type` | payload | Odoo behaviour |
|--------|---------|----------------|
| `text_chunk` | `{"content": "<delta>"}` | accumulate + forward as SSE chunk (streaming) |
| `tool_call` / `tool_result` / `tool_error` / `tool_unavailable` / `status` | passthrough | forwarded to SSE as-is (`done:false`) |
| `done` | (optional `full_response`) | terminal — Odoo persists the accumulated text as the assistant's `mail.message`, sends SSE `done`, closes |
| `error` | `{"error": "<msg>", "fatal": true\|false}` | Odoo surfaces the error to SSE; `fatal:true` ends the stream |

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

When processing a turn the pod fetches its skill bundle (skills/template/MCP
snapshot) from Odoo, authenticating with `SKILL_BUNDLE_TOKEN`:

```
GET/POST {ODOO_BASE_URL}/<skill-bundle endpoint>
Authorization: Bearer <SKILL_BUNDLE_TOKEN>
ODOO_BASE_URL = http://stg-odoo-dev-sandbox-odoo.dev-sandbox-stg.svc.cluster.local:8069
```

NetworkPolicy for this egress (pod→Odoo) + the WS ingress (Odoo→pod:8000) is in
place (Odoo side: `paas-managed=true` label match; pod side: `odooNamespace`).
**Caveat:** a template with no published skills yields an *empty* bundle — that
is expected; fetching an empty bundle still counts as a working path.

## 6. e2e status snapshot (2026-05-25 pilot)

| stage | status |
|-------|--------|
| pod spawn / chart install / image pull / probe / NetworkPolicy | ✅ |
| WS upgrade `/v1/chat` accepted | ✅ |
| `ws_token` verified (`invoker_role` extracted) | ✅ |
| `user_message` received by pod | ✅ |
| **pod emits `text_chunk` … `done`** | ❌ **image team to implement** |
| skill-bundle fetch (pod→Odoo) | ❌ not reached (pod closes before processing) |
| assistant reply rendered in UI | ❌ blocked on the above |
