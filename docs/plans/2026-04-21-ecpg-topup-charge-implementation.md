# ECPG Top-up Charge Flow — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire `/api/billing/wallet/topup` to real ECPG token charges so users buying a top-up plan trigger an actual credit-card transaction and the wallet is credited atomically after ECPay's callback.

**Architecture:** Phase 1 adds a new `woow.wallet.topup` order model, extends `payment.transaction` with a back-reference, provides a reusable `payment.token._charge()` helper, and hooks `_set_done`/`_set_error` so the single ECPG callback handler auto-credits the wallet. Frontend redirects to ECPay's mandatory 3DS page and reads a new status endpoint on return.

**Tech Stack:** Odoo 18 (Python 3.11), OWL 2.0, `payment_ecpay_ecpg` SDK, `sh_subscription_wallet` (reused, not modified), ECPay stage for tests.

**Design reference:** `docs/plans/2026-04-21-ecpg-topup-charge-design.md`

---

## Conventions

- **TDD:** every task starts with a failing test, then minimal code, then green, then commit.
- **Commit messages:** use conventional-commits per CLAUDE.md (`feat:`, `fix:`, `test:`, `refactor:`, `chore:`, `docs:`).
- **Run tests with:** `./scripts/test-addon.sh` (runs inside Docker with Odoo test framework) unless stated otherwise. First test run of the day starts Docker; expect ~30s warm-up.
- **Module reload on .py change:** tests auto-reload. View (`.xml`) / manifest changes require `-u woow_paas_platform`.
- **No commit without green:** if a step fails, fix and re-run — do not commit yellow/red.
- **No amend:** each step is its own commit.
- **File-path convention:** absolute paths from repo root (`src/...` means `woow_paas_platform/src/...`).

---

# Phase 1 — Backend (Tasks 1 – 9)

## Task 1: Scaffold `woow.wallet.topup` model (empty shell)

**Why first:** every later test references `env['woow.wallet.topup']`, so the model must register before anything else.

**Files:**
- Create: `src/models/wallet_topup.py`
- Modify: `src/models/__init__.py`
- Modify: `src/security/ir.model.access.csv`
- Create: `src/tests/__init__.py` (if missing)
- Create: `src/tests/test_wallet_topup_model.py`
- Modify: `src/__manifest__.py` (no file list change, just sanity check that `security/ir.model.access.csv` is already in `data`)

### Step 1: Write the failing test

**File:** `src/tests/test_wallet_topup_model.py`

```python
from odoo.tests.common import TransactionCase


class TestWalletTopupModel(TransactionCase):
    def test_model_exists_and_has_required_fields(self):
        model = self.env['woow.wallet.topup']
        fields = model._fields
        for name in (
            'partner_id', 'plan_id', 'amount', 'credits', 'bonus',
            'currency_id', 'state', 'payment_transaction_id',
            'wallet_transaction_id', 'failure_code', 'failure_message',
            'invoice_carrier_type', 'invoice_carrier', 'invoice_tax_id',
            'invoice_title', 'invoice_lovecode',
            'invoice_account_move_id', 'invoice_issued_at',
        ):
            self.assertIn(name, fields, f"missing field {name}")

    def test_state_selection_values(self):
        model = self.env['woow.wallet.topup']
        selection = dict(model._fields['state'].selection)
        self.assertEqual(
            set(selection.keys()),
            {'draft', 'pending', 'done', 'failed', 'cancelled'},
        )
```

### Step 2: Run the test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_wallet_topup_model
```
Expected: `KeyError: 'woow.wallet.topup'`.

### Step 3: Minimal implementation

**File:** `src/models/wallet_topup.py`

```python
from odoo import api, fields, models


class WalletTopup(models.Model):
    _name = 'woow.wallet.topup'
    _description = 'Wallet Top-up Order'
    _order = 'create_date desc, id desc'

    partner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='restrict')
    plan_id = fields.Many2one('woow_paas_platform.topup_plan', required=True, ondelete='restrict')

    amount = fields.Monetary(required=True)
    credits = fields.Integer(required=True)
    bonus = fields.Integer(default=0)
    currency_id = fields.Many2one('res.currency', required=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, index=True)

    payment_transaction_id = fields.Many2one('payment.transaction', ondelete='restrict', index=True)
    wallet_transaction_id = fields.Many2one('wallet.transaction', readonly=True)

    failure_code = fields.Char()
    failure_message = fields.Char()

    invoice_carrier_type = fields.Selection([
        ('email', 'Email carrier'),
        ('mobile', 'Mobile barcode'),
        ('citizen', 'Citizen digital cert'),
        ('triplicate', 'Triplicate (B2B)'),
    ], default='email')
    invoice_carrier = fields.Char()
    invoice_tax_id = fields.Char()
    invoice_title = fields.Char()
    invoice_lovecode = fields.Char()
    invoice_account_move_id = fields.Many2one('account.move', readonly=True)
    invoice_issued_at = fields.Datetime(readonly=True)
```

**File:** `src/models/__init__.py` — add line

```python
from . import wallet_topup
```

**File:** `src/security/ir.model.access.csv` — append

```csv
access_woow_wallet_topup_user,woow.wallet.topup.user,model_woow_wallet_topup,base.group_user,1,1,1,0
access_woow_wallet_topup_manager,woow.wallet.topup.manager,model_woow_wallet_topup,base.group_system,1,1,1,1
```

**File:** `src/tests/__init__.py` — create if absent, add

```python
from . import test_wallet_topup_model
```

### Step 4: Run the test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_wallet_topup_model
```
Expected: `2 passed`.

### Step 5: Commit

```bash
git add src/models/wallet_topup.py src/models/__init__.py \
        src/security/ir.model.access.csv src/tests/__init__.py \
        src/tests/test_wallet_topup_model.py
git commit -m "feat(billing): add woow.wallet.topup model"
```

---

## Task 2: Add `payment.transaction.woow_topup_id` field

