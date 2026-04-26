# Subscription Failure Handling — Feature Gap

**Status**: Gap identified during T19 e2e regression sweep (2026-04-26).
Originally listed as "未實測：連續 6 次失敗 → 自動取消" in the `ecpay-test`
report. On code inspection it turned out the path is **not implemented**,
hence non-testable at present. This document captures the gap, explains
why it exists, and proposes a concrete shape for the fix.

## Current Behavior

`_sh_create_auto_invoice` (in `src/models/sh_subscription_fix.py`) is the
daily cron that drives recurring billing. When it fires for a due
subscription:

1. Build invoice via `_generate_invoice_vals()`.
2. Advance `sh_date_of_next_payment` by `recurrency × unit` **immediately** —
   independent of payment outcome.
3. Wallet covers? → `_handle_invoice_creation` posts + pays from wallet.
4. Wallet short? → `_attempt_auto_topup` charges the bound card.
   - Top-up succeeds → wallet credited → invoice paid.
   - Top-up fails (no token, 3DS needed, `tx.state` not done, …) → invoice
     left **unpaid**; one-per-day low-balance email queued (deduped by
     `woow_last_low_balance_warned_for`).

Subscription state machine — `sh.subscription.subscription.state` only
mutates through:

- `sh_subscription_confirm` (→ `in_progress`)
- upstream `auto_expired` cron (end-date driven; **never fires** for
  `sh_never_expire=True` plans, which is *all* PaaS plans today)
- manual `cancel` action
- `change_subscription_plan_wizard` / `[SH] change_plan` (→ `close`)

There is **no failure counter**, **no retry budget**, and **no auto-cancel
on N consecutive billing failures**. A subscription with a permanently
broken card stays `in_progress` forever, accruing one unpaid invoice per
cycle.

## Why This Matters

| Consequence | Detail |
|---|---|
| **Revenue leakage** | `subscription_lifecycle_cron` only suspends services when sub state ∈ `('expire','close','cancel')`. A perpetually-`in_progress` sub with unpaid invoices keeps the service running for free. |
| **Cost leakage** | `_do_suspend` / `_do_cleanup` never trigger, so K8s resources (Helm releases, PVCs, Cloudflare tunnels) remain allocated indefinitely. |
| **ARR distortion** | Unpaid invoices accumulate while the sub still counts as active — finance reports overstate recurring revenue. |
| **No backstop on partner intent** | A customer who removes their card expecting service termination instead accumulates debt. |

## Why Upstream Doesn't Have It

Upstream `sh_subscription` has `sh_no_of_billing_cycle` — a *total* cycle
budget, not a *consecutive failure* budget. It assumes the user is on a
fixed-term plan that naturally ends. Our PaaS scenario uses
`sh_never_expire=True` everywhere (subscriptions are indefinite until
cancelled), which exits the fixed-term assumption — leaving no upstream
mechanism to terminate a broken sub. The fix is local to `paas-platform`;
no upstream edit needed.

## Proposed Implementation

### Schema

Add to `sh.subscription.subscription` (via local extension model in
`src/models/sh_subscription_fix.py`):

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
woow_paas_platform.max_consecutive_payment_failures = 6  # default; 0 disables
```

### Cron logic change (`_sh_create_auto_invoice`)

In the unpaid branch (auto-topup also failed):

```python
subscription.write({
    'woow_consecutive_payment_failures':
        subscription.woow_consecutive_payment_failures + 1,
    'woow_last_payment_failure_at': fields.Datetime.now(),
})
threshold = int(env['ir.config_parameter'].sudo().get_param(
    'woow_paas_platform.max_consecutive_payment_failures', '6'))
if threshold and subscription.woow_consecutive_payment_failures >= threshold:
    _logger.warning(
        '[SUB_FIX] auto_cancel sub=%s — %s consecutive failures',
        subscription.id, subscription.woow_consecutive_payment_failures,
    )
    subscription.write({'state': 'cancel'})
```

In the success branches (wallet paid OR auto-topup recovered), reset:

```python
if subscription.woow_consecutive_payment_failures:
    subscription.write({'woow_consecutive_payment_failures': 0})
```

### Side effects (already covered by existing crons)

Once `state='cancel'`, no further work is needed in `_sh_create_auto_invoice`.
The existing `subscription_lifecycle_cron._enforce_single` chain takes over:

| Trigger | Action | Cron |
|---|---|---|
| `state=cancel` detected | `_do_suspend` (scale K8s pods to 0) | next `subscription_lifecycle_cron` run |
| `cancel_date` > `cleanup_after_months` (default 6) | `_do_cleanup` (helm uninstall, release K8s resources) | same cron, later |

So the only thing T19 adds is the **transition into** `cancel`. Suspend +
cleanup are already battle-tested.

## Implementation effort breakdown

| Component | LOC (est.) | File |
|---|---|---|
| Model fields + default | ~10 | `src/models/sh_subscription_fix.py` |
| Cron logic (increment / reset / threshold check) | ~25 | same file |
| Pre-init migration (back-fill 0 on existing subs) | ~5 | `src/migrations/...` |
| Unit tests (5 cases T19a–T19e) | ~150 | `src/tests/test_subscription_failure_handling.py` |
| Doc update (CHANGELOG, possibly user-facing docs) | ~10 | `CHANGELOG.md` |
| **Total** | **~200** | |

Estimated effort: **1–2 hours** including local verification.

Risk: **low** — pure additions, no edits to existing payment paths. Behaviour
is gated by `threshold > 0`, so test-only deployments can opt out by
setting the param to `0`.

## Test plan

| # | Scenario | Setup | Expected |
|---|---|---|---|
| **T19a** | Single failure increments counter, doesn't cancel | empty wallet, no card; run cron once | `failures=1`, `state=in_progress` |
| **T19b** | N-1 failures then a success resets counter | run cron N-1 times empty, then top up wallet, run cron | `failures=0`, `state=in_progress`, invoice paid |
| **T19c** | N consecutive failures → cancel + suspend | run cron N times empty | `state=cancel`; next lifecycle_cron triggers `_do_suspend` |
| **T19d** | Per-sub scope (failures don't bleed across subs) | one partner, two subs A (failing) + B (paying), N cycles | only A cancels; B stays `in_progress` |
| **T19e** | `threshold=0` disables the feature | set param to 0, run cron N+1 times empty | `state` remains `in_progress`, counter still increments (informational only) |

## Open design questions

1. **Should the cycle advance also be guarded?** Today even a permanent
   payment failure still advances `sh_date_of_next_payment`, generating one
   stale unpaid invoice per cycle. Alternative: only advance after a paid
   attempt (or after final cancel). Simpler to leave as-is for v1.
2. **Should the customer get a distinct "subscription cancelled" email**,
   or reuse `mail_template_low_balance`? Recommendation: new template,
   different copy ("your service has been suspended due to repeated
   failures"); reuse the same dedup field.
3. **Should `threshold=0` mean "disabled" (current proposal) or "infinite"
   (never cancel)?** Disabled is more useful for test environments.

## Recommendation

Implement before next material customer onboarding wave. The current
unbounded retry behaviour silently bleeds compute and skews ARR reporting.
~200 LOC, ~1–2 hours, low risk, fully local to `paas-platform`.

When implementing, the existing `subscription_lifecycle_cron` is the
contract — T19 only needs to flip state to `cancel`; the rest of the
suspend/cleanup machinery is already verified end-to-end (T22 Phase B,
2026-04-26).
