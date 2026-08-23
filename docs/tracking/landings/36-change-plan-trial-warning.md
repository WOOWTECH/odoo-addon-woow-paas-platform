---
category: tracking
status: active
created: 2026-08-23
updated: 2026-08-23
related:
  - docs/tracking/followups/follow-ups.md
  - scripts/audit/verify-import-union.py
  - scripts/audit/verify-baseline-monotonic.py
---

# #36 落地紀錄：試用中變更方案的確認步驟（Q9 判準被推翻，改採 P2）

**PR #879**（merge commit `29f355e8`）｜開單 2026-05-14，落地 2026-08-23。

## 這張單真正的產出不是那個 modal

issue #36 開單時的三層 Scope 裡，**(a) SH 側 wrapper 與 (b) ChangePlanModal ＋ detail page 接線，早在 2026-07-16 就由 `ca80f6f0`（Wave C PR-C3）落地了** —— 那顆 commit 只寫 `Refs #330`，issue 兩端都沒有回連紀錄，所以 #36 在 backlog 上以三層份量存在了約三個月。此形態已記為 `F-36-1`（見 `docs/tracking/followups/follow-ups.md`）。

真正剩下的 (c)「Q9 trial→no-trial 確認步驟」在實作前追查後端時，翻出**判準本身是錯的**。

## 🔴 Q9 把「真正會扣款」的那一格劃進了「靜默」

`#34` 的 Q9 裁決逐字：

> **Trial→no-trial only**：modal warns "billing starts now"。Trial→trial is silent。

但後端在 change-plan 路徑上**完全不採用目標方案的試用設定**：

`src/models/subscription_lifecycle_mixin.py` 的 `_create_and_confirm_subscription`：

```python
if plan.sh_trial and not entity_has_prior_sub:
    self._force_trial_dates_from_plan(subscription, plan)
else:
    self._force_active_dates_from_plan(subscription, plan)
```

`entity_has_prior_sub` 在 change-plan 上**結構性恆為 True**：三個 surface 的 change-plan endpoint 自己的 gate 讀的就是同一個欄位（`old_sub = service.sudo().subscription_id` → 沒有就直接回 `No active subscription`），而從那道 gate 到 mixin 呼叫之間對舊訂閱只寫 `state` / `sh_reason`，沒有任何地方清掉 entity 的 `subscription_id`；解綁另有 fail-loud guard 擋死。

⇒ 一律走 `_force_active_dates_from_plan`（明寫 `sh_trial=False`、`sh_start_date=today`）⇒ `sh_subscription_confirm()` 當場開首期發票。

**這條行為早已被既有測試釘死**（不是讀碼推論）：`src/tests/test_trial_reset_guard_367.py` 的 `test_returning_partner_change_plan_no_trial`，目標 plan 是一個**帶 7 天試用**的席數變體，斷言卻是 `assertFalse(new_sub.sh_trial)` ＋ 首期發票 `amount_total > 0`。依據是 #367（試用綁 entity、一生一次）。

### 後果

使用者在試用中，看到另一張**標著「N-day free trial」**的卡片（`PlanSelector.xml` 對 `has_trial` 為真的方案就是這樣顯示），選它、確認 —— **Q9 判定為 trial→trial ⇒ 靜默**，而後端當場結束試用並開帳單。反過來，Q9 要警告的 trial→no-trial 在現實資料下幾乎不觸發。

⇒ 照 Q9 字面實作，會交付一個幾乎不會跳出來的警告，同時讓真正扣錢的路徑保持沉默。

**2026-08-23 改裁 P2：`subscription.is_trial` 單獨成立即警告**，不看目標方案。改判紀錄回寫於 #34 / #36 留言，未在 PR 內悄悄換語意。

曾被考慮但排除的第三案「改後端讓 trial→trial 保留試用」會直接推翻 #367 並讓既有測試變紅。

## 交付內容

