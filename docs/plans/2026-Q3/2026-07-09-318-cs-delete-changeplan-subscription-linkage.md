---
title: "#318 CS 刪除／換 plan 訂閱聯動 Implementation Plan"
category: plans
status: done
created: 2026-07-09
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-09-318-cs-delete-changeplan-subscription-linkage.md
promoted-at: 2026-07-19
---

# #318 CS 刪除／換 plan 訂閱聯動 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 刪除 CloudService 時於同一交易內取消其關聯訂閱並按剩餘週期比例退點（以可辨識的 `refund` 型 wallet 交易入帳），並把所有換 plan 的退點交易一併正名為 `refund` 型，杜絕孤兒訂閱持續計費。

**Architecture:** 退點計算沿用唯一 SSOT `sh.subscription.subscription._compute_unused_refund()`（trial→0、自然月天數、四守衛），不另立公式。新增 `res.partner.refund_wallet_points()` 作為 `refund`-typed 的錢包加點 API（現有 `add_wallet_points` 硬編 `topup`、無型別參數）。訂閱結算邏輯集中為 `sh.subscription.subscription._settle_on_entity_delete()`（冪等：只結算 `in_progress`），由 `CloudService.unlink()` 這個 catch-all override 在 `super().unlink()` 後呼叫，因此涵蓋前端刪除／workspace cascade／cron cleanup→poll／手動 ORM 所有刪除路徑。換 plan 已是原子的（Odoo request 單一交易、無 explicit commit），本次只把它與刪除的退點統一改走 `refund_wallet_points`。

**Tech Stack:** Odoo 18、Python 3.11、`odoo.tests.common.TransactionCase`、第三方 addon `sh_subscription` / `sh_subscription_wallet`（`sh.subscription.subscription`、`wallet.transaction`、`res.partner.add_wallet_points/deduct_wallet_points`）。

## Global Constraints

- **退點公式 single source**：一律呼叫 `old_sub._compute_unused_refund()`（`src/models/sh_subscription_fix.py:25`），禁止內聯 `price * remaining / total`、禁止寫死「月＝30 天」。週期天數為實際日曆天 `(sh_end_date - sh_start_date).days`（自然月經 relativedelta 再 −1 天）。與 #273/#274 分群對齊即對齊此 helper。
- **退點型別**：退點交易 `wallet.transaction.transaction_type` 必為 `'refund'`（上游 enum 早已存在＝`sh_subscription_wallet/models/wallet_transaction.py:27`，但全 codebase 無寫入者）；不得再記成 `'topup'`。退點交易須帶 `subscription_id`（上游直接欄位＝`wallet_transaction.py:47`）供對帳追溯。
- **刪除結算原子性**：訂閱取消＋退點與 `super().unlink()` 在同一交易內；若 helm fail-loud（`_cleanup_helm()` 回 False）先 raise，則不得取消訂閱、不得退點（全交易 rollback，可重試）。禁止在流程中 `env.cr.commit()`。
- **刪除訂閱終態**：刪服務用 `state='cancel'`（對齊 issue 文字「轉 cancel」＋服務終止語意；與換 plan 關舊約用的 `'close'` 區分）。`sh_reason='Service deleted'`。
- **冪等**：`_settle_on_entity_delete()` 只結算 `state=='in_progress'` 的訂閱；已 `close/cancel/expire`（lifecycle-cleanup 或 dunning-cancel 路徑）一律 no-op 回 `0.0`，不得二次退點。
- **trial 邊界**：trial 期（`sh_trial=True` 或 `sh_end_date` 為 NULL）退點為 0，`_compute_unused_refund()` 已守；退點 0 時不建 wallet 交易（`refund_wallet_points` 對 `amount<=0` 會 raise，呼叫端須 `if refund > 0` 守衛）。
- **權限**：portal workspace owner 通常無 `sh.subscription.subscription` / `loyalty.card` 寫權；結算路徑一律 `sudo()`（比照現有 change-plan）。
- **版號**：完成後 `src/__manifest__.py` 版號 bump 一版（現值 `18.0.1.0.48` → `18.0.1.0.49`；開 PR 前 rebase 檢查是否有人先 bump）。
- **新增 test 檔必註冊**：任何新 test 檔須在 `src/tests/__init__.py` `import`，否則不會被收集。
- **commit message**：zh-tw、prefix 英文小寫（`feat:`/`fix:`/`test:`…）、不得含任何 AI 產生字樣（不加 Co-Authored-By / Generated with）。
- **範圍界定**：刪除聯動涵蓋 **CloudService（#318）＋ SmartHome / SecurityAccess（#322，三者共用同一 mixin 與同缺口，本 PR 一併接上）**——皆呼叫共用的 `_settle_on_entity_delete()`。換 plan 的**退點型別**四個入口（CS controller、CS/SH 共用 wizard、SH controller、SA controller）一併正名為 `refund`。**不在本 PR**：使用者主動取消訂閱（`billing.py api_subscription_cancel`）目前不退點/不拆服務，與刪除退點不對稱＝**#323**（產品決策，另 PR）；刪除終態用 `'cancel'`（對齊 #318 文字）不改。

---

## File Structure

**新增：**
- `src/models/res_partner_wallet.py` — `res.partner` 擴充，新增 `refund_wallet_points()`（`refund`-typed 錢包加點，帶 `subscription_id`）。單一責任：退點 API。
- `src/tests/test_refund_wallet_points.py` — `refund_wallet_points` 單元測試。
- `src/tests/test_service_delete_subscription.py` — 訂閱結算 method 測試 + `CloudService.unlink()` 刪除聯動整合測試（含原子性）。

