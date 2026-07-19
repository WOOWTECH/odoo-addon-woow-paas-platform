---
title: "Design: agent-runtime cold-start retry（#123）+ skill/mcp on-publish 即時推送（#111）"
category: archived
status: archived
created: 2026-06-02
archived: 2026-07-19
archived-from: docs/superpowers/specs/2026-06-02-agent-runtime-coldstart-onpublish-design.md
archived-reason: "設計已隨 PR #130 完整落地，D1–D4 決策與所有語意（retry 只包 connect、best-effort 推送、snapshot 單一來源）已等值轉入 docs/guides/development/agent-runtime-chat-architecture.md，本檔的「現況事實」表為修前快照已過時"
---

# Design: agent-runtime cold-start retry（#123）+ skill/mcp on-publish 即時推送（#111）

> 狀態：design approved（brainstorming 2026-06-02），待 writing-plans。
> 範圍：gitea **#123**（cold-start 首則訊息 timing race）+ **#111**（skill/mcp
> 發布即時推送 running pod）。兩者皆**純 Odoo 端**，operator 與 chart 不動、
> 不需重發 image。
> 共用樞紐：snapshot 重算邏輯從 controller 下沉到 `agent_session` model，
> 讓 chat 路徑（#123 沿用）與 publish 路徑（#111 新增）呼叫同一份。

## 背景

`agent-runtime` 是 per-project on-demand spawn 的 pod；Odoo 以「兩跳橋接」
（browser ⇄ Odoo SSE ⇄ pod WS）relay 對話（見
`docs/guides/development/agent-runtime-chat-architecture.md`）。兩個已登記的缺口：

- **#123（F-ARL-1 / F-PILOT-10 G3，OPEN）**：teardown 或閒置回收後第一則 chat
  觸發 cold-start（helm install → image pull → pod startup → WS ready），全程
  數十秒。實測前 1~2 則訊息失敗、需手動「重試」第 3 次才成功：
  1. `Read timed out (read timeout=30)` — Odoo→operator 的 `spawn-or-attach`
     HTTP 在 30s 內沒回。
  2. `[Errno 111] Connect call failed ('<pod-ip>', 8000)` — pod 已建但 WS
     服務尚未 ready（connection refused）。
  3. ✅ 第 3 次成功。
  純 UX / robustness（non-blocking），warm 後 attach 即時。

- **#111（feature gap，OPEN）**：snapshot 只在「下一次 chat 的 spawn_or_attach」
  才刷新既有 pod。對話進行中發布新 skill / 改 MCP 綁定，running pod **不會即時
  看到**。operator 端 `POST /{hash}/invalidate-skills`、`/invalidate-mcp` 端點
  都已存在（#104/#106），但 **Odoo 端零 caller**。

### 為什麼一起做

兩者正交（#123 是 cold pod 還沒 ready 的 connect race；#111 是 warm pod 推
snapshot），但**動到同一批程式碼**：`agent_runtime_client.py` 與 controller 的
`_build_skill_snapshot` / `_build_mcp_snapshot`。一次 refactor 共用，避免雙寫漂移
（呼應 `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` 的單一來源紀律）。

## 現況事實（已核對原始碼）

