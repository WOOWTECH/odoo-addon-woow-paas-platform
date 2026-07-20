---
title: Harness 蒸餾——把 CLAUDE.md 軟約束下沉為機器強制（pattern-guard hook + 假綠 gates + 減法 audit + retro 迴路）
status: draft
date: 2026-07-06
owner: woow-paas-platform (harness)
reviewers: eugene
related: ["docs/decisions/patterns/README.md", ".claude/hooks/pattern-guard.py", "scripts/audit/repo-lint.py"]
---

# Harness 蒸餾 design

> **一句話**：Harness 的知識資產幾乎全堆在「書面原則」層（靠模型讀到、理解、記得），本計畫把可機械化的部分下沉到「機器強制」層（hook / lint gate），把剩餘部分收斂為單一登錄點（README index），並建立讓 Harness 隨使用複利成長的 retro 迴路。

## 背景：槓桿階梯

Harness 對模型行為的約束力有四層，由強到弱：

1. **機器強制**（hooks、CI gate、lint）——零智能需求，永不失效
2. **結構化流程**（skill checklist）——低智能需求，照做即可
3. **書面原則**（CLAUDE.md、pattern docs）——需要被讀到、被理解、被記得應用；遵守率隨模型能力與 context 壓力衰減
4. **模型自身判斷**——最貴、最不穩定

本 repo 的守線資產（25 條 pattern docs、31 條 pitfalls、CLAUDE.md 的 Known patterns 段）品質極高，但**全部位於第 3 層**。優化方向不是再加規則，而是把既有規則往上推層。

## 現況盤點（2026-07-06 實測）

| 指標 | 數值 | 含義 |
| --- | --- | --- |
| 專案 CLAUDE.md | **48,931 chars（~12K tokens）** | 每個 session 開場付全額 context 稅 |
| pattern docs 實際數 | **25** | CLAUDE.md Known patterns 只列 13 → **雙寫已漂移** |
| pitfall docs 實際數 | **31** | CLAUDE.md 只列 ~18 → **雙寫已漂移** |
| controllers `.sudo(` 呼叫點 | **155**（14 檔） | pattern doc 記載「34 處 audit checklist」→ **手動 checklist 已失守 4.5 倍** |
| 專案 `.claude/settings.json` | 只有 permissions | 無任何專案層 hook（乾淨落點） |

三個數字共同指向同一結論：**靠人 / 模型自覺維護的第 3 層約束，在這個 repo 的迭代速度下必然漂移**。CLAUDE.md 自己寫著「詳細結構已交由其他文件維護，避免雙寫漂移」，但 patterns / pitfalls 區塊正是雙寫且已漂移的活例。

## 四個 workstream

### W1：CLAUDE.md 減法 audit（本文件 §audit 清單；執行見 implementation doc）

原則：**CLAUDE.md 只留「每個 session 都需要的操作事實」與「單行指針」**；觸發式知識交給 hook 在對的時刻餵、完整索引交給 README。

### W2：pattern-guard PreToolUse hook（已實作、已測試，**安裝待核准**）

把 CLAUDE.md「改 X 前必看 Y」清單機器強制化：

- **機制**：Edit/Write 守護路徑時，本 session 首次觸碰該類別 → deny 一次並列出必讀 docs（含一行 why），寫 marker；**重試同一編輯即放行**。speed-bump，不是牆。
- **對照表外置**：`.claude/hooks/pattern-guard-map.json`——新增守線只 append 一行，不改程式。這是 retro 迴路（W4）的落點之一。
- **Fail-open**：任何解析錯誤 / 非本 repo 路徑 / 缺 map → 放行；`CLAUDE_PATTERN_GUARD=off` 可整段關閉。
- **已驗證行為**（scratchpad pipe-test，2026-07-06）：首次 deny ✓、marker 放行 ✓、非守護路徑直通 ✓、`agent_template_default.xml` 雙 class 命中取聯集 ✓。

守護類別（7 類，完整內容見 map 檔）：

| class | pattern | 對應守線 |
| --- | --- | --- |
| acl-security | `src/security/{ir.model.access.csv,ir_rules.xml}` | 6 條 ACL pattern + README 索引 |
| controller-endpoint | `src/controllers/*.py` | orm-vs-endpoint、two-layer-lockstep、asymmetric-deny |
| model-write-guard | `src/models/{agent_session,agent_template,security_access,smart_home,cloud_service,mcp_server}*.py` | su-env-exempt、scope-flip、invariant-recompute、reaped-since、two-stage-idempotency |
| seed-data | `src/data/*.xml` | seed-noupdate-sticky、invariant-recompute |
| seed-default-template | `agent_template_default.xml` + `src/hooks.py` | tenant-neutral invariant 全套 |
| owl-template | `src/static/src/**/*.xml` | owl-template-crash |
| paas-operator | `extra/paas-operator/**` | 202-mock-contract、idempotency、integration tests README |