| 項目 | 說明 |
| --- | --- |
| 二階段確認 | `ChangePlanModal` 的 `willEndTrial` getter ＋ `state.confirmingTrialEnd` view flag。第一次 `onConfirm()` 只切旗標並 return，**不呼叫 `confirmChange`**；切旗標**不動 `selectedPlanId`** ⇒ 按「返回」時選擇（含席數變體）原封不動。次要鍵標「返回」（回選擇階段、不關 modal），主要鍵 `danger` ＋ 字面「結束試用並開始計費」。 |
| advisory 文案 | 原本一句話對所有人顯示，提到「試用中」時談的是**不退點**，沒說試用會當場結束並產生首期帳單。拆成通用（只講退點）＋ 試用專屬（講後果）兩句。順帶修正一個既有的不精確：「本期尚未扣款」→「本期尚未**成功**扣款」（依據 `_compute_unused_refund` 的 `_has_paid_current_cycle`）。 |
| 後端前提守線 | `src/tests/test_change_plan_ends_trial_36.py`，SA / SH 各 2 條。**CS 已由 `test_trial_reset_guard_367.py` 守住，不重寫同義的**。 |

### ⚠ 共用元件 ⇒ Security Access 連帶獲得同一行為

`ChangePlanModal` 是 service-type-agnostic 的共用元件，CS / SH / SA 三個 detail page 共用。Q9 的裁決文脈只涵蓋 SH + CS，**這一格是刻意的擴及**，已在 PR body 明標供覆核。

### 為什麼守線測試要涵蓋三個 surface

P2 的文案對使用者保證「一定會結束試用並產生首期帳單」，而那個「一定」是**後端保證的**，不是 UI 保證的。若日後有人改後端讓 trial→trial 保留試用，文案就變成謊言而沒有東西會叫。

「共用同一個 mixin 所以測一邊就夠」在本 repo **不成立**：#34 的 Q6 處理的正是「CS 曾用 inline refund calc、與 SH 的 `_compute_unused_refund` 不一致」，PR #35 才把兩邊拉齊 —— 這兩條 controller 歷史上真的漂移過。故守的是 controller→mixin→confirm **整條**。

**每個 surface 各配一條方向相反的陽性對照**（全新首購＝有 trial ＋ **零**帳單）。沒有它，`assertTrue(invoices)` 可能只是反映「平台對任何新訂閱都開發票」而恆真、等於沒測。

## 驗證

| 階段 | 結果 |
| --- | --- |
| RED | `2 failed, 0 error(s) of 4 tests`，兩條主張紅在 `assertFalse(new_sub.sh_trial)`（`True is not false`）；**兩條對照維持綠** ⇒ 注入定位在 change-plan 路徑，不是「整個 trial 機制壞了」 |
| 注入到位 | **在 pod 內**驗：`RED-INJECTION-36` 命中 1、`and not entity_has_prior_sub` 命中 0。沒有這格，紅綠兩種結果都不能解讀 |
| GREEN | `0 failed, 0 error(s) of 4 tests`，四條 `Starting …` 行皆在（排除「0 failed of 0」） |
| 沙盒全套件 | `2 failures, 0 errors of 1464 tests` —— 那 2 條是 **#804**（沙盒把 `paas_domain` seed 成 `{slug}.woowtech.io`，測試斷言假設該參數未設定），非本 PR 造成 |
| E2E | playwright-cli `--headed` 四情境全過，**含非試用的負向對照**（警告不得出現）。沒有負向那條，「警告總是跳」與「判準沒接上、無條件跳」不可區分 |
| **合併後 backstop** | develop 的 `odoo-tests / full-suite` success，`0 failed, 0 error(s) of **3459** tests`；併入前 `706b6958` 是 **3455** ⇒ **+4，恰為 2 class × 2 條** |

E2E 的非試用 fixture 刻意走 mixin 自己的 `_force_active_dates_from_plan` 產生，而非手寫欄位 —— 這讓它與「真的做過一次 change-plan 之後」的狀態同形，負向對照才不是在人造狀態上驗。

## 附帶產出：兩支解衝突驗證工具

`scripts/audit/verify-import-union.py`、`scripts/audit/verify-baseline-monotonic.py`。

⚠ **兩支都是手動工具，未接 CI** —— 沒有任何 workflow 或 hook 會呼叫它們。docstring 開頭已明標，並列出各自的已知盲區。

放 `scripts/audit/` 而非 `evidence/36-*/` 的理由：它們**不是 #36 專屬** —— 解任何 append 型衝突、驗任何 baseline 都能用；埋進 evidence 目錄會讓下一個遇到同樣衝突的人找不到。

## 過程產物（不進 repo）

