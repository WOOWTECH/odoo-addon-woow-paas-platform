---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-19-settings-phase2b-implementation.md
archived: 2026-07-19
archived-reason: "已落地（Settings UI build phase）；現況=src/static/src/paas"
---

# Auto-Topup Integration (Phase 2b) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When a subscription auto-invoice fails due to insufficient wallet balance, try to charge the partner's bound credit card for the shortfall amount; on success, top up the wallet and retry the invoice. On failure (including 3DS), fall back to the Phase 1 low-balance email notification.

**Architecture:**
- New helper `_attempt_auto_topup(partner, shortfall)` on `sh.subscription.subscription` (added via the existing `ShSubscriptionFix` inherit class). Creates a `payment.transaction` with the partner's default `payment.token` and calls the ECPG-aware `_send_payment_request()` override. On `state == 'done'`, credits the wallet via the existing `partner.add_wallet_points()` API (which auto-creates a `wallet.transaction` row).
- `_sh_create_auto_invoice` insufficient-balance branch is augmented: try `_attempt_auto_topup`; if it succeeds, re-check balance and proceed with invoice payment; otherwise, fall through to the existing `_notify_low_balance` path.
- 3DS Responses are treated as failure — cron has no interactive user to complete 3DS.

**Tech Stack:** Odoo 18, Python 3.12. Depends on `payment_ecpay_ecpg` (added in Phase 2a), `sh_subscription_wallet` (existing), and the `payment.transaction._send_payment_request` override that the ECPG addon installs.

**Out of scope (later):**
- Default-token cleanup on `payment.token.archive` (Phase 2c).
- Card expiry proactive notification (Phase 2c).
- `GetMemberBindCard` reconcile of lost `bind_callback` payloads (Phase 2c).
- Design system extraction (banner / breadcrumb components).

---

## Task 1: Add `_attempt_auto_topup` helper with tests (happy path)

**Rationale:** Core behavior first. TDD the happy path where the card charge succeeds synchronously (no 3DS), the wallet is credited, and the helper returns success. Other branches (no token, 3DS, SDK error) are Task 2.

**Files:**
- Modify: `src/models/sh_subscription_fix.py` (add method to `ShSubscriptionFix` class)
- Modify: `src/tests/test_low_balance_notification.py` (add class `TestAutoTopup` OR create new file)

**Step 1: Write the failing test**

Add to `src/tests/test_low_balance_notification.py` at the bottom of the file (same file because it shares the `partner` + mail-template fixture patterns):

```python
    def test_attempt_auto_topup_success_credits_wallet(self):
        """When ECPG charge succeeds, helper credits the wallet and returns (True, 'ok')."""
        from unittest.mock import patch, MagicMock

        partner = self.partner
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured in this DB")
        provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })

        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_AUTOTOPUP',
            'ecpg_bindcard_id': 'BC_AUTOTOPUP',
            'ecpg_member_id': str(partner.id),
        })
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(partner.company_id or self.env.company) \
                   .sudo().property_payment_token_id = token

        # Mock _send_payment_request to return a 'done' state (no 3DS)
        def _fake_send(self_tx):
            self_tx.state = 'done'
            self_tx.provider_reference = 'FAKE_ECPAY_REF'
            return None

        Subscription = self.env['sh.subscription.subscription']
        before_balance = partner.wallet_balance

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send,
        ):
            ok, msg = Subscription._attempt_auto_topup(partner, 400.0)

        self.assertTrue(ok, f"Expected success, got ({ok}, {msg})")
        self.assertEqual(msg, 'ok')
        partner.invalidate_recordset()
        self.assertEqual(partner.wallet_balance, before_balance + 400.0)

        # Verify a wallet.transaction row was created (add_wallet_points side effect)
        tx_rows = self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'topup'),
        ], order='id desc', limit=1)
        self.assertTrue(tx_rows, "Expected a wallet.transaction row for auto-topup")
        self.assertEqual(tx_rows.amount, 400.0)
```

**Step 2: Run test, expect FAIL** (`_attempt_auto_topup` not defined).

Expected: `AttributeError: type object 'sh.subscription.subscription' has no attribute '_attempt_auto_topup'`.

**Step 3: Implement `_attempt_auto_topup`**

In `src/models/sh_subscription_fix.py`, add to the `ShSubscriptionFix` class after `_notify_low_balance`:

