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
  - src/models/redemption_code.py
  - src/models/__init__.py
  - src/models/oauth_code.py
  - src/models/subscription_lifecycle_mixin.py
  - src/controllers/billing.py
  - src/security/ir.model.access.csv
---

# Smart Home「買主機半年免費」— eWallet 送點數 實作

> 設計與決策依據見 design 雙檔的另一半。本檔只列「改哪個檔的哪一段、改成什麼、怎麼驗」。
> 依賴順序：Phase 1（model 地基）→ Phase 2（兌換 endpoint）→ Phase 3（admin 發放）→ Phase 4（前端）→ Phase 5（sandbox 端到端）。每個 Phase 結束都要綠才進下一個。
> **核心心法**：消費鏈（自動扣→dunning→cancel→停服）已 stg live-verify 證實可用，**完全不碰**；本 epic 只新增「把半年份點數灌進錢包」這條薄路。

## Phase 列表

- [ ] **Phase 1**：`redemption_code` model + 單次性/並行三層 + ACL + 單元測試
- [ ] **Phase 2**：兌換 endpoint（`SELECT FOR UPDATE`）+ HttpCase cross-user 負向
- [ ] **Phase 3**：admin 後台發放（手動發點 + 批量產碼）
- [ ] **Phase 4**：`/woow` 前端兌換碼輸入 component（headed 實測零 console error）
- [ ] **Phase 5**：sandbox 端到端（補 stg 未觀察的 happy-path：灌點→扣款→停服）

---

## Phase 1 — `redemption_code` model

**新增 `src/models/redemption_code.py`**：`woow_paas_platform.redemption_code`
- 欄位：`code`（Char, required, index, copy=False）、`points`（Float，這張碼發多少點）、`state`（Selection `unused`/`used`/`expired`, default `unused`）、`redeemed_by`（Many2one `res.partner`）、`redeemed_at`（Datetime）、`expires_at`（Datetime, optional）、`batch_label`（Char，產碼批次標籤，供對帳）、`description`（Char）。
- `_sql_constraints`：`UNIQUE(code)`（**注意：`oauth_code` 只有 index 沒 unique，這裡必須補**）。
- `is_valid()`：`state == 'unused' and (not expires_at or expires_at > now)`（套 `oauth_code.py` idiom）。
- `write()` guard：擋 `state` 從 `used`/`expired` 逆轉回 `unused`（呼應 [[scope-flip-write-guard-not-ir-rule]]：不可變狀態轉移在 `write()` 強制，不靠 ir.rule）。
- `redeem(partner)`：**必須先 `SELECT ... FOR UPDATE` 鎖本 row** 再驗 `is_valid()` → 呼叫 `partner.add_wallet_points(self.points, f'Smart Home 半年贈點 [{code}]')` → `write({state:'used', redeemed_by, redeemed_at:now})`。建議用 `self.env.cr.execute("SELECT id FROM redemption_code WHERE id=%s FOR UPDATE NOWAIT", ...)` 或 `_for_update`。
- `generate(n, points, ...)`（admin 用 classmethod / model method）：批量產碼，值用 `secrets.token_urlsafe`（足夠隨機、非順序可猜）。

**`src/models/__init__.py`**：加 `from . import redemption_code`。
**`src/security/ir.model.access.csv`**：加 ACL（portal user 只能透過 endpoint 兌換、**不可**直接讀寫 code；admin 可 CRUD）。

**驗收**：
- 單元測試 `src/tests/test_redemption_code.py`：產碼→兌換→點數入錢包（`partner.wallet_balance` 增加）+ ledger 有 `wallet_transaction`；二次兌換被拒；`used→unused` 被 guard 擋；過期碼被拒。
- 並行：模擬兩個交易同碼 `FOR UPDATE`，只一個成功。

## Phase 2 — 兌換 endpoint

**`src/controllers/billing.py`** 加 route：
```
@route('/api/billing/redeem-code', auth='user', methods=['POST'], type='json')
def api_redeem_code(self, code, **kw):
    # 找 partner = request.env.user.partner_id
    # env['woow_paas_platform.redemption_code'].sudo() 查 code（FOR UPDATE）
    # 成功 → redeem(partner) → 回 {success, new_balance}
    # 失敗分流：not_found / already_used / expired
```
- **不可**用 `.sudo()` 繞過後再把別人的碼兌給自己——partner 一律取自 `request.env.user`，code 是 bearer（持有即可兌），但 redeemed_by 必為當前登入者。

**驗收**：
- HttpCase `test_redeem_code_endpoint.py`（[[orm-vs-endpoint-acl-test-coverage]]）：登入 A 兌換成功；同碼 B 兌換被拒（already_used）；亂碼/過期碼回正確錯誤；兌換後 A 的 `wallet_balance` 增加。
- 確認無卡 partner 兌換不觸發任何 ECPay 刷卡。

## Phase 3 — admin 後台發放

- 手動發點：admin 對指定 partner 直接 `add_wallet_points`（server action / 簡易 wizard）。
- 批量產碼：wizard 呼叫 `redemption_code.generate(n, points, expires_at, batch_label)`，可匯出 CSV 供出貨配發。**評估復用原生 `loyalty_generate_wizard`**（已安裝）vs 自建薄 wizard；若復用要確認其產出能對應到本 `redemption_code` 兌換流程，否則自建較單純。
- backend views（list/form for `redemption_code`，含 state/redeemed_by 過濾）。

**驗收**：admin 能產 N 張碼、能手動發點給 partner；list view 可查已用/未用。

## Phase 4 — `/woow` 前端兌換碼輸入

- billing 頁加「輸入兌換碼」component（OWL，`src/static/src/paas/`），打 `/api/billing/redeem-code`，成功後刷新錢包餘額顯示。
- **OWL 紀律**（[[owl-template-crash-class-syntax]]）：component prop 直接 `name="expr"`（勿 `t-att-`）；按鍵在 handler 讀 `ev.key`（無 `.enter`）。

**驗收**：headed 瀏覽器實走兌換流程，console **零 OWL/JS error**（本 repo 無 JS 測試框架，headed 實測是唯一攔截網）。

## Phase 5 — sandbox 端到端（補 happy-path）

走 `/woowprd:k8s-dev-sandbox`，在 sandbox（非 stg）：
1. 兌換碼發點 → 確認 `loyalty.card.points` 入帳。
2. 建 Smart Home → 7 天 trial 結束 → 觸發/等 `_cron_wallet_charge` → 確認**成功扣點**（`wallet_transaction` `state=done`、`balance_after` 下降、訂閱 `sh_date_of_next_payment` 推進）。**← 這是 stg live-verify 沒觀察到的那一步。**
3. 點數扣光 → 確認連續失敗累計 → 達 6 → `cancel` → `_do_suspend` 停服。

**驗收**：端到端「灌點 → 自動扣 6 期 → 第 7 期 fail → 停服」在 sandbox 跑通並截圖/log 存 `docs/tracking/`。

---

## 不做 / 範圍外（本 epic）

- 不碰消費鏈任何 cron / 扣款 / dunning / 停服邏輯（已驗證可用）。
- 不做 `sale.order` confirm 自動發點（Eugene：先不做）。
- 不做 host 身分錨定（已棄）。
- 不改 auto-topup。
