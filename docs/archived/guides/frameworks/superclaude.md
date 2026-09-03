---
title: SuperClaude_Framework (SuperClaude-Org)
category: archived
status: archived
created: 2026-05-19
updated: 2026-05-19
---

> **已退役**：SuperClaude_Framework 已退役（`~/.claude/commands/sc/` 不再安裝），本檔留作考古；現行框架＝mattpocock/skills，消費規則見 `docs/agents/`。

# SuperClaude_Framework — `/sc:*` Commands

GitHub: <https://github.com/SuperClaude-Org/SuperClaude_Framework>

User-scope slash commands 集合（30+），覆蓋 plan / design / build / test / PM / research / document 等領域，安裝在 `~/.claude/commands/sc/`（**跨專案共享**）。本 repo **選擇性啟用**：`/sc:brainstorm` ＋ 其他 ad hoc；產出一律落在 `claudedocs/`（git-ignored），要保留需手動 promote。

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
| `/sc:brainstorm` | ✅ 直接呼叫 | 多 persona 對話探索；寫 `claudedocs/`（git-ignored），要保留需手動 `/docs:new` |
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
4. 跟既有的 issue-driven PM 流程（Gitea issues）重複

PM 工作流走 **Gitea issues**（`tea` CLI），不重複建立 SC pm namespace。

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

**沒有自動流向。** 所有 `/sc:*`（含 `/sc:brainstorm`）都寫到 `claudedocs/`——該目錄 git-ignored，
不入版控。要保留任何產出，必須**人工挑選後手動**搬進主結構：

```
/sc:brainstorm <topic>

  ↓ 多 persona 對話

claudedocs/<...>.md              (git-ignored — 不做任何事的話就停在這，不入版控)

  ↓ 人工挑出長期有價值的內容，手動執行

/docs:new <category> <name>      → docs/{decisions,reference,plans,tracking}/
```

要**開工**的項目走 Gitea issue（`tea` CLI），不經 `claudedocs/`。

> **2026-08-07 變更（#631）**：此處原本有 `/sc:brainstorm` → `.claude/prds/` → ccpm → `docs/`
> 的自動路徑，入口是 `/brainstorm-sc-to-ccpm`。ccpm 已退役，該指令與整棵 ccpm tree 一併移除，
> 因此 SuperClaude 的產出現在**只有手動路徑**。歷史產出見
> [`docs/archived/ccpm-v2/`](../../archived/ccpm-v2/README.md)。

## CLAUDE.md 引用順序

當有 `~/.claude/commands/sc/*.md` 全域指令時，會被 Claude Code skills 系統自動列出。本 repo CLAUDE.md **不**重複列出全部 `/sc:*` 觸發詞 — 預設讓 SC 自己的 skill description 觸發。

## 更新

依官方 clone 位置 `git pull`，或重新 sync user-scope `~/.claude/commands/sc/`。

## 與 superpowers 的關係

- **superpowers**：plugin 形式、SessionStart hook 自動注入。SuperClaude vs superpowers 的
  `brainstorming` skill **功能重疊**，兩者擇一即可。
- 選擇依據是**產出落點**：`/sc:brainstorm` 寫 `claudedocs/`（git-ignored，探索痕跡不留）；
  superpowers `brainstorming` 寫 `docs/superpowers/`（入版控，可事後 `/docs:promote`）。
  想留下 rejected options / persona 觀點就用 superpowers。