**修改：**
- `src/models/__init__.py` — 新增 `from . import res_partner_wallet`。
- `src/models/sh_subscription_fix.py` — 新增 `_settle_on_entity_delete()`。
- `src/models/cloud_service.py` — `unlink()` 內捕獲 in_progress 訂閱、`super().unlink()` 後結算。
- `src/controllers/paas.py` — CS change-plan 退點（`:1011`）改走 `refund_wallet_points`。
- `src/wizard/change_subscription_plan_wizard.py` — 退點（`:83`）改走 `refund_wallet_points`。
- `src/controllers/smart_home.py` — SH change-plan 退點（`:186`）改走 `refund_wallet_points`。
- `src/controllers/security_access.py` — SA change-plan 退點（`:345`）改走 `refund_wallet_points`。
- `src/tests/__init__.py` — 註冊兩個新 test 檔。
- `src/__manifest__.py` — 版號 bump。

**測試執行方式（Global）：** 本機 arm64 有兩道牆，優先用 `/woow-dev:k8s-dev-sandbox`（`--context docker-desktop`）在沙盒內 `-u woow_paas_platform --test-tags /woow_paas_platform:<Class>` 實跑；沙盒起不來則把測試寫齊，回報註明未實跑原因。plan 內每個 `Run:` 指令為沙盒內語意，示意如：
`./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestRefundWalletPoints --stop-after-init`

---

### Task 1: `refund_wallet_points` — refund-typed 錢包加點 API

**Files:**
- Create: `src/models/res_partner_wallet.py`
- Modify: `src/models/__init__.py`（新增一行 import）
- Test: `src/tests/test_refund_wallet_points.py`
- Modify: `src/tests/__init__.py`（註冊）

**Upstream verification（primary source，已核對；executor 無需再查即可信賴，但路徑列出供覆核）：**
上游第三方 addon 位於 `/Users/eugene/Documents/woow/woow-paas-repo/WOOWTECH/sh_subscription/`。
- `sh_subscription_wallet/models/res_partner.py:143` — `def add_wallet_points(self, amount, description=None, payment_transaction=None)`：**無 `transaction_type` 參數**；`:159` 建 `wallet.transaction` 時硬編 `'transaction_type': 'topup'`。故「傳 type 給既有 API」不可行，必須新增 `refund_wallet_points`（或動上游，本 plan 不動上游）。
- `sh_subscription_wallet/models/wallet_transaction.py:24-32` — `transaction_type` Selection **含 `('refund', 'Refund')`（`:27`）**；此 model 為 `_name='wallet.transaction'`（本 repo 只 `_inherit` 加 `base_amount`/`bonus_amount`）。
- `sh_subscription_wallet/models/wallet_transaction.py:47-51` — `subscription_id = fields.Many2one('sh.subscription.subscription', ...)` 為 **`wallet.transaction` 的直接欄位**（上游定義；本 repo grep 不到定義是因為它在上游，`deduct_wallet_points`（`res_partner.py:209`）已在寫它）。
- `sh_subscription_wallet/models/res_partner.py:34-64` — `wallet_balance` 為 **非 stored compute**（`_compute_wallet_balance`），讀 `loyalty.card.points`；故加點後 `invalidate_recordset(['wallet_balance'])` 才讀得到新值。

**Interfaces:**
- Consumes: upstream `res.partner.get_wallet_loyalty_card()`（`res_partner.py`；回傳該 partner 的 `loyalty.card`，`.points` 為錢包餘額點數）、`wallet.transaction`（欄位 `transaction_type` Selection 含 `'refund'`＝`wallet_transaction.py:27`、`amount` Float、`balance_after` Float、`subscription_id` M2O＝`wallet_transaction.py:47`、`description` Text）。
- Produces: `res.partner.refund_wallet_points(self, amount, description=None, subscription=None) -> bool` — 加 `amount` 點到錢包並建立一筆 `transaction_type='refund'` 的 `wallet.transaction`（`amount` 為正、`balance_after` 為加點後餘額、`subscription_id` 帶入 `subscription`）；`amount <= 0` raise `UserError`。Task 3/4 依賴此簽名。

- [ ] **Step 1: 寫失敗測試**

Create `src/tests/test_refund_wallet_points.py`:

```python
"""Tests for res.partner.refund_wallet_points (issue #318)."""
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


class TestRefundWalletPoints(TransactionCase):
    """refund_wallet_points 建立 refund 型交易並加點（對比 add_wallet_points 硬編 topup）。"""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': '#318 Refund Partner'})
        self.WalletTx = self.env['wallet.transaction']

    def _refund_txns(self):
        return self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])

    def test_refund_creates_refund_typed_transaction(self):
        card = self.partner.get_wallet_loyalty_card()
        before = card.points
        ok = self.partner.refund_wallet_points(
            amount=120.0, description='Service deleted refund: n8n')
        self.assertTrue(ok)
        card.invalidate_recordset(['points'])
        self.assertEqual(card.points, before + 120.0)
        txns = self._refund_txns()
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns.transaction_type, 'refund')
        self.assertEqual(txns.amount, 120.0)
        self.assertEqual(txns.balance_after, before + 120.0)

    def test_refund_links_subscription_for_reconciliation(self):
        plan = self.env['sh.subscription.plan'].create({
            'name': '#318 Refund Plan', 'sh_plan_price': 100.0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': False,
        })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': plan.id,
            'sh_plan_price': 100.0,
        })
        self.partner.refund_wallet_points(
            amount=50.0, description='x', subscription=sub)
        txn = self._refund_txns()
        self.assertEqual(txn.subscription_id.id, sub.id)

    def test_refund_rejects_non_positive_amount(self):
        with self.assertRaises(UserError):
            self.partner.refund_wallet_points(amount=0.0)
        with self.assertRaises(UserError):
            self.partner.refund_wallet_points(amount=-5.0)
```

