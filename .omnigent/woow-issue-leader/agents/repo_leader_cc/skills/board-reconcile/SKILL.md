---
name: board-reconcile
description: 從 Gitea label 重建看板 .woow-issues/board.md；只有 issue-leader 能寫，看板是 derived view。
---

# board-reconcile

## 為什麼只有你能寫

Gitea label 是跨環境可接力的 **single source of truth**；`.woow-issues/board.md` 是從 Gitea
重建的 **derived view**。全樹只有 `woow-issue-leader` 有寫權；兩個 `repo_leader` 與所有
worker 都唯讀，由機器守線 **P-board** 強制（寫入／編輯工具與 shell 改寫都 DENY）。

下層只更新 Gitea label／comment，不直接更新看板。

## 重建流程

1. 對六個 repo 各跑：

   ```bash
   tea issues list --login git-prod.woowtech.io --repo <owner/repo> --state open \
     --fields index,state,labels,title --limit 100 < /dev/null
   ```

2. 只列帶 `agent-status:*` 或 `needs-*` 的 open issue。
3. closed issue 一律從看板清掉。
4. 每列寫 repo、issue 編號、當前 `agent-status:*`／`needs-*`，以及 PR 連結（若為
   `agent-status:pr-open`）。
5. ⛔ 不手寫看板上的狀態、不從記憶覆蓋；每次一律依當下 Gitea 查詢結果完整重建。

## 維護原則

- 已完成的流水帳不留在看板；歷史在 git log 與 issue。
- 不抄測試數、版號、叢集狀態等會漂移的事實；需要時只寫連結。
- 看板膨脹到看不懂時，先把全文存底進 `docs/archived/`，再精簡；⛔ 不要直接刪。
