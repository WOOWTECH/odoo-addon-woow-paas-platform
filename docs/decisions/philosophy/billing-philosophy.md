# PaaS 計費與發票開立的產品哲學

## PaaS 計費與發票開立原則

### 核心理念

**點數（wallet）是平台主要支付手段，信用卡僅在必要時動用，發票只對應實際的信用卡交易。**

### 五大原則

**1. 點數為主要支付手段**

所有訂閱扣款，以及未來規劃的按量計費服務，都優先以點數扣抵。點數是用戶與平台之間的主要計價單位。

**2. 信用卡只在兩個時機動用**

- 用戶**主動儲值**購買點數
- 訂閱週期到期時，**點數不足**，由 auto-topup 自動補足差額

除此之外，平台不主動向用戶信用卡收款。

**3. 發票對應實際的信用卡交易**

每一張送往 ECPay 開立的電子發票，都對應一筆真實的信用卡扣款（儲值或 auto-topup 補差額）。

純粹的「點數消費」（例如訂閱週期 cycle invoice 由 wallet 扣抵完成）**不另外開立發票**，避免對同一筆金流重複開票。

**4. Odoo invoice 與 ECPay 發票分層處理**

- Odoo invoice **永遠存在**，作為內部會計憑證，記錄所有交易事件（含純點數扣抵）
- 是否送往 ECPay 開立電子發票，由 `woow_invoice_issue_state` 欄位控制：
  - `not_required`：純 wallet 扣款，不送 ECPay
  - 其他狀態：對應信用卡交易，由異步 cron 處理開立
- 兩者透過 `account.move.ecpay_invoice_id` 建立關聯

**5. 與上游 ERP 解耦**

PaaS 自己建立、帶 `woow_skip_upstream_hook=True` 的發票隔離上游 inline hook，並依
`woow_invoice_issue_state` 走自己的異步 cron 流程（`ecpay_invoice_cron`），含 retry
與失敗通知機制；不改 company 級預設，也不改 ERP／官網的原生開票行為。

`/woow` 也可能列出 ERP／官網建立的 flagless 發票。#980 的 A 窄守衛規定：當該張發票的
`company.auto_invoice='automatic'` 時，list/detail 必須先標示不可由 `/woow` 付款，付款
端點與目前會建立 flagless 發票的 **Cloud Service skip-trial** 必須在任何 savepoint、
wallet 扣款、`action_post()` 前拒絕。這不是 B 方案：平台不抑制 inline einvoice、不把
flagless 發票改送 async cron。`auto_invoice!='automatic'` 時，flagless 發票維持既有
`/woow` 付款行為。

此處的 skip-trial 守衛**僅涵蓋 Cloud Service**。Security Access 與 Smart Home 的
skip-trial 尚未傳入同一個 `before_mutation` guard，因此未受保護；這是既有缺口，留待
#864 §8.3 的產品取捨裁決。現況不得概括成全產品 replay-safe。

> **Scope limitation (2026-05):** The current `woow_skip_upstream_hook` mechanism
> only isolates `out_invoice` issuance (`create_ecpay_invoice`). Refund flows
> (`out_refund` → upstream `run_refund`) are not yet covered — `run_refund` will
> need a parallel override when 折讓/退費 is implemented.

---

## 對照表：理念 vs. 實作

| 產品理念 | 實作機制 |
|---------|---------|
| 只有實際動用信用卡才產生發票 | cycle invoice 標 `not_required`；儲值 + auto-topup 補差額才送 ECPay |
| ECPay 自動開發票串接 | `ecpay_invoice_cron` 異步呼叫 `create_ecpay_invoice()`，含 retry + 失敗通知 |
| Odoo invoice 與 ECPay 發票關聯 | 透過 `account.move.ecpay_invoice_id` 連接（既有上游機制） |
| 與上游解耦 | 帶 skip flag 的 PaaS 發票走 async；flagless＋automatic 在 `/woow` 前置拒付，不改 ERP／官網 hook |

---

## 適用範圍

此哲學是 `docs/plans/2026-Q2/2026-05-03-invoice-issue-fix-path-b.md` 完成後的恆定行為，作為後續計費相關功能（按量計費、退款、發票作廢等）設計時的指導原則。