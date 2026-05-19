---
allowed-tools: Read, Write, Edit, AskUserQuestion, Bash, Glob
---

# /docs:promote — 把 ccpm / superpowers 探索升級到主結構

ccpm epic 完成、或 superpowers brainstorming/writing-plans 跑完之後，把其中**長期有價值**的決策、pitfall、follow-up、可複用 pattern 從工具自管理區搬到 `docs/` 主結構，並建立雙向 cross-link。

## Usage

```
/docs:promote ccpm:<feature-name>
/docs:promote superpowers:<spec-or-plan-filename>
```

`<feature-name>` 是 ccpm 流程的 feature slug，**PRD / Epic / Tasks 三者共用同一個 name**（如 `wallet-billing`、`odoo-service-config-enhancement`），對應到：
- `.claude/prds/<feature-name>.md`（PRD — 由 plan skill 產出）
- `.claude/epics/<feature-name>/epic.md`（Epic — parse-prd 後）
- `.claude/epics/<feature-name>/<N>.md`（Tasks — decompose 後）
- `.claude/epics/<feature-name>/updates/<N>/`（執行進度）

例：

```
/docs:promote ccpm:wallet-billing
/docs:promote ccpm:odoo-service-config-enhancement
/docs:promote superpowers:2026-05-16-ai-chat-mermaid-rendering-design.md
```

## Instructions

### Step 0 — 驗證輸入

1. 解析 source 類型：
   - `ccpm:<feature-name>`：檢查 `.claude/prds/<name>.md` **或** `.claude/epics/<name>/epic.md`（含 `.claude/epics/archived/<name>/`）至少其一存在。
   - `superpowers:<file>`：檢查 `docs/superpowers/specs/<file>` 或 `docs/superpowers/plans/<file>` 存在。
2. 全部不存在 → 用 `AskUserQuestion` 請使用者確認名稱或路徑。
3. **記錄 datetime**：
   ```bash
   NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
   ```

### Step 1 — 讀內容並識別 promote 候選

**ccpm 模式**（讀完整 ccpm tree，PRD → Epic → Tasks → updates）：

- 讀 `.claude/prds/<name>.md`（如存在）
- 讀 `.claude/epics/<name>/epic.md`（如存在）
- 讀所有 `.claude/epics/<name>/<N>.md` task files
- 掃 `.claude/epics/<name>/updates/<N>/` 內 `progress.md` / `stream-*.md`（含 archived/）
- 識別段落：
  - **PRD 來源**：
    - "User Stories" + Acceptance Criteria → 候選去 `docs/reference/features/`
    - "Functional Requirements" / "Non-Functional Requirements" → 同上
    - "Success Criteria"（可量測指標）→ `docs/reference/features/` 或 `docs/decisions/philosophy/`
  - **Epic 來源**：
    - "Architecture Decisions" / "Trade-offs" / "Technical Approach" → `docs/decisions/refactor/` 或 `decisions/philosophy/`
    - 可複用 pattern → `docs/decisions/patterns/`
  - **Tasks / updates 來源**：
    - "Pitfalls" / "Incidents" / "What went wrong" / "Bug" → `docs/tracking/pitfalls/`
    - "Follow-up" / "Tech debt" / "TODO" / "Caveat" → append 到 `docs/tracking/followups/follow-ups.md`
    - 實作中發現的 pattern → `docs/decisions/patterns/`

**superpowers 模式**：
- 讀 `docs/superpowers/specs/<file>` 或 `docs/superpowers/plans/<file>`
- 識別段落：
  - 完整 design+impl plan（writing-plans 輸出）→ 候選去 `docs/plans/<quarter>/` 雙檔
  - 含 trade-off 論述（brainstorming options）→ 候選去 `docs/decisions/`
  - 含 incident / bug 記錄 → 候選去 `docs/tracking/pitfalls/`

### Step 2 — 互動式確認

對每個 promote 候選用 `AskUserQuestion`：

- **是否 promote？**（是 / 略過 / 修改後 promote）
- **目標子目錄？**（從上面候選清單，可選其他）
- **檔名？**（pre-fill: source name 衍生，使用者可改）
- **是否合併**（如果目標檔已存在，問是合併還是另建）

### Step 3 — 產出新檔 / append

新建檔的 frontmatter：

```yaml
---
title: <段落標題或 user 指定>
category: decisions | plans | tracking
status: active
created: <NOW>
updated: <NOW>
source: <原 source 路徑>
promoted-at: <NOW>
related: [<list>]
---
```

`follow-ups.md` 模式：append 一個 H2 段落，格式：

```markdown
## <title>（<YYYY-MM-DD>）

- **Source**: <原 source 路徑>
- **Owner**: <如有>
- **Status**: open
- **Context**: <一段說明>
```

雙檔 plan 模式（superpowers writing-plans 輸出）：產出 `<quarter>/<YYYY-MM-DD>-<slug>-design.md` + `-implementation.md`，分別把 design / implementation 段落寫入。

### Step 4 — 在原檔加 promote 註記

**ccpm 模式**（不動 schema 欄位）：在 epic.md 或對應 task 的**內文最後**加：

```markdown
---

> **Promoted to**: `docs/<path>` (<NOW>)
```

**superpowers 模式**：在原檔頂端（H1 之後、frontmatter 之外）加：

```markdown
> **Promoted to**: `docs/<path>` (<NOW>)
```

### Step 5 — 回報

```
✅ Promoted <source> to docs/

  decisions/<dir>/<file>.md
  tracking/pitfalls/<file>.md
  plans/<quarter>/<file>-design.md + -implementation.md
  ...

  Cross-link 已建立：
  - 新檔 frontmatter: source: <original>
  - 原檔內文: > Promoted to: docs/<path>

下一步：人工檢查新檔內容、調整 title / related 欄位、必要時把 status 從 draft → active。
```

## Notes

- **不刪原檔**：保留探索脈絡（ccpm epic 內文 / superpowers 原 spec）
- **不動 ccpm schema 欄位**：epic.md / task.md 的 frontmatter 維持 ccpm v2 規範，promote 註記寫在內文末
- **跨 promote 不重複**：如果發現原檔已有 `> Promoted to:`，用 `AskUserQuestion` 確認是否要再 promote 一份
- **多次 promote**：同一 source 可以多次 promote 到不同類別（如某 epic 同時產出 decision 與 pitfall）
- **失敗回滾**：如果中途中斷，已產出的 docs/ 新檔保留，原檔註記可能未完成 — 重跑時用 `AskUserQuestion` 確認是否覆蓋
