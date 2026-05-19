# Plan: fix `sh_end_date < sh_start_date` on fresh non-trial subs (issue #39)

## Context

PR #38 修了 trial path 的 NULL / `start - 1d` bug（`_force_trial_dates_from_plan`
unconditional 寫 trial 日期 triple），但 helper gated 在 `if plan.sh_trial:`，
**non-trial path 完全沒進新邏輯**。`_create_and_confirm_subscription` 依靠
`sh_subscription` 第三方 module 的 onchange chain
(`_onchange_sh_trial_subcription_end_date`) 計算 non-trial sub 的
`sh_end_date`，公式為 `start + sh_recurrency * sh_no_of_billing_cycle * unit - 1d`。
我們所有 plan 設 `sh_no_of_billing_cycle = -1`（unlimited billing），所以
`cycle_delta = 1 * -1 month = -1 month`，結果 `sh_end_date = sh_start_date - 31d`。

Reviewer 在 PR #38 R1 review S1 段、PR #35 R1 review S2 段都觀察到這個
shape — trial 那半邊已在 PR #38 修，本 issue 處理 **non-trial 那半邊**。

財務面：`_compute_unused_refund` 的 `max(0, end - today)` clamp + `today < start → 0`
guard 守住 refund（PR #38 已加 `test_refund_zero_when_end_before_start` 驗證），
所以 wrong-value `end_date` 不會導致退款溢付。但 UI countdown / grace-period /
cleanup-window 任何讀 `sh_end_date` 而不讀 `sh_date_of_next_payment` 的下游
讀者都會拿到錯資料。next_payment 計算正確不受影響。

目標：non-trial 走跟 trial 對稱的 plan-driven path，永遠不依賴第三方 onchange
給出 coherent date triple。

## Decisions

### D1. 修法 — 採用 issue body 選項 (a)：sibling helper `_force_active_dates_from_plan`

challenge 過 issue body 列的選項 (b) unify into single helper：

| | (a) sibling helper（推薦） | (b) unify into `_force_dates_from_plan` |
|---|---|---|
| Helper 名稱 vs 行為 | 兩個 helper 名稱描述各自意圖 | 「force trial」名稱對 non-trial path 語意錯 |
| 欄位 set | trial 寫 7 個欄位 / active 寫 3 個 | 一個 helper 內部分支寫不同 set |
| Diff size | 新增 helper + 加 `else:` 1 行 | rename + 加內部分支 + 改 docstring |
| 既有 caller | trial helper signature 不動，PR #38 test 不需 touch | rename + 改所有 caller |
| 抽 cycle_delta 共用 | 可選 follow-up（兩個 helper 都用同 5 行 lookup） | helper 內天然共用 |

**結論：(a)。** Tradeoff 是 `cycle_delta` 5-line lookup table 會在兩個 helper 重複
（不抽 helper 即 9 LoC 複製）— 列為 commit 4 optional refactor，不強制。
若 reviewer 不喜歡，可在實作階段直接抽 `_plan_cycle_delta(plan)` module-level
helper 解掉。

### D2. Helper signature 與寫入欄位

```python
def _force_active_dates_from_plan(self, subscription, plan):
    """Force start/end/next-payment fields on a non-trial ``subscription``."""
    cycle_delta = {
        'day':   relativedelta(days=plan.sh_duration),
        'week':  relativedelta(weeks=plan.sh_duration),
        'month': relativedelta(months=plan.sh_duration),
        'year':  relativedelta(years=plan.sh_duration),
    }.get(plan.sh_unit, relativedelta())
    start_date = date.today()
    end_date = start_date + cycle_delta - relativedelta(days=1)
    subscription.write({
        'sh_start_date': start_date,
        'sh_end_date': end_date,
        'sh_date_of_next_payment': start_date + cycle_delta,
    })
```

寫入欄位：
- `sh_start_date = today`
- `sh_end_date = start + cycle_delta - 1d`（**不乘** `sh_no_of_billing_cycle`，
  避開第三方 `-1` 設定）
- `sh_date_of_next_payment = start + cycle_delta`

刻意 **不寫**：
- `sh_trial = False` — plan-driven onchange 已正確設好；redundant write 增加
  side-effect 風險。
- `sh_trial_end_date`、`sh_trial_duration`、`sh_trial_unit` — non-trial 不需要。

### D3. Dispatch 在 `_create_and_confirm_subscription`

現有 line 173-174：

