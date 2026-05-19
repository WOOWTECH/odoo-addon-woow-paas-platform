# G4-P2: Dashboard Summary API + Real Data Wiring

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the "Coming soon" placeholder on `#/dashboard` (currently shipping from G4-P0) with real data fetched from a single backend endpoint. The dashboard becomes a useful post-login overview rather than visual filler.

**Architecture:**
1. **Backend** — new endpoint `POST /api/dashboard/summary` (auth=user, JSON-RPC) that aggregates user / billing / workspaces / members / recent activity into a single response. All monetary fields share the same source as the header CREDITS indicator (`partner.wallet_balance`) so they cannot disagree (the original Issue #9 root cause was independent mock vs real data — eliminating that means single source).
2. **Frontend** — `DashboardPage` becomes stateful: `setup()` fetches via a new `dashboardService.fetchSummary()`, renders loading/error/data states. The placeholder card is removed; the original three stat cards (Members, Billing, Workspaces) come back, but populated from real data and using `formatPoints` for any monetary value (G1 consistency).

**Tech Stack:** Odoo 18 (Python 3.11), OWL.js, no local test runner — verify post-deploy via `kubectl exec`.

**Branch:** `develop` (sequential).

**Reference design:** `docs/plans/2026-04-28-billing-issue-fix-design.md` § G4. The design doc's API contract is the source of truth; this plan extends it with codebase-specific mappings and the realistic fallbacks for fields that have no upstream concept (e.g. "Pending Invites" — the workspace_access model has no pending state).

---

## Out of scope

- A new `audit.log` model. The codebase has no audit log table; building one is a separate epic. **Recent activity** is sourced from `wallet.transaction` (the only ordered timeline of user-relevant events that actually exists today). When richer event types ship later, add them here.
- "Pending Invites" as a real concept. `workspace_access` has no `state='pending'`; users either have access or don't. Render `pending = 0` and document it.
- Multi-workspace users. The current admin user has 1 workspace; the API and tests assume the response shape works for N workspaces but we don't add a per-workspace breakdown.
- Plan-tier limits ("Pro Plan Limits" with X/Y caps). The codebase has no plan-tier model. Render `limit: null` so the frontend shows raw counts (e.g. "1 active" instead of "1/3").
- Quick Access tiles. The G4-P0 stripped them; G4-P2 leaves them stripped (separate scope).
- Real-time updates / refresh-on-mutation. The dashboard fetches once on mount; user must reload to see changes.

---

## API contract

`POST /api/dashboard/summary` (auth=user, type=json):

```json
{
  "success": true,
  "data": {
    "user": {
      "id": 6,
      "name": "Mitchell Admin",
      "avatar_url": "/web/image/res.partner/3/avatar_128"
    },
    "billing": {
      "credits_left": 576.15,
      "current_cycle_start": "2026-04-27",
      "current_cycle_end": "2026-05-27",
      "current_cycle_usage": 573.85,
      "next_bill_date": "2026-05-27",
      "next_bill_estimate": 573.85,
      "currency": "pts"
    },
    "workspaces": {
      "total": 1,
      "limit": null,
      "services": {
        "running": 1,
        "total": 1
      }
    },
    "members": {
      "total": 1,
      "active": 1,
      "pending": 0
    },
    "recent_activity": [
      {
        "ts": "2026-04-27T15:56:00Z",
        "actor": "Mitchell Admin",
        "action": "topup",
        "target": "NT$ 1000",
        "description": "Top-up: NT$ 1000 Credits + 100 bonus = 1,100 pts"
      }
    ]
  }
}
```

Errors return `{ "success": false, "error": "<message>" }`. The endpoint is single-user (uses `request.env.user`); errors are limited to "user has no partner" (skip — `auth=user` guarantees a partner) and uncaught exceptions logged server-side.

---

## Data source mappings

| Field | Source | Notes |
|---|---|---|
| `user.id` | `request.env.user.id` | |
| `user.name` | `request.env.user.partner_id.name` | Same source as Header `formattedName` |
| `user.avatar_url` | `/web/image/res.partner/{partner.id}/avatar_128` | Standard Odoo URL |
| `billing.credits_left` | `partner.wallet_balance` | **Same source as Header CREDITS** — guaranteed match |
| `billing.current_cycle_start` | active subscription's `sh_start_date` | If multiple subs, take the most recently started; if none, use first day of this calendar month |
| `billing.current_cycle_end` | active subscription's `sh_date_of_next_payment` minus 1 day | Falls back to last day of cycle_start month |
| `billing.current_cycle_usage` | sum of `wallet.transaction.amount` where `transaction_type='deduct'` and `create_date >= cycle_start` | Always non-negative; debits are negative amounts so we negate the sum |
| `billing.next_bill_date` | active subscription's `sh_date_of_next_payment` | None if user has no active subscription |
| `billing.next_bill_estimate` | sum of active subscriptions' `sh_plan_price` × (1 + tax_rate) | Use the same `_skip_trial_estimate`-style tax computation per subscription |
| `billing.currency` | hardcoded `"pts"` | |
| `workspaces.total` | count of distinct workspaces accessible to user (via `workspace_access.user_id`) | |
| `workspaces.limit` | `null` | Placeholder for future plan-tier feature |
| `workspaces.services.total` | count of `cloud_service` records owned by those workspaces | |
| `workspaces.services.running` | count where `cloud_service.state == 'running'` | The `state` field exists on `cloud_service`; verify field name during implementation. |
| `members.total` | count of `workspace_access` records across user's accessible workspaces | |
| `members.active` | same as `total` (no pending concept exists) | Per design doc note. |
| `members.pending` | `0` | Hardcoded; documented as a placeholder. |
| `recent_activity` | last 5-10 `wallet.transaction` rows for `partner_id=user.partner_id`, ordered by `create_date DESC` | |
| `recent_activity[].ts` | `wt.create_date.isoformat()` | |
| `recent_activity[].actor` | `partner.name` (single-user view) | |
| `recent_activity[].action` | mapped from `wt.transaction_type`: topup→"topup", deduct→"subscription_charge", refund→"refund", adjustment→"adjustment", others→type literal | |
| `recent_activity[].target` | derived: top-up uses `topup.plan_id.name` if linked, else "Wallet"; deduct uses linked subscription's plan name if available, else "Subscription" | |
| `recent_activity[].description` | `wt.description` (already includes the bonus breakdown after G2-B) | |

**Note on `next_bill_estimate`**: implementing this requires per-subscription tax computation, mirroring `_skip_trial_estimate`'s `account.tax.compute_all` path. To avoid duplication, extract a tiny helper `_subscription_total_with_tax(sub)` on the lifecycle mixin that both `_skip_trial_estimate` and the dashboard endpoint can call. **Acceptable simplification**: if the helper extraction blows scope, return `next_bill_estimate = sum(sub.sh_plan_price)` (untaxed) and document it. Frontend label can read "Next Bill (estimated, before tax)" in that case.

---

## Files & responsibilities

| File | Change |
|---|---|
| `src/controllers/paas.py` (or new `src/controllers/dashboard.py`) | New `api_dashboard_summary` endpoint. Pick `paas.py` to avoid a new file unless code count merits separation (~80 lines threshold). |
| `src/models/subscription_lifecycle_mixin.py` | Optionally extract `_subscription_total_with_tax(sub)` helper. |
| `src/static/src/paas/services/dashboard_service.js` | NEW. Mirror the shape of `cloud_service.js` / `billing_service.js`. Single `fetchSummary()` method. |
| `src/static/src/paas/pages/dashboard/DashboardPage.js` | Replace G4-P0 stub. `setup()` fetches via service; `state` holds `loading`, `error`, `data`. Replace empty class fields with computed getters that read from `state.data.*`. |
| `src/static/src/paas/pages/dashboard/DashboardPage.xml` | Replace placeholder card with the original 3 stat cards (Members / Billing / Workspaces) populated from `state.data.*`. Add Recent Activity card and (optionally) restore Quick Access — see Out of scope. |
| `src/tests/test_dashboard_summary.py` | NEW. 4-5 backend tests. |
| `src/tests/__init__.py` | Register new test module. |

Total: 1 new model edit (optional), 1 new controller method, 1 new frontend service, 2 frontend file edits, 1 new test file + registration.

---

## Frontend rendering

`DashboardPage.xml` after the change:

```
[Welcome heading - already wired to profileService.partner.name]

[Loading state]: spinner + "Loading dashboard..."
[Error state]:   icon + state.error
[Loaded state]:
  - Members card:    Total / Active (no pending row, since pending=0 always)
  - Billing card:    Credits Left, Month Usage, Next Bill (date + estimate)
  - Workspaces card: Total, Services Running/Total
  - Recent Activity card: list of state.data.recent_activity rows
```

Each monetary value uses `formatPoints(...)` (G1). Dates use `formatDate(...)` from billing_helpers if present; otherwise raw ISO strings are acceptable for sprint scope.

The G4-P0 placeholder card is **removed entirely** in this change — its purpose was a stop-gap.

---

## Test cases

`src/tests/test_dashboard_summary.py`, all `HttpCase` (controller test) + `@tagged('post_install', '-at_install')`.

| Test | Setup | Assertion |
|---|---|---|
| `test_summary_returns_user_block` | Authenticated user with partner | Response `data.user.id`/`name`/`avatar_url` populated correctly |
| `test_summary_returns_billing_block` | Partner with `wallet_balance=576.15` and one active subscription with `sh_plan_price=499` | `data.billing.credits_left == 576.15`; `current_cycle_start`/`end` are valid dates; `next_bill_estimate ≈ 573.85` (with tax) |
| `test_summary_returns_workspaces_block` | One workspace with one running service | `data.workspaces.total == 1`; `services.running == 1`; `services.total == 1`; `limit is None` |
| `test_summary_returns_members_block` | Workspace with 1 owner | `members.total == 1`; `active == 1`; `pending == 0` |
| `test_summary_returns_recent_activity` | Multiple wallet.transaction records | `data.recent_activity` is ordered DESC by `ts`; each row has `ts/actor/action/target/description` keys; topup mapped to `"topup"`, deduct mapped to `"subscription_charge"` |

If any test's fixture is genuinely intractable (e.g., `cloud_service.state='running'` requires a real K8s deployment to set up), simplify to structural assertions only and document the limitation.

---

## TDD ordering

Same discipline as G2-A/G2-B: write test file first (commit ordering), implement, single commit. Verification deferred to post-deploy `kubectl exec`.

---

## Constraints

- **Single commit**: backend + frontend + tests bundle for atomic revert.
- **No DB migration**: no new fields. Manifest version NOT bumped (no `_skip_trial_estimate`-style change to existing schema).
- **In scope**: 6 files (1-2 new, 4 existing).
- **Out of scope**: per the explicit list above. Especially: no new audit log model, no plan-tier limit semantics, no Quick Access tile restoration, no real-time refresh.
- **Commit message format**: `feat:` (new behavior + new endpoint), no `Co-Authored-By` footer.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| `_subscription_total_with_tax` extraction blows scope | Plan accepts the simpler `sum(sh_plan_price)` fallback; document the label change. |
| `cloud_service.state` field name guessed wrong | Read the model file at implementation time; correct field name is verified via `grep`. |
| Frontend cache: G4-P0 placeholder shown until asset bundle regenerates | Same as G2-B: clear `ir_attachment` post-deploy if needed. |
| `wallet.transaction` order is `create_date DESC, id DESC` per upstream model — verified | No mitigation needed; aligns with our needs. |
| User has zero subscriptions | `next_bill_date` and `next_bill_estimate` return `null`; frontend shows "—" for those fields. |
| User has multiple subscriptions | `current_cycle_start` uses the most-recently-started; estimate sums all. Multi-sub UX is a future concern. |
| `recent_activity` mixed transaction types may confuse users | Each row has `description` field which already encodes the type via prefix ("Top-up:", "Subscription invoice:"); rendering just uses description. Action-type mapping is for future filtering UI. |

---

## Acceptance criteria

- [ ] Post-deploy backend tests (5) pass via `kubectl exec`.
- [ ] `GET /api/dashboard/summary` returns the documented shape with non-mock values.
- [ ] Header `CREDITS` value and Dashboard `Billing.credits_left` are byte-equal in the rendered DOM (the original Issue #9 root cause: header showed `$576.15` while card showed `$1,255.00`).
- [ ] `Welcome back, X!` shows the logged-in user's real name (already wired in G4-P0; regression check).
- [ ] Workspaces card shows real counts (no fake `5/10` ratio).
- [ ] Recent Activity card lists actual `wallet.transaction` records, including the bonus-breakdown description from G2-B.
- [ ] No regression in G1 (formatPoints), G2-A (Skip Trial breakdown), G2-B (transaction bonus), G3 (BEST VALUE), G5 (subscription manage), G6 (NotFound).
