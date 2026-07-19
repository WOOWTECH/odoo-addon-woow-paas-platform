---
title: "#324 訂閱計費雙缺口修復 Implementation Plan"
category: plans
status: done
created: 2026-07-10
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-10-324-billing-dual-gap-fix.md
promoted-at: 2026-07-19
---

# #324 訂閱計費雙缺口修復 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓本 repo fix 層的 `_sh_create_auto_invoice`（dunning-aware）成為唯一訂閱計費路徑，並堵住「從未成功付款」訂閱刪服務/換 plan 退未賺得點的財務套利。

**Architecture:** 上游 `sh_subscription_wallet` 有兩支 buggy cron（daily `_cron_wallet_charge` 扣款不推進 `next_payment`；hourly `_cron_wallet_charge_retry` 每次失敗又建新 `failed` 交易，讓 24h 重試窗永不過期）。本 repo 已用 `sh_subscription_fix.py` override 上游 base 的 `_sh_create_auto_invoice`（正確的 invoice-based dunning）。本 PR 在同一 fix 層 (1) 把上游兩支 cron 方法 override 成 no-op（method-override 比 flip `ir.cron.active` 更耐上游升級），(2) 把退點前提從「日期 proxy」改成「本訂閱有成功扣款（`done` deduct 交易）」，無則退 0（對齊 trial→0 語意）。附帶交付 stg 清理腳本（冪等、condition-based）。

**Tech Stack:** Odoo 18 addon（Python 4-space、Odoo ORM inherit override）、Odoo test framework（`TransactionCase`）、本地 docker-desktop paas-odoo image 跑測試。

## Global Constraints

- **版號**：`src/__manifest__.py` `version` 由 `18.0.1.0.50` bump 到 `18.0.1.0.51`（開 PR 前 rebase 檢查是否有人先 bump）。
- **不改上游 repo**：`sh_subscription` / `sh_subscription_wallet` 一律唯讀參考，所有變更落在本 repo `src/`。
- **無 migration / 無 ACL 變更**：本 PR 不新增欄位、不新增 model，只加 method + 改既有 method 邏輯。
- **MRO 前提**：`woow_paas_platform` 的 `__manifest__.py` `depends` 已含 `sh_subscription_wallet`（在 `sh_subscription` 之後），故本 repo fix 層對 `sh.subscription.subscription` 的 method override 在 MRO 中晚於上游、會勝出。所有 override 落在既有 class `ShSubscriptionFix`（`src/models/sh_subscription_fix.py`，`_inherit = 'sh.subscription.subscription'`）。
- **Python 4-space 縮排、PEP 8；程式碼註解用繁體中文**（對齊本 repo 慣例）。
- **commit message**：zh-tw、prefix 英文小寫（`fix:`/`test:`/`refactor:`/`docs:`/`chore:`）、不得含任何 AI 產生字樣。
- **測試**：走本地 docker-desktop paas-odoo image（recipe 見 `docs`／leader memory `reference_odoo_test_docker_desktop_paas_image`）；full suite 須綠（唯 2 個 `TestNamespaceLeftSetUnion` error＝缺 `AGENT_RUNTIME_NAMESPACE_SECRET` 的既有環境 flake，可忽略）。

---

## 背景事實（實作者必讀，已在本 plan 撰寫時逐一坐實）

三支計費 cron 目前並存：

| Cron（ir.cron XML）| 方法 | 頻率 | 狀態 |
| --- | --- | --- | --- |
| `sh_subscription.sh_ir_cron_subscription_auto_invoice` | `_sh_create_auto_invoice`（**本 repo fix 層已 override**）| daily | 正確路徑（保留）|
| `sh_subscription_wallet.ir_cron_subscription_wallet_charge` | `_cron_wallet_charge` | daily | **缺陷 B：扣完不推進 next_payment、`==` 等值比對**（本 PR neutralize）|
| `sh_subscription_wallet.ir_cron_subscription_wallet_retry` | `_cron_wallet_charge_retry` | hourly | **缺陷 A：失敗又建新 failed → 窗永不過期 → 無限重試**（本 PR neutralize）|

**「本週期已付」如何認定（Task 3 核心，已坐實）：**
- 真扣款路徑＝`account.move._pay_from_wallet()` → `res.partner.deduct_wallet_points()`（`sh_subscription_wallet/models/res_partner.py:204`）建一筆 `wallet.transaction`，`transaction_type='deduct'`、`subscription_id=<sub>`、**`state` 未指定 → 預設 `'done'`**（`wallet_transaction.py:85` default='done'）。
- 首購扣款：`_create_and_confirm_subscription` → `sh_subscription_confirm()`（`today>=sh_start_date` 時）→ fix 層 `_handle_invoice_creation` → 餘額足夠才 `_pay_from_wallet`（產生 `done` deduct）；餘額不足則 fallback `super()`（**不碰錢包、無 `done` deduct**）。
- `#324` never-paid 訂閱：只有 `state='failed'` 的 deduct 交易（上游 retry cron 產生），**無 `done` deduct**。
- ⚠️ **既有 `#318` happy-path 測試 fixture 的「已付」sub 其實餘額為 0、無真扣款、無 `done` deduct**（`test_service_delete_subscription.py`、`test_change_plan_refund.py::TestChangePlanRefundIsRefundTyped`）；純靠 `today==sh_start_date` 日期 proxy 過關。加了 payment gate 後這些 fixture 必須補上真扣款（Task 3 Step 8–10）才不會誤退 0。