| 元件 | 現況 | 位置 |
| --- | --- | --- |
| operator `/invalidate-skills`、`/invalidate-mcp` | 都已存在 | `extra/paas-operator/src/api/agent_runtimes.py:470,478` |
| schema `skill_snapshot: List` / `mcp_snapshot: Dict` | 已定義 | `extra/paas-operator/src/models/schemas.py:421,428` |
| client `invalidate_mcp(hash, dict)` | 已存在，**全 repo 零 caller** | `src/services/agent_runtime_client.py:140` |
| client `invalidate_skills` | **不存在** | — |
| client `_TIMEOUT = 30` | 全域固定，spawn 與一般呼叫共用 | `src/services/agent_runtime_client.py:16` |
| client `get_status(hash)` | 已存在（可作 readiness poll 備案） | `src/services/agent_runtime_client.py:155` |
| `spawn_or_attach` | idempotent（operator `_release_exists` 在就 attach） | `src/services/agent_runtime_client.py:110` |
| `_stream_agent_runtime` | spawn 在 generator **外**（同步預取 ORM 後回 Response） | `src/controllers/ai_assistant.py:643` |
| `websockets.connect()` | 在 `relay()` 內，**零 retry**，refused 直接拋到 SSE error | `src/controllers/ai_assistant.py:724` |
| `_build_mcp_snapshot(project, template)` → `{'servers': {...}}` | 綁在 controller | `src/controllers/ai_assistant.py:867` |
| `_build_skill_snapshot(project, template)` → `[{name,version,sha256,description}]` | 綁在 controller | `src/controllers/ai_assistant.py:874` |
| `_resolve_agent_template(project)` | 綁在 controller，用 `request.env` | `src/controllers/ai_assistant.py:831` |
| `agent_session.state` | `absent/spawning/active/reaped/degraded/spawn_failed`，UNIQUE(project_id) | `src/models/agent_session.py:54` |
| skill 觸發點 | `action_publish` / `action_deprecate` / `write`（state 變動） | `src/models/skill.py:208,224,150` |
| mcp 觸發點 | `mcp_server.write`（active/state/scope guard）、`mcp_tool.write` | `src/models/mcp_server.py:134`、`src/models/mcp_tool.py:33` |
| skill / mcp scope | `system`（project_id NULL）/ `user`（project_id NOT NULL） | `src/models/skill.py:33`、`src/models/mcp_server.py:49` |

## 目標

1. cold-start 首則訊息**不再需要使用者手動重試**：Odoo relay 自動退避重連到 pod
   ready，期間前端顯示「啟動中…」而非紅色 error。
2. skill / mcp 發布或變更時，Odoo **主動**對受影響的 running pod 即時推送新
   snapshot，best-effort、不阻斷 publish。

## 已拍板的架構決策（brainstorming 2026-06-02）

| # | 決策 | 選擇 | 理由 |
| --- | --- | --- | --- |
| D1 | 處理範圍 | #123 + #111 一起完整做 | client 層共用一次 refactor |
| D2 | #123 修法層 | **純 Odoo relay 修復**（不動 operator） | 對標 G3 durable 修法、不需重發 image、可快速迭代 |
| D3 | #111 推送邏輯歸屬 | **`agent_session` model 集中** | session 即 pod 的天然歸屬；snapshot builders 下沉於此，chat/publish 共用一份 |
| D4 | #111 推送時機 | **同步 best-effort** | 符合 issue 明訂 best-effort；實作簡單；下一輪 chat attach refresh 是 fallback |

## 設計

### Refactor① — snapshot 邏輯下沉到 `agent_session`（D3，#111+#123 共用基礎）

把以下三者從 controller 抽到 `agent_session` model（或其 helper），controller 改為
delegate；簽名改成不依賴 `request`、改吃 `env` / `project` / `template`：

- `_build_mcp_snapshot(project, template)` → `agent_session` 上的方法。
- `_build_skill_snapshot(project, template)` → 同上。
- `_resolve_agent_template(project)` → 同上（`request.env` → `self.env` / 傳入 env）。

`_stream_agent_runtime`（chat 路徑）改呼叫下沉後的共用版，行為不變（回歸測試守住）。

> 邊界注意：下沉後 controller 仍負責 SSE/HTTP，model 只算純資料（snapshot dict/list、
> resolve template）。不把 HTTP/SSE 邏輯混進 model。

### #123 — cold-start relay 修復（純 Odoo，4 子項）

**子項 1：HTTP timeout 分級**
`AgentRuntimeClient._request` 改接受 per-call `timeout`，全域 `_TIMEOUT` 保留為預設：

- `spawn_or_attach`：較長 timeout（建議 **90s**），給 cold helm install + image pull 餘裕。
- `invalidate_skills` / `invalidate_mcp`：較短 timeout（建議 **10s**），best-effort 不久候。
- 其他（reap/resume/status/...）：維持預設 30s。

**子項 2：spawn-or-attach bounded retry（client 內）**
`spawn_or_attach` 對「連線/讀取逾時類」例外做 bounded retry（建議最多 2 retry，
線性或指數 backoff，總時間受子項 1 timeout 約束）。**安全性依據**：spawn-or-attach
idempotent（release 已存在就 attach），重試不會重複建 pod。非逾時類錯誤（4xx 等）
不 retry，直接拋。

**子項 3：WS connect retry（generator 內，G3 核心）**
`relay()` 的 `websockets.connect` 包 bounded retry + backoff：

