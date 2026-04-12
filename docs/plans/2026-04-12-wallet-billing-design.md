# Wallet & Billing Dashboard Design

## Context

PaaS 平台的訂閱計費系統需要完整的前端介面。目前 Header 的 CREDITS 是寫死的，Billing 頁面只有空 route，沒有 invoice/wallet/subscription 管理 UI。後端 wallet 模組（`sh_subscription_wallet`）已完整，包含儲值、扣款、ECPay 信用卡綁定。需要：

1. 動態 Header credits 顯示
2. Billing Dashboard（Account Overview）
3. Top-up Store（儲值方案 + 信用卡扣款）
4. Invoice 列表 + wallet Pay Now
5. Wallet 交易紀錄
6. 訂閱管理（變更方案 / 取消）
7. 餘額檢查（建立服務 / 跳過試用 / 自動扣款時檢查餘額）

## 設計決策

| 決策 | 選項 | 理由 |
|------|------|------|
| Pay Now 付款方式 | 只用 wallet 點數 | 簡化流程，不夠就提示儲值 |
| 儲值流程 | 確認 modal（顯示卡號末四碼+金額）| 財務操作需明確確認 |
| 信用卡管理 | 跳轉到 Odoo portal `/my/wallet/bind_card` | 避免在 OWL 重建 ECPay 串接 |
| 儲值方案 | 後台 Odoo 可設定 | 靈活調整方案和 bonus |
| Sidebar 導航 | 拆成多個入口 | Billing / Invoices / Top-up Store 各自獨立 |

## 路由結構

```
#/billing              → Account Overview（餘額 + 訂閱 + 近期發票）
#/billing/topup        → Credit Top-up Store
#/billing/invoices     → Invoice 列表
#/billing/transactions → Wallet 交易紀錄
```

Sidebar 新增（在 AI Assistant 下方，Settings 上方）：

```
── Finance ──────────
  payments  Billing        → #/billing
  receipt   Invoices       → #/billing/invoices
  storefront Top-up Store  → #/billing/topup
```

## 頁面設計

### 1. Account Overview (`#/billing`)

```
┌─ Credit Balance ─────────────────── [Top-up →] ─┐
│  0 Credits                                      │
│  Conversion Rate: 1 Point = NT$ 1               │
└─────────────────────────────────────────────────┘

┌─ Subscription & Payment ────────────────────────┐
│  SUB-00007                                      │
│  Odoo Monthly    [Active]           Visa •1234  │
│  Next: 2026-05-12  499 pts                      │
│  [Change Plan]  [Cancel]                        │
│─────────────────────────────────────────────────│
│  SUB-00010                                      │
│  n8n Yearly      [Trial ends 04/18]             │
│  [Skip Trial]  [Change Plan]  [Cancel]          │
└─────────────────────────────────────────────────┘

┌─ Recent Invoices ──────────── [View All →] ─────┐
│  INV/2026/00001  Apr 12  573 pts   Paid         │
│  INV/2026/00002  Apr 12  199 pts   Waiting [Pay]│
└─────────────────────────────────────────────────┘

┌─ About WoowTech ───┐  ┌─ Billing Support ──────┐
│  ...               │  │  [Live Chat]  [Email]  │
└────────────────────┘  └────────────────────────┘
```

### 2. Top-up Store (`#/billing/topup`)

- Header 顯示目前餘額
- 儲值方案 grid（從後台 model 載入）：金額、bonus、價格、BEST VALUE 標籤
- 篩選：All Packs / Recommended / Large Volume / Custom Amount
- 點 Purchase → 確認 modal（卡號末四碼 + 金額 + bonus）→ ECPay 扣款
- 未綁定信用卡 → 導向 `/my/wallet/bind_card`
- About Credits + Billing Support 區塊

### 3. Invoice 列表 (`#/billing/invoices`)

- 欄位：Invoice #, Date, Due Date, Amount, Status
- Status badges: Draft (灰), Waiting for Payment (藍), Paid (綠), Overdue (紅)
- Pay Now 按鈕（Waiting/Overdue 狀態）→ wallet 扣款
- 排序 / 篩選（Date, Amount, Status）
- 分頁

### 4. Transaction 紀錄 (`#/billing/transactions`)

- 欄位：Date, Description, Type (credit/debit), Amount (+/-), Balance
- 來源：`wallet.transaction` model
- 分頁

## API Endpoints（新增）

