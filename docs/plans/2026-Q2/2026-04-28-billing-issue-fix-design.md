# Billing & Dashboard Issue Fix — Design

**Date**: 2026-04-28
**Scope**: Fix 9 issues discovered during stg.woowtech.io exploration covering Billing, Subscription, Top-up Store, Dashboard, and routing.

---

## Background

During an interactive exploration of the staging environment (stg.woowtech.io) on 2026-04-27, 9 issues were discovered while testing the Skip Trial flow and surrounding billing surfaces. Full per-issue detail lives in the Maestri `issue` note (118 lines, markdown). This document groups those issues by root cause, proposes fixes, and sequences delivery.

**Issue inventory** (from Maestri `issue` note):

| # | Title | Severity |
|---|---|---|
| 1 | Skip Trial dialog does not disclose tax (499 → 573.85) | UX |
| 2 | Invoice amount precision inconsistent across views (574 vs 573.85) | UX |
| 3 | Mixed currency unit notation ($, pts, Points, NT$, Credits) | UX |
| 4 | Top-up bonus rule shown only on purchase page, history strips it | UX |
| 5 | 🐛 SubscriptionListPage `showManage` throws TypeError on click | **BUG (functional block)** |
| 6 | Debit transactions lack negative sign | UX |
| 7 | Top-up Store "BEST VALUE" tag on the wrong pack | UX (misleading) |
| 8 | Unknown route silently falls back to Dashboard, no 404 | UX |
| 9 | 🚨 Dashboard renders entirely hardcoded mock data | **Severe (credibility)** |

---

## Section 1 — Grouping by Root Cause

The 9 issues collapse into 6 thematic groups. Fixing the root cause of a group resolves multiple issues in one change.

| Group | Name | Issues | Common Root Cause |
|---|---|---|---|
| **G1** | Display consistency | #2, #3, #6 | No unified Money/Points formatter; each view hardcodes its own format (precision, prefix/suffix, sign), no single source of truth |
| **G2** | Billing disclosure | #1, #4 | Dialog/record copy does not integrate billing breakdown; Skip Trial preview is a hardcoded `499 pts` rather than a backend `estimate` API; top-up history copy lacks bonus metadata |
| **G3** | Marketing labels | #7 | "BEST VALUE" is a hardcoded view string, not derived from actual bonus % |
| **G4** | Data wiring | #9 | DashboardPage is fully mocked; no wiring to workspace/billing/activity APIs; `Welcome back, X` ignores the session user |
| **G5** | Event contract | #5 | `WoowButton` `onClick` does not consistently forward the native event; handlers assume `ev` exists and call `.stopPropagation()` |
| **G6** | Routing fallback | #8 | Hash router default case falls back to dashboard; no `NotFoundPage` or toast |

**Key observations**:

- **G1 has the widest blast radius** — one formatter resolves 3 issues and prevents recurrence in future billing components.
- **G2 is structurally an architecture issue**, not two separate UX issues — both Skip Trial preview and top-up history should source breakdown data from the backend.
- **G5 is the only functional bug** and lurks anywhere `WoowButton` is used.
- **G4 has the largest cognitive damage** — it is the post-login first impression with internally contradictory numbers (header `$576.15` vs Billing card `$1,255.00`).

---

## Section 2 — Fix Strategy per Group

### G1 — Display Consistency (#2, #3, #6) — frontend / **M**

1. Add `src/static/src/paas/utils/format_points.js` with a single `formatPoints` utility:

   ```js
   formatPoints(value, opts = {})
     opts.signed   : bool   // default false; emits +/- prefix when true
     opts.precision: number // default 2
     opts.unit     : 'pts' | 'Points' // default 'pts'
   ```

2. Add a `WoowPoints` OWL component wrapping the formatter and applying red/green semantic color when `signed: true`.
3. Sweep all billing surfaces (list / detail / header / cards) and replace ad-hoc money rendering with `<WoowPoints />`.
4. Remove `$`, `Points`, `NT$`, `Credits` mixed usage. Standardize on `pts`. The only place where `NT$` remains is the Top-up purchase flow, which must explicitly show the `NT$ X → Y pts` conversion.

### G2 — Billing Disclosure (#1, #4) — frontend + backend / **L**