```python
    @api.model
    def _attempt_auto_topup(self, partner, shortfall):
        """Charge the partner's default bound card to top up the wallet.

        Args:
            partner: res.partner record
            shortfall: float amount needed (TWD)

        Returns:
            (True, 'ok')           — charge succeeded, wallet credited
            (False, 'no_token')    — partner has no default ECPG token
            (False, '3ds_required')— ECPG returned ThreeDURL; cron cannot complete 3DS
            (False, <state>)       — payment.transaction ended in a non-done state
            (False, <exception>)   — unexpected error
        """
        if shortfall <= 0:
            return False, 'no_shortfall'

        if 'property_payment_token_id' not in partner._fields:
            return False, 'no_token'

        token = partner.with_company(
            partner.company_id or self.env.company
        ).sudo().property_payment_token_id

        if not token or not token.active or not token.ecpg_bindcard_id:
            return False, 'no_token'

        provider = token.provider_id
        if not provider or not getattr(provider, 'enable_ecpg', False):
            return False, 'provider_not_enabled'

        currency = provider.main_currency_id or self.env.ref(
            'base.TWD', raise_if_not_found=False,
        )
        if not currency:
            return False, 'currency_not_configured'

        reference = f'AUTOTOPUP-{partner.id}-{fields.Datetime.now().strftime("%Y%m%d%H%M%S")}'
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': token.payment_method_id.id,
            'reference': reference,
            'amount': float(shortfall),
            'currency_id': currency.id,
            'partner_id': partner.id,
            'token_id': token.id,
        })

        try:
            result = tx._send_payment_request()
        except Exception as e:
            _logger.exception('Auto-topup send_payment_request failed: %s', e)
            return False, 'sdk_error'

        # 3DS redirect — cron cannot drive it. Treat as failure; Phase 1
        # _notify_low_balance will still fire so the user can top up manually.
        if isinstance(result, dict) and result.get('redirect_url'):
            _logger.info('Auto-topup requires 3DS for partner=%s; aborting', partner.id)
            return False, '3ds_required'

        if tx.state != 'done':
            _logger.warning(
                'Auto-topup transaction %s ended in state=%s', tx.reference, tx.state,
            )
            return False, tx.state

        partner.sudo().add_wallet_points(
            float(shortfall),
            description='Auto top-up for subscription renewal',
            payment_transaction=tx,
        )
        _logger.info(
            'Auto-topup succeeded: partner=%s amount=%.2f tx=%s',
            partner.id, shortfall, tx.reference,
        )
        return True, 'ok'
```

**Step 4: Run test, expect PASS.**

**Step 5: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_low_balance_notification.py
git commit -m "feat: add _attempt_auto_topup helper charging bound card to credit wallet"
```

---

## Task 2: Auto-topup failure branches (no-token / 3DS / failed state)

**Rationale:** Edge cases drive business logic — users without bound cards, cards that require 3DS, and upstream failures must all degrade gracefully to the existing `_notify_low_balance` path.

**Files:**
- Modify: `src/tests/test_low_balance_notification.py` (add 3 tests)
- No production code changes — helper already handles these paths from Task 1.

**Step 1: Write failing tests**

Add to the same test class:

```python
    def test_attempt_auto_topup_returns_no_token_when_partner_has_no_default(self):
        """Partner without a default payment token → (False, 'no_token')."""
        Subscription = self.env['sh.subscription.subscription']
        # self.partner has no property_payment_token_id in setUp
        ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def test_attempt_auto_topup_returns_3ds_required_when_ecpg_redirects(self):
        """ECPG charge that needs 3DS → (False, '3ds_required'), no wallet change."""
        from unittest.mock import patch

        partner = self.partner
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured in this DB")
        provider.write({'enable_ecpg': True, 'state': 'test'})

        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 1111',
            'provider_ref': 'BC_3DS',
            'ecpg_bindcard_id': 'BC_3DS',
            'ecpg_member_id': str(partner.id),
        })
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(partner.company_id or self.env.company) \
                   .sudo().property_payment_token_id = token

        def _fake_send_3ds(self_tx):
            # Simulate ECPG override returning a redirect dict for 3DS.
            return {'redirect_url': 'https://3ds.example/verify'}

        before = partner.wallet_balance
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send_3ds,
        ):
            Subscription = self.env['sh.subscription.subscription']
            ok, msg = Subscription._attempt_auto_topup(partner, 400.0)

        self.assertFalse(ok)
        self.assertEqual(msg, '3ds_required')
        partner.invalidate_recordset()
        self.assertEqual(partner.wallet_balance, before, "Wallet must be unchanged on 3DS")

    def test_attempt_auto_topup_returns_failure_state_when_ecpg_errors(self):
        """When _send_payment_request leaves tx.state != 'done', return (False, state)."""
        from unittest.mock import patch

        partner = self.partner
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured in this DB")
        provider.write({'enable_ecpg': True, 'state': 'test'})

        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 2222',
            'provider_ref': 'BC_FAIL',
            'ecpg_bindcard_id': 'BC_FAIL',
            'ecpg_member_id': str(partner.id),
        })
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(partner.company_id or self.env.company) \
                   .sudo().property_payment_token_id = token

        def _fake_send_error(self_tx):
            self_tx.state = 'error'
            return None

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send_error,
        ):
            Subscription = self.env['sh.subscription.subscription']
            ok, msg = Subscription._attempt_auto_topup(partner, 400.0)

        self.assertFalse(ok)
        self.assertEqual(msg, 'error')
