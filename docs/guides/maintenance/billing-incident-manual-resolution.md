---
title: 帳務 incident 人工查證與關單 runbook
category: guides
status: active
created: 2026-08-30
updated: 2026-08-31
related:
  - docs/decisions/864-wallet-charged-not-credited.md
---

# 帳務 incident 人工查證與關單 runbook

## 適用範圍

本 runbook 只處理 Odoo 後台 **Woow PaaS → Billing Incidents** 中 `state` 不是
`resolved` 的記錄。操作人必須屬於 `account.group_account_manager`。

`woow.billing.incident` 是刻意設計的 **global finance ledger**，不是 company-owned
業務表：它沒有 `company_ref`／company record rule。每一位 account manager 都可跨
company 讀取全部 incident，也可依 model allowlist 保存兩個人工查證欄位；這是 Eugene
裁決的安全契約，不是待補的隔離缺口。`orphan`／`needs_human` 的專屬 review activity
同樣派給群組內**全體** account managers，不依使用者目前 company 篩選。

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
關單前 account manager 只能修改前兩個人工欄位；每次 generic `write()` 都會先對完整
recordset 做 `FOR UPDATE` 並重讀 state。`sudo()` 不構成 lifecycle 權限；只有模組內
JSON-RPC 無法表達物件身分的 private capability 能執行一次精確的
`unresolved → resolved` transition。一旦 `state = resolved`，整筆 first resolution audit
完全 immutable，連這兩個人工欄位也不得再改。incident ledger 也一律不可 `unlink`
（一般 `sudo` 與 RPC 偽造 context 皆不例外；只相容 Odoo 真正 module uninstall 流程）。

ECPay **查得到或查不到都只代表「查證結果已記錄，這張 incident 已關單」**。兩種結果都
不得宣稱按鈕已補償款項，也不得把「關單」寫成「款項已補回」。

## 禁止動作

- ⛔ 不得直接修改 `loyalty_card.points`。
- ⛔ 不得用 `refund_wallet_points` 代替補點或 incident 關單。
- ⛔ 不得自行建立 `adjustment` ledger、手改 wallet transaction，或以其他臨時方法送點。
- ⛔ 不得因 Odoo 查不到 topup 記錄就略過 ECPay 查證。
- ⛔ 不得把 `ResultData`、卡號或其他卡片密文貼進 `detail`／`resolution_note`。

`_log(snapshot)` 的 field allowlist 只是一道 **shape boundary**，不是完整 PCI
boundary。共用 sensitive validator 會套到 `_log` 的所有文字 ingress、manager
`resolution_note` RPC write、人工關單按鈕與 `_resolve(resolution_note)`：明確的
`ResultData`／`Card4No`／`Card6No`／`CardNo`／`CVV`／`CVC`／`CID`／`CSC`／
`security code`／`SAD`（含 `sensitive authentication data`）／`PAN` marker，以及
Luhn-valid 13–19 位 PAN-like 數字都會被拒絕。只有專用 `external_ref`／`trade_no` 欄位
可豁免「數字碰巧通過 Luhn」；兩欄仍拒絕上述 marker，並另有長度、安全字元與
CRLF/control-character shape gate。`external_ref` 只接受 1–20 字元 raw provider MTN，
或 `<namespace>:<safe-reference>`；`trade_no` 只接受 provider-safe 字元。合法 20 碼
numeric MTN 仍可原樣顯示在 activity／後台，但輸入先經 shape 驗證，HTML 輸出再 escape；
任何 error/collision log 只記 incident id 或固定 64 hex SHA-256 fingerprint，不回印 raw
reference。自由文字中的 `MTN:`／`TradeNo:` prefix **不是豁免**。caller 仍有責任只交付
必要、去敏的 `detail` 安全摘要；不得把「通過 validator」解讀為 payload 已符合 PCI。

上述限制避免人工動作與後續自動化重複給點；任何需要改變 wallet 餘額的產品流程不屬於本
runbook，也不是「人工已處理」按鈕的功能。

## 稽核欄位

關單後確認以下欄位均存在：

- `state = resolved`
- `manual_check_result` 是 `ecpay_found` 或 `ecpay_not_found`
- `resolution_note` 非空
- `resolved_at` 非空
- `resolved_by_ref` 是執行者的純值 user id

系統只會移除這一筆 incident、且 activity type 精確為
`woow_paas_platform.mail_activity_type_billing_incident_review` 的 activities；同一筆
incident 的 generic Todo／其他 activity type，以及其他 incident 的待辦都會保留。
專屬 type 已限制 `res_model = woow.billing.incident`，是 schedule、dedupe、cleanup 的唯一
provenance；summary 不是識別依據。dedupe 只看 active 的專屬 activity：inactive／standard
`action_done()` 後再收到 duplicate observation 會重建 active alert，而完成 activity 本身
**不會**把 incident 關成 resolved。

ledger 的 source/business 欄位仍完全 relation-free。為使用 Odoo 標準
`mail.activity.create()`／notification bus／`action_done()`／`unlink()`，model 繼承
`mail.thread`；其 follower/message relational metadata 只指向已由第一個獨立交易 commit
完成的 incident。這與禁止 outer transaction source row 形成 dangling FK 的約束正交：
前者是 Odoo mail infrastructure metadata，後者才是禁止加入 ledger 的 business/source
Many2one／Many2many。
