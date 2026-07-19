---
title: Harness 蒸餾 implementation plan（配對 design：2026-07-06-harness-distillation-design.md）
status: draft
date: 2026-07-06
owner: woow-paas-platform (harness)
reviewers: eugene
related: ["docs/plans/2026-Q3/2026-07-06-harness-distillation-design.md"]
---

# Harness 蒸餾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 CLAUDE.md 第 3 層軟約束下沉為 hook / gate 機器強制，CLAUDE.md 減量 ~50%，並建立 retro 迴路防止長回來。

**Architecture:** 四個獨立 task 對應 design doc 的 W2（hook 安裝）、W1-A（Known patterns 段）、W1-B（pitfalls 索引化）、W4（retro skill）。Task 1 需 eugene 明確核准（self-modification 類）；Task 2/3 動策展知識庫，執行前先讓 eugene 核過 design doc 的 audit 表。

**Tech Stack:** Claude Code hooks（PreToolUse/PostToolUse）、Python 3（stdlib only）、Markdown。

## Global Constraints

- 一切文字（commit message、doc、hook 訊息）用 zh-tw；commit prefix 用英文小寫（`feat:`/`fix:`/`docs:`/`chore:`）；commit message **不加** AI 產生署名（user global CLAUDE.md 規定）。
- 已存在且已驗證的檔案**不要重寫**：`.claude/hooks/pattern-guard.py`、`scripts/audit/repo-lint.py`、`scripts/audit/baselines/*`（2026-07-06 已 seed + 測試）。
- 所有 hook 必須 fail-open（內部錯誤=放行）；這是既有實作的不變式，改動時保持。
- 刪 CLAUDE.md 內容前，先確認同等內容存在於目的地 doc；CLAUDE.md 段落若含 doc 沒有的 synthesis（如「同假綠家族」cross-link 句），**先搬進 doc 再刪**。

---

### Task 1: 安裝 pattern-guard + repo-lint hooks（需 eugene 核准）

**Files:**
- Create: `.claude/hooks/pattern-guard-map.json`（全文 = scratchpad 已測版本，內容如下）
- Modify: `.claude/settings.json`（加 `hooks` 段，permissions 不動）

**Interfaces:**
- Consumes: `.claude/hooks/pattern-guard.py`（已存在）、`scripts/audit/repo-lint.py --hook`（已存在）
- Produces: 專案層 PreToolUse / PostToolUse hooks，後續 task 與所有 session 生效

- [ ] **Step 1: 寫入 map 檔**

`.claude/hooks/pattern-guard-map.json` 全文（與 2026-07-06 scratchpad 測試版一字不差；7 個 class：acl-security、controller-endpoint、model-write-guard、seed-data、seed-default-template、owl-template、paas-operator）：

