# Pattern: plan-driven date helper（with post-confirm coupling）

- 日期：2026-05-17（#367 per-entity trial guard 更新見下方兩顆 commit）
- 涉及檔案：`src/models/subscription_lifecycle_mixin.py`
- 相關 PR：
  - PR #38 / issue #33 — `_force_trial_dates_from_plan`（trial path）
  - PR #42 / issue #39 — `_force_active_dates_from_plan`（non-trial path）
  - issue #367 — trial reset guard，**兩次落地**：`bc6dd22e`（per-partner 版，`Closes #367`）→ `30409935`（改為 per-entity，`Refs #367`）。先做成 per-partner 再改成 per-entity，看 guard 的語意時**兩顆都要看**，只讀後面那顆會漏掉「為什麼不是 per-partner」的脈絡。
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
# subscription_lifecycle_mixin.py:169（entry，Subscription.create() 之前）
# #367：先抓「此 entity 是否已持有訂閱」。方法尾端的
# self.write({'subscription_id': ...})（:234）會覆蓋它，所以必須在這裡讀。
entity_has_prior_sub = bool(self._sub())

# subscription_lifecycle_mixin.py:211-232
subscription._onchange_sh_subscription_plan_id()
subscription._onchange_sh_product_id()
subscription._onchange_sh_trial_subcription_start_date()
subscription._onchange_sh_trial_subcription_end_date()

# Always override the onchange-chain-computed dates with plan-driven
# values; the third-party onchange compute is unreliable on both
# branches (issue #33 for trial, #39 for non-trial). See helper
# docstrings for the specific arithmetic each branch corrects.
#
# #367（per-entity）：`plan.sh_trial` 只回答「這張 plan 有沒有 trial」，
# **不是**「這次該不該給 trial」。少了 entity_has_prior_sub 這個 conjunct，
# 換方案會重啟 7 天試用且首期扣 0——詳見 §Why 4。
if plan.sh_trial and not entity_has_prior_sub:
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
| 寫入欄位數 | 7（含 4 個 trial 欄位） | 5（含 #367 清 trial 的 2 個欄位） |
| trial 旗標 | 寫 `sh_trial=True` | 寫 `sh_trial=False` 並清 `sh_trial_end_date`（#367：active 分支現在對 trial plan 也可達） |
| _logger.info 行為 | "Forced trial for ... billing %s → %s" | "Forced active dates for ... billing %s → %s" |

兩個 helper 唯一 production caller 是 `_create_and_confirm_subscription:227` / `:229`
（grep 驗證過）。Test code 只透過該 caller 間接觸發。
（`:169` 現在是 `entity_has_prior_sub` 的捕捉點，別再拿它指 helper。）

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

Dispatch 位置（`:226-229`）在四個 onchange 顯式呼叫之後、`sh_subscription_confirm()`
之前。若 helper 在 onchange 之前跑，onchange 會把 helper 的寫入再覆蓋回壞值。
Helper 必須是「last word」才有意義。
（`entity_has_prior_sub` 是唯一的例外：它必須在 `Subscription.create()` **之前**讀，見 §Why 4。）

### 3. 為什麼不改第三方 module

`sh_subscription` 是第三方 addon，platform 不該 monkey-patch；其他依賴它的下游
路徑（手動建立 subscription、UI form view）可能依賴現有 onchange 行為。Plan-driven
helper 在 platform 自己的抽象層補洞，影響面侷限於 `_create_and_confirm_subscription`
這條路徑。

### 4. Trial 的 dispatch 條件是 plan × entity，不是 plan（#367）

`_force_trial_dates_from_plan` 無條件寫 `sh_trial=True`，會蓋掉第三方 onchange 自己算的
`sh_trial = not (sub_exists and sub_exists[-1].id != self.id) and plan.sh_trial`
（`sub_exists` 搜 partner × plan × product）。platform seed 的 plan 幾乎全 `sh_trial=True`，
所以「只照 plan 決定」＝「每次建 sub 都給 trial」。

Trial 語意是 **per-service（per-entity）一生一次**（#367 的第二顆 commit `30409935`；
第一顆 `bc6dd22e` 做的是 per-partner，**後來被改掉**）：

- 全新 entity（含同一 partner 的第 2、3 個 service）→ `subscription_id` 空 → 各自享有 trial
- 同一 service 換方案 / 加席 → change-plan 只把舊 sub 設 `close`、**不解除** `subscription_id`
  → truthy → 不給 trial，走 active dates 首期實扣
- suspend/resume → `_do_resume` 不重建 sub，走不到這條路 → trial 不受影響

判準欄位是 entity 自己的 `subscription_id`（經 `self._sub()`），且**必須在 `create()` 與
方法尾端 `self.write({'subscription_id': ...})` 之前讀**——這是它跟其他 plan-driven 值
（在 onchange 之「後」才寫）方向相反的地方。

⚠ 措辭精度：判準是 `subscription_id` 是否 truthy（**任何既往訂閱**），不是「是否曾用過 trial」。
一個先訂非-trial plan、之後才換到 trial plan 的 entity，會在從未享用過 trial 的情況下被拒。

⚠ **拿掉這個 conjunct 的後果不是日期算錯，是計費繞過。** trial 讓 `sh_start_date` 落在未來，
`sh_subscription_confirm()` 的 `if fields.Date.today() >= self.sh_start_date:`（第三方 `:280`）
不成立 → **首期發票不建立** → 換方案首期扣 0。

