---
name: stale-sweep
description: 按需的 stale 掃描：找出 agent-status:in-progress 且 N 小時無 Gitea 活動、無 needs-* 的 issue，換成 blocked 並留言。不排程、不輪詢，人叫才跑。
---

# stale-sweep

## 什麼時候用

**只在人明確叫你掃的時候**（例如「掃一下 stale」「哪些 issue 卡住了」）。⛔ 不要排程、不要
輪詢、不要自己起 timer——喚醒只保證一跳，掃描的觸發者永遠是人。

判準：掛 `agent-status:in-progress`、**N 小時**內沒有任何 Gitea 活動（issue 的 `updated`
時間），而且沒掛 `needs-decision`／`needs-info`（那兩種是在等人，不是死了）。N 預設
**6 小時**；人叫你時有講就用他講的。

## 步驟

1. 依 `board-reconcile` skill 的七 repo 清單執行其
   [`scripts/board-scan.py`](../board-reconcile/scripts/board-scan.py)。腳本輸出的 `MATCH` 列含
   `updated_at`；從中取帶 `agent-status:in-progress` 的 issue，再排除任何 `needs-*`。

   ⛔ 呼叫掃描腳本／tea 時指令不得含反斜線（續行、\n 皆不可）：路徑含 board 字根，P-escape 會把「反斜線＋敏感字根」一律 DENY。

   腳本以 Gitea API 真正 JSON 的 `--page N --limit 50` 等價查詢逐頁掃到合法空 array，並以
   `open_issues_count` 對帳。任一頁 `rc != 0`、UTF-8／JSON／欄位解析失敗或總數不一致都必須
   fail-loud，⛔ 不得當成空頁，也不得用 `head -c` 等 byte 截斷輸出。

   若人工改用 `tea issues list` 複查，必須加 `--page N --limit 50 --output simple` 並逐頁直到
   空頁；其 `labels` 顯示值是空白串接字串，須以空白切分。`rc != 0` 或解析失敗仍須
   fail-loud。
2. 逐列判定：`updated_at` 距現在 ≥ N 小時、且 `labels` 沒有任何 `needs-*` ⇒ stale。
   ⚠ 回報時寫明你用的「現在」與時區；API 的時間字串保留原值，讓人能重算。
3. 每張 stale issue（`tea` 在非 TTY 可能等 stdin，兩條都要 `< /dev/null`）：

   ```bash
   tea issues edit <n> --add-labels agent-status:blocked --remove-labels agent-status:in-progress --login git-prod.woowtech.io --repo <owner/repo> < /dev/null
   tea comment --login git-prod.woowtech.io --repo <owner/repo> <n> "stale-sweep：<掃描時間> 起 <N> 小時無活動且無 needs-*，疑似 worker 靜默死亡，改 blocked。人排除後改回 in-progress，並叫 issue-leader 續談同 title 的 repo_leader。" < /dev/null
   ```

4. 寫入後逐張用 `tea issues <n> --comments` 重讀，確認 label 與留言都已落地；不能只信 write
   指令的 `rc=0`。
5. 掃完用 `board-reconcile` 重建看板（你是唯一能寫看板的人）。
6. 回報：掃了哪幾個 repo、各幾張 in-progress、幾張判 stale（列 repo＋編號）、幾張因 `needs-*`
   跳過、你用的 N 與「現在」。⛔ 不要只回「掃完了」。

## 邊界

- 你只改 label 與留言，⛔ 不派 worker、不去看 worker 的 session、不猜它死在哪一步——那是人
  排除後的事。
- 判錯（其實還活著）的代價是人多改一次 label；漏判的代價是 issue 無聲卡死。所以邊界情況
  （差幾分鐘就滿 N）判 stale。