**退點呼叫者（三處，全走 SSOT `_compute_unused_refund`）：**
- `sh_subscription_fix.py:_settle_on_entity_delete`（CS/SH/SA 刪除聯動，#318）。
- `controllers/paas.py:api_service_change_plan`（換 plan controller）。
- `woow_paas_platform.change_plan_wizard`（後台換 plan wizard，`_compute_refund`/`action_change_plan`）。
把 gate 放進共用的 `_compute_unused_refund` → 三處自動受保護（defense in depth，不怕漏改呼叫點）。

**fix 層 `_sh_create_auto_invoice` 既有 dunning 覆蓋（`test_subscription_failure_handling.py`，成為唯一路徑後仍有效）：**
- T19b：失敗→成功 reuse invoice、reset counter、**成功後 next_payment 前進**。
- T19c：連續 N 次失敗 → auto-cancel + 一封信（有限收斂）。
- T19g：`next_payment <= today` 的 past-due 會被重新處理。

---

## File Structure

- `src/models/sh_subscription_fix.py`（Modify）— 新增兩支 cron no-op override（Task 1）；抽出純比例公式 `_prorata_unused_refund`、新增 `_has_paid_current_cycle`、`_compute_unused_refund` 加 payment gate（Task 2、3）。
- `src/tests/test_wallet_cron_neutralized.py`（Create）— Task 1 的 cron no-op 測試 + 「有餘額一週期只扣一次」測試。
- `src/tests/test_refund_requires_payment.py`（Create）— Task 3 的 payment gate 測試（never-paid→0、paid→prorata）。
- `src/tests/test_change_plan_refund.py`（Modify）— Task 2 把純公式測試重指向 `_prorata_unused_refund`；Task 3 給 `TestChangePlanRefundIsRefundTyped` fixture 補真扣款。
- `src/tests/test_service_delete_subscription.py`（Modify）— Task 3 給 `_DeleteFixture` / SH / SA fixture 補真扣款 helper。
- `scripts/maintenance/cleanup_324_stg.py`（Create）— Task 4 stg 清理腳本（odoo shell，冪等、dry-run 預設）。
- `docs/guides/maintenance/2026-07-10-324-stg-cleanup-sop.md`（Create）— Task 4 SOP 文件。
- `src/__manifest__.py`（Modify）— Task 5 版號 bump。

---

### Task 1: Neutralize 上游兩支 buggy wallet cron（part a）

**Files:**
- Modify: `src/models/sh_subscription_fix.py`（在 class `ShSubscriptionFix` 內新增兩個 `@api.model` override）
- Test: `src/tests/test_wallet_cron_neutralized.py`（Create）
- Modify: `src/tests/__init__.py`（註冊新測試檔）

**Interfaces:**
- Consumes: 上游 `sh.subscription.subscription._cron_wallet_charge()` / `._cron_wallet_charge_retry()`（`@api.model`，各只被對應 ir.cron 呼叫；本 repo src 零呼叫，已 grep 坐實）。
- Produces: 兩支 method 變 no-op（log + return，**不呼叫 super**）；`_sh_create_auto_invoice` 成為唯一有效計費路徑。無新公開簽章供後續 task 使用。

- [ ] **Step 1: 寫失敗測試（retry cron 不再自我續命）**

新增 `src/tests/test_wallet_cron_neutralized.py`：

```python
"""#324 part (a): 上游兩支 buggy wallet cron 被 neutralize 成 no-op。

紅→綠也同時複現 bug：override 前，retry cron 會對窗內 failed 交易重試、
再建一筆新 failed（缺陷 A）；charge cron 會對 due 訂閱扣款失敗建 failed、
且不推進 next_payment（缺陷 B）。override 後兩者皆 no-op。
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class _WalletCronFixture(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Cron Neu Plan', 'sh_plan_price': 499.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        self.partner = self.env['res.partner'].create({'name': 'Cron Neu Partner'})

    def _due_sub(self):
        """建一張 due today、in_progress、餘額為 0（扣款必失敗）的訂閱。
        直接 create 免走 confirm/onchange（保持單元純度）。"""
        today = date.today()
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 499.0,
            'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today, 'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress',
        })

    def _deduct_txns(self, sub):
        return self.WalletTx.search([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'),
        ])


class TestWalletRetryCronNeutralized(_WalletCronFixture):
    def test_retry_cron_does_not_create_new_failed_txn(self):
        sub = self._due_sub()
        # 種一筆「窗內」failed deduct（模擬缺陷 A 的既有殘留）。
        seed = self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -499.0, 'subscription_id': sub.id, 'state': 'failed',
        })
        before = self._deduct_txns(sub)
        self.assertEqual(len(before), 1)

        self.Sub._cron_wallet_charge_retry()

        after = self._deduct_txns(sub)
        self.assertEqual(
            len(after), 1,
            "retry cron 已被 neutralize，不得再產生新的 failed deduct 交易",
        )
        self.assertEqual(after.id, seed.id)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `--test-tags /woow_paas_platform:TestWalletRetryCronNeutralized`
Expected: FAIL — override 前 retry cron 會呼叫 `_process_subscription_wallet_payment` 再建一筆新 failed，`len(after) == 2`。

- [ ] **Step 3: 寫失敗測試（charge cron 不再建 failed、不動 next_payment）**

在同檔追加：

```python
class TestWalletChargeCronNeutralized(_WalletCronFixture):
    def test_charge_cron_is_noop_on_due_sub(self):
        sub = self._due_sub()
        original_next = sub.sh_date_of_next_payment

        self.Sub._cron_wallet_charge()

        self.assertFalse(
            self._deduct_txns(sub),
            "charge cron 已被 neutralize，不得對 due 訂閱建任何 deduct 交易",
        )
        self.assertEqual(
            sub.sh_date_of_next_payment, original_next,
            "neutralize 後 charge cron 不得改動 next_payment",
        )
