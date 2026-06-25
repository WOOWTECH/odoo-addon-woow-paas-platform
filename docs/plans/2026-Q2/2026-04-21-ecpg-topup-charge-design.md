# ECPG Top-up Charge Flow — Design

**Date**: 2026-04-21
**Author**: Eugene + Claude (brainstorming session)
**Status**: Design approved, pending implementation plan
**Scope**: Connect `/api/billing/wallet/topup` to a real ECPG token charge. Do not touch subscription auto-charge in this round, but carve out a reusable helper so the follow-up work below can pick it up without rewriting.

---

## Problem

`src/controllers/billing.py:516-529` currently short-circuits with a feature flag and, even when opened, calls `add_wallet_points()` directly without any ECPay interaction. The "Pre-pay & Get Credits" button in `AddFundsModal` therefore cannot produce a real charge against the user's bound credit card.

The infrastructure to actually charge a bound ECPG card already exists in `payment_ecpay_ecpg` (see `models/payment_transaction.py:39-103` and `controllers/ecpg_callback.py:28-81`). What is missing is the glue that:

1. Creates the right `payment.transaction` when a user buys a top-up plan,
2. Redirects the browser through the mandatory 3DS challenge,
3. Credits the wallet after ECPay's S2S callback confirms success,
4. Remains idempotent and recoverable if the user abandons the 3DS page.

## Decisions (from brainstorming, confirmed)

| # | Question | Decision |
|---|---|---|
| Q1 | AIO (fill card again) vs ECPG (bound-card + 3DS)? | **ECPG bound-card + 3DS** |
| Q2 | 3DS UX — same tab / new tab / iframe? | **Same-tab redirect**, OrderResultURL brings user back to `/woow#/billing/topup?topup_id=N` |
| Q3 | Error display strategy | **Simplified frontend message + full log in backend.** Admin queries `payment.transaction.state_message` / `woow.wallet.topup.failure_code/message` for details |
| Q4 | E-invoice | **Phase 1: record invoice intent only.** Phase 2 cron builds `account.move` and calls `ecpay_invoice_tw.create_ecpay_invoice()` |
| Q5 | Abstraction level | **Extract a reusable `payment.token._charge()` helper** — topup uses it now, subscription auto-charge migration will use it later |
| Q6 | Scope of this round | **Topup only**, but Phase 2 roadmap (below) is planned inside this document to not lose the linkage |
| Q7 | Idempotency against double-clicks | **At most one `pending` topup per partner.** If a new `api_topup` call finds an existing pending, return the existing `topup_id` + its `redirect_url` so the user resumes the same flow instead of creating a second `payment.transaction` |

## Non-Goals in this round

- Subscription auto-charge (cron) migration from AIO to ECPG — **planned in Phase 2**
- Card-binding flow unification (`/my/wallet/bind_card` legacy vs OWL `/settings/payment-methods` ECPG) — **planned in Phase 3**
- Electronic-invoice issuing — **planned in Phase 2**
- ECPay query-API based reconciliation for stale pending topups — Phase 1 cron just cancels them; query API is optional later

---

## Architecture

```
┌──────────────────┐       ┌────────────────────────────────────┐
│  OWL (frontend)  │       │         Odoo backend                │
│                  │       │                                     │
│ TopupStorePage   │       │  BillingController                  │
│  ↓ click Pre-pay │──POST─▶  /api/billing/wallet/topup         │
│  AddFundsModal   │       │    ├─ guard: flag + no_card +       │
│                  │       │    │         duplicate_pending      │
│                  │       │    ├─ build woow.wallet.topup (draft)│
│                  │       │    └─ token._charge()               │
│                  │◀──────┤       → {tx, redirect_url}          │
│                  │       │                                     │
│  window.location │       │  payment.token._charge()   (helper) │
│   = redirect_url │       │    ├ create payment.transaction     │
│                  │       │    └ _send_payment_request()        │
└────────┬─────────┘       └──────────────┬──────────────────────┘
         │                                │
         ▼                                │
    ┌────────┐                            │
    │ ECPay  │◀─── 3DS verify ────────────┘
    │        │
    │        │─── S2S callback ──▶ /payment/ecpg/pay_callback
    │        │                     └─ tx._set_done() / _set_error()
    │        │                            │
    │        │                            ▼
    │        │                  [NEW] payment.transaction
    │        │                    _set_done/_set_error override:
    │        │                    if tx.woow_topup_id:
    │        │                      topup._on_payment_done / _failed
    │        │                        → add_wallet_points()
    │        │                        → topup.state = done/failed
    │        │
    │        │─── browser redirect ──▶ /woow#/billing/topup?topup_id=N
    └────────┘                         └─ OWL queries /api/billing/topup/<id>/status
                                         → shows TopupSuccessModal or error
```

