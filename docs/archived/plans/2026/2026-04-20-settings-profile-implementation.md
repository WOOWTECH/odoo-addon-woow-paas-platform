---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-20-settings-profile-implementation.md
archived: 2026-07-19
archived-reason: "已落地（Settings UI build phase）；現況=src/static/src/paas"
---

# Phase 2c #6 Implementation Plan: /settings/profile page

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the logged-in user edit their own `name`, `email`, `phone` in the SPA at `#/settings/profile`, and wire the Header to show real partner data (removing the hardcoded "Sarah Wilson / PRO PLAN / SW").

**Architecture:** New `ProfileController` exposes `POST /api/me` (read) and `POST /api/me/update` (write, whitelisted to 3 fields, always writes current session partner). New reactive `profileService` is shared by a new `ProfilePage` OWL component and the existing `Header`. Settings list gains a "Profile" card linking to the new route.

**Tech Stack:** Odoo 18 controllers (`@route` + `type='json'`), OWL 2 components + reactive state, HttpCase for backend tests.

**Design reference:** `docs/plans/2026-04-20-settings-profile-design.md`

---

## Pre-flight

**Step 0.1: Confirm branch**

Run: `git branch --show-current`
Expected: `develop`

**Step 0.2: Confirm tree state**

Run: `git status --short`
Expected: at most untracked files (screenshots, `.claude/skills/`, `extra/extra-addons/` dirs). No staged/modified tracked files related to this work.

---

## Task 1: Backend — ProfileController + HttpCase tests (TDD)

**Files:**
- Create: `src/controllers/profile.py`
- Create: `src/tests/test_profile_api.py`
- Modify: `src/controllers/__init__.py` (add import)

Follows Phase 2c #2's test convention: `self.partner = self.env.ref('base.partner_admin')` in setUp (`self.env.user` is `__system__` in HttpCase; HTTP session is admin).

### Step 1.1: Create the test file FIRST (TDD red)

Create `src/tests/test_profile_api.py`:

```python
"""Tests for /api/me profile endpoints."""
from odoo.tests.common import HttpCase


class TestProfileApi(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        # self.env.user is __system__ (partner=OdooBot) in HttpCase, but the HTTP
        # session authenticated as admin (partner=Administrator). Use the admin
        # partner explicitly; see test_payment_methods_api.py for context.
        self.partner = self.env.ref('base.partner_admin')

    def _post(self, url, params_json):
        return self.url_open(
            url,
            data='{"jsonrpc":"2.0","method":"call","params":' + params_json + '}',
            headers={'Content-Type': 'application/json'},
        )

    def test_me_returns_current_partner(self):
        response = self._post('/api/me', '{}')
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['id'], self.partner.id)
        self.assertEqual(body['data']['name'], self.partner.name)
        self.assertEqual(body['data']['email'], self.partner.email or '')
        self.assertEqual(
            body['data']['avatar_url'],
            f'/web/image/res.partner/{self.partner.id}/image_128',
        )

    def test_update_saves_fields(self):
        response = self._post(
            '/api/me/update',
            '{"name":"New Name","email":"new@example.com","phone":"+886-912-000-000"}',
        )
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.name, 'New Name')
        self.assertEqual(self.partner.email, 'new@example.com')
        self.assertEqual(self.partner.phone, '+886-912-000-000')

    def test_update_rejects_empty_name(self):
        response = self._post('/api/me/update', '{"name":""}')
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'name_required')

    def test_update_rejects_whitespace_only_name(self):
        response = self._post('/api/me/update', '{"name":"   "}')
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'name_required')

    def test_update_rejects_invalid_email(self):
        response = self._post(
            '/api/me/update', '{"name":"OK","email":"not-an-email"}',
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'invalid_email')

    def test_update_accepts_empty_email(self):
        response = self._post('/api/me/update', '{"name":"OK","email":""}')
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")

    def test_update_ignores_foreign_partner_id(self):
        other = self.env['res.partner'].sudo().create({'name': 'Other Person'})
        original_name = other.name
        response = self._post(
            '/api/me/update',
            f'{{"partner_id":{other.id},"name":"Hijacked"}}',
        )
        body = response.json()['result']
        self.assertTrue(body['success'], f"Unexpected: {body}")
        other.invalidate_recordset()
        self.assertEqual(other.name, original_name,
                         "Endpoint must ignore partner_id from body")
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.name, 'Hijacked')
```