### W3：假綠 gates——`scripts/audit/repo-lint.py`（已實作、已測試；hook wiring 待核准）

把「假綠家族」pitfalls 中**可機械判定**的三條轉成 gate（每條訊息內建 doc 指針）：

| check | 攔什麼 | 對應 pitfall | 驗證結果 |
| --- | --- | --- | --- |
| controller-sudo | `src/controllers` 新增 `.sudo(` 超出 per-file baseline | orm-test-green-endpoint-leak-blindness | 合成違規檔 → 觸發 ✓ |
| owl-template | component 上 `t-att*`、`t-on-*.enter` 類 modifier、XML parse error | owl-template-crash-class-syntax | 兩種違規皆觸發 ✓ |
| data-noupdate | 新增 `noupdate="1"` data 檔未列 baseline | seed-noupdate-sticky-template-links | 觸發 ✓ |

- 三模式：`--all`（CI / merge 前全掃）、`--hook`（PostToolUse 即時回饋，違規以 `decision:block` 餵回模型自我修正）、`--update-baseline`（有意識接受現況）。
- baseline 已 seed 現況（sudo 14 檔 155 點、noupdate 19 檔）＝**grandfather 既有、只攔新增**；縮減是好事，會在 `--update-baseline` 時自然收斂。
- 155 處存量 sudo 的逐點清查是獨立工程（見 follow-ups），gate 先止血。

**不機械化的**（判斷密集，留第 3 層 + pattern-guard 指針）：single-path state stamp 類（要理解狀態機）、cross-repo 落點假設、gap-chain 類——這些由 pattern-guard 在對的檔案上餵 doc。

### W4：retro 迴路（spec；SKILL.md 草稿見 implementation doc Task 4）

現行 pitfall 沉澱靠使用者自律。補一個 `/retro` project skill：session 收尾時掃描翻車點 → 判定一次性 vs class → 按強度選落點：

1. 可機械判定 → `repo-lint.py` 加 check（最強）
2. 綁特定路徑 → `pattern-guard-map.json` append（次強）
3. 跨情境判斷 → pattern doc（`/docs:new`）
4. 敘事對照 → pitfall doc（`/docs:new`）

關鍵紀律：**已被 code guard / gate 機械化的教訓，不再寫成散文提醒**——這是讓 CLAUDE.md 不再長回來的機制。

## W1 audit 清單（逐條判定）

### A. Known patterns 段（CLAUDE.md 現列 13 條 + 4 條 model 前置）→ **整段換成指針 + hook**

判定依據：patterns/README.md 已有完整 25 條「何時必看」觸發表；pattern-guard 在對的檔案上主動餵。CLAUDE.md 保留的唯一價值是「不知道要看哪」的入口——一行指針就夠。

| 現有條目 | 判定 | 去處 / 依據 |
| --- | --- | --- |
| CloudService/Project decoupling | 縮指針 | refactor doc 自足；7 invariants 在 doc 內 |
| odoo-addon-std-rule-symmetric-deactivation | 刪（hook 接管） | pattern-guard `acl-security` class |
| paas-workspace-member-acl-tightening | 刪（hook 接管） | 同上，README 索引涵蓋 |
| odoo-std-field-no-override | 刪（索引接管） | README「何時必看」已列 |
| constraint-input-trust-expansion | 刪（索引接管） | 同上 |
| attachment-row-level-acl-and-anchor-stamping | 刪（索引接管） | 同上 |
| orm-vs-endpoint-acl-test-coverage | 刪（hook 接管） | `controller-endpoint` + `acl-security` 雙 class |
| two-layer-acl-lockstep | 刪（hook 接管） | 同上 |
| unbound-project-acl-dual-leg | 刪（hook 接管） | `acl-security` class |
| mcp-acl-6-rule-split | 刪（hook 接管） | 同上 |
| scope-flip-write-guard-not-ir-rule | 刪（hook 接管） | `acl-security` + `model-write-guard` |
| su-env-exempt-guard-false-green | 刪（hook 接管） | `model-write-guard` class |
| two-stage-remote-provision-idempotency | 刪（hook 接管） | `model-write-guard` + `paas-operator` |
| phase-leader-brief-as-pointer-not-source | 刪（索引接管） | agent 工作流通用，README 索引即可 |
| Seeded agent_template tenant-neutral invariant（長段） | 縮 3 行 | 預防已入 code（`agent_template.py:125` constraint）+ pattern-guard `seed-default-template` class 餵 spec/pitfall |