Register in `src/tests/__init__.py`（檔尾 append）:

```python
from . import test_refund_wallet_points
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags /woow_paas_platform:TestRefundWalletPoints`
Expected: FAIL — `AttributeError: 'res.partner'... has no attribute 'refund_wallet_points'`

- [ ] **Step 3: 寫最小實作**

Create `src/models/res_partner_wallet.py`:

```python
from odoo import _, models
from odoo.exceptions import UserError


class ResPartnerWallet(models.Model):
    """退點（refund）進錢包的 API。

    upstream ``add_wallet_points`` 把 ``transaction_type`` 硬編成 ``'topup'``
    且無型別參數，導致換 plan／刪服務的退點在對帳報表上與加值混在一起無法
    分辨。此 method 建立 ``transaction_type='refund'`` 的交易，並可帶
    ``subscription_id`` 供對帳追溯是哪張訂閱退的點。加點邏輯與 upstream
    ``add_wallet_points`` 一致（loyalty.card.points += amount）。
    """
    _inherit = 'res.partner'

    def refund_wallet_points(self, amount, description=None, subscription=None):
        self.ensure_one()
        if amount <= 0:
            raise UserError(_('Refund amount must be greater than 0'))
        loyalty_card = self.get_wallet_loyalty_card()
        if not loyalty_card:
            raise UserError(_('Unable to get wallet loyalty card'))
        loyalty_card.points += amount
        self.env['wallet.transaction'].create({
            'partner_id': self.id,
            'transaction_type': 'refund',
            'amount': amount,
            'description': description or _('Points refund'),
            'subscription_id': subscription.id if subscription else False,
            'balance_after': loyalty_card.points,
        })
        return True
```

Register in `src/models/__init__.py`（緊接 `from . import res_company` 前後皆可，放檔尾）:

```python
from . import res_partner_wallet
```

- [ ] **Step 4: 跑測試確認通過**

Run: `... --test-tags /woow_paas_platform:TestRefundWalletPoints`
Expected: PASS（3 tests）

- [ ] **Step 5: Commit**

```bash
git add src/models/res_partner_wallet.py src/models/__init__.py \
        src/tests/test_refund_wallet_points.py src/tests/__init__.py
git commit -m "feat: #318 新增 res.partner.refund_wallet_points（refund 型退點 API）"
```

---

### Task 2: `_settle_on_entity_delete` — 訂閱結算（取消＋按比例退點）

**Files:**
- Modify: `src/models/sh_subscription_fix.py`（在 `_compute_unused_refund` 之後新增 method）
- Test: `src/tests/test_service_delete_subscription.py`（新檔，先放 settlement-method 測試）
- Modify: `src/tests/__init__.py`（註冊）

**Interfaces:**
- Consumes: `self._compute_unused_refund()`（Task-independent，既有）、`res.partner.refund_wallet_points(amount, description, subscription)`（Task 1）。
- Produces: `sh.subscription.subscription._settle_on_entity_delete(self, reason='Service deleted') -> float` — 回傳**實際入帳的退點金額**（credited）。若 `state != 'in_progress'` 回 `0.0`（no-op）；否則 `refund = self._compute_unused_refund()`，僅在 `refund > 0` **且 `partner` 存在**時 `partner.sudo().refund_wallet_points(refund, description, subscription=self)` + `invalidate_recordset(['wallet_balance'])` 並記 `credited=refund`；無論是否退點都 `self.sudo().write({'state': 'cancel', 'sh_reason': reason})`；回傳 `credited`。`partner` 守衛因 `sh_partner_id` 上游**非 required**（`sh_subscription/models/sh_subscription_subscription.py:41` 無 `required=True`），partner-less 的 in_progress sub 仍須被取消以止付、但無對象可退點。Task 3 依賴此簽名。

- [ ] **Step 1: 寫失敗測試**

Create `src/tests/test_service_delete_subscription.py`:

```python
"""Tests for #318: deleting a CloudService cancels + prorata-refunds its
subscription. Part A — the subscription-level settlement method."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class _DeleteFixture(TransactionCase):
    """共用 fixture：建 plan(+product) / template / workspace / service，
    並用 service._create_and_confirm_subscription 造一張綁定的訂閱。"""

    def setUp(self):
        super().setUp()
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.CSTemplate = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']

        self.plan_paid = self._make_plan('CS Del Paid', 300.0, trial=False)
        self.plan_trial = self._make_plan('CS Del Trial', 200.0, trial=True)

        self.template = self.CSTemplate.create({
            'name': 'CS Del Tmpl', 'slug': 'cs-del',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cs-del', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.owner = self.env['res.users'].create({
            'name': 'CS Del Owner', 'login': 'cs_del_owner',
            'email': 'cs_del_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.Workspace.create({
            'name': 'CS Del WS', 'owner_id': self.owner.id,
        })

    def _make_plan(self, name, price, trial):
        vals = {
            'name': name, 'sh_plan_price': price,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': trial,
        }
        if trial:
            vals.update({'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        plan = self.Plan.create(vals)
        self.env['product.template'].sudo().create({
            'name': name + ' Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
            'list_price': price,
        })
        return plan

    def _new_service(self, name='CS Del Svc'):
        # 不設 helm_release_name/helm_namespace → unlink() 跳過 helm 清理，
        # 無需 mock operator。
        return self.CloudService.create({
            'name': name, 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

    def _seed_sub(self, service, plan):
        partner = self.workspace.owner_id.partner_id
        service._create_and_confirm_subscription(partner, plan)
        return service.sudo().subscription_id

    def _refund_txns(self, partner):
        return self.WalletTx.search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'refund'),
        ])


class TestSettleOnEntityDelete(_DeleteFixture):

    def test_paid_sub_cancelled_and_refunded(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        self.assertEqual(sub.state, 'in_progress')
        partner = sub.sh_partner_id

        refund = sub._settle_on_entity_delete()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(sub.sh_reason, 'Service deleted')
        # 當天刪除 → today == sh_start_date → 全額退（remaining == total）。
        self.assertEqual(refund, 300.0)
        txn = self._refund_txns(partner)
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.transaction_type, 'refund')
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)

    def test_trial_sub_cancelled_without_refund(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_trial)
        self.assertTrue(sub.sh_trial)
        partner = sub.sh_partner_id

        refund = sub._settle_on_entity_delete()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(refund, 0.0)
        self.assertFalse(self._refund_txns(partner))

    def test_already_cancelled_sub_is_noop(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        sub.sudo().write({'state': 'close'})
        partner = sub.sh_partner_id

        refund = sub._settle_on_entity_delete()

        self.assertEqual(refund, 0.0)
        self.assertEqual(sub.state, 'close')  # 未被改成 cancel
        self.assertFalse(self._refund_txns(partner))

    def test_partnerless_sub_cancelled_without_refund(self):
        """sh_partner_id 上游非 required；partner-less 的 in_progress sub 仍須
        被取消以止付，但無對象可退點（不得 crash）。用正常 seed 再 null
        partner，避免直接 create 撞上游其他 required 欄位。"""
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        sub.sudo().write({'sh_partner_id': False})
        self.assertFalse(sub.sh_partner_id)

        credited = sub._settle_on_entity_delete()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(credited, 0.0)
```

Register in `src/tests/__init__.py`:

```python
from . import test_service_delete_subscription
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags /woow_paas_platform:TestSettleOnEntityDelete`
Expected: FAIL — `AttributeError: ... has no attribute '_settle_on_entity_delete'`

- [ ] **Step 3: 寫最小實作**

Modify `src/models/sh_subscription_fix.py`，在 `_compute_unused_refund` method 之後、`_sh_create_auto_invoice` 之前插入：

```python
    def _settle_on_entity_delete(self, reason='Service deleted'):
        """Cancel this subscription and prorata-refund the unused paid cycle
        to the wallet when the owning entity (CloudService/SmartHome/SA) is
        deleted (issue #318).

        The entity↔subscription FK is ``ondelete='set null'`` (protects only
        the reverse direction), so deleting the entity never touches the sub —
        leaving an ``in_progress`` orphan the billing cron keeps charging.

        Idempotent: only an ``in_progress`` sub is settled. The
        lifecycle-cleanup path (sub already cancelled, cleanup fired after
        ``cleanup_months``) and dunning-cancelled subs are ``!= in_progress``
        → no-op returning ``0.0`` (no double refund). Refund uses the SSOT
        ``_compute_unused_refund`` (trial → 0). Credited as a ``refund``-typed
        wallet transaction so reconciliation can tell it from a top-up.

        Returns the amount actually credited (0.0 when trial / already-settled
        / no partner on the sub — ``sh_partner_id`` is not required upstream).
        The sub is always cancelled to stop billing even when nothing is
        credited.
        """
        self.ensure_one()
        if self.state != 'in_progress':
            return 0.0
        refund = self._compute_unused_refund()
        partner = self.sh_partner_id
        credited = 0.0
        if refund > 0 and partner:
            partner.sudo().refund_wallet_points(
                amount=refund,
                description='%s refund: %s' % (
                    reason, self.sh_subscription_plan_id.name),
                subscription=self,
            )
            partner.invalidate_recordset(['wallet_balance'])
            credited = refund
        self.sudo().write({'state': 'cancel', 'sh_reason': reason})
        return credited
```

- [ ] **Step 4: 跑測試確認通過**

Run: `... --test-tags /woow_paas_platform:TestSettleOnEntityDelete`
Expected: PASS（4 tests）

- [ ] **Step 5: Commit**

```bash
git add src/models/sh_subscription_fix.py \
        src/tests/test_service_delete_subscription.py src/tests/__init__.py
git commit -m "feat: #318 訂閱結算 _settle_on_entity_delete（取消＋按比例退點，冪等）"
```

---

### Task 3: `CloudService.unlink()` 接上刪除聯動

**Files:**
- Modify: `src/models/cloud_service.py`（`unlink()`，`:1298`）
- Test: `src/tests/test_service_delete_subscription.py`（append 整合測試 class）

**Interfaces:**
- Consumes: `sub._settle_on_entity_delete(reason)`（Task 2）、既有 `super().unlink()` / `_cleanup_helm()`。
- Produces: 無新對外簽名；行為契約＝`service.unlink()` 後其 `in_progress` 訂閱轉 `cancel` 且退點入帳；helm fail-loud 時整體 rollback（訂閱不動、record 保留）。

- [ ] **Step 1: 寫失敗測試**

Append 到 `src/tests/test_service_delete_subscription.py`（沿用同檔 `_DeleteFixture`）:

```python
class TestCloudServiceUnlinkSettlesSubscription(_DeleteFixture):

    def test_unlink_cancels_and_refunds_paid_sub(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_paid)
        partner = sub.sh_partner_id
        self.assertEqual(sub.state, 'in_progress')

        svc.unlink()

        self.assertFalse(svc.exists())
        self.assertTrue(sub.exists())            # 訂閱 record 存活
        self.assertEqual(sub.state, 'cancel')    # 已停止計費
        txn = self._refund_txns(partner)
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)

    def test_unlink_trial_sub_cancelled_no_refund(self):
        svc = self._new_service()
        sub = self._seed_sub(svc, self.plan_trial)
        partner = sub.sh_partner_id

        svc.unlink()

        self.assertEqual(sub.state, 'cancel')
        self.assertFalse(self._refund_txns(partner))

    def test_unlink_service_without_subscription_is_clean(self):
        svc = self._new_service()
        self.assertFalse(svc.subscription_id)
        svc.unlink()
        self.assertFalse(svc.exists())

    def test_helm_failure_aborts_delete_and_preserves_subscription(self):
        """原子性：helm fail-loud（_cleanup_helm→False）時 unlink raise，
        訂閱維持 in_progress、無退點、record 保留。"""
        from odoo.exceptions import UserError
        svc = self.CloudService.create({
            'name': 'CS Del Helm', 'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'svc-deadbeef',
            'helm_namespace': 'paas-ws-stg-cafef00d',
        })
        sub = self._seed_sub(svc, self.plan_paid)
        partner = sub.sh_partner_id

        with patch.object(type(svc), '_cleanup_helm', return_value=False):
            with self.assertRaises(UserError):
                svc.unlink()

        self.assertTrue(svc.exists())
        self.assertEqual(sub.state, 'in_progress')
        self.assertFalse(self._refund_txns(partner))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags /woow_paas_platform:TestCloudServiceUnlinkSettlesSubscription`
Expected: FAIL — `test_unlink_cancels_and_refunds_paid_sub` 斷言 `sub.state == 'cancel'` 失敗（現況 unlink 不碰訂閱，state 仍 `in_progress`）；trial 版同理。（`without_subscription` 與 `helm_failure` 兩測在現況即會通過，屬回歸護欄。）

- [ ] **Step 3: 寫最小實作**

Modify `src/models/cloud_service.py` 的 `unlink()`。在 method 內、`affected = self.mapped('project_ids')` 之前插入捕獲：

```python
        # #318: 在 super().unlink() 前捕獲仍 in_progress 的關聯訂閱。
        # subscription_id 是 ondelete='set null'（只保護反向），刪 service
        # 不會動訂閱 → 孤兒訂閱會被計費 cron 續扣。super().unlink() 後
        # record 離場但訂閱 record 存活，逐一結算（取消＋按比例退點）。
        # 僅結算 in_progress；lifecycle-cleanup / dunning 已取消者跳過。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
```

接著在 `res = super().unlink()` 之後、`for workspace, helm_namespace in quota_targets:` 之前插入結算迴圈：

```python
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Service deleted')
```

（順序關鍵：捕獲在 `super().unlink()` 前；結算在 `super().unlink()` 後。helm fail-loud 的 `raise UserError` 位於捕獲之後、`super().unlink()` 之前，故 raise 時結算不會執行，全交易 rollback。）

- [ ] **Step 4: 跑測試確認通過**

Run: `... --test-tags /woow_paas_platform:TestCloudServiceUnlinkSettlesSubscription`
Expected: PASS（4 tests）

- [ ] **Step 5: 跑既有 CS 刪除測試確認未回歸**

Run: `... --test-tags /woow_paas_platform:TestCloudService`（含 `test_service_delete_cascade` 等）
Expected: PASS（既有全綠）

- [ ] **Step 6: Commit**

```bash
git add src/models/cloud_service.py src/tests/test_service_delete_subscription.py
git commit -m "feat: #318 CloudService.unlink() 刪除時聯動取消訂閱並按比例退點"
```

---

### Task 4: 換 plan 退點統一為 refund 型（4 入口）

**Files:**
- Modify: `src/controllers/paas.py`（`api_service_change_plan` 退點，`:1010-1015`）
- Modify: `src/wizard/change_subscription_plan_wizard.py`（`action_change_plan` 退點，`:83-88`）
- Modify: `src/controllers/smart_home.py`（`api_smarthome_change_plan` 退點，`:186-190`）
- Modify: `src/controllers/security_access.py`（`api_sa_change_plan` 退點，`:345-349`）
- Test: `src/tests/test_change_plan_refund.py`（append 型別斷言）

**Interfaces:**
- Consumes: `partner.refund_wallet_points(amount, description, subscription)`（Task 1）。
- Produces: 四個換 plan 入口的退點交易 `transaction_type == 'refund'`（原 `'topup'`）。無新對外簽名。

- [ ] **Step 1: 先確認既有測試沒有斷言 `topup`**

Run: `grep -rn "topup" src/tests/test_change_plan_refund.py`
若有針對「換 plan 退點」斷言 `transaction_type == 'topup'` 之處，那是在鎖定舊的錯誤行為，於 Step 4 一併改成 `'refund'`（issue 明確要求退點可辨識）。純看 wallet 餘額增減的斷言不受影響（退點金額不變）。

- [ ] **Step 2: 寫失敗測試**

Append 到 `src/tests/test_change_plan_refund.py`（若該檔已有可造 in_progress 非-trial CS sub 的 fixture 則複用；否則新增以下自足 class）:

```python
class TestChangePlanRefundIsRefundTyped(TransactionCase):
    """#318: 換 plan 退點交易須為 refund 型（原本被記成 topup）。"""

    def setUp(self):
        from unittest.mock import MagicMock, patch
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        super().setUp()
        self._patch = patch
        self._MagicMock = MagicMock
        self.controller = PaasController()
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']
        self.plan_a = self._make_plan('CP Refund A', 300.0)
        self.plan_b = self._make_plan('CP Refund B', 500.0)
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'CP Refund Tmpl', 'slug': 'cp-refund',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cp-refund', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'subscription_plan_ids': [(6, 0, [self.plan_a.id, self.plan_b.id])],
        })
        self.owner = self.env['res.users'].create({
            'name': 'CP Refund Owner', 'login': 'cp_refund_owner',
            'email': 'cp_refund_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'CP Refund WS', 'owner_id': self.owner.id,
        })
        self.service = self.env['woow_paas_platform.cloud_service'].create({
            'name': 'CP Refund Svc', 'workspace_id': ws.id,
            'template_id': template.id,
        })
        partner = ws.owner_id.partner_id
        self.service._create_and_confirm_subscription(partner, self.plan_a)
        self.partner = partner

    def _make_plan(self, name, price):
        plan = self.Plan.create({
            'name': name, 'sh_plan_price': price, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        self.env['product.template'].sudo().create({
            'name': name + ' P', 'type': 'service', 'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': price,
        })
        return plan

    def test_change_plan_refund_is_refund_typed(self):
        req = self._MagicMock(env=self.env(user=self.owner))
        with self._patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request', new=req):
            res = self.controller.api_service_change_plan(
                service_id=self.service.id, plan_id=self.plan_b.id)
        self.assertTrue(res.get('success'))
        refund_txns = self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])
        self.assertEqual(len(refund_txns), 1)
        self.assertEqual(refund_txns.amount, 300.0)  # 當天換 → 全額退

    def test_wizard_change_plan_refund_is_refund_typed(self):
        """後台 wizard 路徑（api「四個入口」之一，與 controller 分開驗證，
        防 wizard 呼叫點被漏改／回退）。wizard 是 model method，免 patch
        request。"""
        wizard = self.env['woow_paas_platform.change_plan_wizard'].create({
            'cloud_service_id': self.service.id,
            'current_plan_id': self.plan_a.id,
            'new_plan_id': self.plan_b.id,
        })
        wizard.action_change_plan()
        refund_txns = self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])
        self.assertEqual(len(refund_txns), 1)
        self.assertEqual(refund_txns.amount, 300.0)
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `... --test-tags /woow_paas_platform:TestChangePlanRefundIsRefundTyped`
Expected: FAIL（兩 test：controller + wizard）— 退點被記成 `topup`，`refund` 型交易數為 0。

- [ ] **Step 4: 改四個退點呼叫點**

`src/controllers/paas.py`（CS，約 `:1010`）：

```python
        # Refund unused points
        if refund > 0:
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
```

`src/wizard/change_subscription_plan_wizard.py`（`:82`）：

```python
        # 1. Refund unused points to wallet
        if self.refund_amount > 0:
            partner.sudo().refund_wallet_points(
                amount=self.refund_amount,
                description="Plan change refund: %s -> %s" % (
                    self.current_plan_id.name, self.new_plan_id.name,
                ),
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
```

`src/controllers/smart_home.py`（`:185`）：

```python
        if refund > 0:
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
```

`src/controllers/security_access.py`（`:341`，保留原 sudo 註解）：

```python
        if refund > 0:
            # sudo: crediting wallet points writes loyalty.card.points which
            # portal users can't write; same narrow widening as the role-gated
            # cascade above (mirrors smart_home.api_smarthome_change_plan).
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s"
                            % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
```

若 Step 1 找到鎖定 `topup` 的舊斷言，於此步一併改為 `'refund'`。

- [ ] **Step 5: 跑測試確認通過**

Run: `... --test-tags /woow_paas_platform:TestChangePlanRefundIsRefundTyped`
Expected: PASS

- [ ] **Step 6: 跑既有換 plan 退點測試確認未回歸**

Run: `... --test-tags /woow_paas_platform:TestCloudServiceChangePlanTrialMode`（及 `test_change_plan_refund.py` 既有 class、SH/SA change-plan 測試）
Expected: PASS（退點金額不變，僅型別改變）

- [ ] **Step 7: Commit**

```bash
git add src/controllers/paas.py src/wizard/change_subscription_plan_wizard.py \
        src/controllers/smart_home.py src/controllers/security_access.py \
        src/tests/test_change_plan_refund.py
git commit -m "fix: #318 換 plan 退點統一記為 refund 型並帶 subscription 供對帳"
```

---

### Task 5: 版號 bump 與全套測試

**Files:**
- Modify: `src/__manifest__.py`（`:3` version）

**Interfaces:** 無。

- [ ] **Step 1: rebase 檢查版號**

Run: `git fetch origin && git log origin/develop -1 --format='%H %s'`
確認 `src/__manifest__.py` 版號未被他人先 bump（若已被 bump，接續其後 +1）。

- [ ] **Step 2: bump 版號**

Modify `src/__manifest__.py`，`'version': '18.0.1.0.48'` → `'version': '18.0.1.0.49'`。

- [ ] **Step 3: 跑全套 #318 相關測試**

Run:
```
... --test-tags /woow_paas_platform:TestRefundWalletPoints,\
/woow_paas_platform:TestSettleOnEntityDelete,\
/woow_paas_platform:TestCloudServiceUnlinkSettlesSubscription,\
/woow_paas_platform:TestChangePlanRefundIsRefundTyped,\
/woow_paas_platform:TestCloudService,\
/woow_paas_platform:TestCloudServiceChangePlanTrialMode
```
Expected: 全 PASS

- [ ] **Step 4: Commit**

```bash
git add src/__manifest__.py
git commit -m "chore: #318 版號 bump 18.0.1.0.49"
```

---

### Task 6: SmartHome / SecurityAccess 刪除聯動 parity（#322）

**Files:**
- Modify: `src/models/smart_home.py`（`unlink()`，`:251`）
- Modify: `src/models/security_access.py`（`unlink()`，`:494`）
- Test: `src/tests/test_service_delete_subscription.py`（append 兩個 entity-specific class）

**Interfaces:**
- Consumes: `sub._settle_on_entity_delete(reason)`（Task 2，共用、已測）。
- Produces: 無新對外簽名。行為契約＝`smart_home.unlink()` / `security_access.unlink()` 後其 `in_progress` 訂閱轉 `cancel` 且退點入帳。

**設計備註**：`SmartHome.unlink()`（smart_home.py:251）與 `SecurityAccess.unlink()`（security_access.py:494）皆為「fail-loud teardown 守衛（tunnel/CF）→ `super().unlink()`」的同一結構，與 CS 一致。故接法完全比照 Task 3：`super().unlink()` 前捕獲 in_progress 訂閱、之後結算。結算 method 的邊界（trial→0、冪等、partner-less、原子性）已由 CS 的 `TestSettleOnEntityDelete` / `TestCloudServiceUnlinkSettlesSubscription` 窮舉，且三者共用同一 method；故 SH/SA 各只加**一條 happy-path**（paid→cancel+refund）證明**捕獲+結算的 wiring 已接上**即可，不重複測共用 method 的邊界。

- [ ] **Step 1: 寫失敗測試**

Append 到 `src/tests/test_service_delete_subscription.py`：

```python
class TestSmartHomeUnlinkSettlesSubscription(TransactionCase):
    """#322: 刪 SmartHome 聯動取消訂閱＋按比例退點（wiring 驗證）。"""

    def setUp(self):
        super().setUp()
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']
        self.plan = self.Plan.create({
            'name': 'SH Del Plan', 'sh_plan_price': 300.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
        })
        self.env['product.template'].sudo().create({
            'name': 'SH Del Plan Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
            'list_price': 300.0,
        })
        self.owner = self.env['res.users'].create({
            'name': 'SH Del Owner', 'login': 'sh_del_owner',
            'email': 'sh_del_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SH Del WS', 'owner_id': self.owner.id,
        })
        self.smart_home = self.SmartHome.create({
            'name': 'SH Del Home', 'workspace_id': self.workspace.id,
        })

    def test_unlink_cancels_and_refunds_paid_sub(self):
        partner = self.workspace.owner_id.partner_id
        self.smart_home._create_and_confirm_subscription(partner, self.plan)
        sub = self.smart_home.sudo().subscription_id
        self.assertEqual(sub.state, 'in_progress')

        self.smart_home.unlink()

        self.assertTrue(sub.exists())
        self.assertEqual(sub.state, 'cancel')
        txn = self.WalletTx.search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'refund')])
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)