```python
if plan.sh_trial:
    self._force_trial_dates_from_plan(subscription, plan)
```

改為：

```python
if plan.sh_trial:
    self._force_trial_dates_from_plan(subscription, plan)
else:
    self._force_active_dates_from_plan(subscription, plan)
```

Dispatch 位於所有 onchange chain 跑完之後，跟 trial helper 同位置，確保
plan-driven write 永遠 last word。

### D4. Schema 不動，不做 DB backfill

純 code path 改寫，不動任何欄位定義、不動 migration。

既有 stg/prod **已存在** `end < start` 的 non-trial sub 怎麼處理：

- 財務面：`_compute_unused_refund` 兩個 clamp（`today < start → 0`、
  `max(0, end - today)`）守住，refund 不會溢付。PR #38 commit 4 已加
  `test_refund_zero_when_end_before_start` 守住此 invariant。
- UI 面：cosmetic 影響。新建 non-trial sub 修完後乾淨；既有 bad 資料需
  「自然 churn」（下次 change-plan 或續訂時新 sub 重建）。
- **建議不 backfill**。理由：
  1. clamp 已守住財務正確性
  2. backfill query 需鎖定哪些 sub 真的 end<start（不是 trial、不是已 cancel）
  3. 風險 / 收益不對稱：誤改 prod 資料的成本 > UI cosmetic 修復
- ⚠ **Open question to leader**：若要 backfill 需開另一個 issue（資料修
  + dry-run + rollback plan）。**本 PR scope 不含**。

### D5. `_compute_unused_refund` clamp 仍 load-bearing — 本 PR 不收緊成 strict

Issue body 提及 "or 1 fallback / max(0, ...) clamp 退回成 strict mode" 是可選工作。
本 PR **不做**。理由：

- 既有 stg/prod legacy 資料還有 `end < start` 殘留（見 D4），收緊成
  `raise` 會炸掉 production refund 計算
- backlog gotcha #5 已記 clamp 是 coincidence 而非 design intent，下次重構
  可獨立評估
- 列為 follow-up：「等 non-trial fresh-create 路徑修完 + 確認 prod 無
  end<start 殘留後，可獨立 PR 把 clamp 改 strict」

## Rejected options

- **(b) Unify into `_force_dates_from_plan`** — 見 D1 對比表。名稱對 non-trial
  語意錯，且 diff 比 (a) 大。
- **Backfill prod / stg `end < start` 資料** — 見 D4。clamp 已守住財務面，
  cosmetic 修復不值得 prod 資料風險。
- **收緊 `_compute_unused_refund` 為 strict（`raise` instead of clamp）** — 見 D5。
  legacy 資料還在，現在收緊會炸 prod。
- **改第三方 `sh_subscription._onchange_sh_trial_subcription_end_date`** —
  第三方 module，不該改。Plan-driven helper 是正確抽象層。
- **直接砍 `_onchange_sh_trial_subcription_end_date` 的 trigger 連動** —
  影響面不可預估（其他 path 可能依賴），冒不必要的險。

## Acceptance criteria

1. **Fresh create non-trial sub invariant**：透過
   `_create_and_confirm_subscription(partner, non_trial_plan)` 建立的新
   subscription 必須滿足：
   - `sh_trial == False`
   - `sh_start_date == today`
   - `sh_end_date >= sh_start_date`
   - `sh_end_date == sh_start_date + cycle_delta - 1d`（cycle_delta 由
     `plan.sh_duration` * `plan.sh_unit`，**不乘** `sh_no_of_billing_cycle`）
   - `sh_date_of_next_payment == sh_start_date + cycle_delta`
2. **Trial path regression 不變**：PR #38 引入的 `TestForceTrialDatesFromPlan`
   三個 case 全綠（trial path code 完全沒動）。
3. **PR #35 trial→no-trial change-plan regression**：
   `test_trial_to_no_trial_enters_active` 強化後，新 sub 滿足 D2 的所有
   invariant（不只是 `sh_start_date == today`，還要 `end >= start`、
   `next_payment` 正確）。
4. **既有 refund clamp 不退化**：`test_change_plan_refund.py` 全部既有 test
   case 全綠（特別是 PR #38 commit 4 的 boundary cases）。

## Affected files