- 攔 `ConnectionRefusedError` / `OSError`（Errno 111）/ connect timeout。
- 退避重連（建議 backoff 1s→2s→…→上限 5s），**總上限 ~60s deadline**。
- **retry 只包 connect**：connect 成功才進入既有 `ws.send(user_message)` + drain 迴圈。
  避免「send 後斷線重連又重送 user_message」造成 pod 收到重複訊息（idempotency 陷阱）。
- 達 deadline 仍 refused → yield 一個 fatal error envelope（前端顯示「agent 啟動逾時，
  請稍後再試」），而非低層 `Errno 111` 字串。

**子項 4：SSE 進度 event + 前端**

- WS connect retry 期間，`relay()` 先 yield `{'type':'status','payload':{'stage':'spawning','elapsed_ms':N}}`；
  generator 主迴圈已會把 `status` 型別轉成 SSE（`ai_assistant.py:770-776` 既有分支）。
- 前端 `src/static/src/paas/components/ai-chat/AiChat.js`：對 `stage:'spawning'` 的
  status event 顯示「啟動中…」連線指示（沿用既有 `connectionIndicator` 狀態機 +
  `reconnecting` 樣式），retry 全程不顯示紅色 error；只有最終 fatal 才轉 error。

> **取捨（spawn 階段的 UX）**：`spawn_or_attach` 維持在 generator **外**（保持 ORM
> 預取 cursor 生命週期簡單）。代價是 spawn 進行中（最長 90s）前端為「連線中」
> （EventSource 已開、尚未收第一個 event），WS 階段才有「啟動中…」字樣。這已比現況
> （spawn 逾時 → 紅色 `spawn_failed`）好。把 spawn 也移進 generator 以統一進度文案，
> 列為 follow-up（需在 generator 內重建 ORM env，複雜度較高，本次 YAGNI）。

### #111 — skill/mcp on-publish 即時推送（純 Odoo）

**子項 1：client 補 `invalidate_skills`**
`AgentRuntimeClient.invalidate_skills(project_hash, skill_snapshot)`，對稱
`invalidate_mcp`，POST `/{hash}/invalidate-skills`，body `{'skill_snapshot': list or []}`，
用子項（#123-1）的短 timeout。

**子項 2：`agent_session` 的推送協調方法（D3 集中點）**
在 `agent_session` 新增 best-effort 推送方法，職責：

- 入參：受影響的 scope（system / user）+（user 時）`project_id`、變更類型（skill / mcp）。
- 列舉受影響 running pod：查 `agent_session` `state = 'active'`。
  - **system scope** → 全部 active session（跨 project）。
  - **user scope** → 該 `project_id` 的 active session（至多一筆，UNIQUE(project_id)）。
  - 只推 **active**：`spawning` 的 cold-start 走 spawn_or_attach 本來就帶最新 snapshot，
    不需重推；`reaped/absent` 沒有 running pod。
- 對每個受影響 session：`_resolve_agent_template(project)` → 重算對應 snapshot
  （skill→`_build_skill_snapshot`、mcp→`_build_mcp_snapshot`）→ 呼叫對應 client invalidate。
- **best-effort**：`get_agent_runtime_client(env)` 為 None（operator 未設定）→ 整段 no-op；
  單一 session 推送 raise → `_logger.warning` 後**繼續下一個**，不向上拋（不中斷 publish）。

**子項 3：觸發 hook**

- skill：`action_publish` / `action_deprecate` 末段呼叫推送（state→published/deprecated
  改變了「哪些 skill 進 pod」）；`write` 對 state 欄位變動兜底。傳 `scope` +（user 時）
  `project_id`。
- mcp：`mcp_server.write` 對 `active` / `state` / 綁定變動觸發；`mcp_tool.write` 對影響
  pod 可見 tool 的變動觸發（透過 `server_id.scope` 推導範圍）。
- **防遞迴**：skill 的 `_recompute_is_latest` 內部 write 的是 `is_latest`（不含 state），
  不踩 state 條件（`skill.py:172-179` 已有此防護），推送掛在 state 分支即可，不會遞迴。

**子項 4：穩健性與邊界**

- ConfigMap kubelet 傳播延遲：「即時」是盡力，仍可能下一輪才實際生效（issue 已明訂，
  文件記錄此語意）。
