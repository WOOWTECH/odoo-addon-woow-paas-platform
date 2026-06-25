---
title: Subscription Failure Handling
category: reference
implementation_status: implemented
note: "Originally a documented gap (T19); the dunning path is now implemented — see frontmatter status note below and src/models/sh_subscription_fix.py."
---

# Subscription Failure Handling — Feature Gap

**Status**: **Implemented** (verified 2026-05-29). Originally a gap
identified during the T19 e2e regression sweep (2026-04-26) — listed as
"未實測：連續 6 次失敗 → 自動取消" in the `ecpay-test` report and, on the
then-current code, **not implemented**. The dunning path has since been
built: the consecutive-failure counter, threshold-driven auto-cancel
(default 6) and one-shot cancellation email all live in
`src/models/sh_subscription_fix.py` (`_sh_create_auto_invoice` →
`_on_payment_failure`), with the schema fields in
`src/models/sh_subscription_warning_field.py`, the back-fill in
`src/migrations/18.0.1.0.13/post-migrate.py`, and the
`woow_paas_platform.max_consecutive_payment_failures` config_parameter
registered for cleanup in `src/hooks.py`. The Decisions and Proposed
Implementation sections below are retained as the design record that the
shipped code follows.

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

## Decisions

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Guard the cycle advance? | **Yes** — only advance `sh_date_of_next_payment` on success. Failed cron also skips creating a duplicate invoice if an unpaid one already exists for the current period. | Without the guard each cycle produces another stale unpaid invoice on top of the previous one; AR ledger gets noisy fast. The guard collapses "1 invoice + N retries" into the cleaner shape and removes the late-payment ambiguity (see Decision 4). |
| 2 | Email template at cancel? | **New** template `mail_template_subscription_cancelled` (separate from `mail_template_low_balance`). Different copy ("repeated payment failures → service cancelled"). Use a **new** dedup field `woow_cancellation_notified` so we never spam if a state transition retriggers. | Customer-facing finality is a different message from "your wallet is low"; sharing one template would overload its meaning. |
| 3 | `threshold = 0` semantics | **Disabled** (no auto-cancel; sub stays `in_progress` forever). | More useful for test/staging deployments that don't want dunning side effects; "infinite" is the same as "very large", which is already expressible via a big number. |
| 4 | Recovery model: catch-up vs. forgiveness vs. hard-cancel | **Hard-cancel (Option C)** — short grace period (default 6 days), then auto-cancel; no in-place late recovery. To return to service, the customer creates a fresh subscription. K8s data is preserved by `_do_suspend` for `cleanup_after_months` (default 6 months) so a re-subscribe can re-link existing storage. | Catch-up billing is operationally complex and surprises customers with bulk charges. Forgiveness leaks revenue (free service during the grace period). Hard-cancel matches modern SaaS norms (Stripe / AWS / GCP all do ~7–14d grace then suspend), keeps the model simple, and re-uses already-verified suspend/cleanup machinery. |

### Closed gap from earlier draft (cron filter)

Earlier drafts left the cron's due-day filter as `sh_date_of_next_payment ==
today`. With Decision 1 (no advance on failure), that filter would only
fire once on Day 0 and then skip the sub forever. Implementation must
broaden the filter to **`<=`** so a stuck due-date keeps re-entering the
loop until either resolution (paid) or auto-cancel.

## Proposed Implementation

### Schema

Add to `sh.subscription.subscription` (via local extension model in
`src/models/sh_subscription_fix.py`):

```python
woow_consecutive_payment_failures = fields.Integer(
    default=0,
    help='Number of consecutive cron ticks where neither wallet nor '
         'auto-topup could settle the outstanding invoice. Reset to 0 on '
         'successful payment.',
)
woow_last_payment_failure_at = fields.Datetime(
    help='Timestamp of most recent failed billing attempt.',
)
woow_cancellation_notified = fields.Boolean(
    default=False,
    help='Set when the auto-cancellation email has been queued, so a '
         'state-transition retrigger does not double-send.',
)
```

### Threshold (config_parameter)

```
woow_paas_platform.max_consecutive_payment_failures = 6  # default; ≈ 6 days
                                                          # of daily-cron retries
                                                          # 0 disables (no auto-cancel)
