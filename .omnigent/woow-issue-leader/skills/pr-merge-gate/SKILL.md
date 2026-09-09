---
name: pr-merge-gate
description: PR 收斂與人工 merge gate：覆核完成後該做什麼、以及為什麼 agent 永遠不代 merge。
---

# pr-merge-gate

## ⛔ 核心：merge 是人工 gate

PR 由 agent 開，**merge 由人做**。你、`repo_leader`、所有 worker 都不代 merge。
機器守線 **P-merge** 會 DENY `tea pr merge` / `gh pr merge` / `glab mr merge`。

這跟 prod build 的 `/approve` 同級：**即使人類當場明確授權、即使手上憑證就是授權者本
人，也不代簽**（2026-08-16 反面先例，裁決無例外）。機器守線 **P-approve**、**P-tag** 分
別擋 `/approve` 留言與推 tag。

## PR 收斂流程

1. **確認標題前綴**：`⛔ 勿代 merge｜待 <覆核者> 覆核 — `。沒有就請實作者補。
2. **確認描述**是 zh-tw，而且**貼了驗收指令的真實輸出**。只寫「測試通過」不算數。
3. **確認跨 vendor 覆核跑過**，而且覆核回報分了 blocking／non-blocking／建議、每條附
   `file:line`。
4. **blocking 回頭派給原實作者修**，⛔ 不要另找一個人改同一份 diff。
5. 全部收斂後，在 issue 留言（zh-tw）：PR 連結、覆核結論、驗收證據、**「等人工 merge」**。

## 本 repo 的 gate 特例

- PR 的 CI 綠燈**不含 Odoo 測試**（`odoo-tests.yml` 刻意不掛 `pull_request`，只在 merge
  進 develop 後與每日 02:00 跑）⇒ 動 `src/` 的 PR，**合併前要有沙盒測試結果貼在 PR 描
  述裡**。曾經發生過綠燈 merge 後 develop 紅。
- `src/__manifest__.py` 版號**一般 PR 不動**（必須等於 `origin/develop`），只有新增
  `src/migrations/<X>/` 的 PR 與 `release/*` 發版 PR 才 bump。`manifest-version-gate` 是
  develop 唯一 required check。
- 發版一律走 `/woow:deploy`。⚠ **phase 1 綠燈 ≠ 發版完成**。