### Key layers
- **`woow.wallet.topup`** — a top-up order. Holds plan snapshot, invoice intent, payment-transaction link, lifecycle state, failure info.
- **`payment.token._charge(...)`** — generic helper. Encapsulates "build a `payment.transaction`, wire its purpose correlation, call `_send_payment_request()`, return the redirect URL or final state". Topup uses it now; subscription migration (Phase 2) uses it later.
- **`payment.transaction._set_done` / `_set_error` override** — the single post-payment confluence point. Drives `woow.wallet.topup.state` transitions. Future subscription charges will route through the same hook.
- **OWL frontend** — just orchestrates "call API → redirect or show error" + "on return, query status → show success / translate error".

### Invariants
- No wallet points are credited unless ECPay's S2S callback (or a subsequent `_set_done`) has fired. Browser redirect alone never credits.
- Exactly one `wallet.transaction` row with `transaction_type='topup'` per successful top-up. Enforced via `payment_transaction_id` lookup in `_on_payment_done`.
- At most one `pending` `woow.wallet.topup` per partner at a time. Prevents double-charge from double-click.

---

## Data Model

### New — `woow.wallet.topup`

```python
class WalletTopup(models.Model):
    _name = 'woow.wallet.topup'
    _description = 'Wallet Top-up Order'
    _order = 'create_date desc, id desc'

    # Identity
    partner_id         = fields.Many2one('res.partner', required=True, index=True)
    plan_id            = fields.Many2one('woow_paas_platform.topup_plan', required=True, ondelete='restrict')

    # Plan snapshot (so later plan edits don't alter historical orders)
    amount             = fields.Monetary(required=True)
    credits            = fields.Integer(required=True)
    bonus              = fields.Integer(default=0)
    currency_id        = fields.Many2one('res.currency', required=True)

    # Lifecycle
    state              = fields.Selection([
        ('draft', 'Draft'),          # created, not yet sent to ECPay
        ('pending', 'Pending'),      # 3DS in flight, awaiting S2S callback
        ('done', 'Done'),            # wallet credited
        ('failed', 'Failed'),        # ECPay rejected / 3DS failed
        ('cancelled', 'Cancelled'),  # cron-expired or user aborted
    ], default='draft', required=True, index=True)

    # Payment linkage
    payment_transaction_id = fields.Many2one('payment.transaction', ondelete='restrict', index=True)
    wallet_transaction_id  = fields.Many2one('wallet.transaction', readonly=True)

    # Failure diagnostics (Q3 — simplified for users, full detail for admins)
    failure_code       = fields.Char()   # mapped enum: card_declined | limit_exceeded | …
    failure_message    = fields.Char()   # full RtnMsg from ECPay, admin-facing

    # Invoice intent (Phase 1 stores only; Phase 2 cron issues)
    invoice_carrier_type = fields.Selection([
        ('email', 'Email carrier'),
        ('mobile', 'Mobile barcode'),
        ('citizen', 'Citizen digital cert'),
        ('triplicate', 'Triplicate (B2B)'),
    ], default='email')
    invoice_carrier    = fields.Char()    # carrier number or tax ID (B2B)
    invoice_tax_id     = fields.Char()    # buyer tax ID (triplicate)
    invoice_title      = fields.Char()    # buyer title (triplicate)
    invoice_lovecode   = fields.Char()    # donation code, if donating instead
    invoice_account_move_id = fields.Many2one('account.move', readonly=True)
    invoice_issued_at  = fields.Datetime(readonly=True)
```

### Modified — `payment.transaction`

```python
woow_topup_id = fields.Many2one(
    'woow.wallet.topup',
    ondelete='set null',
    index=True,
    help="When set, this transaction is a wallet top-up. _set_done/_set_error will drive the linked topup state.",
)
```