```

- [ ] **Step 4: 跑測試確認失敗**

Run: `--test-tags /woow_paas_platform:TestWalletChargeCronNeutralized`
Expected: FAIL — override 前 charge cron 會建一筆 failed deduct（`_deduct_txns` 非空）。

- [ ] **Step 5: 實作 no-op override**

在 `src/models/sh_subscription_fix.py` class `ShSubscriptionFix` 內（建議放在 `_sh_create_auto_invoice` 之上、`_settle_on_entity_delete` 之下），新增：

```python
    @api.model
    def _cron_wallet_charge(self):
        """[#324] Neutralize 上游 sh_subscription_wallet 的 daily 扣款 cron。

        上游 ``_cron_wallet_charge`` 有缺陷 B（扣款成功不推進
        ``sh_date_of_next_payment``、且用 ``==`` 等值比對），與本 repo
        dunning-aware 的 ``_sh_create_auto_invoice`` 職責重疊且互相打架。
        本 fix 層（MRO 晚於上游）將其 override 成 no-op，讓 invoice-based
        的 ``_sh_create_auto_invoice`` 成為唯一計費路徑（該路徑為超集：
        錢包扣款 + auto-topup 刷卡 + dunning 收斂）。

        刻意不呼叫 ``super()``——目的就是完全停用上游行為。以 method
        override 而非 flip ``ir.cron.active``，是為了耐上游模組升級
        （升級不會重加此方法，override 恆勝出）。
        """
        _logger.info(
            "[SUB_FIX] _cron_wallet_charge neutralized (#324); "
            "billing handled solely by _sh_create_auto_invoice"
        )
        return

    @api.model
    def _cron_wallet_charge_retry(self):
        """[#324] Neutralize 上游 sh_subscription_wallet 的 hourly 重試 cron。

        上游 ``_cron_wallet_charge_retry`` 有缺陷 A：重試失敗時
        ``_process_wallet_payment`` 會 rollback 後再 create 一筆新的
        ``state='failed'`` 交易（create_date=now），使「近 24h failed」窗
        永遠有剛產生的成員 → 窗永不過期 → 每小時無限重試、failed 列爆量
        （stg 坐實 832 筆）。dunning 重試已由 ``_sh_create_auto_invoice``
        以有限次數（threshold 後 auto-cancel）承擔，故此處 no-op。
        """
        _logger.info(
            "[SUB_FIX] _cron_wallet_charge_retry neutralized (#324); "
            "bounded retry handled by _sh_create_auto_invoice dunning"
        )
        return
