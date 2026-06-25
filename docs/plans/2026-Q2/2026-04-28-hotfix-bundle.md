# Hotfix Bundle Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix three high-priority issues discovered during stg exploration on 2026-04-27 — Subscription "Manage" button crash, Dashboard mock data hallucination, and Top-up "BEST VALUE" badge on the wrong pack.

**Architecture:** Three surgical, independent fixes.
- **Task 1 (G5-A)** is a one-line null-coalesce on `event.stopPropagation()`.
- **Task 2 (G3)** adds a backend `is_best_value` computed field on the `topup_plan` model, exposes it via the existing `/api/billing/wallet/topup-plans` endpoint, removes the hardcoded label from XML data, and switches the frontend badge logic from string match to boolean.
- **Task 3 (G4-P0)** replaces the hardcoded mock arrays in `DashboardPage` with empty placeholders and wires the welcome name to `profileService` (mirroring how `Header.js` already does it).

**Tech Stack:** Odoo 18 (Python 3.11), OWL.js, `playwright-cli` for manual UI verification.

**Branch:** `develop` (no worktree; tasks must be run sequentially).

**Reference design:** `docs/plans/2026-04-28-billing-issue-fix-design.md`

---

## Pre-flight

**Step 0.1: Confirm clean working tree**

Run:
```bash
git status
```

Expected:
```
On branch develop
nothing to commit, working tree clean
```

If not clean, stop and resolve before proceeding.

**Step 0.2: Pull latest**

Run:
```bash
git pull origin develop
```

Expected: `Already up to date.` or fast-forward.

---

## Task 1: G5-A — Tolerant `showManage` event handling

**Issue:** Clicking the "Manage" button on a subscription card throws `TypeError: Cannot read properties of undefined (reading 'stopPropagation')`. Root cause: `WoowButton.handleClick` calls `this.props.onClick()` without arguments (verified at `src/static/src/paas/components/button/WoowButton.js:14-19`), so `showManage(ev, sub)` receives `ev = undefined`. The fix is to make the call site tolerant; the structural `WoowButton` refactor is deferred to sprint 2.

**Files:**
- Modify: `src/static/src/paas/pages/billing/SubscriptionListPage.js:150-160` (also fix `showCancel` at line 156-160 — same bug pattern)

**Step 1.1: Read current code**

Run:
```bash
sed -n '150,162p' src/static/src/paas/pages/billing/SubscriptionListPage.js
```

Expected output:
```javascript
  showManage(ev, sub) {
    ev.stopPropagation();
    this.state.selectedSubscription = sub;
    this.state.showServicesModal = true;
  }

  showCancel(ev, sub) {
    ev.stopPropagation();
    this.state.selectedSubscription = sub;
    this.state.showCancelModal = true;
  }
```

**Step 1.2: Apply fix**

Use Edit tool to change `ev.stopPropagation();` to `ev?.stopPropagation();` in **both** `showManage` and `showCancel` (two occurrences).

Verification:
```bash
grep -n "stopPropagation" src/static/src/paas/pages/billing/SubscriptionListPage.js
```

Expected:
```
151:    ev?.stopPropagation();
158:    ev?.stopPropagation();
```

**Step 1.3: Update Odoo module to load the new asset**

Run:
```bash
./scripts/test-addon.sh --no-test 2>/dev/null || ./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```

