---
allowed-tools: AskUserQuestion, Read, Write, Bash, LS
---

# /docs:new — 建立新 doc 並套用標準 frontmatter

把「建檔」這件事固定化，避免每次手動填 frontmatter / 想路徑 / 漏分類。

## Usage

```
/docs:new <category> <name>
```

- `category`：`guides` | `reference` | `decisions` | `plans` | `tracking`
  - `archived/` 不可手動建檔（從其他類移入）
  - `superpowers/` 是已退役 obra/superpowers plugin 的舊產出區，不手動建也不再有新產出
- `name`：kebab-case slug（小寫字母起頭、可含數字與連字號）

例：

```
/docs:new guides k8s-secret-rotation
/docs:new decisions cloud-service-namespace-strategy
/docs:new tracking sandbox-dns-flap
```

## Instructions

### Step 0 — 驗證輸入

1. **category**：必須是 5 個合法類別之一。否則用 `AskUserQuestion` 重問。
2. **name**：必須是 kebab-case。不合規用 `AskUserQuestion` 請使用者重給。
3. **衝突檢查**：對應檔已存在 → 用 `AskUserQuestion` 確認覆蓋或改名。

### Step 1 — 互動式問子目錄

每個 category 有不同子目錄選擇。用 `AskUserQuestion` 詢問：

| category | 子目錄選項 |
|----------|----------|
| `guides` | `getting-started`（root）/ `development` / `deployment` / `testing` / `tutorials` |
| `reference` | `api` / `features` / `architecture` / `pencil` |
| `decisions` | `philosophy` / `refactor` / `patterns` / `reviews` |
| `plans` | `<current quarter>`（如 `2026-Q2`，從 `date` 推算）/ `journal` |
| `tracking` | `issues` / `pitfalls` / `followups`（會 append 到 `follow-ups.md`，不是建新檔） / `test-reports` |

特殊規則：

- `plans/<quarter>/`：問是否雙檔模式（design + implementation 兩檔）。是 → 產出兩個檔，檔名前加 `YYYY-MM-DD-`（用 `date -u +"%Y-%m-%d"`）。
- `tracking/followups`：不建新檔，而是 append 一個 H2 段落到 `docs/tracking/followups/follow-ups.md`。

### Step 2 — 互動式問 frontmatter

用 `AskUserQuestion` 問：

- `title`：human readable 標題（pre-fill 從 name 衍生）
- `related`：是否要連結到其他 doc？（可多選 / 略過）

### Step 3 — 產出檔案

路徑：`docs/<category>/<sub>/<name>.md`（雙檔模式：`<name>-design.md` + `<name>-implementation.md`）

frontmatter：

```yaml
---
title: <user input>
category: <category>
status: draft
created: <UTC date YYYY-MM-DDTHH:MM:SSZ>
updated: <same as created>
related: [<list from step 2>]
---
```

模板內容：

```markdown
# <title>

## 目的

<說明這份 doc 要解決什麼問題、給誰看>

## 內容

<主體>

## 相關

<連結到其他 doc / decision / plan / spec>
```

雙檔模式 `-implementation.md` 額外加：

```markdown
## Phase 列表

- [ ] Phase 1：...
- [ ] Phase 2：...

## 驗證

- [ ] ...
```

### Step 4 — 回報

```
✅ Created docs/<path>
   category: <category>
   status: draft
   下一步：填內容後，把 status 從 draft 改為 active。
```

雙檔模式回報兩個檔案路徑。

`tracking/followups` 模式回報：

```
✅ Appended new follow-up to docs/tracking/followups/follow-ups.md
   標題：<title>
   日期：<date>
```

## Notes

- **不更新 INDEX**：本 repo 暫不維護 `docs/INDEX.md`（Phase 2 才加 CI 自動產生）
- **frontmatter `updated` 維護**：由作者後續編輯時手動更新，本指令不重複觸發
- **與 superpowers 的關係**：superpowers 已退役（2026-09-03）。歷史產出在 `docs/superpowers/`，要撈舊的走 `/docs:promote`；新文件一律用本指令。