### Reused from upstream (no change)
- `wallet.transaction.state` / `payment_transaction_id` — we only *read* upstream fields; we do not extend this model, so the upstream (`sh_subscription_wallet` @ main 82ff1f4) can keep evolving without our layer conflicting
- `partner.add_wallet_points(amount, description, payment_transaction)` — already creates the `wallet.transaction` for us

---

## Component Contracts

### Generic helper — `payment.token._charge(amount, reference, currency_id, purpose_field=None, purpose_id=None)`

```python
def _charge(self, amount, reference, currency_id=None, purpose_field=None, purpose_id=None):
    """Charge this bound payment token.

    For ECPG tokens this ends up calling `create_payment_with_card_id`, which
    — as of 2025 ECPay spec — always requires a 3DS challenge.

    Args:
        amount:         charge amount (int/float, caller decides rounding)
        reference:      unique reference string for `payment.transaction.reference`
        currency_id:    fallback = token company currency
        purpose_field:  name of a Many2one on `payment.transaction` pointing to
                        the domain object that owns this charge
                        (e.g. 'woow_topup_id')
        purpose_id:     id for that Many2one

    Returns:
        {
            'tx': <payment.transaction record>,
            'redirect_url': str | None,  # truthy → caller must redirect browser
            'state': 'pending' | 'done' | 'error',
        }
    """
```

Caller responsibilities:
- Decide the correlation object and pass it via `purpose_field`/`purpose_id`.
- If `state == 'done'` and caller has post-success work, it will already have run via the `_set_done` override — the caller only needs to respond to the user.
- If `state == 'error'`, inspect `tx.state_message` or the translated error returned alongside to render a message.

### Controller — `BillingController.api_topup` (rewrite)

```python
@route("/api/billing/wallet/topup", auth="user", methods=["POST"], type="json")
def api_topup(self, plan_id=None, invoice_intent=None, **kw):
    # plan + flag guards (existing)
    # partner + default-token guard (reuse _get_primary_card_info)

    partner = request.env.user.partner_id.sudo()

    # Q7: at most one pending per partner
    existing = request.env['woow.wallet.topup'].sudo().search([
        ('partner_id', '=', partner.id),
        ('state', '=', 'pending'),
    ], limit=1)
    if existing:
        return {
            'success': True,
            'data': {
                'topup_id': existing.id,
                'redirect_url': self._resume_3ds_url(existing),  # may be None if 3DS
                                                                 # token expired; the UI
                                                                 # is expected to show
                                                                 # "awaiting confirmation"
                'resumed': True,
            },
        }

    plan, token, error = self._resolve_topup_plan_and_token(partner, plan_id)
    if error:
        return error

    # Persist invoice intent (Phase 1 only stores)
    topup_vals = self._build_topup_vals(partner, plan, invoice_intent)
    topup = request.env['woow.wallet.topup'].sudo().create(topup_vals)

    result = token._charge(
        amount=plan.amount,
        reference=f"TOPUP{topup.id}",  # ECPay rejects '-' / '_'; keep A-Za-z0-9
        currency_id=plan.currency_id.id,
        purpose_field='woow_topup_id',
        purpose_id=topup.id,
    )
    # The _charge helper already linked tx.woow_topup_id and (if 3DS) put the tx
    # in pending state. Propagate to topup state so the UI & cron can observe it.
    topup.sudo().write({
        'payment_transaction_id': result['tx'].id,
        'state': 'pending' if result['state'] == 'pending' else result['state'],
    })
    return {'success': True, 'data': {
        'topup_id': topup.id,
        'redirect_url': result['redirect_url'],
        'state': topup.state,
    }}
```

### New endpoint — `GET /api/billing/topup/<id>/status`

Returns `{success, data: {state, amount, credits, bonus, new_balance, failure_code, failure_message}}`. Used by OWL when the 3DS redirect returns, to decide whether to show `TopupSuccessModal` or the error state.

Access: owner-only (partner match), else 404.

### Model method — `woow.wallet.topup._on_payment_done(tx)`

