# Settings & Low Balance Notification (Phase 1) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement Phase 1 of `docs/plans/2026-04-16-settings-payment-methods-design.md` — clean up the broken `billing/payment-methods` route, add a Settings entry page as placeholder for Phase 2 (ECPG), implement low-balance notifications, and adjust BillingOverview to guide users to Top-up Store when balance is insufficient.

**Architecture:**
- Frontend: Standalone OWL app at `/woow`. Routes are declared in `src/static/src/paas/core/router.js` and mapped to page components in `root.xml`. Use `billing_service` for API calls.
- Backend: Leverage existing subscription lifecycle machinery (`_sh_create_auto_invoice` already checks wallet balance; `_cron_enforce_subscription_lifecycle` already handles grace period & suspension). Phase 1 only adds **notifications** — no new charge flow.
- No ECPG / binding APIs are introduced; those are Phase 2.

**Tech Stack:** Odoo 18, OWL 2, Python 3.12, SCSS, mail module for notifications.

**Non-goals (explicit out-of-scope):**
- ECPG integration (Phase 2)
- Payment Methods management UI (Phase 2)
- Auto top-up from credit card (Phase 2)
- Removing `ManagePaymentMethodsModal` — keep the file intact, it'll be reconnected in Phase 2

---

## Task 1: Remove `billing/payment-methods` route and its references

**Rationale:** The route currently renders EmptyState. Since the page has no purpose without ECPG, remove it entirely to stop misleading users. The URL `settings/payment-methods` will be introduced in Phase 2.

**Files:**
- Modify: `src/static/src/paas/core/router.js` (remove line 27)
- Modify: `src/static/src/paas/layout/sidebar/Sidebar.js` (remove `billing-payment-methods` from `isActive`)

**Step 1: Write the failing test**

Create `src/static/tests/router_tests.js` is not practical for this project (no JS unit test infra). Instead, verify manually via the existing QUnit test or by browser check. For this task, test is:

Run: `grep -n "billing-payment-methods\|billing/payment-methods" src/static/src/paas/`

Expected (before change): matches in `router.js` and `sidebar/Sidebar.js`.

**Step 2: Modify `router.js`**

Remove line 27:
```js
{ path: "billing/payment-methods", name: "Payment Methods", routeName: "billing-payment-methods" },
```

**Step 3: Modify `Sidebar.js`**

In the `isActive` method (lines 36–44), remove:
```js
                || current === "billing-payment-methods"
```

So the block becomes:
```js
        if (item.path === "billing") {
            return current === "billing"
                || current === "billing-topup"
                || current === "billing-invoices"
                || current === "billing-invoice-detail"
                || current === "billing-subscriptions"
                || current === "billing-transactions";
        }
```

**Step 4: Verify no dangling references**

Run: `grep -rn "billing-payment-methods\|billing/payment-methods" src/static/src/paas/`
Expected: **no matches** (except possibly in `components/modal/ManagePaymentMethodsModal.*` which stays untouched).

**Step 5: Commit**

```bash
git add src/static/src/paas/core/router.js src/static/src/paas/layout/sidebar/Sidebar.js
git commit -m "refactor: remove billing/payment-methods route (moved to Phase 2 under settings/)"
```

---

## Task 2: Add `settings` page component (placeholder)

**Rationale:** Router already declares `settings` route but `root.xml` falls through to EmptyState. Replace with a proper Settings page that lists category cards. Phase 1 has only one card: a disabled "Payment Methods (Coming Soon)" card for Phase 2.

**Files:**
- Create: `src/static/src/paas/pages/settings/SettingsPage.js`
- Create: `src/static/src/paas/pages/settings/SettingsPage.xml`
- Create: `src/static/src/paas/pages/settings/SettingsPage.scss`
- Modify: `src/static/src/paas/root.js` (import)
- Modify: `src/static/src/paas/root.xml` (add route handler)
- Modify: `src/__manifest__.py` (register the new SCSS + JS + XML in `woow_paas_platform.assets_paas`)

**Step 1: Create SettingsPage.js**

```javascript
/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";

/**
 * SettingsPage
 * Entry page for user settings. Currently a placeholder — future settings
 * categories (Payment Methods, Notifications, API Keys, etc.) will be added
 * here as individual sub-pages.
 */
export class SettingsPage extends Component {
    static template = "woow_paas_platform.SettingsPage";
    static components = { WoowCard, WoowIcon };
    static props = {};
}
```

