---
title: Skill Claude Code 模型 — 跨 repo LAND runbook
category: archived
status: archived
created: 2026-06-08
archived: 2026-07-19
archived-from: docs/superpowers/plans/2026-06-08-skill-claude-code-model-LAND-runbook.md
archived-reason: 一次性上線 runbook，STG＋prod 皆已落地；長期知識（kill-switch 鏈、4 gap、fail-closed）已完整轉入 docs/guides/paas-skill-system/12-skill-runner-bash-sidecar.md，現行權威為該 guide。
---

# Skill Claude Code 模型 — LAND runbook

所有實作完成、四個跨組件 gap 全收尾、gate 鏈單一真相源、全部 fail-closed default。
本 runbook 是 land 窗口的部署依賴鏈與步驟。

## Land 狀態（2026-06-09）

### ✅ STG 已上線並完整驗證

- **merged**：runtime PR #27（Drop A）+ #28（Drop B）→ main；charts PR #20（agent-runtime 0.4.0）+ #21（operator 0.1.14）→ main；Odoo PR #170 → develop。
- **images published**：`agent-runtime:1.4.0`（digest e0a8dd05，含 bash_bridge）、`paas-operator:20260609.0233`（git tag operator-v20260609.0233 ↔ cce58a3，含 skillRunner forward）。
- **STG Fleet flip**：paas-app-bundle-cd **PR #12** merged（operator config skillRunner.enabled=true + agent_runtime_image_tag=1.4.0 + chart 0.4.0 / operator 0.1.14）。
- **STG 驗證全綠**（活 pod，project CTX-Verify-147 / ns paas-ws-stg-pa-nrxh4a5ml2dovwha）：
  - infra：2 container、sidecar runAsUser=10002、both ready、liveness pass、agent env `AGENT_RUNTIME_SKILL_RUNNER_ENABLED=true`、operator rolled、`shareProcessNamespace` unset、roRoot、supplementalGroups=[10003]。
  - 功能：bash:execute 端到端回傳 `hello-sidecar`。
  - bridge log corroboration：cmd 走異 UID sidecar（peer_uid=10001、cwd=project hash 目錄、exit 0、bytes=14）。

### ⏸️ Prod 待辦（user 暫緩，改天）

1. **paas-cd prod Fleet flip**：同 STG（operator prod 部署 chart 0.1.14 + image cce58a3 + env true + image_tag 1.4.0 + chart 0.4.0）。artifact 全部已 publish、不必重 build。
2. **podPidsLimit**（4 node，與 prod 一起做）：k3s `pod-max-pids=4096` 滾動重啟。⚠️ 3/4 control-plane/etcd → quorum 風險、需 node SSH access。第 3 道兜底、非 blocker。
3. 驗 prod fresh spawn 起 sidecar（同 STG 流程）。

### ⏸️ 其他待辦

- **paas-odoo addon 發佈**（validation 移除 entrypoint + demo 純 SKILL.md + migration 18.0.1.0.35）：走 `v18.0.1.0.35` tag → paas-odoo image。origin 有 `chore/bump-manifest-18.0.1.0.35` branch，**Odoo addon release 歸屬待 user 與團隊確認**（不卡 bash 功能）。Odoo PR #170 已 merge 進 develop。
- **paas-agent-runtime PR #25**（#23 tool-call args trace 續作，open）：與 skill 功能無耦合，留給 canvas agent 自行 merge。

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
