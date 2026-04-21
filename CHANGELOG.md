# Changelog

`woow_paas_platform` 的所有重要變更都記錄在此。

格式參考 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。
版號遵循 Odoo 慣例 `{odoo_version}.{major}.{minor}.{patch}`；每個
tag 以 `v{version}` 形式推送，並觸發 `paas-odoo-ci` 打包 image。

## [Unreleased]

### 文件
- 新增 `CHANGELOG.md`，追溯涵蓋 `v18.0.1.0.4` 到 `v18.0.1.0.11`。
  (`7c87b49`)
- 在 `docs/plans/2026-04-19-e2e-test-results.md` 標記 Phase 2c #5 已
  在 staging 驗證完成。(`fb4ead0`)

## [18.0.1.0.11] — 2026-04-21

### 修復
- **Phase 2c #5** — 訂閱 auto-invoice 現在會實際從錢包扣款。
  先前 ECPay auto-topup 儲值成功後，invoice 雖已 post 卻停留在
  `payment_state=not_paid`。`sh_subscription_fix._handle_invoice_creation`
  現在會在 `wallet_balance >= amount_total` 時繞過 upstream 的
  post-only / dummy-payment 兩種模式，改呼叫共用 helper 扣款、
  post invoice 並 reconcile 應收帳款。(`93f7d63`)

### 變更
- 從 `BillingController.api_invoice_pay` 的 inline 流程抽出
  `account.move._pay_from_wallet(subscription=None)`。Controller
  簡化為 model method 的 thin wrapper；訂閱 cron 與使用者手動
  付款共用同一套「扣款 + post + reconcile」實作。新增 5 個 unit
  tests 獨立覆蓋 helper。(`b171228`)

## [18.0.1.0.10] — 2026-04-21

### 修復
- **Phase 2c #6 後續** — Header 頭像不再以原始 128×128 渲染；透過
  `object-fit: cover` + `overflow: hidden` 限制進 40×40 圓形。同時
  移除 `.o_woow_user_plan` 已失效的 CSS 規則（`e6d2945` 拿掉
  `PRO PLAN` span 後就變成 dead CSS）。(`0ec2789`)

## [18.0.1.0.9] — 2026-04-21

### 新增
- **Phase 2c #6** — `/woow#/settings/profile` 頁面讓登入使用者編輯
  自己的 `res.partner.name`、`email`、`phone`。後端新增
  `POST /api/me` + `POST /api/me/update` 端點，security invariant：
  「寫入一律對 `request.env.user.partner_id`，忽略 body 裡的任何
  `partner_id`」。9 個 HttpCase 測試涵蓋 contract（full-replace
  語意、驗證、security guard）。(`61ccd41`, `3bf0bf8`, `f89491d`,
  `f46f133`, `e6d2945`, `876cdcd`)
- 新的 reactive `profileService`（與 `billingService` pattern
  一致），被 ProfilePage form 與 Header 共享；`fetch()` 有
  in-flight 去重機制，確保同時 mount 的兩邊看到同一份 partner。

### 變更
- **Header** 透過 `profileService` 渲染真實 `partner.name` + 首字
  縮寫 / 頭像；移除寫死的 `Sarah Wilson` / `PRO PLAN` / `SW`。
  Settings 首頁在 Payment Methods 上方新增 Profile 卡片。

### 修復
- **Phase 2c #2** — `TestPaymentMethodsApi` 3 個失敗的 HttpCase
  修復，fixture 改用 `self.env.ref('base.partner_admin')` 建 token。
  HttpCase 的 `self.env.user` 預設是 `__system__`（partner=OdooBot），
  但 `self.authenticate('admin', 'admin')` 讓 HTTP session 認證為
  admin（partner=Administrator），partner 不匹配導致 endpoint filter
  查不到。(`39ee823`)
