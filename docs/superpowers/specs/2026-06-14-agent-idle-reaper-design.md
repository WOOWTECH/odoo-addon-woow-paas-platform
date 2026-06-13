---
title: Agent Idle Reaper（D4）設計
category: spec
status: approved
created: 2026-06-14
---

# Agent Idle Reaper（D4）設計

> **Goal**：閒置的 agent runtime pod 自動 scale-to-0（`active → reaped`），釋放 cluster quota（特別是 RAM）。這是使用者最初「agent 閒置兩天沒下線」問題的正解。

## 背景（現況）

- `agent_session.last_active`（`agent_session.py:148`，readonly）**全 repo 零生產寫入點** —— idle 判定的輸入是死的。
- 唯一的 cron 是 `_cron_drift_detection`（5 分鐘，只對帳，不下線）。
- `client.reap(project_hash)`（`agent_runtime_client.py:160`）已實作（→ operator `POST /{hash}/reap` → scale-to-0 + 寫 `reaped-since` annotation），但 production 零 caller。
- operator reap 端點**冪等**（already_reaped → HTTP 200）。

## 架構決策：Odoo cron + Odoo heartbeat（自包含）

design §2.3 原規劃「cluster 端」：pod 寫 `agent.woow.tw/last-active` annotation + cluster CronJob（`paas-system`，ClusterRole）掃描。**本設計改採 Odoo 端**，理由：

- **自包含於本 repo**：複用既有 Odoo cron pattern（drift cron）+ 既有 `client.reap()`，無需動 pod（跨 repo `paas-agent-runtime`）、無需新 cluster manifest（CronJob/ClusterRole）、無需 pod RBAC（patch namespace）。
- **活動訊號其實不粗**：chat 走 SSE，`api_ai_stream`（`ai_assistant.py:488`）在每個 user turn 的串流期間都在迴圈裡（代理 pod 回應給瀏覽器），Odoo 知道每個 turn 的起訖。
- **取捨**：看不到「純 pod 內、零 Odoo 參與的長時間自主活動」，但 reap 是**非破壞性** scale-to-0、PVC 保留、resume 可復原，30 min 門檻下可接受（見下「殘餘風險」）。

> 本設計**不含**：D3 long-term GC cron、cluster CronJob、pod annotation heartbeat。涵蓋 D4（reaper）+ Odoo 端 heartbeat（原 D5 的 Odoo 版）+ `agent_idle_reap_seconds` config（D6 的必要切片）。

## 元件

### ① Heartbeat（Odoo 寫 `last_active`）

在 SSE 串流處理 `api_ai_stream`：

- **串流開始**（spawn_or_attach 後）stamp `last_active = now()` —— 記錄「user 起了一個 turn」。同時確保 spawn/resume 路徑把 `state` 設回 `active`（防 re-reap loop 的 invariant）。
- **串流結束**（正常收尾）再 stamp 一次 —— 讓「長 turn」結束時刷新，避免跑很久的 turn 被誤判 idle。
- **節流**：只在 `last_active` 比 now 舊 ≥ 60s 才寫，避免寫入放大。
- 冷啟 spawn 當下也 stamp，確保 active session 的 `last_active` 永不為空。

**殘餘風險（已接受）**：單一 turn 連續串流 > T_idle 且中途無串流結束 → reaper 可能在串流中途 reap。對互動式 portal chat 極罕見；reap 非破壞性、resume 可復原（串流會錯、user 重送即恢復）。YAGNI：**不**在串流代理熱路徑做週期性補 stamp（若日後出現長自主任務再加）。

### ② Reaper cron

- 新 `ir_cron_agent_session_idle_reaper` → `model._cron_idle_reaper()`，每 **5 分鐘**，加進 `agent_session_cron.xml`（`noupdate="1"`，同 drift cron）。
- **掃描**：`state='active' AND (last_active or spawn_at) < now - T_idle`。`last_active` 為空的 legacy session fallback 用 `spawn_at`（Python 端算 effective time，每 project 一筆、量小）。
- **每筆 reap**：
  1. `get_agent_runtime_client` 取不到 → log + skip。
  2. `client.reap(project_namespace_hash)`：
     - 成功 → `state='reaped'`、`reaped_since=now()`（eager set 求即時一致；operator 亦寫 annotation/可能回 webhook）。
     - 失敗（`AgentRuntimeOperatorError`/網路）→ **log warning + skip，留 `active`，下輪重試**。
- **best-effort（與 D2 的關鍵差異）**：reap 失敗不造成孤兒/計費風險（pod 續 active、下輪重試），故**不阻塞、不 raise**；一筆失敗不影響同批其他 session（cron 不可整批炸）。對比 D2 teardown 失敗會留孤兒 → 必須 fail-block。
- **冪等 + drift 互動**：operator reap 冪等；reap 後 `state='reaped'` 與 drift cron 讀回的 operator status 一致，無衝突。

### ③ Config

- `ir.config_parameter` `woow_paas_platform.agent_idle_reap_seconds`，預設 `1800`（30 min）。
- 加進 `src/hooks.py` 的 `_CONFIG_PARAMETERS`（卸載清理）。

## 測試（TransactionCase + mock client，本機 k8s-dev-sandbox 可跑）

**Reaper**：
- reap 過期 active → `client.reap` 被呼叫、`state='reaped'`、`reaped_since` 有值。
- 不 reap 新鮮 active（last_active 近期）。
- 不 reap 非 active 狀態（reaped/spawning/absent）。
- best-effort：`client.reap` raise → session 留 `active`、`_cron_idle_reaper` 不拋例外（同批其他仍處理）。
- `last_active` 為空 → fallback `spawn_at` 判定。
- T_idle 可由 `agent_idle_reap_seconds` 調整（設短值→立即 reap；設長值→不 reap）。

**Heartbeat**：
- helper 在 `last_active` 舊於節流（60s）時更新、近期時跳過。
- spawn/resume 路徑把 `state` 設 `active` + stamp `last_active`（防 re-reap loop）。

**回歸**：完整模組套件 0 failed。

## Related

- 設計依據：`docs/plans/2026-Q2/agent-runtime-pod-design.md` §2.3
- 前置（已出貨）：D1+D2 teardown — PR #195、commits `91ee778`/`e5d458d`
- 既有元件：`client.reap`（`agent_runtime_client.py:160`）、drift cron（`agent_session_cron.xml`）、SSE gateway（`ai_assistant.py:488`）
- 後續：D3 long-term GC cron（依賴本 D4 產生 `reaped` 狀態才有輸入）