- 推送發生在 publish 事務內（同步）：用短 timeout 限制單 pod 阻塞；system skill 推多
  pod 時 publish 會略慢——可接受（publish 非高頻、非熱路徑）。

## 資料流

```
# #123 cold-start chat（修後）
browser ──EventSource──▶ _stream_agent_runtime
  ├─ spawn_or_attach（generator 外，timeout=90s + bounded retry，idempotent）
  │     └─ 仍失敗 → SSE error（罕見）
  └─ generate()/relay()（generator 內）
        ├─ websockets.connect ──refused/timeout──┐
        │      ▲                                  │ backoff（≤5s），deadline 60s
        │      └────── yield status:spawning ─────┘ → 前端「啟動中…」
        ├─ connect 成功 → ws.send(user_message)（只送一次）
        └─ async for envelope → SSE relay（text_chunk/tool_*/done/error）

# #111 on-publish 推送（修後）
skill.action_publish / mcp_server.write
  └─ agent_session.<push>（best-effort，同步）
        ├─ client = get_agent_runtime_client(env)；None → no-op
        ├─ 列舉 active session（system=全部 / user=該 project）
        └─ for session: resolve template → 重算 snapshot
              └─ client.invalidate_skills / invalidate_mcp（timeout=10s）
                    └─ raise → warning，繼續下一個（不中斷 publish）
```

## 錯誤處理

| 情境 | 行為 |
| --- | --- |
| spawn 連線/讀取逾時 | client bounded retry（idempotent）；耗盡 → SSE `spawn_failed` |
| WS connect refused（pod 未 ready） | relay 退避重連到 deadline；期間 `status:spawning` |
| WS connect 達 deadline 仍 refused | fatal error envelope（友善文案，非 Errno 111） |
| connect 成功後 WS 中途斷 | **不**重送 user_message（沿用既有 error 分支） |
| publish 時 operator 未設定 | 推送整段 no-op |
| publish 時單 pod invalidate 失敗 | warning + 繼續；publish 照常成功 |

## 測試策略（Odoo-env，mock operator client）

- **#123**：
  - WS connect 連續 refused 後成功 → 不報錯、最終出 `done`（mock connect 前 N 次拋
    `ConnectionRefusedError`）。
  - connect 達 deadline 仍 refused → 出 fatal error，不是裸 `Errno 111`。
  - connect 成功後 drain → `user_message` **只送一次**（守 idempotency 陷阱）。
  - spawn timeout → client retry 次數正確、idempotent、耗盡才 `spawn_failed`。
- **#111**：
  - system skill publish → 對**所有** active session 呼叫 `invalidate_skills`；
    user skill publish → 只對該 project。
  - mcp 變更對等。
  - 無 active session / operator 未設定 / 單 pod 失敗 → publish 不中斷（best-effort）。
  - snapshot 內容 = 下沉後共用 builder 的輸出（與 chat 路徑一致）。
- **回歸**：既有 `test_agent_pod_phase_*`（spawn/aclose/client/webhook）續綠；snapshot
  下沉不改既有 chat 行為。
- 執行：CI / `k8s-dev-sandbox`（依 CLAUDE.md「本地 e2e 走 k8s-dev-sandbox」）。

## docs 更新清單（任務尾段）

- `docs/tracking/followups/follow-ups.md`：F-ARL-1（#123）標 RESOLVED + 指向本 spec / PR；
  #111 收尾紀錄。
- `docs/tracking/pitfalls/agent-runtime-fpilot10-cluster-e2e-gap-chain.md`：G3 `OPEN`→`CLOSED`，
  補實作細節（retry/backoff/deadline）。
- `docs/guides/development/agent-runtime-chat-architecture.md`：§1.4 補 WS retry + 進度 event；
  新增「on-publish 即時推送」小節（§5.4 對照）。
- 視通用性抽 pattern doc（候選 `cold-start-connect-retry.md` / `snapshot-single-source-push.md`）——
  decision 留到實作後評估，不預先承諾。

## 非目標 / YAGNI

- 不動 operator / chart / image（D2）。
- 不把 spawn 移進 generator 統一進度文案（follow-up）。
- 不做背景 queue 推送（D4 同步 best-effort）。
- 不在 snapshot 內補 rich args_schema（既有 deferred enhancement，與本次無關）。
- 不改 SSE↔WS 的兩跳橋接架構本身（只補 robustness）。
