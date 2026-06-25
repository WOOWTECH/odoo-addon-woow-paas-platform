---
title: ccpm framework (automazeio/ccpm)
category: guides
status: active
created: 2026-05-19
updated: 2026-05-19
---

# ccpm — Claude Code Project Management

GitHub: <https://github.com/automazeio/ccpm>

Spec-driven project management agent skill。功能開發拆成 **PRD → Epic → GitHub Issue** 三層，agent 平行執行、git worktree 隔離。本 repo 把 ccpm 當作主要的 feature delivery 工具。

## 安裝

1. clone 倉到本機（不放 repo 內、避免被當 vendor）：

   ```bash
   git clone https://github.com/automazeio/ccpm.git ~/Documents/SKILL/ccpm
   ```

2. symlink 到本 repo 的 `.claude/skills/ccpm`：

   ```bash
   ln -s ~/Documents/SKILL/ccpm/skill/ccpm .claude/skills/ccpm
   ```

3. 驗證：

   ```bash
   readlink .claude/skills/ccpm
   # 應回傳上面那個 clone 路徑
   ```

## 觸發方式

skill 自動路由，不用手動 slash command — 觸發詞表：

| 意圖 | 觸發語句範例 |
|------|------------|
| Plan a feature | `write a PRD for X`、`let's plan X`、`scope this out` |
| Decompose epic | `break this epic into tasks`、`decompose the X epic` |
| Sync to GitHub | `sync the X epic`、`push tasks to github` |
| Start work | `start working on issue N`、`let's work on issue N` |
| Status / standup | `standup`、`what's next`、`what's blocked` |
| Close issue | `close issue N`、`mark task done` |
| Merge epic | `merge the X epic` |

## 產物位置（schema 不可動）

```
.claude/
├── prds/<feature-name>.md                # PRD（plan skill 產出）
├── prds/_drafts/                         # /brainstorm-sc-to-ccpm 暫存
├── prds/_design-notes/                   # /brainstorm-sc-to-ccpm 架構選項
└── epics/
    ├── <feature-name>/
    │   ├── epic.md                       # 技術 Epic
    │   ├── <N>.md                        # Task（N = GitHub issue 號）
    │   ├── <N>-analysis.md               # 平行 stream 分析
    │   ├── github-mapping.md
    │   ├── execution-status.md
    │   └── updates/<N>/
    │       ├── stream-A.md, B.md, ...
    │       ├── progress.md
    │       └── execution.md
    └── archived/<feature-name>/          # 完成 epic
```

**Schema 規範**：見 `.claude/skills/ccpm/references/conventions.md`。frontmatter 欄位（name、status、created、updated、github 等）由 ccpm 工具維護，**不要手動改**。

## 工作流（5 phases）

```
plan      → write PRD              → .claude/prds/<name>.md
structure → parse PRD into epic    → .claude/epics/<name>/epic.md
          → decompose epic         → .claude/epics/<name>/<N>.md
sync      → push to GitHub         → gh issue create + worktree
execute   → start working          → spawn parallel agents
track     → standup / close        → update status + close issues
```

詳細各 phase 步驟見 `.claude/skills/ccpm/references/{plan,structure,sync,execute,track}.md`。

## 與本 repo `docs/` 結構的整合

epic 完成 + archive 後（`mv .claude/epics/<name> .claude/epics/archived/`），把長期價值內容透過 `/docs:promote` 升級到 `docs/` 主結構：

```
/docs:promote ccpm:<feature-name>
```

候選段落（互動式選擇）：

| 來源段落 | 目標 |
|---------|------|
| Architecture Decisions / Trade-offs / Technical Approach | `docs/decisions/refactor/` |
| User Stories + AC、Functional Requirements、Success Criteria | `docs/reference/features/` |
| Philosophy（跨 feature 的設計原則） | `docs/decisions/philosophy/` |
| 可複用 pattern | `docs/decisions/patterns/` |
| Pitfalls / Bugs / Incidents | `docs/tracking/pitfalls/` |
| Follow-up / Tech debt / Caveat | `docs/tracking/followups/follow-ups.md` |

`/docs:promote` 的完整流程見 `.claude/commands/docs/promote.md`。

## 新 feature 探索期入口

如果要做新 feature 但需求還沒釐清，用 `/brainstorm-sc-to-ccpm` 而不是直接 `write a PRD`：

```
/brainstorm-sc-to-ccpm <feature-slug>
```

它會：

1. 啟動 `/sc:brainstorm` 多 persona 對話（SuperClaude）
2. 收尾時把對話結論存到 `.claude/prds/_drafts/`
3. 塑形為 ccpm-compliant PRD 寫到 `.claude/prds/<name>.md`
4. 若有 ≥2 個 architecture option，產 `.claude/prds/_design-notes/<name>.md`
5. 詢問是否接 ccpm parse → epic

詳見 `.claude/commands/brainstorm-sc-to-ccpm.md`。

## 更新 skill

```bash
git -C "$(readlink .claude/skills/ccpm)/../.." pull
```

## 版本歷史

- 本 repo 從 ccpm v1（散置 `.claude/agents/`、`.claude/commands/pm/`、`.claude/rules/`、`.claude/scripts/pm/`）遷移到 v2 Agent Skill 形式
- v1 的 project-context docs 已 archive 到 `docs/archived/ccpm-v1/context/`
- v2 ccpm **不**維護全域 context dir — 每個 agent 從對應 `epics/<feature>/` 讀 context

## 第一次在新機器上設定

1. clone `automazeio/ccpm` 到 skills 資料夾外的位置
2. `ln -s <clone>/skill/ccpm .claude/skills/ccpm`
3. `readlink .claude/skills/ccpm` 確認 link 正確
