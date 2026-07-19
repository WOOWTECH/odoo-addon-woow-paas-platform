---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-28-sprint1-g1-points-formatter.md
archived: 2026-07-19
archived-reason: "一次性 sprint 微任務，已落地"
---

# G1: Unified Points Formatter — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate three issues at once by introducing a single canonical formatter for points/credits and sweeping every billing-area display:
- **Issue #2**: invoice total renders as `574 pts` in lists and `573.85 pts` in detail subtotal — same field shown with different precision.
- **Issue #3**: `$`, `pts`, `Points`, `NT$`, `Credits` are mixed across header / cards / lists / Top-up.
- **Issue #6**: subscription debit transactions show `$573.85` (no negative sign) while top-ups show `+$1,100.00`; users can't tell debit from credit at a glance.

**Architecture:** A pure utility `formatPoints(value, opts)` in `billing_helpers.js` replaces the existing pair of `formatAmount` / `formatAmountDecimal`. All display sites switch to it. Convention: **always two decimals, always `pts` suffix, optional sign prefix for transaction rows**. The Top-up Store retains `NT$` as the purchase-currency prefix (different concept — fiat input vs. point output) but renders the resulting credits via `formatPoints`. The header CREDITS indicator drops its `$` prefix.

**Tech Stack:** OWL.js / vanilla JS. Pure frontend (no backend or migration needed). No local test runner — backend tests verified post-deploy via `kubectl exec`. No frontend test framework wired; rely on careful code review + post-deploy Playwright smoke.

**Branch:** `develop` (sequential).

**Reference design:** `docs/plans/2026-04-28-billing-issue-fix-design.md` § G1.

---

## Out of scope

- A `<WoowPoints>` OWL component. The original design doc proposed one, but in practice every call site already wraps the formatted string with surrounding context (e.g. `<strong>X pts</strong>`, `Subtotal: X pts`, `+X pts` with semantic color decided by parent template). A pure utility is more flexible and avoids forcing every site to refactor its surrounding markup. **If a future call site genuinely benefits from a packaged component, add it then.**
- Removing the `formatAmount` / `formatAmountDecimal` helpers. They're called from 8+ files; deprecating them is a separate cleanup. Re-implement them in terms of `formatPoints` so behavior stays consistent, then leave as thin wrappers for backward compatibility.
- Localizing strings (e.g. `"pts"` translated to `"點"` for zh-TW). The codebase has no `_t` infrastructure on the frontend; add later.
- Refactoring the Top-up flow's NT$ → pts conversion display beyond what the issue requires (i.e. labels stay readable).
- Backend changes (the issue is purely how the frontend displays existing data).

---

## New utility

`src/static/src/paas/utils/billing_helpers.js`:

```js
/**
 * Format a points / credits value for display.
 *
 * Conventions:
 * - Always two decimals (eliminates "574 vs 573.85" precision mismatch).
 * - Always `pts` suffix (replaces the prior $ / Points / Credits zoo).
 * - Optional `+` / `-` sign prefix for transaction rows so debits and
 *   credits are visually distinct.
 *
 * Examples:
 *   formatPoints(573.85)                   → "573.85 pts"
 *   formatPoints(573.85, { signed: true }) → "+573.85 pts"
 *   formatPoints(-573.85, { signed: true }) → "-573.85 pts"
 *   formatPoints(0, { signed: true })      → "0.00 pts"  (no sign on zero)
 *   formatPoints(null)                     → "0.00 pts"
 *   formatPoints(1234.5, { precision: 0 }) → "1,235 pts" (rounded)
 *
 * @param {number|null|undefined} value
 * @param {{signed?: boolean, precision?: number}} [opts]
 * @returns {string}
 */
export function formatPoints(value, { signed = false, precision = 2 } = {}) {
  const n = Number(value ?? 0);
  const abs = Math.abs(n);
  const body = abs.toLocaleString(undefined, {
    minimumFractionDigits: precision,
    maximumFractionDigits: precision,
  });
  let prefix = "";
  if (signed && n > 0) prefix = "+";
  else if (n < 0) prefix = "-";
  return `${prefix}${body} pts`;
}
```

`formatAmount` and `formatAmountDecimal` are kept but rewritten to use `formatPoints` internally, so any caller we miss in the sweep still produces correct values:

```js
/** @deprecated use formatPoints(amount, {precision: 0}) directly */
export function formatAmount(amount) {
  // Preserve historical contract: no suffix, no decimals.
  return Number(amount ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  });
}

/** @deprecated use formatPoints(amount) directly */
export function formatAmountDecimal(amount) {
  // Preserve historical contract: no suffix, two decimals.
  return Number(amount ?? 0).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}
```

