# guides/ — 任務導向

「**如何做某事**」的操作手冊。新人 onboarding、部署步驟、測試流程、tutorial 都歸這。

## 進入條件

- 給「如何做 X」的答案 + **步驟可重現**（複製貼上能跑）
- 對象是要動手做事的人（developer、operator、tester）

## 退出條件

- 步驟過期超過 2 個 release 或對應功能下架 → `archived/`

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`getting-started.md`](getting-started.md) | 新人快速上手（入口檔） |
| [`development/`](development/oauth2-architecture.md) | 開發者技術細節（[OAuth2 架構](development/oauth2-architecture.md)、[/woow AI Chat（agent-runtime）資料流與 Secret 關係](development/agent-runtime-chat-architecture.md)、[merge train 每一節的驗收清單](development/merge-train-acceptance.md) 等） |
| [`deployment/`](deployment/k8s-setup.md) | **[發佈到 Production SOP](deployment/prod-release-sop.md)**（Fleet GitOps 全流程 + 版本矩陣，給人類與自動化 agent）；K8s setup、運維、troubleshooting、admin roles；runbook（[agent-runtime image 升版 SOP](deployment/agent-runtime-image-upgrade-sop.md)、[agent-runtime pilot flip-gate](deployment/agent-runtime-pilot-flip-gate-runbook.md)） |
| [`testing/`](testing/TESTING.md) | Odoo test、PaaS operator test、E2E test guide |
| [`tutorials/`](tutorials/cloud-odoo-addon-management.md) | 完整逐步指引（cloud odoo addon management、[Security Access route 管理與狀態判讀](tutorials/security-access-routes.md)） |
| [`../archived/guides/frameworks/`](../archived/guides/frameworks/) | 已退役框架（superpowers、SuperClaude）的舊指南；現行 mattpocock/skills 見 [`../agents/`](../agents/issue-tracker.md) |
| [`paas-skill-system/`](paas-skill-system/) | PaaS Skill 系統（agent-runtime-pod）onboarding 知識庫（含端到端流程、對話/串流、Pod 內部、安全模型、術語表等；篇數見該目錄 README） |
| [`maintenance/`](maintenance/plan-catalog-update.md) | 維運 SOP：[plan catalog 改價](maintenance/plan-catalog-update.md)、[stg 清理（#324）](maintenance/2026-07-10-324-stg-cleanup-sop.md) |
| [`smart-home-half-year-gift-points-admin-sop.md`](smart-home-half-year-gift-points-admin-sop.md) | Smart Home 半年贈點 eWallet 發放 SOP（admin 操作） |

## 命名

- `kebab-case`，禁空格與底線
- 入口檔提升至 `guides/` root（如 `getting-started.md`）
- 主題目錄放專業領域文檔

## 寫作慣例

- 步驟必須含**前置條件 → 動作 → 預期結果**
- 命令行範例優先（複製貼上即可執行）
- 失敗情境的 troubleshooting 放在主步驟之後
- 引用 reference/ 規格時用相對路徑（從 guides 子目錄看，形如 `../../reference/api/<file>.md`）
