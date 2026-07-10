---
title: "#327 退點 prorata 視窗改以 sh_date_of_next_payment 推導"
category: plans
status: draft
created: 2026-07-10
issue: 327
refs: [318, 324, 326]
---

# #327 退點視窗改 next_payment 推導 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓「當期已付視窗」跟隨遞迴計費實際推進的 `sh_date_of_next_payment`，使第 2 個計費週期起、已付整期的長壽 `in_progress` 訂閱中途取消／刪服務／換 plan 時，能正確退未使用比例，而非因凍結的 `sh_end_date` 恆退 0。

**Architecture:** 單點修正 SSOT 純公式 `sh.subscription.subscription._prorata_unused_refund`（`src/models/sh_subscription_fix.py`）：把「當期視窗」從建立時一次寫定、之後永不推進的 `[sh_start_date, sh_end_date]` 改為由 `sh_date_of_next_payment` 反推的 `[next_payment − cycle_delta, next_payment)`。所有退點呼叫者（3 實體刪除 `_settle_on_entity_delete`、4 換 plan 入口 `_compute_unused_refund`）經同一 SSOT 自動受惠。**刻意不碰 `sh_end_date`**，故對上游 `_sh_auto_expired_subscription` 零交互影響。

**Tech Stack:** Odoo 18 addon（Python 4-space）、`dateutil.relativedelta`、`odoo.tests.common.TransactionCase`；本地測試走 docker-desktop paas-odoo image bind-mount recipe（見 memory `reference_odoo_test_docker_desktop_paas_image`）。

## Global Constraints

- 版號：`src/__manifest__.py` `18.0.1.0.51` → `18.0.1.0.52`（PR 前 rebase 檢查——`feat/330-pricing-seed-redesign` 並行可能撞 manifest）。
- 無 migration、無 ACL、無新欄位、無 seed 改動（純公式行為修正）。
- Python 4-space 縮排；程式碼註解用繁體中文；commit prefix 英文小寫、繁中訊息、不含 AI 字樣。
- 守則 d（不可違反）：單次退點恆 ∈ `[0, 一個週期 plan 價]`，禁止跨期回溯多退。
- trial→0、never-paid→0（#324 `_has_paid_current_cycle` gate）語意**不變**。
- 修法**不得**造成不死訂閱、也**不得**提早 expire（不碰 `sh_end_date` 即滿足）。

---

## Design Rationale（對照 issue #327 設計題 a–e）

### (b) 關鍵交互現況——用 code/line 釘死「長壽 in_progress 從何而來」

上游 `sh_subscription`（image `custom-addons/sh_subscription`，本 repo 未 vendored）：

- `_sh_auto_expired_subscription`（`models/sh_subscription_subscription.py:409-413`）：
  ```python
  for subscription in ...search([('state', '=', 'in_progress')]):
      if subscription.sh_end_date and subscription.sh_end_date == fields.Date.today():
          subscription.state = 'renewed' if <有子約> else 'expire'
  ```
  由 daily cron `sh_ir_cron_subscription_auto_expired`（`data/subscription_cron.xml:17-26`）呼叫。
- 本 repo **不 override 此方法、不停用此 cron**（`grep -rn _sh_auto_expired_subscription src/` 空、`grep -rn sh_ir_cron_subscription_auto_expired src/` 空）→ 依上游預設 active。

**謎題解答**：此 cron 用**脆弱的 `==` 等值比對**。只有「cron 恰在 `sh_end_date` 當天執行且該訂閱當時為 in_progress」才命中 expire。一旦錯過當天（cron 停用期、環境剛部署、當天未觸發、訂閱在 end_date 之後才 confirm），隔天起 `today > sh_end_date`，`==` 永遠為 False → 訂閱成為**長壽 `in_progress`**，由平台 fix 層 `_sh_create_auto_invoice` 以 `next_payment <= today` 續期扣款、`_on_payment_success` 只推進 `sh_date_of_next_payment`（`sh_subscription_fix.py:308-329`）續命。這正是「cycle-2+ 已付訂閱」與退 0 bug 的命中族群。Task 1 以 characterization test 坐實此 `==` miss 機制。

**平台 sh_end_date 是單週期且凍結**：唯二生產寫入點 `subscription_lifecycle_mixin.py:214`（trial 建立）、`:245`（active 建立），皆 `end_date = start + cycle_delta − 1day`；續期路徑（`_on_payment_success` / 上游 `_sh_create_auto_invoice` / `_skip_trial`）**零寫入** `sh_end_date`（`grep` 佐證）。

