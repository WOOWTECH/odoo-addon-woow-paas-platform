---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-28-sprint2-g2a-skip-trial-estimate.md
archived: 2026-07-19
archived-reason: "一次性 sprint 微任務，已落地"
---

# G2-A: Skip Trial Estimate API + Dialog Breakdown — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate the "Generate your first invoice of 499 pts" → actually charged 573.85 pts UX surprise. Add a side-effect-free estimate endpoint that the Skip Trial dialog calls on mount; render the full breakdown (untaxed / tax / total) and gate the confirm button on wallet sufficiency.

**Architecture:** New `_skip_trial_estimate` method on the existing subscription lifecycle mixin computes the breakdown using Odoo's canonical `account.tax.compute_all`. A new `POST /api/cloud-services/services/<id>/skip-trial-estimate` endpoint wraps it. Frontend `SkipTrialModal` loads the estimate on mount via a new `cloudService.getSkipTrialEstimate` method and renders a loading → breakdown → ready confirm flow. The existing `_skip_trial` and its endpoint are unchanged.

**Tech Stack:** Odoo 18 (Python 3.11), OWL.js, no local test runner — backend tests verified post-deploy in `dev-sandbox-stg`.

**Branch:** `develop` (no worktree; tasks must run sequentially).

**Reference design:** `docs/plans/2026-04-28-billing-issue-fix-design.md` § G2.

---

## Out of scope

- G2-B (transaction `bonus_amount` metadata) — separate plan.
- Refactoring the existing `_skip_trial` method.
- Changing how tax is configured (the 15% tax already comes from product / fiscal-position config).
- Frontend test framework — none wired in this repo; backend tests cover the contract.
- Race conditions between estimate and actual skip-trial (wallet could change in between). The actual `_skip_trial` already validates balance at execution time; estimate is informational.

---

## Files & responsibilities

| File | Change |
|---|---|
| `src/models/subscription_lifecycle_mixin.py` | Add `_skip_trial_estimate` method **above** existing `_skip_trial` (around line 234). Same preconditions, no mutations. |
| `src/controllers/paas.py` | Add `api_service_skip_trial_estimate` route **above** existing `api_service_skip_trial` (around line 932). Mirror auth/access. |
| `src/static/src/paas/services/cloud_service.js` | Add `getSkipTrialEstimate(serviceId)` method **after** existing `skipTrial` (around line 221). Mirror its shape. |
| `src/static/src/paas/components/modal/SkipTrialModal.js` | Load estimate in `onMounted`. Expose breakdown getters. Compute `canConfirm`. |
| `src/static/src/paas/components/modal/SkipTrialModal.xml` | Replace the hardcoded "Generate your first invoice of 499 pts" line with a breakdown table (Subtotal / Tax / Total). Gate confirm button on `canConfirm`. Add insufficient-balance hint with top-up link. |
| `src/tests/test_skip_trial_estimate.py` | New: 5 tests covering breakdown, sufficiency, side-effect-free, missing-trial precondition. |
| `src/tests/__init__.py` | Register new test module. |

---

## API contract

`POST /api/cloud-services/services/<id>/skip-trial-estimate` (auth=user, JSON-RPC):

```json
{
  "success": true,
  "data": {
    "untaxed": 499.00,
    "tax_rate": 0.15,
    "tax_amount": 74.85,
    "total": 573.85,
    "currency": "pts",
    "wallet_balance": 1150.00,
    "sufficient": true,
    "shortage": 0.00,
    "billing_cycle_start": "2026-04-28",
    "billing_cycle_end": "2026-05-28"
  }
}
```

Errors return `{ "success": false, "error": "<message>" }`. Same error surfaces as `api_service_skip_trial`:

- "Service not found" — invalid id
- "Admin access required" — caller lacks owner/admin role on workspace
- "No subscription linked to this service." — UserError from mixin
- "Subscription is not active." — UserError from mixin
- "Subscription is not in trial period." — UserError from mixin