(They lose the `pts` suffix because their original API never had one — adding it now would break templates that hardcode `<t t-esc="formatAmount(...)"/> pts` after the call. The sweep replaces those templates anyway.)

---

## Sweep list

Each row is one site that today displays a points value. After the sweep, every site reads from `formatPoints` and produces a uniform `[+|-]X.XX pts` string (or `NT$ X` for the purchase-currency exception in Top-up).

### Group A — fixes Issue #2 (precision drift)

| File | Line | Before | After |
|---|---|---|---|
| `src/static/src/paas/pages/billing/BillingOverviewPage.xml` | 178 | `formatAmount(invoice.amount_total) pts` | `formatPoints(invoice.amount_total)` |
| `src/static/src/paas/pages/billing/InvoiceListPage.xml` | 87 | `formatAmount(invoice.amount_total) pts` | `formatPoints(invoice.amount_total)` |
| `src/static/src/paas/pages/billing/InvoiceDetailPage.xml` | 43-45 | `formatAmount(invoice.amount_total)` + adjacent `Points` span | `formatPoints(invoice.amount_total)` (drop `Points` span) |
| `src/static/src/paas/pages/billing/InvoiceDetailPage.xml` | 131,139,150,154,158 | `formatAmountDecimal(...)` | `formatPoints(...)` |

Once all five touch the same `formatPoints`, the 574 / 573.85 split disappears.

### Group B — fixes Issue #3 (unit mixing)

| File | Line | Before | After |
|---|---|---|---|
| `src/static/src/paas/layout/header/Header.js` | 30-32 | `` `$${this.walletBalance.toLocaleString("en-US", {min:2,max:2})}` `` | `formatPoints(this.walletBalance)` |
| `src/static/src/paas/pages/billing/BillingOverviewPage.xml` | 56 | `<span>Points</span>` (separate label) | merge into the value via `formatPoints(...)`, drop the standalone `Points` span |
| `src/static/src/paas/pages/billing/BillingOverviewPage.xml` | 31 | `formatAmountDecimal(lowBalanceShortfall) pts` | `formatPoints(lowBalanceShortfall)` |
| `src/static/src/paas/pages/billing/TopupStorePage.xml` | 38 | `<span>Points</span>` | drop standalone span; render value via `formatPoints` |
| `src/static/src/paas/pages/billing/TopupStorePage.xml` | 93 | `formatAmount(plan.credits)` (the credits-on-card display) | leave alone — this is the Top-up "you'll get N credits" label, where the unit is intentionally implicit (the surrounding card already says "Credits"). **Decision**: don't break this for unit unification; the design of the Top-up card is the one place a non-`pts` label is appropriate. |
| `src/static/src/paas/pages/billing/TopupStorePage.xml` | 113 | `$<t t-esc="formatAmount(plan.amount)"/>` | `NT$<t t-esc="formatAmount(plan.amount)"/>` |
| `src/static/src/paas/components/modal/PayInvoiceModal.xml` | 31 | `<t t-esc="formattedBalance"/> pts` (where `formattedBalance` is from Header.js) | leave the template; update `formattedBalance` getter on the modal (or have it use `formatPoints` directly) — verify by reading the modal |
| `src/static/src/paas/components/modal/CreditHistoryModal.xml` | 19 | `<t t-esc="formattedBalance"/>` (no unit suffix) | `<t t-esc="formatPoints(walletBalance)"/>` (replace getter with explicit formatPoints) |

Acceptance: a fresh `grep -rn "\\\$\\\${\\|formatAmount\\|formatAmountDecimal\\|>Points<\\|>Credits<" src/static/src/paas/` returns either zero hits OR only the explicitly-allowed exceptions (NT$ prefix in Top-up purchase price; legacy helpers if they survive).

### Group C — fixes Issue #6 (debit sign)

| File | Function | Before | After |
|---|---|---|---|
| `src/static/src/paas/pages/billing/TransactionListPage.js` | `formatAmount(txn)` (line 76-87) | manual `+`/`-` prefix construction with `formatAmountDecimal` | `formatPoints(txn.amount, { signed: true })` |
| `src/static/src/paas/components/modal/CreditHistoryModal.xml` (and `.js`) | the per-row amount cell | confirm same call site uses `formatPoints(txn.amount, { signed: true })` | (read both, change accordingly) |

