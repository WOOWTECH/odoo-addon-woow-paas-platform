# Invoice Issue Fix — Path B (Auto-topup as wallet.topup + Upstream Hook Isolation) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修正訂閱 cycle invoice 重複開立 ECPay 發票、auto-topup 漏開發票兩個 bug，並讓 PaaS 開立流程與上游 `ecpay_invoice_tw` 的 inline hook 完全隔離（避免未來官方網站把 `company.auto_invoice` 改成 `automatic` 時破壞 PaaS）。

**設計依據：** 本 plan 是 [`docs/architecture/billing-philosophy.md`](../architecture/billing-philosophy.md) 5 大原則的具體實作。實作時若有任何取捨，以哲學文件為準。

**Architecture:**
- **訂閱 cycle invoice → `not_required`**：訂閱用 wallet 扣款不應再開 ECPay 發票（儲值時已開過）
- **Auto-topup 重構為 wallet.topup**：將 `_attempt_auto_topup` 改成創建 `woow.wallet.topup(source='auto')`，走與手動儲值對稱的流程，自然觸發 `_ensure_topup_invoice()` 開發票
- **Hook 防禦**：給 PaaS 創建的 invoice 加 `woow_skip_upstream_hook` flag，覆寫 `account.move.action_post()` 在 flag=True 時跳過上游 `ecpay_invoice_tw` 的 inline `create_ecpay_invoice()` 呼叫

**Tech Stack:** Python 3.11+ / Odoo 18 / pytest (Odoo TransactionCase) / 既有 ecpay_invoice_tw + sh_subscription 上游 addon

**Scope estimate:** 1.5-2 週（6 phases，~30 個 commits）

**Out of scope（明確排除）:**
- Multi-company 隔離（保留為未來選項，獨立 ticket 評估）
- 主動 auto-topup cron（已記錄在 maestri 「pending」筆記，按量計費規劃時再評估）
- ECPay 發票折讓 / 退費邏輯（無歷史資料需要補救，因尚未上線）

---

## 前置檢查

執行任何 task 前先確認：

```bash
# 1. 確認在正確的 worktree
pwd
# Expected: /Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--featrueecpg

# 2. 確認 git 乾淨
git status
# Expected: working tree clean

# 3. 確認在 featrue/ecpg branch（決策點 3：單一 branch + 單一 PR）
git rev-parse --abbrev-ref HEAD
# Expected: featrue/ecpg

# 4. 啟動 Docker 開發環境
./scripts/start-dev.sh
# Expected: Odoo running at http://localhost
```

**Commit 順序紀律（決策點 3 配套）：** 因為採用單一 PR，所有 commit 必須**嚴格按 Phase 順序排列**（Phase 1 → 2 → 3 → 4 → 5 → 6），避免穿插。Reviewer 可以一段一段看 commit 範圍，等同於「2 PR 的 review 體驗壓在 1 PR 內」。

---

## Phase 1：資料模型 — 新增欄位

### Task 1.1: 為 `woow.wallet.topup` 新增 `source` 欄位 + 讓 `plan_id` 可為空

**Files:**
- Modify: `src/models/wallet_topup.py:14-15`（plan_id 改為 optional）
- Modify: `src/models/wallet_topup.py:22-28`（新增 source 欄位接在 state 後）
- Test: `src/tests/test_wallet_topup_model.py`（新建）

**Step 1: 寫失敗測試**

新建 `src/tests/test_wallet_topup_model.py`：

```python
"""Tests for wallet.topup model schema changes (Path B)."""
from odoo.tests.common import TransactionCase


class TestWalletTopupModel(TransactionCase):

    def test_source_field_exists_with_manual_default(self):
        """wallet.topup must have source field defaulting to 'manual'."""
        topup = self.env['woow.wallet.topup']
        self.assertIn('source', topup._fields)
        field = topup._fields['source']
        self.assertEqual(field.default(topup), 'manual')
        valid_values = [v for v, _label in field.selection]
        self.assertIn('manual', valid_values)
        self.assertIn('auto', valid_values)

    def test_plan_id_is_optional_for_auto_source(self):
        """Auto topups don't have a fixed plan; plan_id must be nullable."""
        plan_field = self.env['woow.wallet.topup']._fields['plan_id']
        self.assertFalse(plan_field.required, "plan_id must be optional for auto-topup")

    def test_create_auto_topup_without_plan(self):
        """An auto-topup record can be created without a plan_id."""
        partner = self.env['res.partner'].create({'name': 'Test'})
        twd = self.env.ref('base.TWD')
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': partner.id,
            'amount': 50.0,
            'credits': 50.0,
            'currency_id': twd.id,
            'source': 'auto',
        })
        self.assertEqual(topup.source, 'auto')
        self.assertFalse(topup.plan_id)
```

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_model -v
```
Expected: FAIL with "source not in _fields" 或 plan_id required error

**Step 3: 修改 model**

`src/models/wallet_topup.py:14`：
```python
# Before
plan_id = fields.Many2one('woow_paas_platform.topup_plan', required=True, ondelete='restrict')

