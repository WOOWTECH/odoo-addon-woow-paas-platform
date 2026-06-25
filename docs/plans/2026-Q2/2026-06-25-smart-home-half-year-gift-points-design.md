---
title: Smart Home「買主機半年免費」— eWallet 送點數 設計
category: plans
status: draft
created: 2026-06-25
updated: 2026-06-25
topic: smart-home-half-year-gift-points
phase: design
related:
  - src/models/subscription_lifecycle_mixin.py
  - src/models/sh_subscription_fix.py
  - src/models/subscription_lifecycle_cron.py
  - src/models/wallet_topup.py
  - src/models/wallet_transaction.py
  - src/models/oauth_code.py
  - src/controllers/billing.py
  - src/controllers/smart_home.py
  - src/security/ir.model.access.csv
  - docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md
  - docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md
  - docs/tracking/pitfalls/owl-template-crash-class-syntax.md
  - docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md
---

# Smart Home「買主機半年免費」— eWallet 送點數 設計

> 實作步驟見 implementation 雙檔的另一半。本檔記「為什麼這樣設計」與「現況事實依據」。

## 背景 / 需求

買 WOOW HA 主機者，附贈 Smart Home Connect **半年免費**，半年後才開始收費、才需綁信用卡。來源：2026-06-17 與 2026-06-22 兩次 PaaS 會議記錄。

## 決策歷程（為什麼最後是「eWallet 送點數」）

這個需求經過三輪唯讀調查 workflow + 跨 agent 協調（`ha-sm-component`、`ecpay`）後，方向轉了兩次才定案，留下這段歷程避免後人重走：

1. **棄「錨定主機身分」**：原想「一台主機一次半年、錨主機身分」。但跨 repo 查證發現 HA 元件唯一能自動拿到的穩定識別是 `core.uuid`，它表達「HA 安裝實例」非「實體主機」——重裝會新生、備份會跨機複製，防濫用兩頭漏；且「一帳號買多台」使 OAuth-user cap 不可用。PaaS 接收端目前完全收不到任何硬體 id（`ha_api.py` 認證是 per-user OAuth Bearer；`smart_home.reference_id` 是 server 每次自生的 UUID）。HA 端 Custom Component 由 maestri agent `ha-sm-component` 負責，要它提供穩定難偽造 id 是另一條未開工的跨 repo 工程。

2. **棄「延長試用」**：把首次 trial 設成半年的做法被 2026-06-22 會議否決——現有試用是**全域**的（每個服務建立都有，與買主機無關），無法直接當「買主機才有」的半年贈送。

3. **定案「送點數」（2026-06-22 會議）**：用 Odoo 原生 **eWallet（電子錢包）送半年份點數**。點數有擴充性、可消費其他服務、不綁死單一用途。停服判定基準是「**這一期沒扣款到**」。`ecpay` agent 確認 ecpay/sh_subscription 生態無現成兌換券（ECPay 的 Redeem 是紅利折抵、ECTicket 沒串）。

## 現況事實依據（stg `woow_stg` live-verify，2026-06-25，全唯讀）

**「點數 = 免費月份」的消費鏈整套現成、且在 stg 真的在跑**（這是本設計能成立的地基；曾被擔心「自動扣靠的上游 cron 不在本 repo、可能沒跑」，已親驗排除）：

| cron id | 方法 | 週期 | 狀態 |
| --- | --- | --- | --- |
| 26 | `_sh_create_auto_invoice()` | 每日 | active，2026-06-24 跑過，0 失敗 |
| 31 | `_cron_wallet_charge()` | 每日 | active（錢包扣款專屬 cron） |
| 32 | `_cron_wallet_charge_retry()` | 每小時 | active（重試） |
| 36 | `_cron_enforce_subscription_lifecycle()` | 每日 | active（停服） |
| 30 | ECPay 到期訂閱扣款 | 每日 | active |

- **「扣不到 → 累計 → cancel → 停服」用真實資料證實**：stg 訂閱有 **3 筆 `state=cancel` 且 `woow_consecutive_payment_failures=6`**；`sub 11` 現場示範 trial（06-20）結束後每帳期扣款失敗、`fails` 爬到 4。
- 失敗計數**每帳期（每日）+1，非每次 hourly retry +1**。
- 點數底層 = `loyalty.card.points`；寫入 API `partner.add_wallet_points(amount, description)`（upstream `sh_subscription_wallet`，本 repo 5 處在用）；ledger = `wallet_transaction`。
- `sale_loyalty` + `loyalty_generate_wizard`（原生「產生禮品卡/eWallet」精靈）**已安裝**。
- **trial 實際是 7 天**（14 個 plan 全 `sh_trial_duration=7 day`；會議口述「30 天」是記錯）。
- **停服延遲 ≈ trial 結束後 6 帳期（≈6 天）** 才 cancel，再由 cron 停服。即「半年免費」實際是 **半年 + 約一週** 才真的斷（閾值 6 可調）。
- **唯一未實地觀察**：happy path「錢包有點 → 成功扣一期」。因 stg 從無 funded wallet（0 張 `loyalty_card`、0 筆 topup、522 筆扣款全 `failed`），此步在 stg 從未被觸發；但 code 路徑清楚、失敗紀錄顯示流程已走到「嘗試扣款」步。→ 留待實作 sandbox 端到端補觀察。