Background: `TransactionListPage.js:76-87` already implements signed display, but in a bespoke way that disagrees with how Top-up rows render in the same view (top-ups show `+$1,100.00` from the controller's pre-formatted string; subscription debits show `$573.85` with no sign). Switching to `formatPoints(amount, { signed: true })` produces `+1,100.00 pts` and `-573.85 pts` consistently, driven from the same numeric `amount` field.

### Group D — minor consistency (no issue, but worth touching while sweeping)

| File | Line | Note |
|---|---|---|
| `src/static/src/paas/pages/billing/SubscriptionListPage.js` | 127 (`formatAmount(price)`) | wraps existing `formatAmount`. Switch to `formatPoints(price)` so the subscription card's price display also adopts the unified suffix. |
| `src/static/src/paas/components/modal/SkipTrialModal.xml` | 53,61,67,73,85 | already uses `.toFixed(2) pts` — switch to `formatPoints(...)` for uniform formatting (commas in thousands separator, etc.). Visual change: `1234.50 pts` → `1,234.50 pts`. |

---

## Tests

Add `src/tests/test_format_points.py` — but wait, this is a JS utility. The repo has no frontend test framework wired (per prior plans).

**Decision**: skip JS unit tests for this task. The function is small enough to verify by reading + post-deploy Playwright smoke tests:

- After deploy, navigate to the Transaction List page and assert:
  - subscription debit row shows `-573.85 pts` (negative, two decimals, `pts` suffix).
  - top-up row shows `+1,100.00 pts` (positive sign, two decimals).
  - top-up with bonus row shows the description with breakdown (G2-B regression check).
- Navigate to Invoice List and assert the amount column shows `573.85 pts` (two decimals, no sign).
- Navigate to Invoice Detail and assert the total shows `573.85 pts` (matching list — no more 574 vs 573.85 split).
- Header CREDITS shows `576.15 pts` (no `$` prefix, `pts` suffix).
- Top-up Store shows `NT$ 1000` for the purchase price and `1,000 Credits` for the resulting credits (purchase currency exception preserved).

Document these checks as a smoke-test checklist in the implementer's report.

If a frontend unit-test framework is later wired, the formatter is the obvious first thing to lock down with explicit cases.

---

## Constraints

- **Single commit**: the sweep is large but atomic. A `git revert` should restore the prior state in one shot.
- **No backend changes** in this task. (Backend already returns numeric `amount` / `wallet_balance` values; we just change presentation.)
- **No new dependencies**.
- **Manifest version NOT bumped** because there's no DB migration.
- **Commit message format**: `<prefix>: <message>`, prefix `refactor:` (consolidating display logic; no new behavior).
- **Env**: NO `./odoo-bin`, NO `playwright-cli` during implementation, NO DB ops.

---

## Acceptance criteria

- [ ] `grep -rn "formatAmountDecimal\|formatAmount(" src/static/src/paas/` returns either zero hits in templates (`.xml`) and JS call sites OR only documented backward-compat shims.
- [ ] `grep -rn "\\\$\\\${\\|>Points<\\|>Credits<" src/static/src/paas/` returns zero hits except the Top-up purchase price (`NT$<...>`).
- [ ] All 5 group-A sites display the same precision for the same invoice (no 574 vs 573.85 split).
- [ ] Header CREDITS shows `X.XX pts` format (no `$` prefix).
- [ ] Transaction list subscription debit row shows `-573.85 pts`; top-up row shows `+1,100.00 pts`.
- [ ] No regression in G2-A Skip Trial breakdown, G2-B Top-up bonus rendering, G3 BEST VALUE badge, or G4-P0 Dashboard placeholder.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Sweep misses a site → mixed display persists | Acceptance criterion 1 grep + post-deploy smoke test catches the most visible misses. |
| Some template uses `formatAmount(...) + " pts"` literal — switching to `formatPoints` doubles the suffix | Read each template before editing; the plan's sweep table calls out which sites have explicit `pts` text after the helper. |
| Browser caches old asset bundle | Same as G2-B: clear `ir_attachment` rows for `woow_paas_platform.assets_paas` post-deploy. |
| `formatPoints(null)` returns `"0.00 pts"` instead of `"-"` (placeholder) | Acceptable: rendering 0 explicitly is more accurate than a dash that hides "amount unknown". If a template wants a placeholder, it can use `t-if`. |
| Locale: `toLocaleString(undefined, ...)` uses browser locale; `1,234.50` in en-US, `1.234,50` in de-DE | The codebase already uses this pattern (in `formatAmount`/`formatAmountDecimal`); preserves existing behavior. |
