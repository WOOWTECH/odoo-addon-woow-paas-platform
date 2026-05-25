---
title: Agent Runtime 演進史 (Phase A~G)
category: guides
status: active
created: 2026-05-25
updated: 2026-05-25
tags: [paas, skill-system, roadmap, phases]
related:
  - README.md
  - 09-security-model.md
  - ../../plans/2026-Q2/agent-runtime-pod-plan.md
  - ../deployment/agent-runtime-pilot-flip-gate-runbook.md
---

# Agent Runtime 演進史 (Phase A~G)

> 這套系統不是一次蓋好的，而是分 7 個 phase 漸進推進，並刻意用 enable-gate 讓功能「先到位、後開啟」。對應計畫：`docs/plans/2026-Q2/agent-runtime-pod-plan.md`。

## 🗺️ 時間軸

```mermaid
graph LR
    A["Phase A<br/>schema + 4 models"] --> B["Phase B<br/>runtime repo + chart"]
    B --> C["Phase C<br/>Odoo + operator wiring"]
    C --> D["Phase D<br/>enable-gate 硬化"]
    D --> E["Phase E<br/>pilot dual-write"]
    E --> F["Phase F<br/>破壞性清理"]
    F --> G["Phase G<br/>艦隊 CD"]
    style D fill:#fff0b3,stroke:#e6a700,stroke-width:2px
```

## 📋 各 Phase 做了什麼

| Phase | 內容 | 狀態 |
|---|---|---|
| **A** | schema M:N 遷移（`cloud_service_id`→`cloud_service_ids`）+ 新增 4 個 model（agent_template / agent_session / skill / project_access）+ 16 條 ACL + 4 個後台 view | ✅ |
| **B** | 建新 repo `woow-paas-agent-runtime`（runtime image）+ helm chart（`woow-paas-charts/charts/agent-runtime/`）；CI build/push 到 JCR；**不部署** | ✅ |
| **C** | Odoo wiring：skill-bundle 端點 + client/webhook + SSE gateway；operator 端點（spawn/reap/resume/delete）；token 生命週期 | ✅ |
| **D** | enable-gate caveat 修補（F-PC-1~7 / F-PA-14 硬化）+ admin 啟用面 + 跨 repo F-PC-7 | ✅ **現在** |
| **E** | pilot + 2~12 週 dual-write；對 pilot cohort 翻 `use_agent_runtime=True`；觀察；逐步 rollout | ⏳ 下一步 |
| **F** | 破壞性清理：確認無需 rollback 後移除舊路徑程式碼 | ⬜ |
| **G** | 艦隊 production CD + 永久 dual-write 收尾 | ⬜ |

## 📍 目前狀態（2026-05）

- 在 branch `feature/agent-pod-phase-d`，**Phase D 完成**。
- 整套 agent-runtime 仍以 **enable-gate 關閉**，尚未對使用者開啟。
- Pod 端 transport/skill/安全鷹架已備，但 **agent loop（`langgraph_runtime`）仍是 Phase B stub**，真正 langgraph 調用待後續 phase——見 [agent-runtime Pod 內部](06-pod-internals.md)。
- **翻 gate 的前置條件**：`paas-agent-runtime` 的 **F-PC-7**（Pod 端 `token_use` 嚴格驗證）必須先 merge 到 main 並部署，否則有 1 小時在途 token 空窗（D-CAVEAT-2）。見 [Skill 系統安全模型](09-security-model.md)。

## 🧠 兩個關鍵策略名詞

- **enable-gate** — 功能先合併到位但用開關關閉，降低「合併即上線」的風險。
- **dual-write** — Phase E 讓新舊路徑並行寫入，比對一致後才正式切換，可隨時回退。

## 📁 相關文件

- [PaaS Skill 系統總覽](README.md)
- [Skill 系統安全模型](09-security-model.md) — F-PC-7 / D-CAVEAT 細節
- [Agent Runtime Pod 部署機制](07-deployment.md)
- repo 內：`docs/plans/2026-Q2/agent-runtime-pod-plan.md`、`docs/guides/deployment/agent-runtime-pilot-flip-gate-runbook.md`