class TestSecurityAccessUnlinkSettlesSubscription(TransactionCase):
    """#322: 刪 SecurityAccess 聯動取消訂閱＋按比例退點（wiring 驗證）。"""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Plan = self.env['sh.subscription.plan']
        self.WalletTx = self.env['wallet.transaction']
        self.plan = self.Plan.create({
            'name': 'SA Del Plan', 'sh_plan_price': 300.0, 'sh_duration': 1,
            'sh_unit': 'month', 'sh_no_of_billing_cycle': -1, 'sh_trial': False,
            'sa_max_routes': 3,
        })
        # SA fixture 慣例：用 product.product（非 product.template）
        self.env['product.product'].sudo().create({
            'name': 'SA Del Plan Product', 'type': 'service',
            'sh_product_subscribe': True, 'sh_subscription_plan_id': self.plan.id,
        })
        # journal 前置（_create_and_confirm_subscription 需要；鏡像既有 SA fixture）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id
        self.owner = self.env['res.users'].create({
            'name': 'SA Del Owner', 'login': 'sa_del_owner',
            'email': 'sa_del_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA Del WS', 'owner_id': self.owner.id,
        })
        self.sa = self.SA.create({
            'name': 'SA Del Gateway', 'workspace_id': self.workspace.id,
        })

    def test_unlink_cancels_and_refunds_paid_sub(self):
        partner = self.workspace.owner_id.partner_id
        self.sa._create_and_confirm_subscription(partner, self.plan)
        sub = self.sa.sudo().subscription_id
        self.assertEqual(sub.state, 'in_progress')

        self.sa.unlink()

        self.assertTrue(sub.exists())
        self.assertEqual(sub.state, 'cancel')
        txn = self.WalletTx.search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'refund')])
        self.assertEqual(len(txn), 1)
        self.assertEqual(txn.amount, 300.0)
        self.assertEqual(txn.subscription_id.id, sub.id)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `... --test-tags /woow_paas_platform:TestSmartHomeUnlinkSettlesSubscription,/woow_paas_platform:TestSecurityAccessUnlinkSettlesSubscription`
Expected: FAIL — `sub.state == 'in_progress'`（現況 SH/SA unlink 不結算）。

- [ ] **Step 3: 寫最小實作**

`src/models/smart_home.py` `unlink()`：`super().unlink()` 前捕獲、之後結算。

```python
    def unlink(self):
        """..."""
        # #322: 比照 CloudService（#318）— super().unlink() 前捕獲 in_progress
        # 訂閱，刪除成功後結算（取消＋按比例退點）。共用 _settle_on_entity_delete。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        for record in self:
            if record.tunnel_id:
                record._cleanup_tunnel()
            if record.tunnel_id:
                raise UserError(
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. The record was kept so the deletion can be "
                    "retried. Please try again shortly."
                )
        res = super().unlink()
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Smart Home deleted')
        return res
```

