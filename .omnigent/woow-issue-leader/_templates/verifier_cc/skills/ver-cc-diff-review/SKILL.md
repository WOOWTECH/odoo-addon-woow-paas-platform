---
name: ver-cc-diff-review
description: claude-native 覆核 worker：只對著驗收契約審 pi 實作（降級模式下為 claude-native 實作）的 diff，分 blocking／non-blocking／建議回報。
---

# ver-cc-diff-review

## 什麼時候用

拿到 `purpose: review` 且附了 diff ＋ 驗收契約時。

## ⛔ 前提

- 你**唯讀**（`read_only_os` 機器守線）。發現問題只回報，⛔ 不動手改。
- 你正常審的是 **pi 實作**的 diff。同 vendor 自審不算獨立覆核。
- 降級模式（pi 池不可用）下才會審 **claude-native 實作**：工單必須標 implementer 的
  harness／model；沒標、或 model 與你相同，就拒審並回報（見 AGENTS.md「跨 vendor 覆核」）。
- 你只有 diff ＋ 契約，**不要**去翻實作者的 worktree。

## 步驟

1. **先讀契約，再讀 diff**。反過來會被 diff 帶著走，變成審「他寫得好不好」而不是
   「有沒有做到」。
2. **逐條對照契約**：每一條驗收條件標記 達成／未達成／證據不足。
3. **找真的會壞的地方**。每個 blocking 都要寫出**具體失敗情境**：什麼輸入或狀態 → 什麼錯
   誤輸出或 crash。寫不出失敗情境的，就不是 blocking。
4. **驗數字**：實作者回報的測試數，要比對同一條指令、同一組檔案、同一個 commit。pytest
   的 ground truth 是 **collected cases**（`--collect-only -q`），不是
   `grep -c 'def test_'`——一個 parametrized function 會展開成多個 case。對不上再說對不
   上，⛔ 不要憑印象指控。

## 回報格式

三段，**每一條附 `file:line`**：

- **Blocking**：違反契約／會壞／驗收未達成（附失敗情境）
- **Non-blocking**：真的有問題但不擋合併
- **建議**：可做可不做

沒有 blocking 就寫「無 blocking」。⛔ 不要為了顯得認真硬湊。