### Step 1.2: Run tests, verify red

Run (via staging pod; see Task 4 for local alternative):

```bash
POD=$(kubectl get pods -n dev-sandbox-stg -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')
kubectl cp src/tests/test_profile_api.py dev-sandbox-stg/$POD:/opt/bitnami/odoo/custom-addons/woow_paas_platform/tests/test_profile_api.py
kubectl exec -n dev-sandbox-stg deploy/stg-odoo-dev-sandbox-odoo -- bash -c "
psql 'postgresql://odoo:odoo@stg-odoo-dev-sandbox-postgres/postgres' -c \"DROP DATABASE IF EXISTS test_profile;\" >/dev/null 2>&1
psql 'postgresql://odoo:odoo@stg-odoo-dev-sandbox-postgres/postgres' -c \"CREATE DATABASE test_profile TEMPLATE template0;\" >/dev/null 2>&1
odoo -c /etc/odoo/odoo.conf -d test_profile -i woow_paas_platform \
  --test-enable --test-tags '/woow_paas_platform:TestProfileApi' \
  --http-port 8899 --stop-after-init --without-demo=all 2>&1 | tail -40
"
```

Expected: all 7 tests FAIL with 404 or "not found" errors because `/api/me` routes don't exist yet.

### Step 1.3: Create the controller

Create `src/controllers/profile.py`:

```python
from __future__ import annotations

import logging
import re
from typing import Any

from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

# Error codes — stable contract for frontend
ERR_NAME_REQUIRED = 'name_required'
ERR_INVALID_EMAIL = 'invalid_email'

# Accepts typical emails; keeps it simple (RFC 5322 is intentionally wider).
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ProfileController(Controller):
    _name = 'woow_paas_platform.profile'

    # ==================== Internal helpers ====================

    def _me_guard(self):
        """Return the current session user's partner (sudoed for write access).

        Security invariant: this is the ONLY way the two endpoints below
        identify the target partner. Callers cannot supply a partner_id.
        """
        return request.env.user.partner_id.sudo()

    def _serialize(self, partner):
        return {
            'id': partner.id,
            'name': partner.name or '',
            'email': partner.email or '',
            'phone': partner.phone or '',
            'avatar_url': f'/web/image/res.partner/{partner.id}/image_128',
        }

    # ==================== Endpoints ====================

    @route("/api/me", auth="user", methods=["POST"], type="json")
    def api_me(self, **kw) -> dict[str, Any]:
        """Return the current session user's partner profile."""
        return {'success': True, 'data': self._serialize(self._me_guard())}

    @route("/api/me/update", auth="user", methods=["POST"], type="json")
    def api_me_update(
        self,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """Update the current user's partner name/email/phone.

        Only these three fields are accepted. All other kwargs (including
        any `partner_id` a caller might supply) are ignored — writes always
        target `request.env.user.partner_id`.
        """
        partner = self._me_guard()

        if name is None or not name.strip():
            return {
                'success': False,
                'error': 'Name is required',
                'code': ERR_NAME_REQUIRED,
            }

        email_clean = (email or '').strip()
        if email_clean and not EMAIL_RE.match(email_clean):
            return {
                'success': False,
                'error': 'Invalid email format',
                'code': ERR_INVALID_EMAIL,
            }

        partner.write({
            'name': name.strip(),
            'email': email_clean or False,
            'phone': (phone or '').strip() or False,
        })
        return {'success': True, 'data': self._serialize(partner)}
```

### Step 1.4: Register the controller

Edit `src/controllers/__init__.py`. Find the existing imports and add `profile`:

```python
# Before:
from . import billing
from . import paas
# ... (other imports)

# After (add profile alongside the others):
from . import billing
from . import paas
from . import profile
# ... (other imports, if any)
```

(Use the actual ordering in the current file; the point is one new `from . import profile` line.)

### Step 1.5: Re-run tests, verify green

Re-run the same command as Step 1.2 (with updated profile.py + __init__.py also copied into the pod):

