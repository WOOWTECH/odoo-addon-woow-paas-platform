---
title: SuperClaude_Framework (SuperClaude-Org)
category: guides
status: active
created: 2026-05-19
updated: 2026-05-19
---

# SuperClaude_Framework — `/sc:*` Commands

GitHub: <https://github.com/SuperClaude-Org/SuperClaude_Framework>

User-scope slash commands 集合（30+），覆蓋 plan / design / build / test / PM / research / document 等領域，安裝在 `~/.claude/commands/sc/`（**跨專案共享**）。本 repo **選擇性啟用**：只用 `/sc:brainstorm`（透過 `/brainstorm-sc-to-ccpm` 包裝），其他 ad hoc。

## 安裝

依官方 README 把 `commands/sc/` 整個搬到 `~/.claude/commands/sc/`（user scope，不放 project 內）。

## 完整 commands 列表

| Command | 用途 |
|---------|------|
| `/sc:brainstorm` | 多 persona 互動式需求探索 |
| `/sc:design` | 系統 / API / component 設計 |
| `/sc:workflow` | 從 PRD 產 implementation workflow（寫 `claudedocs/`） |
| `/sc:research` | 深度 web research（寫 `claudedocs/`） |
| `/sc:build` | build / compile / package |
| `/sc:test` | 跑 test + coverage 報告 |
| `/sc:analyze` | code quality / security / performance / arch 分析 |
| `/sc:explain` | 解釋 code / concept |
| `/sc:document` | 為 component / API 產文件 |
| `/sc:improve` | 系統化品質改善 |
| `/sc:troubleshoot` | 診斷錯誤 |
| `/sc:cleanup` | 清死 code + 結構優化 |
| `/sc:estimate` | 任務 / feature 工時估算 |
| `/sc:implement` | 跟 persona 跑實作 |
| `/sc:index-repo` | repo 索引（94% token 縮減） |
| `/sc:index` | 產 project 文件 / knowledge base |
| `/sc:load` / `/sc:save` | session lifecycle（serena MCP） |
| `/sc:pm` | PDCA-style PM agent |
| `/sc:reflect` | task reflection / validation |
| `/sc:select-tool` | MCP tool 選擇 |
| `/sc:spawn` | meta-system task 編排 |
| `/sc:spec-panel` | 多專家 spec review |
| `/sc:task` | 複雜 task 工作流管理 |
| `/sc:agent` | agent 委派 |
| `/sc:git` | git operations + commit msg |
| `/sc:business-panel` | business analysis |
| `/sc:recommend` | command 推薦引擎 |
| `/sc:help` | command 列表 |

## 本 repo 的使用範圍

| Command | 啟用？ | 說明 |
|---------|--------|------|
| `/sc:brainstorm` | ✅ 經 `/brainstorm-sc-to-ccpm` 包裝 | 唯一進入 ccpm tree 的入口；多 persona 對話探索 |
| `/sc:workflow`、`/sc:research` | ⚠️ ad hoc | 寫 `claudedocs/`（**git-ignored**），不污染版控 |
| `/sc:analyze`、`/sc:explain`、`/sc:document`、`/sc:design`、`/sc:troubleshoot` | ⚠️ ad hoc | 純對話 / 偶爾用，不固定流程 |
| `/sc:test`、`/sc:build`、`/sc:cleanup`、`/sc:improve` | ⚠️ ad hoc | 看需求 |
| `/sc:index-repo` | ⚠️ 可選 | 大 repo 探索時 token 經濟 |
| **`/sc:pm`** | ❌ **不啟用** | 會寫 `docs/{temp,patterns,pdca,mistakes,checklists}/` — **跟我們 6 類結構與 `/docs:promote` 工作流衝突**（見下） |
| 其他 | ad hoc | — |

## 為什麼不啟用 `/sc:pm`

`/sc:pm` 預設行為（從 `~/.claude/commands/sc/pm.md` line 60-90）：

```
Create docs/temp/hypothesis-YYYY-MM-DD.md
Update docs/temp/experiment-YYYY-MM-DD.md
Update docs/temp/lessons-YYYY-MM-DD.md
Success → docs/patterns/[pattern-name].md
Failure → docs/mistakes/mistake-YYYY-MM-DD.md
Move docs/temp/ → docs/patterns/ or docs/mistakes/
Create docs/pdca/[feature]/...
docs/checklists/...
```

啟用的話：

1. 會建 5 個一級目錄（`temp/`、`patterns/`、`mistakes/`、`pdca/`、`checklists/`）— 直接破壞我們 6 類結構上限
2. `docs/patterns/` 跟我們 `docs/decisions/patterns/` 撞名（認知混淆）
3. `docs/mistakes/` 邏輯該歸 `docs/tracking/pitfalls/`，SC pm 另開一份
4. 跟 ccpm 的 epic-based PM 流程重複

PM 工作流由 **ccpm + `/docs:promote`** 承擔，不重複建立 SC pm namespace。

## 產出位置

| Command | 預設位置 | 入版控？ |
|---------|---------|---------|
| `/sc:workflow` | `claudedocs/workflow_*.md` | ❌（`.gitignore` 已加） |
| `/sc:research` | `claudedocs/research_*.md` | ❌ |
| `/sc:pm` | `docs/{temp,patterns,pdca,mistakes,checklists}/` | **不啟用** |
| `/sc:document` | 自行指定 | 看內容 |
| 其他 | 多為純對話 / 自指定 | — |

`claudedocs/` 是 SC 自用 namespace，整個 git-ignored，要保留就走 `/docs:new` 或 `/docs:promote`。

## 與本 repo `docs/` 結構的整合

唯一整合點：`/sc:brainstorm` → `.claude/prds/` → ccpm → `docs/`

```
/brainstorm-sc-to-ccpm <feature-slug>

  ↓ Step 1: /sc:brainstorm 多 persona 對話

.claude/prds/_drafts/<name>-brainstorm.md

  ↓ Step 2: 塑形成 ccpm schema

.claude/prds/<name>.md                       (PRD)

  ↓ ccpm parse-prd

.claude/epics/<name>/epic.md                 (Epic)

  ↓ ccpm decompose + sync + execute

.claude/epics/<name>/<N>.md + GitHub issues

  ↓ epic 完成 + ccpm archive

.claude/epics/archived/<name>/

  ↓ /docs:promote ccpm:<name>

docs/decisions/   docs/reference/features/
docs/tracking/    docs/plans/<quarter>/
```

其他 `/sc:*` 寫到 `claudedocs/`（git-ignored），要保留就走 `/docs:new` 或 `/docs:promote`。

## CLAUDE.md 引用順序

當有 `~/.claude/commands/sc/*.md` 全域指令時，會被 Claude Code skills 系統自動列出。本 repo CLAUDE.md **不**重複列出全部 `/sc:*` 觸發詞 — 預設讓 SC 自己的 skill description 觸發。

## 更新

依官方 clone 位置 `git pull`，或重新 sync user-scope `~/.claude/commands/sc/`。

## 與 ccpm / superpowers 的關係

- **ccpm**：產出在 `.claude/`，整套 PM 流程。SuperClaude 只在「探索期入口」交集（`/sc:brainstorm` → `/brainstorm-sc-to-ccpm` → ccpm PRD）
- **superpowers**：plugin 形式、SessionStart hook 自動。SuperClaude vs superpowers 的 `brainstorming` skill **功能重疊**，本 repo 統一走 `/brainstorm-sc-to-ccpm`（用 `/sc:brainstorm` 而非 `superpowers:brainstorming`）— 因為 SuperClaude 的 brainstorm 與 ccpm schema 對齊較好