# After
plan_id = fields.Many2one('woow_paas_platform.topup_plan', ondelete='restrict')
```

`src/models/wallet_topup.py` 在 state 欄位之後新增：
```python
source = fields.Selection([
    ('manual', 'Manual (user-initiated)'),
    ('auto', 'Auto (system-triggered shortfall)'),
], default='manual', required=True, index=True,
   help='manual = user clicked topup; auto = system triggered to cover '
        'subscription invoice shortfall.')
```

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_model -v
```
Expected: 3 passed

**Step 5: Commit**

```bash
git add src/models/wallet_topup.py src/tests/test_wallet_topup_model.py
git commit -m "feat: add source field to wallet.topup and make plan_id optional"
```

---

### Task 1.2: 為 `account.move` 新增 `woow_skip_upstream_hook` flag

**Files:**
- Modify: `src/models/account_move_invoice_status.py`（既有 inherit account.move 的檔案）
- Test: `src/tests/test_account_move_skip_hook_field.py`（新建）

**Step 1: 寫失敗測試**

`src/tests/test_account_move_skip_hook_field.py`：

```python
"""Test the woow_skip_upstream_hook flag on account.move."""
from odoo.tests.common import TransactionCase


class TestAccountMoveSkipHook(TransactionCase):

    def test_field_exists_with_false_default(self):
        move = self.env['account.move']
        self.assertIn('woow_skip_upstream_hook', move._fields)
        # default False so existing flows unchanged
        partner = self.env['res.partner'].create({'name': 'X'})
        rec = move.create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
        })
        self.assertFalse(rec.woow_skip_upstream_hook)
```

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_account_move_skip_hook_field -v
```
Expected: FAIL with field missing

**Step 3: 新增欄位**

`src/models/account_move_invoice_status.py` 在 `woow_invoice_next_retry_at` 後新增：

```python
woow_skip_upstream_hook = fields.Boolean(
    default=False,
    copy=False,
    help='When True, account.move.action_post() bypasses the upstream '
         'ecpay_invoice_tw inline hook. PaaS-created invoices set this so '
         'our async ecpay_invoice_cron is the sole issuer. Has no effect '
         'when company.auto_invoice is "manual" (upstream hook is a no-op '
         'in that case anyway).',
)
```

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_account_move_skip_hook_field -v
```
Expected: PASS

**Step 5: Commit**

```bash
git add src/models/account_move_invoice_status.py src/tests/test_account_move_skip_hook_field.py
git commit -m "feat: add woow_skip_upstream_hook flag to account.move"
```

---

## Phase 2：覆寫 `create_ecpay_invoice` + cron context 後門（防禦未來 automatic 設定）

### Task 2.1: 覆寫 `create_ecpay_invoice` 在 flag=True 時 short-circuit

**設計選擇（決策點 1）：** 採用 inheritance + context 的方式，不使用 monkey-patch。理由：thread-safe、無 class-level 修改、符合 Odoo addon 慣例。

**Files:**
- Modify: `src/models/account_move.py`（既有 _pay_from_wallet 的檔案）
- Test: `src/tests/test_action_post_hook_isolation.py`（新建）

**Step 1: 寫失敗測試**

`src/tests/test_action_post_hook_isolation.py`：