```bash
POD=$(kubectl get pods -n dev-sandbox-stg -l app.kubernetes.io/component=odoo -o jsonpath='{.items[0].metadata.name}')
kubectl cp src/controllers/profile.py dev-sandbox-stg/$POD:/opt/bitnami/odoo/custom-addons/woow_paas_platform/controllers/profile.py
kubectl cp src/controllers/__init__.py dev-sandbox-stg/$POD:/opt/bitnami/odoo/custom-addons/woow_paas_platform/controllers/__init__.py
kubectl exec -n dev-sandbox-stg deploy/stg-odoo-dev-sandbox-odoo -- bash -c "
psql 'postgresql://odoo:odoo@stg-odoo-dev-sandbox-postgres/postgres' -c \"DROP DATABASE IF EXISTS test_profile;\" >/dev/null 2>&1
psql 'postgresql://odoo:odoo@stg-odoo-dev-sandbox-postgres/postgres' -c \"CREATE DATABASE test_profile TEMPLATE template0;\" >/dev/null 2>&1
odoo -c /etc/odoo/odoo.conf -d test_profile -i woow_paas_platform \
  --test-enable --test-tags '/woow_paas_platform:TestProfileApi' \
  --http-port 8899 --stop-after-init --without-demo=all 2>&1 | tail -30
"
```

Expected: `0 failed, 0 error(s) of 7 tests when loading database 'test_profile'`.

If any fail, STOP. Fix within this task — do not move to Task 2 with red tests.

### Step 1.6: Commit

```bash
git add src/controllers/profile.py src/controllers/__init__.py src/tests/test_profile_api.py
git status --short   # expect only these 3 files M/A
git commit -m "$(cat <<'EOF'
feat(profile): /api/me GET + update endpoints with HttpCase tests

New ProfileController exposes POST /api/me (read current partner) and
POST /api/me/update (write name/email/phone). Security: both endpoints
derive the target partner from request.env.user.partner_id; the update
endpoint whitelists exactly 3 fields and ignores any partner_id in the
body. Validation: name required (non-blank), email optional but must
match simple regex if provided.

7 HttpCase tests cover get, save, empty/whitespace name, invalid email,
empty email, and the foreign-partner_id ignoring guard.
EOF
)"
```

---

## Task 2: Frontend — profile_service + ProfilePage + routing

**Files:**
- Create: `src/static/src/paas/services/profile_service.js`
- Create: `src/static/src/paas/pages/settings/ProfilePage.js`
- Create: `src/static/src/paas/pages/settings/ProfilePage.xml`
- Create: `src/static/src/paas/pages/settings/ProfilePage.scss`
- Modify: `src/static/src/paas/core/router.js` (add route entry)
- Modify: `src/static/src/paas/root.js` (import + register component)
- Modify: `src/static/src/paas/root.xml` (mount on new routeName)
- Modify: `src/__manifest__.py` (register new asset files if needed)

### Step 2.1: Create profile_service.js

Create `src/static/src/paas/services/profile_service.js`. Mirror `billing_service.js`'s `jsonRpc` pattern (timeout handling, error normalization). Read `billing_service.js` first to copy the helper.

```javascript
/** @odoo-module **/
import { reactive } from "@odoo/owl";

const REQUEST_TIMEOUT_MS = 30000;

async function jsonRpc(url, params, timeoutMs = REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
        response = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                jsonrpc: "2.0",
                method: "call",
                params,
                id: Math.floor(Math.random() * 1000000),
            }),
            signal: controller.signal,
        });
    } catch (error) {
        if (error.name === "AbortError") throw new Error("Request timed out.");
        throw new Error("Network error.");
    } finally {
        clearTimeout(timeoutId);
    }
    if (!response.ok) {
        if (response.status === 401) throw new Error("Session expired. Please refresh.");
        if (response.status === 403) throw new Error("Access denied.");
        if (response.status >= 500) throw new Error("Server error.");
        throw new Error(`Request failed (${response.status})`);
    }
    const json = await response.json();
    if (json.error) throw new Error(json.error.message || "Request failed");
    return json.result;
}

/**
 * profileService — reactive store for the current user's partner profile.
 * Shared by Header (for name/avatar) and ProfilePage (for edit).
 */
export const profileService = reactive({
    partner: null,        // {id, name, email, phone, avatar_url} or null
    loading: false,
    error: null,

    async fetch() {
        if (this.loading) return;
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/me", {});
            if (!result.success) throw new Error(result.error || "Failed to load profile");
            this.partner = result.data;
        } catch (err) {
            this.error = err.message || String(err);
        } finally {
            this.loading = false;
        }
    },

    /**
     * @param {{name?: string, email?: string, phone?: string}} patch
     * @returns {Promise<{success: boolean, error?: string, code?: string}>}
     */
    async update(patch) {
        this.loading = true;
        this.error = null;
        try {
            const result = await jsonRpc("/api/me/update", patch);
            if (result.success) {
                this.partner = result.data;
            } else {
                this.error = result.error || "Update failed";
            }
            return result;
        } catch (err) {
            const msg = err.message || String(err);
            this.error = msg;
            return { success: false, error: msg };
        } finally {
            this.loading = false;
        }
    },
});
```