**Files:**
- Create: `src/models/payment_transaction.py`
- Modify: `src/models/__init__.py`
- Create: `src/tests/test_payment_transaction_fields.py`
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_payment_transaction_fields.py`

```python
from odoo.tests.common import TransactionCase


class TestPaymentTransactionFields(TransactionCase):
    def test_woow_topup_id_field_registered(self):
        fields = self.env['payment.transaction']._fields
        self.assertIn('woow_topup_id', fields)
        self.assertEqual(fields['woow_topup_id'].comodel_name, 'woow.wallet.topup')
```

Add to `src/tests/__init__.py`:

```python
from . import test_payment_transaction_fields
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_payment_transaction_fields
```
Expected: `AssertionError: 'woow_topup_id' not found`.

### Step 3: Minimal implementation

**File:** `src/models/payment_transaction.py`

```python
from odoo import fields, models


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    woow_topup_id = fields.Many2one(
        'woow.wallet.topup',
        ondelete='set null',
        index=True,
        help="When set, this transaction is a wallet top-up. "
             "_set_done/_set_error will drive the linked topup state.",
    )
```

**File:** `src/models/__init__.py` — add

```python
from . import payment_transaction
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_payment_transaction_fields
```
Expected: pass.

### Step 5: Commit

```bash
git add src/models/payment_transaction.py src/models/__init__.py \
        src/tests/test_payment_transaction_fields.py src/tests/__init__.py
git commit -m "feat(billing): link payment.transaction to woow.wallet.topup"
```

---

## Task 3: `payment.token._charge()` helper

**Files:**
- Create: `src/models/payment_token.py`
- Modify: `src/models/__init__.py`
- Create: `src/tests/test_payment_token_charge.py`
- Modify: `src/tests/__init__.py`

**Pre-reading:**
- `extra/extra-addons/payment_ecpay_ecpg/models/payment_transaction.py:39-103` — `_send_payment_request` return value format and state transitions.
- If absent in this repo, check sibling `/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons/payment_ecpay_ecpg/models/payment_transaction.py:39-103`.

### Step 1: Write the failing test

**File:** `src/tests/test_payment_token_charge.py`

```python
from unittest.mock import patch, MagicMock
from odoo.tests.common import TransactionCase


class TestPaymentTokenCharge(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'CA'})
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.provider.write({'state': 'test'})
        self.token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_details': '**** 4242',
            'ecpg_bindcard_id': 'STUB_BIND_ID',
            'ecpg_card_expire': '2812',
        })

    def test_charge_returns_redirect_when_3ds(self):
        """Helper returns redirect_url when ECPG flow requires 3DS."""
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request',
            return_value={'redirect_url': 'https://ecpay.example/3ds'},
        ):
            result = self.token._charge(
                amount=100,
                reference='TEST1',
                currency_id=self.env.company.currency_id.id,
            )
        self.assertTrue(result['tx'])
        self.assertEqual(result['redirect_url'], 'https://ecpay.example/3ds')
        self.assertIn(result['state'], ('pending', 'draft'))

    def test_charge_wires_purpose_field(self):
        """Caller can attach a correlation object (e.g. a topup order)."""
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self._ensure_stub_plan().id,
            'amount': 100,
            'credits': 100,
            'currency_id': self.env.company.currency_id.id,
        })
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request',
            return_value=None,
        ):
            result = self.token._charge(
                amount=100,
                reference='TEST2',
                currency_id=self.env.company.currency_id.id,
                purpose_field='woow_topup_id',
                purpose_id=topup.id,
            )
        self.assertEqual(result['tx'].woow_topup_id, topup)

    def _ensure_stub_plan(self):
        Plan = self.env['woow_paas_platform.topup_plan']
        existing = Plan.search([('name', '=', 'Stub')], limit=1)
        if existing:
            return existing
        return Plan.create({
            'name': 'Stub', 'amount': 100, 'credits': 100, 'active': True,
        })
```

Add to `src/tests/__init__.py`:

```python
from . import test_payment_token_charge
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_payment_token_charge
```
Expected: `AttributeError: _charge` (method missing).

### Step 3: Minimal implementation

**File:** `src/models/payment_token.py`

```python
from odoo import api, fields, models


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    def _charge(self, amount, reference, currency_id=None,
                purpose_field=None, purpose_id=None):
        """Charge this payment token and return the transaction + redirect info.

        For ECPG tokens this triggers `create_payment_with_card_id`, which
        currently always produces a 3DS challenge. Callers must redirect the
        browser to ``redirect_url`` when truthy.

        Args:
            amount: charge amount
            reference: unique string for payment.transaction.reference
            currency_id: override; defaults to current company currency
            purpose_field: Many2one field on payment.transaction that points
                to the domain object this charge fulfills (e.g. 'woow_topup_id')
            purpose_id: id for that Many2one

        Returns:
            dict with keys:
                tx          -- payment.transaction recordset
                redirect_url -- str | None
                state        -- 'pending' | 'done' | 'error' | 'draft'
        """
        self.ensure_one()
        Tx = self.env['payment.transaction'].sudo()
        currency_id = currency_id or self.env.company.currency_id.id

        vals = {
            'provider_id': self.provider_id.id,
            'partner_id': self.partner_id.id,
            'amount': amount,
            'currency_id': currency_id,
            'reference': reference,
            'token_id': self.id,
            'operation': 'online_token',
        }
        if purpose_field and purpose_id and purpose_field in Tx._fields:
            vals[purpose_field] = purpose_id

        tx = Tx.create(vals)
        send_result = tx._send_payment_request()

        redirect_url = None
        if isinstance(send_result, dict):
            redirect_url = send_result.get('redirect_url')

        return {'tx': tx, 'redirect_url': redirect_url, 'state': tx.state}
```

**File:** `src/models/__init__.py` — add

```python
from . import payment_token
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_payment_token_charge
```
Expected: `2 passed`.

### Step 5: Commit

```bash
git add src/models/payment_token.py src/models/__init__.py \
        src/tests/test_payment_token_charge.py src/tests/__init__.py