## 設計

**架構 = 薄薄一層兌換 + 100% reuse 已驗證消費鏈。**

```
[admin 發放 / 使用者輸兌換碼]
        │  partner.add_wallet_points(半年份點數)
        ▼
   loyalty.card.points  ←（既有錢包）
        │  （使用者照常建立 Smart Home 服務）
        ▼
  SH 訂閱 7 天 trial → 每帳期 cron 自動從點數扣 → 連扣到 0
        │  點數光 + 無卡 → 扣款失敗累計 6 → cancel → _do_suspend 停服
        ▼
        停服
```

- **兌換 = 純發點**，不順帶綁特定 SH（呼應「點數可花任何服務」已拍板）。使用者照既有流程建 Smart Home，點數自動覆蓋帳單。
- **發點走 `partner.add_wallet_points()`**（贈點免費、不經 ECPay、不開客戶發票）。**不要**借 `wallet.topup`——那是付費路徑，會誤開一張「已付」發票。
- **半年點數值** = 目標 SH plan 月費 × 6（或 admin 產碼時填 explicit amount，存在兌換碼上）。
- **單次碼 idiom** 可參考 `src/models/oauth_code.py`（`code`/`is_used`/`expires_at`/`is_valid()`/`mark_used()`），但**不可照抄並行安全性**（見約束）。

## 決策鎖定（Eugene，2026-06-25）

1. 送點數觸發 = **admin 後台手動發 + 兌換碼自助兌換**（兩者皆做）。
2. **接受**點數可花在任何服務（不綁特定 SH）。
3. **不改** auto-topup：無卡買家點數光只會 dunning→停服（`_resolve_default_token` 無 token → 不刷卡），安全。
4. **先不做** 機器銷售 `sale.order` confirm 自動發點。

## 約束 / 風險 / 取捨

- **🔴 並行兌換必須 `SELECT FOR UPDATE` 鎖 code row**：`oauth_code` 既有單次碼 pattern **沒有 row locking**（裸 TOCTOU），照抄會讓同一碼被並行兌換兩次（兌出兩份點數）。單次性需三層：`_sql_constraints UNIQUE(code)` + `state` 機 + redeem 時 `FOR UPDATE` + `write()` guard 擋 `used→unused` 逆轉。
- **跨服務外洩（已接受）**：點數是 partner 全域（`loyalty.card.points` 綁 partner 非綁訂閱），送的半年點數可被拿去付任何服務的帳單（`/api/billing/invoices/<id>/pay` 不限服務）。Eugene 已接受。若日後不可接受，eWallet 架構擋不住，需改受限用途的 gift_card。
- **auto-topup 邊界**：有卡買家點數光時會被 auto-topup 自動續扣其卡（community 版 `_resolve_default_token` 會借該 partner 任一服務綁的最近一張卡）。已決定不改——視為「願綁卡＝願續訂」的合理行為。
- **停服延遲 ≈ 6 天**（閾值 6 + 每日累計）。半年贈送下這段額外免費期可接受；閾值可調。
- **bearer 弱點**：兌換碼持有即資格，外洩可被他人領一次。用過期（`expires_at`）+ 足夠隨機 + 稽核（`redeemed_by/at`）緩解。
- **Repo patterns 必守**：`ir.model.access.csv` ACL；**ORM 綠 ≠ endpoint secure**（[[orm-vs-endpoint-acl-test-coverage]]）→ 補 HttpCase cross-user 負向測試；OWL template **別用 Vue 語法**（[[owl-template-crash-class-syntax]]，`.enter` / component 上 `t-att-` → 白屏）；若用 seed 注意 sticky-`noupdate`（[[seed-noupdate-sticky-template-links]]）。

## 待實作中決的小項（不阻擋開工）

- 贈點要不要 GL 分錄/會計憑證，還是 `wallet_transaction` ledger 足夠合規？
- 停服延遲（閾值 6 ≈ 6 天）要不要為贈送情境調低？
- admin 批量產碼復用原生 `loyalty_generate_wizard` vs 自建薄 wizard？
- 半年點數精確算法（plan 月費 × 6 vs admin explicit）。