### Step 2.2: Create ProfilePage.js

Create `src/static/src/paas/pages/settings/ProfilePage.js`:

```javascript
/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { profileService } from "../../services/profile_service";
import { router } from "../../core/router";

const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;

export class ProfilePage extends Component {
    static template = "woow_paas_platform.ProfilePage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
        this.profile = useState(profileService);
        this.state = useState({
            form: { name: "", email: "", phone: "" },
            fieldErrors: {},          // {name: "...", email: "..."}
            saving: false,
            saveSuccess: false,
            serverError: null,
        });

        onMounted(async () => {
            if (!this.profile.partner) {
                await profileService.fetch();
            }
            this._resetFormFromService();
        });
    }

    _resetFormFromService() {
        const p = this.profile.partner;
        if (!p) return;
        this.state.form.name = p.name || "";
        this.state.form.email = p.email || "";
        this.state.form.phone = p.phone || "";
        this.state.fieldErrors = {};
        this.state.serverError = null;
    }

    get isLoading() {
        return this.profile.loading && !this.profile.partner;
    }

    get avatarUrl() {
        return this.profile.partner?.avatar_url || "";
    }

    get displayName() {
        return this.profile.partner?.name || "—";
    }

    _validate() {
        const errors = {};
        if (!this.state.form.name.trim()) {
            errors.name = "Name is required";
        }
        if (this.state.form.email.trim() && !EMAIL_RE.test(this.state.form.email.trim())) {
            errors.email = "Invalid email format";
        }
        this.state.fieldErrors = errors;
        return Object.keys(errors).length === 0;
    }

    async onSave() {
        this.state.saveSuccess = false;
        this.state.serverError = null;
        if (!this._validate()) return;

        this.state.saving = true;
        const result = await profileService.update({
            name: this.state.form.name.trim(),
            email: this.state.form.email.trim(),
            phone: this.state.form.phone.trim(),
        });
        this.state.saving = false;

        if (result.success) {
            this._resetFormFromService();
            this.state.saveSuccess = true;
            // Auto-dismiss banner after 3s
            setTimeout(() => { this.state.saveSuccess = false; }, 3000);
        } else if (result.code === "name_required") {
            this.state.fieldErrors = { name: result.error };
        } else if (result.code === "invalid_email") {
            this.state.fieldErrors = { email: result.error };
        } else {
            this.state.serverError = result.error || "Update failed";
        }
    }

    onCancel() {
        this._resetFormFromService();
    }

    goBack() {
        this.router.navigate("settings");
    }
}
```

### Step 2.3: Create ProfilePage.xml

