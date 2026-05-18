# Issue #39 Test Coverage — non-trial sub end-date invariant

**Branch**: `fix/sh-end-date-fresh-non-trial`  **Base**: `develop`
**Audience**: reviewer / stakeholder（30 秒內要能說出本 PR 保護了什麼）
**狀態**: TDD red phase 完成（3 個 mandatory test 全部紅燈，AssertionError on end_date / invariant violation）

---

## Why this PR exists

新建立「非試用方案（non-trial plan）」訂閱時，下游程式碼用平台所有方案 default 帶的「無限續訂」設定（`sh_no_of_billing_cycle = -1`）去算結束日，造成計算式裡的乘數變號，**結束日反而比開始日早一個月**。例如 2026-05-17 開始的月繳訂閱，系統把 `sh_end_date` 寫成 2026-04-16，違反「結束日不可早於開始日」這條基本不變式。

**影響面**：

- **UI cosmetic 錯亂**：訂閱頁、續訂倒數、寬限期、清理視窗等任何讀 `sh_end_date` 的元件都拿到錯資料。
- **財務面已被守住**：退款計算 `_compute_unused_refund` 既有 `max(0, ...)` clamp 與 `today < start → 0` guard（PR #38 commit 4 已測），不會因本 bug 多退款。
- **觸發條件**：所有走 `_create_and_confirm_subscription` 的非試用方案建立流程 — 包含 fresh subscribe、change-plan from trial → no-trial。

**修法摘要**（reviewer 對照 diff 用）：在 `_create_and_confirm_subscription` 內 trial 分支旁加 `else:` 分支，呼叫新的 sibling helper `_force_active_dates_from_plan`，於所有 onchange chain 跑完之後最後一刻覆寫 plan-driven 三個日期欄位（`sh_start_date` / `sh_end_date` / `sh_date_of_next_payment`）。對稱於 PR #38 的 trial path 修法。

---

## What we're testing — 3 個 mandatory test

每個 test 守的是一條 business invariant，不是 implementation detail。

### Test 1 — `TestForceActiveDatesFromPlan.test_fresh_non_trial_plan_writes_active_date_triple`（新增 class）

**Business intent**：拿一個非試用、月繳、無限續訂的方案呼叫「建立並確認訂閱」流程，驗證寫進 DB 的三個日期欄位都被 plan 計算式定義（start = today、end = today + 1 個月 − 1 天、next_payment = today + 1 個月），不被第三方 onchange 鏈拉壞。

**守住的不變式**：
- 非試用方案不應產生試用訂閱（`sh_trial == False`）
- 開始日等於今天
- **核心修復**：結束日為 plan-driven，不再倒退一個月
- **核心修復**：next_payment 為 plan-driven
- **不變式**：結束日 >= 開始日

**為何選這個 boundary**：這是修法 helper 的「第一現場」test，缺它整個 fix 沒有 unit-level 鏡像。與 PR #38 的 `test_fresh_trial_plan_writes_full_date_triple` 結構對稱（見 §Symmetry）。

### Test 2 — `TestForceTrialDatesFromPlan.test_non_trial_plan_leaves_trial_fields_inert`（既有強化）

**Business intent**：此 test 原本守「非試用方案不可誤填試用欄位（`sh_trial`、`sh_trial_end_date`）」。本次補上一條讀者一看就懂的不變式：**結束日不可早於開始日**。

**守住的不變式**：
- 既有（不動）：`sh_trial` 與 `sh_trial_end_date` 兩欄維持 inert
- **新**：結束日 >= 開始日
- **新**：next_payment 對齊 plan 計算式

**為何補在這條 test**：原 test 已建一個非試用訂閱、檢查試用欄位 inert 狀態，但漏掉日期不變式。在「現場」test 補一條 invariant 比另開新 test 雜訊小。Test 1 在新 class、Test 2 在舊 class，兩個 fixture seed 都各驗一次，覆蓋面更廣。

### Test 3 — `TestSmartHomeChangePlanTrialMode.test_trial_to_no_trial_enters_active`（既有強化）