```python
def _on_payment_done(self, tx):
    self.ensure_one()
    if self.state == 'done' or self.wallet_transaction_id:
        return  # idempotent

    self.partner_id.add_wallet_points(
        self.credits + self.bonus,
        description=f"Top-up: {self.plan_id.name}",
        payment_transaction=tx,
    )
    wtx = self.env['wallet.transaction'].search([
        ('payment_transaction_id', '=', tx.id),
        ('transaction_type', '=', 'topup'),
    ], limit=1)
    self.write({'state': 'done', 'wallet_transaction_id': wtx.id})
```

### Model method — `woow.wallet.topup._on_payment_failed(tx)`

```python
def _on_payment_failed(self, tx):
    self.ensure_one()
    if self.state in ('done', 'failed'):
        return
    self.write({
        'state': 'failed',
        'failure_message': tx.state_message or '',
        'failure_code': self._classify_failure(tx.state_message),
    })
```

### Hook — `payment.transaction._set_done` / `_set_error` (new override)

File: `src/models/payment_transaction.py`

```python
from odoo import models

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

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

### Frontend — `AddFundsModal.onConfirm` (rewrite)

```js
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
  if (result.data.redirect_url) {
    window.location.href = result.data.redirect_url;  // leaves for 3DS
    return;
  }
  // Rare: immediate done without 3DS (should not happen with current ECPay spec)
  await billingService.fetchWallet();
  this.props.onSuccess(result.data);
}
```

### Frontend — `TopupStorePage.setup` (detect return)

```js
onMounted(async () => {
  const q = router.params.query || {};
  if (q.topup_id) {
    const r = await billingService.getTopupStatus(q.topup_id);
    if (r.success) {
      if (r.data.state === 'done') {
        this.state.successData = r.data;
        this.state.showSuccessModal = true;
      } else if (r.data.state === 'failed') {
        this.state.globalError = translateTopupError(r.data.failure_message, r.data.failure_code);
      } else if (r.data.state === 'pending') {
        this.state.globalError = 'Still confirming payment — please refresh in a moment.';
      }
    }
    const cleanHash = window.location.hash.split("?")[0];
    window.history.replaceState(null, "", cleanHash);
  }
});
```

### Frontend — error translation (`src/static/src/paas/services/topup_errors.js`)

Maps known RtnCodes / internal codes to 4-5 user-friendly strings. Extensible per Q3.

---

## Data Flow

### Happy path (with 3DS)

```
 1. User clicks Pre-pay
 2. POST /api/billing/wallet/topup { plan_id }
    → guards pass, no existing pending
    → build woow.wallet.topup (state=draft, plan snapshot frozen)
    → token._charge(...)
        → build payment.transaction (tx.woow_topup_id=topup.id)
        → tx._send_payment_request()  (ECPG create_payment_with_card_id)
        → 3DS URL returned
        → tx._set_pending()  (state=pending)
    → topup.state=pending, save payment_transaction_id
    → respond { topup_id, redirect_url }
 3. Browser redirects to 3DS URL
 4. User completes 3DS
 5. ECPay fires S2S → /payment/ecpg/pay_callback
    → tx._set_done()
    → [override] topup._on_payment_done()
        → add_wallet_points → wallet.transaction (type=topup) + loyalty_card.points +=
        → topup.state=done, wallet_transaction_id=wtx.id
 6. ECPay redirects browser → /woow#/billing/topup?topup_id=N
 7. OWL TopupStorePage onMounted → GET /api/billing/topup/N/status
    → state=done → show TopupSuccessModal
```

### Failure paths

| When | Mechanism | User sees |
|---|---|---|
| Pre-flight (no card / flag off / plan inactive) | `api_topup` returns `code` | Targeted inline message + CTA ("Add card" / "Try later") |
| ECPG SDK throws | `_charge` → `tx._set_error` → override → `_on_payment_failed('sdk_error')` | "Payment service unavailable, try again" |
| ECPay rejects create_payment (RtnCode != 1) | `_send_payment_request` → `_set_error` → override | Translated reason |
| 3DS failure | S2S `pay_callback` calls `_set_error` | Same as above, after browser returns |
| User abandons 3DS, S2S still arrives | Same as done/failed, user sees result on next visit | — |
| User abandons 3DS, S2S never arrives | Cron (below) eventually cancels | User must retry |

### Cron — `woow_paas_platform.cron_topup_cleanup`

```
Every 10 min:
  For each woow.wallet.topup where state=pending AND create_date < now - 30min:
    tx = topup.payment_transaction_id
    if not tx:
      topup.state = cancelled   # shouldn't happen, defensive
    elif tx.state == 'done':
      topup._on_payment_done(tx)   # catch-up (S2S eventually arrived, override ran but we raced)
    elif tx.state in ('error', 'cancel'):
      topup._on_payment_failed(tx)
    elif tx.state in ('draft','pending') and create_date < now - 60min:
      tx._set_canceled("Timed out waiting for 3DS confirmation")
      topup.write({'state': 'cancelled'})
