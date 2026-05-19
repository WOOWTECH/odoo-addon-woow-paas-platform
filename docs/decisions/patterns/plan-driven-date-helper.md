# Pattern: plan-driven date helper（with post-confirm coupling）

- 日期：2026-05-17
- 涉及檔案：`src/models/subscription_lifecycle_mixin.py`
- 相關 PR：
  - PR #38 / issue #33 — `_force_trial_dates_from_plan`（trial path）
  - PR #42 / issue #39 — `_force_active_dates_from_plan`（non-trial path）
- 相關第三方公式：`sh_subscription/models/sh_subscription_subscription.py`
  - `_onchange_sh_trial_subcription_end_date` 寫 `sh_end_date`
  - `sh_subscription_confirm()` line 274–304 寫 `sh_date_of_next_payment`

## When to use

當你要建立 `sh.subscription.subscription` 紀錄，而第三方 `sh_subscription` module 既有的
onchange chain 對 platform 特定的 plan shape（`sh_no_of_billing_cycle = -1` unlimited
billing、或 `sh_never_expire = True`）算出來的日期不可信時。

具體觸發點：`SubscriptionLifecycleMixin._create_and_confirm_subscription` 是平台唯一
建立 subscription 的入口（被 `smart.home` / `cloud.service` 共用），這條路徑需要 plan-driven
helper 覆寫 onchange chain 結尾的值，才能保證下游讀者拿到 coherent date triple。

## What it looks like

對稱兩個 helper（trial + active），分支 dispatch 在 mixin 內：

```python
# subscription_lifecycle_mixin.py:159-174
subscription._onchange_sh_subscription_plan_id()
subscription._onchange_sh_product_id()
subscription._onchange_sh_trial_subcription_start_date()
subscription._onchange_sh_trial_subcription_end_date()

# Always override the onchange-chain-computed dates with plan-driven
# values; the third-party onchange compute is unreliable on both
# branches (issue #33 for trial, #39 for non-trial). See helper
# docstrings for the specific arithmetic each branch corrects.
if plan.sh_trial:
    self._force_trial_dates_from_plan(subscription, plan)
else:
    self._force_active_dates_from_plan(subscription, plan)

# Confirm → in_progress (starts trial if configured)
subscription.sh_subscription_confirm()
```

| 對稱面 | `_force_trial_dates_from_plan`（PR #38） | `_force_active_dates_from_plan`（PR #42） |
|---|---|---|
| `cycle_delta` lookup | `dict(day/week/month/year=...).get(plan.sh_unit)` | 同（5 行字面重複） |
| `end_date` 公式 | `start + cycle - 1d` | `start + cycle - 1d` |
| `sh_date_of_next_payment` 寫法 | `start_date` | `start_date` |
| `sh_start_date` | `trial_end + 1d`（**未來**） | `today` |
| 寫入欄位數 | 7（含 4 個 trial 欄位） | 3 |
| _logger.info 行為 | "Forced trial for ... billing %s → %s" | "Forced active dates for ... billing %s → %s" |

兩個 helper 唯一 production caller 是 `_create_and_confirm_subscription:169` / `:171`
（grep 驗證過）。Test code 只透過該 caller 間接觸發。

## Why this pattern

### 1. 第三方 onchange 對 platform plan shape 不可信

`sh_subscription` 的 `_onchange_sh_trial_subcription_end_date` 把 `cycle_delta` 乘
`sh_no_of_billing_cycle`：

```
cycle_delta = sh_recurrency * sh_no_of_billing_cycle * unit
```

Platform 每張 plan 都設 `sh_no_of_billing_cycle = -1`（unlimited billing 語意）。
代入後 `cycle_delta = 1 * -1 * month = -1 month`，於是 `sh_end_date = start - 31d`，
違反 `end >= start` 不變式 31 天。

Prod plan 另設 `sh_never_expire = True`，會把 onchange 整個 gate off，於是
`sh_end_date` 落在 NULL — 同樣壞。Helper 不讀 `sh_never_expire`，自己從
`plan.sh_duration` / `plan.sh_unit` 算 cycle，兩種 plan shape 都被守住。

### 2. Plan-driven helper 必須在 onchange chain **之後**

Dispatch 位置（`:168-171`）在四個 onchange 顯式呼叫之後、`sh_subscription_confirm()`
之前。若 helper 在 onchange 之前跑，onchange 會把 helper 的寫入再覆蓋回壞值。
Helper 必須是「last word」才有意義。

### 3. 為什麼不改第三方 module

`sh_subscription` 是第三方 addon，platform 不該 monkey-patch；其他依賴它的下游
路徑（手動建立 subscription、UI form view）可能依賴現有 onchange 行為。Plan-driven
helper 在 platform 自己的抽象層補洞，影響面侷限於 `_create_and_confirm_subscription`
這條路徑。

