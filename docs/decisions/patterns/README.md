# patterns/ — 可複用 pattern 索引

> `docs/decisions/patterns/` 收錄跨 refactor cycle 抽出的可複用 pattern。**改動相關 model / ACL / lifecycle / 測試前，先在此表定位適用 pattern，再點進細節**——這是避免 B1-class data leak 與同類 bug 重炸的第一道導引。
>
> CLAUDE.md「Known patterns」段只收錄其中高頻 ACL / data-leak 守線子集；**本表為完整 18 條**。pattern slug 為 AI 自組 cross-link identifier，請勿更名（會斷交聯）。

## 何時必看（按主題分組）

### ACL / 資料隔離（改 `ir.rule`、`ir.model.access.csv`、controller access 前）

| pattern | 何時觸發 / 必看 |
|---|---|
| [[odoo-addon-std-rule-symmetric-deactivation]] | 在 `ir.model.access.csv` 給 `base.group_user` 加 project / task / Odoo std model 的 RWCU 時 → 必須對稱停用對應 std ir.rule |
| [[paas-workspace-member-acl-tightening]] | 收緊 PaaS workspace member 對 workspace 內資源的可見 / 可寫範圍時 |
| [[orm-vs-endpoint-acl-test-coverage]] | 改 controller endpoint 的 list / get / stats / aggregation 路徑 + ir.rule-protected model：controller layer 不可 `.sudo()` 繞 ir.rule，ORM test 綠 ≠ endpoint secure，須對等補 HttpCase cross-user negative（含 anti-pattern 三變體 A/B/C） |
| [[two-layer-acl-lockstep]] | 同 model 被兩層以上 enforcement 守（ir.rule + controller carve-out，或多 endpoint 各自 access resolver）：任一層改 domain / 收緊，另一層必須同步，否則 silent divergence 比未修更糟 |
| [[unbound-project-acl-dual-leg]] | model 可選綁 workspace-bearing model（nullable M2O），unbound row 仍須對部分 user（creator / follower）可見 → dual-leg ir.rule domain（bound walk + unbound carve-out） |
| [[mcp-acl-6-rule-split]] | 同 model 混兩種 scope（platform public-read + workspace-scoped）且需在 `base.group_user` 開 CRUD → 按 perm direction 拆 3 條 + sub-model 鏡像 = 6 條（single dual-leg 不夠，create direction 有提權面） |
| [[constraint-input-trust-expansion]] | model 有 attacker-writable Many2many + ir.rule / `@api.constrains` 用該 m2m 做 subset / authority 推導時 |
| [[attachment-row-level-acl-and-anchor-stamping]] | 自家 model 透過 `ir.attachment` 存敏感 / RCE-class binary payload → anchor stamping + ir.attachment ir.rule 兩層 |
| [[odoo-std-field-no-override]] | 任何想動 Odoo std model 欄位型態 / shape 的 PR |

### ORM 寫入 / lifecycle / cleanup

| pattern | 何時觸發 / 必看 |
|---|---|
| [[invariant-recompute-on-orm-write-hook]] | model 有 cross-record derived flag / invariant（如至多一筆 `is_latest` per group）只在 transition method 收斂，但還有第二條 ORM 寫入入口（fixture / data XML / API / cron）會 drift → 把 recompute 搬到 `create()` / `write()` hook 兜底 |
| [[cloud-service-unlink-override-cleanup]] | parent 刪除時需先清 wrapper-class child rows、FK ondelete 不能 cascade（child 真正 owner 是別的 model）、且 caller 有多條 path → parent `unlink()` override 清理 |
| [[sidecar-backed-flag-lifecycle]] | 同 model 既有系統 / lifecycle 建立的 row 也有 user-managed row、兩者 cleanup / sync 行為不同 → schema boolean flag 標誌物理依賴，cleanup filter + create path 對等遵守契約 |
| [[spawn-install-values-missed-forward]] | 有組裝點把上游資料 → 下游 helm install values / config，下游硬性 `required`：撞「缺一個 value」時通常系統性漏轉多個 → 修一個就全盤對齊 + 加 contract test |
| [[plan-driven-date-helper]] | subscription 日期由 plan 強制覆寫（trial / non-trial path），且與第三方 onchange 有 post-confirm coupling 時 |

### 測試紀律（multi-phase / credential 硬化）

| pattern | 何時觸發 / 必看 |
|---|---|
| [[phase-versioned-test-assertion]] | phase test 硬編當時 schema 快照值（manifest version `== "x"` / constraint 名 / 欄位 count），後續 phase 合法演進同塊 schema 時撞 test-vs-test red → 改用 monotonic `>=` / 存在性斷言鎖 intent |
| [[credential-claim-test-fixture-coupling]] | 硬化共用 credential 路徑（token 加 required claim / secret optional→required fail-closed）→ 所有自建該 credential 的 test fixture 都 forward-coupled，須 lock-step 補，否則紅（好：暴露）或假綠（壞：繞過 gate） |

### Agent / hand-off 紀律（maestri 多 phase agent pod 工作流）

| pattern | 何時觸發 / 必看 |
|---|---|
| [[plan-gap-stop-and-report]] | agent 執行 leader hand-off / 連修 e2e gap chain 時遇 plan gap（沒設計過的決策點）或 test 假設錯 → 停下回報 escalate，別硬塞假值掩蓋 gap、別 bend impl 湊綠 |
| [[phase-leader-brief-as-pointer-not-source]] | phase leader 寫給 phase agent 的 brief 是導讀（pointer）非 source-of-truth；agent 撞到 brief 與 plan signed-off / decision invariant / schema 實際 shape 不符時 → trust source、回報 leader 校正 brief |

## 完整檔案清單（供瀏覽 / link check）

- [attachment-row-level-acl-and-anchor-stamping](attachment-row-level-acl-and-anchor-stamping.md)
- [cloud-service-unlink-override-cleanup](cloud-service-unlink-override-cleanup.md)
- [constraint-input-trust-expansion](constraint-input-trust-expansion.md)
- [credential-claim-test-fixture-coupling](credential-claim-test-fixture-coupling.md)
- [invariant-recompute-on-orm-write-hook](invariant-recompute-on-orm-write-hook.md)
- [mcp-acl-6-rule-split](mcp-acl-6-rule-split.md)
- [odoo-addon-std-rule-symmetric-deactivation](odoo-addon-std-rule-symmetric-deactivation.md)
- [odoo-std-field-no-override](odoo-std-field-no-override.md)
- [orm-vs-endpoint-acl-test-coverage](orm-vs-endpoint-acl-test-coverage.md)
- [paas-workspace-member-acl-tightening](paas-workspace-member-acl-tightening.md)
- [phase-leader-brief-as-pointer-not-source](phase-leader-brief-as-pointer-not-source.md)
- [phase-versioned-test-assertion](phase-versioned-test-assertion.md)
- [plan-driven-date-helper](plan-driven-date-helper.md)
- [plan-gap-stop-and-report](plan-gap-stop-and-report.md)
- [sidecar-backed-flag-lifecycle](sidecar-backed-flag-lifecycle.md)
- [spawn-install-values-missed-forward](spawn-install-values-missed-forward.md)
- [two-layer-acl-lockstep](two-layer-acl-lockstep.md)
- [unbound-project-acl-dual-leg](unbound-project-acl-dual-leg.md)