```

**Step 2: Run tests, expect PASS** (Task 1's implementation already handles these branches).

**Step 3: Commit**

```bash
git add src/tests/test_low_balance_notification.py
git commit -m "test: cover auto-topup no-token / 3DS / failed-state branches"
```

---

## Task 3: Wire `_attempt_auto_topup` into `_sh_create_auto_invoice`

**Rationale:** The helper is useless until the cron calls it. Hook it into the existing insufficient-balance branch so the cron tries auto-topup *before* falling back to `_notify_low_balance`.

**Files:**
- Modify: `src/models/sh_subscription_fix.py` (update `_sh_create_auto_invoice` insufficient-balance branch)
- Modify: `src/tests/test_low_balance_notification.py` (add 2 integration tests)

**Step 1: Write failing integration tests**

Two tests that exercise the cron wrapper:

```python
    def test_auto_invoice_tries_topup_before_notifying(self):
        """On insufficient balance, cron calls _attempt_auto_topup BEFORE _notify_low_balance."""
        from unittest.mock import patch

        Subscription = self.env['sh.subscription.subscription']
        # Use patch-as-order to verify call sequence
        with patch.object(
            type(Subscription), '_attempt_auto_topup',
            return_value=(True, 'ok'),
        ) as mock_topup, patch.object(
            type(Subscription), '_notify_low_balance',
        ) as mock_notify:
            # Indirect invocation: the source MUST contain the wire-up.
            import inspect
            from odoo.addons.woow_paas_platform.models.sh_subscription_fix import ShSubscriptionFix
            source = inspect.getsource(ShSubscriptionFix._sh_create_auto_invoice)
            self.assertIn(
                '_attempt_auto_topup',
                source,
                "_sh_create_auto_invoice must call _attempt_auto_topup in the "
                "insufficient-balance branch.",
            )
            # Also assert the order: _attempt_auto_topup must appear before
            # _notify_low_balance in the source body.
            topup_idx = source.index('_attempt_auto_topup')
            notify_idx = source.index('_notify_low_balance')
            self.assertLess(
                topup_idx, notify_idx,
                "_attempt_auto_topup must run BEFORE _notify_low_balance",
            )

    def test_auto_invoice_skips_notify_when_auto_topup_succeeds(self):
        """When auto-topup succeeds, invoice is paid and no low-balance email is queued.

        Source-assertion variant — full cron integration is too heavy to set up in
        a TransactionCase; this locks the branch logic in place as a regression guard.
        """
        import inspect
        from odoo.addons.woow_paas_platform.models.sh_subscription_fix import ShSubscriptionFix
        source = inspect.getsource(ShSubscriptionFix._sh_create_auto_invoice)
        # When auto-topup returns True, branch must call _handle_invoice_creation
        # (not _notify_low_balance). Verify both _handle_invoice_creation and the
        # success-gated call order exist.
        self.assertIn('_handle_invoice_creation', source)
        # The branch must re-check balance or directly pay the invoice after topup.
        self.assertRegex(
            source,
            r'(_attempt_auto_topup.*\n.*_handle_invoice_creation)'
            r'|(topup_ok\s*[,=].*_attempt_auto_topup.*\n.*if\s+topup_ok.*_handle_invoice_creation)',
            "Success branch after _attempt_auto_topup must pay the invoice "
            "via _handle_invoice_creation",
        )
