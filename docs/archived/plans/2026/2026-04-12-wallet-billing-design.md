---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-12-wallet-billing-design.md
archived: 2026-07-19
archived-reason: "已落地；設計論述現行權威=docs/decisions/philosophy/billing-philosophy.md"
---

# Wallet & Billing Dashboard Design (v2)

## Context

PaaS 平台的訂閱計費系統需要完整的前端介面。目前 Header 的 CREDITS 是寫死的，Billing 頁面只有空 route，沒有 invoice/wallet/subscription/payment method 管理 UI。後端 wallet 模組（`sh_subscription_wallet`）已完整，包含儲值、扣款、ECPay 信用卡綁定。

## 設計決策

| 決策 | 選項 | 理由 |
|------|------|------|
| Pay Now 付款方式 | 只用 wallet 點數 | 簡化流程，不夠就提示儲值 |
| 儲值流程 | Add Funds modal（金額 + 付款方式 + 確認）| 財務操作需明確確認 |
| 信用卡管理 | **在 SPA 內做**（Add Card modal + Manage modal）| 完整體驗，不跳出 SPA |
| 儲值方案 | 後台 Odoo 可設定 | 靈活調整方案和 bonus |
| Sidebar 導航 | 拆成多個入口 | Billing / Invoices / Top-up Store 各自獨立 |

## 路由結構

```
#/billing                  → Account Overview（餘額卡片 + 訂閱 + 近期發票）
#/billing/topup            → Credit Top-up Store
#/billing/invoices         → Invoice 列表
#/billing/invoices/:id     → Invoice Detail（發票明細）
#/billing/subscriptions    → Subscription Management
#/billing/payment-methods  → Payment Method 管理
#/billing/transactions     → Wallet 交易紀錄
```

Sidebar 新增（在 AI Assistant 下方，Settings 上方）：

```
── Finance ──────────
  payments      Billing        → #/billing
  receipt_long  Invoices       → #/billing/invoices
  storefront    Top-up Store   → #/billing/topup
```

## 頁面設計

### 1. Account Overview (`#/billing`)

三欄 summary 卡片 + 訂閱區塊 + 近期發票。

**Summary Cards (3-col grid):**
- **Available Credits** — 餘額數字 + auto-reload 狀態
- **Current Usage** — 本月已用 + 預估總額
- **Default Method** — Visa •••• 4242 + [Update →] 連結

**Subscription & Payment 區塊：**
- 訂閱卡片（SUB ID、plan name、status badge、付款方式）
- 點擊卡片 → 導向 `#/billing/subscriptions` 或展開詳情
- 提示文字：「點擊訂閱名稱查看詳情」

**Recent Invoices 區塊：**
- 最近 2-3 筆 invoice（Invoice #、日期、金額、status badge、download）
- [View All →] 連結到 `#/billing/invoices`

**底部 2-col grid：**
- About WoowTech 區塊
- Billing Support 區塊（Live Chat + Email）

### 2. Top-up Store (`#/billing/topup`)

- Header 顯示目前餘額（Balance: NT$ 125.00）
- 搜尋欄 + 篩選 tabs（All Packs / Recommended / Large Volume / Custom Amount）
- Grid/List 切換
- 儲值方案卡片（4-col grid，從後台 model 載入）：
  - icon、credit 金額、bonus 描述、價格、BEST VALUE 標籤
  - [Purchase] 按鈕 → 開啟 Add Funds Modal
- 底部：About Credit Points + Billing Support 區塊

### 3. Invoice 列表 (`#/billing/invoices`)

- 欄位：Invoice #, Date, Due Date, Amount, Status
- Status badges: Draft (灰), Waiting for Payment (藍), Paid (綠), Overdue (紅)
- Pay Now 按鈕（Waiting/Overdue 狀態）→ wallet 扣款
- 排序 / 篩選（Date, Amount, Status / Invoices, Quotes, Credit Notes）
- 點擊 invoice row → 導向 `#/billing/invoices/:id`
- 分頁

### 4. Invoice Detail (`#/billing/invoices/:id`)

兩欄佈局：

**Left sidebar (sticky)：**
- Total Amount（大字）
- Due in X days badge
- [Pay Now] 按鈕
- [Download PDF] 按鈕
- Sales Representative 資訊

**Main content：**
- 公司資訊（WoowTech ↔ 客戶）
- Invoice header（#、Status badge）
- 日期欄位（Invoice Date、Due Date、Source、Reference）
- Line items table（Description、Quantity、Unit Price、Taxes、Amount）
- Summary（Untaxed、Taxes、Total）
- Terms & Conditions

### 5. Subscription Management (`#/billing/subscriptions`)