### (a) 視窗推導二選一——擇 next_payment 推導（不碰 sh_end_date）

| 方案 | 做法 | auto-expire 交互 | 判定 |
| --- | --- | --- | --- |
| **a-1（採用）** | 視窗 = `[next_payment − cycle_delta, next_payment)` | `sh_end_date` 不變 → `_sh_auto_expired_subscription` 的 `==` 輸入不變 → **行為零改變**（既不提早 expire、也不製造不死訂閱） | ✅ |
| a-2（否決） | 每次成功續期推進 `sh_end_date` | `sh_end_date` 每期前推 → `==` 在每個週期末更易命中 → 可能提早 expire 長壽訂閱；若要避免又須連帶停用 cron → scope 爆炸、風險高 | ❌ |

決定性理由：a-1 對「上游 auto-expire cron 在特定環境是否 active」這個環境變數**完全免疫**——無論它 active 與否、命中與否，退點正確性都只取決於 `next_payment`（billing 唯一推進的欄位）。

### next_payment 對齊「當前已付週期」的證明

confirm / skip-trial / 每次續期成功後，`next_payment` 被推進為 `<週期起點> + cycle_delta`（`_on_payment_success:319`、`_skip_trial:457`）。故任一時刻已付訂閱的 `next_payment` = 當前已付週期的**結束邊界（exclusive）**：

- cycle-1（confirm 後）：`next_payment = start + cycle` → 視窗 `[start, start+cycle)` = cycle-1 ✓
- cycle-2（續期後）：`next_payment = start + 2·cycle` → 視窗 `[start+cycle, start+2·cycle)` = cycle-2 ✓

`sh_start_date` 僅保留作 **trial guard**（`today < sh_start_date → 0`）；視窗起點改用動態 `cycle_start = next_payment − cycle_delta`。

### (c) 邊界語意不變

- `sh_no_of_billing_cycle = -1`（平台所有 plan，見 mixin:228 註）：無限續期，公式只看 `next_payment`，天然一致。
- 有限 cycle 最後一期：`next_payment` 仍指向該期結束邊界，公式一致；期滿後 state 轉 expire/close → `_settle_on_entity_delete` 對非 `in_progress` no-op。
- trial→0：保留 `today < sh_start_date → 0` guard。
- never-paid→0：`_compute_unused_refund` 先過 `_has_paid_current_cycle` gate（不動）。

### (d) 守則——單次退點 ∈ [0, price]，不跨期

`remaining_days = max(0, min((next_payment − today).days, cycle_days))` 使比例 ∈ `[0, 1]` → 退點 ∈ `[0, sh_plan_price]`。`min(..., cycle_days)` 明確封住「today 落在更早週期」的異常回溯多退。

### (e) 既有把 bug 固化的測試——刻意翻紅→翻綠

`src/tests/test_change_plan_refund.py` 直接測純公式 `_prorata_unused_refund`，其 `_make_sub` **不含 `sh_date_of_next_payment`** 且斷言建立在 `[start, end]` 假設上。其中 `test_refund_zero_after_end_date`（`today > sh_end_date → 0`）**固化了本 bug**：長壽已付訂閱退 0。Task 2 將其按新語意重寫為「長壽 in_progress 退比例」（先紅後綠），並為 `_make_sub` 補 `next_payment`。

### Scope 邊界（已評估、刻意不動）

- 三個換 plan controller 的 `if not sh_end_date or not sh_start_date` 完整性守衛（`paas.py:988`、`smart_home.py:173`、`security_access.py:335`）：僅防壞資料 crash，非退點計算。長壽 `in_progress` 訂閱 `sh_end_date` 凍結但**非 null** → 守衛通過、不阻礙 fix。**保留不動**。
- `_has_paid_current_cycle` gate 邏輯（#324）：不動，只更新其 docstring 的限制註記（Task 3）。
- mixin `_force_*_dates_from_plan` / `_skip_trial` 的 `cycle_delta` 計算：來源為 `plan.sh_duration`（建立期 sub 未定型），與退點的 `sub.sh_recurrency` 來源不同，不強制統一（避免擴大 scope）。

---

## File Structure

- **Modify** `src/models/sh_subscription_fix.py`
  - 新增 `_cycle_delta(self)` helper（`sh.subscription.subscription`）。
  - 改寫 `_prorata_unused_refund`（視窗改 next_payment 推導）。
  - `_on_payment_success` 複用 `_cycle_delta`（同檔 DRY，消除 unit→delta 對照漂移）。
  - 更新 `_has_paid_current_cycle` docstring 限制註記。