git commit -m "feat(billing): add payment.token._charge helper"
```

---

## Task 4: `_classify_failure` helper on `woow.wallet.topup`

**Files:**
- Modify: `src/models/wallet_topup.py`
- Create: `src/tests/test_wallet_topup_classify_failure.py`
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_wallet_topup_classify_failure.py`

```python
from odoo.tests.common import TransactionCase


class TestClassifyFailure(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Topup = self.env['woow.wallet.topup']

    def test_classify_card_declined(self):
        self.assertEqual(
            self.Topup._classify_failure('Card declined by issuing bank'),
            'card_declined',
        )

    def test_classify_limit_exceeded(self):
        self.assertEqual(
            self.Topup._classify_failure('amount exceeds limit'),
            'limit_exceeded',
        )

    def test_classify_duplicate(self):
        self.assertEqual(
            self.Topup._classify_failure('RtnCode=10100073 duplicate'),
            'duplicate_transaction',
        )

    def test_classify_unknown_falls_to_generic(self):
        self.assertEqual(
            self.Topup._classify_failure('totally random error'),
            'generic',
        )

    def test_classify_empty_or_none(self):
        self.assertEqual(self.Topup._classify_failure(''), 'generic')
        self.assertEqual(self.Topup._classify_failure(None), 'generic')
```

Add to `src/tests/__init__.py`:

```python
from . import test_wallet_topup_classify_failure
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_wallet_topup_classify_failure
```
Expected: `AttributeError: _classify_failure`.

### Step 3: Minimal implementation

**File:** `src/models/wallet_topup.py` — append to the `WalletTopup` class:

```python
    _FAILURE_KEYWORDS = (
        ('card_declined', ('declined', 'decline', '10100248')),
        ('limit_exceeded', ('limit', 'insufficient', '10100058')),
        ('duplicate_transaction', ('duplicate', '10100073')),
    )

    @api.model
    def _classify_failure(self, message):
        if not message:
            return 'generic'
        low = message.lower()
        for code, keywords in self._FAILURE_KEYWORDS:
            if any(k in low for k in keywords):
                return code
        return 'generic'
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_wallet_topup_classify_failure
```
Expected: `5 passed`.

### Step 5: Commit

```bash
git add src/models/wallet_topup.py src/tests/test_wallet_topup_classify_failure.py src/tests/__init__.py
git commit -m "feat(billing): classify ECPay failure messages"
```

---

## Task 5: `_on_payment_done` / `_on_payment_failed` on `woow.wallet.topup`

**Files:**
- Modify: `src/models/wallet_topup.py`
- Create: `src/tests/test_wallet_topup_payment_hooks.py`
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_wallet_topup_payment_hooks.py`

```python
from unittest.mock import MagicMock
from odoo.tests.common import TransactionCase


class TestTopupPaymentHooks(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'TopupUser'})
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'P1', 'amount': 200, 'credits': 180, 'bonus': 20, 'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 200,
            'credits': 180,
            'bonus': 20,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
        })
        self.tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self._get_provider().id,
            'partner_id': self.partner.id,
            'amount': 200,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TOPUP%d' % self.topup.id,
            'woow_topup_id': self.topup.id,
        })

    def _get_provider(self):
        p = self.env['payment.provider'].sudo().search([('code', '=', 'ECPay')], limit=1)
        if not p:
            self.skipTest("ECPay provider not installed")
        return p

    def test_on_payment_done_credits_wallet_and_flips_state(self):
        starting = self.partner.wallet_balance
        self.topup._on_payment_done(self.tx)
        self.assertEqual(self.topup.state, 'done')
        self.assertTrue(self.topup.wallet_transaction_id)
        self.assertAlmostEqual(self.partner.wallet_balance - starting, 200.0)

    def test_on_payment_done_is_idempotent(self):
        self.topup._on_payment_done(self.tx)
        first_wtx = self.topup.wallet_transaction_id
        first_balance = self.partner.wallet_balance
        self.topup._on_payment_done(self.tx)  # replay
        self.assertEqual(self.topup.wallet_transaction_id, first_wtx)
        self.assertEqual(self.partner.wallet_balance, first_balance)

    def test_on_payment_failed_records_failure(self):
        self.tx.state_message = 'Card declined by bank'
        self.topup._on_payment_failed(self.tx)
        self.assertEqual(self.topup.state, 'failed')
        self.assertEqual(self.topup.failure_code, 'card_declined')
        self.assertEqual(self.topup.failure_message, 'Card declined by bank')

    def test_on_payment_failed_no_op_when_done(self):
        self.topup._on_payment_done(self.tx)
        self.topup._on_payment_failed(self.tx)
        self.assertEqual(self.topup.state, 'done')
```

Add to `src/tests/__init__.py`:

```python
from . import test_wallet_topup_payment_hooks
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_wallet_topup_payment_hooks
```
Expected: `AttributeError: _on_payment_done`.

### Step 3: Minimal implementation

**File:** `src/models/wallet_topup.py` — append to the class:

```python
    def _on_payment_done(self, tx):
        self.ensure_one()
        if self.state == 'done' or self.wallet_transaction_id:
            return
        description = f"Top-up: {self.plan_id.name}"
        self.partner_id.add_wallet_points(
            self.credits + self.bonus,
            description=description,
            payment_transaction=tx,
        )
        wtx = self.env['wallet.transaction'].search([
            ('payment_transaction_id', '=', tx.id),
            ('transaction_type', '=', 'topup'),
        ], limit=1)
        self.write({'state': 'done', 'wallet_transaction_id': wtx.id})

    def _on_payment_failed(self, tx):
        self.ensure_one()
        if self.state in ('done', 'failed'):
            return
        message = tx.state_message or ''
        self.write({
            'state': 'failed',
            'failure_message': message,
            'failure_code': self._classify_failure(message),
        })
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_wallet_topup_payment_hooks
```
Expected: `4 passed`.

### Step 5: Commit

```bash
git add src/models/wallet_topup.py \
        src/tests/test_wallet_topup_payment_hooks.py src/tests/__init__.py
