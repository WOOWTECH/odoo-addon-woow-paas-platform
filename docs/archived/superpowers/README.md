# archived/superpowers/ — 退役的 superpowers 探索文件

superpowers（brainstorming / writing-plans）產出中**已落地且知識已轉入主 docs、或被後續設計取代**的 plan / spec 歸這。每檔 frontmatter 帶 `status: archived` 與 `archived-reason`（2026-07-19 批次起；更早歸檔的 8 檔理由見 commit `a2b3d914`）。

本 README 只放一行摘要與指向現行權威的指標，**不重複文件內容**。

## plans/

| 檔案 | 摘要 | 歸檔原因（現行權威） |
|------|------|--------------------|
| [2026-06-01-agent-runtime-state-sync-webhook.md](plans/2026-06-01-agent-runtime-state-sync-webhook.md) | operator state-update webhook 接線＋雙端 secret 計畫；PR #118 落地 | 機制權威＝`guides/development/agent-runtime-chat-architecture.md` §4/§5.1；延後項在 follow-ups F-ASW-1~6 |
| [2026-06-01-default-agent-template.md](plans/2026-06-01-default-agent-template.md) | #112 平台預設 agent template 實作計畫；PR #112 落地 | 現行落點見 `guides/development/agent-runtime-chat-architecture.md`（#111 refactor 後） |
| [2026-06-01-seed-agent-template.md](plans/2026-06-01-seed-agent-template.md) | seed 平台預設 template 實作計畫；PR #116 落地 | 設計依據 spec 現於 `decisions/patterns/seed-agent-template-design.md`（2026-06-25 批次歸檔） |
| [2026-06-02-agent-runtime-coldstart-onpublish.md](plans/2026-06-02-agent-runtime-coldstart-onpublish.md) | cold-start 退避重連（#123）＋發布即時推送（#111）計畫；PR #130 落地 | 權威＝`guides/development/agent-runtime-chat-architecture.md` §1.4/§4.5 |
| [2026-06-05-portal-task-agent-chat.md](plans/2026-06-05-portal-task-agent-chat.md) | portal task 頁 agent chat 實作計畫；PR #149 落地 | 架構權威＝`guides/development/agent-runtime-chat-architecture.md`（2026-06-25 批次歸檔） |
| [2026-06-07-security-access-p2-access-control.md](plans/2026-06-07-security-access-p2-access-control.md) | SA P2 Access control 實作計畫；live E2E 驗證落地 | 決策權威＝`decisions/refactor/security-access-bare-tunnel-zero-route.md`（2026-06-25 批次歸檔） |
| [2026-06-08-skill-claude-code-model-LAND-runbook.md](plans/2026-06-08-skill-claude-code-model-LAND-runbook.md) | 跨 repo 上線 runbook；STG＋prod 已 flip（PR #170/#174） | 一次性 runbook；kill-switch 鏈知識在 `guides/paas-skill-system/12-skill-runner-bash-sidecar.md` |
| [2026-06-08-skill-claude-code-model-odoo.md](plans/2026-06-08-skill-claude-code-model-odoo.md) | claude-code skill 模型重設計 Odoo 端計畫；已落地 | 姊妹計畫 -runtime 現於 `plans/2026-Q2/`（2026-06-25 批次歸檔） |
| [2026-06-14-agent-gc-cron.md](plans/2026-06-14-agent-gc-cron.md) | D3 長期 GC cron 計畫；已落地 | 生命週期知識權威＝pitfall `reaped-since-single-path-stamp-gc-immunity`（2026-06-25 批次歸檔） |
| [2026-06-14-agent-idle-reaper.md](plans/2026-06-14-agent-idle-reaper.md) | D4 idle reaper（scale-to-0）計畫；已落地 | 同上（2026-06-25 批次歸檔） |
| [2026-06-14-drift-reaped-gc-eligibility.md](plans/2026-06-14-drift-reaped-gc-eligibility.md) | drift/webhook 讓 reaped session GC 免疫的修復計畫；PR #200 落地（.40） | 知識已轉入 pitfall `reaped-since-single-path-stamp-gc-immunity` |
| [2026-07-10-330-pricing-seed-redesign-plan.md](plans/2026-07-10-330-pricing-seed-redesign-plan.md) | #330 定價 seed 重定＋taxonomy 實作 plan；PR #333 merged | 現行 SOP＝`guides/maintenance/plan-catalog-update.md`；拍板權威＝`plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md`；價目已被 Wave B 對齊 214 manifest 取代 |

## specs/

| 檔案 | 摘要 | 歸檔原因（現行權威） |
|------|------|--------------------|
| [2026-06-01-default-agent-template-design.md](specs/2026-06-01-default-agent-template-design.md) | #112 設計 spec（config_parameter＋resolver fallback）；已被 #116/#125 演進取代 | 權威＝`guides/development/agent-runtime-chat-architecture.md`＋pitfall `default-agent-template-project-bind-silent-autoenable`＋`decisions/patterns/seed-agent-template-design.md` |
| [2026-06-02-agent-runtime-coldstart-onpublish-design.md](specs/2026-06-02-agent-runtime-coldstart-onpublish-design.md) | #123＋#111 設計 spec（D1–D4 拍板）；PR #130 落地 | 語意已等值轉入 `guides/development/agent-runtime-chat-architecture.md`；「現況事實」表為修前快照 |
| [2026-06-05-portal-task-agent-chat-design.md](specs/2026-06-05-portal-task-agent-chat-design.md) | portal chat 設計 spec（含實作後記）；PR #149 落地 | 架構＝`guides/development/agent-runtime-chat-architecture.md`；偏差紀錄＝follow-ups F-PORTAL-1~3；ACL 紀律＝`decisions/patterns/two-layer-acl-lockstep.md` |
| [2026-06-14-agent-gc-cron-design.md](specs/2026-06-14-agent-gc-cron-design.md) | D3 GC cron 設計 spec；已落地 | 同 plans/ 對應條（2026-06-25 批次歸檔） |
| [2026-06-14-agent-idle-reaper-design.md](specs/2026-06-14-agent-idle-reaper-design.md) | D4 idle reaper 設計 spec；已落地 | 同 plans/ 對應條（2026-06-25 批次歸檔） |
| [2026-06-14-drift-reaped-gc-eligibility-design.md](specs/2026-06-14-drift-reaped-gc-eligibility-design.md) | reaped_since ⟺ state invariant 設計 spec；PR #200 實現 | 知識＝pitfall `reaped-since-single-path-stamp-gc-immunity`＋`decisions/patterns/scope-flip-write-guard-not-ir-rule.md` |