- **Modify** `src/tests/test_change_plan_refund.py`
  - `_make_sub` 加 `next_payment` 並改 day-based cycle（斷言穩定）；重寫固化-bug 測試、調整 guards。
- **Create** `src/tests/test_refund_window_next_payment.py`
  - #327 專屬：auto-expire 交互護欄、長壽 in_progress 退比例、守則 d、trial/無限 cycle 邊界、month-based relativedelta 範圍斷言。
- **Modify** `src/__manifest__.py`（`.51` → `.52`）。
- **Modify** `docs/tracking/followups/follow-ups.md`（F-BILLING-1 標記已修 + 連結 plan/PR）。

> **測試斷言穩定性**：新公式以 `relativedelta` 反推 `cycle_start`，month cycle 的天數隨月份浮動（28–31）。故所有**精確**斷言測試用 **day-based cycle**（`sh_unit='day'`, `sh_recurrency=<固定天數>`）使 `cycle_days` 固定；另用一個 month-based 測試以**範圍斷言**（`0 < refund <= price`）覆蓋 relativedelta 月路徑不炸。

> **本地測試 recipe**（memory `reference_odoo_test_docker_desktop_paas_image`）：paas-odoo image bind-mount worktree `src`；單 class 循環 `docker exec odoo /opt/bitnami/odoo/bin/odoo -d test -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:<Class> --stop-after-init --no-http --addons-path=/opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/custom-addons --data-dir=/tmp/odoo-data --db_host pg --db_user odoo --db_password odoo --log-handler=odoo.tests.result:INFO`；抓 `"N failed, M error(s) of K tests"`。full suite 已知 2 個 `TestNamespaceLeftSetUnion` error＝缺 `AGENT_RUNTIME_NAMESPACE_SECRET`（環境非回歸；`docker exec -e AGENT_RUNTIME_NAMESPACE_SECRET=x` 可歸零）。

---

## Task 1: auto-expire 交互護欄（characterization——釘死 (b) 現況）

坐實「長壽 in_progress 成因＝上游 `==` miss」，並作為 fix 不破壞 auto-expire 交互的護欄。此測試 fix 前後**皆綠**（characterization，非 red→green）。

**Files:**
- Create: `src/tests/test_refund_window_next_payment.py`

**Interfaces:**
- Consumes: `sh.subscription.subscription._sh_auto_expired_subscription()`（上游 `@api.model`）。
- Produces: 測試檔骨架（Task 2 續用同檔）。

- [ ] **Step 1: 寫 characterization 測試**

```python
"""#327: 退點視窗改以 sh_date_of_next_payment 推導。

涵蓋：
1. auto-expire 交互護欄（釘死長壽 in_progress 成因＝上游 == miss；fix 不得破壞）
2. 長壽 in_progress 退比例（#327 核心，Task 2 red→green）
3. 守則 d（單次退點 ∈ [0, price]）、trial / 無限 cycle 邊界
"""
from datetime import date, timedelta

from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestAutoExpireInteraction(TransactionCase):
    """釘死上游 _sh_auto_expired_subscription 的 == 等值語意。

    這是長壽 in_progress 訂閱能存在（並命中退 0 bug）的 code-level 成因，
    也是本 fix「不碰 sh_end_date → auto-expire 行為零改變」的護欄。

    上游 method 在命中時會 _sh_send_subscription_email，比照上游
    test_sub010_cron_auto_expire 以 patch mock 掉 email 副作用；cron 在
    另一 recordset 改 state，呼叫後需 invalidate_recordset 重讀。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription'].sudo()
        cls.partner = cls.env['res.partner'].sudo().create({'name': 'AExp P'})

    def _in_progress_sub(self, end_offset_days):
        today = date.today()
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': 100.0, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=30),
            'sh_end_date': today + timedelta(days=end_offset_days),
            'sh_date_of_next_payment': today + timedelta(days=15),
            'state': 'in_progress',
        })

    def test_auto_expire_hits_on_exact_end_date(self):
        """sh_end_date == today → in_progress 訂閱被 expire（無子約）。"""
        sub = self._in_progress_sub(end_offset_days=0)
        with patch.object(type(sub), '_sh_send_subscription_email',
                          return_value=None):
            self.Sub._sh_auto_expired_subscription()
        sub.invalidate_recordset()
        self.assertEqual(sub.state, 'expire')

    def test_auto_expire_misses_when_past_end_date(self):
        """sh_end_date < today → == 比對 miss → 訂閱維持 in_progress。

        這正是長壽 in_progress 的成因：一旦錯過 end_date 當天，之後
        today > end_date，== 永遠 False，訂閱靠 next_payment 續期長存。
        """
        sub = self._in_progress_sub(end_offset_days=-5)
        with patch.object(type(sub), '_sh_send_subscription_email',
                          return_value=None):
            self.Sub._sh_auto_expired_subscription()
        sub.invalidate_recordset()
        self.assertEqual(sub.state, 'in_progress')
```