git commit -m "feat(billing): credit wallet on topup payment done"
```

---

## Task 6: `payment.transaction._set_done` / `_set_error` override

**Files:**
- Modify: `src/models/payment_transaction.py`
- Create: `src/tests/test_payment_transaction_overrides.py`
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_payment_transaction_overrides.py`

```python
from odoo.tests.common import TransactionCase


class TestPaymentTransactionOverrides(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'T'})
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'P', 'amount': 100, 'credits': 100, 'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 100, 'credits': 100,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
        })
        self.tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'amount': 100,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TX%d' % self.topup.id,
            'woow_topup_id': self.topup.id,
        })

    def test_set_done_triggers_topup_credit(self):
        self.tx._set_done()
        self.topup.invalidate_recordset()
        self.assertEqual(self.topup.state, 'done')

    def test_set_done_no_op_without_topup_link(self):
        tx2 = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'amount': 1,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'SOLO1',
        })
        # Must not raise and must not touch any topup
        tx2._set_done()
        self.assertFalse(tx2.woow_topup_id)

    def test_set_error_triggers_topup_failure(self):
        self.tx._set_error('Card declined by bank')
        self.topup.invalidate_recordset()
        self.assertEqual(self.topup.state, 'failed')
        self.assertEqual(self.topup.failure_code, 'card_declined')
```

Add to `src/tests/__init__.py`:

```python
from . import test_payment_transaction_overrides
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_payment_transaction_overrides
```
Expected: `AssertionError: 'pending' != 'done'` (override not wired yet).

### Step 3: Minimal implementation

**File:** `src/models/payment_transaction.py` — extend the class:

```python
    def _set_done(self, state_message=None, **kwargs):
        res = super()._set_done(state_message=state_message, **kwargs)
        for tx in self:
            if tx.woow_topup_id and tx.woow_topup_id.state == 'pending':
                tx.woow_topup_id.sudo()._on_payment_done(tx)
        return res

    def _set_error(self, state_message=None, **kwargs):
        res = super()._set_error(state_message=state_message, **kwargs)
        for tx in self:
            if tx.woow_topup_id and tx.woow_topup_id.state == 'pending':
                tx.woow_topup_id.sudo()._on_payment_failed(tx)
        return res
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_payment_transaction_overrides
```
Expected: `3 passed`.

### Step 5: Commit

```bash
git add src/models/payment_transaction.py \
        src/tests/test_payment_transaction_overrides.py src/tests/__init__.py
git commit -m "feat(billing): hook payment done/error to topup handlers"
```

---

## Task 7: Rewrite `api_topup` controller