- Active / Past / All tab 切換
- 訂閱卡片清單，每張卡片：
  - icon、Plan ID、Plan Name、Start/End Date
  - Status badge（In Progress / Active / Trial / Expired）
  - Monthly Usage 進度條（可選）
  - [Manage] 按鈕 → 展開/導向詳情
  - [Close] 按鈕 → CancelSubscriptionModal
- [New Subscription] 按鈕（導向 marketplace）
- 分頁

**Current Subscribed Services Modal：**
- 點 [Manage] 時顯示
- 總月費 + 預估用量
- 服務清單（service name、workspace、status、next bill、price/mo）

### 6. Payment Method 管理 (`#/billing/payment-methods`)

**Manage Payment Methods Modal（或獨立頁面）：**
- Active Method（radio selected、Default badge）
- Other Methods 清單（Set Default / Delete）
- [+ Add Payment Method] 按鈕

**Add Credit Card Modal：**
- Card Number（帶 lock icon）
- Expiry Date + CVC
- Cardholder Name
- [Set as default] checkbox
- 支援的卡片 logo（VISA / MC / AMEX）
- [Save Card] 按鈕 → ECPay tokenization
- 「Secured by WoowTech Payment Gateway」提示

**Credit Card Added Success Modal：**
- Check circle icon
- 「Your new Visa •••• 4242 has been added and set as default」
- [Done] 按鈕

### 7. Transaction 紀錄 (`#/billing/transactions`)

- 欄位：icon、Description、Date、Amount (+/-)、Balance
- 交易類型 icon：
  - wallet icon（藍）→ Credit Top-up
  - redeem icon（紫）→ Promotion / Bonus
  - arrow_outward icon（灰）→ Usage / Deduction
- 來源：`wallet.transaction` model
- 分頁

**Credit Point History Modal（替代方案）：**
- 可從 Account Overview 的餘額區塊打開
- 顯示 Current Balance + [Add Funds] 按鈕
- 交易明細清單

## Modals 清單

| Modal | 觸發位置 | 功能 |
|-------|---------|------|
| AddFundsModal | Top-up Store [Purchase]、Header [+]、Overview [Top-up] | 金額輸入 + 付款方式選擇 + 確認扣款 |
| TopupSuccessModal | AddFundsModal 成功後 | 「$50.00 credits added」確認 |
| AddCardModal | Payment Methods [+ Add]、AddFundsModal 無綁卡時 | 信用卡表單 + ECPay tokenization |
| CardAddedSuccessModal | AddCardModal 成功後 | 綁卡成功確認 |
| ManagePaymentMethodsModal | Overview [Update] 連結 | 切換預設卡 / 刪除卡 / 新增卡 |
| PayInvoiceModal | Invoice [Pay Now] | 確認用 wallet 付款 |
| CancelSubscriptionModal | Subscription [Close] | 確認取消訂閱 |
| SubscribedServicesModal | Subscription [Manage] | 顯示訂閱下的服務清單 |
| CreditHistoryModal | Overview 餘額區塊 [View History] | 快速查看交易紀錄 |

## API Endpoints（新增）

| Endpoint | Method | 用途 |
|----------|--------|------|
| `/api/billing/wallet` | POST | Wallet 餘額 + 信用卡列表 |
| `/api/billing/wallet/topup` | POST | 用指定信用卡儲值 |
| `/api/billing/wallet/topup-plans` | POST | 取得可用儲值方案 |
| `/api/billing/invoices` | POST | 列出用戶 invoices（分頁） |
| `/api/billing/invoices/<id>` | POST | 取得單張 invoice 明細 |
| `/api/billing/invoices/<id>/pay` | POST | 用 wallet 付單張 invoice |
| `/api/billing/subscriptions` | POST | 列出訂閱（支援 filter: active/past/all） |
| `/api/billing/subscriptions/<id>` | POST | 取得單個訂閱 + 關聯服務 |
| `/api/billing/subscriptions/<id>/cancel` | POST | 取消訂閱 |
| `/api/billing/transactions` | POST | Wallet 交易紀錄（分頁） |
| `/api/billing/payment-methods` | POST | 列出已綁定的付款方式 |
| `/api/billing/payment-methods/add` | POST | 新增信用卡（ECPay tokenization） |
| `/api/billing/payment-methods/<id>/delete` | POST | 刪除付款方式 |
| `/api/billing/payment-methods/<id>/set-default` | POST | 設為預設 |

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

## 餘額檢查邏輯

| 時機 | 檢查方式 | 餘額不足處理 |
|------|---------|-------------|
| Skip Trial 按鈕 | API 回傳前檢查 | 拒絕，提示儲值 |
| Invoice Pay Now | API 回傳前檢查 | 拒絕，提示儲值 |
| 月度自動扣款 (cron) | `_sh_create_auto_invoice` 後 | 建 invoice 但不自動扣款，標記 unpaid |

Trial 期間不需要餘額（免費），試用結束進入計費時才檢查。

