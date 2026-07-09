---
title: "#324 stg 清理 SOP — failed 垃圾交易 + next_payment 校正"
category: guides
status: active
created: 2026-07-10
topic: 324-stg-cleanup
related:
  - docs/superpowers/plans/2026-07-10-324-billing-dual-gap-fix.md
  - scripts/maintenance/cleanup_324_stg.py
  - src/models/sh_subscription_fix.py
---

# #324 stg 清理 SOP

修好 #324（上游兩支 wallet cron neutralize + 退點 payment gate）後，stg 上仍殘留兩類壞資料，需一次性清理。本 SOP 對應腳本 `scripts/maintenance/cleanup_324_stg.py`（冪等、condition-based、預設 dry-run）。

> **執行者＝leader**（本 repo agent 只交付腳本＋SOP，不代跑 stg 寫入）。

## 要清什麼

| 類別 | condition | 動作 |
| --- | --- | --- |
| failed deduct 垃圾交易 | `transaction_type='deduct'` 且 `state='failed'` 且 `subscription_id` 非空 | `state → 'cancelled'`（保留審計、不刪除）|
| never-paid 逾期訂閱 | `state='in_progress'` 且 `next_payment < today` 且**無 done deduct** | `next_payment → today + 一個週期`（避免修好後仍卡過去到期日被 dunning 立即重扣）|

> stg 07-09 時點：failed deduct ~832 筆（每小時仍在長）＋ 9 張壞訂閱。實際筆數以執行當下 dry-run 報告為準。

## 前置條件

- 已確認目標為 **stg**（prod 現零訂閱，勿在 prod 跑）。
- 取得目標 DB 名稱與 odoo pod。namespace 依鐵律限 `paas-ws-stg-*` / `paas-stg-system`。
- 修正版 addon（≥ 18.0.1.0.51，含 cron neutralize）**已部署到該環境**——否則清完 retry cron 又會再生 failed。

## 步驟

1. **dry-run（預設，只印報告不改）**

   ```bash
   kubectl -n <stg-ns> exec -it <odoo-pod> -- \
     odoo shell -c /etc/odoo/odoo.conf -d <db> --no-http \
       < scripts/maintenance/cleanup_324_stg.py
   ```

   讀輸出：`failed deduct garbage to cancel: N`、`never-paid overdue subs to re-date: M` 及每張 sub 的 `next_payment X -> Y`。確認 N/M 與預期相符。

2. **實跑**：把腳本頂端 `DRY_RUN = True` 改為 `False`，重跑同一指令。輸出末尾出現 `[#324] committed.` 即完成。

3. **回歸觀察**：等下一個整點與隔日，確認：
   - failed deduct 列不再增長（retry cron 已 neutralize）。
   - 受影響訂閱不再被每小時重扣。
   - 有餘額訂閱一週期只扣一次、成功後 next_payment 前進。

## 冪等性 / 回退

- **冪等**：已 `cancelled` 的交易不會再被抓（只抓 `state='failed'`）；已無 done-deduct 且 next_payment 已在未來的訂閱不再被改（只抓 `next_payment < today`）。可安全重跑。
- **回退**：
  - 交易誤標：把該筆 `state` 由 `cancelled` 改回 `failed` 即可（審計未遺失）。
  - next_payment 誤校正：手動再寫回原值或重算。
- **風險**：本腳本只動 `wallet.transaction.state` 與 `sh_date_of_next_payment` 兩欄，不刪任何 record、不碰錢包點數、不碰服務/ns。