**Files:**
- Modify: `src/controllers/billing.py:485-540` (replace `api_topup`)
- Create: `src/tests/test_topup_controller.py` (HttpCase)
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_topup_controller.py`

```python
from unittest.mock import patch
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestTopupController(HttpCase):
    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.provider.write({'state': 'test'})
        self.user = self.env['res.users'].create({
            'name': 'Topup Tester', 'login': 'topup_test', 'password': 'topup_test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.user.partner_id.id,
            'payment_details': '**** 4242',
            'ecpg_bindcard_id': 'STUB_BIND',
            'ecpg_card_expire': '2812',
        })
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'TopupCtl', 'amount': 500, 'credits': 500, 'active': True,
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_payment_enabled', 'True',
        )
        self.authenticate('topup_test', 'topup_test')

    def _post(self, path, payload):
        return self.url_open(path, json={'params': payload}).json()

    def test_happy_path_returns_redirect_url(self):
        with patch(
            'odoo.addons.woow_paas_platform.models.payment_token.PaymentToken._charge',
            return_value={
                'tx': self.env['payment.transaction'].sudo().create({
                    'provider_id': self.provider.id,
                    'partner_id': self.user.partner_id.id,
                    'amount': 500,
                    'currency_id': self.env.company.currency_id.id,
                    'reference': 'STUBREF',
                }),
                'redirect_url': 'https://ecpay.example/3ds',
                'state': 'pending',
            },
        ):
            result = self._post(
                '/api/billing/wallet/topup', {'plan_id': self.plan.id},
            )['result']
        self.assertTrue(result['success'])
        self.assertIn('topup_id', result['data'])
        self.assertEqual(result['data']['redirect_url'], 'https://ecpay.example/3ds')

    def test_rejects_when_flag_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_payment_enabled', 'False',
        )
        result = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'payment_not_configured')

    def test_rejects_when_no_card(self):
        self.token.active = False
        result = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'no_card')

    def test_rejects_plan_not_found(self):
        result = self._post(
            '/api/billing/wallet/topup', {'plan_id': 99999},
        )['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'plan_not_found')

    def test_duplicate_pending_returns_existing_topup(self):
        first = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        # Force previous topup into pending state
        topup = self.env['woow.wallet.topup'].browse(first['data']['topup_id'])
        topup.state = 'pending'
        second = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        self.assertTrue(second['success'])
        self.assertEqual(second['data']['topup_id'], first['data']['topup_id'])
        self.assertTrue(second['data'].get('resumed'))
```

Add to `src/tests/__init__.py`:

```python
from . import test_topup_controller
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_topup_controller
```
Expected: `AssertionError` (current api_topup still returns placeholder).

### Step 3: Minimal implementation

**File:** `src/controllers/billing.py` — replace `api_topup` body (located at `line 485-540`).

Full replacement:

```python
    @route("/api/billing/wallet/topup", auth="user", methods=["POST"], type="json")
    def api_topup(self, plan_id: int | None = None,
                  invoice_intent: dict | None = None, **kw) -> dict[str, Any]:
        """Start a top-up charge.

        Returns:
            success payload with either:
                { topup_id, redirect_url, state }         -- new flow started
                { topup_id, redirect_url, resumed: true } -- existing pending found
            or error with stable `code`.
        """
        ICP = request.env['ir.config_parameter'].sudo()
        if not ICP.get_param('woow_paas_platform.topup_payment_enabled', False):
            return {
                'success': False,
                'error': 'Credit top-up is not yet available.',
                'code': 'payment_not_configured',
            }

        if not plan_id:
            return {'success': False, 'error': 'plan_id is required', 'code': 'missing_params'}

        Plan = request.env['woow_paas_platform.topup_plan'].sudo()
        plan = Plan.browse(int(plan_id))
        if not plan.exists() or not plan.active:
            return {'success': False, 'error': 'Plan not found or inactive',
                    'code': 'plan_not_found'}

        partner = request.env.user.partner_id.sudo()
        card_info = self._get_primary_card_info(partner)
        if not card_info['bound']:
            return {'success': False, 'error': 'No payment method configured',
                    'code': 'no_card'}

        # Q7: at most one pending topup per partner
        Topup = request.env['woow.wallet.topup'].sudo()
        existing = Topup.search([
            ('partner_id', '=', partner.id),
            ('state', '=', 'pending'),
        ], limit=1)
        if existing:
            redirect_url = self._resume_3ds_url(existing)
            return {'success': True, 'data': {
                'topup_id': existing.id,
                'redirect_url': redirect_url,
                'resumed': True,
                'state': existing.state,
            }}

        token = self._get_default_ecpg_token(partner)
        if not token:
            return {'success': False, 'error': 'No payment method configured',
                    'code': 'no_card'}

        invoice_vals = self._build_invoice_intent_vals(invoice_intent or {})
        topup = Topup.create({
            'partner_id': partner.id,
            'plan_id': plan.id,
            'amount': plan.amount,
            'credits': plan.credits,
            'bonus': plan.bonus,
            'currency_id': (plan.currency_id or request.env.company.currency_id).id,
            'state': 'draft',
            **invoice_vals,
        })

        try:
            result = token._charge(
                amount=plan.amount,
                reference=f"TOPUP{topup.id}",
                currency_id=topup.currency_id.id,
                purpose_field='woow_topup_id',
                purpose_id=topup.id,
            )
        except Exception as e:
            _logger.exception("Topup charge failed for partner %s", partner.id)
            topup.write({'state': 'failed', 'failure_code': 'sdk_error',
                         'failure_message': str(e)[:200]})
            return {'success': False, 'error': 'Payment service unavailable',
                    'code': 'sdk_error'}

        tx = result['tx']
        topup.write({
            'payment_transaction_id': tx.id,
            'state': 'pending' if result['state'] in ('draft', 'pending') else result['state'],
        })

        return {'success': True, 'data': {
            'topup_id': topup.id,
            'redirect_url': result['redirect_url'],
            'state': topup.state,
        }}

    def _build_invoice_intent_vals(self, intent):
        """Whitelist invoice fields we persist on the topup order."""
        allowed = ('invoice_carrier_type', 'invoice_carrier', 'invoice_tax_id',
                   'invoice_title', 'invoice_lovecode')
        return {k: intent[k] for k in allowed if intent.get(k)}

    def _resume_3ds_url(self, topup):
        """Return the most recent ThreeDURL for a pending topup, if still valid.

        Phase 1 simply returns None so the frontend surfaces an "awaiting
        confirmation" state. Phase 2 can call ECPay query API to refresh.
        """
        return None
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_topup_controller
```
Expected: `5 passed`.

### Step 5: Commit

```bash
git add src/controllers/billing.py \
        src/tests/test_topup_controller.py src/tests/__init__.py
git commit -m "feat(billing): wire api_topup to ECPG token charge"
```

---

## Task 8: Status endpoint `/api/billing/topup/<id>/status`

**Files:**
- Modify: `src/controllers/billing.py` (append new route)
- Create: `src/tests/test_topup_status.py`
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_topup_status.py`

```python
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestTopupStatusEndpoint(HttpCase):
    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].create({
            'name': 'StatusUser', 'login': 'stat', 'password': 'stat',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'S1', 'amount': 100, 'credits': 100, 'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.user.partner_id.id,
            'plan_id': self.plan.id,
            'amount': 100, 'credits': 100,
            'currency_id': self.env.company.currency_id.id,
            'state': 'done',
        })
        self.authenticate('stat', 'stat')

    def _get(self, topup_id):
        return self.url_open(
            f'/api/billing/topup/{topup_id}/status',
            json={'params': {}},
        ).json()

    def test_owner_reads_status(self):
        r = self._get(self.topup.id)['result']
        self.assertTrue(r['success'])
        self.assertEqual(r['data']['state'], 'done')
        self.assertEqual(r['data']['credits'], 100)

    def test_other_user_gets_not_found(self):
        other = self.env['res.users'].create({
            'name': 'Other', 'login': 'other', 'password': 'other',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.authenticate('other', 'other')
        r = self._get(self.topup.id)['result']
        self.assertFalse(r['success'])
        self.assertEqual(r['code'], 'not_found')
```

Add to `src/tests/__init__.py`:

```python
from . import test_topup_status
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_topup_status
```
Expected: `404 Not Found`.

### Step 3: Minimal implementation

**File:** `src/controllers/billing.py` — append after `api_topup`:

```python
    @route("/api/billing/topup/<int:topup_id>/status",
           auth="user", methods=["POST"], type="json")
    def api_topup_status(self, topup_id, **kw) -> dict[str, Any]:
        partner = request.env.user.partner_id
        topup = request.env['woow.wallet.topup'].sudo().browse(topup_id)
        if not topup.exists() or topup.partner_id.id != partner.id:
            return {'success': False, 'error': 'Not found', 'code': 'not_found'}
        return {'success': True, 'data': {
            'topup_id': topup.id,
            'state': topup.state,
            'amount': topup.amount,
            'credits': topup.credits,
            'bonus': topup.bonus,
            'new_balance': partner.wallet_balance,
            'failure_code': topup.failure_code or '',
            'failure_message': topup.failure_message or '',
            'plan_name': topup.plan_id.name,
        }}
```

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_topup_status
```
Expected: `2 passed`.

### Step 5: Commit

```bash
git add src/controllers/billing.py \
        src/tests/test_topup_status.py src/tests/__init__.py
git commit -m "feat(billing): add topup status endpoint"
```

---

## Task 9: Cleanup cron

**Files:**
- Create: `src/data/cron_topup.xml`
- Modify: `src/__manifest__.py` — add `'data/cron_topup.xml'` to `data` list
- Modify: `src/models/wallet_topup.py`
- Create: `src/tests/test_topup_cron.py`
- Modify: `src/tests/__init__.py`

### Step 1: Write the failing test

**File:** `src/tests/test_topup_cron.py`

```python
from datetime import timedelta
from freezegun import freeze_time
from odoo import fields
from odoo.tests.common import TransactionCase


class TestTopupCron(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Cron'})
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'Cron', 'amount': 100, 'credits': 100, 'active': True,
        })

    def _make_pending_topup(self, tx_state='pending', age_minutes=35):
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'amount': 100,
            'currency_id': self.env.company.currency_id.id,
            'reference': f'CRON{fields.Datetime.now().timestamp()}',
        })
        tx.state = tx_state
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 100, 'credits': 100,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
            'payment_transaction_id': tx.id,
        })
        # backdate via SQL (create_date is normally readonly)
        old = fields.Datetime.now() - timedelta(minutes=age_minutes)
        self.env.cr.execute(
            "UPDATE woow_wallet_topup SET create_date = %s WHERE id = %s",
            (old, topup.id),
        )
        topup.invalidate_recordset(['create_date'])
        return topup, tx

    def test_fresh_pending_ignored(self):
        topup, _ = self._make_pending_topup(age_minutes=5)
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        self.assertEqual(topup.state, 'pending')

    def test_catches_up_when_tx_already_done(self):
        topup, tx = self._make_pending_topup(age_minutes=35)
        tx.state = 'done'
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        topup.invalidate_recordset()
        self.assertEqual(topup.state, 'done')

    def test_marks_failed_when_tx_errored(self):
        topup, tx = self._make_pending_topup(age_minutes=35)
        tx.state = 'error'
        tx.state_message = 'Card declined'
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        topup.invalidate_recordset()
        self.assertEqual(topup.state, 'failed')

    def test_cancels_long_stuck_pending(self):
        topup, tx = self._make_pending_topup(age_minutes=70)
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        topup.invalidate_recordset()
        self.assertEqual(topup.state, 'cancelled')
