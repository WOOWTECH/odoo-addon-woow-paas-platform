---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-12-wallet-billing-implementation.md
archived: 2026-07-19
archived-reason: "已落地；設計論述現行權威=docs/decisions/philosophy/billing-philosophy.md"
---

# Wallet & Billing Dashboard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a complete billing dashboard with wallet credits, invoices, subscriptions, top-up store, and payment method management in the PaaS OWL frontend.

**Architecture:** Backend exposes billing data through new API endpoints in a dedicated `billing.py` controller, reusing `sh_subscription` and `sh_subscription_wallet` models. Frontend adds 7 new page components + 9 modals + 1 API service, following existing OWL patterns (WoowButton, WoowCard, Modal backdrop pattern). One new Odoo model (`topup_plan`) for configurable top-up packages.

**Tech Stack:** Odoo 18 (Python), OWL 2 (JS), SCSS, JSON-RPC

**Design Doc:** `docs/plans/2026-04-12-wallet-billing-design.md`

---

## Phase 1: Foundation (Header + Wallet API + Routing + Overview Shell)

### Task 1.1: Billing Controller + Wallet API Endpoint

**Files:**
- Create: `src/controllers/billing.py`
- Modify: `src/controllers/__init__.py`

**Steps:**

1. Create `src/controllers/billing.py` with class `BillingController(Controller)`:
   - Route `POST /api/billing/wallet` (auth="user", type="json")
   - Returns `{ success, data: { balance, card_bound, card_last4, card_expiry } }`
   - Uses `request.env.user.partner_id.wallet_balance` for balance
   - Uses `partner.ecpay_card_token`, `partner.ecpay_card_last4` for card info

2. Add import in `src/controllers/__init__.py`

3. Commit: `feat: add billing controller with wallet endpoint`

### Task 1.2: Billing Service (Frontend API Client)

**Files:**
- Create: `src/static/src/paas/services/billing_service.js`

**Steps:**

1. Create `billing_service.js` following `cloud_service.js` pattern:
   - `reactive({})` with methods
   - `fetchWallet()` → POST `/api/billing/wallet`
   - Store `wallet: { balance, cardBound, cardLast4, cardExpiry }`
   - Export `billingService`

2. Commit: `feat: add billing_service.js API client`

### Task 1.3: Dynamic Header Credits

**Files:**
- Modify: `src/static/src/paas/layout/header/Header.js`
- Modify: `src/static/src/paas/layout/header/Header.xml`

**Steps:**

1. Import `billingService` in Header.js
2. Call `billingService.fetchWallet()` in `onMounted`
3. Replace hardcoded `$2,450.00` in Header.xml with `billingService.wallet.balance`
4. Wire `+` button to navigate to `#/billing/topup`

5. Commit: `feat: dynamic header credits from wallet API`

### Task 1.4: Sidebar Finance Section + Routes

**Files:**
- Modify: `src/static/src/paas/layout/sidebar/Sidebar.js`
- Modify: `src/static/src/paas/layout/sidebar/Sidebar.xml`
- Modify: `src/static/src/paas/core/router.js`

**Steps:**

1. Add routes to `router.js`:
   ```javascript
   { path: "billing", name: "Billing", routeName: "billing" },
   { path: "billing/topup", name: "Top-up Store", routeName: "billing-topup" },
   { path: "billing/invoices", name: "Invoices", routeName: "billing-invoices" },
   { path: "billing/invoices/:id", name: "Invoice Detail", routeName: "billing-invoice-detail", pattern: /^billing\/invoices\/(\d+)$/ },
   { path: "billing/subscriptions", name: "Subscriptions", routeName: "billing-subscriptions" },
   { path: "billing/payment-methods", name: "Payment Methods", routeName: "billing-payment-methods" },
   { path: "billing/transactions", name: "Transactions", routeName: "billing-transactions" },
   ```