- `post_init_hook` 已在 `__manifest__.py` 宣告卻沒從 `src/__init__.py`
  匯出，導致新 DB 安裝時丟 `AttributeError`。補上 import。(`c850fad`)

### 文件
- Phase 2c #3 標記為 obsolete — 驗證 `PaymentMethodsListPage.xml`
  本來就已用 `is_default` 條件控制 `Default` badge 與 `Set Default`
  按鈕；doc 記載的是 fallback 加之前的過期觀察。(`027795a`)
- 補上 Phase 2c #1 staging 驗證紀錄。(`fcc9e7f`)

## [18.0.1.0.8] — 2026-04-20

本版本打包 Phase 2a（Payment Methods）、Phase 2b（Auto-topup）與
Phase 2c #1（3DS bind session 流失）— 跨日 E2E session 後一次發出。
版號 `1.0.7` 跳號未發佈。

### 新增
- **Phase 2a — Payment Methods UI + API**：`/woow#/settings/payment-methods`
  含列表、新增（透過 ECPG SDK 走 ECPay 3DS 綁卡）、設為預設、刪除。
  新端點 `/api/billing/payment-methods/*`（`prepare-bind`、`create-bind`、
  `bind-result/<trade_no>`、`<id>/delete`、`<id>/set-default`）。相依
  upstream `payment_ecpay_ecpg`。
- **Phase 2b — 訂閱續訂 auto-topup**：錢包餘額不足以支付即將到來
  的 invoice 時，`_attempt_auto_topup` 刷使用者綁定的 ECPG token
  替錢包儲值，然後繼續開 invoice。關鍵防護：ECPay reference 唯一化、
  savepoint 隔離（ECPay 已扣款但 wallet credit 失敗時不會整個
  rollback 導致重複扣款）、低餘額 email 透過 `woow_last_low_balance_warned_for`
  去重。
- 低餘額警告 cron — 續訂日前 3 天寄信通知錢包不足且無 auto-topup
  卡片的使用者。`BillingOverview` 同步顯示站內橫幅。
- Settings 入口頁（`/woow#/settings`）作為 Payment Methods（及未來
  Profile 等）的父層。

### 修復
- **Phase 2c #1** — 3DS 綁卡完成後不再被踢回 `/web/login`。在
  OrderResultURL bouncer 加 `save_session=False`，避免 Odoo 的
  `_save_session` hook 在 ECPay cross-site POST return 時，把
  使用者認證用的 session_id 蓋成一個新匿名 session。對齊 upstream
  `payment_mollie.mollie_return_from_checkout` 與
  `payment_aps.aps_return_from_checkout`。(`41a9e18`)
- ECPG 綁卡 bouncer 自行持久化 `payment.token`（sandbox 環境 S2S
  callback 不穩定觸發），並回 HTML+JS redirect 避開「303 + 跨站
  POST context 帶進 GET 導致 cookie 被 strip」的陷阱。
  (`99ca65e`, `8e89f08`)
- ECPay JS SDK `initialize` 不再丟 `TypeError: i is not a function`
  — 停止從 CDN preload `node-forge`；讓 `sdk-1.0.0.js` 自行載入
  內建 forge。(`574512e`)
- 送給 ECPay 前對 `partner.phone` 做 sanitize：剝除 `+/-/空白/括號`，
  避免 demo data 的 `+1 555-555-5555` 使綁卡請求被拒。已被 upstream
  `payment_ecpay_ecpg` `38fcf13` 接手處理，此處 workaround 現屬冗餘。
  (`0e15108`)
- Community Odoo 的預設 token fallback — `property_payment_token_id`
  是 Enterprise-only 欄位；沒有時挑最近一張 active 的 ECPG token，
  列表與 auto-topup 都能正確工作。(`984af0e`)
- Auto-topup reference 20 字元截斷、shortfall integer ceiling、
  專用 error code（no-token、3DS-required、SDK errors、
  wallet-credit-failed）。(`9a473fd`, `8579948`)
