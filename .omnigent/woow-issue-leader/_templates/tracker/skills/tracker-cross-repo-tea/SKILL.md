---
name: tracker-cross-repo-tea
description: 六 repo 的 tea 對帳流程：查重先於開單、關聯開單當下就寫、每條狀態附當下證據。
---

# tracker-cross-repo-tea

## 什麼時候用

`purpose: triage` / `search`，要跨 repo 盤點 issue／PR 或開新單時。

## 1. 查重先於開單

開任何新 issue 之前，**先搜尋**。`tea issues list`／`tea issues search` 用兩三組不同關鍵
字找過再說。找到既有的就回報既有編號，⛔ 不要開第二張。

## 2. 開單

- description 一律 **zh-tw 繁體中文**。
- **關聯在開單當下就寫進 description**，⛔ 不要事後補：
  - 會關閉某 issue 的 PR：`Closes #<n>`
  - 只參照不關閉：`Refs #<n>` 或直接貼連結
  - Issue 對應到處理它的 PR：在 issue description 或留言連結該 PR
- ⚠ `Closes` 只在合併進**專案預設分支**、且專案有開「auto-close referenced issues」時才
  自動關閉。**先確認該 repo 的實際預設分支**（有些是 `develop` 而不是 `master`／`main`）；
  target 不是預設分支時要自行手動關閉。

## 3. 對帳要有當下證據

每一條狀態附**當下**查到的來源：issue 編號＋連結，或你跑的 `tea` 指令與輸出。
⛔ 不要引用幾天前的快照——狀態會漂。

## 4. 寫權邊界

你有寫權，但**只寫產物**：issue／PR 內容、對帳報告檔。
⛔ 不碰產品原始碼，⛔ 不碰 `.woow-issues/board.md`（P-board 會 DENY）。
