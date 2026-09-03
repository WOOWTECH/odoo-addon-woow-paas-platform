---
allowed-tools: Read, Write, Edit, AskUserQuestion, Bash, Glob
---

# /docs:promote — 把 superpowers 探索升級到主結構

> **2026-09-03**：superpowers plugin 已退役，`docs/superpowers/` 不再有新產出；本指令只剩清舊產出用（目前 0 份待 promote）。新文件一律 `/docs:new`。

superpowers brainstorming/writing-plans 跑完之後，把其中**長期有價值**的決策、pitfall、follow-up、可複用 pattern 從工具自管理區搬到 `docs/` 主結構，並建立雙向 cross-link。

> **2026-08-07（#631）**：原本還有 `ccpm:<feature-name>` 分支（從 `.claude/prds/` / `.claude/epics/` promote）。
> ccpm 已退役，該分支移除；歷史產出見 [`docs/archived/ccpm-v2/`](../../../docs/archived/ccpm-v2/README.md)，
> 那批檔案已在 `docs/` 內，不需要也不應該再 promote。

## Usage

```
/docs:promote superpowers:<spec-or-plan-filename>
```

例：

```
/docs:promote superpowers:2026-05-16-ai-chat-mermaid-rendering-design.md
```

## Instructions

### Step 0 — 驗證輸入

1. 解析 source：`superpowers:<file>` — 檢查 `docs/superpowers/specs/<file>` 或 `docs/superpowers/plans/<file>` 存在。
2. 不存在 → 用 `AskUserQuestion` 請使用者確認名稱或路徑。
3. **記錄 datetime**：
   ```bash
   NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
   ```

### Step 1 — 讀內容並識別 promote 候選

- 讀 `docs/superpowers/specs/<file>` 或 `docs/superpowers/plans/<file>`
- 識別段落：
  - 完整 design+impl plan（writing-plans 輸出）→ 候選去 `docs/plans/<quarter>/` 雙檔
  - 含 trade-off 論述（brainstorming options）→ 候選去 `docs/decisions/`
  - 可複用 pattern → `docs/decisions/patterns/`
  - 含 incident / bug 記錄 → 候選去 `docs/tracking/pitfalls/`
  - "Follow-up" / "Tech debt" / "TODO" / "Caveat" → append 到 `docs/tracking/followups/follow-ups.md`

### Step 2 — 互動式確認

對每個 promote 候選用 `AskUserQuestion`：

- **是否 promote？**（是 / 略過 / 修改後 promote）
- **目標子目錄？**（從上面候選清單，可選其他）
- **檔名？**（pre-fill: source name 衍生，使用者可改）
- **是否合併**（如果目標檔已存在，問是合併還是另建）

### Step 3 — 產出新檔 / append

**3.1 預設 frontmatter（適用 `decisions/` 與 `reference/features/`、`plans/`）**：

```yaml
---
title: <段落標題或 user 指定>
category: decisions | plans | reference
status: active
created: <NOW>
updated: <NOW>
source: <原 source 路徑>
promoted-at: <NOW>
related: [<list>]
---
```

**3.2 `tracking/pitfalls/` 模式 — 對齊既有手寫格式（無 frontmatter）**：

`docs/tracking/pitfalls/` 內既有檔案都是「無 frontmatter + H1 + 摘要 quote + 多個 H2 section」結構，CLAUDE.md `Known patterns` 直接引用。promote 出來的檔遵循同一格式：

```markdown
# Pitfall: <title>

> Source: <原 source 路徑> (promoted <YYYY-MM-DD>)
>
> <一句話 summary：什麼情境會踩到 + 後果，1-2 行>

## Symptom

<觀察到的現象，可含 reproducer code block>

## Root Cause

<根因分析>

## Fix

<修法 — 含 PR / commit ref 如有>

## Prevention

<未來怎麼避免、檢查清單 / pattern doc 連結>
```

如 source 含 incident timeline，可額外加 `## Timeline` 段落（既有 `cross-workspace-task-data-leak.md` 有）。

**3.3 `tracking/followups/follow-ups.md` 模式 — 對齊既有 F# 流水號**：

`follow-ups.md` 採跨 refactor cycle 連續編號（F1, F2, ...）+ 頂部 Index 表格。promote 工具**不自動編號**（編號由 reporter 後續整理時統一指派，避免 race condition）。append 格式：

```markdown
## <title>（<YYYY-MM-DD>）— pending F#

- **Source**: <原 source 路徑>
- **Severity**: <minor | medium | major | info | trivial>
- **Origin**: <pre-existing | refactor | refactor (test) | refactor (deferred) | refactor (caveat)>
- **Cycle**: <如 source 提及，未明標就空白>
- **Status**: open
- **Action**: <建議動作 — 1 行>

<段落說明：何時觸發、為什麼延後、相關連結>

> **Reporter TODO**: 指派 F# 編號（既有最大 + 1）並加入頂部 Index 表格。
```

附加位置：在 `follow-ups.md` 的**最末**（不要試圖插進現有 cycle section，避免 reporter 整理時順序混亂）。

**3.4 雙檔 plan 模式（superpowers writing-plans 輸出）**：產出 `<quarter>/<YYYY-MM-DD>-<slug>-design.md` + `-implementation.md`，分別把 design / implementation 段落寫入。

### Step 4 — 在原檔加 promote 註記

在原檔頂端（H1 之後、frontmatter 之外）加：

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

- **原檔處置**：superpowers promote＝`git mv` 搬入主結構（原檔不留副本，2026-07-19 起），新檔 frontmatter `source:` 記原路徑；inbound 引用同步改寫。詳見 `docs/README.md` 的 Cross-link 規則。
- **跨 promote 不重複**：如果發現原檔已有 `> Promoted to:`，用 `AskUserQuestion` 確認是否要再 promote 一份
- **多次 promote**：同一 source 可以多次 promote 到不同類別（如某份 spec 同時產出 decision 與 pitfall）
- **失敗回滾**：如果中途中斷，已產出的 docs/ 新檔保留，原檔註記可能未完成 — 重跑時用 `AskUserQuestion` 確認是否覆蓋