```json
{
  "_readme": "pattern-guard.py 的守護路徑對照表。pattern 是對 repo 相對路徑的 regex（re.search）；同一檔案可命中多個 class（docs 取聯集、marker 各自記）。新增守線時：加一個 class 或在既有 class 的 docs append 一行即可，不用改 pattern-guard.py。",
  "classes": [
    {
      "class": "acl-security",
      "pattern": "^src/security/(ir\\.model\\.access\\.csv|ir_rules\\.xml)$",
      "why": "ACL / ir.rule 守線（B1-class cross-tenant data leak 重災區）",
      "docs": [
        "docs/decisions/patterns/README.md — 完整 pattern 索引，先在「何時必看」表定位適用條目",
        "docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md — 給 base.group_user 開 std model RWCU 時必須對稱停用 std ir.rule",
        "docs/decisions/patterns/two-layer-acl-lockstep.md — ir.rule 與 controller carve-out 任一層改動，另一層必須同步",
        "docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md — 窄→寬 scope flip 要 write() guard，ir.rule 擋不住",
        "docs/decisions/patterns/mcp-acl-6-rule-split.md — 混 platform public-read + workspace-scoped 的 model 要 6-rule split",
        "docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md — ORM test 綠 ≠ endpoint secure，須補 HttpCase cross-user negative"
      ]
    },
    {
      "class": "controller-endpoint",
      "pattern": "^src/controllers/.*\\.py$",
      "why": "controller layer 不可 .sudo() 繞 ir.rule；新增 sudo() 會被 repo-lint 白名單 gate 攔",
      "docs": [
        "docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md — sudo() 是 security primitive 不是 perf primitive；anti-pattern 三變體 A/B/C",
        "docs/decisions/patterns/two-layer-acl-lockstep.md — 多 endpoint 各自 access resolver 時任一收緊須同步",
        "docs/tracking/pitfalls/two-layer-acl-asymmetric-deny.md — 兩層 deny 不對稱的實際翻車案例",
        "docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md — backend ORM test 全綠遮蔽 controller sudo bypass 的 incident 敘事"
      ]
    },
    {
      "class": "model-write-guard",
      "pattern": "^src/models/(agent_session|agent_template|security_access|smart_home|cloud_service|mcp_server)[a-z_]*\\.py$",
      "why": "這些 model 有 write()/create() invariant guard（identity anchor / scope flip / state stamp），改壞會假綠或孤兒資源",
      "docs": [
        "docs/decisions/patterns/su-env-exempt-guard-false-green.md — invariant guard 不可用 env.su / has_group 豁免（TransactionCase 是 su env → 假綠）",
        "docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md — scope/tenancy flip 必靠 write() guard",
        "docs/decisions/patterns/invariant-recompute-on-orm-write-hook.md — cross-record invariant 收斂要放 create()/write() hook 兜底",
        "docs/tracking/pitfalls/reaped-since-single-path-stamp-gc-immunity.md — 狀態轉移 invariant 集中一處強制、不逐路徑 stamp",
        "docs/decisions/patterns/two-stage-remote-provision-idempotency.md — 多段式 remote provision 必須 stored-id guard 冪等"
      ]
    },
    {
      "class": "seed-data",
      "pattern": "^src/data/.*\\.xml$",
      "why": "seed record 的 noupdate 是 sticky 的（寫進 ir.model.data），fresh install 綠 ≠ 升級路徑綠",
      "docs": [
        "docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md — noupdate=1 sticky → 其他 data 檔對同 record 的更新被靜默 skip",
        "docs/decisions/patterns/invariant-recompute-on-orm-write-hook.md — data XML 是 ORM 寫入的第二入口，會 drift cross-record invariant"
      ]
    },
    {
      "class": "seed-default-template",
      "pattern": "^(src/data/agent_template_default\\.xml|src/hooks\\.py)$",
      "why": "agent_template_default 是跨租戶 cluster-wide fallback，必須維持 tenant-neutral（unbound + 無 user-scope 綁定）",
      "docs": [
        "docs/decisions/patterns/seed-agent-template-design.md — seeded default 的設計依據與 invariant",
        "docs/tracking/pitfalls/default-agent-template-project-bind-silent-autoenable.md — default 被綁 project → auto-enable 靜默全失效；demo 與 default 分工勿互換",
        "CLAUDE.md「Seeded 平台預設 agent_template 的 tenant-neutral invariant」條 — post_init 未設才指、不覆寫 admin 已選"
      ]
    },
    {
      "class": "owl-template",
      "pattern": "^src/static/src/.*\\.xml$",
      "why": "OWL template 語法錯誤會過 XML parse + 全部 Python test，runtime 直接全站白屏；唯一攔截網是 headed 實走 + repo-lint 靜態檢查",
      "docs": [
        "docs/tracking/pitfalls/owl-template-crash-class-syntax.md — component 上不可 t-att-（prop 直接吃 expression）；無 .enter/.esc modifier，按鍵判斷在 handler 內讀 ev.key"
      ]
    },
    {
      "class": "paas-operator",
      "pattern": "^extra/paas-operator/",
      "why": "operator 包外部 API：mock 狀態碼要對齊真實回值；無 CI runner，merge 前必須本地跑 integration tests",
      "docs": [
        "docs/tracking/pitfalls/operator-cf-access-delete-202-mock-contract.md — CF Access DELETE 回 202；mock 寫死 200 → live 才爆",
        "docs/decisions/patterns/two-stage-remote-provision-idempotency.md — 先建遠端資源再跑後續的 method 必須 stored-id guard",
        "extra/paas-operator/tests/integration/README.md — 本地 integration tests 跑法（HELM_BINARY=$(which helm) pytest -m integration）"
      ]
    }
  ]
}
```

- [ ] **Step 2: 合併 hooks 段進 `.claude/settings.json`**（保留既有 permissions，只新增 `hooks` key）

