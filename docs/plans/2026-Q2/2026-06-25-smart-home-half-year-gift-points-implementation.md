---
title: Smart Home「買主機半年免費」— eWallet 送點數 實作
category: plans
status: draft
created: 2026-06-25
updated: 2026-06-25
topic: smart-home-half-year-gift-points
phase: implementation
related:
  - docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-design.md
  - src/models/loyalty_card.py
  - src/models/__init__.py
  - src/controllers/billing.py
  - src/models/subscription_lifecycle_mixin.py
  - src/security/ir.model.access.csv
---

# Smart Home「買主機半年免費」— eWallet 送點數 實作

> 設計與決策依據見 design 雙檔的另一半。本檔只列「改哪個檔的哪一段、改成什麼、怎麼驗」。
> **核心心法**：消費鏈（自動扣→dunning→cancel→停服）已 stg live-verify 證實可用，**完全不碰**；**產碼用 Odoo 原生（零自訂 code）**；我們只做「把碼兌成錢包點數」這條 `/woow` 自助路 + 前端。

> **2026-06-25 設計修正**：原版假設自建 `redemption_code` model + 自建產碼 wizard。實查 stg 後改用原生並**砍掉自訂 model**——錢包本身就是原生 ewallet program，且已有 gift_card program + 原生產碼精靈。碼直接用原生 `loyalty.card`，admin 產碼 100% 用原生精靈，我們只剩「兌換邏輯 + endpoint + UI」。

## 原生事實依據（stg `woow_stg` 2026-06-25 查證）

- `loyalty_program`：id 1 `gift_card`「Gift Cards」、id 2 `ewallet`「**Wallet Points**」(= 你的錢包)，皆 active。`partner.wallet_balance` = partner 在 program 2 下的 `loyalty.card.points`。
- `loyalty_card` 欄位：`code`, `points`, `expiration_date`, `partner_id`, `program_id`, `active`, `order_id`。**無 `state` 欄位 → 「已用」= `points` 歸 0。**
- 產碼精靈 `loyalty.generate.wizard`：`program_id`, `coupon_qty`(產幾張), `points_granted`(每張幾點), `valid_until`, `mode`(匿名/指定客戶)。
- **點數 ↔ 金額 1:1**：消費鏈直接拿 `loyalty.card.points`(= wallet_balance) 比 `invoice.amount_total` 並扣 `amount`，故點數即金額單位 → 「半年份 = SH 月費 × 6」。

## Phase 列表

- [ ] **Phase 1**：兌換核心邏輯（`_inherit='loyalty.card'` 加 `_redeem_to_wallet`，`FOR UPDATE` + 作廢卡）+ 單元測試。**無新 model。**
- [ ] **Phase 2**：兌換 endpoint `/api/billing/redeem-code` + HttpCase cross-user 負向
- [ ] **Phase 3**：admin 產碼 SOP（原生 `loyalty.generate.wizard`，零 code）+ admin 手動發點
- [ ] **Phase 4**：`/woow` 前端兌換碼輸入 component（headed 實測零 console error）
- [ ] **Phase 5**：sandbox 端到端（補 stg 未觀察的 happy-path：兌換→扣款→停服）

---

## Phase 1 — 兌換核心邏輯（extend 原生 `loyalty.card`，無新 model）

**新增 `src/models/loyalty_card.py`**：`class LoyaltyCard(models.Model): _inherit = 'loyalty.card'`
- `_redeem_to_wallet(self, partner)`（單張）：
  1. **鎖本 row 防並行重領**：`self.env.cr.execute("SELECT id FROM loyalty_card WHERE id=%s FOR UPDATE NOWAIT", (self.id,))`（[[scope-flip-write-guard-not-ir-rule]] 同哲學：不可變狀態轉移在寫入處強制；`oauth_code` 既有單次碼 pattern **無 row lock**，是裸 TOCTOU，不可照抄）。
  2. **validate**：屬 gift_card program、`points > 0`、`active`、`(not expiration_date or expiration_date >= today)`；否則 raise 分流（not_found / already_used / expired）。
  3. **發點**：`partner.add_wallet_points(self.points, f'兌換禮品碼 {self.code}')` → 點數進 partner 的 ewallet 錢包（program 2）+ 建 `wallet_transaction` ledger。
  4. **作廢卡**：`self.write({'points': 0, 'partner_id': partner.id})`（`points=0` 即已用；`partner_id` 記兌換者供稽核）。
- `_find_redeemable(self, code)`：search gift_card program + `code`（回單張或空）。

**`src/models/__init__.py`**：加 `from . import loyalty_card`。
**`src/security/ir.model.access.csv`**：portal user **不可**直接讀寫 `loyalty.card`，一律走 endpoint（endpoint 內 `.sudo()` 但 partner 一律取自登入者，見 Phase 2）。

**驗收** `src/tests/test_gift_redeem.py`：
- 產 1 張 gift_card（points=600）→ `_redeem_to_wallet(A)` → A `wallet_balance` +600、card `points=0`、`partner_id=A`。
- 二次兌換被 `points=0` 擋；過期卡被擋；非 gift_card program 的卡被拒。
- **並行**：兩交易同卡 `FOR UPDATE`，只一個成功（另一個 NOWAIT 失敗或讀到 points=0）。