- ECPay `MerchantTradeDate` 改用 UTC+8（台灣），而非 server UTC。
- 首次安裝會自動設定訂閱 journal（`post_init_hook`），關掉「invoice
  cron 因缺 journal 靜默失敗」的地雷。

### 變更
- Payment Methods 的 list/delete/set-default 直接操作 `payment.token`
  （不再有自訂 wrapper），共用 DRY 的 `_get_owned_token` 擁有權 guard。
  到期日以 `MM/YY` 呈現。
- 低餘額 banner 改為 BillingOverview 的可關閉 alert。

## [18.0.1.0.6] — 2026-04-06

本版本是基礎建設 + 錢包/帳務基礎的大合併（約 100 個 commits）。
重點如下。

### 新增
- **錢包 & Billing dashboard** — `/woow#/billing` 包含 Overview、
  Top-up Store、Invoices（列表 + 詳細 + 錢包支付）、Subscriptions
  （列表 + 取消 + services modal）、交易歷史。Controller 在
  `src/controllers/billing.py`、前端 state store 在 `billing_service.js`。
- **Cloud Services 後端 views** — `woow_paas_platform.cloud_service`
  與 `woow_paas_platform.cloud_odoo_addon` 的 list 與 form views，
  掛在 Woow PaaS 選單結構下。
- **Odoo addon 選擇** — 雲端服務可從 `catalog.json` 同步的目錄
  中勾選特定 Odoo addon；含相依性 guard、安全移除對話框、部署後
  addon update 機制。
- **訂閱整合** — `sh.subscription.subscription` 及對應的產品 /
  方案 / 範本連結、lifecycle mixin、方案變更 wizard、skip-trial
  action。將 `sh_subscription` 與 `sh_subscription_wallet` vendor
  進 `extra/extra-addons/`。
- **三種 sandbox 模式**（local / ci-local / stg）對應正確的 addons
  paths、PVC lifecycle、Cloudflare tunnel ingress 支援、可設定的
  namespace 前綴、imagePullSecret 自動佈建、`--ingress` flag。
- `/woow:deploy-stg` skill 一鍵部署 staging：用 `tea` CLI 建
  git tag 觸發（取代先前的 API dispatch 方式）。

### 變更
- Cloud service deploy 從背景 thread 改為 `ir.cron`，避開
  `odoo.registry()` 的 deadlock。
- Cloud-odoo image registry 從內部 JCR path 改為對外 JCR domain，
  與其他 registry 保持一致。

### 修復
- Upstream `sh_subscription._sh_create_auto_invoice` bug：original
  版本在 `subscription` 迴圈中呼叫 `self._generate_invoice_vals()`
  （`@api.model` 下 self 是空 recordset），invoice 欄位全 NULL。
  Override 改傳 `subscription` 進去。(`04f4ced`)
- 其他多項小修 — 完整清單見 `git log v18.0.1.0.5..v18.0.1.0.6`。

## [18.0.1.0.5] — 2026-03-28

### 變更
- 將 `__manifest__.py` 所有字串統一改為雙引號。(`9e607ca`)

## [18.0.1.0.4] — 2026-03-28

第一個公開 tag。更早的歷史（commit `b631d77` 及之前）是模組骨架
（OWL SPA shell、Workspace / WorkspaceAccess models、Cloud Services
scaffolding、PaaS Operator client、Claude Code PM skills）。

[unreleased]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.11...develop
[18.0.1.0.11]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.10...v18.0.1.0.11
[18.0.1.0.10]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.9...v18.0.1.0.10
[18.0.1.0.9]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.8...v18.0.1.0.9
[18.0.1.0.8]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.6...v18.0.1.0.8
[18.0.1.0.6]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.5...v18.0.1.0.6
[18.0.1.0.5]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/compare/v18.0.1.0.4...v18.0.1.0.5
[18.0.1.0.4]: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/releases/tag/v18.0.1.0.4