- [ ] **Step 2: 跑，確認綠（characterization 證明現況）**

Run（Class 名 `TestAutoExpireInteraction`）：
```
docker exec odoo /opt/bitnami/odoo/bin/odoo -d test -u woow_paas_platform \
  --test-enable --test-tags /woow_paas_platform:TestAutoExpireInteraction \
  --stop-after-init --no-http \
  --addons-path=/opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/custom-addons \
  --data-dir=/tmp/odoo-data --db_host pg --db_user odoo --db_password odoo \
  --log-handler=odoo.tests.result:INFO 2>&1 | grep -E "failed|passed|Ran"
```
Expected: `0 failed, 0 error(s) of 2 tests`（現況即綠——證明 auto-expire `==` 語意與長壽成因）。

- [ ] **Step 3: Commit**

```bash
git add src/tests/test_refund_window_next_payment.py
git commit -m "test: 坐實上游 auto-expire == miss 為長壽 in_progress 成因（#327 護欄）"
```

---

## Task 2: `_prorata_unused_refund` 視窗改 next_payment 推導（核心 red→green，(a)(c)(d)(e)）

一次完成：新增 #327 red 測試 → 確認紅 → 改公式（`_cycle_delta` + `_prorata_unused_refund` + `_on_payment_success` 複用）→ 連帶按新語意重寫既有 `test_change_plan_refund.py` → full suite 綠。改公式與連帶測試調整必須原子（同一 SSOT 語意變更），故同屬一個 review gate。

**Files:**
- Modify: `src/models/sh_subscription_fix.py`（`_cycle_delta` 新增、`_prorata_unused_refund` 改寫、`_on_payment_success` 複用）
- Modify: `src/tests/test_refund_window_next_payment.py`（加 #327 退點測試）
- Modify: `src/tests/test_change_plan_refund.py`（`_make_sub` + 重寫固化測試 + guards）

**Interfaces:**
- Produces:
  - `_cycle_delta(self) -> relativedelta | None`：一個計費週期長度，由 `sh_unit` + `sh_recurrency` 決定；非 day/week/month/year 回 `None`。
  - `_prorata_unused_refund(self) -> float`：視窗 `[next_payment − _cycle_delta(), next_payment)` 的剩餘比例退點，∈ `[0, sh_plan_price]`。
- Consumes: `sh_date_of_next_payment`、`sh_start_date`、`sh_unit`、`sh_recurrency`、`sh_plan_price`。

- [ ] **Step 1: 加 #327 退點 red 測試（append 到 test_refund_window_next_payment.py）**

