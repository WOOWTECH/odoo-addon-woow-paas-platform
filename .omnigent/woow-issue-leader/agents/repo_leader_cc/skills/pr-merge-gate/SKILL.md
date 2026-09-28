---
name: pr-merge-gate
description: PR 收斂與人工 merge gate：覆核完成後該做什麼、以及為什麼 agent 永遠不代 merge。
---

# pr-merge-gate

## 兩件事要分清楚

這是**兩層不同的規則**，常被混為一談：

### 1. 「agent 永遠不代 merge」——絕對，不分類型

不管是純 docs、typo 修正還是大重構，**agent 一律不按 merge**。機器守線 **P-merge** 會
DENY 所有 forge CLI 的 merge 路徑（含 `gh api -X PUT .../merge` 這種 REST 變形）。

這跟 prod build 的 `/approve` 同級：**即使人類當場明確授權、即使手上憑證就是授權者本
人，也不代簽**——裁決無例外。

### 2. 「人類覆核者的拍板分級」——分類型

這一層決定的是**人類**看完之後要不要再找人討論，不是 agent 能不能動手：

| PR 類型 | 人類覆核者可以怎麼做 |
| --- | --- |
| 純 docs／純 bug fix／test-only／純 CI-config，**且無待討論的取捨** | CI 全綠 ＋ 覆核 diff 後**可直接合併** |
| 含取捨／行為權衡／動生產碼行為／`feat`／`refactor`／CI **政策**變更 | **待人類拍板**，不要逕自合併 |

⚠ 「純 CI-config」指的是設定值調整（逾時、快取鍵、runner 標籤）；改變**什麼情況會擋下
PR** 屬於 CI 政策，落在第二列。

你的工作是把 PR 收斂到「人類可以套用上表判斷」的狀態，並在 issue 留言裡**明講這張 PR 落
在哪一列、依據是什麼**。⛔ 你自己不做第 1 層的動作。

機器守線 **P-approve**、**P-tag** 另外擋 `/approve` 留言與推 tag。

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
