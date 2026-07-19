# archived/plans/ — 已收官 epic 的 plan 檔

`docs/plans/README.md` 退場政策的落點：**epic 完成、知識已被主 docs（reference / decisions / guides）或 code 等值覆蓋**的 plan 檔移入此處（按年度子目錄）。逐檔 frontmatter 帶 `archived-from` + `archived-reason`（指向現行權威）。

> 搬入前必須通過 brace-aware inbound 掃描（`{design,implementation}` 花括號引用展開比對）——被 CLAUDE.md / hooks / src / tests 引用的 plan 檔屬 load-bearing，不得歸檔。

## 2026/

2026-07-19 首批（April–May epic，26 檔）：

| 檔案群 | 主題 | archive 理由 / 現行權威 |
|---|---|---|
| `2026-04-05-backend-views-cloud-service-odoo-addon{,-design}.md` | CloudService backend views | 已落地；規格=`../../reference/features/cloud-services.md` |
| `2026-04-08-addon-deselection-safe-removal{,-design}.md` | addon 反選安全移除 | 已落地；現況在 src 與 cloud-services spec |
| `2026-04-12-code-review-fixes-design.md` | review 修正批 | 一次性，已落地 |
| `2026-04-12-wallet-billing-{design,implementation}.md` | Wallet & Billing dashboard | 已落地；論述=`../../decisions/philosophy/billing-philosophy.md` |
| `2026-04-16~20-settings-*.md`（6 檔） | Settings UI 五 phase | 已落地；現況=`src/static/src/paas` |
| `2026-04-19-e2e-test-results.md` | E2E 測試快照 | 一次性快照 |
| `2026-04-20-session-loss-3ds-bind-{design,implementation}.md` | 3DS 綁卡 session loss | 已落地；現況=`src/controllers/billing.py` |
| `2026-04-21-ecpg-topup-charge-{design,implementation}.md` | ECPG 儲值扣款 | 已落地；操作指南=ecpay skill |
| `2026-04-28-hotfix-bundle.md`、`2026-04-28-sprint*.md`（4 檔） | hotfix / sprint 微任務 | 一次性，已落地 |
| `2026-05-06-pkce-oauth-migration{,-design}.md` | PKCE OAuth 遷移（含 journal） | 已上線；現況=`src/controllers/oauth2.py`＋`../../guides/development/oauth2-architecture.md` |
| `2026-05-10-smart-home-{role-check-audit,subscription-template-design}.md` | Smart Home 角色稽核／訂閱模板 | 已落地；現況=`src/models/smart_home` 系列 |

**刻意保留在 `docs/plans/` 的同期檔**（複驗發現 load-bearing，勿再提案歸檔）：
- `2026-04-28-billing-issue-fix-design.md` — 被 `2026-04-28-sprint3-g4p2-dashboard-summary.md` 標為 API contract source of truth
- `2026-04-28-sprint3-g4p2-dashboard-summary.md` — 被 `src/controllers/dashboard.py` 引用