```python
class TestRefundWindowFromNextPayment(TransactionCase):
    """#327: 退點視窗跟隨 sh_date_of_next_payment（day-based cycle 使斷言穩定）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription'].sudo()
        cls.partner = cls.env['res.partner'].sudo().create({'name': 'RWin P'})

    def _sub(self, start_off, next_pay_off, end_off=None, price=100.0,
             cycle_days=30):
        """day-based cycle（sh_recurrency=cycle_days）→ cycle_days 固定、斷言穩定。

        end_off 預設 None（新公式不看 sh_end_date）；核心回歸測試明確傳
        過期 end_off 以證明「凍結 end_date 不再導致退 0」。
        """
        today = date.today()
        vals = {
            'sh_partner_id': self.partner.id,
            'sh_plan_price': price, 'sh_unit': 'day', 'sh_recurrency': cycle_days,
            'sh_start_date': today + timedelta(days=start_off),
            'sh_date_of_next_payment': today + timedelta(days=next_pay_off),
        }
        if end_off is not None:
            vals['sh_end_date'] = today + timedelta(days=end_off)
        return self.Sub.new(vals)

    # --- #327 核心：長壽 in_progress 活過凍結 end_date 仍退比例 --------------- #

    def test_long_lived_past_frozen_end_date_refunds_prorata(self):
        """凍結 sh_end_date 早已過，但 next_payment 在未來（已續期）→ 退剩餘比例。

        舊公式看 sh_end_date（過期）→ max(0, end−today)=0 → 退 0（bug）。
        新公式看 [next_payment−cycle, next_payment) → 退比例。
        """
        # cycle_days=30, next_payment=+15 → cycle_start=-15, remaining=15 → 50
        sub = self._sub(start_off=-90, next_pay_off=15, end_off=-60,
                        price=100.0, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    def test_refund_full_at_cycle_start(self):
        """today == cycle_start（剛續期）→ 全額退。"""
        # next_payment=+30, cycle=30 → cycle_start=today, remaining=30 → 100
        sub = self._sub(start_off=-30, next_pay_off=30, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 100.0)

    def test_refund_zero_at_next_payment_boundary(self):
        """today == next_payment（週期用盡、下期扣款日到）→ remaining 0 → 退 0。"""
        sub = self._sub(start_off=-30, next_pay_off=0, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_clamped_when_today_past_next_payment(self):
        """today > next_payment（cron 落後未推進）→ clamp 0，不為負。"""
        sub = self._sub(start_off=-40, next_pay_off=-3, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    # --- 守則 d：單次退點 ∈ [0, price]，不跨期回溯多退 ---------------------- #

    def test_refund_never_exceeds_one_cycle_price(self):
        """異常：today 落在更早週期（next_payment 遠在未來）→ 仍 clamp 到 price。"""
        # next_payment=+100 但 cycle 只有 30 → 不得退超過一週期價
        sub = self._sub(start_off=-5, next_pay_off=100, price=100.0, cycle_days=30)
        self.assertLessEqual(sub._prorata_unused_refund(), 100.0)

    # --- 邊界：trial guard / 缺欄位 / 無法解析 cycle ------------------------ #

    def test_refund_zero_during_trial(self):
        """today < sh_start_date（trial / 未開始）→ 0。"""
        sub = self._sub(start_off=7, next_pay_off=7, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_next_payment_missing(self):
        """無 sh_date_of_next_payment → 0（新視窗來源缺失）。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 100.0,
            'sh_unit': 'day', 'sh_recurrency': 30,
            'sh_start_date': today - timedelta(days=5),
            'sh_date_of_next_payment': False,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_cycle_unresolvable(self):
        """sh_unit 非 day/week/month/year → _cycle_delta None → 0。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 100.0,
            'sh_unit': False, 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=5),
            'sh_date_of_next_payment': today + timedelta(days=15),
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_recurrency_zero(self):
        """sh_recurrency=0（壞資料）→ _cycle_delta None → 0（否則 cycle_days
        會塌成 0、退全額；plan-review advisory）。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 100.0,
            'sh_unit': 'day', 'sh_recurrency': 0,
            'sh_start_date': today - timedelta(days=5),
            'sh_date_of_next_payment': today + timedelta(days=15),
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    # --- month-based relativedelta 路徑（範圍斷言，不依賴執行日期天數）------- #

    def test_month_cycle_prorata_is_within_bounds(self):
        """月繳（relativedelta 月路徑）中途 → 0 < refund <= price（不炸、不越界）。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 300.0,
            'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=120),
            'sh_end_date': today - timedelta(days=90),          # 凍結 end 已過
            'sh_date_of_next_payment': today + timedelta(days=10),
        })
        refund = sub._prorata_unused_refund()
        self.assertGreater(refund, 0.0)
        self.assertLessEqual(refund, 300.0)
```

- [ ] **Step 2: 跑，確認核心測試紅**

Run: `--test-tags /woow_paas_platform:TestRefundWindowFromNextPayment`（同 Step 2 of Task 1 的指令模板換 Class 名）。
Expected: FAIL——`test_long_lived_past_frozen_end_date_refunds_prorata`（舊公式回 0 ≠ 50）、`test_refund_full_at_cycle_start`（舊公式看 [start,end]）、`test_month_cycle_prorata_is_within_bounds`（舊公式回 0，`assertGreater(0,0)` 失敗）等。確認是「舊公式看凍結 sh_end_date」導致的紅。

- [ ] **Step 3: 實作 `_cycle_delta` helper**

在 `sh_subscription_fix.py`，`_has_paid_current_cycle` 之前插入：

```python
    def _cycle_delta(self):
        """一個計費週期的 relativedelta（由 sh_unit + sh_recurrency 決定）。

        退點視窗（``_prorata_unused_refund``）與續期推進（``_on_payment_success``）
        共用，避免 unit→delta 對照表在多處漂移。回 ``None``（呼叫端據此保守退
        0 / 不推進）於：sh_recurrency 為 0/未設（否則 cycle_delta 為零長度、
        cycle_days=0，語意無效且會退全額）、或 sh_unit 非 day/week/month/year。
        """
        self.ensure_one()
        if not self.sh_recurrency:
            return None
        return {
            'day': relativedelta(days=self.sh_recurrency),
            'week': relativedelta(weeks=self.sh_recurrency),
            'month': relativedelta(months=self.sh_recurrency),
            'year': relativedelta(years=self.sh_recurrency),
        }.get(self.sh_unit)
```