```

Add to `src/tests/__init__.py`:

```python
from . import test_topup_cron
```

### Step 2: Run test to verify it fails

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_topup_cron
```
Expected: `AttributeError: _cron_cleanup_pending_topups`.

### Step 3: Minimal implementation

**File:** `src/models/wallet_topup.py` — append to class:

```python
    _CLEANUP_GRACE_MIN = 30
    _CLEANUP_HARD_CANCEL_MIN = 60

    @api.model
    def _cron_cleanup_pending_topups(self):
        from datetime import timedelta
        grace = fields.Datetime.now() - timedelta(minutes=self._CLEANUP_GRACE_MIN)
        hard = fields.Datetime.now() - timedelta(minutes=self._CLEANUP_HARD_CANCEL_MIN)
        stuck = self.sudo().search([
            ('state', '=', 'pending'),
            ('create_date', '<', grace),
        ])
        for topup in stuck:
            tx = topup.payment_transaction_id
            if not tx:
                topup.write({'state': 'cancelled'})
                continue
            if tx.state == 'done':
                topup._on_payment_done(tx)
            elif tx.state in ('error', 'cancel'):
                topup._on_payment_failed(tx)
            elif tx.state in ('draft', 'pending') and topup.create_date < hard:
                try:
                    tx._set_canceled("Timed out waiting for 3DS confirmation")
                except Exception:
                    pass
                topup.write({'state': 'cancelled'})
```

**File:** `src/data/cron_topup.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="cron_topup_cleanup" model="ir.cron">
            <field name="name">Woow PaaS: Cleanup stale pending topups</field>
            <field name="model_id" ref="model_woow_wallet_topup"/>
            <field name="state">code</field>
            <field name="code">model._cron_cleanup_pending_topups()</field>
            <field name="interval_number">10</field>
            <field name="interval_type">minutes</field>
            <field name="active" eval="True"/>
        </record>
    </data>
</odoo>
```

**File:** `src/__manifest__.py` — add `'data/cron_topup.xml'` inside the `data` list (order with other `data/` entries).

### Step 4: Run test to verify it passes

```bash
./scripts/test-addon.sh --tags woow_paas_platform.test_topup_cron
```
Expected: `4 passed`.

### Step 5: Commit

```bash
git add src/models/wallet_topup.py src/data/cron_topup.xml \
        src/__manifest__.py \
        src/tests/test_topup_cron.py src/tests/__init__.py
git commit -m "feat(billing): cleanup cron for stale pending topups"
```

---

# Phase 2 — Frontend (Tasks 10 – 13)

## Task 10: Extend `billing_service` with `topup` + `getTopupStatus`

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js`

**Note:** Frontend tests require a live browser and are slower. Smoke in-session by loading the app under `./scripts/start-dev.sh`; regression covered by Task 14 manual checklist.

### Step 1: Write the failing test

Frontend has no automated unit tests in this repo. Instead, write a *behaviour checklist* and run through it at Step 4:

- [ ] `billingService.topup(planId)` resolves to `{ success, data?, error?, code? }`
- [ ] `billingService.getTopupStatus(id)` resolves to same shape

### Step 2: Observe current behaviour

Open browser, click Pre-pay; current network call returns `{success:false,error:'...'}` or adds points directly (pre-Phase-1 behaviour). Confirm before changing.

### Step 3: Implement

**File:** `src/static/src/paas/services/billing_service.js`

Find the existing `topup` method (grep for `async topup`) and replace it with:

```javascript
    async topup(planId, invoiceIntent = null) {
        try {
            const result = await jsonRpc("/api/billing/wallet/topup", {
                plan_id: planId,
                invoice_intent: invoiceIntent,
            });
            return result;
        } catch (err) {
            console.error("topup call failed", err);
            return { success: false, error: String(err), code: 'sdk_error' };
        }
    },

    async getTopupStatus(topupId) {
        try {
            return await jsonRpc(`/api/billing/topup/${topupId}/status`, {});
        } catch (err) {
            console.error("getTopupStatus failed", err);
            return { success: false, error: String(err), code: 'sdk_error' };
        }
    },
