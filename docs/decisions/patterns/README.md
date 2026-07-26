# patterns/ — 可複用 pattern 索引

> `docs/decisions/patterns/` 收錄跨 refactor cycle 抽出的可複用 pattern。**改動相關 model / ACL / lifecycle / 測試前，先在此表定位適用 pattern，再點進細節**——這是避免 B1-class data leak 與同類 bug 重炸的第一道導引。
>
> CLAUDE.md「Known patterns」段已指針化（不再雙寫子集）；**本表為完整 29 條**。路徑觸發的自動導引由 pattern-guard hook 供給（對照表 `.claude/hooks/pattern-guard-map.json`）。pattern slug 為 AI 自組 cross-link identifier，請勿更名（會斷交聯）。

## 維護慣例（新增 pattern doc 時）

1. **同步登錄本索引**：新增 `patterns/<slug>.md` 時，務必在本檔「何時必看（按主題分組）」對應主題表補一行（pattern slug + 何時觸發 / 必看），並在「完整檔案清單」加一筆連結；同時更新頂部「完整 N 條」計數。
2. **評估是否綁特定路徑**：若該 pattern 有明確的觸發檔案路徑（如 `src/security/`、`src/controllers/`、特定 model 檔）——則**額外**在 `.claude/hooks/pattern-guard-map.json` 對應 class 的 `docs` append 一行（`路徑 — 一句何時必看`），或新增 class；hook 會在編輯該路徑時自動餵給模型。純判斷類（無路徑可綁）的 pattern **只留本索引**。**勿再把守線散文寫回 CLAUDE.md**（該段已指針化，雙寫必漂移）。

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
| [[scope-flip-write-guard-not-ir-rule]] | model 有 scope / tenancy 欄位（scope system/user、project_id、public）且對 `base.group_user` 可寫，要擋「窄→寬 flip」（提權 / 跨租戶外洩）→ 必須在 `write()` guard 攔 vals + 放行 sudo/admin，ir.rule 對 pre-write 判定擋不住（與 6-rule split 互補：split 守 create+可見性、guard 守 flip） |
| [[su-env-exempt-guard-false-green]] | 守「不可變 identity anchor」（subdomain_prefix / canonical_name / reference_id）的 model `create()` / `write()` guard：**不可**用 `not env.su` / `not has_group()` 豁免（`TransactionCase` 是 su env → deny 分支跑不到 → 假綠 + sudo 路徑靜默繞過）→ 改 context-key escape hatch 無條件擋所有人，且 create + write 雙向覆蓋（與 scope-flip 分野：那條值可 gated 改、本條值不可變） |
| [[constraint-input-trust-expansion]] | model 有 attacker-writable Many2many + ir.rule / `@api.constrains` 用該 m2m 做 subset / authority 推導時 |
| [[attachment-row-level-acl-and-anchor-stamping]] | 自家 model 透過 `ir.attachment` 存敏感 / RCE-class binary payload → anchor stamping + ir.attachment ir.rule 兩層 |
| [[odoo-std-field-no-override]] | 任何想動 Odoo std model 欄位型態 / shape 的 PR |
| [[hidden-template-presentation-chain]] | 新增/修改 /woow controllers 曝光點（list、by-id、dashboard、訂閱/發票清單）時 — template.is_hidden SSOT＋DRY helper 全鏈呈現層過濾（非 ACL；/my、/report 繞過；D2 專屬 hidden workspace 不變式） |

### 資料模型 / relation 設計（新增 agent 資源類型或動 binding 模型前）