2. Add Finance section to Sidebar with separator label "Finance":
   - `payments` icon → Billing (#/billing)
   - `receipt_long` icon → Invoices (#/billing/invoices)
   - `storefront` icon → Top-up Store (#/billing/topup)

3. Commit: `feat: add billing routes and sidebar finance section`

### Task 1.5: BillingOverviewPage Shell + Root Wiring

**Files:**
- Create: `src/static/src/paas/pages/billing/BillingOverviewPage.js`
- Create: `src/static/src/paas/pages/billing/BillingOverviewPage.xml`
- Create: `src/static/src/paas/pages/billing/BillingOverviewPage.scss`
- Modify: `src/static/src/paas/root.js`
- Modify: `src/static/src/paas/root.xml`

**Steps:**

1. Create BillingOverviewPage component with static template rendering:
   - 3-col summary cards (Available Credits, Current Usage, Default Method)
   - Subscription & Payment section (placeholder)
   - Recent Invoices section (placeholder)
   - About + Support footer cards
   - All data fetched via `billingService`

2. Wire in root.js: import component, add to `static components`, add route case in root.xml

3. Commit: `feat: add BillingOverviewPage shell with summary cards`

### Task 1.6: Deploy & Verify Phase 1

**Steps:**

1. Push to develop, trigger CI, deploy to STG
2. Browser test: navigate to `#/billing`, verify:
   - Header shows dynamic credits (from wallet API)
   - Sidebar has Finance section with 3 links
   - BillingOverviewPage renders with summary cards
3. Commit any fixes

---

## Phase 2: Invoice Functionality

### Task 2.1: Invoice API Endpoints

**Files:**
- Modify: `src/controllers/billing.py`

**Steps:**

1. Add route `POST /api/billing/invoices`:
   - Query `account.move` filtered by current user's partner
   - Support pagination (`page`, `limit`), sorting (`sort_by`, `sort_order`), filtering (`status`)
   - Return `{ success, data: [...], total, page, limit }`

2. Add route `POST /api/billing/invoices/<id>`:
   - Return full invoice with line items from `account.move.line`
   - Include: dates, partner info, company info, status, totals

3. Add route `POST /api/billing/invoices/<id>/pay`:
   - Check wallet balance >= invoice amount
   - Call `partner.deduct_wallet_points(amount, description, subscription)`
   - Post invoice via `invoice.action_post()` if draft
   - Reconcile payment
   - Return `{ success, new_balance }`

4. Commit: `feat: add invoice list, detail, and pay endpoints`

### Task 2.2: Billing Service — Invoice Methods

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js`

**Steps:**

1. Add methods:
   - `fetchInvoices(page, limit, sortBy, status)` → POST `/api/billing/invoices`
   - `fetchInvoice(id)` → POST `/api/billing/invoices/${id}`
   - `payInvoice(id)` → POST `/api/billing/invoices/${id}/pay`
2. Store `invoices`, `invoiceDetail`, `invoicePagination`

3. Commit: `feat: add invoice methods to billing service`

### Task 2.3: InvoiceListPage

**Files:**
- Create: `src/static/src/paas/pages/billing/InvoiceListPage.js`
- Create: `src/static/src/paas/pages/billing/InvoiceListPage.xml`
- Create: `src/static/src/paas/pages/billing/InvoiceListPage.scss`
- Modify: `src/static/src/paas/root.js` + `root.xml`

**Steps:**

1. Create InvoiceListPage following the mockup:
   - Column headers: Invoice #, Date, Due Date, Amount, Status
   - Status badges (Draft=gray, Waiting=blue, Paid=green, Overdue=red)
   - Pay Now button for unpaid invoices
   - Sort by / Filter dropdowns
   - Click row → navigate to `#/billing/invoices/${id}`
   - Pagination component

2. Wire in root.js/xml

3. Commit: `feat: add InvoiceListPage with filters and pagination`

### Task 2.4: InvoiceDetailPage

**Files:**
- Create: `src/static/src/paas/pages/billing/InvoiceDetailPage.js`
- Create: `src/static/src/paas/pages/billing/InvoiceDetailPage.xml`
- Create: `src/static/src/paas/pages/billing/InvoiceDetailPage.scss`
- Modify: `src/static/src/paas/root.js` + `root.xml`

**Steps:**

1. Create InvoiceDetailPage with 2-column layout (mockup reference):
   - Left sticky sidebar: Total Amount, Due badge, Pay Now, Download PDF, Rep info
   - Main: Company ↔ Customer info, Invoice header, dates, line items table, totals
2. Wire in root.js/xml, extract `invoiceId` from `router.params`

3. Commit: `feat: add InvoiceDetailPage with line items and pay action`

### Task 2.5: PayInvoiceModal

**Files:**
- Create: `src/static/src/paas/components/modal/PayInvoiceModal.js`
- Create: `src/static/src/paas/components/modal/PayInvoiceModal.xml`

**Steps:**

1. Create PayInvoiceModal (follow DeleteServiceModal pattern):
   - Shows: invoice #, amount, current wallet balance
   - If balance insufficient: show warning + link to top-up
   - Confirm button calls `billingService.payInvoice(id)`
   - On success: refresh wallet balance + close modal

2. Commit: `feat: add PayInvoiceModal with balance check`

### Task 2.6: Wire Invoices into BillingOverviewPage

**Files:**
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.js`
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.xml`

**Steps:**

1. Fetch recent invoices (limit=3) on mount
2. Render in Recent Invoices section with status badges
3. Add "View All →" link to `#/billing/invoices`

4. Commit: `feat: wire recent invoices into billing overview`

### Task 2.7: Deploy & Verify Phase 2

---

## Phase 3: Top-up Store

### Task 3.1: TopupPlan Model + Data + Admin Views

**Files:**
- Create: `src/models/topup_plan.py`
- Modify: `src/models/__init__.py`
- Create: `src/data/topup_plans.xml`
- Create: `src/views/topup_plan_views.xml`
- Modify: `src/views/res_config_settings_views.xml`
- Modify: `src/__manifest__.py` (data + views)
- Modify: `src/security/ir.model.access.csv`

**Steps:**

1. Create TopupPlan model with fields: name, amount, credits, bonus, label, category (selection: all/recommended/large_volume), sequence, active
2. Add default data (NT$50, NT$100+5bonus, NT$500+30bonus, NT$1000+100bonus)
3. Add list/form views and menu action
4. Add "Manage Top-up Plans" button in Settings → Woow PaaS
5. Add security access rules

6. Commit: `feat: add TopupPlan model with default plans and admin views`

### Task 3.2: Top-up API Endpoints

**Files:**
- Modify: `src/controllers/billing.py`

**Steps:**

1. Add route `POST /api/billing/wallet/topup-plans`:
   - Returns active TopupPlan records sorted by sequence
   - Optional `category` filter

2. Add route `POST /api/billing/wallet/topup`:
   - Params: `plan_id` (or `amount` for custom)
   - Validates card is bound
   - Calls ECPay charge via existing `_process_wallet_payment` or `add_wallet_points`
   - Returns `{ success, new_balance, credits_added, bonus }`

3. Commit: `feat: add topup-plans and topup endpoints`

### Task 3.3: Billing Service — Top-up Methods

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js`

**Steps:**

1. Add: `fetchTopupPlans(category)`, `topup(planId)`, `topupCustom(amount)`
2. Store: `topupPlans`

3. Commit: `feat: add topup methods to billing service`

### Task 3.4: TopupStorePage

**Files:**
- Create: `src/static/src/paas/pages/billing/TopupStorePage.js`
- Create: `src/static/src/paas/pages/billing/TopupStorePage.xml`
- Create: `src/static/src/paas/pages/billing/TopupStorePage.scss`
- Modify: `src/static/src/paas/root.js` + `root.xml`

**Steps:**

1. Create TopupStorePage following mockup:
   - Balance header bar
   - Search + category filter tabs + grid/list toggle
   - Plan cards in 4-col grid (icon, credits, bonus tag, price, BEST VALUE label, Purchase button)
   - Bottom info cards (About Credits + Billing Support)
2. Wire in root

3. Commit: `feat: add TopupStorePage with plan cards and filters`

### Task 3.5: AddFundsModal + TopupSuccessModal

**Files:**
- Create: `src/static/src/paas/components/modal/AddFundsModal.js`
- Create: `src/static/src/paas/components/modal/AddFundsModal.xml`
- Create: `src/static/src/paas/components/modal/TopupSuccessModal.js`
- Create: `src/static/src/paas/components/modal/TopupSuccessModal.xml`

**Steps:**

1. AddFundsModal:
   - Amount display (from selected plan or custom input)
   - Payment method display (default card: Visa •••• last4)
   - Total to pay summary
   - "Pre-pay & Get Credits" button → calls `billingService.topup(planId)`
   - If no card bound → show warning + link to add card
   - On success → show TopupSuccessModal

2. TopupSuccessModal:
   - Check circle icon
   - "$X credits added to your account" message
   - [Done] button → close + refresh wallet

3. Commit: `feat: add AddFundsModal and TopupSuccessModal`

### Task 3.6: Deploy & Verify Phase 3

---

## Phase 4: Payment Method Management

### Task 4.1: Payment Method API Endpoints

**Files:**
- Modify: `src/controllers/billing.py`

**Steps:**

1. `POST /api/billing/payment-methods` — list bound cards from partner ECPay fields
2. `POST /api/billing/payment-methods/add` — trigger ECPay card tokenization (reuse logic from `sh_subscription_wallet` portal controller)
3. `POST /api/billing/payment-methods/<id>/delete` — unbind card
4. `POST /api/billing/payment-methods/<id>/set-default` — set as default

5. Commit: `feat: add payment method CRUD endpoints`

### Task 4.2: AddCardModal + CardAddedSuccessModal

**Files:**
- Create: `src/static/src/paas/components/modal/AddCardModal.js`
- Create: `src/static/src/paas/components/modal/AddCardModal.xml`
- Create: `src/static/src/paas/components/modal/CardAddedSuccessModal.js`
- Create: `src/static/src/paas/components/modal/CardAddedSuccessModal.xml`

**Steps:**

1. AddCardModal (mockup reference):
   - Card number input (lock icon, card brand logos)
   - Expiry + CVC row
   - Cardholder name
   - "Set as default" checkbox
   - "Save Card" button → calls payment-methods/add API
   - "Secured by WoowTech Payment Gateway" footer
   - On success → show CardAddedSuccessModal

2. CardAddedSuccessModal:
   - Check circle + "Visa •••• XXXX added and set as default"
   - [Done] button

3. Commit: `feat: add AddCardModal and CardAddedSuccessModal`

### Task 4.3: ManagePaymentMethodsModal

**Files:**
- Create: `src/static/src/paas/components/modal/ManagePaymentMethodsModal.js`
- Create: `src/static/src/paas/components/modal/ManagePaymentMethodsModal.xml`

**Steps:**

1. ManagePaymentMethodsModal (mockup reference):
   - Active method with Default badge
   - Other methods list with "Set Default" / Delete actions
   - "+ Add Payment Method" dashed button → opens AddCardModal
2. Wire into BillingOverviewPage "Update" link on Default Method card

3. Commit: `feat: add ManagePaymentMethodsModal`

### Task 4.4: PaymentMethodPage (optional standalone page)

**Files:**
- Create: `src/static/src/paas/pages/billing/PaymentMethodPage.js`
- Create: `src/static/src/paas/pages/billing/PaymentMethodPage.xml`
- Modify: `src/static/src/paas/root.js` + `root.xml`

**Steps:**

1. Full page version of payment method management (for `#/billing/payment-methods` route)
2. Reuses ManagePaymentMethodsModal content as inline section

3. Commit: `feat: add PaymentMethodPage`

### Task 4.5: Deploy & Verify Phase 4

---

## Phase 5: Subscription Management

### Task 5.1: Subscription API Endpoints

**Files:**
- Modify: `src/controllers/billing.py`

**Steps:**

1. `POST /api/billing/subscriptions`:
   - Query `sh.subscription.subscription` for current user's partner
   - Filter: `active` (in_progress), `past` (expire/close/cancel), `all`
   - Return subscription data + linked service info

2. `POST /api/billing/subscriptions/<id>`:
   - Single subscription detail + associated cloud services

3. `POST /api/billing/subscriptions/<id>/cancel`:
   - Set state to 'close' with reason
   - Trigger service suspension via lifecycle mixin

4. Commit: `feat: add subscription list, detail, and cancel endpoints`

### Task 5.2: Billing Service — Subscription Methods

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js`

**Steps:**

1. Add: `fetchSubscriptions(filter)`, `fetchSubscription(id)`, `cancelSubscription(id, reason)`
2. Store: `subscriptions`, `subscriptionDetail`

3. Commit: `feat: add subscription methods to billing service`

### Task 5.3: SubscriptionListPage

**Files:**
- Create: `src/static/src/paas/pages/billing/SubscriptionListPage.js`
- Create: `src/static/src/paas/pages/billing/SubscriptionListPage.xml`
- Create: `src/static/src/paas/pages/billing/SubscriptionListPage.scss`
- Modify: `src/static/src/paas/root.js` + `root.xml`

**Steps:**

1. Create SubscriptionListPage following mockup:
   - Active / Past / All tab pills
   - Subscription cards: icon, Plan ID, Plan Name, Start Date, Status badge, usage bar
   - [Manage] button → SubscribedServicesModal
   - [Close] button → CancelSubscriptionModal
   - [New Subscription] button → navigate to marketplace
   - Pagination
2. Wire in root

3. Commit: `feat: add SubscriptionListPage with tabs and actions`

### Task 5.4: CancelSubscriptionModal + SubscribedServicesModal

**Files:**
- Create: `src/static/src/paas/components/modal/CancelSubscriptionModal.js`
- Create: `src/static/src/paas/components/modal/CancelSubscriptionModal.xml`
- Create: `src/static/src/paas/components/modal/SubscribedServicesModal.js`
- Create: `src/static/src/paas/components/modal/SubscribedServicesModal.xml`

**Steps:**

1. CancelSubscriptionModal:
   - Warning icon + "Cancel subscription for [plan name]?"
   - Explanation of consequences (service suspension)
   - Optional reason textarea
   - Cancel + Confirm buttons

2. SubscribedServicesModal (mockup reference):
   - Total monthly cost + estimated usage header
   - Service list: icon, name, workspace, status badge, next bill, price/mo
   - Scrollable list for many services

3. Commit: `feat: add CancelSubscriptionModal and SubscribedServicesModal`

### Task 5.5: Wire Subscriptions into BillingOverviewPage

**Files:**
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.js`
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.xml`

**Steps:**

1. Fetch user subscriptions on mount
2. Render Subscription & Payment section with subscription cards
3. Show payment method on each card

4. Commit: `feat: wire subscriptions into billing overview`

### Task 5.6: Deploy & Verify Phase 5

---

## Phase 6: Transaction History

### Task 6.1: Transaction API Endpoint

**Files:**
- Modify: `src/controllers/billing.py`

**Steps:**

1. `POST /api/billing/transactions`:
   - Query `wallet.transaction` for current user's partner
   - Support pagination
   - Return: date, description, type (credit/debit), amount, running balance

2. Commit: `feat: add wallet transaction list endpoint`

### Task 6.2: Billing Service — Transaction Methods

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js`

**Steps:**

1. Add: `fetchTransactions(page, limit)`
2. Store: `transactions`, `transactionPagination`

3. Commit: `feat: add transaction methods to billing service`

### Task 6.3: TransactionListPage

**Files:**
- Create: `src/static/src/paas/pages/billing/TransactionListPage.js`
- Create: `src/static/src/paas/pages/billing/TransactionListPage.xml`
- Create: `src/static/src/paas/pages/billing/TransactionListPage.scss`
- Modify: `src/static/src/paas/root.js` + `root.xml`

**Steps:**

1. Create TransactionListPage following mockup:
   - Row items: type icon (wallet=blue, redeem=purple, outward=gray), description, date, amount (+/-), balance
   - Pagination
2. Wire in root

3. Commit: `feat: add TransactionListPage`

### Task 6.4: CreditHistoryModal

**Files:**
- Create: `src/static/src/paas/components/modal/CreditHistoryModal.js`
- Create: `src/static/src/paas/components/modal/CreditHistoryModal.xml`

**Steps:**

1. CreditHistoryModal (quick-view from overview):
   - Current Balance header + [Add Funds] button
   - Transaction list (same data as TransactionListPage, limit=10)
   - Scrollable
2. Wire into BillingOverviewPage credit balance card

3. Commit: `feat: add CreditHistoryModal`

### Task 6.5: Balance Check Integration

**Files:**
- Modify: `src/models/subscription_lifecycle_mixin.py` (`_skip_trial`)
- Modify: `src/models/sh_subscription_fix.py` (`_sh_create_auto_invoice`)
- Modify: `src/controllers/billing.py` (pay endpoint)

**Steps:**

1. In `_skip_trial()`: check `partner.wallet_balance >= plan_price` before creating invoice
2. In `_sh_create_auto_invoice()`: if wallet balance insufficient, create invoice but don't auto-pay, log warning
3. In `/api/billing/invoices/<id>/pay`: check balance before deducting, return clear error if insufficient

4. Commit: `fix: add wallet balance checks before payment operations`

### Task 6.6: Final Deploy & End-to-End Verification

**Steps:**

1. Deploy all phases to STG
2. End-to-end test flow:
   - Header shows dynamic credits ✓
   - Billing Overview loads with all sections ✓
   - Top-up Store shows plans, Purchase → AddFunds → Success ✓
   - Invoice list with Pay Now (wallet deduction) ✓
   - Invoice detail with line items ✓
   - Subscription list with Active/Past tabs ✓
   - Cancel subscription flow ✓
   - Transaction history ✓
   - Balance check blocks payment when insufficient ✓

---

## File Summary

### New Files (31)

**Backend (4):**
- `src/controllers/billing.py`
- `src/models/topup_plan.py`
- `src/data/topup_plans.xml`
- `src/views/topup_plan_views.xml`

**Pages (7 × 3 = 21):**
- `src/static/src/paas/pages/billing/BillingOverviewPage.{js,xml,scss}`
- `src/static/src/paas/pages/billing/TopupStorePage.{js,xml,scss}`
- `src/static/src/paas/pages/billing/InvoiceListPage.{js,xml,scss}`
- `src/static/src/paas/pages/billing/InvoiceDetailPage.{js,xml,scss}`
- `src/static/src/paas/pages/billing/SubscriptionListPage.{js,xml,scss}`
- `src/static/src/paas/pages/billing/PaymentMethodPage.{js,xml,scss}`
- `src/static/src/paas/pages/billing/TransactionListPage.{js,xml,scss}`

**Modals (9 × 2 = 18, some without scss):**
- `AddFundsModal.{js,xml}`
- `TopupSuccessModal.{js,xml}`
- `AddCardModal.{js,xml}`
- `CardAddedSuccessModal.{js,xml}`
- `ManagePaymentMethodsModal.{js,xml}`
- `PayInvoiceModal.{js,xml}`
- `CancelSubscriptionModal.{js,xml}`
- `SubscribedServicesModal.{js,xml}`
- `CreditHistoryModal.{js,xml}`

**Service (1):**
- `src/static/src/paas/services/billing_service.js`

### Modified Files (10)
- `src/controllers/__init__.py`
- `src/models/__init__.py`
- `src/__manifest__.py`
- `src/security/ir.model.access.csv`
- `src/views/res_config_settings_views.xml`
- `src/static/src/paas/core/router.js`
- `src/static/src/paas/root.js` + `root.xml`
- `src/static/src/paas/layout/header/Header.js` + `Header.xml`
- `src/static/src/paas/layout/sidebar/Sidebar.js` + `Sidebar.xml`
- `src/models/subscription_lifecycle_mixin.py`
- `src/models/sh_subscription_fix.py`