```

### Step 4: Manual verification

1. `./scripts/start-dev.sh` (if not running)
2. Open `http://localhost/woow#/billing/topup`
3. With DevTools Network open, click Pre-pay on any plan
4. Observe POST to `/api/billing/wallet/topup`; response should be a JSON-RPC envelope with `result: { success, data: { topup_id, redirect_url } }`

### Step 5: Commit

```bash
git add src/static/src/paas/services/billing_service.js
git commit -m "feat(billing): billingService.topup returns full payload"
```

---

## Task 11: Rewrite `AddFundsModal.onConfirm` for 3DS redirect

**Files:**
- Create: `src/static/src/paas/services/topup_errors.js`
- Modify: `src/static/src/paas/components/modal/AddFundsModal.js`

### Step 1: Prepare error translation

**File:** `src/static/src/paas/services/topup_errors.js`

```javascript
const CODE_MAP = {
    payment_not_configured: "Credit top-up is not available yet. Please contact support.",
    no_card: "Please add a payment method before topping up.",
    plan_not_found: "This plan is no longer available.",
    provider_disabled: "Payment service is temporarily unavailable.",
    duplicate_pending: "Your previous top-up is still processing, please wait.",
    sdk_error: "Payment service unavailable. Please try again in a moment.",
    card_declined: "The card was declined. Please verify the card or try another.",
    limit_exceeded: "The card's limit was exceeded.",
    duplicate_transaction: "Duplicate transaction detected. Please wait a moment and retry.",
    generic: "Payment failed. Please retry or contact support.",
};

export function translateTopupError(message, code) {
    if (code && CODE_MAP[code]) return CODE_MAP[code];
    if (message) return message;
    return CODE_MAP.generic;
}
```

### Step 2: Observe current behaviour

`onConfirm` currently calls `billingService.topup(this.plan.id)` and, on success, calls `props.onSuccess(result.data)` — treating the response as if points were credited immediately. Confirm by reading `src/static/src/paas/components/modal/AddFundsModal.js:77-93`.

### Step 3: Implement

**File:** `src/static/src/paas/components/modal/AddFundsModal.js`

At the top, add import:

```javascript
import { translateTopupError } from "../../services/topup_errors";
```

Replace `onConfirm`:

```javascript
  async onConfirm() {
    if (this.state.processing || !this.hasCard) return;
    this.state.processing = true;
    this.state.error = null;

    const result = await billingService.topup(this.plan.id);
    if (!result.success) {
      this.state.error = translateTopupError(result.error, result.code);
      this.state.processing = false;
      return;
    }
    if (result.data && result.data.redirect_url) {
      window.location.href = result.data.redirect_url;
      return;  // Browser is leaving; no further UI work
    }
    // Rare: server says state=done without 3DS (non-3DS ECPay path)
    await billingService.fetchWallet();
    this.props.onSuccess(result.data);
  }
```

### Step 4: Manual verification

1. Refresh `/woow#/billing/topup`
2. Click Pre-pay on a plan
3. Browser should navigate away to ECPay stage 3DS URL (or show translated error if fail path)
4. Intentionally disable the flag (admin settings) and retry → modal shows "Credit top-up is not available yet."

### Step 5: Commit

```bash
git add src/static/src/paas/services/topup_errors.js \
        src/static/src/paas/components/modal/AddFundsModal.js
git commit -m "feat(billing): redirect to 3DS on topup confirm"
```

---

## Task 12: `TopupStorePage` detects return from 3DS

**Files:**
- Modify: `src/static/src/paas/pages/billing/TopupStorePage.js`
- Modify: `src/static/src/paas/pages/billing/TopupStorePage.xml` (new error banner)

### Step 1: Observe current behaviour

`TopupStorePage.setup` currently doesn't read query params. When returning from 3DS with `?topup_id=N` it silently ignores it.

### Step 2: Implement JS

**File:** `src/static/src/paas/pages/billing/TopupStorePage.js`

Add import at top:

```javascript
import { translateTopupError } from "../../services/topup_errors";
```

In `setup()`, inside `state`, add:

```javascript
    this.state = useState({
      activeCategory: null,
      showAddFundsModal: false,
      showSuccessModal: false,
      selectedPlan: null,
      successData: null,
      globalError: null,
    });
```

After the existing `onMounted` handler (if any), add another:

```javascript
    onMounted(async () => {
      const q = this.router.params.query || {};
      if (!q.topup_id) return;
      const result = await billingService.getTopupStatus(q.topup_id);
      if (result && result.success) {
        const data = result.data;
        if (data.state === 'done') {
          this.state.successData = data;
          this.state.showSuccessModal = true;
          await billingService.fetchWallet();
        } else if (data.state === 'failed') {
          this.state.globalError = translateTopupError(
            data.failure_message, data.failure_code,
          );
        } else if (data.state === 'pending') {
          this.state.globalError =
            'Still confirming payment — please refresh in a moment.';
        } else if (data.state === 'cancelled') {
          this.state.globalError =
            'Previous top-up was cancelled. You can start a new one.';
        }
      }
      const cleanHash = window.location.hash.split('?')[0];
      window.history.replaceState(null, '', cleanHash);
    });
```

Add `onMounted` to the imports if not already:

```javascript
import { Component, useState, onMounted } from "@odoo/owl";
```

### Step 3: Implement banner

**File:** `src/static/src/paas/pages/billing/TopupStorePage.xml`

