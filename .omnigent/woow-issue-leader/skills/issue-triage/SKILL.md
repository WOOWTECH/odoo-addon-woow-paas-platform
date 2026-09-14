---
name: issue-triage
description: issue 進來時的分流：值不值得做、拆成什麼工單、派給哪個 repo_leader 與哪個 worker。
---

# issue-triage

## 什麼時候用

有新的 issue 或使用者交辦事項進來時。

## 1. 先判斷值不值得做

- 是不是既有 issue 的重複？先派 `tracker_*` 查重。
- 資訊夠不夠拆成可驗收的工單？不夠就先派 `scout_pi_*` 或 `researcher_*` 補齊，
  ⛔ 不要拿模糊需求直接派 `implement`。

## 2. 拆成可驗收的工單

每一份 `implement` 工單至少要有：

1. **範圍**：可以動哪些檔案／目錄，不可以動什麼。
2. **驗收條件**：跑哪一條指令、在哪個 commit、預期什麼輸出。
3. **worktree 路徑**與**整合分支**（先確認該 repo 的實際整合分支，⛔ 不要寫死）。
4. **PR 要求**：標題前綴、zh-tw 描述、驗收輸出要貼進描述。

## 3. 選 harness 與角色

- 需要兩輪獨立判斷的重要變更 → 兩個 `repo_leader` 各跑一輪；worker 路徑仍固定為 pi 實作、
  claude-native 覆核。
- 只是單線推進 → 挑一個 `repo_leader`（看哪邊額度池還有餘裕）。
- 實作固定 pi、覆核固定 claude-native：`impl_pi_*` 的 diff → `ver_cc_*`，單向確保跨 vendor。

## 4. `args.purpose` 對照

| 工作 | purpose |
|---|---|
| 改任何檔案（碼、測試、docs、config） | `implement` |
| 審 diff | `review` |
| 唯讀調查 | `explore` / `search` |
| 上游 addon／Odoo 18 行為查證 | `researcher` |
| 部署、叢集操作 | `deploy` |
| 對帳、查重、開單 | `triage` |

## 5. 標籤

work-state label **六選一**，同一張 open issue 同時只掛一個。agent 工作另外掛
`agent-context:*`、`agent-status:in-progress`、`agent-assignee:leader1`～`leader5`。
細節見 repo 的 `docs/agents/triage-labels.md`。

派工 `repo_leader_*` 之後，標 `agent-status:in-progress` ＋ `agent-context:*`：

```bash
tea issues edit <n> --add-labels agent-status:in-progress,agent-context:<context> \
  --login git-prod.woowtech.io --repo <owner/repo> < /dev/null
```

然後**結束回合**——責任結束，不等結果。之後的 `agent-status:blocked`／
`agent-status:pr-open`／`needs-*` 由 `repo_leader` 掛。
