---
title: superpowers framework (obra/superpowers)
category: guides
status: active
created: 2026-05-19
updated: 2026-05-19
---

# superpowers — Claude Code Plugin

GitHub: <https://github.com/obra/superpowers>

Claude Code **plugin** 形式安裝（不是專案內 skill）。SessionStart hook 自動載入主入口 skill，所有 conversation 開頭即可呼叫其下 16+ 個工作流 skills（brainstorming / TDD / debugging / writing-plans 等）。

## 安裝

由 Claude Code plugin 系統管理：

```
/plugin add claude-plugins-official/superpowers
```

安裝後實際位置（user-scope，跨專案共享）：

```
~/.claude/plugins/cache/claude-plugins-official/superpowers/<version>/
├── SKILL.md / README.md
├── hooks/
│   ├── session-start             # SessionStart hook 入口
│   └── hooks.json
├── skills/                       # 16+ skills
│   ├── using-superpowers/
│   ├── brainstorming/
│   ├── writing-plans/
│   ├── test-driven-development/
│   ├── systematic-debugging/
│   └── ...
└── docs/                         # plugin 內嵌文件
```

## 啟動方式

- **SessionStart hook 自動**：每個 Claude Code session 起跳時，hook 注入 `using-superpowers` skill 的內容到 system prompt
- **手動觸發**：透過 `Skill` tool 呼叫，或符合 skill description 的 keyword 自動匹配

## 重點 skills

| Skill | 何時用 |
|-------|-------|
| `using-superpowers` | 主入口（每個 session 起跳自動） |
| `brainstorming` | 創意 / 設計 / 加功能前的探索（要求多 persona 對話） |
| `writing-plans` | 有 spec / 需求後，寫多階段 implementation plan |
| `executing-plans` | 跟著已寫的 plan 跑實作（含 review checkpoint） |
| `test-driven-development` | 實作前先寫 test（強制 red-green-refactor） |
| `systematic-debugging` | 遇到 bug / test fail 時的結構化 debug |
| `requesting-code-review` | 完工前自我請求 review |
| `verification-before-completion` | claim "done" 前驗證 |
| `using-git-worktrees` | feature 開工先建 worktree 隔離 |
| `dispatching-parallel-agents` | 2+ 獨立任務時平行派 agent |
| `subagent-driven-development` | 在當前 session 內用 subagent 跑獨立 task |
| `finishing-a-development-branch` | merge / PR 前的收尾流程 |
| `receiving-code-review` | 收到 review feedback 時的處理 |
| `writing-skills` | 自己寫 skill 時的 meta skill |

完整列表 + 描述：`~/.claude/plugins/cache/claude-plugins-official/superpowers/<version>/skills/`。

## 產出位置（**會碰 docs/**）

兩個 skill 會自動寫入專案 `docs/superpowers/`：

```
docs/superpowers/
├── specs/                                   # brainstorming skill
│   └── YYYY-MM-DD-<topic>-design.md
└── plans/                                   # writing-plans skill
    └── YYYY-MM-DD-<feature>.md
```

**這是工具自管理區**，不算 docs/ 6 類主題目錄。詳見 `../../superpowers/README.md`。

## 與本 repo `docs/` 結構的整合

`docs/superpowers/` 內的 spec / plan **預設留在原處**（作為探索痕跡）。當其中內容有長期價值時，透過 `/docs:promote` 升級到主結構：

```
/docs:promote superpowers:<filename>
```

候選去處：

| 來源內容 | 目標 |
|---------|------|
| writing-plans 完整 design+impl 雙檔 | `docs/plans/<quarter>/<file>-design.md` + `-implementation.md` |
| brainstorming 含 trade-off / option 論述 | `docs/decisions/` |
| 含 incident / bug 記錄 | `docs/tracking/pitfalls/` |

promote 後在原檔頂端加 `> Promoted to: docs/<path>` 註記，**不刪原檔**（保留探索脈絡）。

## CI 例外

`docs/superpowers/` 已加入 `.docs-check.yml` 的 orphan check 例外（探索檔可以是 orphan、不算問題）。lychee broken link check 仍會掃 `docs/superpowers/` 內檔的 link。

## 為什麼把產出位置設在 `docs/` 而不是 `.claude/`

obra/superpowers 的 skill 預設輸出路徑寫死在 `docs/superpowers/`，不可 redirect。改 plugin 內檔有風險（plugin 升級會覆蓋）。所以我們**接受** `docs/superpowers/` 成為工具自管理區，並用 promote 流程承接。

## 更新

```
/plugin update superpowers
```

或讓 Claude Code 自動拉新版（多版本並存於 cache，新 session 用最新）。

## 與 ccpm / SuperClaude_Framework 的關係

- **ccpm**：產出在 `.claude/`，superpowers 不碰。promote 路徑共用 `/docs:promote`
- **SuperClaude `/sc:brainstorm`**：跟 superpowers `brainstorming` skill 功能重疊，我們透過 `/brainstorm-sc-to-ccpm` 走 SuperClaude 路徑（產 ccpm-compliant PRD）；superpowers brainstorming 適合純探索（非 ccpm 場景）