Find the top-level container (grep for `o_woow_topup_store`). Right after the opening `<div>`, insert:

```xml
                <t t-if="state.globalError">
                    <div class="o_woow_topup_global_error">
                        <WoowIcon name="'error'" size="20"/>
                        <span t-esc="state.globalError"/>
                        <button class="o_woow_topup_dismiss"
                                t-on-click="() => (state.globalError = null)">
                            ×
                        </button>
                    </div>
                </t>
```

(SCSS for the banner is not required for green tests; reuse existing alert styling if available, otherwise leave as plain text — Phase 2 polish.)

### Step 4: Manual verification

1. Trigger successful top-up; after 3DS returns, TopupSuccessModal should open and sidebar credits should update.
2. Trigger wrong OTP in 3DS; after return, the red banner should show a translated message.

### Step 5: Commit

```bash
git add src/static/src/paas/pages/billing/TopupStorePage.js \
        src/static/src/paas/pages/billing/TopupStorePage.xml
git commit -m "feat(billing): show topup result on return from 3DS"
```

---

## Task 13: Wire everything through `__manifest__.py`

**Files:**
- Modify: `src/__manifest__.py`

### Step 1: Review

Run:

```bash
grep -n "data/cron_topup.xml\|security/ir.model.access.csv" src/__manifest__.py
```

Confirm `data/cron_topup.xml` is listed (added in Task 9) and that `security/ir.model.access.csv` was already listed (no action required, just verify).

### Step 2: Bump version

**File:** `src/__manifest__.py`

Bump `version` by one patch level (e.g. `18.0.1.0.11` → `18.0.1.0.12`). This keeps CHANGELOG and later release workflow aligned.

### Step 3: Full module reinstall

```bash
./scripts/test-addon.sh  # runs the entire module test suite
```
Expected: **all green**, no regression in unrelated tests.

### Step 4: Commit

```bash
git add src/__manifest__.py
git commit -m "chore(billing): bump version for topup charge feature"
```

---

# Phase 3 — Smoke Validation (Task 14)

## Task 14: Manual E2E on staging

**Not a code change.** Perform the checklist from the design doc against staging. No commit; record results in a new file `docs/plans/2026-04-21-ecpg-topup-charge-smoke-results.md` if preserving evidence is desired.

### Checklist

- [ ] Deploy via `/woow:deploy-stg`
- [ ] Enable `topup_payment_enabled` in settings
- [ ] Remove any existing bound ECPG card → Pre-pay shows bind CTA
- [ ] Re-bind via `/settings/payment-methods/add` with test card `4311-9522-2222-2222`
- [ ] Pre-pay → 3DS stage → test OTP → returns to `/woow#/billing/topup?topup_id=N`
- [ ] TopupSuccessModal appears, wallet balance increases by `credits + bonus`
- [ ] Re-test with wrong OTP → translated error banner appears
- [ ] Admin backoffice: `payment.transaction` record shows RtnCode/RtnMsg; `wallet.transaction` has exactly one topup row; `woow.wallet.topup` state is `done`
- [ ] Close 3DS tab without completing; wait 35 min; run cron manually via debug menu; `topup.state` becomes `cancelled`, no points added
- [ ] Double-click Pre-pay: second call returns `resumed: true`; only one `payment.transaction` exists

### Rollback plan if any check fails
- Flip `topup_payment_enabled=False` (immediate block).
- `git revert` the offending commits on `develop`.
- Redeploy.

---

# Reference Material

## Relevant skills for executing this plan
- superpowers:executing-plans — work through tasks sequentially
- superpowers:test-driven-development — TDD discipline (red → green → refactor → commit)
- superpowers:verification-before-completion — evidence before claiming done
- superpowers:subagent-driven-development — if dispatching per-task subagents

## Files this plan touches

**Create:**
- `src/models/wallet_topup.py`
- `src/models/payment_transaction.py`
- `src/models/payment_token.py`
- `src/data/cron_topup.xml`
- `src/static/src/paas/services/topup_errors.js`
- `src/tests/__init__.py` (if missing)
- `src/tests/test_wallet_topup_model.py`
- `src/tests/test_payment_transaction_fields.py`
- `src/tests/test_payment_token_charge.py`
- `src/tests/test_wallet_topup_classify_failure.py`
- `src/tests/test_wallet_topup_payment_hooks.py`
- `src/tests/test_payment_transaction_overrides.py`
- `src/tests/test_topup_controller.py`
- `src/tests/test_topup_status.py`
- `src/tests/test_topup_cron.py`

**Modify:**
- `src/models/__init__.py`
- `src/security/ir.model.access.csv`
- `src/controllers/billing.py` (api_topup rewrite + new status route + helpers)
- `src/static/src/paas/services/billing_service.js`
- `src/static/src/paas/components/modal/AddFundsModal.js`
- `src/static/src/paas/pages/billing/TopupStorePage.js`
- `src/static/src/paas/pages/billing/TopupStorePage.xml`
- `src/__manifest__.py`

**Not touched in Phase 1:**
- `extra/extra-addons/payment_ecpay_ecpg/` (upstream)
- `extra/extra-addons/sh_subscription_wallet/` (upstream; its mixin migration is a Phase 2 work item per design doc)

## Estimated task durations (for planning)

| Task | Focus | Rough time |
|---|---|---|
| 1 | Model scaffold | 20 min |
| 2 | TX field | 10 min |
| 3 | Token._charge helper | 40 min |
| 4 | Error classifier | 15 min |
| 5 | Payment hooks | 30 min |
| 6 | TX overrides | 25 min |
| 7 | Controller rewrite | 60 min |
| 8 | Status endpoint | 20 min |
| 9 | Cleanup cron | 40 min |
| 10 | Frontend service | 15 min |
| 11 | AddFundsModal | 25 min |
| 12 | TopupStorePage | 30 min |
| 13 | Manifest / version | 10 min |
| 14 | Smoke | 60 min |
| **Total** | | **~6.5 h** engineer time |