| 檔案 | 變更類型 | 描述 |
|------|---------|------|
| `src/models/subscription_lifecycle_mixin.py` | M | 新增 `_force_active_dates_from_plan` helper（在 `_force_trial_dates_from_plan` 之後，約 line 225 後）；`_create_and_confirm_subscription` line 173-174 的 `if plan.sh_trial:` 加 `else:` 分支 |
| `src/tests/test_subscription_lifecycle.py` | M | 新增 `TestForceActiveDatesFromPlan` class（鏡像既有 `TestForceTrialDatesFromPlan` 結構）；強化既有 `test_non_trial_plan_leaves_trial_fields_inert` 加 `end >= start` invariant |
| `src/tests/test_smart_home_subscription_flow.py` | M | 強化既有 `test_trial_to_no_trial_enters_active`（PR #38 reviewer 找到 bug 的「現場」test）加 end-date / next-payment invariant 斷言 |

**不動**：
- `src/models/sh_subscription_fix.py`（refund clamp 留著）
- `src/models/cloud_service.py`、`src/models/smart_home.py`、`src/models/smart_home_template.py`
- `src/controllers/`、frontend、views、security
- 任何 schema / migration / data file

## Test scope 建議（給 tester）

### Must cover (mandatory)

1. **`test_fresh_non_trial_plan_writes_active_date_triple`**（新，在新
   `TestForceActiveDatesFromPlan` class 中）：
   - Setup：non-trial plan（`sh_trial=False`, `sh_duration=1`, `sh_unit='month'`,
     `sh_no_of_billing_cycle=-1`）
   - Act：`smart_home._create_and_confirm_subscription(partner, plan_notrial)`
   - Assert：D2 寫入欄位全對；特別斷言 `sh_end_date >= sh_start_date`
2. **強化 `test_non_trial_plan_leaves_trial_fields_inert`**（既有 test，
   `TestForceTrialDatesFromPlan` 內）：
   - 既有斷言：`sh_trial == False`、`sh_trial_end_date == False`（不變）
   - 新增斷言：`sh_end_date >= sh_start_date`、`sh_date_of_next_payment == sh_start_date + cycle_delta`
3. **強化 `test_trial_to_no_trial_enters_active`**（既有 test，
   `TestSmartHomeChangePlanTrialMode` 內）：
   - 既有斷言：`sh_trial == False`、`sh_start_date == today`（不變）
   - 新增斷言：`sh_end_date >= sh_start_date`、
     `sh_end_date == sh_start_date + relativedelta(months=1) - timedelta(days=1)`、
     `sh_date_of_next_payment == sh_start_date + relativedelta(months=1)`

### Regression（既有 test 跑過綠即可，不新增）

- `TestForceTrialDatesFromPlan` 三個 case（PR #38 已加）— trial path 行為
  不可動到
- `test_change_plan_refund.py` 整套（特別是 PR #38 commit 4 的
  `test_refund_zero_when_end_before_start` 等 clamp boundary）
- `test_active_to_active_regression_proration`（active→active proration，
  fixture 手動 force `sh_end_date = today + 15d` 不走我們修的 path，但
  跑一下確認沒誤傷）

### 故意不測（與理由）

- **CloudService 版本的 `_create_and_confirm_subscription`**：mixin 共用同一
  helper，CloudService caller 跟 SmartHome 等價。issue body 點名的 stg
  incident 是 SmartHome path。CloudService 各 cover 一個會 noise；若日後 CS
  reported 問題另開 issue。⚠ **Open question to leader**：要不要保險也加一個
  CS test。
- **non-trial plan 各種 `sh_unit` 組合（year / week / day）**：trial helper
  的 test 也只 cover `month` + `day`。`cycle_delta` lookup 邏輯跟 trial 相
  同，覆蓋一個 unit 即可，多 unit 是 over-specification。
- **`_force_active_dates_from_plan` 單獨直接呼叫的單元測試**：private
  convenience helper，cover by `_create_and_confirm_subscription` integration
  test 即可。
- **DB backfill / migration test**：見 D4，本 PR 不做 backfill。
- **`_compute_unused_refund` strict 模式 test**：見 D5，本 PR 不收緊 clamp。
- **`_compute_unused_refund` 直接用 fresh non-trial sub 跑 refund**：clamp
  測試已由 PR #38 commit 4 cover，重複測 noise。

### Cycle delta 計算（明確寫出）

```
cycle_delta = plan.sh_duration * plan.sh_unit
```