```python
"""Verify woow_skip_upstream_hook + woow_force_issue context behave correctly."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestCreateEcpayInvoiceShortCircuit(TransactionCase):

    def setUp(self):
        super().setUp()
        # Force company into automatic mode so upstream hook would fire
        self.env.company.auto_invoice = 'automatic'
        self.partner = self.env['res.partner'].create({'name': 'Test'})

    def _make_invoice(self, skip_flag):
        return self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'woow_skip_upstream_hook': skip_flag,
            'invoice_line_ids': [(0, 0, {
                'name': 'Test',
                'quantity': 1,
                'price_unit': 100.0,
            })],
        })

    def test_flag_true_short_circuits_without_calling_super(self):
        """flag=True + no force ctx → return early, upstream not called."""
        inv = self._make_invoice(skip_flag=True)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.create_ecpay_invoice()
            upstream.assert_not_called()

    def test_flag_false_calls_upstream(self):
        """flag=False → super() runs normally."""
        inv = self._make_invoice(skip_flag=False)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.create_ecpay_invoice()
            upstream.assert_called()

    def test_force_issue_context_bypasses_skip(self):
        """flag=True + woow_force_issue=True → upstream called (cron's path)."""
        inv = self._make_invoice(skip_flag=True)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.with_context(woow_force_issue=True).create_ecpay_invoice()
            upstream.assert_called()
```

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_action_post_hook_isolation -v
```
Expected: FAIL — override 不存在

**Step 3: 覆寫 `create_ecpay_invoice`**

`src/models/account_move.py` 在 `_pay_from_wallet` 之前新增：

```python
def create_ecpay_invoice(self):
    """Short-circuit when invoice is PaaS-flagged and not forced.

    Why: upstream `ecpay_invoice_tw` calls `create_ecpay_invoice()` inline
    from its `action_post()` hook (when company.auto_invoice='automatic'),
    raising UserError on ECPay failure — which would roll back the entire
    `_pay_from_wallet` transaction (including the wallet deduction).

    PaaS-created invoices set `woow_skip_upstream_hook=True` so this
    method returns early when called from the upstream hook. Our async
    `ecpay_invoice_cron` opens the back door via
    `with_context(woow_force_issue=True)` so it can still issue the invoice.

    Thread-safe: pure inheritance + context (no class-level monkey-patch).
    """
    if (self.woow_skip_upstream_hook
            and not self.env.context.get('woow_force_issue')):
        _logger.debug(
            "[INV] create_ecpay_invoice short-circuit invoice=%s "
            "(skip flag set, no force ctx)",
            self.id,
        )
        return
    return super().create_ecpay_invoice()
```

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_action_post_hook_isolation -v
```
Expected: 3 passed

**Step 5: Commit**

```bash
git add src/models/account_move.py src/tests/test_action_post_hook_isolation.py
git commit -m "feat: short-circuit upstream ecpay hook for PaaS-flagged invoices"
```

---

### Task 2.2: `ecpay_invoice_cron` 用 `with_context(woow_force_issue=True)` 開立

**設計理由：** Task 2.1 的 short-circuit 會擋住 **所有** PaaS-flagged invoice，包含我們自己 cron 的呼叫。Cron 必須開「context 後門」才能正常開立發票。

**Files:**
- Modify: `src/models/ecpay_invoice_cron.py:78`（`_issue_one` 內的 `move.create_ecpay_invoice()`）
- Test: `src/tests/test_ecpay_invoice_cron_force_issue.py`（新建）

**Step 1: 寫失敗測試**

```python
"""Verify ecpay_invoice_cron can issue PaaS-flagged invoices via context bypass."""
from unittest.mock import patch
from odoo.tests.common import TransactionCase


class TestEcpayCronForceIssue(TransactionCase):

    def test_cron_issues_paas_flagged_invoice(self):
        """_issue_one must use woow_force_issue ctx so flagged invoices get issued."""
        partner = self.env['res.partner'].create({'name': 'Test'})
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'woow_skip_upstream_hook': True,
            'woow_invoice_issue_state': 'pending',
            'invoice_line_ids': [(0, 0, {
                'name': 'Test', 'quantity': 1, 'price_unit': 100.0,
            })],
        })
        inv.action_post()

        cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            cron._issue_one(inv)
            upstream.assert_called(), (
                "Cron must reach upstream create_ecpay_invoice; if not, the "
                "context bypass is missing"
            )
```

**Step 2: 執行測試確認失敗**

Expected: FAIL — cron 無 context flag，被 Task 2.1 的 short-circuit 擋掉

**Step 3: 修改 cron**

`src/models/ecpay_invoice_cron.py:78` 改為：

```python
# Before
move.create_ecpay_invoice()

# After
# 透過 woow_force_issue context bypass Task 2.1 的 short-circuit
# （PaaS-flagged invoice 只允許這支 cron 開立，杜絕上游 inline hook）
move.with_context(woow_force_issue=True).create_ecpay_invoice()
```

**Step 4: 執行測試確認通過**

Expected: PASS

**Step 5: Commit**

```bash
git add src/models/ecpay_invoice_cron.py src/tests/test_ecpay_invoice_cron_force_issue.py
git commit -m "feat: ecpay_invoice_cron uses force_issue ctx to bypass skip flag"
```

---

## Phase 3：訂閱 cycle invoice 改為 `not_required`

### Task 3.1: 改寫 `_get_or_create_cycle_invoice` 不要標 pending

**設計理由（對齊 billing-philosophy 原則 4）：** Cycle invoice **仍會建立並 post**，作為內部會計憑證記錄純 wallet 扣款的交易事件。我們**只**改變「是否送 ECPay」這一點。**未來不要因為「不開發票了那為什麼還要建 invoice？」而把 cycle invoice 一起拿掉** — Odoo invoice 是 wallet 扣款的會計憑證、與 sh_subscription 的對帳依據，不可省略。