1. **Backend**: new endpoint `POST /api/cloud-services/services/{id}/skip-trial-estimate`:

   ```json
   {
     "service_id": 1,
     "service_name": "myodoo",
     "untaxed": 499.00,
     "tax_rate": 0.15,
     "tax_amount": 74.85,
     "total": 573.85,
     "currency": "pts",
     "wallet_balance": 1150.00,
     "sufficient": true,
     "shortage": 0.00,
     "billing_cycle_start": "2026-04-27",
     "billing_cycle_end": "2026-05-27"
   }
   ```

   The dialog renders a breakdown table directly from this response. The confirm button is `disabled` iff `!sufficient`, with an inline link to top-up showing the `shortage`.

2. **Backend**: extend `account.move.line` (or whichever transaction model) with a structured `bonus_amount` field. Do NOT bake the bonus into the `name` string — i18n breaks otherwise.
3. **Frontend Transaction History** reads the new field and composes:
   `Top-up: NT$ 1000 + 100 bonus = 1,100 pts`.

### G3 — Marketing Label (#7) — backend (computed field) / **S**

Choose **Option B**: dynamic `is_best_value` computed from actual bonus ratio.

```python
# src/models/topup_pack.py (proposed location)
@api.depends('credits', 'bonus')
def _compute_is_best_value(self):
    all_packs = self.search([('active', '=', True)])
    best_ratio = max((p.bonus / p.credits) for p in all_packs if p.credits)
    for pack in self:
        ratio = pack.bonus / pack.credits if pack.credits else 0
        pack.is_best_value = (ratio == best_ratio and ratio > 0)
```

Adding/changing packs auto-shifts the badge. Future pricing changes never re-introduce this issue.

### G4 — Data Wiring (#9) — frontend + backend / **XL** (two-phase, both required)

**Phase P0 (immediate hotfix, 30 min)**:
- Replace mock cards with skeleton/"Coming soon" placeholders. **Do NOT** keep mock numbers — they actively damage credibility.
- `Welcome back, X` reads `session.user.name`.

**Phase P2 (target end-state, sprint 3)**:
- New backend controller `src/controllers/dashboard.py` exposing `GET /api/dashboard/summary`:

  ```json
  {
    "user": { "id": 1, "name": "Mitchell Admin", "avatar_url": "..." },
    "billing": {
      "credits_left": 576.15,
      "current_cycle_start": "2026-04-27",
      "current_cycle_end": "2026-05-27",
      "current_cycle_usage": 573.85,
      "next_bill_date": "2026-05-27",
      "next_bill_estimate": 573.85
    },
    "workspaces": {
      "total": 1,
      "limit": null,
      "services": { "running": 1, "total": 1 }
    },
    "members": { "total": 1, "active": 1, "pending": 0 },
    "recent_activity": [
      { "ts": "2026-04-27T15:56:00Z", "actor": "Mitchell Admin", "action": "skip_trial", "target": "myodoo" }
    ]
  }
  ```

- `limit: null` means no plan cap → frontend renders `1 active` rather than fake `1/3`.
- All monetary fields share a single source with the header credits indicator (eliminates the $576.15 vs $1,255.00 contradiction).
- `recent_activity` must support at minimum `skip_trial / topup / deploy / delete` action types.

### G5 — Event Contract (#5) — frontend / **A=XS / B=M**

Two-step:

- **A (hotfix, 1 line)**: in `SubscriptionListPage.showManage`, change `ev.stopPropagation()` → `ev?.stopPropagation()`. Unblocks immediately.
- **B (refactor, sprint 2)**: standardize `WoowButton` OWL component — `onClick(ev)` always forwards the native event; align all callers. Prevents the same bug from latent occurrences elsewhere.

### G6 — Routing Fallback (#8) — frontend / **S**

1. In `src/static/src/paas/core/router.js`, add a default fallback handler.
2. New `NotFoundPage` component with a "Back to Dashboard" CTA.
3. Alternative: redirect to `#/dashboard` and show a toast `URL invalid; returned to home` — pick one and document the convention.

---

## Priority Matrix