```

The unit is **cron ticks** (≈ days, since the cron is daily). 6 is a common
SaaS dunning window; ops can tune via `ir_config_parameter` per environment
without code change.

### Cron logic change (`_sh_create_auto_invoice`)

Top-level filter must broaden the due-day check (see "Closed gap" above):

```python
subscriptions = env['sh.subscription.subscription'].sudo().search([
    ('state', '=', 'in_progress'),
    ('sh_date_of_next_payment', '<=', fields.Date.today()),
])
```

Pseudocode for the per-subscription block:

```python
today = fields.Date.today()

# Decision 1: re-use the existing unpaid invoice instead of creating a
# duplicate when a previous tick already produced one for this cycle.
existing_unpaid = self.env['account.move'].sudo().search([
    ('sh_subscription_id', '=', subscription.id),
    ('payment_state', 'not in', ('paid', 'in_payment')),
    ('state', '!=', 'cancel'),
], order='invoice_date desc', limit=1)

invoice = existing_unpaid or self.env['account.move'].sudo().create(
    subscription._generate_invoice_vals()
)

paid = _try_pay(subscription, invoice)  # wallet → auto-topup → True/False

if paid:
    # Decision 1: only advance on success.
    delta = unit_deltas.get(subscription.sh_unit)
    if delta:
        subscription.sh_date_of_next_payment = today + delta
    if subscription.woow_consecutive_payment_failures:
        subscription.woow_consecutive_payment_failures = 0
    continue

# --- failure path ---
subscription.write({
    'woow_consecutive_payment_failures':
        subscription.woow_consecutive_payment_failures + 1,
    'woow_last_payment_failure_at': fields.Datetime.now(),
})

threshold = int(env['ir.config_parameter'].sudo().get_param(
    'woow_paas_platform.max_consecutive_payment_failures', '6'))
if threshold and subscription.woow_consecutive_payment_failures >= threshold:
    _logger.warning(
        '[SUB_FIX] auto_cancel sub=%s after %s consecutive failures',
        subscription.id, subscription.woow_consecutive_payment_failures,
    )
    subscription.write({'state': 'cancel'})
    if not subscription.woow_cancellation_notified:
        self._notify_subscription_cancelled(subscription, invoice)
        subscription.write({'woow_cancellation_notified': True})
```

### Email template (Decision 2)

New file `src/data/mail_template_subscription_cancelled.xml`, registered in
`__manifest__.py`. Mirror the structure of `mail_template_low_balance` but
with finality copy:

```xml
<record id="mail_template_subscription_cancelled" model="mail.template">
    <field name="name">Subscription Cancelled — Repeated Payment Failures</field>
    <field name="model_id" ref="base.model_res_partner"/>
    <field name="subject">[Woow PaaS] Your subscription has been cancelled</field>
    <field name="body_html" type="html">…</field>