Create `src/static/src/paas/pages/settings/ProfilePage.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
    <t t-name="woow_paas_platform.ProfilePage">
        <div class="o_woow_profile_page">
            <div class="o_woow_page_header">
                <div class="o_woow_breadcrumb">
                    <a href="#/settings">Settings</a>
                    <span class="o_woow_breadcrumb_sep">›</span>
                    <span>Profile</span>
                </div>
                <h1>Profile</h1>
                <p>Edit your name, email, and phone number.</p>
            </div>

            <t t-if="state.saveSuccess">
                <div class="o_woow_success_banner" role="alert">
                    <WoowIcon name="'check_circle'" size="24"/>
                    <span>Profile saved.</span>
                </div>
            </t>

            <t t-if="state.serverError">
                <div class="o_woow_error_banner" role="alert">
                    <WoowIcon name="'error'" size="20"/>
                    <span t-esc="state.serverError"/>
                </div>
            </t>

            <t t-if="isLoading">
                <div class="o_woow_loading">Loading profile…</div>
            </t>

            <t t-else="">
                <WoowCard shadow="true" class="'o_woow_profile_card'">
                    <div class="o_woow_profile_header">
                        <div class="o_woow_profile_avatar">
                            <img t-if="avatarUrl" t-att-src="avatarUrl" alt=""/>
                        </div>
                        <div class="o_woow_profile_name" t-esc="displayName"/>
                    </div>

                    <div class="o_woow_profile_form">
                        <div class="o_woow_field">
                            <label for="profile_name">Name</label>
                            <input id="profile_name" type="text"
                                   t-model="state.form.name"
                                   t-att-class="state.fieldErrors.name ? 'o_woow_input_error' : ''"/>
                            <t t-if="state.fieldErrors.name">
                                <div class="o_woow_field_error" t-esc="state.fieldErrors.name"/>
                            </t>
                        </div>

                        <div class="o_woow_field">
                            <label for="profile_email">Email</label>
                            <input id="profile_email" type="email"
                                   t-model="state.form.email"
                                   t-att-class="state.fieldErrors.email ? 'o_woow_input_error' : ''"/>
                            <t t-if="state.fieldErrors.email">
                                <div class="o_woow_field_error" t-esc="state.fieldErrors.email"/>
                            </t>
                        </div>

                        <div class="o_woow_field">
                            <label for="profile_phone">Phone</label>
                            <input id="profile_phone" type="tel"
                                   t-model="state.form.phone"/>
                            <div class="o_woow_field_hint">
                                Any format accepted. ECPay binding will auto-strip spaces and dashes.
                            </div>
                        </div>

                        <div class="o_woow_profile_actions">
                            <WoowButton variant="'secondary'" size="'md'" onClick.bind="onCancel"
                                        disabled="state.saving">
                                Cancel
                            </WoowButton>
                            <WoowButton variant="'primary'" size="'md'" onClick.bind="onSave"
                                        disabled="state.saving">
                                <t t-if="state.saving">Saving…</t>
                                <t t-else="">Save</t>
                            </WoowButton>
                        </div>
                    </div>
                </WoowCard>
            </t>
        </div>
    </t>
</templates>
```

### Step 2.4: Create ProfilePage.scss