```

Phase 1 intentionally **does not** call an ECPay query API. Rationale:
- ECPG SDK does not include a payment-query endpoint (only bind queries).
- Cancelling a pending row without crediting is safe: worst case a genuinely-charged-but-callback-lost user sees "cancelled" and contacts support; admin can reconcile via `tx.provider_reference` (ECPay TradeNo).
- Adding the query integration is scoped under Phase 2 if it becomes worth the risk trade-off.

### State machines

```
woow.wallet.topup.state:  draft ─▶ pending ─▶ done
                             │         │
                             │         └─▶ failed
                             └─▶ cancelled  (cron timeout or manual)

payment.transaction.state:  draft ─▶ pending ─▶ done
                                         │
                                         └─▶ error / cancel
```

Invariant: `topup.state == 'done' ⇒ wallet_transaction_id IS NOT NULL ⇒ loyalty_card.points += credits+bonus already applied`.

---

## Error Handling (Q3)

### Layer → behaviour

| Layer | Where caught | User message | Admin trail |
|---|---|---|---|
| L1 pre-flight | `api_topup` | targeted copy | controller log |
| L2 SDK/network | `token._charge` wraps `ECPGError`/`requests.*` | "Payment service unavailable" | `_logger.exception` + full trace |
| L3 ECPay business (RtnCode≠1 on create) | `_send_payment_request` → `_set_error` | translated via `_classify_failure` | `tx.state_message` keeps full RtnMsg |
| L4 3DS failure (callback) | `pay_callback` → `_set_error` → override | translated | same as L3 |
| L5 S2S lost / timeout | cron cancels | "Cancelled — please retry" (on return) | log `_logger.warning` |

### Error codes (stable contract for frontend)

```
payment_not_configured  → "Top-up is not available yet, please contact support"
no_card                 → "Please bind a credit card first"  (+ CTA to /settings/payment-methods/add)
plan_not_found          → "This plan is no longer available"
provider_disabled       → "System is temporarily unavailable"
duplicate_pending       → (frontend resumes existing redirect_url; no error shown unless resume fails)
sdk_error               → "Payment service unavailable, please try again"
card_declined           → "Card was declined, please verify or try another card"
limit_exceeded          → "Card limit exceeded"
duplicate_transaction   → "Duplicate transaction detected, please wait a moment"
generic                 → "Payment failed, please retry or contact support"
```

### Transactional integrity

`_on_payment_done` performs its work inside Odoo's cursor (the callback handler already runs in a single request cr). If an exception raises during `add_wallet_points` or the subsequent writes, the whole unit-of-work rolls back, leaving `tx.state == 'done'` but `topup.state` still `pending`. The cron will retry on its next pass, so partial-credit states are not observable.

---

## Test Plan

### Unit tests (`src/tests/test_topup_charge.py` — TransactionCase)

**`payment.token._charge()`**
1. `test_charge_builds_payment_transaction_with_token_and_purpose` — tx linked to token, reference sanitized, `woow_topup_id` set
2. `test_charge_returns_redirect_url_when_3ds` — mocked ECPG returns ThreeDURL → helper returns redirect_url + state=pending
3. `test_charge_sets_error_on_ecpg_exception` — mocked raises ECPGError → tx.state=error, helper returns state=error
4. `test_charge_done_without_3ds` — mocked returns RtnCode=1 with no ThreeDURL → helper returns state=done

**`payment.transaction._set_done/_set_error` overrides**
5. `test_set_done_credits_wallet_when_linked_to_topup` — topup.state flips to done, wallet.transaction row created, loyalty points += credits+bonus
6. `test_set_done_no_op_on_unrelated_tx` — no woow_topup_id → no wallet side-effects (protects subscription flows)
7. `test_set_done_idempotent_on_replay` — second call skips (guarded by wallet_transaction_id)
8. `test_set_error_marks_topup_failed_and_classifies_code`

**`woow.wallet.topup`**
9. `test_topup_snapshot_plan_data` — amount/credits/bonus frozen at creation, later plan edits don't affect
10. `test_on_payment_done_stores_wallet_transaction_id`
11. `test_on_payment_failed_keeps_raw_message_for_admin`
12. `test_classify_failure_maps_known_rtncode`

**Cron cleanup**
13. `test_cron_ignores_fresh_pending_topups` — create_date < 30min → skipped
14. `test_cron_catches_up_when_tx_already_done` — tx.state=done but topup.state=pending → credits applied, topup.state=done
15. `test_cron_marks_failed_when_tx_errored`
16. `test_cron_cancels_long_stuck_pending_and_calls_set_canceled` — tx still pending > 60 min

### Integration tests (`src/tests/test_topup_controller.py` — HttpCase)

1. `test_api_topup_happy_path_redirects_to_3ds`
2. `test_api_topup_rejects_when_flag_disabled` → `code=payment_not_configured`
3. `test_api_topup_rejects_without_card` → `code=no_card`
4. `test_api_topup_rejects_invalid_plan` → `code=plan_not_found`
5. `test_api_topup_returns_existing_pending_when_duplicate` → `code=duplicate_pending` (or success with `resumed=true`)
6. `test_pay_callback_done_credits_wallet_end_to_end` — simulate ECPay S2S with encrypted ResultData → wallet balance increases
7. `test_pay_callback_idempotent_on_replay` — callback twice → credited once
8. `test_topup_status_endpoint_returns_current_state`
9. `test_topup_status_rejects_other_users`

### Manual smoke test (stg, ECPay sandbox test card `4311-9522-2222-2222`)

Run through after deploy:
- [ ] With `topup_payment_enabled=False`: Pre-pay shows "Top-up not available yet"
- [ ] Flag on + no card: AddFundsModal shows bind CTA (regression check from today's fix)
- [ ] Flag on + card: Pre-pay → redirected to ECPay stage 3DS → test OTP → returns to `/woow#/billing/topup?topup_id=N` → TopupSuccessModal → wallet balance += credits + bonus
- [ ] 3DS wrong OTP → returns with `state=failed` → AddFundsModal (reopened) shows translated error
- [ ] Close 3DS tab → 30 min later cron cancels; user revisits and can start over
- [ ] Admin backend: `payment.transaction` shows RtnCode/RtnMsg in `state_message`
- [ ] `wallet.transaction` has exactly one `topup` row with correct `payment_transaction_id`
- [ ] Double-click Pre-pay within 1 second → second call returns existing pending; only one `payment.transaction` created

