# Settings 頁面 + Payment Methods + ECPay 整合設計（兩階段）

**日期**：2026-04-16
**狀態**：Phase 1 待實作 / Phase 2 未來實作
**相關 Issue**：[WOOWTECH/ecpay_odoo18#10](https://github.com/WOOWTECH/ecpay_odoo18/issues/10)

## 起源

使用者瀏覽 `https://stg.woowtech.io/woow#/billing/payment-methods` 發現頁面顯示 "Coming Soon"，需要實作。調查後發現 `ManagePaymentMethodsModal` 已實作但從未被引用，且後端 `POST /api/billing/payment-methods/add` 是 stub（回傳 `ecpay_required` 錯誤）。

深度探討後確認：

1. 平台採用**錢包制**（Wallet-based billing）— 訂閱費從錢包餘額扣，不直接扣信用卡
2. ECPay 唯一介入時機是**儲值**（Top-up）
3. 若使用者手動儲值即可，**不需要綁卡功能**
4. 若要支援「餘額不足時自動從卡片補值」，則**需要 ECPG 綁卡**

## 整體架構決策

### 付款流程

```
使用者付錢 → 錢包餘額增加（Top-up via ECPay）
                    ↓
訂閱月費扣款 ← 從錢包餘額扣（內部交易，不經 ECPay）
雲端服務用量 ← 從錢包餘額扣（內部交易，不經 ECPay）
```

### 餘額不足策略（Option D: 混合模式）

```
訂閱扣款日 → 檢查錢包餘額
              ├── 足夠 → 直接扣款
              └── 不足 → 嘗試自動從綁定卡片補值
                          ├── 成功 → 補值 + 扣款
                          └── 失敗/無綁卡 → 通知使用者手動儲值
                                              ↓
                                    寬限期 N 天 → 逾期暫停服務
```

**關鍵**：「自動從卡片補值」需要 ECPG 綁卡能力，這是 Phase 2 的範圍。

### ECPay 技術路線

| 協議 | Domain | 用途 | 現況 |
|------|--------|------|------|
| AIO | `payment.ecpay.com.tw` | 手動儲值（跳轉付款） | ✅ `payment_ecpay` 模組已支援 |
| ECPG | `ecpg.ecpay.com.tw` | 綁卡 + 幕後扣款 | ❌ 需新增（Phase 2） |

兩者**共存且共用同一個 MerchantID/HashKey/HashIV**，不是「遷移」而是「新增能力」。

---

## Phase 1：Settings 入口頁 + 低餘額通知（✅ 已完成 — shipped 2026-04-16）

> 實作 PR：commits `4a36c8c..16c9d9a` on `develop`。執行計畫見 [`docs/plans/2026-04-16-settings-phase1-implementation.md`](./2026-04-16-settings-phase1-implementation.md)。
>
> 已交付：
> - 移除 `#/billing/payment-methods` 路由（含 AddFundsModal 死連結清理）
> - 新增 Settings 入口頁（`#/settings`），含 Phase 2 Payment Methods 佔位卡片
> - Mail template `mail_template_low_balance` + 扣款失敗時觸發通知
> - 每日 pre-renewal 警示 cron（3 天前檢查餘額，以 `woow_last_low_balance_warned_for` 去重）
> - BillingOverview 低餘額警示 banner，CTA 連到 Top-up Store
>
> Phase 2 起始點見本文件下半部。

### 範圍

此階段**不引入 ECPG**，只做以下事：

1. **清理現有路由**：`billing/payment-methods` → 改為 `settings/payment-methods`（或移除）
2. **Settings 入口頁**：卡片式列表（目前無可用項目，為 Phase 2 預留）
3. **低餘額通知機制**：訂閱扣款前檢查餘額，不足時通知使用者
4. **寬限期處理**：扣款失敗後的服務暫停邏輯
5. **BillingOverview 調整**：指引使用者到 Top-up Store 手動儲值
6. **移除未使用程式碼**：`ManagePaymentMethodsModal`（或保留備用，待 Phase 2 接回）

### Frontend 變更

| 操作 | 檔案 |
|------|------|
| 修改 | `core/router.js` — 移除 `billing/payment-methods`，新增 `settings` 的子路由邏輯（視 Phase 2 決定） |
| 修改 | `root.js` / `root.xml` — 新增 Settings 頁面元件掛載 |
| 新增 | `pages/settings/SettingsPage.js/xml/scss` — Settings 入口（卡片式列表） |
| 修改 | `layout/sidebar/Sidebar.js` — 更新 `isActive` 邏輯，移除 `billing-payment-methods` 引用 |
| 修改 | `pages/billing/BillingOverviewPage.js/xml` — 低餘額警示 + 引導到 Top-up |
| 保留 | `components/modal/ManagePaymentMethodsModal.js/xml/scss` — 保留原檔不刪除，Phase 2 接回 |

### Backend 變更

| 操作 | 檔案 |
|------|------|
| 修改 | `controllers/billing.py` — `/api/billing/payment-methods` 系列端點先保留（回傳空陣列/停用），Phase 2 改接 ECPG |
| 新增 | `models/workspace_subscription.py`（或相關 model）— 扣款前檢查餘額 + 失敗處理 + 寬限期欄位 |
| 新增 | 低餘額通知：mail template + cron job |
| 新增 | 寬限期 cron：逾期自動暫停服務 |

### Settings 頁面 UI

```
┌─────────────────────────────────────┐
│ ⚙ Settings                          │
│                                     │
│ ┌─────────────────────────────────┐ │
│ │ 💳 Payment Methods  [Coming Soon]│ │ ← Phase 2 實作，灰階顯示
│ │ Manage credit cards for auto    │ │
│ │ top-up when balance is low      │ │
│ └─────────────────────────────────┘ │
│                                     │
└─────────────────────────────────────┘
```

或更直接：Settings 頁面暫時顯示 Empty State 說明即將推出。由使用者確認 UX 偏好。

### 低餘額處理流程

```
訂閱到期前 3 天 cron → 檢查錢包餘額
                          ├── 足夠 → 無動作
                          └── 不足 → 發送「錢包餘額不足」通知
                                      ├── Email
                                      └── 平台內通知（bell icon）

訂閱到期日 cron → 嘗試扣款
                    ├── 成功 → 續訂
                    └── 失敗（餘額不足）→ 標記 grace_period_until = today + 7
                                           ├── 發送「扣款失敗」通知
                                           └── 顯示儲值 CTA

Grace period 到期 cron → 檢查是否已補值成功
                           ├── 是 → 續訂
                           └── 否 → 暫停服務（狀態改為 suspended）
```

---

## Phase 2：ECPG 綁卡 + 自動扣款（進行中）

> **更新 2026-04-19**：ecpay agent 已完成 `payment_ecpay_ecpg` addon（branch `feat/ecpg-phase3-subscription`），已整合進 `paas-odoo-ci` commit `f32c677`。Phase 2 分拆為 2a + 2b 兩個 PR。
>
> ### Phase 2a — Payment Methods UI（✅ 已完成 — shipped 2026-04-19）
>
> 實作 PR：commits `dc86e75..d232037` on `develop`。執行計畫見 [`docs/plans/2026-04-18-settings-phase2a-implementation.md`](./2026-04-18-settings-phase2a-implementation.md)。
>
> 已交付：
> - OWL `#/settings/payment-methods` 列表頁 + `#/settings/payment-methods/add` 綁卡子頁
> - 後端 `/api/billing/payment-methods/*` wrapper（直接 import `payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient`）
>   - `prepare-bind` — 取得 ECPay Token（OrderResultURL 導回 OWL）
>   - `create-bind` — 消費 BindCardPayToken，回 ThreeDURL
>   - `list` / `delete` / `set-default` — 基於 `payment.token`
> - 前端 ECPay JS SDK 動態載入（jQuery → node-forge → sdk-1.0.0.js）
> - 啟用 SettingsPage 的 Payment Methods 卡片
> - ManagePaymentMethodsModal 接回 BillingOverview 當快速入口
> - `payment_ecpay_ecpg` 加入 `__manifest__.py` depends
> - ECPay MerchantTradeDate 使用 UTC+8
>
> ### Phase 2b — Auto-topup 整合（✅ 已完成 — shipped 2026-04-19）
>
> 實作 commits：`da3a54a..2082a40` on `feat/settings-phase2a-payment-methods`。執行計畫見 [`docs/plans/2026-04-19-settings-phase2b-implementation.md`](./2026-04-19-settings-phase2b-implementation.md)。
>
> 已交付：
> - `_attempt_auto_topup(partner, shortfall)` helper on `sh.subscription.subscription`（via `ShSubscriptionFix` inherit）
> - 掛到 `_sh_create_auto_invoice` 不足分支：先 auto-topup，成功則續扣訂閱費；失敗保留 `_notify_low_balance`
> - 8 個失敗路徑覆蓋（no_shortfall / no_token / provider_not_enabled / currency_not_configured / 3ds_required / failed state / sdk_error / ok）
> - 4 個新測試：happy path + 3 failure branches + 2 source-assertion regression guards
> - Dead code 清理：`AddCardModal.{js,xml,scss}` 和 `CardAddedSuccessModal.{js,xml,scss}` 刪除
>
> **更新 2026-04-19**：範圍與設計決策確認。
>
> 核心：訂閱扣款失敗 fallback，先嘗試從綁定卡片扣款補值。
>
> **設計決策**：
> - **(A) 金額策略** — 精準扣差額（訂閱費 − 餘額 = 應扣金額）
> - **(X) 發動方式** — 同步 inline：訂閱 cron 發現餘額不足時當場呼叫 `charge_with_token`，成功則補值後立刻完成訂閱扣款
> - **(P) 卡片選擇** — `property_payment_token_id`（Phase 2a 已建立的預設卡機制）
> - **(M) 3DS 處理** — `charge_with_token` 回 `redirect_3ds` 視為失敗，改走低餘額通知（cron 環境無人可過 3DS）
>
> **實作範圍**：
> - `_attempt_auto_topup(partner, shortfall)` helper on `sh.subscription.subscription`
> - 掛到 `_sh_create_auto_invoice` 的 `balance < amount_total` 分支：先 auto-topup，成功則支付發票；失敗則保留現有 `_notify_low_balance` 通知
> - 錢包交易記錄（auto-topup 明細納入 billing.transaction）
> - 用 `payment.transaction` record 追蹤 ECPay 扣款（不用自建 model）
> - Dead code 清理：刪除 `AddCardModal.{js,xml,scss}` 和 `CardAddedSuccessModal.{js,xml,scss}`（Phase 2a 的 full page flow 已取代）
>
> **留給未來（Phase 2c 或獨立 PR）**：
> - 預設卡片被刪除後 `property_payment_token_id` 自動清空
> - 卡片到期前 30 天提醒（`ecpg_card_expire` 欄位）
> - GetMemberBindCard reconcile（催回遺失的 bind callback）
> - Design system 元件抽取（breadcrumb / banner）
>
> 執行計畫：見 [`docs/plans/2026-04-19-settings-phase2b-implementation.md`](./2026-04-19-settings-phase2b-implementation.md)（即將建立）。
>
> ### Phase 2 設計決策
> - **(B) 卡片表單位置**：獨立 sub-page `#/settings/payment-methods/add`（Modal 放不下 ECPay SDK 強制的 `<div id="ECPayPayment">` 卡片表單）
> - **(X) 3DS 處理**：整頁 redirect，綁定完成後 ECPay 的 `OrderResultURL` 帶 URL query 參數導回 `/woow#/settings/payment-methods?bind_status=ok&trade_no=...`，OWL 偵測後 refresh 卡片列表
> - **(R) API 層級**：在 `woow_paas_platform` 包一層 `/api/billing/payment-methods/*` wrapper，**直接 import `ECPGClient`（不透過 HTTP）**，這樣可以自訂 `ReturnURL`/`OrderResultURL` 讓使用者 3DS 完成後回到 OWL 而非 ecpay portal
> - **SDK 載入**：在 `AddPaymentMethodPage` 的 `setup()` 動態注入 3 支 `<script>`（jQuery → node-forge → `sdk-1.0.0.js`），不加進全域 `assets_paas`（綁卡低頻）
>
> ### Phase 2a 執行計畫
> 詳細 task 清單見 [`docs/plans/2026-04-19-settings-phase2a-implementation.md`](./2026-04-19-settings-phase2a-implementation.md)（待建立）。

---

## Phase 2 原始設計（保留供參考）

> 以下為 2026-04-16 初版 Phase 2 設計。實際執行時 ecpay agent 的 `payment_ecpay_ecpg` addon 已提供現成 SDK + API，所以我們不需要從零寫 `ECPGAesService` / `ECPGClient`，只需做 wrapper 層。本節保留作為設計思路參考。

### 前置條件

1. ECPay 帳戶已開通 ECPG 站內付 2.0 服務
2. 完成 ECPay 合約（確認是代收付模式或新型閘道模式）
3. `payment_ecpay` 模組（或獨立新模組）新增 ECPG 能力（見 [WOOWTECH/ecpay_odoo18#10](https://github.com/WOOWTECH/ecpay_odoo18/issues/10)）

### 範圍

1. **後端**：ECPG API 整合（5 個端點）+ AES 加密 service + 資料庫 schema
2. **前端**：Payment Methods 頁面 + AddCardModal（3D Secure 流程）+ BillingOverview 整合
3. **自動扣款**：訂閱扣款邏輯加入「餘額不足時自動從卡片補值」的 fallback

### Backend 實作

#### 1. AES-CBC-PKCS7 加密 Service

ECPG 的協議是 AES-JSON（與 AIO 的 CheckMacValue 完全不同）：

```python
# src/services/ecpg_aes.py（新檔案）
class ECPGAesService:
    def encrypt(self, data: dict) -> str:
        """AES-128-CBC + PKCS7 padding + Base64，key = HashKey，iv = HashIV"""
    def decrypt(self, encrypted: str) -> dict:
        """反向解密 + URL decode（aesUrlEncode，不是 ecpayUrlEncode）"""
```

**重要**：AES 的 URL encode 與 CheckMacValue 的 URL encode **邏輯不同**（大小寫、特殊字元處理），**不可共用**。

#### 2. ECPG Client

```python
# src/services/ecpg_client.py（新檔案）
class ECPGClient:
    # 雙 Domain 路由
    TOKEN_DOMAIN = "ecpg-stage.ecpay.com.tw"       # Token + CreatePayment
    PAYMENT_DOMAIN = "ecpayment-stage.ecpay.com.tw" # 查詢 + 退款

    def get_token_by_binding_card(self, member_id, amount=100): ...
    def create_bind_card(self, bind_card_pay_token, member_id): ...
    def get_member_bind_card(self, member_id): ...
    def delete_member_bind_card(self, bind_card_id): ...
    def create_payment_with_card_id(self, bind_card_id, amount, trade_no): ...
```

#### 3. 資料庫 Schema

```python
# src/models/payment_method.py（新檔案）
class PaymentMethod(models.Model):
    _name = 'woow_paas_platform.payment_method'

    partner_id = fields.Many2one('res.partner', required=True)
    bind_card_id = fields.Char(required=True, help="ECPay 綁卡 ID")
    card_type = fields.Selection([('visa', 'Visa'), ('master', 'MasterCard'), ...])
    card_last4 = fields.Char(help="末四碼")
    card_expiry = fields.Char(help="MM/YY")
    is_default = fields.Boolean()
    created_at = fields.Datetime()
    ecpay_member_id = fields.Char(help="MerchantMemberID")
```

#### 4. Controller 更新

`controllers/billing.py` 的 stub 改為實作：

```python
@route('/api/billing/payment-methods/add', type='json', auth='user', methods=['POST'])
def api_payment_method_add(self):
    """步驟 1: 回傳 ECPG token + 前端 3D Secure URL"""
    token = ecpg_client.get_token_by_binding_card(member_id=partner.id)
    return {'success': True, 'data': {
        'bind_card_pay_token': token['BindCardPayToken'],
        'three_d_url': token['ThreeDURL'],
        'action_url': '...'
    }}

@route('/api/billing/payment-methods/bind-callback', type='http', auth='public', csrf=False)
def api_bind_callback(self, **kwargs):
    """步驟 2: ECPay 綁卡結果 callback（Server POST）"""
    # 解密 AES → 驗證 CheckMacValue → 儲存到 payment_method table
    return '1|OK'
```

#### 5. 自動扣款邏輯

```python
# models/workspace_subscription.py
def _attempt_auto_topup_on_insufficient_balance(self):
    """扣款前發現餘額不足時呼叫，嘗試自動從綁定卡片補值"""
    shortfall = self.amount - partner.wallet_balance
    default_card = partner.payment_method_ids.filtered('is_default')
    if not default_card:
        return False
    result = ecpg_client.create_payment_with_card_id(
        bind_card_id=default_card.bind_card_id,
        amount=shortfall,
        trade_no=f'AUTO{self.id}{int(time.time())}',
    )
    if result['success']:
        partner.wallet_balance += shortfall  # 或透過 wallet transaction model
        return True
    return False
```

### Frontend 實作

#### Payment Methods 頁面（`#/settings/payment-methods`）

```
┌─────────────────────────────────────────┐
│ Settings > Payment Methods              │
│                                         │
│ Bound credit cards will be used to      │
│ automatically top up your wallet when   │
│ balance runs low.                       │
│                                         │
│ ┌─ Active Method ─────────────────────┐ │
│ │ VISA  •••• 4242    Default    [Del] │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ ┌─ Other Methods ─────────────────────┐ │
│ │ MC    •••• 5678  [Set Default][Del] │ │
│ └─────────────────────────────────────┘ │
│                                         │
│ [ + Add Payment Method ]                │
└─────────────────────────────────────────┘
```

- 新增卡片時打開 `AddCardModal`，流程為：
  1. 使用者填卡資訊
  2. 前端送到 `/api/billing/payment-methods/add`
  3. 後端回傳 ECPG token + 3D Secure URL
  4. 前端跳轉 3D Secure（綠界提供頁面，輸入簡訊驗證碼 `1234` 測試）
  5. ECPay callback 我們的 server → 儲存 BindCardID
  6. 前端輪詢狀態或收到回跳參數 → 顯示「綁卡成功」

#### BillingOverview 整合

- 加「Manage Payment Methods」按鈕 → 開 `ManagePaymentMethodsModal`（Phase 1 保留的檔案）
- 錢包卡片加「Auto-topup: ON/OFF」開關（選擇預設卡片後自動啟用）

### 檔案變更清單

| 操作 | 檔案 |
|------|------|
| 新增 | `services/ecpg_aes.py` |
| 新增 | `services/ecpg_client.py` |
| 新增 | `models/payment_method.py` |
| 修改 | `controllers/billing.py` — 實作 ECPG 5 個端點 |
| 新增 | `controllers/ecpg_callback.py` — 綁卡/扣款 callback |
| 修改 | `models/workspace_subscription.py` — auto-topup fallback |
| 修改 | `static/src/paas/services/billing_service.js` — 接新 API |
| 新增 | `static/src/paas/pages/settings/PaymentMethodsPage.js/xml/scss` |
| 修改 | `static/src/paas/components/modal/AddCardModal.js/xml` — 3D Secure 流程 |
| 修改 | `static/src/paas/components/modal/ManagePaymentMethodsModal.*` — 接回使用 |
| 修改 | `static/src/paas/pages/billing/BillingOverviewPage.*` — 整合 ManagePaymentMethodsModal |

### ECPG 雷區速查（Phase 2 實作時務必遵守）

參考 [ECPay API Skill](https://github.com/ECPay/ECPay-API-Skill) `guides/02-payment-ecpg.md`：

1. **雙 Domain 不可混用**：Token/CreatePayment 走 `ecpg`，查詢/退款走 `ecpayment`，混用必 404
2. **AES URL encode ≠ CheckMacValue URL encode**：`aesUrlEncode()` 不做 lowercase、不做 .NET 字元還原
3. **3D Secure 必處理**：2025/8 起強制，`ThreeDURL` 在巢狀 `ThreeDInfo` 裡，不是扁平 `data.ThreeDURL`
4. **Callback 雙格式**：`ReturnURL`（S2S JSON POST，回 `1|OK`）vs `OrderResultURL`（前端 Form POST + ResultData，不需 `1|OK`）
5. **RtnCode 型別**：ECPG 解密後是**整數 `1`**（非字串 `'1'`），比較寫 `int(code) == 1`
6. **ConsumerInfo 必填**：`MerchantMemberID` + `Email` + `Phone` 缺一不可，缺失會導致 `GetTokenbyBindingCard` 回 RtnCode ≠ 1 但無明確錯誤訊息
7. **Base64 用標準 alphabet**（`+/=`）不可用 URL-safe（`-_`）
8. **冪等性**：Callback 可能重送 4 次，處理邏輯以 `MerchantTradeNo` 為 key 做 upsert
9. **MerchantTradeDate** 必須 UTC+8，格式 `yyyy/MM/dd HH:mm:ss`

### 未來執行 Phase 2 的指令

在未來的 session 中對 Claude 說：

```
執行 docs/plans/2026-04-16-settings-payment-methods-design.md 裡的 Phase 2，
先確認 payment_ecpay 模組（或 ecpay_odoo18 repo）是否已加入 ECPG 綁卡能力，
然後用 writing-plans skill 產出實作計畫。
```

Claude 會：
1. 讀此 design doc
2. 檢查 `WOOWTECH/ecpay_odoo18#10` 的進度
3. 確認目前 Phase 1 已完成的狀態
4. 產出 `docs/plans/YYYY-MM-DD-ecpg-binding-implementation.md`
5. 徵求確認後開始實作

---

## 參考資源

- **ECPay API Skill**：https://github.com/ECPay/ECPay-API-Skill（綠界官方 AI 整合助手）
  - `guides/02-payment-ecpg.md` §綁卡付款流程 — ECPG 綁卡完整流程
  - `guides/14-aes-encryption.md` — AES 加密的 12 種語言實作
  - `guides/19-http-protocol-reference.md` — HTTP 協議細節
- **ECPay 開發者文件**：https://developers.ecpay.com.tw
- **Odoo ECPay 模組 repo**：https://github.com/WOOWTECH/ecpay_odoo18
- **ECPay PDF 操作手冊**：`/Users/eugene/Documents/woow/ECPay金流、物流、電子發票模組Odoo操作手冊_1.0.2.pdf`
- **測試帳號**：
  - MerchantID: `3002607`
  - HashKey: `pwFHCqoQZGmho4w6`
  - HashIV: `EkRm7iFT261dpevs`
  - 測試卡號: `4311-9522-2222-2222`（安全碼任意三碼）
  - 3D 驗證碼: `1234`（測試環境固定）