---

## Backend method spec

```
def _skip_trial_estimate(self):
    self.ensure_one()
    sub = self.sudo().subscription_id
    # Same three preconditions as _skip_trial:
    #   - sub exists
    #   - sub.state == 'in_progress'
    #   - sub.sh_trial is True
    # All three raise UserError with the same messages as _skip_trial.

    untaxed = sub.sh_plan_price
    taxes = (sub.sh_subscription_plan_id.product_id.taxes_id
             if sub.sh_subscription_plan_id and sub.sh_subscription_plan_id.product_id
             else self.env['account.tax'])

    if taxes:
        compute = taxes.compute_all(
            untaxed,
            partner=sub.sh_partner_id,
            currency=sub.currency_id or self.env.company.currency_id,
        )
        tax_amount = compute['total_included'] - compute['total_excluded']
        tax_rate = (tax_amount / untaxed) if untaxed else 0.0
    else:
        tax_amount = 0.0
        tax_rate = 0.0

    total = untaxed + tax_amount
    today = fields.Date.today()
    delta = {
        'day':   relativedelta(days=sub.sh_recurrency),
        'week':  relativedelta(weeks=sub.sh_recurrency),
        'month': relativedelta(months=sub.sh_recurrency),
        'year':  relativedelta(years=sub.sh_recurrency),
    }.get(sub.sh_unit, relativedelta(months=1))

    wallet_balance = sub.sh_partner_id.wallet_balance
    return {
        'untaxed': float(untaxed),
        'tax_rate': float(tax_rate),
        'tax_amount': float(tax_amount),
        'total': float(total),
        'currency': 'pts',
        'wallet_balance': float(wallet_balance),
        'sufficient': wallet_balance >= total,
        'shortage': float(max(0.0, total - wallet_balance)),
        'billing_cycle_start': today.isoformat(),
        'billing_cycle_end': (today + delta).isoformat(),
    }
```

The implementer is free to refine the field structure if a cleaner factoring emerges, but **the contract above is what the frontend reads** — any rename needs the frontend updated in the same commit.

---

## Frontend modal behavior

States the modal can be in:

| State | Trigger | UI |
|---|---|---|
| `loadingEstimate` | onMounted, before estimate returns | "Loading invoice breakdown…" placeholder; confirm disabled |
| `estimateError` | API returns `success: false` | Inline error message; confirm disabled |
| Loaded + sufficient | Estimate loaded, `wallet_balance >= total` | Breakdown table; confirm enabled |
| Loaded + insufficient | Estimate loaded, `wallet_balance < total` | Breakdown table + warning ("Insufficient: X short, [Top up]"); confirm disabled |
| Skipping | User clicked confirm | Spinner on confirm; cancel still enabled |
| Skip error | `cloudService.skipTrial` returned error | Error banner; confirm re-enabled if fixable |

Reuse existing `state.skipping` and `state.error` for the last two states; add `state.loadingEstimate`, `state.estimateError`, `state.estimate`.

`canConfirm` getter:

```
!state.loadingEstimate && !state.estimateError && state.estimate?.sufficient && !state.skipping
```

The "Top up your wallet" hint should link to `#/billing/topup` (the existing top-up route).

---

## Test cases

Create `src/tests/test_skip_trial_estimate.py`. All tests use `TransactionCase` + `@tagged('post_install', '-at_install')` (same pattern as `test_topup_plan_best_value.py`).

Fixture setup must produce: a partner with a known wallet balance, a subscription in `state='in_progress'` and `sh_trial=True` with `sh_plan_price=499`, linked to a `cloud_service`. Read these existing tests for fixture patterns:

- `src/tests/test_subscription_lifecycle.py` — for the standard subscription-in-trial fixture
- `src/tests/test_change_plan_refund.py` — for partner+wallet fixture
- `src/tests/test_low_balance_notification.py` — for `wallet_balance` writes