| Order | Task | Severity | Effort | Sprint |
|---|---|---|---|---|
| 1 | G5-A tolerant fix (1 line) | 🔴 high (blocking) | XS | **hotfix** |
| 2 | G4-P0 (skeleton cards + Welcome name) | 🔴 high (credibility) | S | **hotfix** |
| 3 | G3 dynamic `is_best_value` | 🟡 medium (misleading) | S | **hotfix** |
| 4 | G1 unified `formatPoints` + `WoowPoints` | 🟡 medium | M | sprint 1 |
| 5 | G6 NotFound route | 🟡 medium | S | sprint 1 |
| 6 | G5-B `WoowButton` refactor | 🟢 low (already mitigated) | M | sprint 2 |
| 7 | G2 estimate API + bonus metadata | 🟡 medium | L | sprint 2 |
| 8 | G4-P2 full dashboard wiring | 🔴 high | XL | sprint 3 |

**Hotfix bundle (tasks 1–3)**: ~1 hour total wall time. Eliminates the three first-impression damages — broken button, contradictory numbers, misleading badge.

---

## Section 3 — Architecture & Testing Detail

### 3.1 G1 — Money formatter contract

```js
// src/static/src/paas/utils/format_points.js
formatPoints(value, opts = {})
  opts.signed   : bool   // default false
  opts.precision: number // default 2
  opts.unit     : 'pts' | 'Points' // default 'pts'
  // Output never contains '$' or 'NT$'
  // null / undefined → 'N/A'
  // 0 → '0.00 pts' (no '+' prefix even if signed)
```

```xml
<!-- WoowPoints OWL component -->
<WoowPoints value="record.amount" signed="true" />
<!-- Renders "+573.85 pts" (green) or "-573.85 pts" (red) -->
```

### 3.2 G2 — Estimate API contract

The dialog renders the breakdown directly from the response. Insufficient-balance handling moves from "after click → error" to "before click → button disabled + shortage hint", removing a wasted round-trip.

Top-up bonus is persisted as a structured `bonus_amount` field (not embedded in description strings) so localization and presentation can vary independently.

### 3.3 G3 — Computed best value

The computed field re-evaluates whenever pack credits or bonus change, so the badge always tracks the highest bonus ratio.

### 3.4 G4 — Dashboard summary contract

Single endpoint, single source of truth for the post-login screen. `limit: null` is a deliberate signal that the frontend must not invent fake X/Y ratios.

### 3.5 Test strategy

| Group | Test type | Focus |
|---|---|---|
| G1 | Frontend unit (`formatPoints`) + visual snapshot | 6 boundary cases; cross-component snapshot diff |
| G2 | Backend API integration (K8s Dev Sandbox) | Tax inclusion, insufficient balance, bonus metadata roundtrip |
| G3 | Backend Odoo test (`@api.depends` recompute) | Adding a higher-bonus pack auto-transfers the badge |
| G4 | Backend integration + Playwright E2E | Header `credits_left` and dashboard card must always be equal |
| G5-A | Manual regression + Playwright (modal opens) | Click `Manage` no longer logs a console error |
| G5-B | OWL component unit test (`WoowButton.onClick` props) | Native event is forwarded |
| G6 | Playwright (goto unknown path) | NotFound rendered or toast shown |

### 3.6 Release plan

| Sprint | Scope | Feature flag? |
|---|---|---|
| **hotfix (same day)** | G5-A, G4-P0, G3 | ❌ none |
| sprint 1 | G1 formatter, G6 NotFound | G1 gated by `WOOW_NEW_FORMATTER` for one week with fallback |
| sprint 2 | G5-B `WoowButton` refactor, G2 estimate API | ❌ |
| sprint 3 | G4 full dashboard wiring | API ships first, frontend cuts over after |

### 3.7 Migration & data caveats

- After G1 ships, **historical invoice/transaction descriptions written by the old code (`Top-up: NT$ 1000 Credits`) will not be backfilled**. Older history will continue to display the old strings until the records are regenerated. Document this in release notes.
- G2 backend changes to `account.move.line` (new `bonus_amount` field) require an Odoo data migration to populate the field for existing rows.

---

## Approval

Approved 2026-04-28 by user (interactive brainstorm session). Next step: invoke `writing-plans` skill to expand each prioritized group into a step-by-step implementation plan.