## Header Credits 改造

- 移除寫死的 `$2,450.00`
- App 載入時 fetch `/api/billing/wallet`
- 動態顯示餘額
- 點 `+` 按鈕 → 開啟 AddFundsModal 或導向 `#/billing/topup`
- 付款/儲值後自動 refresh

## 前端元件結構

```
src/static/src/paas/
├── pages/
│   └── billing/
│       ├── BillingOverviewPage.js/xml       # Account Overview
│       ├── TopupStorePage.js/xml             # Top-up Store
│       ├── InvoiceListPage.js/xml            # Invoice 列表
│       ├── InvoiceDetailPage.js/xml          # Invoice 明細
│       ├── SubscriptionListPage.js/xml       # Subscription Management
│       ├── PaymentMethodPage.js/xml          # Payment Method 管理
│       └── TransactionListPage.js/xml        # 交易紀錄
├── components/
│   └── modal/
│       ├── AddFundsModal.js/xml              # 儲值（金額+付款方式+確認）
│       ├── TopupSuccessModal.js/xml          # 儲值成功
│       ├── AddCardModal.js/xml               # 新增信用卡
│       ├── CardAddedSuccessModal.js/xml      # 綁卡成功
│       ├── ManagePaymentMethodsModal.js/xml  # 管理付款方式
│       ├── PayInvoiceModal.js/xml            # Pay Now 確認
│       ├── CancelSubscriptionModal.js/xml    # 取消訂閱
│       ├── SubscribedServicesModal.js/xml     # 訂閱服務清單
│       └── CreditHistoryModal.js/xml         # 交易紀錄快查
├── services/
│   └── billing_service.js                    # Billing API client
└── layout/
    └── header/Header.js/xml                  # 動態 credits
```

## 實作分期建議

### Phase 1: 基礎建設
- 動態 Header credits
- `/api/billing/wallet` endpoint
- `billing_service.js` API client
- Sidebar 路由調整
- BillingOverviewPage（靜態框架）

### Phase 2: Invoice 功能
- `/api/billing/invoices` + `/<id>` endpoints
- InvoiceListPage + InvoiceDetailPage
- PayInvoiceModal（wallet 扣款）
- 餘額檢查邏輯

### Phase 3: Top-up 儲值
- TopupPlan model + 後台管理 + 預設資料
- `/api/billing/wallet/topup-plans` + `/topup` endpoints
- TopupStorePage
- AddFundsModal + TopupSuccessModal

### Phase 4: 信用卡管理
- `/api/billing/payment-methods` endpoints
- AddCardModal（ECPay tokenization）
- ManagePaymentMethodsModal
- PaymentMethodPage

### Phase 5: 訂閱管理
- `/api/billing/subscriptions` endpoints
- SubscriptionListPage（Active/Past/All tabs）
- CancelSubscriptionModal
- SubscribedServicesModal

### Phase 6: 交易紀錄
- `/api/billing/transactions` endpoint
- TransactionListPage
- CreditHistoryModal

## 關鍵參考檔案

| 用途 | 檔案 |
|------|------|
| Wallet 餘額/扣款 | `extra/extra-addons/sh_subscription_wallet/models/res_partner.py` |
| Wallet 交易 model | `extra/extra-addons/sh_subscription_wallet/models/wallet_transaction.py` |
| ECPay 信用卡綁定 | `extra/extra-addons/sh_subscription_wallet/models/subscription_wallet_mixin.py` |
| ECPay Portal 頁面 | `extra/extra-addons/sh_subscription_wallet/views/portal_templates.xml` |
| Header (寫死 credits) | `src/static/src/paas/layout/header/Header.js` + `.xml` |
| Sidebar | `src/static/src/paas/layout/sidebar/Sidebar.js` + `.xml` |
| Router | `src/static/src/paas/core/router.js` |
| Root (route dispatch) | `src/static/src/paas/root.js` |
| Modal 範本 | `src/static/src/paas/components/modal/DeleteServiceModal.js` |
| Controller 範本 | `src/controllers/paas.py` |
| 現有 skip-trial | `src/models/subscription_lifecycle_mixin.py` (_skip_trial) |
| 現有 change-plan | `src/controllers/paas.py` (api_service_change_plan, line 853) |

## UI 參考 Mockup

所有頁面和 modal 的 HTML mockup 已由使用者提供，包含：
- Account Overview（含 sidebar、header、summary cards）
- Top-up Store（credit packages grid）
- Invoice 列表 + Invoice Detail（line items table）
- Subscription Management（Active/Past/All tabs）
- Payment Method 選擇頁
- Add Credit Card modal、Add Funds modal
- Pre-payment Success modal、Card Added Success modal
- Manage Payment Methods modal
- Credit Point History modal
- Current Subscribed Services modal
- Billing & Finance Overview（多個風格變體）