**Files:**
- Modify: `src/models/sh_subscription_fix.py:140`
- Test: `src/tests/test_subscription_invoice_state.py`（新建）

**Step 1: 寫失敗測試**

`src/tests/test_subscription_invoice_state.py`：

```python
"""Subscription cycle invoices must be not_required + skip upstream hook."""
from odoo.tests.common import TransactionCase


class TestSubscriptionInvoiceState(TransactionCase):

    def test_cycle_invoice_marked_not_required(self):
        """New subscription cycle invoice must NOT be queued for ECPay issuance."""
        # Setup: minimal subscription
        partner = self.env['res.partner'].create({
            'name': 'Test', 'email': 'test@example.com',
        })
        # Use existing test plan/template fixtures from the repo if available
        # … (test scaffold to be filled with the same fixture pattern as
        # test_low_balance_notification.py setUp)

        # Trigger invoice creation via _get_or_create_cycle_invoice
        # Assert:
        # - woow_invoice_issue_state == 'not_required'
        # - woow_skip_upstream_hook == True
```

(完整 fixture 細節依 repo 既有測試 setUp 模式，於實作時補完)

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_subscription_invoice_state -v
```
Expected: FAIL — invoice 仍標 pending

**Step 3: 修改 invoice 創建邏輯**

`src/models/sh_subscription_fix.py:140` 改為：

```python
# Before
invoice_vals['woow_invoice_issue_state'] = 'pending'

# After
# Cycle invoice 由 wallet 扣款結算（儲值時已開過 ECPay 發票），不重複開
invoice_vals['woow_invoice_issue_state'] = 'not_required'
invoice_vals['woow_skip_upstream_hook'] = True
```

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_subscription_invoice_state -v
```
Expected: PASS

**Step 5: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_subscription_invoice_state.py
git commit -m "fix: subscription cycle invoice should not request ECPay e-invoice"
```

---

## Phase 4：Auto-topup 重構（Path B 核心）

### Task 4.1: 在 `wallet.topup` model 新增 `create_auto_topup` class method

**Files:**
- Modify: `src/models/wallet_topup.py`（在 class body 加入新方法）
- Test: `src/tests/test_wallet_topup_auto.py`（新建）

**Step 1: 寫失敗測試**

```python
"""Tests for wallet.topup.create_auto_topup factory method."""
from odoo.tests.common import TransactionCase


class TestCreateAutoTopup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test', 'email': 'test@example.com',
        })

    def test_create_auto_topup_returns_topup_record(self):
        """create_auto_topup creates a draft topup with source=auto."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.source, 'auto')
        self.assertEqual(topup.state, 'draft')
        self.assertEqual(topup.amount, 50.0)
        self.assertEqual(topup.credits, 50.0)
        self.assertEqual(topup.bonus, 0.0)
        self.assertFalse(topup.plan_id, "auto-topup must NOT pin a plan")
        self.assertEqual(topup.partner_id, self.partner)

    def test_create_auto_topup_inherits_partner_invoice_carrier(self):
        """The new topup carries the partner's invoice preference."""
        self.partner.write({
            'woow_invoice_carrier_type': 'mobile',
            'woow_invoice_carrier': '/ABC1234',
        })
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.invoice_carrier_type, 'mobile')
        self.assertEqual(topup.invoice_carrier, '/ABC1234')

    def test_create_auto_topup_zero_amount_raises(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=0,
            )
```

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_auto -v
```
Expected: FAIL — method doesn't exist

**Step 3: 實作 `create_auto_topup`**

在 `src/models/wallet_topup.py` 的 `WalletTopup` class 內新增：

