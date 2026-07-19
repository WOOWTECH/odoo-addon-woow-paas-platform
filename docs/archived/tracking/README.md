# archived/tracking/ — 已收官的一次性報告

tracking 區的 point-in-time 產物（CAQA / reviewer 驗收、phase durable、測試覆蓋、可行性稽核）在 cycle 收官後移入此處。逐檔 frontmatter 帶 `archived-from` + `archived-reason`。**全 repo inbound 引用已同步改寫指向本處**（patterns / pitfalls 以這些報告為「落地 case」證據錨點，引用鏈仍有效）。

2026-07-19 首批（42 檔）：

| 檔案群 | 數量 | 性質 |
|---|---|---|
| `caqa-report-2026-05-*.md` | 8 | CAQA 驗收報告（mcp-refactor、project-detail、agent-runtime phase A/C/D/E0/F-PILOT-10、decouple-frontend） |
| `reviewer-report-2026-05-*.md` | 8 | 對應同名 cycle 的 reviewer 驗收報告 |
| `agent-runtime-pod-phase-{b,c,d}-*.md` | 3 | agent-runtime epic phase durable（epic 已收官、prod 已上線） |
| `test-coverage-*.md`、`test-report-*.md`、`test-scope-report*.md`、`issue-39-test-coverage.md`、`*-e2e-test-report.md`、`*-tdd-red.md`、`2026-05-28-skill-e2e-test-coverage.md` | 15 | 一次性測試報告（原 `tracking/test-reports/`） |
| `cf-env-token-paas-env-feasibility.md`、`cf-ns-prod-stg-audit-report.md` | 2 | CF 可行性／稽核報告（F-ENV 系列後續在 `tracking/followups/`） |
| `2026-06-25-fenv2-l4-reconciler-explainer.md` | 1 | L4 reconciler 說明（設計權威=`docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md`） |
| `2026-05-28-decuple-project-deliverables.md`、`2026-06-25-prod-release-sop-audit-closeout.md`、`2026-05-28-decouple-project-frontend-endpoint.md`、`2026-05-03-paas-meeting-goal-progress.md` | 4 | 一次性交付／稽核收口（原 `tracking/reports/` 與 `test-reports/`） |
| `2026-04-28-issue.md`、`sandbox-hostpath-rot.md`、`stg-sandbox-networkpolicy-cross-ns-block.md` | 3 | 已完結 issue（9/9 完成；local-mode 廢止；NP 排查已被 #279/#280 landing 取代） |

> 新的一次性報告仍寫到 `docs/tracking/`（reports/、test-reports/），收官後批次移入本處——別直接寫進 archived。