If the existing helpers don't quite fit, a small `_make_service_in_trial` helper local to this file is acceptable.

| Test | Setup | Assertion |
|---|---|---|
| `test_estimate_returns_breakdown_with_tax` | wallet=1000, plan=499, taxes configured to 15% | `untaxed==499`, `tax_amount≈74.85`, `total≈573.85`, `currency=='pts'`, `sufficient==True`, `shortage==0` |
| `test_estimate_flags_insufficient_balance` | wallet=100 | `sufficient==False`, `shortage≈473.85`, `wallet_balance==100` |
| `test_estimate_does_not_create_invoice` | wallet=1000 | `account.move` count unchanged before/after |
| `test_estimate_does_not_advance_subscription` | wallet=1000 | `sh_trial` unchanged, `sh_date_of_next_payment` unchanged |
| `test_estimate_raises_when_not_in_trial` | wallet=1000, then set `sh_trial=False` | `UserError` raised |

**If the test fixture setup is genuinely too tangled** (e.g., `sh_subscription` plan creation needs upstream data), simplify to structural assertions only (e.g., `result.keys() == {expected set}`, types are correct, `sufficient` reflects wallet vs. total) and document the limitation. Land what you can.

---

## TDD ordering

We have no local `odoo-bin`. TDD is preserved by **commit ordering inside the single commit**: write the test file first, then the implementation. Both land together in one commit (since we can't run intermediate checkpoints). The test serves as a contract; the post-deploy backend test run in `dev-sandbox-stg` is the verification gate.

---

## Verification gates

1. **Self-review** — implementer traces through each test mentally against the implementation.
2. **Spec review** — independent agent verifies file contents match this plan.
3. **Code quality review** — independent agent assesses the implementation for production readiness.
4. **Post-deploy smoke test** — `kubectl exec` runs the 5 new backend tests + Playwright verifies the modal renders breakdown and gates the confirm button.

---

## Constraints

- **In scope**: 7 files listed above. Single commit.
- **Out of scope**: G2-B bonus metadata, refactoring `_skip_trial`, changing tax configuration.
- **Env**: NO local test runner (`./odoo-bin`, `pytest`, `./scripts/test-addon.sh`), NO asset bundle update (`-u woow_paas_platform`), NO `playwright-cli`, NO DB ops.
- **Commit**: format `<prefix>: <message>`, prefix `feat:` (new endpoint + new behavior), no `Co-Authored-By` footer.
- **Branch**: `develop`, sequential.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `plan.product_id.taxes_id` is empty in test fixture → tax_amount=0 → assertion fails | Document the assumption; if the fixture can't establish a tax, the test for tax breakdown should be skipped or assert structural-only. |
| `account.tax.compute_all` signature differs across Odoo versions | We're on Odoo 18; the signature is stable. Cite Odoo docs in comments if unsure. |
| `sh_partner_id.wallet_balance` is a computed field with caching → estimate sees stale value | Acceptable. The actual `_skip_trial` re-checks at execution. Estimate is informational. |
| `_handle_invoice_creation` post-pay logic might mutate state in ways the estimate doesn't model (e.g., create payment record) | Estimate is intentionally simpler — it doesn't run that path. Post-deploy smoke test is the integration check. |

---

## Acceptance criteria

- [ ] All 5 backend tests pass post-deploy in `dev-sandbox-stg`.
- [ ] Modal opens, shows "Loading invoice breakdown…" briefly, then renders a 3-row table with subtotal / tax / total.
- [ ] Total in the modal exactly matches the actual amount that gets charged when the user confirms.
- [ ] When wallet balance < total, confirm button is disabled and the modal shows shortage hint with "Top up your wallet" link.
- [ ] When wallet balance ≥ total, confirm proceeds as before, generating the invoice.
- [ ] No regression in existing skip-trial flow (existing `_skip_trial` + `api_service_skip_trial` unchanged).