```python
@api.model
def create_auto_topup(self, partner, amount):
    """Factory: create a draft auto-topup for the given shortfall.

    Returns: woow.wallet.topup record (state=draft, source=auto).
    Raises: ValidationError if amount <= 0.
    """
    from odoo.exceptions import ValidationError
    if amount <= 0:
        raise ValidationError("Auto-topup amount must be positive (got %s)" % amount)

    twd = self.env.ref('base.TWD', raise_if_not_found=True)
    vals = {
        'partner_id': partner.id,
        'amount': float(amount),
        'credits': float(amount),
        'bonus': 0.0,
        'currency_id': twd.id,
        'source': 'auto',
        'state': 'draft',
    }
    # Inherit partner's invoice preference (mirrors manual topup flow)
    if hasattr(partner, '_woow_partner_invoice_vals'):
        prefs = partner._woow_partner_invoice_vals()
        for key in ('invoice_carrier_type', 'invoice_carrier', 'invoice_tax_id',
                    'invoice_title', 'invoice_lovecode'):
            if key in prefs:
                vals[key] = prefs[key]
    return self.sudo().create(vals)
```

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_auto -v
```
Expected: 3 passed

**Step 5: Commit**

```bash
git add src/models/wallet_topup.py src/tests/test_wallet_topup_auto.py
git commit -m "feat: add wallet.topup.create_auto_topup factory method"
```

---

### Task 4.2: 新增 `wallet.topup.charge_via_token` 執行扣款 → 觸發 `_on_payment_done`

**Files:**
- Modify: `src/models/wallet_topup.py`
- Test: `src/tests/test_wallet_topup_auto.py`（追加 test）

**Step 1: 寫失敗測試**

在 `test_wallet_topup_auto.py` 追加（用 mock 模擬 ECPay）：

```python
def test_charge_via_token_credits_wallet_and_creates_invoice(self):
    """Successful charge: wallet credited + invoice created with skip flag."""
    from unittest.mock import patch, MagicMock
    provider, token = self._setup_ecpg_token('AB12345678')  # helper from setUp pattern
    topup = self.env['woow.wallet.topup'].create_auto_topup(
        partner=self.partner, amount=50.0,
    )

    fake_tx = MagicMock(state='done', id=999, reference='TOPUPXX')
    with patch.object(
        self.env['payment.transaction'].__class__, 'create',
        return_value=fake_tx,
    ), patch.object(
        type(fake_tx), '_send_payment_request', return_value={},
    ):
        ok, msg = topup.charge_via_token(token)

    self.assertTrue(ok, f"charge should succeed: {msg}")
    self.assertEqual(topup.state, 'done')
    self.assertEqual(self.partner.wallet_balance, 150.0)  # 100 + 50
    self.assertTrue(topup.invoice_account_move_id)
    self.assertTrue(topup.invoice_account_move_id.woow_skip_upstream_hook)
    self.assertEqual(topup.invoice_account_move_id.woow_invoice_issue_state, 'pending')

def test_charge_via_token_returns_3ds_required(self):
    """When ECPG returns redirect_url, we abort and stay in pending."""
    # … (mirror logic from existing test_attempt_auto_topup_returns_3ds_required)

def test_charge_via_token_returns_failure_state(self):
    """When tx.state != done, we mark topup as failed."""
    # …
```

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_auto -v
```
Expected: FAIL — method doesn't exist

**Step 3: 實作 `charge_via_token`**

複製現有 `_attempt_auto_topup` 主要邏輯到 wallet.topup model 上，但走自身的 state machine：

```python
import math
import uuid

# Add to top of file:
TOPUP_OK = 'ok'
TOPUP_NO_TOKEN = 'no_token'
TOPUP_PROVIDER_NOT_ENABLED = 'provider_not_enabled'
TOPUP_CURRENCY_NOT_CONFIGURED = 'currency_not_configured'
TOPUP_3DS_REQUIRED = '3ds_required'
TOPUP_SDK_ERROR = 'sdk_error'
TOPUP_WALLET_CREDIT_FAILED = 'wallet_credit_failed'


def charge_via_token(self, token):
    """Synchronously charge `token` for self.amount, credit wallet, create invoice.

    Returns (ok: bool, message: str). Side effects:
      - state transitions: draft → pending → done/failed
      - on done: wallet credited, invoice created (woow_skip_upstream_hook=True)
      - on 3DS required: state stays 'pending', returns (False, '3ds_required')
    """
    self.ensure_one()
    if not token or not token.active or not token.ecpg_bindcard_id:
        return False, TOPUP_NO_TOKEN

    provider = token.provider_id
    if not provider or not getattr(provider, 'enable_ecpg', False):
        return False, TOPUP_PROVIDER_NOT_ENABLED

    currency = provider.main_currency_id or self.env.ref('base.TWD', raise_if_not_found=False)
    if not currency:
        return False, TOPUP_CURRENCY_NOT_CONFIGURED

    self.write({'state': 'pending'})
    charge_amount = int(math.ceil(self.amount))
    reference = f'TOPUP{uuid.uuid4().hex[:15].upper()}'

    tx = self.env['payment.transaction'].sudo().create({
        'provider_id': provider.id,
        'payment_method_id': token.payment_method_id.id,
        'reference': reference,
        'amount': float(charge_amount),
        'currency_id': currency.id,
        'partner_id': self.partner_id.id,
        'token_id': token.id,
        'operation': 'offline',
    })
    self.write({'payment_transaction_id': tx.id})

    try:
        result = tx._send_payment_request()
    except Exception:
        _logger.exception('[TOPUP_AUTO] send_payment_request failed topup=%s', self.id)
        self.write({'state': 'failed', 'failure_code': TOPUP_SDK_ERROR})
        return False, TOPUP_SDK_ERROR

    if isinstance(result, dict) and result.get('redirect_url'):
        # 3DS — keep state pending, don't fail (user may complete via web)
        return False, TOPUP_3DS_REQUIRED

    if tx.state != 'done':
        self.write({'state': 'failed', 'failure_code': tx.state})
        return False, tx.state

    # Run the existing happy-path: credit wallet + create invoice (skip flag set)
    try:
        self._on_payment_done(tx)
    except Exception:
        _logger.exception(
            'CRITICAL: auto-topup charged ECPay (tx=%s amount=%.2f) but '
            'post-charge bookkeeping failed; manual reconciliation required '
            '(topup=%s, partner=%s)',
            tx.reference, charge_amount, self.id, self.partner_id.id,
        )
        return False, TOPUP_WALLET_CREDIT_FAILED

    return True, TOPUP_OK
```

