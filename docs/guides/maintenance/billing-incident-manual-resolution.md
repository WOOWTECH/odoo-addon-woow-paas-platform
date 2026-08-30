---
title: 帳務 incident 人工查證與關單 runbook
category: guides
status: active
created: 2026-08-30
updated: 2026-08-30
related:
  - docs/decisions/864-wallet-charged-not-credited.md
---

# 帳務 incident 人工查證與關單 runbook

## 適用範圍

本 runbook 只處理 Odoo 後台 **Woow PaaS → Billing Incidents** 中 `state` 不是
`resolved` 的記錄。操作人必須屬於 `account.group_account_manager`。

畫面上的警告是本流程的不變式：

> topup 記錄可能已被回捲、在 Odoo 裡查不到是預期現象，不是關單理由

## 唯一合法動作

1. 以 incident 的 `external_ref`（payment incident 是原始 ECPay MerchantTradeNo）到
   **ECPay 後台查證**。
2. 回到 incident form，在「人工查證」選擇且只能選擇一個結果：
   - **Found in ECPay**（`ecpay_found`）
   - **Not found in ECPay**（`ecpay_not_found`）
3. 在 `resolution_note` 寫下查證依據與結論。不得只寫「Odoo 查不到 topup」。
4. 按 **「人工已處理」**。

按鈕只會保存 `manual_check_result`、`resolution_note`、`resolved_at`、
`resolved_by_ref` 並把 incident 關成 `resolved`；它**不會修改 wallet、不會補點、也不會退款**。

ECPay **查得到或查不到都只代表「查證結果已記錄，這張 incident 已關單」**。兩種結果都
不得宣稱按鈕已補償款項，也不得把「關單」寫成「款項已補回」。

## 禁止動作

- ⛔ 不得直接修改 `loyalty_card.points`。
- ⛔ 不得用 `refund_wallet_points` 代替補點或 incident 關單。
- ⛔ 不得自行建立 `adjustment` ledger、手改 wallet transaction，或以其他臨時方法送點。
- ⛔ 不得因 Odoo 查不到 topup 記錄就略過 ECPay 查證。
- ⛔ 不得把 `ResultData`、卡號或其他卡片密文貼進 `detail`／`resolution_note`。

上述限制避免人工動作與後續自動化重複給點；任何需要改變 wallet 餘額的產品流程不屬於本
runbook，也不是「人工已處理」按鈕的功能。

## 稽核欄位

關單後確認以下欄位均存在：

- `state = resolved`
- `manual_check_result` 是 `ecpay_found` 或 `ecpay_not_found`
- `resolution_note` 非空
- `resolved_at` 非空
- `resolved_by_ref` 是執行者的純值 user id

系統會只移除這一筆 incident 的 Todo activities；不會碰其他 incident 的待辦。