- [ ] **Step 4: 改寫 `_prorata_unused_refund`（視窗改 next_payment 推導）**

整段取代 `sh_subscription_fix.py:67-83`：

```python
    def _prorata_unused_refund(self):
        """純比例退點公式：today 落在「當前已付週期」的剩餘比例（不驗付款）。

        當前已付週期以 ``sh_date_of_next_payment`` 反推（#327）：遞迴計費只推進
        next_payment、永不推進凍結的 ``sh_end_date``，故視窗必須跟隨 next_payment
        才能正確涵蓋 cycle-2+ 的長壽 in_progress 訂閱（否則過期後 clamp 恆退 0）。

        視窗 = ``[next_payment − cycle_delta, next_payment)``；剩餘比例
        = ``(next_payment − today) / cycle_days``，clamp 到 ``[0, cycle_days]`` 使
        單次退點 ∈ ``[0, 一週期 plan 價]``（守則 d：不跨期回溯多退）。

        Returns 0 when next_payment / start_date 缺失、``today < sh_start_date``
        （trial / 未開始）、或 cycle_delta 無法解析。``sh_start_date`` 僅作 trial
        guard；視窗起點用動態 ``cycle_start``，不再依賴凍結的 ``sh_end_date``，
        故對上游 ``_sh_auto_expired_subscription``（sh_end_date==today→expire）
        零交互影響。
        """
        self.ensure_one()
        next_payment = self.sh_date_of_next_payment
        if not next_payment or not self.sh_start_date:
            return 0
        today = fields.Date.today()
        if today < self.sh_start_date:
            return 0
        cycle_delta = self._cycle_delta()
        if not cycle_delta:
            return 0
        cycle_start = next_payment - cycle_delta
        cycle_days = (next_payment - cycle_start).days or 1
        remaining_days = max(0, min((next_payment - today).days, cycle_days))
        return round(self.sh_plan_price * remaining_days / cycle_days, 2)
```

- [ ] **Step 5: `_on_payment_success` 複用 `_cycle_delta`（同檔 DRY）**

`sh_subscription_fix.py:310-316` 的 `unit_deltas` dict + `.get(subscription.sh_unit)` 取代為：

```python
        delta = subscription._cycle_delta()
```
（保留其後 `vals = {}` / `if delta:` 邏輯不變。）

- [ ] **Step 6: 跑 #327 測試 + Task 1 護欄，確認綠**

Run: `--test-tags /woow_paas_platform:TestRefundWindowFromNextPayment,TestAutoExpireInteraction`
Expected: `0 failed, 0 error(s) of 13 tests`（11 退點 + 2 護欄；護欄仍綠＝fix 不破壞 auto-expire）。

- [ ] **Step 7: 按新語意重寫 `test_change_plan_refund.py`**

`_make_sub`（`:30-44`）整段取代（加 next_payment、改 day-based 使斷言穩定）：

```python
    def _make_sub(self, start_offset_days, next_payment_offset_days,
                  price=100.0, cycle_days=30):
        """Build a sub record without going through confirm/onchange.

        #327：視窗改以 sh_date_of_next_payment 推導，故顯式提供 next_payment；
        day-based cycle（sh_recurrency=cycle_days）使 cycle_days 固定、斷言穩定。
        """
        today = date.today()
        return self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today + timedelta(days=start_offset_days),
            'sh_date_of_next_payment': today + timedelta(days=next_payment_offset_days),
            'sh_plan_price': price,
            'sh_unit': 'day', 'sh_recurrency': cycle_days,
        })
```

替換受影響的測試（`:48-143`）為新語意（保留測試意圖、更新呼叫簽章與斷言）：