⚠️ **同時修改 `_create_topup_invoice` 在創建 invoice 時設定 `woow_skip_upstream_hook=True`**（行 236-251 區段）。

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_auto -v
```
Expected: 6 passed

**Step 5: Commit**

```bash
git add src/models/wallet_topup.py src/tests/test_wallet_topup_auto.py
git commit -m "feat: add wallet.topup.charge_via_token for auto-topup flow"
```

---

### Task 4.3: 把 `_attempt_auto_topup` 變 thin wrapper（deprecation shim）

**設計選擇（決策點 2）：** 採用 deprecation shim 漸進式重構，**保留 `_attempt_auto_topup` 為 thin wrapper** delegate 到新的 `wallet.topup.charge_via_token`。理由：

- 既有 27 個測試的呼叫點與回傳語意完全不變 → **多數測試自動 pass，不需大量適配**
- 短期 technical debt 換來零 regression 風險
- 未來 follow-up ticket 統一砍 wrapper（已記錄在 plan 末段 Out of scope）

**Files:**
- Modify: `src/models/sh_subscription_fix.py:386-545`（`_attempt_auto_topup` 整段改為 wrapper）
- Test: `src/tests/test_low_balance_notification.py`（**原則上不動，必須繼續 pass**）

**Step 1: 重寫 `_attempt_auto_topup` 為 wrapper**

`src/models/sh_subscription_fix.py:386-545` 整段刪除，改為：

```python
@api.model
def _attempt_auto_topup(self, partner, shortfall):
    """[DEPRECATED] Thin wrapper around wallet.topup.charge_via_token.

    Kept for backwards compatibility with existing tests and the existing
    `_try_settle_invoice` call site. New code should call wallet.topup
    directly.

    Returns (ok, msg) — same contract as before. The actual ECPay charge,
    wallet credit, and invoice creation are delegated to wallet.topup.

    TODO: remove after one stable release once all callers migrated.
    Tracked in: docs/plans/2026-05-03-invoice-issue-fix-path-b.md → Out of scope
    """
    if shortfall <= 0:
        return False, 'no_shortfall'

    token = self._resolve_default_token(partner)
    if not token:
        return False, 'no_token'

    topup = self.env['woow.wallet.topup'].sudo().create_auto_topup(
        partner=partner, amount=shortfall,
    )
    return topup.charge_via_token(token)


def _resolve_default_token(self, partner):
    """Extract default-token resolution logic.

    Phase 2c known limitation: community ed has no property_payment_token_id
    so we fall back to the most recently created active ECPG-bound token.
    """
    Token = self.env['payment.token'].sudo()
    token = Token
    if 'property_payment_token_id' in partner._fields:
        token = partner.with_company(
            partner.company_id or self.env.company,
        ).sudo().property_payment_token_id
    if not token or not token.active or not token.ecpg_bindcard_id:
        token = Token.search(
            [
                ('partner_id', '=', partner.id),
                ('active', '=', True),
                ('ecpg_bindcard_id', '!=', False),
            ],
            order='id desc', limit=1,
        )
    return token if token and token.active and token.ecpg_bindcard_id else None
```

**Step 2: 跑既有 27 個測試確認 pass**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_low_balance_notification -v
```
Expected: **絕大多數 pass**（shim 保留同樣 (ok, msg) 回傳語意 + 副作用）

**注意**：少數測試可能因 mock 的精準位置改變需要微調（例如原本 mock `payment.transaction.create`，現在實際呼叫鏈是 `wallet.topup.create_auto_topup → charge_via_token → payment.transaction.create`，mock 點仍可生效，但 assertion 可能要對應微調）。

