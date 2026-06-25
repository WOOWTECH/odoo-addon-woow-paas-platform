---
allowed-tools: Skill, Read, Write, Edit, Bash, LS, AskUserQuestion
---

# /brainstorm-sc-to-ccpm — Discover with /sc:brainstorm, ship into ccpm

把 `/sc:brainstorm` 多 persona 探索 + ccpm PRD/Epic schema 塑形 銜接起來。Brainstorm 適合開放式發掘，但輸出不符合 ccpm 的 frontmatter / section schema，這個 command 做塑形 + 銜接。

## Usage

```
/brainstorm-sc-to-ccpm <kebab-case-feature-name>
```

例：`/brainstorm-sc-to-ccpm ai-chat-mermaid-rendering`

未給 feature name → 透過 `AskUserQuestion` 詢問。

## Instructions

### Step 0 — 驗證輸入 & 準備目錄

1. **Feature name**：
   - 必須 kebab-case（小寫字母起頭、可含數字與連字號）
   - 不合規 → 用 `AskUserQuestion` 請使用者重給
2. **衝突檢查**：
   - `.claude/prds/<name>.md` 已存在 → 用 `AskUserQuestion` 確認是否覆蓋
   - `.claude/prds/_drafts/<name>-brainstorm.md` 已存在 → 同上
3. **建目錄**（不存在才建）：
   ```bash
   mkdir -p .claude/prds/_drafts .claude/prds/_design-notes
   ```
4. **記錄 datetime**：
   ```bash
   NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
   ```

### Step 1 — 啟動 /sc:brainstorm 探索

用 `Skill` 工具呼叫 `sc:brainstorm`，topic 用 feature name + 使用者後續補充。

**對話過程注意**：
- 鼓勵多 persona（architect、security、frontend、backend、devops）對話
- 主動驅動把 Out of Scope 問清楚（brainstorm 預設常漏）
- 對每條 user story 主動問 acceptance criteria（brainstorm 預設可能不到 ccpm 要的粒度）
- **絕對不允許** brainstorm 直接寫到 `.claude/prds/<name>.md`（會破壞 ccpm schema）

**收尾時**將整段對話結論（保留 trade-off、rejected options、persona 觀點）存到：

```
.claude/prds/_drafts/<name>-brainstorm.md
```

frontmatter：

```yaml
---
name: <name>
created: <NOW>
source: sc:brainstorm
status: raw
---
```

### Step 2 — 塑形成 ccpm PRD

讀 `_drafts/<name>-brainstorm.md`，產出 `.claude/prds/<name>.md`，**必須完全符合 ccpm schema**：

frontmatter：

```yaml
---
name: <name>
description: <一行摘要，<= 100 字>
status: backlog
created: <NOW>
---
```

H1：`# PRD: <name>`

九個 section（缺一不可）：

1. `## Executive Summary`
2. `## Problem Statement`
3. `## User Stories` — **每條都帶 acceptance criteria**
4. `## Functional Requirements`
5. `## Non-Functional Requirements`
6. `## Success Criteria` — **必須可量測（具體數值 / 比例 / 時間）**
7. `## Constraints & Assumptions`
8. `## Out of Scope` — **明確列項，至少 3 條；brainstorm 漏的要主動補**
9. `## Dependencies`

**Quality gates（未過要回頭問使用者補，不可放水）：**

- 每個 section 都有實質內容（無 `TBD` / `TODO` / placeholder）
- User stories 全數帶 acceptance criteria
- Success criteria 每條都可量測
- Out of Scope ≥ 3 條
- description 一行可摘要本 PRD

### Step 3 — 抽 Design Notes（條件式）

掃 `_drafts/<name>-brainstorm.md` 找架構選項討論。**只有當 brainstorm 內含 ≥ 2 個 architecture options（或對立 trade-off）時才產出**；否則跳過這 step。

產出 `.claude/prds/_design-notes/<name>.md`：

```yaml
---
name: <name>
created: <NOW>
source: sc:brainstorm
linked-prd: .claude/prds/<name>.md
---

# Design Notes: <name>

## Options Considered

### Option A: <name>
- Approach: ...
- Pros: ...
- Cons: ...
- Persona: <提這方案的 persona>

### Option B: <name>
...

## Selected Approach

<決定哪個 + 一段 rationale>

## Rejected & Why

- Option B → <reason>
- Option C → <reason>

## Cross-cutting Concerns

<security / devops / perf persona 提的注意事項，這些之後要進 epic Architecture Decisions>
```

### Step 4 — 回報 + 詢問 hand off

呈現結果：

```
✅ Brainstorm draft: .claude/prds/_drafts/<name>-brainstorm.md
✅ PRD (ccpm-compliant): .claude/prds/<name>.md
{若有產出} ✅ Design notes: .claude/prds/_design-notes/<name>.md
```

用 `AskUserQuestion` 問下一步：

- **接 ccpm parse to epic**（會把 design-notes 合進 epic 的 Architecture Decisions）
- **先停在這**，之後手動 trigger ccpm
- **修 PRD 哪段** — 用 Edit 直接改

### Step 5 — (若選 hand off) 觸發 ccpm

用 `Skill` 工具呼叫 `ccpm`，prompt 明確說：

```
parse the <name> PRD into an epic.
若 .claude/prds/_design-notes/<name>.md 存在，
把其中 Selected Approach / Rejected & Why / Cross-cutting Concerns
合進 epic 的 Architecture Decisions 與 Technical Approach section。
```

之後回到 ccpm 標準流：使用者可以接 `decompose the <name> epic` → `sync the <name> epic` → ...

---

## Notes

- `_drafts/` 與 `_design-notes/` **進 git**（保留 rejected options 與 persona 觀點，未來回查或多 agent 同步看得到）
- `_` 開頭目錄不會被 ccpm 的 `prd-list.sh` 抓（用 `*.md` glob 不會比對到 `_drafts/`，且 ccpm 對隱藏/底線目錄無遞迴）
- 中斷或失敗時，已寫的 `_drafts/<name>-brainstorm.md` 不要刪 — 下次重跑可從 Step 2 起跳
- 若 brainstorm 過程 user 改變了 feature 命名 → 在 Step 2 開始前確認最終 name，三個檔保持一致