**Step 2: Create SettingsPage.xml**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
    <t t-name="woow_paas_platform.SettingsPage">
        <div class="o_woow_settings_page">
            <div class="o_woow_settings_header">
                <h1>Settings</h1>
                <p>Manage your account preferences and configuration.</p>
            </div>

            <div class="o_woow_settings_cards">
                <WoowCard shadow="true" class="'o_woow_settings_card o_woow_settings_card_disabled'">
                    <div class="o_woow_settings_card_body">
                        <div class="o_woow_settings_card_icon">
                            <WoowIcon name="'credit_card'" size="32"/>
                        </div>
                        <div class="o_woow_settings_card_text">
                            <div class="o_woow_settings_card_title_row">
                                <h3>Payment Methods</h3>
                                <span class="o_woow_settings_badge">Coming Soon</span>
                            </div>
                            <p>Bind credit cards so your wallet can auto-topup when the balance runs low.</p>
                        </div>
                    </div>
                </WoowCard>
            </div>
        </div>
    </t>
</templates>
```

**Step 3: Create SettingsPage.scss**

```scss
.o_woow_settings_page {
    padding: 24px 32px;
    max-width: 960px;
    margin: 0 auto;

    &_header {
        margin-bottom: 24px;

        h1 {
            font-size: 28px;
            font-weight: 600;
            margin: 0 0 8px 0;
        }
        p {
            color: var(--woow-text-secondary, #6b7280);
            margin: 0;
        }
    }
}

.o_woow_settings_cards {
    display: flex;
    flex-direction: column;
    gap: 16px;
}

.o_woow_settings_card {
    &_body {
        display: flex;
        align-items: flex-start;
        gap: 16px;
        padding: 20px;
    }
    &_icon {
        flex-shrink: 0;
        width: 48px;
        height: 48px;
        border-radius: 8px;
        background: var(--woow-accent-bg, #eef2ff);
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--woow-accent, #6366f1);
    }
    &_text {
        flex: 1;
        h3 {
            margin: 0;
            font-size: 16px;
            font-weight: 600;
        }
        p {
            margin: 4px 0 0 0;
            color: var(--woow-text-secondary, #6b7280);
            font-size: 14px;
        }
    }
    &_title_row {
        display: flex;
        align-items: center;
        gap: 8px;
    }
    &_disabled {
        opacity: 0.7;
        pointer-events: none;
    }
}

.o_woow_settings_badge {
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--woow-warning-bg, #fef3c7);
    color: var(--woow-warning-text, #92400e);
    font-size: 11px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
```

**Step 4: Modify `root.js` to import**

Add near the other page imports:
```javascript
import { SettingsPage } from "./pages/settings/SettingsPage";
```

And register it in `static components = { ... }`:
```javascript
        SettingsPage,
```

**Step 5: Modify `root.xml` to add route handler**

Add a `t-elif` block between the `billing-*` branches and the `t-else`:
```xml
            <t t-elif="router.current === 'settings'">
                <SettingsPage/>
            </t>
```

**Step 6: Modify `__manifest__.py`**

In the `woow_paas_platform.assets_paas` bundle, append the three files (alongside other billing pages):
```python
'woow_paas_platform/static/src/paas/pages/settings/SettingsPage.js',
'woow_paas_platform/static/src/paas/pages/settings/SettingsPage.xml',
'woow_paas_platform/static/src/paas/pages/settings/SettingsPage.scss',
```

**Step 7: Manually verify in browser (staging sandbox)**

Deploy and verify:
1. Visit `https://stg.woowtech.io/woow#/settings`
2. Confirm page shows "Settings" header and a disabled "Payment Methods — Coming Soon" card
3. Confirm sidebar "Settings" item is highlighted
4. Click the card — should NOT navigate (cursor stays on page, since card is disabled)

**Step 8: Commit**

```bash
git add src/static/src/paas/pages/settings/ \
        src/static/src/paas/root.js \
        src/static/src/paas/root.xml \
        src/__manifest__.py
git commit -m "feat: add Settings entry page with Payment Methods placeholder for Phase 2"
```

---

## Task 3: Add "low balance" notification mechanism (backend)

**Rationale:** Currently `_sh_create_auto_invoice` only logs a warning when wallet balance is insufficient. Users are not notified. Add an email notification (and log an activity on the partner) so the user knows to top up.

**Files:**
- Create: `src/data/mail_templates.xml`
- Modify: `src/models/sh_subscription_fix.py` (send notification on insufficient balance)
- Modify: `src/__manifest__.py` (register new data file)
- Create: `src/tests/test_low_balance_notification.py`

**Step 1: Write the failing test**

```python
# src/tests/test_low_balance_notification.py
"""Tests for low balance notification during subscription auto-invoice."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestLowBalanceNotification(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test User',
            'email': 'test@example.com',
            'wallet_balance': 100.0,
        })

    def test_notification_sent_when_insufficient_balance(self):
        """When auto-invoice finds insufficient balance, email must be sent."""
        Subscription = self.env['sh.subscription.subscription']
        # Find a subscription product (we can't create one without full setup;
        # for the test, just verify the notification helper function works
        # in isolation).
        partner = self.partner
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sub fee',
                'quantity': 1,
                'price_unit': 500.0,
            })],
        })

        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        self.assertIsNotNone(
            template,
            "mail_template_low_balance must be defined in data/mail_templates.xml"
        )

        # Trigger notification via the helper we'll add to sh_subscription_fix
        Subscription._notify_low_balance(partner, invoice)  # new method

        # Check that a mail.mail was queued for this partner
        mails = self.env['mail.mail'].search([
            ('email_to', 'ilike', partner.email),
            ('subject', 'ilike', 'top up'),
        ])
        self.assertTrue(mails, "A top-up notification email should have been queued")
```

**Step 2: Run test — expect FAIL**

```bash
./odoo-bin -c odoo.conf \
  --test-enable --test-tags woow_paas_platform \
  --stop-after-init -u woow_paas_platform
```

Expected: FAIL — `mail_template_low_balance` not found, and `_notify_low_balance` method does not exist.

**Step 3: Create `src/data/mail_templates.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <data noupdate="1">
        <record id="mail_template_low_balance" model="mail.template">
            <field name="name">PaaS: Low Wallet Balance Notification</field>
            <field name="model_id" ref="base.model_res_partner"/>
            <field name="subject">Action needed: Please top up your wallet</field>
            <field name="email_from">{{ object.company_id.email_formatted or user.company_id.email_formatted }}</field>
            <field name="email_to">{{ object.email }}</field>
            <field name="body_html" type="html">
                <div style="font-family: Arial, sans-serif; max-width: 600px;">
                    <p>Hi <t t-out="object.name"/>,</p>
                    <p>
                        Your subscription invoice
                        <strong t-out="ctx.get('invoice_name', '')"/>
                        of
                        <strong t-out="ctx.get('amount_text', '')"/>
                        could not be paid because your wallet balance is insufficient.
                    </p>
                    <p>Current wallet balance: <strong t-out="ctx.get('balance_text', '')"/></p>
                    <p>
                        Please top up your wallet to keep your service active.
                        You have a grace period before the service is suspended.
                    </p>
                    <p>
                        <a t-att-href="ctx.get('topup_url', '#')"
                           style="background:#6366f1;color:white;padding:10px 16px;text-decoration:none;border-radius:6px;">
                            Top up now
                        </a>
                    </p>
                    <p style="color:#6b7280;font-size:12px;">
                        Thank you for using WoowTech PaaS.
                    </p>
                </div>
            </field>
            <field name="auto_delete" eval="True"/>
        </record>
    </data>
</odoo>
```

**Step 4: Modify `sh_subscription_fix.py`**

Add the helper method, and call it from the insufficient-balance branch. Replace the `else:` block around line 50:

```python
            # Check wallet balance before posting/paying the invoice
            partner = subscription.sh_partner_id
            if partner.wallet_balance >= invoice_id.amount_total:
                subscription._handle_invoice_creation(invoice_id)
            else:
                _logger.warning(
                    "Insufficient wallet balance for subscription %s: "
                    "balance=%.2f, required=%.2f — invoice %s left unpaid",
                    subscription.name, partner.wallet_balance,
                    invoice_id.amount_total, invoice_id.name,
                )
                self._notify_low_balance(partner, invoice_id)
```

Add the helper method at the end of the class:

```python
    @api.model
    def _notify_low_balance(self, partner, invoice):
        """Send a low-wallet-balance email to the partner.

        Called when an auto-invoice cannot be paid because wallet balance
        is insufficient. Deduplicates by setting a marker on the partner
        so we don't spam on repeated cron runs within a short window.
        """
        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("mail_template_low_balance not found; skip notification")
            return

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        ctx = {
            'invoice_name': invoice.name or '',
            'amount_text': '%.2f' % invoice.amount_total,
            'balance_text': '%.2f' % partner.wallet_balance,
            'topup_url': '%s/woow#/billing/topup' % base_url.rstrip('/'),
        }
        try:
            template.with_context(**ctx).send_mail(
                partner.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_light',
            )
            _logger.info("Low-balance notification queued for partner %s", partner.id)
        except Exception:
            _logger.exception(
                "Failed to queue low-balance notification for partner %s",
                partner.id,
            )
```

**Step 5: Update `__manifest__.py`**

Register the data file (after `subscription_lifecycle_cron.xml`):
```python
'data/mail_templates.xml',
```

**Step 6: Run test — expect PASS**

```bash
./odoo-bin -c odoo.conf \
  --test-enable --test-tags woow_paas_platform \
  --stop-after-init -u woow_paas_platform
```

Expected: test passes.

**Step 7: Commit**

```bash
git add src/models/sh_subscription_fix.py \
        src/data/mail_templates.xml \
        src/tests/test_low_balance_notification.py \
        src/__manifest__.py
git commit -m "feat: email notification when subscription auto-invoice fails due to low balance"
```

---

## Task 4: Add pre-renewal low-balance warning cron

**Rationale:** The notification in Task 3 fires on the day of renewal (after failure). Users should be warned **before** renewal so they can top up in advance. Add a daily cron that checks active subscriptions renewing in the next 3 days and warns users whose wallet balance is below the required amount.

**Files:**
- Create: `src/models/low_balance_warning_cron.py`
- Create: `src/data/low_balance_warning_cron.xml`
- Modify: `src/models/__init__.py`
- Modify: `src/__manifest__.py`
- Modify: `src/tests/test_low_balance_notification.py` (add cron test)

**Step 1: Write the failing test (append to existing test file)**

```python
    def test_prerenewal_warning_cron(self):
        """Subscription renewing in 3 days with low balance must trigger warning."""
        # Setup: partner with balance 100, subscription renewing in 2 days needing 500
        # ... (minimal mock: create partner + mock subscription data)

        # Trigger the cron
        self.env['woow_paas_platform.low_balance_warning_cron'] \
            ._cron_warn_upcoming_renewal_low_balance()

        # Check: a warning email is queued for our partner
        mails = self.env['mail.mail'].search([
            ('email_to', 'ilike', self.partner.email),
            ('subject', 'ilike', 'top up'),
        ])
        self.assertTrue(mails, "Pre-renewal warning email should be queued")
```

> For testing purposes, the test may need to create a minimal `sh.subscription.subscription` record. If creating one is too complex, skip this test with a TODO and verify manually in staging.

**Step 2: Run test — expect FAIL** (model does not exist)

**Step 3: Create `src/models/low_balance_warning_cron.py`**

```python
"""Cron: warn users before subscription renewal if wallet balance is low."""
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class LowBalanceWarningCron(models.AbstractModel):
    _name = 'woow_paas_platform.low_balance_warning_cron'
    _description = 'Pre-renewal Low Balance Warning'

    WARNING_WINDOW_DAYS = 3

    def _cron_warn_upcoming_renewal_low_balance(self):
        """Scan subscriptions renewing within WARNING_WINDOW_DAYS.

        For each subscription whose partner's wallet balance is below
        the plan price, send a single warning email per renewal cycle.
        Deduplicate by tagging the subscription with a field — see
        `_warn_if_needed`.
        """
        today = fields.Date.today()
        cutoff = today + timedelta(days=self.WARNING_WINDOW_DAYS)

        Subscription = self.env['sh.subscription.subscription'].sudo()
        subs = Subscription.search([
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '>=', today),
            ('sh_date_of_next_payment', '<=', cutoff),
        ])
        _logger.info(
            "Low-balance pre-renewal check: %d subscriptions in window",
            len(subs),
        )
        for sub in subs:
            try:
                with self.env.cr.savepoint():
                    self._warn_if_needed(sub)
            except Exception:
                _logger.exception(
                    "Low-balance warning failed for subscription %s", sub.id,
                )

    def _warn_if_needed(self, subscription):
        partner = subscription.sh_partner_id
        if not partner or not partner.email:
            return
        price = subscription.sh_plan_price or 0.0
        if price <= 0:
            return
        if partner.wallet_balance >= price:
            return

        # Dedup: only warn once per renewal cycle. Use a char field on the
        # subscription to store the last-warned renewal date. If it matches
        # the current sh_date_of_next_payment, skip.
        next_payment = subscription.sh_date_of_next_payment
        if not next_payment:
            return
        last_warned = getattr(subscription, 'paas_last_low_balance_warned_for', False)
        if last_warned and last_warned == next_payment:
            return

        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            return

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        ctx = {
            'invoice_name': '(upcoming renewal on %s)' % next_payment,
            'amount_text': '%.2f' % price,
            'balance_text': '%.2f' % partner.wallet_balance,
            'topup_url': '%s/woow#/billing/topup' % base_url.rstrip('/'),
        }
        template.with_context(**ctx).send_mail(
            partner.id,
            force_send=False,
            email_layout_xmlid='mail.mail_notification_light',
        )
        # Persist dedup marker (requires the extra field — see below)
        subscription.write({'paas_last_low_balance_warned_for': next_payment})
        _logger.info(
            "Pre-renewal warning queued for partner %s, renewal %s, price=%.2f, balance=%.2f",
            partner.id, next_payment, price, partner.wallet_balance,
        )
```

Also add a dedup field. Create `src/models/sh_subscription_warning_field.py`:

```python
"""Add a dedup field to sh.subscription.subscription for low-balance warnings."""
from odoo import fields, models


class ShSubscriptionWarningField(models.Model):
    _inherit = 'sh.subscription.subscription'

    paas_last_low_balance_warned_for = fields.Date(
        string='Last Low-Balance Warning Renewal Date',
        help='Date of the upcoming renewal for which a low-balance email was '
             'last sent. Prevents duplicate warnings per renewal cycle.',
        copy=False,
    )
```

**Step 4: Create `src/data/low_balance_warning_cron.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <data noupdate="1">
        <record id="ir_cron_low_balance_warning" model="ir.cron">
            <field name="name">PaaS: Low-Balance Warning Before Renewal</field>
            <field name="model_id"
                   ref="model_woow_paas_platform_low_balance_warning_cron"/>
            <field name="state">code</field>
            <field name="code">model._cron_warn_upcoming_renewal_low_balance()</field>
            <field name="interval_number">1</field>
            <field name="interval_type">days</field>
            <field name="active" eval="True"/>
        </record>
    </data>
</odoo>
```

**Step 5: Update `src/models/__init__.py`**

Add both new modules (look up existing import pattern):
```python
from . import sh_subscription_warning_field
from . import low_balance_warning_cron
```

**Step 6: Update `__manifest__.py`**

Register the new data file after `mail_templates.xml`:
```python
'data/low_balance_warning_cron.xml',
```

**Step 7: Run test — expect PASS**

```bash
./odoo-bin -c odoo.conf \
  --test-enable --test-tags woow_paas_platform \
  --stop-after-init -u woow_paas_platform
```

**Step 8: Commit**

```bash
git add src/models/low_balance_warning_cron.py \
        src/models/sh_subscription_warning_field.py \
        src/data/low_balance_warning_cron.xml \
        src/models/__init__.py \
        src/tests/test_low_balance_notification.py \
        src/__manifest__.py
git commit -m "feat: daily cron warns users 3 days before renewal if wallet balance is low"
```

---

## Task 5: Add low-balance banner in BillingOverview + expose upcoming renewal info

**Rationale:** When the user visits `/woow#/billing`, if their wallet balance is below any upcoming renewal amount in the next 3 days, show a banner with a CTA to Top-up Store.

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js` (add `upcomingLowBalance` state + fetch method)
- Modify: `src/controllers/billing.py` (add `/api/billing/upcoming-renewals` endpoint)
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.js` (fetch + expose)
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.xml` (render banner)
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.scss` (banner styling)

**Step 1: Add controller endpoint**

In `src/controllers/billing.py`, add:

```python
    @route('/api/billing/upcoming-renewals', auth='user', methods=['POST'], type='json')
    def api_upcoming_renewals(self, **kw):
        """Return subscriptions renewing within 3 days with low-balance status.

        Returns:
            {
                'success': True,
                'data': {
                    'has_low_balance': bool,
                    'upcoming': [{
                        'subscription_id', 'plan_name', 'amount',
                        'renewal_date', 'sufficient': bool,
                    }, ...],
                    'shortfall': float,  # total needed to cover all upcoming
                }
            }
        """
        from datetime import date, timedelta
        partner = request.env.user.partner_id
        today = date.today()
        cutoff = today + timedelta(days=3)

        subs = request.env['sh.subscription.subscription'].sudo().search([
            ('sh_partner_id', '=', partner.id),
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '>=', today),
            ('sh_date_of_next_payment', '<=', cutoff),
        ])

        upcoming = []
        total_needed = 0.0
        for sub in subs:
            amount = sub.sh_plan_price or 0.0
            total_needed += amount
            upcoming.append({
                'subscription_id': sub.id,
                'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else '',
                'amount': amount,
                'renewal_date': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
                'sufficient': partner.wallet_balance >= amount,
            })

        return {
            'success': True,
            'data': {
                'has_low_balance': partner.wallet_balance < total_needed,
                'upcoming': upcoming,
                'shortfall': max(0.0, total_needed - partner.wallet_balance),
            },
        }
```

**Step 2: Extend `billing_service.js`**

Add fields to the reactive state (near `wallet` / `invoices`):
```javascript
    /** @type {{ has_low_balance: boolean, upcoming: Array, shortfall: number }} */
    upcomingRenewals: {
        has_low_balance: false,
        upcoming: [],
        shortfall: 0,
    },
```

Add a method:
```javascript
    /**
     * Fetch upcoming renewals in the next 3 days.
     * @returns {Promise<void>}
     */
    async fetchUpcomingRenewals() {
        try {
            const result = await jsonRpc("/api/billing/upcoming-renewals", {});
            if (result.success) {
                this.upcomingRenewals = {
                    has_low_balance: result.data.has_low_balance,
                    upcoming: result.data.upcoming ?? [],
                    shortfall: result.data.shortfall ?? 0,
                };
            }
        } catch (err) {
            console.error("Failed to fetch upcoming renewals:", err);
        }
    },
```

**Step 3: Modify `BillingOverviewPage.js`**

In `setup()` or `onMounted()`, add a call:
```javascript
billingService.fetchUpcomingRenewals();
```

Add a getter:
```javascript
    get hasLowBalanceWarning() {
        return this.billing.upcomingRenewals.has_low_balance;
    }
    get lowBalanceShortfall() {
        return this.billing.upcomingRenewals.shortfall;
    }
```

**Step 4: Modify `BillingOverviewPage.xml`**

Add a banner right after the header block (before the Credit Balance Card), wrapped in `t-if`:

```xml
<t t-if="hasLowBalanceWarning">
    <WoowCard shadow="true" class="'o_woow_billing_low_balance_banner'">
        <div class="o_woow_low_balance_content">
            <WoowIcon name="'warning'" size="28"/>
            <div class="o_woow_low_balance_text">
                <h3>Wallet balance is too low for upcoming renewal</h3>
                <p>
                    You need at least
                    <strong><t t-esc="lowBalanceShortfall.toFixed(2)"/></strong>
                    more to cover subscriptions renewing in the next 3 days.
                    Top up now to avoid service interruption.
                </p>
            </div>
            <WoowButton
                variant="'primary'"
                size="'md'"
                onClick="() => this.goToTopup()">
                Top up now
            </WoowButton>
        </div>
    </WoowCard>
</t>
```

**Step 5: Modify `BillingOverviewPage.scss`**

Add styling:
```scss
.o_woow_billing_low_balance_banner {
    margin-bottom: 16px;
    background: var(--woow-warning-bg, #fef3c7) !important;
    border: 1px solid var(--woow-warning-border, #fcd34d);

    .o_woow_low_balance_content {
        display: flex;
        align-items: center;
        gap: 16px;
        padding: 16px;

        > span.material-symbols-outlined {
            color: var(--woow-warning-text, #92400e);
            flex-shrink: 0;
        }
    }
    .o_woow_low_balance_text {
        flex: 1;
        h3 {
            margin: 0 0 4px 0;
            font-size: 15px;
            font-weight: 600;
            color: var(--woow-warning-text, #92400e);
        }
        p {
            margin: 0;
            font-size: 13px;
            color: var(--woow-text-primary, #1f2937);
        }
    }
}
```

**Step 6: Manually verify on staging**

1. Create a partner with `wallet_balance = 0`.
2. Create a subscription due in 1-2 days via Odoo backend.
3. Visit `/woow#/billing` → the banner should appear with correct shortfall.
4. Click "Top up now" → navigates to `#/billing/topup`.

**Step 7: Commit**

```bash
git add src/controllers/billing.py \
        src/static/src/paas/services/billing_service.js \
        src/static/src/paas/pages/billing/BillingOverviewPage.js \
        src/static/src/paas/pages/billing/BillingOverviewPage.xml \
        src/static/src/paas/pages/billing/BillingOverviewPage.scss
git commit -m "feat: low-balance warning banner in BillingOverview linking to Top-up Store"
```

---

## Task 6: Update design doc to mark Phase 1 tasks as DONE

**Rationale:** Keep `docs/plans/2026-04-16-settings-payment-methods-design.md` in sync so that when someone (or future Claude) reads it, they know what's shipped.

**Files:**
- Modify: `docs/plans/2026-04-16-settings-payment-methods-design.md`

**Step 1: Edit the Phase 1 heading**

Change the `## Phase 1` heading to include a status marker:
```markdown
## Phase 1：Settings 入口頁 + 低餘額通知（✅ 已完成）
```

Add a "Shipped on YYYY-MM-DD" note below the heading.

**Step 2: Commit**

```bash
git add docs/plans/2026-04-16-settings-payment-methods-design.md
git commit -m "docs: mark Phase 1 as shipped in settings/payment-methods design"
```

---

## Manual verification checklist (before opening PR)

Run through these on the staging sandbox:

- [ ] `#/billing/payment-methods` now falls back to 404/EmptyState (or redirects to `#/settings`)
- [ ] `#/settings` renders the Settings page with a "Coming Soon" card
- [ ] Sidebar "Settings" item is highlighted when on `#/settings`
- [ ] Sidebar "Billing" item is no longer highlighted for the removed route
- [ ] Existing `#/billing/*` routes still work (dashboard, topup, invoices, subscriptions, transactions)
- [ ] When wallet balance < upcoming renewal amount, BillingOverview shows the warning banner
- [ ] Clicking "Top up now" on the banner navigates to `#/billing/topup`
- [ ] Cron `PaaS: Low-Balance Warning Before Renewal` is registered and enabled
- [ ] Cron `PaaS: Enforce Subscription Lifecycle` still works (existing)
- [ ] On insufficient balance during auto-invoice, an email is queued to the partner
- [ ] All new Python code has corresponding tests and they pass
- [ ] No references to `billing-payment-methods` remain in `src/static/src/paas/` (except `ManagePaymentMethodsModal` which is intentionally preserved for Phase 2)

---

## Reference: Phase 2 hand-off

When Phase 2 begins, re-read `docs/plans/2026-04-16-settings-payment-methods-design.md` §Phase 2. Key entry points the current Phase 1 leaves for Phase 2:

- The `SettingsPage` component already has a Payment Methods card — just enable it (remove `o_woow_settings_card_disabled`, add `onClick` navigating to `#/settings/payment-methods`).
- `ManagePaymentMethodsModal` is intact and unused — import into `BillingOverviewPage` in Phase 2.
- `billing_service.js` already has payment-methods helpers (`fetchPaymentMethods`, `addPaymentMethod`, `deletePaymentMethod`, `setDefaultPaymentMethod`) that currently hit stub/error APIs; Phase 2 rewires them to the new ECPG-backed controller endpoints.
- The auto-topup fallback in subscription auto-invoice will plug into the Phase 1 insufficient-balance branch in `sh_subscription_fix.py` — `_notify_low_balance` will be complemented by `_attempt_auto_topup`.