---

## Follow-up roadmap (Q6 — tracked here to not lose the linkage)

### Phase 2 — Migrate subscription auto-charge to ECPG

**Problem it solves.** `sh_subscription_wallet.subscription_wallet_mixin._charge_ecpay_token` today calls `payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5` via the legacy AIO SDK and keys off `partner.ecpay_card_token`. That is the old single-token system. The current platform manages cards via ECPG `payment.token` rows, so the mixin effectively never sees today's users' cards.

**Plan.** Rewrite `_charge_ecpay_token` so it:
1. Resolves the partner's default ECPG token via `_get_default_ecpg_token` (shared with the read-side fix we already landed).
2. Calls the new `payment.token._charge(amount, reference=f"SUB{subscription.id}..", purpose_field='woow_subscription_id', purpose_id=subscription.id)`.
3. Uses the same `payment.transaction._set_done`/`_set_error` hook to credit `wallet.transaction` and flip subscription state.
4. Keeps the existing mixin's savepoint/rollback discipline for the points-first-then-card flow.

The daily and retry crons (`_cron_wallet_charge`, `_cron_wallet_charge_retry`) keep their current trigger semantics; only the inner charge call changes.

**Dependencies on Phase 1.** The `payment.token._charge()` helper must exist. The `_set_done` override must be scoped to route by correlation field (`woow_topup_id` vs future `woow_subscription_id`) — design is already doing this.