## Phase 2 — 兌換 endpoint

**`src/controllers/billing.py`** 加 route：
```
@route('/api/billing/redeem-code', auth='user', methods=['POST'], type='json')
def api_redeem_code(self, code, **kw):
    partner = request.env.user.partner_id          # 一律登入者，不可由 client 指定
    card = request.env['loyalty.card'].sudo()._find_redeemable(code)
    if not card: return {'success': False, 'error': 'not_found'}
    card._redeem_to_wallet(partner)                # 內含 FOR UPDATE
    return {'success': True, 'new_balance': partner.wallet_balance, 'points_granted': ...}
```
- 失敗分流 not_found / already_used(points=0) / expired；`sudo()` 僅為讀 loyalty.card，**redeemed partner 必為 `request.env.user`**（不可 mass-assign）。

**驗收** HttpCase `test_redeem_code_endpoint.py`（[[orm-vs-endpoint-acl-test-coverage]]）：登入 A 兌換成功；同碼 B 兌換被拒；亂碼/過期回正確錯誤；A `wallet_balance` 增加；無卡 partner 兌換不觸發任何 ECPay 刷卡。

## Phase 3 — admin 產碼（原生精靈，零自訂 code）+ 手動發點

- **產碼 = 100% 原生**：後台 → Sales/Loyalty →「Gift Cards」program → **Generate** → 填 `coupon_qty`(數量=發幾盒)、`points_granted`(每張 = SH 月費 × 6)、`valid_until`(過期)、`mode = anonymous`(碼不綁客戶,供隨盒配發) → 產出 → 匯出清單。
  - 落地工作只有：① 確認 Gift Cards program(id 1) 設定正確（`program_type=gift_card`、active）；② 把此 SOP 寫進 `docs/guides/`；③（可選）seed 一個「Smart Home 半年贈點」**專屬 gift_card program** 以利批次/報表區分（若 seed 注意 [[seed-noupdate-sticky-template-links]] sticky-`noupdate`）。
- **admin 手動發點**（decision #1a，給已知客戶、免碼）：partner 表單加按鈕 / server action 直接 `partner.add_wallet_points(amount, '半年贈點')`（~5 行）。

**驗收**：admin 能用原生精靈產 N 張碼並匯出；能對指定 partner 手動發點；backend 可查已用(points=0)/未用。

## Phase 4 — `/woow` 前端兌換碼輸入

- billing 頁加「輸入兌換碼」component（OWL，`src/static/src/paas/`），打 `/api/billing/redeem-code`，成功後刷新錢包餘額。
- **OWL 紀律**（[[owl-template-crash-class-syntax]]）：component prop 直接 `name="expr"`（勿 `t-att-`）；按鍵在 handler 讀 `ev.key`（無 `.enter`）。

**驗收**：headed 瀏覽器實走兌換流程，console **零 OWL/JS error**（本 repo 無 JS 測試框架，headed 實測是唯一攔截網）。

## Phase 5 — sandbox 端到端（補 happy-path）

走 `/woowprd:k8s-dev-sandbox`，在 sandbox（非 stg）：
1. 原生精靈產 gift_card → `/woow` 兌換 → 確認點數入 `loyalty.card.points`（錢包）、gift card `points=0`。
2. 建 Smart Home → 7 天 trial 結束 → 觸發/等 `_cron_wallet_charge` → 確認**成功扣點**（`wallet_transaction` `state=done`、`balance_after` 下降、`sh_date_of_next_payment` 推進）。**← stg live-verify 沒觀察到的那一步。**
3. 點數扣光 → 連續失敗累計達 6 → `cancel` → `_do_suspend` 停服。

**驗收**：端到端「兌換 → 自動扣 → 停服」在 sandbox 跑通並截圖/log 存 `docs/tracking/`。

---

## 不做 / 範圍外（本 epic）

- **不自建 `redemption_code` model / 產碼 wizard**（改用原生 gift_card + `loyalty.generate.wizard`）。
- 不碰消費鏈任何 cron / 扣款 / dunning / 停服邏輯（已驗證可用）。
- 不做 `sale.order` confirm 自動發點（Eugene：先不做）。
- 不做 host 身分錨定（已棄）。不改 auto-topup。

## 待中決小項（不阻擋開工）

- 產碼用既有「Gift Cards」program(id 1) vs 新建「Smart Home 半年贈點」專屬 program（批次/報表區分較清楚）。
- 兌換把整張卡 points 一次全額轉入錢包（MVP 建議全額、單次性最單純）vs 允許部分。
- 出貨序號 ↔ 碼對帳：MVP 靠原生精靈的 description/批次，不另建 model（呼應 decision #4 先不做 sale.order）。
- 贈點要不要 GL 會計憑證，還是 `wallet_transaction` ledger 足夠。
- 半年點數精確值（plan 月費 × 6 vs admin explicit）+ 停服延遲（閾值 6 ≈ 6 天）是否調整。