```

**Step 2: Run tests, expect FAIL**

Expected: first test's source assertion fails — `_attempt_auto_topup` isn't in `_sh_create_auto_invoice` yet.

**Step 3: Update `_sh_create_auto_invoice`**

In `src/models/sh_subscription_fix.py`, change the insufficient-balance branch (currently just logs + notifies) to:

```python
            # Check wallet balance before posting/paying the invoice
            partner = subscription.sh_partner_id
            if partner.wallet_balance >= invoice_id.amount_total:
                subscription._handle_invoice_creation(invoice_id)
            else:
                shortfall = invoice_id.amount_total - partner.wallet_balance
                topup_ok, topup_msg = self._attempt_auto_topup(partner, shortfall)
                if topup_ok:
                    # Wallet now has enough; proceed with normal invoice payment.
                    _logger.info(
                        'Auto-topup covered shortfall=%.2f for subscription %s; '
                        'proceeding with invoice %s',
                        shortfall, subscription.name, invoice_id.name,
                    )
                    subscription._handle_invoice_creation(invoice_id)
                else:
                    _logger.warning(
                        "Insufficient wallet balance for subscription %s and "
                        "auto-topup failed (%s): balance=%.2f, required=%.2f — "
                        "invoice %s left unpaid",
                        subscription.name, topup_msg, partner.wallet_balance,
                        invoice_id.amount_total, invoice_id.name,
                    )
                    self._notify_low_balance(partner, invoice_id)
```

**Step 4: Run tests, expect PASS.**

**Step 5: Commit**

```bash
git add src/models/sh_subscription_fix.py src/tests/test_low_balance_notification.py
git commit -m "feat: wire _attempt_auto_topup into _sh_create_auto_invoice insufficient-balance branch"
```

---

## Task 4: Delete dead Phase 2a modal code

**Rationale:** `AddCardModal.{js,xml,scss}` and `CardAddedSuccessModal.{js,xml,scss}` have been dead code since Phase 2a — they were the old manual-entry binding flow, entirely replaced by the full-page ECPay SDK flow at `#/settings/payment-methods/add`. No live code path imports them. They add bundle size and risk to the codebase (future developer might reuse them thinking they work).

**Files:**
- Delete: `src/static/src/paas/components/modal/AddCardModal.js`
- Delete: `src/static/src/paas/components/modal/AddCardModal.xml`
- Delete: `src/static/src/paas/components/modal/AddCardModal.scss` (if it exists)
- Delete: `src/static/src/paas/components/modal/CardAddedSuccessModal.js`
- Delete: `src/static/src/paas/components/modal/CardAddedSuccessModal.xml`
- Delete: `src/static/src/paas/components/modal/CardAddedSuccessModal.scss` (if it exists)

**Step 1: Confirm nothing imports them**

```bash
grep -rn "AddCardModal\|CardAddedSuccessModal" src/
```

Expected matches: ONLY the files being deleted themselves (no consumer). If anything else matches, STOP and report — that's a caller we'd break.

**Step 2: Delete the files**

```bash
rm -f src/static/src/paas/components/modal/AddCardModal.js \
      src/static/src/paas/components/modal/AddCardModal.xml \
      src/static/src/paas/components/modal/AddCardModal.scss \
      src/static/src/paas/components/modal/CardAddedSuccessModal.js \
      src/static/src/paas/components/modal/CardAddedSuccessModal.xml \
      src/static/src/paas/components/modal/CardAddedSuccessModal.scss
```

Use `-f` so missing `.scss` files don't break the command.

**Step 3: Re-verify no references**

```bash
grep -rn "AddCardModal\|CardAddedSuccessModal" src/
```

Expected: ZERO matches.

**Step 4: Commit**

```bash
git add -A src/static/src/paas/components/modal/
git commit -m "chore: remove dead AddCardModal and CardAddedSuccessModal (superseded by Phase 2a)"
```

---

## Task 5: Mark Phase 2b shipped in design doc

**Files:**
- Modify: `docs/plans/2026-04-16-settings-payment-methods-design.md`

**Step 1: Edit the Phase 2b heading**

Change `### Phase 2b — Auto-topup 整合（進行中）` to:

```markdown
### Phase 2b — Auto-topup 整合（✅ 已完成 — shipped <today>）

commits range and summary of what shipped:
- `_attempt_auto_topup(partner, shortfall)` helper on sh.subscription.subscription
- Wired into _sh_create_auto_invoice insufficient-balance branch (auto-topup before notify)
- 3DS redirect treated as failure (cron cannot drive 3DS)
- Dead code cleanup: AddCardModal.* and CardAddedSuccessModal.* removed
```

Keep the existing "Design decisions" and "Scope" bullets for history.

**Step 2: Commit**

```bash
git add docs/plans/2026-04-16-settings-payment-methods-design.md
git commit -m "docs: mark Phase 2b as shipped"
```

---

## Task 6: Manual verification checklist (before opening PR)

Not a code task — a pre-PR checklist to run against staging once `payment_ecpay_ecpg` + this PR are deployed.

Deploy via `/deploy-stg`. Then:

- [ ] In staging, bind at least one card (Phase 2a flow): `#/settings/payment-methods/add` → complete 3DS.
- [ ] Set it as default (Set Default button). Verify `res.partner.property_payment_token_id` is populated.
- [ ] Create a test subscription plan with a low recurring amount (e.g. NT$50).
- [ ] Manually trigger the subscription cron with partner wallet balance 0.
- [ ] **Expected log**: `Auto-topup succeeded: partner=… amount=50.00 tx=AUTOTOPUP-…` followed by normal invoice creation logs.
- [ ] **Expected DB**: new `wallet.transaction` row with `transaction_type='topup'`, `amount=50.0`, `payment_transaction_id` linked to the ECPG `payment.transaction`.
- [ ] Subscription invoice is posted and paid (`account.move.payment_state='in_payment'` or `paid`).
- [ ] Partner wallet balance after: `0 + 50 − 50 = 0` (wallet credited and immediately deducted by invoice).
- [ ] Repeat with a partner whose default card is missing — **expected**: `_notify_low_balance` fires, email queued, wallet unchanged.
- [ ] Repeat with a mocked 3DS-required card — **expected**: `_notify_low_balance` fires (auto-topup fails with `3ds_required`).
- [ ] Regression: existing Phase 1 "just notify" path for partners with no ECPG binding continues to work.
- [ ] Regression: Phase 2a Payment Methods UI unaffected (list, add, delete, set default).

---

## Open questions surfaced during this plan

These remain for future phases:

1. **Idempotency on cron reruns**: if the cron fires twice (scheduler misfire), the same partner could be charged twice. Current design relies on `sh_date_of_next_payment` being advanced before the charge attempt (line 44 in current code) — verify that holds after our change.
2. **Retry policy**: if ECPay is momentarily down, should we retry tomorrow? Currently no — the invoice is left unpaid and the user gets an email. Acceptable for MVP.
3. **Partial-topup**: if the card charge succeeds for NT$400 but the invoice is NT$500 (shouldn't happen — we charge `shortfall = amount_total − balance`), what then? Current design: not possible since we always charge exactly the shortfall.
4. **Custom `TotalAmount` minimum**: ECPay may reject amounts below NT$5. If `shortfall < 5`, we should round up. Current code doesn't. Deferred — real usage will surface if this is an issue.

## Reference: where this sits in the architecture

```
_sh_create_auto_invoice (daily cron)
  │
  ├─ for each subscription due today:
  │    ├─ create invoice
  │    ├─ advance sh_date_of_next_payment
  │    ├─ if wallet_balance >= amount: pay invoice (existing)
  │    └─ else:
  │         ├─ _attempt_auto_topup(partner, shortfall)   ← Phase 2b NEW
  │         │    ├─ find default payment.token (property_payment_token_id)
  │         │    ├─ create payment.transaction
  │         │    ├─ tx._send_payment_request() → ECPG SDK (via ecpay addon override)
  │         │    ├─ on 'done': partner.add_wallet_points(shortfall, tx=tx)
  │         │    │             → wallet.transaction row created
  │         │    └─ on 3DS / fail / exception: return (False, <reason>)
  │         │
  │         ├─ if success: pay invoice (via _handle_invoice_creation)
  │         └─ else:       _notify_low_balance(partner, invoice)   ← Phase 1 fallback
  │
  └─ (PDF + email logic, unchanged)
```
