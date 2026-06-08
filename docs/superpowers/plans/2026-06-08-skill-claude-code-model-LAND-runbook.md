---
title: Skill Claude Code 模型 — 跨 repo LAND runbook
date: 2026-06-08
status: ready-for-land-window
spec: docs/superpowers/specs/2026-06-08-skill-claude-code-model-redesign-design.md
interface_contract: paas-agent-runtime/docs/specs/skill-bash-sidecar-interface.md (a8affca)
security: woow-paas/paas-agent-runtime#26
---

# Skill Claude Code 模型 — LAND runbook

所有實作完成、四個跨組件 gap 全收尾、gate 鏈單一真相源、全部 fail-closed default。
本 runbook 是 land 窗口的部署依賴鏈與步驟。

## 安全性質（為何 land 低風險）

**一個 boolean `skillRunner.enabled` 驅動整條鏈，全部 fail-closed**：
```
paas-cd 設 operator env AGENT_RUNTIME_SKILL_RUNNER_ENABLED=true（land 才設）
  → operator(_build_install_values, cce58a3) 顯式 --set skillRunner.enabled=true 給 agent-runtime chart
    → 同一 chart 值同時驅動：① sidecar container 起 ② agent container gate env=true → runtime 暴露 bash:execute
```
未 flip 前一切 fail-closed（bash:execute 不暴露、無 #26 洞）。故**可任意順序 merge/build/publish，最後一次 flip 才開**。唯一硬約束：flip 時 agent-runtime image 必須 v1.4.0（含 bash_bridge）+ chart 0.4.0 到位 → paas-cd 用 fleet-drift-check + JCR HEAD 確認 image 已 publish 才 flip。

## 產出 inventory（全部 open、未 merge）

| 元件 | repo / 位置 | PR / commit | land 後版本 |
| --- | --- | --- | --- |
| Odoo validation 移除 entrypoint | 本 repo `fix/skill-upload-issues` | `4429a49` `5d2c3ed` | — |
| Odoo operator forward（kill switch） | 本 repo `extra/paas-operator` | `a900a19`→`cce58a3` | operator image rebuild |
| Drop A（roster + load_skill） | paas-agent-runtime | **PR #27** | — |
| Drop B（R4-R7 bridge daemon + bash:execute） | paas-agent-runtime `feat/skill-claude-code-model-drop-b` | 待開 PR | **agent-runtime image v1.4.0** |
| 介面契約 doc | paas-agent-runtime | `a8affca` | — |
| agent-runtime chart（sidecar + agent gate env, default false） | woow-paas-charts | **PR #20** | chart **0.4.0** |
| operator chart（env passthrough, default false） | woow-paas-charts | **PR #21** | chart **0.1.14** |
| land checklist | paas-cd (memory) | — | — |

## 要 build 的 image（land 時）

1. **agent-runtime image v1.4.0**（Drop B，含 bash_bridge daemon）：runtime push `v1.4.0` tag → CI build。
2. **operator image**（含 `cce58a3` skillRunner forward）：本 repo `build-operator` skill → `operator-v*` tag → CI build。land 前給 paas-cd `operator-v* tag ↔ cce58a3` 對照驗證。

## Land 步驟（協調順序）

> 全 fail-closed，前 4 步可平行；第 5 步（flip）是開關，最後做。

1. **Review + merge code/chart PR**（仍 default-off，安全）：
   - Odoo `fix/skill-upload-issues`（Task 1+2+5）→ PR → develop
   - paas-agent-runtime PR #27（Drop A）+ Drop B PR
   - woow-paas-charts PR #20 + PR #21
2. **Build images**：runtime push `v1.4.0`；本 repo `build-operator`（→ operator-v* tag）。
3. **Publish chart**：agent-runtime 0.4.0 + operator 0.1.14 到 registry。
4. **paas-cd 前置驗證**：fleet-drift-check + JCR HEAD 確認 ① operator image(cce58a3) ② operator chart 0.1.14 ③ agent-runtime 0.4.0 + image v1.4.0 都 publish。
5. **paas-cd 一次性 Fleet flip**（開關）：
   - operator 部署：operator chart 0.1.14 + operator image(cce58a3) + **`AGENT_RUNTIME_SKILL_RUNNER_ENABLED=true`** + operator 注入 agent-runtime 的 `agent_runtime_image_tag=v1.4.0` / `chart_version=0.4.0`
   - 4 node `podPidsLimit`（第 3 道兜底，非 blocker）
6. **既有 pod 汰換**：新 config 走 cold-start path，既有 agent pod 需 respawn 才生效（知識型 load_skill 新 pod 即可用；script skill 需 sidecar 到位）。incident kill switch = 翻 `AGENT_RUNTIME_SKILL_RUNNER_ENABLED=false`（下次 spawn 生效，非即時撲滅 live pod — 即時隔離走 L2）。
7. **Odoo Task 3+4**（runtime land 後）：demo hello-world re-pack 成純 SKILL.md（舊 executable demo 在 R5 移除 per-skill 路徑後不能跑）+ migration 18.0.1.0.35（見 Odoo 計畫 Task 3+4，含 `test_skill_dual_scope::test_demo_bundle_entrypoint_runnable` 移除）。

## 編排途中抓到的 4 個跨組件 gap（land 前已全閉）

1. operator 未 forward skillRunner.enabled → silent no-op（paas-cd recon）→ 補 operator forward（cce58a3）
2. chart-default-true 讓 operator env 失效 → 改 operator always-explicit（authoritative kill switch）
3. operator chart env 名 lock-step → 讀碼驗 `AGENT_RUNTIME_SKILL_RUNNER_ENABLED` 一字不差
4. agent container 缺 gate env → bash:execute 永遠 fail-closed no-op（Drop B 揭露）→ 補 agent-container env

## 回滾

- 未 flip：無需回滾（fail-closed）。
- flip 後出問題：① 即時降級 = operator env flip false（新 spawn 停掛 bash，既有 respawn 後生效）② 完整回滾 = paas-cd rollback chartVersion/imageTag。