Create `src/static/src/paas/pages/settings/ProfilePage.scss`. Reuse existing design tokens (`$woow-*` from the project's SCSS vars).

```scss
.o_woow_profile_page {
    padding: 1.5rem 2rem;
    max-width: 40rem;
    margin: 0 auto;

    .o_woow_page_header {
        margin-bottom: 1.5rem;

        .o_woow_breadcrumb {
            font-size: 0.8125rem;
            color: $woow-text-secondary;
            margin-bottom: 0.5rem;

            a {
                color: $woow-text-secondary;
                text-decoration: none;
                &:hover { color: $woow-primary; }
            }
            .o_woow_breadcrumb_sep { margin: 0 0.5rem; opacity: 0.6; }
        }

        h1 {
            font-size: 1.75rem;
            font-weight: 600;
            margin: 0 0 0.5rem 0;
            color: $woow-text-primary;
        }
        p { margin: 0; color: $woow-text-secondary; }
    }
}

.o_woow_profile_card {
    .o_woow_profile_header {
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 1.5rem;
        border-bottom: 1px solid rgba($woow-text-secondary, 0.1);

        .o_woow_profile_avatar {
            width: 64px;
            height: 64px;
            border-radius: 50%;
            overflow: hidden;
            background: rgba($woow-primary, 0.1);
            display: flex;
            align-items: center;
            justify-content: center;

            img { width: 100%; height: 100%; object-fit: cover; }
        }
        .o_woow_profile_name {
            font-size: 1.25rem;
            font-weight: 600;
            color: $woow-text-primary;
        }
    }
}

.o_woow_profile_form {
    padding: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
}

.o_woow_field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;

    label {
        font-size: 0.8125rem;
        font-weight: 500;
        color: $woow-text-secondary;
    }

    input {
        padding: 0.5rem 0.75rem;
        border: 1px solid rgba($woow-text-secondary, 0.2);
        border-radius: 0.375rem;
        font-size: 0.9375rem;

        &:focus {
            outline: none;
            border-color: $woow-primary;
            box-shadow: 0 0 0 3px rgba($woow-primary, 0.15);
        }
        &.o_woow_input_error {
            border-color: #dc2626;
        }
    }
    .o_woow_field_error {
        font-size: 0.8125rem;
        color: #dc2626;
    }
    .o_woow_field_hint {
        font-size: 0.75rem;
        color: $woow-text-secondary;
    }
}

.o_woow_profile_actions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    margin-top: 0.5rem;
}
```

### Step 2.5: Register route

Edit `src/static/src/paas/core/router.js`. Add the new route entry in the `routes` array next to the existing `settings/payment-methods` entries (around line 29-30):

```javascript
    { path: "settings", name: "Settings", routeName: "settings" },
    { path: "settings/profile", name: "Profile", routeName: "settings-profile" },
    { path: "settings/payment-methods", name: "Payment Methods", routeName: "settings-payment-methods" },
    { path: "settings/payment-methods/add", name: "Add Payment Method", routeName: "settings-payment-methods-add" },
```

Order matters only if patterns clash; these are all exact paths, so any order works — keep them grouped under `settings/*`.

### Step 2.6: Mount ProfilePage in root

Edit `src/static/src/paas/root.js`. Add import near the other settings imports:

```javascript
import { ProfilePage } from "./pages/settings/ProfilePage";
```

And register in components:

```javascript
    static components = {
        ...
        SettingsPage,
        ProfilePage,       // ← new
        PaymentMethodsListPage,
        AddPaymentMethodPage,
        ...
    };
```

Edit `src/static/src/paas/root.xml`. Add a new `<t t-elif>` for the new route, placed near the existing `settings` route dispatch (around line 67):

```xml
            <t t-elif="router.current === 'settings'">
                <SettingsPage/>
            </t>
            <t t-elif="router.current === 'settings-profile'">
                <ProfilePage/>
            </t>
            <t t-elif="router.current === 'settings-payment-methods'">
                <PaymentMethodsListPage/>
            </t>
```

### Step 2.7: Register SCSS asset in manifest

Read `src/__manifest__.py`. Find the `woow_paas_platform.assets_paas` bundle and add the new SCSS file next to other settings SCSS (search for `PaymentMethodsListPage.scss` as reference). Pattern:

```python
"woow_paas_platform.assets_paas": [
    ...
    "woow_paas_platform/static/src/paas/pages/settings/SettingsPage.scss",
    "woow_paas_platform/static/src/paas/pages/settings/ProfilePage.scss",   # ← new
    "woow_paas_platform/static/src/paas/pages/settings/PaymentMethodsListPage.scss",
    ...
]
```

JS/XML files are typically picked up by glob patterns — check if new JS/XML need explicit registration. If the manifest uses glob like `"woow_paas_platform/static/src/paas/**/*.js"`, no registration needed for JS/XML. If explicit list, add each new file.

### Step 2.8: Visual sanity via staging deploy (later in Task 4)

Do NOT deploy yet. Commit first, then verify in Task 4.

### Step 2.9: Commit

```bash
git add src/static/src/paas/services/profile_service.js \
        src/static/src/paas/pages/settings/ProfilePage.js \
        src/static/src/paas/pages/settings/ProfilePage.xml \
        src/static/src/paas/pages/settings/ProfilePage.scss \
        src/static/src/paas/core/router.js \
        src/static/src/paas/root.js \
        src/static/src/paas/root.xml \
        src/__manifest__.py
git status --short  # expect only these files
git commit -m "$(cat <<'EOF'
feat(profile): ProfilePage + profileService wired to /api/me

Add #/settings/profile route with ProfilePage form (name/email/phone)
and a reactive profileService shared with the Header (Task 3). The page
validates client-side (name non-empty, email regex) and surfaces both
per-field and server errors. Cancel reverts form; Save posts to
/api/me/update and shows a 3s success banner.
EOF
)"
```

---

## Task 3: Frontend — Header rewire + SettingsPage "Profile" card

**Files:**
- Modify: `src/static/src/paas/layout/header/Header.js`
- Modify: `src/static/src/paas/layout/header/Header.xml`
- Modify: `src/static/src/paas/layout/header/Header.scss` (if exists — remove `.o_woow_user_plan` rule)
- Modify: `src/static/src/paas/pages/settings/SettingsPage.js` (add handler)
- Modify: `src/static/src/paas/pages/settings/SettingsPage.xml` (new card)

### Step 3.1: Wire Header to profileService

Edit `src/static/src/paas/layout/header/Header.js`:

```javascript
/** @odoo-module **/
import { Component, onMounted, useState } from "@odoo/owl";
import { billingService } from "../../services/billing_service";
import { profileService } from "../../services/profile_service";

const WORKSPACE_ROUTES = [
    "workspace-detail", "workspace-team", "marketplace", "configure", "service-detail",
];
const AI_ROUTES = [
    "ai-assistant", "ai-projects", "ai-tasks", "ai-task-detail", "ai-chat", "ai-project-kanban",
];

export class Header extends Component {
    static template = "woow_paas_platform.Header";
    static props = { router: Object };

    setup() {
        this.billingService = billingService;
        this.profileService = useState(profileService);
        onMounted(() => {
            billingService.fetchWallet();
            profileService.fetch();
        });
    }

    get walletBalance() { return billingService.wallet.balance; }

    get formattedBalance() {
        return `$${this.walletBalance.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    }

    get partnerName() {
        return this.profileService.partner?.name || "";
    }

    /** Up to 2 uppercase initials from the partner name. */
    get initials() {
        const name = this.partnerName.trim();
        if (!name) return "";
        const parts = name.split(/\s+/).filter(Boolean);
        const letters = parts.slice(0, 2).map(p => p[0].toUpperCase());
        return letters.join("");
    }

    get avatarUrl() {
        return this.profileService.partner?.avatar_url || "";
    }

    get breadcrumbItems() {
        const route = this.props.router.current;
        const items = [{ label: "Home", path: "dashboard" }];
        if (WORKSPACE_ROUTES.includes(route)) {
            items.push({ label: "Workspaces", path: "workspaces" });
        } else if (AI_ROUTES.includes(route)) {
            items.push({ label: "AI Assistant", path: "ai-assistant" });
        }
        const currentRoute = this.props.router.routes.find(r => r.routeName === route);
        const currentLabel = currentRoute ? currentRoute.name : "Dashboard";
        items.push({ label: currentLabel, path: null });
        return items;
    }

    onBreadcrumbClick(path) {
        if (path) this.props.router.navigate(path);
    }
}
```

### Step 3.2: Update Header.xml — drop "Sarah Wilson" and "PRO PLAN"

Edit `src/static/src/paas/layout/header/Header.xml`. Locate the user-info block (around lines 44-50, containing `Sarah Wilson` and `PRO PLAN` and the `SW` avatar). Replace the hardcoded values:

Before:
```xml
<span class="o_woow_user_name">Sarah Wilson</span>
<span class="o_woow_user_plan">PRO PLAN</span>
...
<div class="o_woow_user_avatar">SW</div>
```

After:
```xml
<span class="o_woow_user_name" t-esc="partnerName"/>
<!-- PRO PLAN span removed (was hardcoded placeholder) -->
...
<div class="o_woow_user_avatar">
    <img t-if="avatarUrl" t-att-src="avatarUrl" alt=""/>
    <t t-else="" t-esc="initials"/>