`src/models/security_access.py` `unlink()`：同法（保留既有 route 級聯與 fail-loud）。

```python
    def unlink(self):
        """..."""
        # #322: 比照 CloudService（#318）— 捕獲 in_progress 訂閱，刪除成功後結算。
        subs_to_settle = self.sudo().mapped('subscription_id').filtered(
            lambda s: s.state == 'in_progress'
        )
        for record in self:
            if record.tunnel_id:
                record._cleanup_cloudflare()
            if record.tunnel_id:
                raise UserError(
                    "Cloudflare cleanup failed; the tunnel is still live and "
                    "billed. The record was kept so the deletion can be "
                    "retried. Please try again shortly."
                )
        # 軸2: routes are freely deletable (no primary guard), so cascade-delete
        # them directly as part of tearing down the whole SA.
        self.route_ids.unlink()
        res = super().unlink()
        for sub in subs_to_settle:
            sub._settle_on_entity_delete(reason='Security Access deleted')
        return res
```

- [ ] **Step 4: 跑測試確認通過**

Run: `... --test-tags /woow_paas_platform:TestSmartHomeUnlinkSettlesSubscription,/woow_paas_platform:TestSecurityAccessUnlinkSettlesSubscription`
Expected: PASS（2 tests）

- [ ] **Step 5: 回歸既有 SH/SA 測試 + Commit**

Run: `... --test-tags /woow_paas_platform:TestSmartHomeSubscriptionFlow,/woow_paas_platform:TestSecurityAccessControllerFlow`（含既有刪除/生命週期）
Expected: PASS

```bash
git add src/models/smart_home.py src/models/security_access.py \
        src/tests/test_service_delete_subscription.py
git commit -m "feat: #322 SmartHome/SecurityAccess.unlink() 刪除聯動取消訂閱並按比例退點"
```

---

## Self-Review

**1. Spec coverage（對照 issue #318 待做 / 驗收）**
- 待做 1「刪除聯動：同交易內 cancel 訂閱＋按比例退點以 top-up 加值入 wallet、留審計軌跡」→ Task 2（結算）+ Task 3（接上 unlink，同交易原子）+ Task 1（refund 型交易＝可辨識審計軌跡、帶 subscription_id）。✅
- 待做 2「換 plan：cancel 舊→按比例退點→建新，原子」→ 換 plan 骨架已存在且原子（Odoo request 交易）；Task 4 補齊退點型別（issue「交易可辨識為退點類型」）。✅（換 plan 的取消／建新既有，未回歸由 Step 6 護欄）
- 待做 3「邊界：trial 退 0；比例基準＝plan 價×剩餘天數/週期天數；與訂閱規格 single source 對齊」→ 全走 `_compute_unused_refund()`（Global Constraints 鎖定），trial→0 有 Task 2/3 測試（`test_trial_*`）。✅
- 注意「與 #273/#274 取同一份訂閱規格，退點換算勿另立數字」→ 對齊 `_compute_unused_refund` SSOT，未新增任何公式。✅
- 注意「wallet 交易可辨識為退點型別」→ Task 1 `refund` 型 + Task 4 四入口正名。✅
- 驗收「刪服務後無 in_progress 訂閱殘留、wallet 收到按比例退點、cron 零扣款」→ Task 3 整合測試斷言 `sub.state=='cancel'`（離開 in_progress，計費 cron domain `state='in_progress'` 自然停手）+ refund 交易。✅
- 驗收「換 plan 後舊 cancel/新 in_progress/退點入帳同一原子」→ 既有換 plan + Task 4 退點型別；原子性由 Odoo 單一 request 交易保證（無 explicit commit）。✅（註：換 plan 關舊約用 `'close'`，與刪除的 `'cancel'` 為既有語意區分，非本 PR 改變）

**2. Placeholder scan**：無 TBD/TODO；每個 code step 均含完整可執行程式碼與斷言。✅

**3. Type consistency**：
- `refund_wallet_points(amount, description=None, subscription=None) -> bool` — Task 1 定義，Task 2/4 呼叫一致。✅
- `_settle_on_entity_delete(reason='Service deleted') -> float` — Task 2 定義，Task 3 呼叫一致。✅
- 退點型別字面值 `'refund'`、刪除終態 `'cancel'`、換 plan 關舊 `'close'` — 全 plan 一致。✅

**已決策點（含 review 二次擴充）**：
1. 刪除終態用 `'cancel'`（對齊 issue #318 文字）；使用者主動取消訂閱不退點/不拆服務的真不一致＝**#323**（產品決策，另 PR），本 PR 不動 `billing.py`、不改 `'cancel'`。
2. 刪除聯動落在 model `unlink()`（覆蓋所有刪除路徑，含 workspace cascade）vs controller `_delete_service`（僅覆蓋使用者主動刪）。本 plan 選 model 層並以「僅結算 in_progress」保證 cron cleanup 路徑不二次退點。
3. 換 plan 四入口退點型別本次一併正名（含 SH/SA controller）。
4. **（review 後擴充）SmartHome / SecurityAccess 刪除聯動＝#322，本 PR 一併接上（Task 6）**；三者共用 `_settle_on_entity_delete`，SH/SA 各加 wiring happy-path 測試，共用 method 邊界已由 CS 測試窮舉不重測。


<!-- peer-reviewed: 2026-07-09T00:08:52Z rounds=3 verdict=approved by=miniclaude -->