**3DS implication.** Subscription auto-charge is usually unattended (cron). ECPay's current 3DS 2.0 mandate means even token charges get a ThreeDURL. For subscriptions this either needs:
- An ECPay merchant-category exemption (recurring merchant), OR
- A "notify user to confirm" flow (e.g. email with a link to complete 3DS).
Must be confirmed with ECPay before Phase 2 work begins. If exemption is not granted, Phase 2 also needs to design the user-notification path.

### Phase 3 — Unify card binding

**Problem it solves.** `sh_subscription_wallet.card_binding` registers cards via the AIO period-billing authorization and stores them in `partner.ecpay_card_token`. The OWL UI (`/settings/payment-methods`) uses ECPG and stores in `payment.token`. Today two card inventories exist independently.

**Plan.**
1. Deprecate `/my/wallet/bind_card` flow (redirect users to `/woow#/settings/payment-methods/add`).
2. One-off data migration: for any partner with `ecpay_card_token` but no ECPG `payment.token`, either (a) prompt a one-time re-bind, or (b) — if ECPay supports converting — move the token into the new system.
3. Drop `has_bound_credit_card()` reliance on `ecpay_card_token`; let it delegate to `_get_primary_card_info(partner)['bound']` which already does both.
4. Eventually remove the four `ecpay_card_*` fields from `res.partner` once Phase 2 migration retires all call sites.

**Risk.** Users mid-subscription with an old token must not lose service. Migration must happen before Phase 2 ships.

### Phase 2/3 invoice automation

Once the topup flow is producing `woow.wallet.topup` records with captured invoice intent, a cron can:
1. Search `woow.wallet.topup` where `state=done AND invoice_account_move_id IS NULL`.
2. For each: create an `account.move` of type `out_invoice` with a single line (the plan), set `carrierType` / `input_carrier_num` / `ecpay_CustomerIdentifier` / `is_donation` / `lovecode` from the topup's invoice_* fields.
3. Call `move.create_ecpay_invoice()` (from `ecpay_invoice_tw`) to hit ECPay e-invoice API and populate `uniform.invoice`.
4. Write the `account.move.id` back to `topup.invoice_account_move_id` and `topup.invoice_issued_at`.

This slots cleanly onto the Phase 1 data model; no schema change is needed beyond what Phase 1 already lays down.

### Tracking

Create separate tickets (not subtasks of the topup implementation ticket) to keep merge surface small:

- `WOOW-?`: Phase 2 — migrate subscription auto-charge to ECPG
- `WOOW-?`: Phase 2b — e-invoice issuing cron
- `WOOW-?`: Phase 3 — unify card binding

Link all three to this design doc as the canonical reference.

---

## Open questions / risks

1. **3DS exemption status with ECPay.** If Phase 2 subscription auto-charge really cannot avoid the 3DS step, that's a UX / product question, not purely technical. Confirm with ECPay commercial team before Phase 2.
2. **`reference` field length.** ECPay rejects characters outside `[A-Za-z0-9]` and caps at 20 chars. `TOPUP{topup.id}` stays under 20 until topup.id exceeds ~10^14 — effectively never, but add a guard truncation for safety (see `payment_transaction._ecpg_build_merchant_trade_no`).
3. **Idempotency under load.** Two rapid `api_topup` calls could both pass the "one pending" guard if they race. Mitigation: use `SELECT … FOR UPDATE` on `res.partner` (or a small `INSERT … ON CONFLICT DO NOTHING` pattern). Document in implementation plan.
4. **`card_bank` display.** The `/api/billing/wallet` endpoint now returns `card_bank`. `AddFundsModal` uses it. If the ECPG `ecpg_issuing_bank` field is empty for a card, UI falls back to "Card". Acceptable.

---

## Out-of-scope reminders

- No changes to `payment_ecpay_ecpg` addon (upstream). All hooks are done in `woow_paas_platform`.
- No changes to `sh_subscription_wallet` in Phase 1. Phase 2 touches `subscription_wallet_mixin` only.
- No UI redesign of TopupStorePage / AddFundsModal beyond what the flow requires (error banner, CTA, success modal reuse).