| pattern | 何時觸發 / 必看 |
|---|---|
| [[resource-ownership-vs-template-activation]] | 平台資源（skill / mcp_server）同時要「屬於誰」與「哪個 agent 啟用」兩種語意 → 歸屬 = 資源端 M2O FK（1:N）+ 配置 = template 端 M2M + subset constraint 黏合；新增第三種資源類型照抄 4 件套；看到「project O2M 與 template M2M 好像重複」或想讓 agent 隱式全吃 project 資源時必讀 |
| [[template-vs-plan-property-boundary]] | 新商品變體上架猶豫「開新 CloudAppTemplate 還是同 template 加 plan」時：需要不同 **template 級屬性**（image/chart/init script）＝必開新 template；只差 plan 級（價格/cs_* 規格/AC 席數/可見性）＝同 template 多 plan；有人提議「plan 加 image 欄位」時必讀 |
| [[seed-agent-template-design]] | 動 `src/data/agent_template_default.xml` / `src/hooks.py` 的平台預設 template seed 前：tenant-neutral invariant（unbound＋無 user-scope 綁定）的設計依據與 trade-off 全文（pattern-guard seed-default-template class 必讀件；2026-07-19 自 superpowers promote 入列） |

### ORM 寫入 / lifecycle / cleanup

| pattern | 何時觸發 / 必看 |
|---|---|
| [[invariant-recompute-on-orm-write-hook]] | model 有 cross-record derived flag / invariant（如至多一筆 `is_latest` per group）只在 transition method 收斂，但還有第二條 ORM 寫入入口（fixture / data XML / API / cron）會 drift → 把 recompute 搬到 `create()` / `write()` hook 兜底 |
| [[cloud-service-unlink-override-cleanup]] | parent 刪除時需先清 wrapper-class child rows、FK ondelete 不能 cascade（child 真正 owner 是別的 model）、且 caller 有多條 path → parent `unlink()` override 清理 |
| [[sidecar-backed-flag-lifecycle]] | 同 model 既有系統 / lifecycle 建立的 row 也有 user-managed row、兩者 cleanup / sync 行為不同 → schema boolean flag 標誌物理依賴，cleanup filter + create path 對等遵守契約 |
| [[spawn-install-values-missed-forward]] | 有組裝點把上游資料 → 下游 helm install values / config，下游硬性 `required`：撞「缺一個 value」時通常系統性漏轉多個 → 修一個就全盤對齊 + 加 contract test |
| [[plan-driven-date-helper]] | subscription 日期由 plan 強制覆寫（trial / non-trial path），且與第三方 onchange 有 post-confirm coupling 時 |
| [[two-stage-remote-provision-idempotency]] | provision method 先建遠端資源（CF tunnel / 雲端 DNS / SaaS 帳號 / k8s ns）再跑後續步驟，且 UI 把 retry 掛在 `error` 上 → 第二段失敗讓遠端資源孤兒掛著、retry 重跑第一段在雲端再建一條（永久孤兒 / 計費外洩）→ 遠端建立步驟必須被 stored-id guard 包住，每條失敗路徑保留 stored id 讓 retry 安全（單段 method 的免-guard 安全是單段性換來的，加段就要買回來） |
| [[ns-quota-hard-limits-limitrange-lockstep]] | 新 ns 類型要加 ResourceQuota、或動既有 ns quota hard map 時：hard 一旦宣告 `limits.cpu/memory`，admission 強制 ns 內每個 container（含 init/sidecar）必帶 limits，缺者拒建且訊息隱晦 → LimitRange default（500m/512Mi）是 correctness requirement 非 best-practice，兩者必須同 PR 同落點 lock-step（出自 #278/PR #312） |
| [[external-resource-env-token-naming]] | 平台代為在 prod/stg **共用**的外部系統（CF account/zone、雲端 DNS、SaaS 帳號）產生資源時，命名/標籤要帶可一眼辨識 prod/stg 的 env token（共用帳號裡無此 token → 孤兒清理 / 稽核無法自證歸屬）→ 注入在「平台命名的單一根」（本 repo = Odoo `naming.py` 的 `canonical_name`，一改全覆蓋 tunnel/DNS/Access 且 create/delete 天然對稱），env 源用**顯式** config（禁從 `namespace_prefix` 隱式推 prod=空字串），只注入不參與反查的標籤欄位（name/comment/tag），靠重算 name 反推的路徑（`generate_subdomain`）須 create↔delete 雙向對稱 |

### 測試紀律（multi-phase / credential 硬化）