</div>
```

Read the current XML first to find the exact lines; this diff is a SKETCH — preserve surrounding wrapper elements.

### Step 3.3: Clean Header.scss (if needed)

If `Header.scss` has a `.o_woow_user_plan` rule, remove it. If no such rule exists, skip this sub-step.

Also ensure `.o_woow_user_avatar img` renders well inside the circle:

```scss
.o_woow_user_avatar {
    // ... existing
    img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        border-radius: inherit;
    }
}
```

Only add if not already present.

### Step 3.4: Add Profile card to SettingsPage

Edit `src/static/src/paas/pages/settings/SettingsPage.js`. Add a new handler:

```javascript
    goToProfile() {
        this.router.navigate("settings/profile");
    }
```

Edit `src/static/src/paas/pages/settings/SettingsPage.xml`. Add a new card **before** the Payment Methods card:

```xml
                <WoowCard shadow="true" class="'o_woow_settings_card'">
                    <div class="o_woow_settings_card_body"
                         t-on-click="goToProfile">
                        <div class="o_woow_settings_card_icon">
                            <WoowIcon name="'person'" size="32"/>
                        </div>
                        <div class="o_woow_settings_card_text">
                            <div class="o_woow_settings_card_title_row">
                                <h3>Profile</h3>
                            </div>
                            <p>Edit your name, email, and phone number.</p>
                        </div>
                        <div class="o_woow_settings_card_arrow">
                            <WoowIcon name="'chevron_right'" size="24"/>
                        </div>
                    </div>
                </WoowCard>

                <!-- existing Payment Methods card follows -->