</record>
```

A `_notify_subscription_cancelled(subscription, invoice)` helper renders
and queues it, mirroring `_notify_low_balance` (already in
`sh_subscription_fix.py`).

### Side effects (already covered by existing crons)

Once `state='cancel'`, no further work is needed in `_sh_create_auto_invoice`
(the search filter remains `state='in_progress'`). The existing
`subscription_lifecycle_cron._enforce_single` chain takes over:

| Trigger | Action | Cron |
|---|---|---|
| `state=cancel` detected | `_do_suspend` (scale K8s pods to 0) | next `subscription_lifecycle_cron` run |
| `cancel_date` > `cleanup_after_months` (default 6 months) | `_do_cleanup` (helm uninstall, release K8s resources) | same cron, later |

So T19 only adds the **transition into `cancel`** plus the cancellation
email. Suspend + cleanup are already verified end-to-end (T22 Phase B,
2026-04-26).

### Recovery story (Decision 4 follow-through)

Hard-cancel means there is **no in-place resume** once a sub is cancelled.
A customer who fixed their card after auto-cancel must:

1. Open a new subscription via the marketplace flow
   (`/api/.../cloud-services` POST) — same UX as a first-time signup.
2. Reattach to the existing K8s release if it is still around
   (`cancel_date` not yet past `cleanup_after_months`). The reattach is
   intentionally out of scope for T19 v1; the data is preserved for up to
   6 months by `_do_cleanup`'s timer, so a manual operator action or a
   future "re-link existing release on re-subscribe" feature is feasible
   without code change in T19.

Communicating this to customers is the job of the cancellation email
(template added by Decision 2): it should call out the 6-month preservation
window and the path back to service.

## Implementation effort breakdown

| Component | LOC (est.) | File |
|---|---|---|
| Model fields + default (3 fields) | ~15 | `src/models/sh_subscription_fix.py` |
| Cron logic (guard advance + reuse-unpaid + counter + threshold) | ~50 | same file |
| `_notify_subscription_cancelled` helper | ~25 | same file |
| Email template XML | ~20 | `src/data/mail_template_subscription_cancelled.xml` |
| Manifest + hooks (config_parameters cleanup, template registration) | ~10 | `src/__manifest__.py`, `src/hooks.py` |
| Pre-init migration (back-fill 0/false on existing subs) | ~5 | `src/migrations/...` |
| Unit tests (5 cases T19a–T19e) | ~250 | `src/tests/test_subscription_failure_handling.py` |
| Doc update (CHANGELOG, README ops note) | ~15 | `CHANGELOG.md` |
| **Total** | **~390** | |

Estimated effort: **2–3 hours** including local verification.

Risk: **low–medium**:
- Pure additions for fields, template, cancel notification — no edits to
  existing payment paths.
- The guard on `sh_date_of_next_payment` is the only behaviour change;
  it's gated by payment outcome and so is observably equivalent to the
  current code on the success path.
- `threshold=0` makes the whole feature a no-op for environments that
  cannot accept dunning behaviour yet.

## Test plan

Daily cron + threshold=6 ⇒ ~6 daily retries before cancel.

| # | Scenario | Setup | Expected |
|---|---|---|---|
| **T19a** | Single failure increments counter, doesn't cancel | empty wallet, no card; run cron once | `failures=1`, `state=in_progress`, exactly 1 invoice (unpaid), `sh_date_of_next_payment` **not advanced** |
| **T19b** | Failure then success on same period: existing unpaid is reused, counter resets | run cron empty (failures=1), top up wallet, run cron next day | `failures=0`, `state=in_progress`, still 1 invoice (now paid), `sh_date_of_next_payment` **advanced** by 1 cycle |
| **T19c** | N consecutive failures → cancel + email + suspend | run cron 6× empty across 6 days | `state=cancel`, `woow_cancellation_notified=True`, exactly 1 invoice (still unpaid), 1 cancellation email queued; next `subscription_lifecycle_cron` triggers `_do_suspend` |
| **T19d** | Per-sub scope (failures don't bleed across subs) | one partner, two subs A (failing) + B (paying), 6 cycles | only A cancels with notification; B stays `in_progress` with `failures=0` |
| **T19e** | `threshold=0` disables auto-cancel | set param to 0, run cron 10× empty | `state` remains `in_progress`, counter still increments (informational), no cancellation email |
| **T19f** | Decision 4 — no in-place recovery after cancel | once T19c has cancelled, top up wallet and run cron | `state` remains `cancel`; no new invoice; no payment; cron's `state='in_progress'` filter excludes the sub |
| **T19g** | Cron filter must reprocess past-due (`<= today`) subs | manually set `next_payment = today - 3` and run cron | sub still gets processed (existing-unpaid reuse + retry); without the `<=` fix this would be a no-op |

## Recommendation

Implement before next material customer onboarding wave. The current
unbounded retry behaviour silently bleeds compute and skews ARR reporting.
~390 LOC, ~2–3 hours, low–medium risk, fully local to `paas-platform`.

When implementing, the existing `subscription_lifecycle_cron` is the
contract — T19 only needs to flip state to `cancel` and queue one email;
the rest of the suspend/cleanup machinery is already verified end-to-end.

The hard-cancel model (Decision 4) intentionally postpones a "re-link
existing release on re-subscribe" feature. As long as the cancellation
email tells the customer about the 6-month preservation window, that
feature can land later without breaking T19's contract.