| pattern | 何時觸發 / 必看 |
|---|---|
| [[phase-versioned-test-assertion]] | phase test 硬編當時 schema 快照值（manifest version `== "x"` / constraint 名 / 欄位 count），後續 phase 合法演進同塊 schema 時撞 test-vs-test red → 改用 monotonic `>=` / 存在性斷言鎖 intent（**前提是被守的量 monotonic**；會下降的量見 [[seed-topology-invariants]]） |
| [[seed-topology-invariants]] | test 硬編 seed 拓撲計數（base plan 數 / 掛 template 的 plan 數 / 訂閱 product 總數 / tier 數）時：這些量**非 monotonic**（純翻 `storefront_hidden`、`active_test` 過濾、seed xmlid 真刪都會讓它下降），`>=` 守衛會裝反、`> 0` 等於刪掉 test → 改集合等值 / 零丟棄 / 結構關係；含 `noupdate=0` 可解析 XML 而 `noupdate=1` 不可的不對稱（出自 #433） |
| [[credential-claim-test-fixture-coupling]] | 硬化共用 credential 路徑（token 加 required claim / secret optional→required fail-closed）→ 所有自建該 credential 的 test fixture 都 forward-coupled，須 lock-step 補，否則紅（好：暴露）或假綠（壞：繞過 gate） |
| [[unittest-mock-dual-namespace-patch]] | 用 `unittest.mock.patch` 攔一個被多 module 共用的 helper（client factory / `requests.post` / `uuid4`），且各 module 用不同 import 形式（頂部 `from m import name` vs function-local import）→ `patch` 是 rebind namespace attribute 非改寫 function，一個 symbol 經 N 個 namespace 使用就要 N 個 patch target；少打一個讓那條路徑 fall through 真 client（false confidence 或 BlockedRequest 紅）；fix 半套 mock 是「補上缺的」非「搬去別處」 |

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
- [external-resource-env-token-naming](external-resource-env-token-naming.md)
- [hidden-template-presentation-chain](hidden-template-presentation-chain.md)
- [invariant-recompute-on-orm-write-hook](invariant-recompute-on-orm-write-hook.md)
- [mcp-acl-6-rule-split](mcp-acl-6-rule-split.md)
- [ns-quota-hard-limits-limitrange-lockstep](ns-quota-hard-limits-limitrange-lockstep.md)
- [odoo-addon-std-rule-symmetric-deactivation](odoo-addon-std-rule-symmetric-deactivation.md)
- [odoo-std-field-no-override](odoo-std-field-no-override.md)
- [orm-vs-endpoint-acl-test-coverage](orm-vs-endpoint-acl-test-coverage.md)
- [paas-workspace-member-acl-tightening](paas-workspace-member-acl-tightening.md)
- [phase-leader-brief-as-pointer-not-source](phase-leader-brief-as-pointer-not-source.md)
- [phase-versioned-test-assertion](phase-versioned-test-assertion.md)
- [plan-driven-date-helper](plan-driven-date-helper.md)
- [plan-gap-stop-and-report](plan-gap-stop-and-report.md)
- [resource-ownership-vs-template-activation](resource-ownership-vs-template-activation.md)
- [scope-flip-write-guard-not-ir-rule](scope-flip-write-guard-not-ir-rule.md)
- [seed-agent-template-design](seed-agent-template-design.md)
- [seed-topology-invariants](seed-topology-invariants.md)
- [sidecar-backed-flag-lifecycle](sidecar-backed-flag-lifecycle.md)
- [spawn-install-values-missed-forward](spawn-install-values-missed-forward.md)
- [su-env-exempt-guard-false-green](su-env-exempt-guard-false-green.md)
- [template-vs-plan-property-boundary](template-vs-plan-property-boundary.md)
- [two-layer-acl-lockstep](two-layer-acl-lockstep.md)
- [two-stage-remote-provision-idempotency](two-stage-remote-provision-idempotency.md)
- [unbound-project-acl-dual-leg](unbound-project-acl-dual-leg.md)
- [unittest-mock-dual-namespace-patch](unittest-mock-dual-namespace-patch.md)