```

### Step 3.5: Commit

```bash
git add src/static/src/paas/layout/header/Header.js \
        src/static/src/paas/layout/header/Header.xml \
        src/static/src/paas/layout/header/Header.scss \
        src/static/src/paas/pages/settings/SettingsPage.js \
        src/static/src/paas/pages/settings/SettingsPage.xml
git status --short  # expect only these files (Header.scss optional)
git commit -m "$(cat <<'EOF'
feat(profile): Header reads real partner; Profile card in Settings

Header now consumes profileService and shows partner.name + initials +
avatar image (when present). Dropped the hardcoded 'Sarah Wilson' /
'PRO PLAN' / 'SW' placeholders. SettingsPage gains a Profile card above
Payment Methods linking to #/settings/profile.
EOF
)"
```

---

## Task 4: Deploy + staging E2E

Follows the same pattern as Phase 2c #1.

### Step 4.1: Bump version and tag

Edit `src/__manifest__.py`, bump to next patch (e.g. `18.0.1.0.9`):

```bash
grep '"version"' src/__manifest__.py
# Edit: bump patch
git add src/__manifest__.py
git commit -m "release: 18.0.1.0.9"
git push origin develop
git tag v18.0.1.0.9
git push origin v18.0.1.0.9
```

### Step 4.2: Deploy staging via skill

Invoke: `/woow:deploy-stg`

Wait for CI build + sandbox upgrade. The new `paas-odoo-stg:YYYYMMDD.HHMM` should end up live on the `dev-sandbox-stg` namespace within ~2 min (CI is usually fast).

### Step 4.3: Browser E2E checklist

Use Playwright MCP (pattern from Phase 2c #1) to walk through:

1. Login at `https://stg.woowtech.io/web/login` (admin/admin)
2. Navigate to `https://stg.woowtech.io/woow#/dashboard`
   - **Expected**: Header right side shows `Administrator` (not `Sarah Wilson`); no `PRO PLAN` text visible; avatar is either the image or `A` initial
3. Click Settings
   - **Expected**: Profile card appears **above** Payment Methods, with `person` icon
4. Click Profile
   - **Expected**: `#/settings/profile` loads; form is pre-filled with current name/email/phone
5. Change `name` to `Administrator Edited`, click Save
   - **Expected**: Green "Profile saved" banner; Header updates immediately to `Administrator Edited`
6. Change `email` to `not-an-email`, click Save
   - **Expected**: Red inline "Invalid email format" under email field; no network request (or server-side rejection with same code)
7. Empty the name, click Save
   - **Expected**: Red inline "Name is required"
8. Revert name to original (for hygiene), Save
9. DevTools Network: POST /api/me/update → response `{success: true, data: {...}}`

### Step 4.4: Update test-results doc

Append a row to `docs/plans/2026-04-19-e2e-test-results.md` Phase 2c Follow-ups table marking #6 as FIXED, following the same pattern as row #1. Include:
- Release version (`18.0.1.0.9`)
- Image tag deployed
- Commits (3 feature + 1 release)
- E2E observations confirming each Task 4.3 step

Commit: `docs: mark Phase 2c #6 (profile page) verified on staging`

---

## Definition of Done

- [x] Design doc committed (`3b1409a`)
- [ ] Task 1: Backend + 7 HttpCase tests green
- [ ] Task 2: ProfilePage loads, form validates and saves
- [ ] Task 3: Header shows real name, no PRO PLAN, Profile card in Settings
- [ ] Task 4.1: Version bumped, tagged, pushed
- [ ] Task 4.2: Staging deployed
- [ ] Task 4.3: All 9 E2E checks pass
- [ ] Task 4.4: e2e-test-results.md updated

## Out of scope (do not drift)

- Avatar upload
- Address fields
- Password change
- Email verification flow
- Language / timezone
- res.users fields
- Concurrent-edit conflict UI
- Other Phase 2c items (#5 wallet auto-deduct)