```

- [ ] **Step 6: 跑兩個測試確認通過**

Run: `--test-tags /woow_paas_platform:TestWalletRetryCronNeutralized,/woow_paas_platform:TestWalletChargeCronNeutralized`
Expected: PASS（兩個 class 全綠）。

- [ ] **Step 7: 寫「有餘額一週期只扣一次」測試（唯一路徑仍正確）**

在同檔追加（走 fix 層唯一路徑 `_sh_create_auto_invoice`，直接坐實驗收條件）：

```python
class TestSoleBillingPathChargesOncePerCycle(_WalletCronFixture):
    """有足額餘額的訂閱：_sh_create_auto_invoice 一週期只扣一次
    （成功後 next_payment 前進，同日再跑不再命中 `<= today`）。"""

    def _fund(self, amount):
        # loyalty card 加點 → wallet_balance 反映
        card = self.partner.get_wallet_loyalty_card()
        card.sudo().points = amount
        self.partner.invalidate_recordset(['wallet_balance'])

    def test_sufficient_balance_charged_once_per_cycle(self):
        # journal 前置（_get_or_create_cycle_invoice 需要）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        sub = self._due_sub()
        self._fund(2000.0)
        original_next = sub.sh_date_of_next_payment

        # 第一次 cron：扣一次、next_payment 前進一個月
        self.Sub._sh_create_auto_invoice()
        done1 = self.WalletTx.search([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'), ('state', '=', 'done')])
        self.assertTrue(done1, "有餘額應成功扣款一次（done deduct）")
        self.assertGreater(
            sub.sh_date_of_next_payment, original_next,
            "成功扣款後 next_payment 必須前進一個週期")

        # 第二次同日再跑 cron：next_payment 已在未來 → 不再命中 → 不重扣
        self.Sub._sh_create_auto_invoice()
        done2 = self.WalletTx.search([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'), ('state', '=', 'done')])
        self.assertEqual(
            len(done2), len(done1),
            "同一週期內第二次 cron 不得重複扣款（一週期只扣一次）")
```

> 註冊測試檔：在 `src/tests/__init__.py` 加入 `from . import test_wallet_cron_neutralized`（依現有字母序位置插入）。

- [ ] **Step 8: 跑整個新測試檔確認通過**

Run: `--test-tags /woow_paas_platform:TestWalletRetryCronNeutralized,/woow_paas_platform:TestWalletChargeCronNeutralized,/woow_paas_platform:TestSoleBillingPathChargesOncePerCycle`
Expected: PASS（三個 class 全綠）。

- [ ] **Step 9: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_wallet_cron_neutralized.py src/tests/__init__.py
git commit -m "fix: #324 停用上游兩支 buggy wallet cron，統一由 _sh_create_auto_invoice 計費"
```

---

### Task 2: 抽出純比例退點公式（refactor，為 Task 3 的 payment gate 鋪路）

**Files:**
- Modify: `src/models/sh_subscription_fix.py`（把 `_compute_unused_refund` 的純數學抽成 `_prorata_unused_refund`）
- Modify: `src/tests/test_change_plan_refund.py`（純公式測試重指向 `_prorata_unused_refund`）

**Interfaces:**
- Consumes: 現有 `_compute_unused_refund(self)`（`sh_subscription_fix.py:25`）。
- Produces: 新 `_prorata_unused_refund(self) -> float`（純比例公式，含 dates 缺失／end<start／zero-duration 守衛，回傳 `round(price * remaining/total, 2)`，clamp 到 0）；`_compute_unused_refund` 改為呼叫它（本 task 行為不變，Task 3 才加 gate）。

- [ ] **Step 1: 抽出純公式方法（行為不變）**

把 `sh_subscription_fix.py` 的 `_compute_unused_refund`（第 25–42 行）改成：保留原簽章與 docstring 意圖，但把「日期守衛 + 比例公式」移到新的 `_prorata_unused_refund`，`_compute_unused_refund` 暫時直接委派（本 task 不改行為）：

```python
    def _compute_unused_refund(self):
        """Pro-rata refund for the unused portion of the current paid cycle.

        本 task（#324 refactor）先把純比例公式抽到
        ``_prorata_unused_refund``；Task 3 會在此加「本週期是否真的付過」
        的 payment gate。此版行為與抽取前完全一致。
        """
        self.ensure_one()
        return self._prorata_unused_refund()

    def _prorata_unused_refund(self):
        """純比例退點公式（不驗付款，只算 today 落在 [start, end] 的剩餘比例）。

        Returns 0 when today < sh_start_date（trial / 未開始）、dates 缺失、
        end < start（歷史壞資料）或 zero-duration cycle。formula
        ``price * (end - today) / (end - start)``；``max(0, end - today)``
        clamp 避免過期後為負。
        """
        self.ensure_one()
        if not self.sh_end_date or not self.sh_start_date:
            return 0
        today = fields.Date.today()
        if today < self.sh_start_date:
            return 0
        total_days = (self.sh_end_date - self.sh_start_date).days or 1
        remaining_days = max(0, (self.sh_end_date - today).days)
        return round(self.sh_plan_price * remaining_days / total_days, 2)
```

- [ ] **Step 2: 純公式測試重指向新方法**

`src/tests/test_change_plan_refund.py` 的 `class TestChangePlanRefund` 內 9 個純公式測試（`test_refund_zero_during_trial_period` … `test_refund_zero_when_zero_duration_cycle`）目前呼叫 `sub._compute_unused_refund()`。這些用 `self.Sub.new({...})`（in-memory、無 persisted id）測「純數學」，Task 3 加 gate 後 `_compute_unused_refund` 會因查不到付款而回 0 → 破。故本 step 把這 9 個測試的呼叫改為 `sub._prorata_unused_refund()`（測純公式的意圖不變）。

逐一把該 class 內 `._compute_unused_refund()` 換成 `._prorata_unused_refund()`（共 9 處；`test_wizard_refund_uses_helper_during_trial` 走 wizard `_compute_refund`，**不改**——它驗端到端仍應在 trial 退 0，Task 3 之後仍成立）。

```bash
# 精準替換（只在 TestChangePlanRefund 的純公式斷言）：手動逐處改，勿全域 sed
# （TestChangePlanRefundIsRefundTyped / wizard 測試不得動）。
```

- [ ] **Step 3: 跑測試確認通過（refactor 無行為變更）**

Run: `--test-tags /woow_paas_platform:TestChangePlanRefund`
Expected: PASS（9 個純公式測試綠；wizard trial 測試綠）。

- [ ] **Step 4: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_change_plan_refund.py
git commit -m "refactor: #324 抽出純比例退點公式 _prorata_unused_refund"
```

---

### Task 3: 退點加 payment gate（part b，堵未賺得退點套利）

**Files:**
- Modify: `src/models/sh_subscription_fix.py`（新增 `_has_paid_current_cycle`；`_compute_unused_refund` 加 gate）
- Test: `src/tests/test_refund_requires_payment.py`（Create）
- Modify: `src/tests/__init__.py`（註冊新測試檔）
- Modify: `src/tests/test_service_delete_subscription.py`（`_DeleteFixture._mark_cycle_paid` helper；`TestSettleOnEntityDelete.test_paid_sub_cancelled_and_refunded` / `TestCloudServiceUnlinkSettlesSubscription.test_unlink_cancels_and_refunds_paid_sub` / SH / SA 付費路徑測試補 `done` deduct）
- Modify: `src/tests/test_change_plan_refund.py`（`TestChangePlanRefundIsRefundTyped` fixture 補真扣款）

**Interfaces:**
- Consumes: `_prorata_unused_refund`（Task 2）、`wallet.transaction`（`transaction_type` / `state` / `subscription_id`）。
- Produces: 新 `_has_paid_current_cycle(self) -> bool`（本訂閱是否存在 `done` 的 `deduct` 交易）；`_compute_unused_refund` = gate + 純公式。三個退點呼叫者（settle-on-delete / change-plan controller / wizard）自動受保護。

- [ ] **Step 1: 寫失敗測試（never-paid 退 0；paid 退 prorata）**

新增 `src/tests/test_refund_requires_payment.py`：

```python
"""#324 part (b): 退點前提＝本訂閱有成功扣款（done deduct）。

堵住套利路徑：訂閱→付款靜默失敗（服務照跑）→刪服務→退未賺得點。
never-paid（只有 failed deduct 或無交易）刪服務退 0；真已付（有 done
deduct）仍按剩餘比例退（#318 happy path 不回歸）。
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestRefundRequiresPayment(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'Gate Partner'})

    def _mid_cycle_sub(self):
        """今天正好在 [start, end] 中點的 in_progress 訂閱（純公式應回 50）。"""
        today = date.today()
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': 100.0, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=15),
            'sh_end_date': today + timedelta(days=15),
            'sh_date_of_next_payment': today + timedelta(days=15),
            'state': 'in_progress',
        })

    def _seed_deduct(self, sub, state):
        return self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -100.0, 'subscription_id': sub.id, 'state': state,
        })

    def test_never_paid_refunds_zero(self):
        """完全無扣款交易 → 退 0（服務照跑但從未成功付款）。"""
        sub = self._mid_cycle_sub()
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_only_failed_deduct_refunds_zero(self):
        """只有 failed deduct（#324 類）→ 退 0。"""
        sub = self._mid_cycle_sub()
        self._seed_deduct(sub, 'failed')
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_done_deduct_refunds_prorata(self):
        """有 done deduct（真已付）→ 按剩餘比例退（純公式 50）。"""
        sub = self._mid_cycle_sub()
        self._seed_deduct(sub, 'done')
        self.assertEqual(sub._compute_unused_refund(), 50.0)

    def test_prorata_formula_still_reachable_without_gate(self):
        """純公式方法本身不受 gate 影響（Task 2 的抽取仍在）。"""
        sub = self._mid_cycle_sub()
        self.assertEqual(sub._prorata_unused_refund(), 50.0)
```

> 註冊：`src/tests/__init__.py` 加 `from . import test_refund_requires_payment`。

- [ ] **Step 2: 跑測試確認失敗**

Run: `--test-tags /woow_paas_platform:TestRefundRequiresPayment`
Expected: FAIL — `test_never_paid_refunds_zero`、`test_only_failed_deduct_refunds_zero` 回 50（現況只看日期，未 gate）。`test_done_deduct_refunds_prorata`、`test_prorata_formula_still_reachable_without_gate` 應已 PASS。

- [ ] **Step 3: 實作 payment gate**

在 `sh_subscription_fix.py` 新增 `_has_paid_current_cycle`，並在 `_compute_unused_refund` 前置 gate：

```python
    def _has_paid_current_cycle(self):
        """本訂閱是否有過成功扣款（``done`` 的 ``deduct`` 交易）。

        真扣款（``_pay_from_wallet`` → ``deduct_wallet_points``）建的
        wallet.transaction 預設 ``state='done'``；#324 類「付款靜默失敗」
        訂閱只有 ``state='failed'`` 的 deduct。以此區分「真付過」與
        「從未付過」。

        註：本訂閱的計費視窗 [sh_start_date, sh_end_date] 於建立時一次寫定、
        不隨週期在原地推進（續約另建新 sub），故「本訂閱付過」等價於「這個
        退點視窗付過」；過期視窗另由 ``_prorata_unused_refund`` 的
        ``max(0, end - today)`` clamp 歸零。NewId（in-memory 記錄）無
        persisted id，直接回 False（純公式路徑不經此 gate）。
        """
        self.ensure_one()
        if not isinstance(self.id, int):
            return False
        return bool(self.env['wallet.transaction'].sudo().search_count([
            ('subscription_id', '=', self.id),
            ('transaction_type', '=', 'deduct'),
            ('state', '=', 'done'),
        ]))

    def _compute_unused_refund(self):
        """Pro-rata refund for the unused portion of the current paid cycle.

        #324 part (b)：退點前提為「本訂閱有成功扣款」。無成功付款（never-paid
        或僅 failed deduct）→ 退 0，對齊 trial→0 語意，堵住「付款靜默失敗→
        刪服務退未賺得點」的套利。有成功付款才套用 ``_prorata_unused_refund``。
        """
        self.ensure_one()
        if not self._has_paid_current_cycle():
            return 0
        return self._prorata_unused_refund()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `--test-tags /woow_paas_platform:TestRefundRequiresPayment`
Expected: PASS（4 個測試全綠）。

- [ ] **Step 5: 跑既有 #318 退點測試，觀察預期回歸（fixture 未補真扣款）**

Run: `--test-tags /woow_paas_platform:TestSettleOnEntityDelete,/woow_paas_platform:TestCloudServiceUnlinkSettlesSubscription,/woow_paas_platform:TestSmartHomeUnlinkSettlesSubscription,/woow_paas_platform:TestSecurityAccessUnlinkSettlesSubscription,/woow_paas_platform:TestChangePlanRefundIsRefundTyped`
Expected: FAIL — 這些 fixture 的「已付」sub 餘額為 0、無 `done` deduct，加 gate 後退 0，斷言 `refund == 300` / `txn` 存在會失敗。**這是預期的**（Step 6–10 補真扣款後轉綠）。

- [ ] **Step 6: 在 `test_service_delete_subscription.py` 的 `_DeleteFixture` 加真扣款 helper**

在 `_DeleteFixture` class 內加一個 helper，並在 `_seed_sub` 後對「付費 plan」的測試補上一筆 `done` deduct（模擬首購真扣款）：

```python
    def _mark_cycle_paid(self, sub):
        """模擬本週期已真扣款：建一筆 done 的 deduct wallet.transaction，
        讓 _has_paid_current_cycle 為真（#324 gate）。"""
        return self.WalletTx.create({
            'partner_id': sub.sh_partner_id.id,
            'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price,
            'subscription_id': sub.id,
            'state': 'done',
        })
```

- [ ] **Step 7: 對付費 plan 的刪除退點測試補真扣款**

**要補款（在 `sub = self._seed_sub(svc, self.plan_paid)` 之後、呼叫 `_settle_on_entity_delete()` / `unlink()` 之前，插入 `self._mark_cycle_paid(sub)`）——只有這兩個付費退點測試：**
- `TestSettleOnEntityDelete.test_paid_sub_cancelled_and_refunded`
- `TestCloudServiceUnlinkSettlesSubscription.test_unlink_cancels_and_refunds_paid_sub`

**不補款（斷言本就退 0 / 與 gate 無關，補了只是多餘 fixture 程式碼）：**
- `TestSettleOnEntityDelete.test_already_cancelled_sub_is_noop`（sub 被改 `close`，非 in_progress → `_settle_on_entity_delete` 早退、退 0）
- `TestSettleOnEntityDelete.test_partnerless_sub_cancelled_without_refund`（partner 被 null → 退 0）
- `test_trial_sub_*`、`test_unlink_trial_*`、`test_unlink_service_without_subscription_is_clean`、`test_helm_failure_*`（trial → 0 或無退點）

`TestSmartHome...` / `TestSecurityAccess...` 兩個 `test_unlink_cancels_and_refunds_paid_sub`：因它們是獨立 class（非 `_DeleteFixture` 子類），在各自 class 內就地建 `done` deduct（用 `self.WalletTx`）：

```python
        # 在 self.smart_home.unlink() / self.sa.unlink() 之前插入：
        self.WalletTx.create({
            'partner_id': partner.id, 'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price, 'subscription_id': sub.id,
            'state': 'done',
        })
```

- [ ] **Step 8: 對 `TestChangePlanRefundIsRefundTyped` 補真扣款**

`test_change_plan_refund.py::TestChangePlanRefundIsRefundTyped.setUp` 尾端（`_create_and_confirm_subscription` 之後、測試方法執行前），對建立的 `self.service.subscription_id` 補一筆 `done` deduct：

```python
        # #324：換 plan 退點也走 payment gate，fixture 補首購真扣款。
        sub = self.service.sudo().subscription_id
        self.WalletTx.create({
            'partner_id': partner.id, 'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price, 'subscription_id': sub.id,
            'state': 'done',
        })
```

- [ ] **Step 9: 跑受影響的 #318 測試確認全綠**

Run: `--test-tags /woow_paas_platform:TestSettleOnEntityDelete,/woow_paas_platform:TestCloudServiceUnlinkSettlesSubscription,/woow_paas_platform:TestSmartHomeUnlinkSettlesSubscription,/woow_paas_platform:TestSecurityAccessUnlinkSettlesSubscription,/woow_paas_platform:TestChangePlanRefundIsRefundTyped`
Expected: PASS（付費退點測試補款後轉綠；trial / partnerless / helm-fail 測試仍綠）。

- [ ] **Step 10: 加一條「換 plan 對 never-paid 退 0」的明確回歸測試**

在 `test_refund_requires_payment.py` 追加（直接坐實套利被堵，跨到 settle 路徑）：

```python
    def test_settle_on_delete_never_paid_credits_zero(self):
        """never-paid in_progress 訂閱刪除結算：取消但退 0（不得退未賺得點）。"""
        sub = self._mid_cycle_sub()          # 無 done deduct
        credited = sub._settle_on_entity_delete()
        self.assertEqual(sub.state, 'cancel')  # 仍取消以止付
        self.assertEqual(credited, 0.0)        # 但不退未賺得點
        self.assertFalse(self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund')]))
```

- [ ] **Step 11: 跑新測試檔全綠**

Run: `--test-tags /woow_paas_platform:TestRefundRequiresPayment`
Expected: PASS（5 個測試全綠）。

- [ ] **Step 12: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_refund_requires_payment.py \
        src/tests/test_service_delete_subscription.py src/tests/test_change_plan_refund.py \
        src/tests/__init__.py
git commit -m "fix: #324 退點前提改為本訂閱有成功扣款，堵未賺得退點套利"
```

---

### Task 4: stg 清理腳本 + SOP（part c，只交付、leader 執行）

**Files:**
- Create: `scripts/maintenance/cleanup_324_stg.py`（odoo shell 腳本）
- Create: `docs/guides/maintenance/2026-07-10-324-stg-cleanup-sop.md`（SOP）

**Interfaces:**
- Consumes: `env`（odoo shell 注入的全域）、`wallet.transaction`、`sh.subscription.subscription`。
- Produces: 冪等、condition-based 的清理函式；預設 `DRY_RUN=True` 只印不改。**本 task 無自動化測試**（腳本在 odoo shell 執行、不進 test suite），驗收＝dry-run 輸出 review + leader 實跑。

- [ ] **Step 1: 寫清理腳本**

Create `scripts/maintenance/cleanup_324_stg.py`：

```python
"""#324 stg 清理腳本（冪等、condition-based、預設 DRY_RUN）。

用途：
  1. 把上游 retry cron 產生的 failed deduct 垃圾交易標記為 cancelled
     （保留審計、不刪除；condition：state='failed' 且 transaction_type='deduct'
     且 subscription_id 非空）。
  2. 校正受影響訂閱的 next_payment（condition：in_progress、next_payment < today、
     且「從未成功扣款」＝無 done deduct）→ 推到 today + 一個週期，避免修好後
     仍卡在過去到期日被 dunning 立即重扣（stg 為測試資料）。

執行（leader，在目標 DB 的 odoo shell 內）：
  DRY_RUN 預設 True 只印報告；確認後把 DRY_RUN 改 False 再跑一次。

  kubectl -n <ns> exec -it <odoo-pod> -- \
    odoo shell -c /etc/odoo/odoo.conf -d <db> --no-http \
      < scripts/maintenance/cleanup_324_stg.py
  # 或在互動 shell 內 exec(open('.../cleanup_324_stg.py').read())

冪等性：已 cancelled 的交易、已無 done-deduct-且已在未來的訂閱不會被重複處理。
"""
import logging
from dateutil.relativedelta import relativedelta

from odoo import fields

_logger = logging.getLogger("cleanup_324_stg")

DRY_RUN = True  # ← leader 確認 dry-run 報告後改 False 實跑

_UNIT_DELTA = {
    'day': lambda n: relativedelta(days=n),
    'week': lambda n: relativedelta(weeks=n),
    'month': lambda n: relativedelta(months=n),
    'year': lambda n: relativedelta(years=n),
}


def _has_done_deduct(env, sub):
    return bool(env['wallet.transaction'].sudo().search_count([
        ('subscription_id', '=', sub.id),
        ('transaction_type', '=', 'deduct'),
        ('state', '=', 'done'),
    ]))


def run(env):
    today = fields.Date.today()

    # (1) failed deduct 垃圾 → cancelled（冪等：只抓仍 failed 的）
    garbage = env['wallet.transaction'].sudo().search([
        ('transaction_type', '=', 'deduct'),
        ('state', '=', 'failed'),
        ('subscription_id', '!=', False),
    ])
    print("[#324] failed deduct garbage to cancel: %s" % len(garbage))
    if not DRY_RUN and garbage:
        garbage.write({'state': 'cancelled'})

    # (2) 受影響訂閱 next_payment 校正
    stuck = env['sh.subscription.subscription'].sudo().search([
        ('state', '=', 'in_progress'),
        ('sh_date_of_next_payment', '<', today),
    ])
    affected = [s for s in stuck if not _has_done_deduct(env, s)]
    print("[#324] never-paid overdue subs to re-date: %s" % len(affected))
    for sub in affected:
        delta_fn = _UNIT_DELTA.get(sub.sh_unit)
        new_next = today + delta_fn(sub.sh_recurrency or 1) if delta_fn else today
        print("  sub=%s name=%s next_payment %s -> %s" % (
            sub.id, sub.name, sub.sh_date_of_next_payment, new_next))
        if not DRY_RUN:
            sub.sudo().write({'sh_date_of_next_payment': new_next})

    if not DRY_RUN:
        env.cr.commit()
        print("[#324] committed.")
    else:
        print("[#324] DRY_RUN — no changes written.")


run(env)  # odoo shell 注入的 env
```

- [ ] **Step 2: 寫 SOP 文件**

Create `docs/guides/maintenance/2026-07-10-324-stg-cleanup-sop.md`（frontmatter 對齊 `docs/README.md` guides 類別；內容：前置條件、dry-run→確認→實跑步驟、冪等性說明、回退方式＝cancelled 交易可改回 failed／next_payment 可再校正、風險註記「只在確認 target DB 後執行、勿在 prod 跑（prod 零訂閱）」）。

- [ ] **Step 3: 本機 lint 腳本可 import（語法檢查，不連 DB）**

Run: `python -c "import ast; ast.parse(open('scripts/maintenance/cleanup_324_stg.py').read())"`
Expected: 無輸出（語法正確）。

- [ ] **Step 4: Commit**

```bash
git add scripts/maintenance/cleanup_324_stg.py docs/guides/maintenance/2026-07-10-324-stg-cleanup-sop.md
git commit -m "chore: #324 stg 清理腳本與 SOP（failed 垃圾交易 + next_payment 校正）"
```

---

### Task 5: 版號 bump + full suite 綠

**Files:**
- Modify: `src/__manifest__.py:3`

- [ ] **Step 1: bump 版號**

把 `src/__manifest__.py` 的 `"version": "18.0.1.0.50",` 改成 `"version": "18.0.1.0.51",`（開 PR 前先 `git fetch origin && git log origin/develop -1` 確認無人先 bump；若已 bump 到 .51 則改 .52）。

- [ ] **Step 2: 跑 full module suite**

Run: full install + `--test-tags /woow_paas_platform`（recipe 見 leader memory `reference_odoo_test_docker_desktop_paas_image`；含 `-e AGENT_RUNTIME_NAMESPACE_SECRET=x` 消除 2 個 ns-left-set error）。
Expected: `0 failed, 0 error(s)`（或唯 2 個 `TestNamespaceLeftSetUnion` error＝未設 secret 時的環境 flake，可忽略）。

- [ ] **Step 3: Commit**

```bash
git add src/__manifest__.py
git commit -m "chore: #324 版號 bump 18.0.1.0.51"
```

---

## Self-Review

**1. Spec coverage：**
- part (a) 停用/覆寫上游兩支 cron → Task 1（method no-op override + 測試坐實）。✓
- part (b) 堵未賺得退點（`_compute_unused_refund` 加「本週期已付」前提）→ Task 2（抽公式）+ Task 3（payment gate + fixture 補款 + never-paid 退 0 回歸）。✓
- part (c) stg 清理腳本（冪等、condition-based）→ Task 4。✓
- 版號 .51 → Task 5。✓
- 驗收條件對照：上游兩支 cron 失效（Task 1 T… 兩 class）、失敗訂閱重試有限收斂（既有 T19c，成為唯一路徑）、成功扣款推進 next_payment（既有 T19b + Task 1 Step 7）、有餘額一週期只扣一次（Task 1 Step 7）、never-paid 刪服務退 0（Task 3 Step 10）、已付按比例退（Task 3 Step 7/9）、full suite 綠（Task 5）。✓

**2. Placeholder scan：** 無 TBD/TODO；每個 code step 皆含完整程式碼與確切指令。✓

**3. Type consistency：** `_prorata_unused_refund`（Task 2 定義，Task 3 消費）、`_has_paid_current_cycle`（Task 3 定義+消費）、`_mark_cycle_paid`（Task 3 Step 6 定義、Step 7 消費）、`_compute_unused_refund`（三 task 一致簽章）名稱一致。✓

## 已知風險與 plan-review 待壓測點

1. **「本訂閱付過」vs「本週期付過」（已驗證）**：本 plan 採「本訂閱有 done deduct」為 proxy。**已於上游源碼坐實**：`sh_start_date`/`sh_end_date` 只在 create/onchange 寫入（`sh_subscription_subscription.py:146–186`），遞迴計費 `_sh_create_auto_invoice` **只推進 `sh_date_of_next_payment`、永不原地推進 start/end**；續約是**另建子約**（`sh_subscription_id` 指向母約，line 230）而非改母約日期。故每張 sub 的 [start,end]＝固定的單一視窗，「本訂閱付過」等價於「這個退點視窗付過」。又：`_prorata_unused_refund` 的 `max(0, end-today)` clamp 使 today 超出首視窗即退 0——與 #318 既有 pro-rata 語意一致（長期續約 sub 因 today>首視窗 end 本就退 0），本 gate 不引入回歸。
2. **cron neutralize 只 override 方法、ir.cron 仍會 fire**（no-op 空轉）：接受（無副作用、耐升級）；若要連 ir.cron 一併停用需額外 force-write 且撞 `noupdate="1"` 反啟用，取捨後選 method-override。
3. **Task 4 腳本無自動化測試**：屬 odoo-shell 維運腳本、不進 suite；以 dry-run 報告 review + leader 實跑為驗收。

<!-- peer-reviewed: 2026-07-09T22:58:01Z rounds=1 verdict=approved by=miniclaude -->