**Business intent**：模擬使用者從「試用方案」切換到「非試用方案」（change-plan 流程）。原 test 守「切換成功 + 新訂閱不在試用 + 退款為 0」三件事。本次加上同一條不變式，並進一步斷言 end 與 next_payment 對齊 plan 計算式。

**守住的不變式**：
- 既有（不動）：切換成功、新訂閱 `sh_trial == False`、`refund_amount == 0`
- **新**：結束日 >= 開始日
- **新**：end 與 next_payment 對齊 plan 計算式

**為何補在這條 test**：這是 PR #35（change-plan trial→no-trial）的「現場」test，也是 reviewer 加 diag 找到本 bug 的入口。把不變式加在這裡，下次再有 regression 會直接從這條 test 浮上來。

---

## What we're NOT testing — 故意不測（含理由與追蹤路徑）

| 不測項目 | 不測理由 | 對稱 / 風險 / 未來追法 |
|---|---|---|
| **CloudService caller side fresh non-trial test** | mixin 共用同一 helper；CloudService 與 SmartHome 兩個 caller 走完全相同的 path；issue body 點名的 stg incident 是 SmartHome path | 與 SmartHome 等價；CS 若浮出 surface bug，spawn 獨立 issue（plan Q1 拍板：不加） |
| **多種 `sh_unit` 矩陣（year / week / day）** | PR #38 trial helper test 也只覆蓋 `month`；`cycle_delta` lookup table 同結構，覆蓋一個 unit 已足夠驗證計算路徑 | 與 trial path 取樣策略一致；over-specification 風險低 |
| **`_force_active_dates_from_plan` 直接呼叫的單元測試** | private convenience helper，由 `_create_and_confirm_subscription` integration test 已完整 cover；單測 helper 反而會耦合內部實作 | 與 trial helper 取樣策略一致 |
| **DB backfill / migration test** | 本 PR 純 code path 修改，不動既有 stg/prod 已存在的 `end < start` 殘留資料。資料殘留財務面已被 refund clamp 守住，UI cosmetic 影響可接受、會自然 churn（下次 change-plan / 續訂時新 sub 重建） | Backfill 若需做，獨立 issue 含 dry-run + rollback plan（plan Q2 拍板：不做） |
| **`_compute_unused_refund` clamp 收緊成 strict（raise）** | legacy stg/prod 還有 `end < start` 殘留，收緊成 `raise` 會炸 prod refund cron；應排在 backfill issue 之後做 | 列為 follow-up issue，依賴 backfill issue 完成（plan Q3 拍板：不做） |
| **Refund 用 fresh non-trial sub 跑** | clamp 測試已由 PR #38 commit 4 cover；fresh non-trial sub 修完後 `end >= start`，refund 不會踩到 clamp，重複測會 noise | 既有 PR #38 refund test 已守 |

---

## Test-to-bug shape map（reviewer 直接驗算）

`today = 2026-05-17`（測試執行當日）。三個 test 共用同一組 plan 參數，會在同一組數字上 fix 前 fail、fix 後 pass。

| 元件 | 值 | 來源 |
|---|---|---|
| `plan.sh_duration` | 1 | 三個 test 共用 fixture |
| `plan.sh_unit` | `month` | 三個 test 共用 fixture |
| `plan.sh_no_of_billing_cycle` | −1 | 平台所有方案 default（unlimited billing） |
| `sh_start_date`（onchange 寫對） | `2026-05-17` | today |
| **`sh_end_date`（buggy；fix 前）** | `2026-04-16` | `today + 1 × (−1) × month − 1d` |
| **`sh_end_date`（expected；fix 後）** | `2026-06-16` | `today + 1 × month − 1d`（**不乘** `sh_no_of_billing_cycle`） |
| **不變式 `end >= start`** | fix 前違反 31 天；fix 後成立 | 三個 test 都斷言 |

Reviewer 可把日期直接打進計算機手算驗證；不需要進 Python 也能對得上。