**判斷標準**：如果 fail > 5 個 → 停下來檢視 `charge_via_token` 的 mock-friendliness 設計；如果 fail ≤ 5 個 → 視為合理適配 cost，逐個微調。

**Step 3: 跑全部 PaaS 測試確認 cross-test 沒有 regression**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform -v
```
Expected: 全部 pass

**Step 4: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_low_balance_notification.py
git commit -m "refactor: _attempt_auto_topup becomes thin wrapper around wallet.topup"
```

---

## Phase 5：手動 topup 路徑也加上 hook isolation flag

### Task 5.1: `_create_topup_invoice` 在 invoice 上設 `woow_skip_upstream_hook=True`

**Files:**
- Modify: `src/models/wallet_topup.py:230-262`（`_create_topup_invoice`）
- Test: `src/tests/test_wallet_topup_invoice.py`（新建）

**Step 1: 寫失敗測試**

```python
"""Manual topup invoices must also bypass the upstream hook."""
from odoo.tests.common import TransactionCase


class TestManualTopupInvoiceSkipsHook(TransactionCase):

    def test_manual_topup_invoice_has_skip_flag(self):
        # ... create a manual topup, trigger _create_topup_invoice
        # assert: invoice.woow_skip_upstream_hook == True
```

**Step 2: 執行測試確認失敗**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_invoice -v
```
Expected: FAIL

**Step 3: 修改 `_create_topup_invoice` move_vals**

`src/models/wallet_topup.py:236-249`，在 move_vals 加入：

```python
move_vals = {
    'move_type': 'out_invoice',
    # … existing fields …
    'woow_invoice_issue_state': 'pending',
    'woow_skip_upstream_hook': True,  # ← 新增
}
```

**Step 4: 執行測試確認通過**

```bash
./scripts/test-addon.sh -- --test-tags woow_paas_platform.test_wallet_topup_invoice -v
```
Expected: PASS

**Step 5: Commit**

```bash
git add src/models/wallet_topup.py src/tests/test_wallet_topup_invoice.py
git commit -m "feat: manual topup invoices also set skip_upstream_hook flag"
```

---

## Phase 6：文件更新

### Task 6.1: 更新 deployment 文件

**Files:**
- Modify: `docs/deployment/admin-roles.md`（或新建 `docs/deployment/invoice-issuing.md`）

**Step 1: 撰寫文件章節**

新增章節「ECPay E-Invoice 開立模式」，內容包含：

- PaaS 假設 `company.auto_invoice='manual'`（上游預設）
- PaaS 創建的所有 invoice 透過 `woow_skip_upstream_hook=True` 與上游 inline hook 隔離
- 即使官方網站把 `auto_invoice` 改為 `automatic`，PaaS topup invoice 仍會由 `ecpay_invoice_cron` 異步開立，不會被上游 hook 同步攔截
- 訂閱 cycle invoice 標 `not_required`，不送 ECPay（儲值時已開過）
- Auto-topup 走與手動儲值對稱的 `wallet.topup(source='auto')` 流程

**Step 2: Commit**

```bash
git add docs/deployment/invoice-issuing.md
git commit -m "docs: document ECPay invoice issuing isolation strategy"
```

---

### Task 6.2: 更新 `meeting-goal-progress` 筆記（標記 P10/P12 已重構）

**Step 1: 透過 maestri CLI 更新筆記**

```bash
maestri note edit "meeting-goal-progress" \
  "<具體要更新的段落>" \
  "<更新後的段落>"