```json
{
  "permissions": {
    "allow": ["…既有 26 條原樣保留…"]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/pattern-guard.py\"",
            "timeout": 10,
            "statusMessage": "pattern-guard：檢查守護路徑"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/scripts/audit/repo-lint.py\" --hook",
            "timeout": 15,
            "statusMessage": "repo-lint：假綠 gate 檢查"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 3: 驗證 JSON 結構**

Run: `jq -e '.hooks.PreToolUse[0].hooks[0].command, .hooks.PostToolUse[0].hooks[0].command, (.permissions.allow | length)' .claude/settings.json`
Expected: 印出兩條 command 字串 + `22`（permissions 完整保留）。

- [ ] **Step 4: live-fire 驗證（新 session 或 `/hooks` 重載後）**

對 `src/controllers/reconcile_report.py` 加一行註解做 Edit：預期第一次被 pattern-guard deny（訊息含 controller-endpoint docs 清單）、重試放行、PostToolUse repo-lint 通過（sudo 數未變）。驗完 revert 該註解。若 hook 未觸發：settings watcher 未重載，請 eugene 開一次 `/hooks` 或重啟 session。

- [ ] **Step 5: Commit**

```bash
git add .claude/hooks/ .claude/settings.json scripts/audit/
git commit -m "feat: harness pattern-guard hook + repo-lint 假綠 gates（read-first 守衛機器強制化）"
```

---

### Task 2: CLAUDE.md Known patterns 段換成指針（先核過 design doc audit 表 A/C 段）

**Files:**
- Modify: `CLAUDE.md`（「## Known patterns」段整段替換）
- Modify: `docs/decisions/patterns/README.md`（「維護慣例」第 2 點）

**Interfaces:**
- Consumes: Task 1 的 hook 已安裝（替換文字引用 pattern-guard）
- Produces: CLAUDE.md 的單行入口；後續新 pattern 的唯一登錄點為 README + map

- [ ] **Step 1: 替換 CLAUDE.md「## Known patterns」段**（從 `## Known patterns` 標題起、到 `Pitfall 持久化紀錄` 前一行止）為：

```markdown
## Known patterns

改 model / ACL / lifecycle / seed / OWL template 前的守線知識採兩層供給，不雙寫在本檔：

1. **pattern-guard hook（機器強制）**：編輯守護路徑時 hook 自動列出必讀 docs，讀完重試即放行；對照表 `.claude/hooks/pattern-guard-map.json`（新增守線在此 append）。
2. **完整索引（人工查閱）**：[`docs/decisions/patterns/README.md`](docs/decisions/patterns/README.md)「何時必看」表。

唯二留本檔的高頻 invariant：

- **CloudService / Project decoupling**：7 條 Invariants 見 `docs/decisions/refactor/cloud-service-project-decouple.md`。
- **Seeded `agent_template_default` 必須 tenant-neutral**（unbound + 無 user-scope 綁定；經 sudo 廣播為全租戶 fallback）。綁定當下有 code guard fail-loud（`src/models/agent_template.py:125`）；設計依據 `docs/decisions/patterns/seed-agent-template-design.md`；與 `agent_template_demo`（draft、可綁 project 測試）分工勿互換。

可機械判定的守線改進 `scripts/audit/repo-lint.py`（gate 比散文強）。
```

- [ ] **Step 2: 改 patterns/README.md 維護慣例第 2 點**：把「額外把它補進 CLAUDE.md『Known patterns』清單」改為「若綁特定路徑，額外在 `.claude/hooks/pattern-guard-map.json` 對應 class 的 docs append 一行（或新增 class）」。

- [ ] **Step 3: 驗證**

Run: `wc -c CLAUDE.md && grep -c "pattern-guard-map" CLAUDE.md docs/decisions/patterns/README.md`
Expected: CLAUDE.md 減 ~7KB；兩檔各至少 1 處引用 map。

- [ ] **Step 4: Commit**：`docs: CLAUDE.md Known patterns 段指針化（守線交 pattern-guard hook + README 索引）`

---

### Task 3: pitfalls 索引化（建 README index + CLAUDE.md 段縮減）

**Files:**
- Create: `docs/tracking/pitfalls/README.md`
- Modify: `CLAUDE.md`（「Pitfall 持久化紀錄」清單段替換）
- Modify（可能）: 個別 pitfall doc（承接 CLAUDE.md 獨有 synthesis 句）

- [ ] **Step 1: 建索引**。對 `docs/tracking/pitfalls/*.md` 每檔產一行 `| [slug](file.md) | 一行症狀（從該檔標題/首段抽） | 機械化狀態 |`。機械化狀態欄位值：`repo-lint gate` / `code guard（file:line）` / `pattern-guard 餵` / `—`。已知三筆範例（照此格式做完全部 31 檔）：

```markdown
| [owl-template-crash-class-syntax](owl-template-crash-class-syntax.md) | OWL template 語法錯過三層測試、runtime 全站白屏 | repo-lint gate |
| [reaped-since-single-path-stamp-gc-immunity](reaped-since-single-path-stamp-gc-immunity.md) | gating 欄位單路徑 stamp → 他路徑到達者永久免疫 GC | code guard（agent_session.py:186-207） |
| [cross-repo-artifact-landing-assumption](cross-repo-artifact-landing-assumption.md) | plan 假設 artifact 落 repo X 實際落 Y → impl 前勘查實體落點 | — |
```

- [ ] **Step 2: 逐條搬運檢查**。對 CLAUDE.md 現有 18 個 pitfall 段落：diff 該段 vs 對應 doc，段落含 doc 沒有的句子（cross-link、後續更正、「同家族」歸類）→ 先 append 進 doc 再縮。特別：`fleet-split-registry-values-merge-drift` 的 ✅ 解決敘事整段搬進 doc 後從 CLAUDE.md 刪除。

