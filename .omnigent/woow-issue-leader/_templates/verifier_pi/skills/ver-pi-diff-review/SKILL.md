---
name: ver-pi-diff-review
description: pi 覆核 worker：只對著驗收契約審 claude-native 實作的 diff，分 blocking／non-blocking／建議回報。
---

# ver-pi-diff-review

## 什麼時候用

拿到 `purpose: review` 且附了 diff ＋ 驗收契約時。內容與 `ver-cc-diff-review` 對齊——分成
兩份是因為來源池的 skill name 必須全域唯一。

## ⛔ 前提

- 你**唯讀**（`read_only_os` 機器守線）。只回報，⛔ 不動手。
- 你審的是 **claude-native 實作**的 diff。
- 你只有 diff ＋ 契約，⛔ 不要去翻實作者的 worktree。

## 步驟

1. 先讀契約再讀 diff。
2. 逐條對照契約標記 達成／未達成／證據不足。
3. 每個 blocking 附**具體失敗情境**（什麼輸入／狀態 → 什麼錯誤結果）。寫不出來的不算
   blocking。
4. 驗數字：同指令、同檔案集、同 commit 才能比。pytest 用 collected cases
   （`--collect-only -q`），不是 `grep -c 'def test_'`。

## 回報格式

**Blocking** / **Non-blocking** / **建議** 三段，每條附 `file:line`。沒有就寫「無」。