(Use whichever command matches the developer's environment. The goal is to refresh the asset bundle so the browser picks up the new JS.)

**Step 1.4: Manual verification with playwright-cli**

Run (in order, one per line):
```bash
playwright-cli open --headed https://stg.woowtech.io/woow#/billing/subscriptions
# Authenticate as admin/admin if prompted (Email field, Password field, Log in button)
playwright-cli click "getByRole('button', { name: 'Manage' })"
playwright-cli console
```

Expected:
- `playwright-cli console` reports **0 errors** (previously: 2 errors with `TypeError ... stopPropagation`)
- A modal opens showing the subscription's services list (or whatever `showManage` is intended to render)

If no modal opens but console is clean, that's still a partial success — the fix unblocked the handler. Document any follow-up modal-content issues as a new GitHub issue, do not stack scope here.

**Step 1.5: Commit**

```bash
git add src/static/src/paas/pages/billing/SubscriptionListPage.js
git commit -m "fix: tolerate missing event in subscription showManage/showCancel"
```

---

## Task 2: G3 — Dynamic `is_best_value` computed field

**Issue:** The "BEST VALUE" badge is hardcoded in `topup_plans.xml` on the `100 Credits` pack (5% bonus), but `1000 Credits` (10% bonus) is the actual best value. The fix replaces the hardcoded `label` string with a computed boolean derived from the highest bonus ratio across active plans.

**Files:**
- Test (create): `src/tests/test_topup_plan_best_value.py`
- Modify: `src/models/topup_plan.py` (add `is_best_value` computed field, helper imports)
- Modify: `src/controllers/billing.py:527-538` (add `is_best_value` to response payload)
- Modify: `src/data/topup_plans.xml:18` (remove `<field name="label">BEST VALUE</field>`)
- Modify: `src/tests/__init__.py` (register new test module)
- Modify: `src/static/src/paas/pages/billing/TopupStorePage.js` and/or `.xml` (read `is_best_value` boolean)

### Step 2.1: Write the failing backend test

Create `src/tests/test_topup_plan_best_value.py`:

```python
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestTopupPlanBestValue(TransactionCase):
    """`is_best_value` should be computed from the highest bonus/credits ratio."""

    def setUp(self):
        super().setUp()
        # Deactivate seed plans so this test is isolated.
        self.env['woow_paas_platform.topup_plan'].search([]).write({'active': False})
        self.Plan = self.env['woow_paas_platform.topup_plan']

    def _create(self, name, credits, bonus, active=True):
        return self.Plan.create({
            'name': name,
            'amount': credits,
            'credits': credits,
            'bonus': bonus,
            'active': active,
        })

    def test_highest_ratio_wins(self):
        p100 = self._create('P100', credits=100, bonus=5)     # 5%
        p500 = self._create('P500', credits=500, bonus=30)    # 6%
        p1000 = self._create('P1000', credits=1000, bonus=100)  # 10%
        self.assertFalse(p100.is_best_value)
        self.assertFalse(p500.is_best_value)
        self.assertTrue(p1000.is_best_value)

    def test_zero_bonus_never_best(self):
        # All plans have zero bonus → none is "best value".
        p_a = self._create('A', credits=100, bonus=0)
        p_b = self._create('B', credits=200, bonus=0)
        self.assertFalse(p_a.is_best_value)
        self.assertFalse(p_b.is_best_value)

    def test_inactive_plans_excluded_from_baseline(self):
        # An inactive plan with high bonus should NOT lift the best-value baseline.
        self._create('Retired', credits=100, bonus=50, active=False)  # 50% but retired
        active = self._create('Active', credits=100, bonus=10)         # 10%
        self.assertTrue(active.is_best_value)

    def test_tie_marks_all_winners(self):
        # If two active plans share the top ratio, both are flagged.
        a = self._create('A', credits=100, bonus=10)  # 10%
        b = self._create('B', credits=200, bonus=20)  # 10%
        self.assertTrue(a.is_best_value)
        self.assertTrue(b.is_best_value)
```

### Step 2.2: Register the test

Modify `src/tests/__init__.py` — append at end of import list:

```python
from . import test_topup_plan_best_value
```

### Step 2.3: Run the test to confirm it fails

Run:
```bash
./odoo-bin -c odoo.conf --test-enable --test-tags=woow_paas_platform.TestTopupPlanBestValue --stop-after-init
```

Expected: tests **FAIL** with `AttributeError: 'woow_paas_platform.topup_plan' object has no attribute 'is_best_value'` or similar.

### Step 2.4: Add computed field to the model

Modify `src/models/topup_plan.py` — add `api` import, `is_best_value` field, and `_compute_is_best_value` method.

Final file should be:

```python
from odoo import api, fields, models


class TopupPlan(models.Model):
    _name = 'woow_paas_platform.topup_plan'
    _description = 'Credit Top-up Plan'
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True)
    amount = fields.Float(string='Price (NT$)', required=True)
    credits = fields.Float(string='Credits', required=True)
    bonus = fields.Float(string='Bonus Credits', default=0)
    label = fields.Char(string='Badge Label')
    category = fields.Selection([
        ('all', 'All'),
        ('recommended', 'Recommended'),
        ('large_volume', 'Large Volume'),
    ], string='Category', default='all')
    icon = fields.Char(string='Icon Name', default='toll')
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)

    is_best_value = fields.Boolean(
        string='Best Value',
        compute='_compute_is_best_value',
        help='True iff this plan has the highest bonus/credits ratio among active plans.',
    )

    @api.depends('credits', 'bonus', 'active')
    def _compute_is_best_value(self):
        active_plans = self.search([('active', '=', True)])
        ratios = [
            (p.bonus / p.credits) for p in active_plans if p.credits
        ]
        best_ratio = max(ratios) if ratios else 0.0
        for rec in self:
            ratio = (rec.bonus / rec.credits) if rec.credits else 0.0
            rec.is_best_value = (ratio == best_ratio and ratio > 0)
```

### Step 2.5: Run the test to confirm it passes

Run:
```bash
./odoo-bin -c odoo.conf --test-enable --test-tags=woow_paas_platform.TestTopupPlanBestValue --stop-after-init
```

Expected: 4 tests **PASS**.

### Step 2.6: Expose `is_best_value` in the controller response

Modify `src/controllers/billing.py:527-538` — change the `data` list comprehension to include the new field:

```python
        return {
            'success': True,
            'data': [{
                'id': p.id,
                'name': p.name,
                'amount': p.amount,
                'credits': p.credits,
                'bonus': p.bonus,
                'label': p.label or '',
                'is_best_value': p.is_best_value,
                'icon': p.icon or 'toll',
                'category': p.category or 'all',
            } for p in plans],
        }
```

### Step 2.7: Remove the hardcoded BEST VALUE label from XML data

Modify `src/data/topup_plans.xml`. Remove line 18 (`<field name="label">BEST VALUE</field>`) from the `topup_plan_100` record.

After edit, the `topup_plan_100` record should look like:
```xml
        <record id="topup_plan_100" model="woow_paas_platform.topup_plan">
            <field name="name">NT$ 100 Credits</field>
            <field name="amount">100</field>
            <field name="credits">100</field>
            <field name="bonus">5</field>
            <field name="icon">monetization_on</field>
            <field name="category">recommended</field>
            <field name="sequence">20</field>
        </record>
```

**Note:** The XML file uses `noupdate="1"`, so existing seeded records in the database **will not** be updated by `-u`. To clear the stale `label` from the running staging DB, run:

```bash
./odoo-bin shell -c odoo.conf -d <db_name> <<'EOF'
env['woow_paas_platform.topup_plan'].sudo().search([('label', '=', 'BEST VALUE')]).write({'label': False})
env.cr.commit()
EOF
```

Document this one-time data fix in the commit message.

### Step 2.8: Update frontend to read `is_best_value`

First, locate where the badge is rendered. Run:
```bash
grep -n "label\|BEST VALUE\|best_value" src/static/src/paas/pages/billing/TopupStorePage.xml src/static/src/paas/pages/billing/TopupStorePage.js 2>/dev/null
```

The most likely current pattern is in `TopupStorePage.xml`: a `t-if="plan.label"` branch that renders the label text.

**Change strategy:**
1. Keep the existing `label` field (still useful for arbitrary marketing tags).
2. Add a separate render for `is_best_value` that always renders the literal `BEST VALUE` badge with the same styling.

Edit `TopupStorePage.xml` — find the existing label render block and add a sibling:

```xml
<!-- existing label render (keep as-is) -->
<span t-if="plan.label" class="o_woow_topup_badge"><t t-esc="plan.label"/></span>

<!-- new: dynamic best-value badge -->
<span t-if="plan.is_best_value" class="o_woow_topup_badge o_woow_topup_badge_best"
      t-att-class="'o_woow_topup_badge o_woow_topup_badge_best'">BEST VALUE</span>
```

If the `t-if="plan.label"` condition was used to render the BEST VALUE badge specifically, swap it for `t-if="plan.is_best_value"`.

(The exact edit depends on the current XML; read it first and adapt.)

### Step 2.9: Update Odoo module to apply changes

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```

### Step 2.10: Manual verification with playwright-cli

Run:
```bash
playwright-cli goto https://stg.woowtech.io/woow#/billing/topup
playwright-cli --raw eval "() => [...document.querySelectorAll('.o_woow_topup_badge_best')].map(el => el.closest('[class*=topup_plan_card], [data-plan-id]')?.textContent?.trim().slice(0, 60))"
```

Expected output: an array containing the `1,000 Credits` card text (with `BEST VALUE` badge), **not** the `100 Credits` card.

### Step 2.11: Commit

```bash
git add src/models/topup_plan.py \
        src/controllers/billing.py \
        src/data/topup_plans.xml \
        src/tests/__init__.py \
        src/tests/test_topup_plan_best_value.py \
        src/static/src/paas/pages/billing/TopupStorePage.xml \
        src/static/src/paas/pages/billing/TopupStorePage.js
git commit -m "fix: derive Top-up BEST VALUE badge from bonus ratio (was hardcoded on wrong pack)"
```

(Drop any of the file paths from `git add` that were not actually modified.)

---

## Task 3: G4-P0 — Replace Dashboard mock data with placeholders + real welcome name

**Issue:** `DashboardPage` renders three mock stat cards with fabricated numbers (e.g., `Credits Left $1,255.00` while the header on the same page shows `$576.15`), 5 fake activity entries, and `Welcome back, Sarah!`. This destroys credibility on the post-login screen. The hotfix removes the fake numbers and wires the welcome to `profileService` (same source `Header.js` uses). Full data wiring (real billing/workspace/activity APIs) is sprint 3.

**Files:**
- Modify: `src/static/src/paas/pages/dashboard/DashboardPage.js` (clear mock arrays, inject `profileService`)
- Modify: `src/static/src/paas/pages/dashboard/DashboardPage.xml` (welcome name, "Coming soon" placeholders)

### Step 3.1: Update DashboardPage.js

Replace the entire file content with:

```javascript
/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { WoowButton } from "../../components/button/WoowButton";
import { profileService } from "../../services/profile_service";

export class DashboardPage extends Component {
    static template = "woow_paas_platform.DashboardPage";
    static components = { WoowCard, WoowIcon, WoowButton };

    setup() {
        this.profileService = useState(profileService);
        onMounted(() => profileService.fetch());
    }

    get welcomeName() {
        const name = this.profileService.partner?.name?.trim();
        return name || "there";
    }

    // Hotfix: mock data removed. Real wiring scheduled for sprint 3
    // (see docs/plans/2026-04-28-billing-issue-fix-design.md G4-P2).
    stats = [];
    activities = [];
    quickAccess = [];
}
```

### Step 3.2: Update DashboardPage.xml

Modify `src/static/src/paas/pages/dashboard/DashboardPage.xml`:

1. Replace the welcome heading at line 7:

   **Before:**
   ```xml
   <h1>Welcome back, Sarah!</h1>
   ```

   **After:**
   ```xml
   <h1>Welcome back, <t t-esc="welcomeName"/>!</h1>
   ```

2. Wrap the stats grid (lines 12-37) with a `t-if` so it only renders when populated, and add a placeholder for the empty state. Replace lines 12-37 with:

   ```xml
   <!-- Stats Grid -->
   <div class="o_woow_stats_grid" t-if="stats.length">
       <t t-foreach="stats" t-as="stat" t-key="stat.label">
           <WoowCard shadow="true" class="'o_woow_stat_card'">
               <div class="o_woow_stat_header">
                   <div t-att-class="'o_woow_stat_icon o_woow_stat_icon_' + (stat.color || 'default')">
                       <WoowIcon name="stat.icon" size="24"/>
                   </div>
                   <div class="o_woow_stat_titles">
                       <span class="o_woow_stat_label"><t t-esc="stat.label"/></span>
                       <span t-if="stat.subtitle" class="o_woow_stat_subtitle"><t t-esc="stat.subtitle"/></span>
                   </div>
               </div>
               <div class="o_woow_stat_items">
                   <t t-foreach="stat.items" t-as="item" t-key="item.label">
                       <div t-att-class="'o_woow_stat_item' + (item.highlight ? ' highlight' : '')">
                           <span class="o_woow_item_label"><t t-esc="item.label"/></span>
                           <span class="o_woow_item_value">
                               <t t-esc="item.value"/>
                               <t t-if="item.max">/<t t-esc="item.max"/></t>
                           </span>
                       </div>
                   </t>
               </div>
           </WoowCard>
       </t>
   </div>

   <!-- Coming-soon placeholder while real dashboard data is wired in sprint 3 -->
   <WoowCard t-if="!stats.length" shadow="true" class="'o_woow_dashboard_placeholder'">
       <div class="o_woow_placeholder_content">
           <WoowIcon name="'hourglass_empty'" size="32"/>
           <h3>Dashboard insights coming soon</h3>
           <p>Workspace, billing, and activity summaries will appear here. Check the <strong>Billing</strong> page for current credit balance.</p>
       </div>
   </WoowCard>
   ```

3. Apply the same `t-if` pattern to the Recent Activity card (around lines 41-58) and Quick Access card (around lines 61-82) — wrap each `<WoowCard>` with `t-if="activities.length"` / `t-if="quickAccess.length"` respectively. Since both arrays are now empty, the cards will not render at all in P0 (which is what we want — fewer cards is better than fake cards).

### Step 3.3: Reload Odoo assets

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```

(Asset bundle picks up template/JS changes.)

### Step 3.4: Manual verification with playwright-cli

Run:
```bash
playwright-cli goto https://stg.woowtech.io/woow#/dashboard
playwright-cli --raw eval "() => document.querySelector('.o_woow_welcome h1')?.textContent"
playwright-cli --raw eval "() => document.querySelector('main').innerText.includes('\$1,255.00')"
playwright-cli --raw eval "() => document.querySelector('main').innerText.includes('Sarah')"
playwright-cli --raw eval "() => document.querySelector('.o_woow_dashboard_placeholder')?.textContent.slice(0, 80)"
```

Expected:
1. First call returns `"Welcome back, Mitchell Admin!"` (or whatever the logged-in user's `partner.name` is).
2. Second call returns `false` (no more `$1,255.00` mock value).
3. Third call returns `false` (no more "Sarah").
4. Fourth call returns the placeholder text starting with `Dashboard insights coming soon`.

### Step 3.5: Commit

```bash
git add src/static/src/paas/pages/dashboard/DashboardPage.js \
        src/static/src/paas/pages/dashboard/DashboardPage.xml
git commit -m "fix: remove hardcoded Dashboard mock data; wire welcome to profileService"
```

---

## Done

After all three tasks land:

```bash
git log --oneline -3
```

Expected: three commits in order — `fix: tolerate missing event...`, `fix: derive Top-up BEST VALUE...`, `fix: remove hardcoded Dashboard mock...`.

Push when ready:
```bash
git push origin develop
```

**Follow-up plans** (will be authored after this hotfix bundle ships):
- `2026-04-28-sprint1-formatter-and-router.md` — G1 (formatPoints utility + WoowPoints OWL component) + G6 (NotFoundPage + router fallback)
- `2026-04-28-sprint2-button-and-estimate.md` — G5-B (WoowButton onClick refactor) + G2 (skip-trial-estimate API + bonus metadata)
- `2026-04-28-sprint3-dashboard-summary.md` — G4-P2 (full dashboard summary endpoint + frontend wiring)

## Verification gates between tasks

If Task 1 fails verification, **do not proceed** to Task 2 — the issues are independent but a broken commit blocks the next push. Each task ends with a working dev environment.
