---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-28-sprint2-g2b-transaction-bonus-metadata.md
archived: 2026-07-19
archived-reason: "一次性 sprint 微任務，已落地"
---

# G2-B: Transaction Bonus Metadata — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make wallet transaction history show top-up bonuses explicitly. Today, a `NT$ 1000 → +1,100 pts` row is recorded as a single `amount: 1100`, hiding the `+100 bonus`. After this change, the row carries `base_amount=1000` and `bonus_amount=100` as structured fields, and the frontend renders `Top-up: NT$ 1000 + 100 bonus = 1,100 pts`.

**Architecture:** Inherit `wallet.transaction` (defined in upstream `sh_subscription_wallet`) and add two `Float` fields. Hook into the existing `wallet_topup._on_payment_done` flow at the point where it already searches and links `wallet_transaction_id` (line 99-103) — write the structured breakdown then. A versioned migration backfills the new fields on existing top-up rows by joining to `woow_wallet_topup`. Controller adds the two keys to the transaction list payload; frontend reads them and renders the breakdown when `bonus_amount > 0`.

**Tech Stack:** Odoo 18 (Python 3.11), OWL.js, post-deploy verification (no local `odoo-bin`).

**Branch:** `develop` (sequential).

**Reference design:** `docs/plans/2026-04-28-billing-issue-fix-design.md` § G2.

---

## Out of scope

- Backfilling rows that have no `wallet_topup` record (e.g., direct `add_wallet_points` calls outside the top-up flow) — these will keep `base_amount=0`, `bonus_amount=0`, and the frontend falls back to the unchanged `amount` display. Acceptable: the only call sites that produce non-zero bonuses are top-up flows.
- Subscription debit / refund / card-charge transactions — they have no bonus concept; the new fields stay 0. Frontend renders unchanged for these `transaction_type` values.
- Refactoring `add_wallet_points` to accept `base_amount`/`bonus_amount` parameters (would require an upstream change). We post-process the created transaction record instead.
- Adding new admin views to expose the fields (the data-hygiene improvement is internal; surface via API only for now).

---

## Files & responsibilities

| File | Change |
|---|---|
| `src/models/wallet_transaction.py` | NEW. Inherit `wallet.transaction`, add `base_amount` + `bonus_amount` Float fields. |
| `src/models/__init__.py` | Register the new module import. |
| `src/models/wallet_topup.py` | After the existing `wtx` search (line 99-103), write `{'base_amount': self.credits, 'bonus_amount': self.bonus}` onto it before the `self.write({'state':'done', ...})`. |
| `src/controllers/billing.py` | `api_transactions` response: add `'base_amount'` + `'bonus_amount'` keys to each row dict. |
| `src/static/src/paas/utils/billing_helpers.js` | New helper `formatTopupDescription(txn)` that returns `Top-up: NT$ 1000 + 100 bonus = 1,100 pts` when `bonus_amount > 0`, else falls back to `txn.description`. |
| `src/static/src/paas/pages/billing/TransactionListPage.xml` (and/or `.js`) | Use the helper to render top-up rows. |
| `src/static/src/paas/components/modal/CreditHistoryModal.xml` (and/or `.js`) | Same as TransactionListPage. |
| `src/__manifest__.py` | Bump version `18.0.1.0.16` → `18.0.1.0.17`. |
| `src/migrations/18.0.1.0.17/post-migrate.py` | NEW. Backfill `base_amount`/`bonus_amount` on existing top-up rows by joining to `woow_wallet_topup`. |
| `src/tests/test_wallet_transaction_bonus.py` | NEW. 5 tests covering the new fields + API + migration shape. |
| `src/tests/__init__.py` | Register new test module. |

Total: 4 new files, 6 modified files.

---

## Field & migration design

### New fields on `wallet.transaction`

```python
base_amount = fields.Float(
    string='Base Amount',
    help=(
        "For top-up rows: the base credits before bonus. "
        "Always 0 for non-topup transaction types and for legacy "
        "rows that predate this field."
    ),
)
bonus_amount = fields.Float(
    string='Bonus Amount',
    help=(
        "For top-up rows: the bonus credits added on top of base. "
        "Always 0 when no bonus applies or for non-topup types."
    ),
)
```

`base_amount + bonus_amount` should equal `amount` for top-up rows after the migration runs and after the `_on_payment_done` hook fires for new top-ups. We do NOT add a constraint enforcing this — fragility outweighs benefit; the frontend treats both fields independently.

### Migration

`src/migrations/18.0.1.0.17/post-migrate.py` follows the project pattern from `18.0.1.0.13` and `18.0.1.0.16`:

```python
"""G2-B: backfill base_amount and bonus_amount on existing top-up rows.

The new fields default to 0. For top-ups recorded before this migration,
join wallet.transaction.id ← woow_wallet_topup.wallet_transaction_id to
recover the original credits + bonus values from the topup record.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        UPDATE wallet_transaction wt
        SET base_amount  = topup.credits,
            bonus_amount = topup.bonus
        FROM woow_wallet_topup topup
        WHERE topup.wallet_transaction_id = wt.id
          AND wt.transaction_type = 'topup'
          AND (wt.base_amount IS NULL OR wt.base_amount = 0)
          AND (wt.bonus_amount IS NULL OR wt.bonus_amount = 0)
        """
    )
    _logger.info(
        "G2-B migration: backfilled base_amount/bonus_amount on %s top-up rows",
        cr.rowcount,
    )
```

**Idempotency**: the `WHERE base_amount IS NULL OR base_amount = 0` clause means re-running `-u` after the first execution updates 0 rows. Safe.

**Limitation**: top-ups whose `wallet_transaction_id` was never set (because the topup hit a non-success state path that bypassed `_on_payment_done`) will not be backfilled. They'll show `base_amount=0`, `bonus_amount=0`, and the frontend falls back to the old display. Acceptable.

---

## Hook into `_on_payment_done`

Existing code (line 99-103 in `wallet_topup.py`):

```python
wtx = self.env['wallet.transaction'].search([
    ('payment_transaction_id', '=', tx.id),
    ('transaction_type', '=', 'topup'),
], limit=1)
self.write({'state': 'done', 'wallet_transaction_id': wtx.id})
```

Insert the breakdown write between the search and the state flip:

```python
wtx = self.env['wallet.transaction'].search([
    ('payment_transaction_id', '=', tx.id),
    ('transaction_type', '=', 'topup'),
], limit=1)
if wtx:
    wtx.write({
        'base_amount': self.credits,
        'bonus_amount': self.bonus,
    })
self.write({'state': 'done', 'wallet_transaction_id': wtx.id})
```

The existing `if wtx:` shape is implied by `wtx.id` already being read on the next line (which would crash if wtx is empty); keep the same defensive level.

---

## API response

`src/controllers/billing.py` `api_transactions` response (around line 144-165 per the explore map):

Add to the row dict:

```python
data.append({
    'id': txn.id,
    'date': txn.create_date.isoformat() if txn.create_date else None,
    'description': txn.description or txn.name or '',
    'transaction_type': 'credit' if txn.amount >= 0 else 'debit',
    'amount': txn.amount,
    'balance_after': txn.balance_after,
    'base_amount': txn.base_amount or 0.0,
    'bonus_amount': txn.bonus_amount or 0.0,
})
```

If there are sibling endpoints in the same controller that return transaction rows (e.g., a "recent transactions" endpoint serving CreditHistoryModal), apply the same addition there. Search `controllers/billing.py` for `wallet.transaction` / `'amount': txn.amount` to find them.

---

## Frontend rendering

A small utility in `src/static/src/paas/utils/billing_helpers.js`:

```js
/**
 * Render a top-up description with bonus breakdown when applicable.
 * Falls back to the existing description for non-topup or legacy rows.
 *
 * Examples:
 *   { description: "Top-up: NT$ 1000", base_amount: 1000, bonus_amount: 100 }
 *     → "Top-up: NT$ 1000 + 100 bonus = 1,100 pts"
 *   { description: "Top-up: NT$ 50",   base_amount: 50,   bonus_amount: 0   }
 *     → "Top-up: NT$ 50"
 *   { description: "Subscription invoice INV/2026/00005", base_amount: 0, bonus_amount: 0 }
 *     → "Subscription invoice INV/2026/00005"
 */
export function formatTopupDescription(txn) {
  if (!txn) return "";
  const bonus = txn.bonus_amount || 0;
  if (bonus <= 0) return txn.description || "";
  const base = txn.base_amount || 0;
  const total = base + bonus;
  return `${txn.description} + ${bonus.toLocaleString()} bonus = ${total.toLocaleString()} pts`;
}
```

(The exact format string can be refined — what matters is that bonus/base/total are all visible.)

`TransactionListPage.xml` and `CreditHistoryModal.xml` change their description cell from `t-esc="txn.description"` to a getter that calls `formatTopupDescription(txn)`. Read each file first to see the exact element structure; the smallest possible change is a getter on the component class plus `t-esc="formatTopupDescription(txn)"`.

If both files share a row template, factor out a `TransactionRow` component — but only if the natural diff is small. Don't refactor for refactor's sake.

---

## Test cases

`src/tests/test_wallet_transaction_bonus.py`, all `TransactionCase` + `@tagged('post_install', '-at_install')`:

| Test | Setup | Assertion |
|---|---|---|
| `test_topup_creates_transaction_with_bonus_metadata` | Create `woow.wallet.topup` with `credits=1000`, `bonus=100`. Drive `_on_payment_done` (or simulate the linked `payment.transaction.state='done'` flow). | The created `wallet.transaction` has `base_amount=1000`, `bonus_amount=100`, `amount=1100`. |
| `test_topup_zero_bonus_records_zero_bonus` | Same but `bonus=0`. | `base_amount=N`, `bonus_amount=0`. |
| `test_non_topup_transaction_has_zero_metadata` | Call `partner.deduct_wallet_points(...)` (subscription debit). | The created `wallet.transaction` row (transaction_type='deduct') has `base_amount=0`, `bonus_amount=0`. |
| `test_api_includes_breakdown_keys` | Top-up + non-topup rows. Call `/api/billing/transactions` via HttpCase or invoke the controller method directly. | Each row dict has the keys `'base_amount'` and `'bonus_amount'`. |
| `test_migration_backfills_legacy_topups` | Create a `woow_wallet_topup` (credits=500, bonus=30) wired to a `wallet.transaction` whose `base_amount`/`bonus_amount` are 0 (simulate pre-migration state). Run the migration SQL directly. | After migration, the transaction has `base_amount=500`, `bonus_amount=30`. |

The fifth test exercises the migration SQL by calling `cr.execute` against the same query the migration uses, then asserting the row updated. It does NOT execute the migration file itself (Odoo runs migrations only on actual `-u`).

If the existing test infrastructure makes any specific test's fixture setup intractable, **simplify rather than skip** — e.g., assert structural properties of the new field rather than full numeric equality.

---

## Constraints

- **Env**: no local test runner; verification via post-deploy `kubectl exec` + Playwright UI smoke test.
- **No `Co-Authored-By`** footer; commit prefix `feat:` (new behavior + new fields).
- **Single commit**: bundle all changes (new model, hook update, API, frontend, manifest bump, migration, tests, registration) so a `git revert` can undo G2-B atomically.
- **In scope**: 10 files listed above. **Out of scope**: refactoring `add_wallet_points`, adding admin views.

---

## Verification gates

1. **Self-review** — implementer traces each test mentally; verifies migration SQL is idempotent.
2. **Spec review** — independent agent verifies the 10 files match this plan.
3. **Code quality review** — independent agent assesses production readiness; flags any concerns about migration race conditions, multi-company scoping, or i18n.
4. **Post-deploy** — backend tests run via `kubectl exec`; Playwright opens Credit History modal and asserts a specific top-up row shows the bonus suffix.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `wallet.transaction.create` triggers from non-topup paths might not set the new fields, leaving them 0 forever | Acceptable. Frontend falls back to `description` when `bonus_amount<=0`. The migration is just a one-shot for existing top-up rows; new rows are handled by `_on_payment_done`. |
| Multiple `_on_payment_done` calls for the same `tx.id` (idempotency check at line 76 prevents this) | Already guarded — re-entry returns early. The new write won't double-up. |
| Migration sees a row whose `base_amount` was manually set to non-zero in the dev DB | The `WHERE` clause skips it. Idempotency holds. |
| `formatTopupDescription` runs on a transaction without `description` | Returns `""`; the frontend shows an empty cell. Acceptable, matches today's behavior. |
| Frontend caching: `TransactionListPage` was loaded before deploy; user sees old description format until reload | Same constraint as G2-A; force asset bundle regen post-deploy if needed. |
| `wallet.transaction.amount` could disagree with `base_amount + bonus_amount` if a top-up is partially refunded later | Out of scope. The fields are descriptive of the original transaction; refunds create their own rows with `transaction_type='refund'`. |

---

## Acceptance criteria

- [ ] Backend: 5 tests pass post-deploy in `dev-sandbox-stg`.
- [ ] Migration logs `G2-B migration: backfilled ... rows` with a non-negative count.
- [ ] DB: existing top-up rows now have non-zero `base_amount`/`bonus_amount` (verifiable via `psql`).
- [ ] API: `/api/billing/transactions` rows include the two new keys.
- [ ] Frontend: TransactionListPage and CreditHistoryModal show `Top-up: NT$ 1000 + 100 bonus = 1,100 pts` for the existing 1000-credit top-up row that has bonus=100.
- [ ] Frontend: a 50-credit top-up row (no bonus) renders unchanged.
- [ ] Frontend: a subscription debit row (`transaction_type='deduct'`) renders unchanged.
- [ ] No regression in G2-A or earlier hotfix bundle (Skip Trial modal still works; subscription Manage button still works).
