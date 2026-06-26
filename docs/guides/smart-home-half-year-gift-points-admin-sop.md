---
title: Smart Home 半年贈點 — 管理員發放 SOP
category: guides
status: active
created: 2026-06-25
topic: smart-home-half-year-gift-points
related:
  - docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-design.md
  - docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-implementation.md
  - src/controllers/billing.py
  - src/models/loyalty_card.py
  - src/wizard/gift_grant_wizard.py
---

# Smart Home 半年贈點 — 管理員發放 SOP

「買 WOOW HA 主機附贈 Smart Home Connect 半年免費」最終以 **Odoo 原生 eWallet 送點數** 落地：
半年份點數灌進客戶錢包（`loyalty.card.points`），客戶照常建立 Smart Home 服務，每帳期由
billing cron 自動從錢包扣款；扣到 0 後連續扣款失敗達閾值即停服。

點數 ↔ 金額 **1:1**，故 **半年份 = 目標 Smart Home plan 月費 × 6**。

發放有兩條路徑，管理員依情境擇一：

| 路徑 | 工具 | 適用 |
| --- | --- | --- |
| **A. 批量產兌換碼** | Odoo 原生 `loyalty.generate.wizard`（零自訂 code） | 隨主機出貨配發、客戶自助兌換 |
| **B. 手動直接發點** | 本 addon 的「贈送錢包點數」wizard | 免碼，直接灌給已知客戶 |

---

## 路徑 A：批量產兌換碼（原生精靈）

> 碼 = 原生 `loyalty.card`（gift_card program）。**完全用 Odoo 原生功能**，不需任何自訂程式。

### 前置：確認 Gift Cards program

1. 後台 → **銷售 / Loyalty**（或 Sales → Products → Gift cards & eWallet）。
2. 確認有一個 **`program_type = gift_card`** 的 program（stg/prod 為 id 1「Gift Cards」），且 **active**。
   - 若要與一般禮品卡區分批次 / 報表，可另建一個「Smart Home 半年贈點」專屬 gift_card program。

### 產碼步驟

1. 開該 Gift Cards program → 點 **Generate**（產生）。
2. 填寫 `loyalty.generate.wizard` 欄位：

   | 欄位 | 填法 |
   | --- | --- |
   | **Quantity**（`coupon_qty`） | 要產幾張碼（= 預計發幾盒主機） |
   | **Grant**（`points_granted`） | 每張點數 = **目標 SH plan 月費 × 6** |
   | **Valid Until**（`valid_until`） | 兌換碼過期日（建議設，降低外洩風險） |
   | **Mode** | **anonymous**（碼不綁客戶，隨盒配發；選 selected 才綁特定客戶） |

3. 確認產出 → 匯出碼清單（隨主機出貨配發）。

### 客戶兌換

客戶在 `/woow` 帳單頁輸入兌換碼自助兌換（Phase 2 endpoint `POST /api/billing/redeem-code`；
前端輸入框為 Phase 4）。兌換成功後：

- 點數灌進客戶 eWallet 錢包（`partner.add_wallet_points`，自帶 `wallet_transaction` ledger）。
- 該 `loyalty.card` 作廢（`points` 歸 0、`partner_id` 記兌換者供稽核）。
- 同碼不可重兌（`FOR UPDATE NOWAIT` 鎖 + `points=0` 雙重守衛）。

---

## 路徑 B：手動直接發點（免碼）

> 用於「給已知客戶直接灌半年份點數」，不經兌換碼。本 addon 自帶 wizard，**僅系統管理員可見**。

1. 後台 → **聯絡人**（res.partner）→ 開目標客戶。
2. 上方 **動作（Action）** 選單 → **「贈送錢包點數」**。
3. 填寫：
   - **客戶**：已自動帶入（從聯絡人啟動）。
   - **贈送點數**：半年份 = SH plan 月費 × 6。
   - **事由**：預設「Smart Home 半年贈點」，寫入 ledger。
4. 按 **贈送**。點數即進客戶錢包，並寫一筆 `wallet_transaction` ledger。

> 此 wizard 走官方 `partner.add_wallet_points`（與兌換碼同一條灌點 API），故餘額與
> 稽核紀錄與自助兌換完全一致。

---

## 稽核

- **錢包異動**：`wallet.transaction` ledger（自助兌換與手動發點皆會寫入；金額為正）。
- **兌換碼狀態**：`loyalty.card` 無 state 欄位，**`points = 0` 即已用**；`partner_id` 記兌換者。
- 後台可在 Gift Cards program 下查未用（points > 0）/ 已用（points = 0）卡。

## 注意事項

- **點數可花在任何服務**（已拍板接受）：送的半年點數是 partner 全域錢包，可付任何服務帳單，
  非僅限該 Smart Home。
- **兌換碼持有即資格**：碼本身無主，任何登入者打對碼即可兌走一次。以 `valid_until` 過期 +
  足夠隨機碼 + ledger 稽核緩解；切勿在公開管道散布未過期碼。
- **無綁卡客戶**點數扣光只會 dunning → 停服（不會被自動刷卡）；有綁卡客戶點數光時會被
  auto-topup 續扣其卡（視為「願綁卡 = 願續訂」）。
- **停服延遲** ≈ 連續扣款失敗閾值（預設 6）× 每日帳期 ≈ 半年後再約一週才真的斷。