> 為什麼第三方的 auto-invoice cron 兜不住這個洞（#547 靜態複查，2026-08）：
> `sh_subscription/data/subscription_cron.xml:5-14` 的 `ir.cron`（每日一次、`model._sh_create_auto_invoice()`）
> **沒有宣告 `active` 欄位** ⇒ 取 `ir.cron.active` 的預設 `True`（`odoo/addons/base/models/ir_cron.py:73`），
> 也就是說它是開著的。但它的實作有 bug：`_sh_create_auto_invoice`（`sh_subscription_subscription.py:373-406`）
> 標了 `@api.model`，迴圈變數是 `subscription`，第 `:377` 行卻寫
> `invoice_vals = self._generate_invoice_vals()` —— **`self` 是 model-level 空 recordset**。
> Odoo 對空 recordset 讀欄位回 null 值而不拋錯（`odoo/fields.py:1224-1227`；`.id` 回 `False`，`odoo/fields.py:5335-5336`），
> 所以那組 vals 是 `sh_subscription_id=False` / `partner_id=False` / `journal_id=False` 的空殼
> （`journal_id` 在 `account.move` 是 `required=True`，所以實際上要嘛 create 直接拋錯、要嘛生出一張
> 與該訂閱毫無關聯的發票——**兩個方向都不會為那筆訂閱補開首期發票**），而 `:381-391` 仍會把
> `sh_date_of_next_payment` 往後推一個週期。
> ⇒ **「首期扣 0」成立，不要指望 cron 兜底。** 至於「無限續」要精確講：每次換方案送一個 trial 週期
> （seed plan 多為 7 天），租戶只要在每個窗口內再換一次方案就能持續規避。
>
> 前提（會過期，用之前先複查）：以上是**靜態讀碼**，讀的是本機 checkout 的 `sh_subscription`，
> **未驗證它與 prod/stg 實際安裝的版本同一版**，也未在任何環境實跑該 cron。

守線目前**只有** `src/tests/test_trial_reset_guard_367.py`（4 條），**沒有** repo-lint / pattern-guard 機械攔——
`scripts/audit/repo-lint.py` 的 check function 無一提到 trial，`.claude/hooks/pattern-guard-map.json` 的
`subscription-billing` class pattern（`^src/models/sh_subscription[a-z_]*\.py$`）**不匹配**
`src/models/subscription_lifecycle_mixin.py`，也就是編輯本 doc 唯一描述的那支檔案時 hook 不會端出本 doc。
動這段之前務必先跑那 4 條測試。

（前端殘留，屬另一張單）`PlanSelector.js` 的 `formatTrial()` 只讀 plan 級的 `has_trial`，
換方案畫面（`ChangePlanModal` 內嵌 PlanSelector）對「已持有訂閱的 service」仍會標示
「7-day free trial」，但後端 `:226` 一定走 active 分支。這是**使用者可見的假承諾**，不是計費繞過
——錢那一側已守住。修法（payload 回傳 entity-aware 的 `trial_available`，或由 modal 傳 `hasPriorSub`
抑制徽章）不在本 doc 範圍。

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
   constraint 強制 — 這是 helper 與後續 confirm hook 耦合（helper-confirm coupling）的 latent risk：helper body 算出的 pre-confirm date 與 callsite 後 confirm hook 觀察到的 post-confirm date 不一致，名稱無法強制此約束。

3. **若要 unit test helper 本身（不透過 `_create_and_confirm_subscription`），
   斷言要對 pre-confirm state**，不能用 plan acceptance criterion 那條值。但
   convention 是用 integration test 走完整 path（含 confirm），所以 unit test
   helper 直接呼叫是反 pattern。

## Cycle_delta lookup 共用 — 已知重複，可接受

兩個 helper 各有一個 5 行 `cycle_delta` lookup（`subscription_lifecycle_mixin.py:257-262`
trial 與 `:300-305` active），字面重複 9 LoC。Plan C4 列為 optional refactor、未做。
若要抽 helper，建議同時加：

- `UserError` for unknown `sh_unit`（取代 silent `relativedelta()` fallback）
- `sh_duration >= 1` constraint on `sh.subscription.plan` model
- 共用 `_CYCLE_UNITS` module 常數或 `_plan_cycle_delta(plan)` helper

詳見 issue #39 reviewer report Recommendation 段 follow-up #1。**單純 rename 不值得，
加 guard 一起做才有 ROI。**

## See also

- 程式碼：`src/models/subscription_lifecycle_mixin.py:140-321`（`_create_and_confirm_subscription` 到 active helper 尾端）
- Plan docs：
  - `docs/plans/2026-05-17-sh-end-date-fresh-non-trial.md`（PR #42）
  - PR #38 plan（trial path 對應 plan，記憶位置 `docs/plans/2026-05-*-trial-dates-from-plan*.md`）
- Test report：`docs/archived/tracking/issue-39-test-coverage.md`
- Trial guard 守線：`src/tests/test_trial_reset_guard_367.py`（4 條：首購有 trial／換方案無 trial 且首期發票 > 0／換席數變體無 trial／同 partner 第 2 個 service 有自己的 trial）
- 第三方 per-partner trial guard（被 helper 蓋掉的那個）：`sh_subscription/models/sh_subscription_subscription.py:194-201`、`:208-217`（`sub_exists`）
- 第三方公式：`sh_subscription/models/sh_subscription_subscription.py`（onchange 公式 line 180-186；confirm advancement line 274-304）
- 相關 invariant：refund clamp `src/models/sh_subscription_fix.py`（`_compute_unused_refund` 的 `max(0, end-today)` + `today<start→0` guard 守住財務面，UI 下游讀者沒有對等 clamp — 這是 helper 必須存在的原因）
