---
name: stale-sweep
description: 按需的 stale 掃描：找出 agent-status:in-progress 且 N 小時無 Gitea 活動、無 needs-* 的 issue，換成 blocked 並留言。不排程、不輪詢，人叫才跑。
---

# stale-sweep

## 什麼時候用

**只在人明確叫你掃的時候**（例如「掃一下 stale」「哪些 issue 卡住了」）。⛔ 不要排程、不要
輪詢、不要自己起 timer——喚醒只保證一跳，掃描的觸發者永遠是人（2026-09-14 裁決）。

判準：掛 `agent-status:in-progress`、**N 小時**內沒有任何 Gitea 活動（issue 的 `updated`
時間），而且沒掛 `needs-decision`／`needs-info`（那兩種是在等人，不是死了）。N 預設
**6 小時**；人叫你時有講就用他講的。

## 步驟

1. 對六個 repo 各跑（repo 清單與 `board-reconcile` 重建看板用的同一組；手上沒有清單就問人，
   ⛔ 不要猜 owner/repo）：

   ```bash
   tea issues list --login git-prod.woowtech.io --repo <owner/repo> --state open \
     --labels agent-status:in-progress --fields index,updated,labels,title --limit 100 < /dev/null
   ```

2. 逐列判定：`updated` 距現在 ≥ N 小時、且 `labels` 沒有任何 `needs-*` ⇒ stale。
   ⚠ `updated` 的時區以 `tea` 印出的為準；回報時寫明你用的「現在」與時區，讓人能重算。
3. 每張 stale issue（`tea` 在非 TTY 可能等 stdin，兩條都要 `< /dev/null`）：

   ```bash
   tea issues edit <n> --add-labels agent-status:blocked --remove-labels agent-status:in-progress \
     --login git-prod.woowtech.io --repo <owner/repo> < /dev/null
   tea comment <n> "stale-sweep：<掃描時間> 起 <N> 小時無活動且無 needs-*，疑似 worker 靜默死亡，改 blocked。人排除後改回 in-progress，並叫 issue-leader 續談同 title 的 repo_leader。" \
     --login git-prod.woowtech.io --repo <owner/repo> < /dev/null
   ```

4. 掃完用 `board-reconcile` 重建看板（你是唯一能寫看板的人）。
5. 回報：掃了哪幾個 repo、各幾張 in-progress、幾張判 stale（列 repo＋編號）、幾張因 `needs-*`
   跳過、你用的 N 與「現在」。⛔ 不要只回「掃完了」。

## 邊界

- 你只改 label 與留言，⛔ 不派 worker、不去看 worker 的 session、不猜它死在哪一步——那是人
  排除後的事。
- 判錯（其實還活著）的代價是人多改一次 label；漏判的代價是 issue 無聲卡死。所以邊界情況
  （差幾分鐘就滿 N）判 stale。