E2E 截圖與 full-suite raw log 體積不適合 git，留在保全目錄 `woow-addons/wt-36-preserve/`（sibling，收割日會清）與 canvas note `wt36-preserve`。**下表是它們被清掉之後唯一能證明「當時有這些產物、內容是什麼」的東西**：

| 檔 | sha256 | 備註 |
| --- | --- | --- |
| `e2e-shots/01-trial-modal-selection.png` | `92b6c0c808a65f6923bd740cf0b033c806c1a37b10704041e098987e7856f9a6` | 試用中 modal 選擇階段，兩句 advisory 都在 |
| `e2e-shots/02-trial-warning-shown.png` | `efe4d4aabe3da4eb409d6c52dbc245f2944634299690830f840f26800741aa43` | 警告階段（三條後果） |
| `e2e-shots/03-back-selection-preserved.png` | `b228b1c1abed321ce0cdb118cc2bed783b131ea5c9a7861c4c89dec01a0589e4` | 按返回後選擇仍保留 |
| `e2e-shots/05-after-commit-trial-ended.png` | `693c78d97427328c0b1f0f1f30b04a11afb2c1b81e4645916623aecbaee6fe1c` | 確認後 Skip Trial 按鈕消失 |
| `e2e-shots/06-active-service-detail.png` | `473aa3806d61f03fc20bb846a77cc3b3a2fe0249016d671d8d954f6d347cb076` | 非試用 service 詳情 |
| `e2e-shots/07-active-modal-no-trial-advisory.png` | `dbb0bd580454a02849b58a3b882e099062a95fe38bea62910be2ad31cf10f211` | 非試用 service 的 modal（無試用那句） |
| `raw-logs/fullsuite.log` | `af55225e99ec521427b5a3b051e3d80f4c0d84f99857e1220e67cc0dad2c2713` | 21654 行 / 3203368 bytes |
| `raw-logs/fullsuite-stats.log` | `20453a226bd46202d7d9eca8016b4b3849007c30edc9ee5472693c404ff89edb` | 27044 行 / 3891746 bytes |

⚠ 截圖清點時發現 `05` 與 `06` 曾經 sha 完全相同（`06` 拍到了同一個 service —— 在「reload 修正頁面身分」之前拍的，SPA 不重繪）。已重拍。**「每情境一張」不等於「每張拍到不同的東西」；清點截圖要算 sha，不能只數檔案數。**

## What was NOT verified

- **「產生首期帳單」≠ 已驗「當場從錢包扣款」**。驗到的是開出 `amount_total > 0` 的帳單。`_skip_trial` 有明確的餘額檢查，**change-plan 的三個 controller 都沒有** ⇒ 文案刻意寫「產生首期帳單」而非「扣款」。
- **餘額不足時 change-plan 的行為**未查（可能開出未付款發票）。
- **E2E 只跑了 Cloud Service 一個 surface**。SA / SH 走同一個共用元件、判準只讀 `is_trial`，行為應相同，但未在那兩個頁面實跑；後端那半有 SA / SH 兩個 class 守著。
- **OWL 模板的錯誤分支未走過**（`confirmChange` 失敗時警告階段的錯誤顯示）。
- **prod / stg 實際的 plan 資料未驗**。「前台可見方案幾乎都有試用」量的是 `src/data/*.xml` 的 seed，營運可在後台手建 plan。（P2 判準不讀目標方案的 trial，不影響行為。）
- **merge 進 develop 之後未重跑模組測試**（沙盒已銷毀）。被測的四個檔（mixin ＋ 三個 controller）在 develop 上 diff 行數皆為 0 是量過的，但「所以結果仍成立」是推論；合併後的 backstop 綠且 +4 對帳成立，補上了這一格的大部分。

## 相關

- 前置後端：#34 / PR #35（trial 期允許 change-plan）
- 判準依據：#367（試用綁 entity、一生一次）
- (a)(b) 的實際落地：`ca80f6f0`（Wave C PR-C3，`Refs #330`）
- 附帶發現另開：**#873**（席數變體 plan 的試用設定與 base 脫鉤，影響**新購**路徑）
- 沙盒環境敏感紅：**#804**
- 中文化欠債：**#107**（`PlanSelector.js` 的 `N-unit free trial` 英文字串）