```python
    # --- BUG-5 regressions（trial guard 保留）---------------------------- #

    def test_refund_zero_during_trial_period(self):
        """today < sh_start_date — customer has not paid → refund = 0."""
        sub = self._make_sub(start_offset_days=7, next_payment_offset_days=7)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_full_when_today_at_cycle_start(self):
        """today == cycle_start（next_payment − cycle）→ 全額（尚未消耗）。"""
        sub = self._make_sub(start_offset_days=0, next_payment_offset_days=30,
                             cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 100.0)

    # --- happy path ----------------------------------------------------- #

    def test_refund_prorated_mid_paid_cycle(self):
        """Half-way through a paid cycle → ~half refund."""
        # cycle_start=-15, remaining=15, cycle=30 → 50
        sub = self._make_sub(start_offset_days=-15, next_payment_offset_days=15,
                             cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    def test_refund_zero_at_next_payment(self):
        """today == next_payment（週期用盡）→ refund = 0（原 end-of-cycle 語意）。"""
        sub = self._make_sub(start_offset_days=-30, next_payment_offset_days=0,
                             cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_prorata_past_frozen_end_date(self):
        """#327 翻紅→翻綠：長壽 in_progress 活過凍結 sh_end_date（過期），但
        next_payment 在未來 → 退剩餘比例（不再恆退 0）。原 test_refund_zero_
        after_end_date 固化了「過期即退 0」的 bug，此處按新語意重寫。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today - timedelta(days=90),
            'sh_end_date': today - timedelta(days=60),          # 凍結 end 已過
            'sh_date_of_next_payment': today + timedelta(days=15),
            'sh_plan_price': 100.0, 'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    # --- guards --------------------------------------------------------- #

    def test_refund_zero_when_next_payment_missing(self):
        """無 sh_date_of_next_payment（新視窗來源缺失）→ refund = 0。"""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': date.today() - timedelta(days=5),
            'sh_date_of_next_payment': False,
            'sh_plan_price': 100.0, 'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_start_date_missing(self):
        """Missing sh_start_date（trial guard 需要）→ refund = 0。"""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': False,
            'sh_date_of_next_payment': date.today() + timedelta(days=10),
            'sh_plan_price': 100.0, 'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_cycle_unresolvable(self):
        """sh_unit 非法 → _cycle_delta None → refund = 0（div-by-zero 護欄）。"""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': date.today() - timedelta(days=5),
            'sh_date_of_next_payment': date.today() + timedelta(days=10),
            'sh_plan_price': 100.0, 'sh_unit': False, 'sh_recurrency': 1,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)
```

`test_wizard_refund_uses_helper_during_trial`（`:147-198`）的 in-line sub（`:172-180`）保持 trial 日期（`sh_start_date = today+7`）即可（trial guard 命中→0），但補 `sh_date_of_next_payment`：在該 `create` vals 內 `sh_start_date` 之後加一行 `'sh_date_of_next_payment': today + timedelta(days=7),`。`TestChangePlanRefundIsRefundTyped`（`:201+`，走 `_create_and_confirm_subscription` 真實路徑，next_payment 由 confirm seed）**不需改**——當天換 next_payment=today+cycle→全額退 300 語意不變。

- [ ] **Step 8: 跑受影響測試檔全綠**

Run: `--test-tags /woow_paas_platform:TestChangePlanRefund,TestChangePlanRefundIsRefundTyped,TestRefundWindowFromNextPayment,TestAutoExpireInteraction`
Expected: `0 failed, 0 error(s)`。

- [ ] **Step 9: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_refund_window_next_payment.py src/tests/test_change_plan_refund.py
git commit -m "fix: 退點 prorata 視窗改以 sh_date_of_next_payment 推導（#327）"
```

---

## Task 3: docstring 修正 + 退點呼叫者回歸驗證

更新 `_has_paid_current_cycle` docstring 已過時的限制註記；跑三退點呼叫者的既有測試確認 SSOT 改動未回歸。

**Files:**
- Modify: `src/models/sh_subscription_fix.py`（`_has_paid_current_cycle` docstring `:35-41`）

- [ ] **Step 1: 更新 docstring 限制註記**

`sh_subscription_fix.py:35-41` 的「範圍限制（沿用 #318…待 follow-up…修正）」整段取代為：

```python
        範圍限制（沿用 #318）：本方法查「本訂閱歷來任一成功扣款」、不做週期
        範圍限定——它只回答「有沒有真付過」，退點**金額**由 ``_prorata_unused_
        refund`` 決定。退點視窗自 #327 起改以 ``sh_date_of_next_payment`` 反推
        當期區間 ``[next_payment − cycle_delta, next_payment)``，已正確涵蓋
        「第 2 個週期起、凍結 sh_end_date 已過」的長壽 in_progress 訂閱（不再
        恆退 0）。本 gate 與視窗計算正交，維持不變。
