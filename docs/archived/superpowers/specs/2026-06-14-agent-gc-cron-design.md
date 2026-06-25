---
title: Agent Long-term GC cron（D3）設計
category: spec
status: approved
created: 2026-06-14
---

# Agent Long-term GC cron（D3）設計

> **Goal**：長期處於 `reaped`（scale-to-0）的 agent session，超過 T_gc（預設 30 天）後**完整 teardown**（helm uninstall + delete namespace）並轉為 `absent`，回收 PVC / helm release object / namespace / 明文 LLM Secret。補齊三段式生命週期 `active →(D4 reaper)→ reaped →(D3 GC)→ absent`。

## 背景（現況，前置已就緒）

- `state` 含 `reaped` / `absent`；`reaped_since`（`agent_session.py:149`）由 D4 reaper 寫入。
- `_teardown_runtime()`（D2 加，`agent_session.py`）已做**完整 teardown**：`client.delete` → operator `DELETE` → `helm uninstall` + `delete namespace`（D1 兩步驟，含 §8.4 LLM Secret 回收）。docstring 明示「shared by the unlink hook & future GC」。它**失敗時 raise**（給 D2 unlink 的 fail-block 用）。
- D4 已建 `agent_session_cron.xml`（drift cron + idle reaper cron）、`hooks.py` config 清理 pattern、`agent_idle_reap_seconds` config。

## 架構：Odoo cron + 複用 `_teardown_runtime`（best-effort）

沿用 D4 拍板的「Odoo cron 自包含」路線（非 cluster CronJob）。GC 比 design §2.5 的「裸 helm uninstall」更完整 —— 直接複用 `_teardown_runtime` 即得 §8.4 完整 teardown。

## 元件

### ① GC cron

- 新 `ir_cron_agent_session_gc` → `_cron_gc_reaped()`，加進 `agent_session_cron.xml`（`noupdate="1"`，同 drift / reaper cron 慣例）。
- **cadence：每天一次**（design §2.5 `0 0 * * *`；GC 為 30 天尺度，不需 reaper 的 5 分鐘）。Odoo ir.cron 設 `interval_number=1, interval_type=days`。
- **掃描**：`state='reaped' AND reaped_since < now - T_gc`。`reaped_since` 為空的 reaped session（理論上不該發生，防禦性）→ skip（無從判定超期，不貿然刪）。
- **每筆**：
  - `session._teardown_runtime()` 包 **try/except**（best-effort）：
    - 成功 → `session.write({'state': 'absent'})`。
    - 失敗（`AgentRuntimeOperatorError` / 網路 / operator 未設定）→ `_logger.warning` + `continue`，留 `reaped`，隔天重試。一筆失敗不影響同批其他（cron 不可整批炸）。
- **保留 row**：不刪 `agent_session`，僅 `state='absent'`。未來該 project 再 chat，`_project_hash` search 沿用既有 row（spawn 重建）。

### ② Config

- `ir.config_parameter` `woow_paas_platform.agent_gc_seconds`，預設 `2592000`（30 天）。
- 加進 `src/hooks.py` 的 `_CONFIG_PARAMETERS`（卸載清理）。

## error handling 對照

| 情境 | 行為 |
|---|---|
| GC teardown 成功 | `state='absent'` |
| GC teardown 失敗（operator 掛/未設定/網路） | log + skip，留 `reaped`，隔天重試（best-effort，**非** fail-block —— GC 失敗不造成新孤兒，pod 本就 scale-to-0） |
| `reaped_since` 為空 | skip（防禦） |

> 與 D2 unlink 的差異：unlink fail-block（失敗會留孤兒 → 必須擋）；GC best-effort（reaped pod 已 scale-to-0，延後回收無孤兒風險）。與 D4 reaper 的 best-effort 一致。

## 測試（TransactionCase + mock client，本機 k8s-dev-sandbox 可跑）

- GC 超期 reaped → `client.delete`（經 `_teardown_runtime`）被呼叫、`state='absent'`。
- 不 GC 未超期 reaped（reaped_since 近期）。
- 不 GC 非 reaped 狀態（active / absent）。
- best-effort：`_teardown_runtime` raise（mock `client.delete` side_effect）→ 留 `reaped`、`_cron_gc_reaped` 不拋例外。
- `reaped_since` 為空 → skip。
- T_gc 可由 `agent_gc_seconds` 調整（設短值 → 立即 GC）。
- 回歸：完整模組套件 0 failed。

## 不在範圍

cluster CronJob；PVC reclaim 策略細節（依 StorageClass，design Q5）；orphan PVC reconciler；pod-side 行為。

## Related

- 設計依據：`docs/plans/2026-Q2/agent-runtime-pod-design.md` §2.5（+ §8.4 完整 teardown）
- 前置（已 merged，PR #195）：D1 兩步驟刪除、D2 `_teardown_runtime`、D4 reaper（產生 `reaped` + `reaped_since`）
- 既有元件：`_teardown_runtime`（`agent_session.py`）、`agent_session_cron.xml`、`hooks.py` `_CONFIG_PARAMETERS`
- 完成後：三段式生命週期 `active → reaped → absent` 補齊（D5 pod-side heartbeat 已由 D4 的 Odoo-side heartbeat 取代，無需）