## Helper–confirm coupling（最重要的隱性 invariant）

**Helper body 寫 _pre-confirm_ state；plan acceptance criterion 守 _post-confirm_ state。**

兩個 helper 都寫 `sh_date_of_next_payment = start_date`，但 post-confirm 的觀察值
不同 — 差異由 `sh_subscription_confirm()` 的 conditional advancement 自然產生：

```python
# sh_subscription_subscription.sh_subscription_confirm() :274-300（節錄）
if fields.Date.today() >= self.sh_start_date:
    invoice_vals = self._generate_invoice_vals()
    invoice_id = self.env['account.move'].sudo().create(invoice_vals)
    ...
    next_payment_date = self.sh_date_of_next_payment + \
                        relativedelta(months=self.sh_recurrency)  # for 'month'
    if next_payment_date:
        self.sh_date_of_next_payment = next_payment_date
```

| Helper | `sh_start_date` | confirm 條件 `today >= start` | confirm 是否 advance | post-confirm `sh_date_of_next_payment` |
|---|---|---|---|---|
| Trial | `trial_end + 1d`（未來） | **False** | 不 advance | `start_date`（= trial_end + 1d） |
| Active | `today` | **True**（==） | advance by 1 cycle | `start + cycle_delta` |

**兩個 helper body byte-identical（都寫 `start_date`），但 post-confirm 行為不同**
— 這是 by design，不是 bug。Trial test 斷言 `next_payment == start_date`，
non-trial test 斷言 `next_payment == start + cycle_delta`，**divergence 是 confirm 行為
而不是 helper 行為**。

## Takeaway：plan author 一定要寫清楚是哪個 state

PR #42 的 plan D2 規定 helper body 寫 `sh_date_of_next_payment = start + cycle_delta`，
acceptance criterion #1 也斷言 `start + cycle_delta`。這在 red phase 立刻爆掉：
helper 寫 `start + cycle`，confirm 又 advance 一次變 `start + 2*cycle`（實測
`2026-07-17 != 2026-06-17`）。Fix 是改 helper 寫 `start_date`，confirm advance 一次
正好抵到 `start + cycle_delta`，post-confirm state 符合 acceptance criterion。

教訓：

1. **若 helper 後面緊接著有 ORM hook（confirm、computed write、stored compute），
   plan 必須區分 helper 內部 state vs post-callsite observable state。**
   acceptance criterion 是 post-callsite，helper body 是 pre-callsite。混在一起寫
   會被 hook 的 conditional 行為翻車。

2. **下次加第三個 helper（例如 paused state）一併評估是否該把 post-confirm 行為
   explicit 加進 test assertion 或 helper docstring。** 現在依賴 `_force_*_dates_from_plan`
   helper 命名隱含「create lifecycle 內、後面有 confirm」，這個約束沒有被 type 或
   constraint 強制 — 是 [[pitfall-helper-confirm-coupling]] 的 latent risk。

3. **若要 unit test helper 本身（不透過 `_create_and_confirm_subscription`），
   斷言要對 pre-confirm state**，不能用 plan acceptance criterion 那條值。但
   convention 是用 integration test 走完整 path（含 confirm），所以 unit test
   helper 直接呼叫是反 pattern。

## Cycle_delta lookup 共用 — 已知重複，可接受

兩個 helper 各有一個 5 行 `cycle_delta` lookup（`subscription_lifecycle_mixin.py:199-204`
trial 與 `:235-240` active），字面重複 9 LoC。Plan C4 列為 optional refactor、未做。
若要抽 helper，建議同時加：

- `UserError` for unknown `sh_unit`（取代 silent `relativedelta()` fallback）
- `sh_duration >= 1` constraint on `sh.subscription.plan` model
- 共用 `_CYCLE_UNITS` module 常數或 `_plan_cycle_delta(plan)` helper

詳見 issue #39 reviewer report Recommendation 段 follow-up #1。**單純 rename 不值得，
加 guard 一起做才有 ROI。**

## See also

- 程式碼：`src/models/subscription_lifecycle_mixin.py:159-251`
- Plan docs：
  - `docs/plans/2026-05-17-sh-end-date-fresh-non-trial.md`（PR #42）
  - PR #38 plan（trial path 對應 plan，記憶位置 `docs/plans/2026-05-*-trial-dates-from-plan*.md`）
- Test report：`docs/test-reports/issue-39-test-coverage.md`
- 第三方公式：`sh_subscription/models/sh_subscription_subscription.py`（onchange 公式 line 180-186；confirm advancement line 274-304）
- 相關 invariant：refund clamp `src/models/sh_subscription_fix.py`（`_compute_unused_refund` 的 `max(0, end-today)` + `today<start→0` guard 守住財務面，UI 下游讀者沒有對等 clamp — 這是 helper 必須存在的原因）