```

- [ ] **Step 2: 跑三退點呼叫者的既有測試回歸**

Run: `--test-tags /woow_paas_platform:TestRefundRequiresPayment,TestSettleOnEntityDelete,TestCloudServiceUnlinkSettlesSubscription,TestSmartHomeUnlinkSettlesSubscription,TestSecurityAccessUnlinkSettlesSubscription`
Expected: `0 failed, 0 error(s)`。（這些 fixture 的 next_payment 皆對齊已付週期——`_mid_cycle_sub` next_payment=+15 對齊 [start,end]；`_create_and_confirm_subscription` 當天 confirm next_payment=today+cycle→全額——故新公式一致、不回歸。）

- [ ] **Step 3: Commit**

```bash
git add src/models/sh_subscription_fix.py
git commit -m "docs: 更新 _has_paid_current_cycle docstring 反映 #327 視窗修正"
```

---

## Task 4: 版號 bump + follow-ups 收尾 + full suite 最終驗證

**Files:**
- Modify: `src/__manifest__.py`
- Modify: `docs/tracking/followups/follow-ups.md`

- [ ] **Step 1: bump manifest**

`src/__manifest__.py:3` `"version": "18.0.1.0.51"` → `"version": "18.0.1.0.52"`。

- [ ] **Step 2: follow-ups.md 標記 F-BILLING-1 已修**

在 `docs/tracking/followups/follow-ups.md` 的 F-BILLING-1 條目「對處理」bullet 後補一行：

```markdown
- **已修**：PR #<PR 號>（`fix/327-refund-window`，Closes #327）— `_prorata_unused_refund` 視窗改以 `sh_date_of_next_payment` 反推 `[next_payment − cycle_delta, next_payment)`，不碰 `sh_end_date`（auto-expire 交互零影響）。plan：`docs/superpowers/plans/2026-07-10-327-refund-window-next-payment.md`。
```

- [ ] **Step 3: full suite 最終驗證**

Run（無 test-tags＝全模組；帶 `-e AGENT_RUNTIME_NAMESPACE_SECRET=x` 消除已知 ns flake）：
```
docker exec -e AGENT_RUNTIME_NAMESPACE_SECRET=x odoo /opt/bitnami/odoo/bin/odoo \
  -d test -u woow_paas_platform --test-enable --test-tags /woow_paas_platform \
  --stop-after-init --no-http \
  --addons-path=/opt/bitnami/odoo/lib/odoo/addons,/opt/bitnami/odoo/custom-addons \
  --data-dir=/tmp/odoo-data --db_host pg --db_user odoo --db_password odoo \
  --log-handler=odoo.tests.result:INFO 2>&1 | grep -E "failed|error" | tail -20
```
Expected: `0 failed, 0 error(s)`（若未帶 secret，允許唯 2 個 `TestNamespaceLeftSetUnion` error＝環境非回歸）。

- [ ] **Step 4: Commit**

```bash
git add src/__manifest__.py docs/tracking/followups/follow-ups.md
git commit -m "chore: bump 版號 .52 並登錄 #327 修正（F-BILLING-1）"
```

- [ ] **Step 5: rebase 檢查 + 開 PR**

```bash
git fetch origin
git rebase origin/develop        # 若 feat/330 已 merge 撞 manifest，手動取 .52 較大者
```
PR（`tea` 必帶 `--repo odoo-addons/woow_paas_platform`；body 繁中、`Closes #327`、連結 plan）。

---

## Self-Review

- **(a) 視窗二選一**：Design Rationale 表格論證擇 a-1（next_payment 推導），決定性理由＝對 auto-expire cron active 狀態免疫。✅
- **(b) 關鍵交互坐實**：Task 1 characterization 釘死上游 `==` miss＝長壽 in_progress 成因；fix 不碰 sh_end_date → 護欄 fix 後仍綠。✅
- **(c) 邊界**：`test_refund_zero_during_trial`（trial→0）、無限 cycle（公式只看 next_payment）、never-paid 由 gate（Task 3 回歸）。✅
- **(d) 守則**：`test_refund_never_exceeds_one_cycle_price` + `min(..., cycle_days)` clamp。✅
- **(e) 翻紅→翻綠**：Task 2 Step 2 確認紅、Step 7 重寫 `test_refund_prorata_past_frozen_end_date`（原 `test_refund_zero_after_end_date`）。✅
- **三退點呼叫者回歸**：Task 3 Step 2 跑 delete（3 實體）+ requires_payment；換 plan wizard/controller 走同 SSOT。✅
- **Placeholder scan**：無 TBD／TODO；每 code step 附完整程式碼。✅
- **Type consistency**：`_cycle_delta()` 回 `relativedelta|None`、`_prorata_unused_refund()` 回 `float`，Task 2 定義、Task 3/4 引用一致。✅

<!-- peer-reviewed: 2026-07-10T11:01:02Z rounds=1 verdict=approved by=miniclaude -->
