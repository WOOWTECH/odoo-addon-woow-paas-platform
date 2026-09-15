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

1. 從 repo checkout 執行本 skill 的 [`scripts/board-scan.py`](scripts/board-scan.py)，一次傳入
   全部七個 repo：

   ```bash
   python3 .omnigent/woow-issue-leader/skills/board-reconcile/scripts/board-scan.py \
     odoo-addons/woow_paas_platform \
     woow-paas/paas-agent-runtime \
     woow-paas/paas-app-bundle-cd \
     woow-paas/paas-odoo-ci \
     woow-paas/woow-paas-charts \
     WOOWTECH/woow-mcp-server \
     ha-components/woow_paas_smart_home
   ```

   腳本只用 stdlib 與 `tea api`，帶自訂 `User-Agent`；每個 repo 以
   `state=open&type=all&page=N&limit=50` 逐頁讀真正的 API JSON，直到**合法空 array**。API
   的 `pull_request` 為 object 時是 PR，必須略過；為 `null` 才計入 open issue。最後以
   repository metadata 的 `open_issues_count` 對帳，任何不一致都非零退出。
2. ⛔ 只有空 array 才是終點。任一頁 `rc != 0`、UTF-8／JSON／欄位解析失敗都要立即
   fail-loud，不得當成空頁；⛔ 不得用 `head -c` 或其他 byte 截斷輸出，也不得印出 token。
3. 若人工用 `tea issues list` 複查，也必須用 `--page N --limit 50 --output simple` 逐頁直到
   空頁；`labels` 顯示值是以空白串接的字串，須用空白切分（不是逗號、也不是 JSON array）。
   `rc != 0` 或解析失敗同樣要 fail-loud。
4. 只把 `MATCH` 列（帶 `agent-status:*` 或 `needs-*`）寫進看板；closed issue 一律清掉。
5. 每列寫 repo、issue 編號、當前 `agent-status:*`／`needs-*`，以及 PR 連結（若為
   `agent-status:pr-open`）。
6. ⛔ 不手寫看板上的狀態、不從記憶覆蓋；每次一律依當下 Gitea 查詢結果完整重建。

## 自測

腳本或解析規則有變更時執行：

```bash
python3 .omnigent/woow-issue-leader/skills/board-reconcile/scripts/test_board_scan.py
```

## 維護原則

- 已完成的流水帳不留在看板；歷史在 git log 與 issue。
- 不抄測試數、版號、叢集狀態等會漂移的事實；需要時只寫連結。
- 看板膨脹到看不懂時，先把全文存底進 `docs/archived/`，再精簡；⛔ 不要直接刪。