- [ ] **Step 3: 替換 CLAUDE.md pitfalls 段**為：

```markdown
Pitfall 持久化紀錄（debug 撞牆時對照）：完整索引見 [`docs/tracking/pitfalls/README.md`](docs/tracking/pitfalls/README.md)（一行症狀 + 機械化狀態）。其中已機械化者撞到 gate / guard 時訊息內附 doc 指針，不需預先記憶。
```

- [ ] **Step 4: 驗證**：`ls docs/tracking/pitfalls/*.md | grep -v README | wc -l` 與 index 表列數相等；lychee（CI）連結不紅；`wc -c CLAUDE.md` 累計降至 ~25KB 內。

- [ ] **Step 5: Commit**：`docs: pitfalls 索引化——CLAUDE.md 摘要副本收斂為單一 README index`

---

### Task 4: 建 `/retro` project skill（retro 迴路）

**Files:**
- Create: `.claude/skills/retro/SKILL.md`

- [ ] **Step 1: 寫入 SKILL.md 全文**：

```markdown
---
name: retro
description: Session 收尾的教訓蒸餾。當 debug / incident / e2e 排錯 session 結束，或使用者說「/retro」「收尾」「這次學到什麼」時使用。掃描本次對話的翻車點，判定是否持久化，並選最強落點（gate > hook map > pattern doc > pitfall doc）。
---

# Retro：教訓蒸餾

## Steps

1. **掃描本 session**：根因是什麼？哪一刻「測試綠但實際壞」（假綠）？哪個前提假設被推翻？哪次 debug 繞了遠路、缺什麼指引本可直達？
2. **分類**：一次性事件（不持久化，口頭總結即可）vs 會重複的 class。判準：同 class 可能再發生、且下次撞到的 debug 成本 > 30 分鐘。
3. **選落點（按強度優先，選最強可行者）**：
   - a. 可機械判定（grep / parse 可攔）→ `scripts/audit/repo-lint.py` 加 check + `--update-baseline`
   - b. 綁特定路徑的 read-first 知識 → `.claude/hooks/pattern-guard-map.json` append docs 行或新增 class
   - c. 跨情境判斷類 → `/docs:new decisions <slug>`（pattern doc）+ 登錄 patterns/README.md 索引
   - d. 敘事對照類 → `/docs:new tracking <slug>`（pitfall doc）+ 登錄 pitfalls/README.md 索引
4. **產出 diff 提案**給使用者核可後落檔；一個教訓可同時落 a+c（gate 攔 + doc 解釋為什麼）。
5. **反向檢查**：這次的教訓是否讓某條既有散文守線變多餘（已被 code guard / gate 取代）？是 → 提案把該條從 CLAUDE.md / index 降級或刪除。CLAUDE.md 不因 retro 而變長是本 skill 的硬約束。

## 紅線

- 不把已機械化的教訓再寫成散文提醒。
- 不新增「要注意 X」式無判準條目——寫不出觸發條件（何時、哪個檔、什麼症狀）的教訓還不成熟，留在 session 總結即可。
```

- [ ] **Step 2: 驗證**：新 session 輸入 `/retro`，skill 觸發且列出 5 steps。
- [ ] **Step 3: Commit**：`feat: /retro skill——session 教訓蒸餾與落點分流（gate > hook map > docs）`

---

### Task 5（可選）: repo-lint 進 CI

**Files:**
- Create: `.gitea/workflows/repo-lint.yml`

- [ ] **Step 1: 寫 workflow**（act-runner 需有 python3；參照 `ci-workflow-container-missing-node.md` pitfall，不用 container image、直接 host runner）：

```yaml
name: repo-lint
on:
  pull_request:
    paths: ["src/**"]
jobs:
  repo-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 scripts/audit/repo-lint.py --all
```

- [ ] **Step 2: 驗證**：開測試 PR 動 `src/` 任一檔，CI 綠；塞一個 `t-att-` 違規 push，CI 紅。驗完 revert。
- [ ] **Step 3: Commit**：`chore: repo-lint 假綠 gates 進 CI（PR 觸發）`

---

## Self-Review 紀錄（2026-07-06）

- Spec coverage：design doc W1→Task 2/3、W2→Task 1、W3→Task 1（wiring）+ Task 5（CI）、W4→Task 4 ✓
- Placeholder scan：Task 3 Step 1 的 31 行索引由「定義好的抽取程序 + 3 個完整範例」產生（來源=各 doc 本體），非 TBD ✓
- 一致性：map JSON 與 scratchpad 測試版一字不差；settings.json 的兩條 command 與已驗證的 pipe-test 呼叫一致 ✓
