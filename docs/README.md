# docs/ — 文件導引

本 repo 的文件採 **6 類混合分類**，每類負責一種「文件使用情境」。寫新檔之前先看：**你想讓讀者「做什麼」？**

## 6 類速覽

| 目錄 | 用途 | 你會在這找到… |
|------|------|--------------|
| [`guides/`](guides/README.md) | **任務導向** — 操作手冊（如何做 X） | k8s setup、testing guide、新人 onboarding、tutorial |
| [`reference/`](reference/README.md) | **查找導向** — 規格、API、藍圖 | feature spec、API contract、架構生命週期圖、design 檔（.pen） |
| [`decisions/`](decisions/README.md) | **解釋導向** — 為什麼這樣設計 | philosophy、refactor 決策、可複用 pattern、PR review verdict |
| [`plans/`](plans/README.md) | **過程紀錄** — 工作計畫雙檔 | YYYY-MM-DD-<slug>-design.md + -implementation.md、季度歸檔、journal |
| [`tracking/`](tracking/README.md) | **問題與追蹤** — issue / pitfall / follow-up | sandbox 故障、incident timeline、技術債清單、test report |
| [`archived/`](archived/README.md) | **歷史檔案** — 不再使用但保留考古 | ccpm-v1 context、退役 plan |

加上一個工具自管理區（不算主題類）：

- [`superpowers/`](superpowers/README.md) — obra/superpowers plugin 自動寫入的 specs / plans，promote 後落到主結構

## 何時用哪類？

```
新檔要寫了，先問自己：
  ↓
  「給讀者答案讓他做事」？──────────────→ guides/
  ↓ 否
  「描述事實 / 規格 / 是什麼」？─────────→ reference/
  ↓ 否
  「論述為什麼、含 trade-off」？─────────→ decisions/
  ↓ 否
  「啟動一個 epic / 大功能的計畫」？─────→ plans/<quarter>/
  ↓ 否
  「記錄一個尚未解決的問題 / 待辦」？───→ tracking/
  ↓ 否
  → 退一步重新審視，或加 <!-- TODO(docs): re-classify --> 暫放最接近類別
```

## 建檔工具

| 動作 | 指令 |
|------|------|
| 建新 doc（含 frontmatter 模板） | `/docs:new <category> <name>` |
| 把 ccpm epic / superpowers 探索 promote 到主結構 | `/docs:promote ccpm:<feature-name>` 或 `/docs:promote superpowers:<file>` |

## 命名規則

- **目錄**：`kebab-case`、複數型（`decisions/` 而非 `decision/`）
- **檔名**：
  - `plans/<quarter>/`：`YYYY-MM-DD-<slug>-design.md` + `YYYY-MM-DD-<slug>-implementation.md`
  - `decisions/`：`<topic>-<aspect>.md`（易過期型可加日期前綴）
  - `tracking/pitfalls/`：`<incident-slug>.md`
  - `tracking/followups/`：固定 `follow-ups.md`，每項以 H2 + 日期標題
  - 其他：`kebab-case`，禁空格與底線
- **frontmatter**（建議非強制）：

```yaml
---
title: <human readable>
category: guides | reference | decisions | plans | tracking | archived
status: draft | active | superseded | archived
created: YYYY-MM-DD
updated: YYYY-MM-DD
related: [docs/decisions/xxx.md, .claude/epics/yyy/epic.md]
---
```

## Cross-link 規則

| 方向 | 規則 |
|------|------|
| docs/ → ccpm | frontmatter `source: .claude/epics/<name>/epic.md` 或 `related: [.claude/...]` |
| ccpm → docs/ | epic.md / task.md 行內 `> Promoted to: docs/decisions/<file>.md`（不動 ccpm schema 欄位） |
| docs/ → superpowers | frontmatter `source: docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` |
| superpowers → docs/ | 原檔頂端 `> Promoted to: docs/decisions/<file>.md` |
| docs/ → docs/ | 相對路徑（範例：從 `plans/<quarter>/` 引用 decisions 寫 `../../decisions/<file>.md`）— CI link check 友善 |
| 外部（GitHub / Gitea PR/issue） | 永遠用絕對 URL |

**何時不可用 `[[wikilink]]`**：指向 superpowers skill 名、尚未建檔的占位（planned-but-not-yet-written），**不要**用 `[[wikilink]]` — lychee broken-link check 不檢查 `[[]]` 語法，這類連結會靜默變成永久 dangling。改用：

- **inline code**（如 `` `verification-before-completion` ``）標示 skill 名或外部概念；
- **先建 stub**（`/docs:new` 產 frontmatter，設 `status: draft`）再用 `[[wikilink]]` 指它；
- 或對已存在的 doc 用相對 `[text](path.md)` 連結。

`[[wikilink]]` 僅在目標檔**已存在**於 `docs/decisions/patterns/` 或 `docs/tracking/` 時使用。

## 工具介接

- **ccpm v2**：產出在 `.claude/prds/` 與 `.claude/epics/`，不直接碰 docs/。完成後透過 `/docs:promote` 把決策 / pitfall / follow-up 落到主結構
- **obra/superpowers**：brainstorming / writing-plans skill 自動寫到 `docs/superpowers/`（工具自管理區），透過 `/docs:promote` 升級到主結構
- **SuperClaude_Framework**：`claudedocs/` 是 SC 自用 namespace（已 git-ignored），不入版控；`/sc:pm` 不啟用

## CI 檢核

- **Phase 1**（目前）：lychee broken link check，PR 觸發，失敗只 warning，**不 block merge**
- **Phase 2**（穩定後）：升級為 block merge，加 orphan doc check
- 例外清單：`docs/archived/`、`docs/superpowers/`、`docs/INDEX.md`

## 「無法分類」處理

- 先放最接近類別，檔頭加 `<!-- TODO(docs): re-classify -->`
- 累積 ≥ 5 個觸發回顧
- **不建臨時目錄**

## 歷史

舊版 14 一級目錄結構於 2026-05-19 重組為 6 類。完整決策依據：`/Users/eugene/.claude/plans/sharded-wondering-squid.md`（本機 plan 檔，不入版控）。
