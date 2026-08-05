# project wallet billing（已封存）

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_wallet_billing`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：大型 feature 開發中 - wallet credits + billing dashboard + invoices + top-up + payment methods + subscriptions

Wallet & Billing Dashboard feature 在 `feat/wallet-billing-dashboard` branch 上開發。

**Why:** PaaS 平台需要完整的計費前端，目前 Header credits 寫死、Billing 頁面空白、沒有 invoice/wallet/subscription 管理 UI。

**How to apply:** 實作計畫在 `docs/plans/2026-04-12-wallet-billing-implementation.md`，分 6 個 phase 共 31 個 task。設計文件在 `docs/plans/2026-04-12-wallet-billing-design.md`。

**進度：** Phase 1 開始中（2026-04-12）

**關鍵決策：**
- Pay Now 只用 wallet 點數
- 信用卡管理在 SPA 內做（不跳 portal）
- 儲值方案後台可設定（TopupPlan model）
- Sidebar 拆 Finance 區塊（Billing / Invoices / Top-up Store）
- 全部建在 sh_subscription + sh_subscription_wallet 之上，不重做