| Endpoint | Method | 用途 | 回傳 |
|----------|--------|------|------|
| `/api/billing/wallet` | POST | Wallet 餘額 + 信用卡狀態 | `{ balance, card_bound, card_last4 }` |
| `/api/billing/wallet/topup` | POST | 用綁定信用卡儲值 | `{ success, new_balance }` |
| `/api/billing/wallet/topup-plans` | POST | 取得可用儲值方案 | `[{ id, amount, bonus, price, label }]` |
| `/api/billing/invoices` | POST | 列出用戶 invoices（分頁） | `{ data: [...], total, page }` |
| `/api/billing/invoices/<id>/pay` | POST | 用 wallet 付單張 invoice | `{ success, new_balance }` |
| `/api/billing/subscriptions` | POST | 列出 active 訂閱 | `[{ id, name, plan, state, next_payment, price, card }]` |
| `/api/billing/subscriptions/<id>/cancel` | POST | 取消訂閱 | `{ success }` |
| `/api/billing/transactions` | POST | Wallet 交易紀錄（分頁） | `{ data: [...], total, page }` |

Header 的 credits 來自 `/api/billing/wallet`，在 app 載入 + 每次付款/儲值後刷新。

## 後台 Model（新增）

### TopupPlan（儲值方案）

```python
class TopupPlan(models.Model):
    _name = 'woow_paas_platform.topup_plan'

    name = fields.Char()            # "NT$ 100 Credits"
    amount = fields.Float()         # 100（購買金額）
    credits = fields.Float()        # 100（獲得點數）
    bonus = fields.Float()          # 5（bonus 點數）
    label = fields.Char()           # "BEST VALUE"
    category = fields.Selection()   # recommended / large_volume / custom
    sequence = fields.Integer()     # 排序
    active = fields.Boolean()
```

在 Settings → Woow PaaS 加一個 「Manage Top-up Plans」按鈕連到 list/form view。

## 餘額檢查邏輯

在以下操作前加 wallet 餘額檢查：

| 時機 | 檢查方式 | 餘額不足處理 |
|------|---------|-------------|
| 建立服務（skip trial 直接付費） | `_skip_trial()` 前檢查 | 拒絕，提示儲值 |
| Skip Trial 按鈕 | API 回傳前檢查 | 拒絕，提示儲值 |
| Invoice Pay Now | API 回傳前檢查 | 拒絕，提示儲值 |
| 月度自動扣款 (cron) | `_sh_create_auto_invoice` 後 | 建 invoice 但不自動扣款，標記 unpaid，通知用戶 |

Trial 期間不需要餘額（免費），試用結束進入計費時才檢查。

## Header Credits 改造

**檔案**: `src/static/src/paas/layout/header/Header.js` + `.xml`

- 移除寫死的 `$2,450.00`
- App 載入時 fetch `/api/billing/wallet` 取得餘額
- 顯示動態值
- 點 `+` 按鈕導航到 `#/billing/topup`
- 付款/儲值後觸發 refresh

## 前端元件結構

```
src/static/src/paas/
├── pages/
│   └── billing/
│       ├── BillingOverviewPage.js/xml      # Account Overview
│       ├── TopupStorePage.js/xml            # Top-up Store
│       ├── InvoiceListPage.js/xml           # Invoice 列表
│       └── TransactionListPage.js/xml      # 交易紀錄
├── components/
│   └── modal/
│       ├── TopupConfirmModal.js/xml         # 儲值確認
│       ├── PayInvoiceModal.js/xml           # Pay Now 確認
│       └── CancelSubscriptionModal.js/xml   # 取消訂閱確認
├── services/
│   └── billing_service.js                   # Billing API client
└── layout/
    └── header/Header.js/xml                 # 動態 credits
```

## 關鍵參考檔案

| 用途 | 檔案 |
|------|------|
| Wallet 餘額/扣款 | `extra/extra-addons/sh_subscription_wallet/models/res_partner.py` |
| Wallet 交易 model | `extra/extra-addons/sh_subscription_wallet/models/wallet_transaction.py` |
| ECPay 儲值 | `extra/extra-addons/sh_subscription_wallet/models/subscription_wallet_mixin.py` |
| Header (寫死 credits) | `src/static/src/paas/layout/header/Header.js` + `.xml` |
| Sidebar | `src/static/src/paas/layout/sidebar/Sidebar.js` + `.xml` |
| Router | `src/static/src/paas/core/router.js` |
| Root (route dispatch) | `src/static/src/paas/root.js` |
| Modal 範本 | `src/static/src/paas/components/modal/DeleteServiceModal.js` |
| Controller 範本 | `src/controllers/paas.py` |
| 現有 skip-trial | `src/models/subscription_lifecycle_mixin.py` (_skip_trial) |
| 現有 change-plan | `src/controllers/paas.py` (api_service_change_plan, line 853) |