---

## Symmetry with PR #38（reviewer 對照 diff 用）

本 PR 在 code 層 mirror PR #38 trial path，test 層也對應補一份：

| PR #38（trial path） | 本 PR #39（active path） |
|---|---|
| Helper `_force_trial_dates_from_plan` | Helper `_force_active_dates_from_plan`（sibling） |
| `if plan.sh_trial:` 分支 dispatch | `else:` 分支 dispatch（同 caller、同位置） |
| 寫 7 個欄位（start/end/next_payment + 4 個 trial-related） | 寫 3 個欄位（start/end/next_payment） |
| `TestForceTrialDatesFromPlan` class | `TestForceActiveDatesFromPlan` class（鏡像 fixture / 鏡像命名） |
| `test_fresh_trial_plan_writes_full_date_triple` | `test_fresh_non_trial_plan_writes_active_date_triple` |
| `test_dedup_collision_still_forces_trial_dates` | —（非試用 path 無 dedup 攻擊面，刻意不對稱） |
| `test_non_trial_plan_leaves_trial_fields_inert`（只驗 trial fields inert） | 同一 method 強化加 `end >= start` invariant |

**Reviewer 讀 diff 順序建議**：

1. `subscription_lifecycle_mixin.py` 的 helper + dispatch 兩處改動 → 對應 §Why
2. `TestForceActiveDatesFromPlan` 新 class → 對應 §Test 1
3. `TestForceTrialDatesFromPlan.test_non_trial_plan_leaves_trial_fields_inert` diff → §Test 2
4. `test_smart_home_subscription_flow.py` 的 change-plan test diff → §Test 3

---

## 隱性假設 / Fixture 條件（reviewer 與重跑者要知道）

1. **`sh_journal_id` 必須有值**：fresh K8s sandbox 上 `res_company.sh_journal_id` 預設為 NULL（`post_init_hook` 在 install 時 wire 不到 sale journal），會讓 `_create_and_confirm_subscription` raise `UserError`。重跑前需確認該欄位指向 type=`sale` 的 journal（通常為 id=1 "Customer Invoices"）。
2. **Owner 用 `base.group_user` 唯一 group**：三個 test 的 owner 都刻意不放 admin / manager group（PR #28 hardening），避免 ACL 隱性 bypass 干擾 onchange chain。
3. **Test 3 故意 force `sh_end_date = NULL`** 模擬 PR #34 stg 歷史殘留（trial 訂閱在 historical state 上 end_date 可能是 NULL），是 regression baseline 的一部分，非 bug。

---

## How to re-run（reviewer 用）

三個 mandatory test 一次跑完：

```bash
SB=dev-sandbox-fix-sh-end-date-fresh-non-trial
DB=woow_fix_sh_end_date_fresh_non_trial

kubectl exec -n $SB deploy/fix-sh-end-date-fresh-non-trial-odoo-dev-sa-odoo -- \
  odoo --test-enable \
  --test-tags /woow_paas_platform:TestForceActiveDatesFromPlan,\
/woow_paas_platform:TestForceTrialDatesFromPlan.test_non_trial_plan_leaves_trial_fields_inert,\
/woow_paas_platform:TestSmartHomeChangePlanTrialMode.test_trial_to_no_trial_enters_active \
  --stop-after-init --http-port=8079 --gevent-port=8080 \
  --log-level=test -d $DB -c /etc/odoo/odoo.conf
```

Regression（PR #38 trial path 不可動到）— 追加 tag 即可：

```
/woow_paas_platform:TestForceTrialDatesFromPlan.test_fresh_trial_plan_writes_full_date_triple
/woow_paas_platform:TestForceTrialDatesFromPlan.test_dedup_collision_still_forces_trial_dates
```

**預期結果**：fix 前三個 mandatory test 全 FAIL（AssertionError on end_date / invariant）；fix 後全 PASS；trial-path regression 永遠 PASS。三個 test 各約 0.5s + 1.6s 模組載入 overhead，總 wall clock < 30s。