```

新增 changelog entry：
```
| 2026-05-XX | Path B 重構：訂閱 cycle invoice 改 not_required；auto-topup 改走 wallet.topup(source='auto')；新增 woow_skip_upstream_hook 隔離官方網站 |
```

---

## Phase 7：整合驗證（手動，部署到 stg）

### Task 7.1: 部署到 staging

```bash
/woow:deploy-stg
```

### Task 7.2: 在 stg 跑端對端流程

驗證以下情境（對應 billing-philosophy 對照表）：

1. **手動儲值 100 → 訂閱 cycle 80** — 驗證原則 1 + 3
   - Topup invoice：開出 100 元 ECPay 發票（信用卡實付 → 對應發票）
   - Cycle invoice：建立且 `not_required`，不送 ECPay（純點數扣抵 → 不重複開）
   - Wallet balance：100 → 20

2. **餘額 30 → 訂閱 cycle 80（觸發 auto-topup）** — 驗證原則 2 + 3
   - Auto wallet.topup（amount=50）：開出 50 元 ECPay 發票（auto-topup 補差額也是信用卡交易）
   - Cycle invoice：建立且 `not_required`，不開
   - Wallet balance：30 → 80（auto-topup +50） → 0（cycle 扣款 80）
   - **重點驗證**：發票總額 100+50=150 = 用戶實付現金 150 ✅（不再多開 100、漏開 50）

3. **手動把 `company.auto_invoice` 改為 `automatic` → 重複情境 1** — 驗證原則 5
   - 預期行為與 manual 模式完全一致（PaaS topup 不受影響）
   - 用 `kubectl logs` 確認 `ecpay_invoice_tw` 的 inline hook **沒有** 對 PaaS invoice 觸發（log 找不到 `create_ecpay_invoice` 的 inline 呼叫，只有 cron 呼叫）
   - 確認官方網站 invoice（無 `woow_skip_upstream_hook` flag）仍會被上游 hook 處理

### Task 7.3: 整理驗證結果到 PR description，準備 merge

---

## 風險與注意事項

| 風險 | 緩解 |
|------|------|
| 資料模型改動（`plan_id` 由 required 變 nullable）影響既有 wallet.topup 資料 | 既有資料 plan_id 都有值，不影響；新加的 `source` 預設 'manual' 也安全 |
| `_attempt_auto_topup` shim 形成 technical debt | 在 Out of scope 列出 follow-up ticket；shim 內部清楚標 [DEPRECATED]，docstring 指向本 plan 末段 |
| 上游 `ecpay_invoice_tw` 升版改 `create_ecpay_invoice` 簽名 | override 用 `super()` 依賴穩定的 Odoo override 慣例；Phase 7.2 stg 情境 3 跑 automatic 模式可即時發現 |
| 上游 hook 改名（不再呼叫 `create_ecpay_invoice`） | Task 2.1 的 short-circuit 失效但不會破壞功能（cron 仍能透過 force_issue ctx 開立）；stg 驗證情境 3 會抓到 |
| Phase 4.3 適配工作量超出 5 個測試 | 視為設計訊號，停下來檢視 `charge_via_token` 是否與測試 mock 模式不友善，調整 API 設計再繼續 |

---

## 驗證 checklist（PR review 時用）

**程式品質**
- [ ] 所有新增測試 pass
- [ ] 既有 27 個 mock test 仍 pass（透過 shim wrapper，可能有 ≤5 個微調）
- [ ] `git log` commit 順序與 phase 順序對齊（Phase 1 → 2 → 3 → 4 → 5 → 6），方便 reviewer 一段一段看
- [ ] 每個 commit 對應一個 task 且 message 清楚
- [ ] `_attempt_auto_topup` shim 維持原本回傳語意 `(ok, msg)`，且 docstring 標 [DEPRECATED]
- [ ] Auto-topup 失敗時的所有 return code 都被 wallet.topup 內部覆蓋（包含 `wallet_credit_failed` 的 critical log）
- [ ] `ecpay_invoice_cron._issue_one` 用 `with_context(woow_force_issue=True)` 呼叫

**整合驗證**
- [ ] stg 整合驗證的 3 個情境全部通過
- [ ] 文件已說明「PaaS 透過 `woow_skip_upstream_hook` 與上游隔離」

**對齊 billing-philosophy.md 5 大原則**
- [ ] 原則 1 — 點數仍是訂閱主要支付手段（wallet 為先邏輯未動）
- [ ] 原則 2 — 信用卡只在儲值 / auto-topup 補差額兩處動用（無第三條路徑）
- [ ] 原則 3 — 純點數扣抵（cycle invoice）不送 ECPay；信用卡交易（topup / auto-topup）必開發票
- [ ] 原則 4 — Cycle invoice 仍建立並 post 作為 Odoo 內部會計憑證，僅 `woow_invoice_issue_state='not_required'`（**沒有**省略 invoice 創建）
- [ ] 原則 5 — `woow_skip_upstream_hook` 機制涵蓋所有 PaaS 創建的 invoice（cycle + manual topup + auto-topup）

---

## Out of scope reminder

不要在這個 PR 中順手做以下事情，留給未來 ticket：

- ❌ 不要改 `company.auto_invoice` 的預設值或加 readonly UI
- ❌ 不要實作主動 auto-topup cron（按量計費規劃時再做，已記錄在 maestri 「pending」筆記）
- ❌ 不要動 multi-company 隔離
- ❌ 不要動 default token 設定 UI（Phase 2c 已知 limitation）
- ❌ 不要加按量計費相關欄位

## Follow-up tickets（這個 PR ship 之後再開）

- 🔧 **砍 `_attempt_auto_topup` shim**：本 PR 為了零 regression 保留 deprecation wrapper（Task 4.3）。一個 stable release 後，把 `_try_settle_invoice` 等所有呼叫點改為直接用 `wallet.topup.charge_via_token`，然後刪掉 wrapper + `_resolve_default_token`。預估 0.5 天。