**連動修改**：patterns/README.md「維護慣例」第 2 點（ACL class 額外補進 CLAUDE.md）→ 改為「補進 `pattern-guard-map.json` 對應 class」。單一登錄點，杜絕雙寫。

### B. Pitfalls 段（CLAUDE.md 現列 ~18 長段落）→ **建 pitfalls/README.md index，CLAUDE.md 縮成一行入口**

通則：每條長段落的敘事本體已在（或應搬進）pitfall doc；CLAUDE.md 的段落是「摘要副本」，正是漂移源（13 條新 pitfall 沒進 CLAUDE.md）。解法不是補列，是**索引化**。

逐條特別判定：

| 條目 | 判定 | 依據 |
| --- | --- | --- |
| fleet-split-registry-values-merge-drift | **從 CLAUDE.md 刪除** | 已標 ✅ 解決（2026-06-25）；doc 留存考古 |
| default-agent-template-project-bind | 縮一行＋標「已有 code guard」 | `agent_template.py:125` `_check_not_platform_default_when_bound` 已 fail-loud |
| reaped-since-single-path-stamp | 縮一行＋標「已有 code guard」 | `agent_session.py:186-207` create/write invariant 已集中強制 |
| owl-template-crash-class-syntax | 縮一行＋標「已入 repo-lint」 | W3 gate 機械化 |
| seed-noupdate-sticky-template-links | 縮一行＋標「已入 repo-lint」 | 同上 |
| operator-cf-access-delete-202 | 縮一行＋標「pattern-guard 餵」 | `paas-operator` class；mock 契約無法純靜態 lint |
| 其餘 12 條（gap chains、cross-repo、Floor env…） | 縮一行症狀指針 | debug 對照性質，撞牆時才需要全文 |
| 未列的 13 條 pitfall docs | 進 index，不進 CLAUDE.md | 索引化後自然涵蓋 |

### C. 其他減法候選（第二輪，價值較低）

| 段落 | 判定 | 依據 |
| --- | --- | --- |
| K8s Dev Sandbox 兩大段 | 縮（指 plugin SKILL.md） | 已移植 woow-dev plugin，CLAUDE.md 自述 plugin 優先 |
| deploy-stg 全流程圖表 | 縮（指 skill） | `/woow:deploy-stg` skill 為權威 |
| Release & Build 手動步驟 | 縮（指 skill） | `/woow:deploy` skill 為權威 |
| Framework integration 三套表 | 保留 | 入口性質，已是漸進載入設計 |
| Development Commands / API 表 / Code Style | 保留 | 每 session 高頻操作事實 |

**預估效果**：48.9KB → ~22-25KB（Known patterns ~7KB→0.5KB、pitfalls ~15KB→2KB、sandbox/deploy ~8KB→3KB）。

## 安全性與 rollback

- pattern-guard 與 repo-lint hook 模式全程 **fail-open**（任何內部錯誤 = 放行），且有 env 開關（`CLAUDE_PATTERN_GUARD=off`）。
- Rollback = 刪除 `.claude/settings.json` 的 hooks 段（permissions 不動）；兩個 script 檔無副作用可留。
- repo-lint 誤攔的成本 = 一次 `--update-baseline`（有意識接受）；pattern-guard 誤攔的成本 = 一次重試（marker 放行）。
- **殘餘風險**：deny 訊息對過度順從的弱模型可能被解讀為「使用者拒絕」→ 訊息內已明文「這不是使用者拒絕…重試即放行」。

## 決策點（需 eugene 核可）

1. **安裝 pattern-guard + repo-lint hooks**（寫 `pattern-guard-map.json` + `.claude/settings.json` hooks 段）——auto-mode classifier 已正確攔下「self-modification」類寫入，待明確核准。
2. W1 的 CLAUDE.md 大改（A/B 段）動的是策展知識庫，建議核過 audit 表後再執行（implementation doc Task 2-3）。
3. patterns/README.md 維護慣例改單一登錄點（連動 W1-A）。