範例：
- `sh_duration=1`, `sh_unit='month'` → `relativedelta(months=1)`
- `sh_duration=3`, `sh_unit='month'` → `relativedelta(months=3)`
- `sh_duration=1`, `sh_unit='year'` → `relativedelta(years=1)`

**絕對不乘** `sh_no_of_billing_cycle`，因為 platform 所有 plan 都設 `-1`
（unlimited），乘進去就是 root cause。

## PR 邊界（commit plan）

`fix/sh-end-date-fresh-non-trial`（base = `develop`，已切 branch）。

| # | 類型 | 描述 |
|---|------|------|
| C1 | `fix(subscription)` | Add `_force_active_dates_from_plan` helper + dispatch in `_create_and_confirm_subscription`（issue body 列的 commit 1 + 2 合併 — refactor-only commit 沒有 caller 是 dead code，合併更乾淨） |
| C2 | `test(subscription)` | 新增 `TestForceActiveDatesFromPlan` + 強化 `test_non_trial_plan_leaves_trial_fields_inert` |
| C3 | `test(subscription)` | 強化 `test_trial_to_no_trial_enters_active`（PR #35 trial→no-trial regression） |
| C4 (optional) | `refactor(subscription)` | 抽 `cycle_delta` lookup 為 module-level helper `_plan_cycle_delta(plan)`，trial + active helper 共用 — reviewer 喜歡再做，不喜歡可割捨 |

預計 3 commits（C4 看 reviewer 偏好決定），符合 issue body 的「3-5 commits」邊界。

## 風險清單

| 風險 | 影響 | 緩解 |
|------|------|------|
| 既有 prod / stg `end < start` non-trial sub 資料殘留 | UI countdown cosmetic 錯誤；refund 不受影響（clamp 守住） | 不 backfill（D4）；若後續 surface UI bug，獨立 issue 處理 |
| 收緊 `_compute_unused_refund` clamp 後沒清乾淨 legacy 資料導致 prod 炸 | refund 計算 raise，churn 大 | 本 PR 不動 clamp（D5），列 follow-up issue |
| `else:` 分支 dispatch 漏 edge case（plan.sh_trial 是 NULL / None 的詭異資料） | 走 trial helper 寫 trial fields 進 non-trial sub | 對齊 issue body 預期：`plan.sh_trial` 是 boolean 欄位 default False，None 視同 False；既有 trial helper 的條件已用 `plan.sh_trial`（truthy 判斷），對稱即可 |
| `_force_active_dates_from_plan` 寫入 `sh_date_of_next_payment` 覆蓋了 onchange chain 算對的值 | 行為退化（但 onchange chain 在 non-trial 上會算錯，本來就是 root cause） | helper 寫的 next_payment = `start + cycle_delta` 跟 issue body root cause 段一致；test C3 斷言這個值 |
| CloudService caller 走同 path 沒測，未來 surface CS-side bug | 視野盲區 | 列 open question 給 leader 拍板 |

## Out of scope / spawn issue

- **DB backfill 既有 `end < start` non-trial sub**：獨立 issue
  「audit + backfill end<start non-trial subs in prod/stg」+ dry-run plan + rollback。
- **`_compute_unused_refund` 收緊成 strict（raise instead of clamp）**：獨立
  issue「remove load-bearing clamps in refund calc once legacy data is
  audited」。Depends on backfill issue。
- **抽 `_plan_cycle_delta(plan)` module-level helper**：若 C4 沒做，列 chore
  issue。
- **CloudService side 對稱的 `_create_and_confirm_subscription` test**：若
  Open Question 1 拍板「先不加」，列 follow-up「CS-side fresh non-trial sub
  date invariant test」（low-priority — mixin 共用 helper 邏輯上等價）。

## Open questions（需 leader / 使用者拍板）

1. **CloudService caller side 要不要也加 fresh non-trial test？**
   - 建議：不加（mixin 共用 helper，SH cover 即等價）；若風險偏好保守可加一個 CS 對稱 test
2. **既有 stg/prod `end < start` non-trial 殘留資料要不要 query + backfill？**
   - 建議：不 backfill（clamp 已守住財務面），列 follow-up issue
3. **`_compute_unused_refund` clamp 收緊成 strict 是否同步做？**
   - 建議：本 PR 不做（legacy 資料還在），列 follow-up
4. **C4 optional refactor（共用 `_plan_cycle_delta` helper）是否強制？**
   - 建議：交 reviewer 決定；planner 中性偏「不強制」（5 行 lookup 重複可接受）

