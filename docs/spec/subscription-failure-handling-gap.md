# Subscription Failure Handling — Feature Gap

**Status**: Gap identified during T19 e2e regression sweep (2026-04-26).
Originally listed as "未實測：連續 6 次失敗 → 自動取消" in `ecpay-test` report.
On code inspection it turned out the path is **not implemented**, hence
non-testable at present. This document captures the gap and a proposed shape.

## Current Behavior

Path: `_sh_create_auto_invoice` (in `src/models/sh_subscription_fix.py`) is the
daily cron that drives recurring billing. When it fires for a due subscription:

1. Build invoice via `_generate_invoice_vals()`.
2. Advance `sh_date_of_next_payment` by `recurrency × unit` **immediately** —
   independent of payment outcome.
3. Wallet covers? → `_handle_invoice_creation` posts + pays from wallet.
4. Wallet short? → `_attempt_auto_topup` charges the bound card.
   - Top-up succeeds → wallet credited → invoice paid.
   - Top-up fails (no token, 3DS needed, `tx.state` not done, …) →
     invoice left **unpaid**; one-per-day low-balance email queued
     (deduped by `woow_last_low_balance_warned_for`).

Subscription state machine:

- `sh.subscription.subscription.state` only mutates through:
  - `sh_subscription_confirm` (→ `in_progress`)
  - upstream `auto_expired` cron (end-date driven; never fires for
    `sh_never_expire=True` plans, which is **all** PaaS plans today)
  - manual `cancel` action
  - `change_subscription_plan_wizard` / `[SH] change_plan` (→ `close`)

There is **no failure counter**, **no retry budget**, and **no auto-cancel
on N consecutive billing failures**. A subscription with a permanently broken
card stays `in_progress` forever, accruing one unpaid invoice per cycle.

## Why This Matters

- **Revenue leakage**: the lifecycle cron only suspends services when sub
  state ∈ `('expire','close','cancel')`. A perpetually-`in_progress` sub
  with unpaid invoices keeps the service running.
- **Cost leakage**: `_do_suspend` / `_do_cleanup` never trigger, so K8s
  resources (Helm releases, tunnels) remain allocated.
- **No backstop on partner intent**: a customer who removes their card
  expecting service termination instead accumulates debt.

## Proposed Implementation

### Schema

Add to `sh.subscription.subscription` (via local extension model):

```python
woow_consecutive_payment_failures = fields.Integer(
    default=0,
    help='Number of consecutive cycles where neither wallet nor auto-topup '
         'could pay the invoice. Reset to 0 on successful payment.',
)
woow_last_payment_failure_at = fields.Datetime(
    help='Timestamp of most recent failed billing attempt.',
)
```

### Threshold (config_parameter)

```
woow_paas_platform.max_consecutive_payment_failures = 6  # default
```

### Cron logic change (`_sh_create_auto_invoice`)

```python
# In the unpaid branch (auto-topup also failed):
subscription.write({
    'woow_consecutive_payment_failures': subscription.woow_consecutive_payment_failures + 1,
    'woow_last_payment_failure_at': fields.Datetime.now(),
})
threshold = int(env['ir.config_parameter'].sudo().get_param(
    'woow_paas_platform.max_consecutive_payment_failures', '6'))
if subscription.woow_consecutive_payment_failures >= threshold:
    _logger.warning(
        '[SUB_FIX] auto_cancel sub=%s — %s consecutive failures',
        subscription.id, subscription.woow_consecutive_payment_failures,
    )
    subscription.write({'state': 'cancel'})
```

In the success branches (wallet paid OR auto-topup recovered) reset:

```python
if subscription.woow_consecutive_payment_failures:
    subscription.write({'woow_consecutive_payment_failures': 0})
```

### Side effects (already covered by lifecycle cron)

Once `state='cancel'`:

- `subscription_lifecycle_cron._enforce_single` → `_do_suspend` (next run).
- After `cleanup_after_months` (default 6) → `_do_cleanup`.

### Open design questions

1. Should the **cycle advance** also be guarded? Today even a permanent
   payment failure still advances `sh_date_of_next_payment`, generating one
   stale unpaid invoice per cycle. Alternative: only advance after a paid
   attempt (or after final cancel).
2. Should the customer get a **distinct** "subscription cancelled" email, or
   reuse `mail_template_low_balance`?
3. Should we retain the old behaviour as a fallback when the threshold param
   is `0` (= disabled), to keep existing test-only deployments working?

### Test plan (once implemented)

T19a — single failure increments counter, doesn't cancel.
T19b — N-1 failures then a success resets counter.
T19c — N consecutive failures → state=cancel, lifecycle cron suspends, email
sent.
T19d — counter respects per-subscription scope (one partner with two subs,
one failing, one paying, only the failing one cancels).
T19e — threshold param = 0 disables the feature (no-op).

## Recommendation

Implement before next material customer onboarding wave. Keeping the current
unbounded retry behaviour silently bleeds compute and skews ARR reporting.
The schema + cron change is ~50 lines, low risk, fully under our control
(no upstream sh_subscription edit needed).
