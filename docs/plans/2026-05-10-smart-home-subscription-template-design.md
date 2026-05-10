# Smart Home Subscription: Align with Cloud-Service Template-Bound Design

**Status:** PR #23 open against `develop` — https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/pulls/23

**Goal:** Smart Home plan discovery currently uses `Plan.search([('name','ilike','Smart Home%')])` — a string-prefix scheme that bypasses the structural M2M relation `cloud_app_template` uses, and is also blocked for portal users by `sh.subscription.plan` ACL. The visible failure on staging: clicking *Add Smart Home* opens the modal but it shows "No plans available" because the API returns an `AccessError`. This PR replaces the name-search path with a `smart_home_template` model that mirrors `cloud_app_template`'s structure, fixes the ACL bug as a side effect (template reads go through `.sudo()`), and brings Smart Home's payment surface (skip-trial, change-plan) up to feature parity with cloud services.

**Architecture:** A new `woow_paas_platform.smart_home_template` model owns the `subscription_plan_ids` M2M. Both template models gain a symmetric `@api.constrains` that rejects sharing a plan across service types. Three controller endpoints (`/api/smart-home/templates`, `/api/smart-home/templates/<id>/plans`, plus a `skip-trial-estimate` / `skip-trial` pair under `/api/workspaces/<wid>/smarthomes/<id>/`) replace the single `/api/smart-home/plans` route, with `_create_smarthome` extended to require and validate `template_id`. `SkipTrialModal` is refactored to take `loadEstimate` / `confirmSkip` callbacks so it stays service-type-agnostic; `ServiceDetailPage` and the new SH wiring on `SmartHomeDetailPage` both pass thunks bound to their respective service modules. UI changes are scoped to the SH detail page (Subscription card + Skip Trial action when in trial) and a template dropdown on the create modal that only renders for multi-template setups.

**Tech Stack:** Odoo 18 (Python 3.11), OWL.js. Backend tests run via `kubectl exec` inside `dev-sandbox-smart-home-sub`; sandbox playwright drives the manual E2E.

**Branch:** `feature/smart-home-subscription-plan` (worktree at `.maestri/floors/woow_paas_platform--featuresmart-home-subscription-plan`).

**Reference design:** Cloud-service template flow at `controllers/paas.py:830-851` (`api_template_plans`) and `controllers/paas.py:1560` (`_create_service` with template-bound plan validation).

---

## Brainstorm decisions (the 11 open questions answered)

These decisions came out of a `/sc:brainstorm` session before any code changed; recorded here so reviewers can see *why* the design is what it is, not just *what* it is.

| # | Question | Decision |
|---|---|---|
| Q1 | Orphan SH instances (`subscription_id IS NULL`) — what to do? | Delete legacy E2E Test Home; orphan branch in `subscription_lifecycle_mixin._is_subscription_active()` (returns `True` for `not sub`) keeps any future legacy data unrestricted, but the orphan UI branch is not exercised because we cleaned up the only existing instance. |
| Q2 | What if no template plan is linked when the modal loads? | **Hard fail.** No fallback to name-search. Surfacing the misconfiguration with a clear error is the design intent — silent fallback would hide admin mistakes and defeat the structural alignment. |
| Q3 | `noupdate` for the SH template data XML? | `noupdate="0"` — every `-u` resets `subscription_plan_ids` to the canonical set. Same convention as `subscription_template_links.xml`. Documented in the "Migration notes" section so admins know UI-added plans don't survive module update. |
| Q4 | Strategy for `SkipTrialModal` refactor? | **X1 strict.** Callback props are required, no backward-compat dual-mode. ServiceDetailPage updated in the same PR so cloud-service Skip Trial keeps working. No OWL unit tests written (codebase has no pattern); manual sandbox playwright covers regression. |
| Q5 | Cross-service-type plan sharing? | **Forbid via `@api.constrains` on both template models.** A plan binds to a single product whose name appears on the invoice line; sharing across service types would mismatch billing with the actual service. Constraint is symmetric so the conflict is caught regardless of which side is edited last. |
| Q6 | How to handle multiple active SH templates in the modal? | **Add a template dropdown.** The user originally wanted "no UI changes" but accepted this small addition because the alternative (server-side merge of all templates' plans) breaks the per-template plan boundary. Dropdown only renders when `templates.length > 1` — single-template setups don't see useless chrome. |
| Q7 | Existing orphan SH and the new validation? | Tied to Q1 — delete the legacy record. New `change-plan` flow already requires an active subscription, so orphans naturally can't reach the validation. |
| Q8 | Should we audit the rest of `controllers/billing.py` for similar ACL bugs? | Yes. The audit found one missing `.sudo()` on the SH `change-plan` endpoint (`controllers/smart_home.py` line 127); fixed in this PR. `billing.py`, `paas.py` and `dashboard.py` all use sudo consistently on `sh.subscription.*` reads. |
| Q9 | ECPay credentials in the seeded sandbox DB? | Sandbox DB is restored from `dev-sandbox-stg`, whose `payment_provider` row is `state='test'`. Test merchant credentials only — no real-money exposure. |
| Q10 | One PR or split (ACL hotfix vs feature vs UI)? | One PR. Splitting would force three review/merge cycles for changes that are tightly coupled — the ACL fix only matters once the structural rewrite lands, and the UI relies on the new endpoints. |
| Q11 | Test coverage target? | **Happy + edge.** 13 new tests cover the model (CRUD, ACL, cross-type rejection from both directions), data preload, controller hard-fail paths and happy path, and skip-trial endpoint delegation. No frontend unit tests (no pattern in repo). |

---

## Out of scope

- Building a CI image with the branch's code baked in. Sandbox iteration uses a `sleep infinity` deploy override + manual `kubectl exec odoo …` so the in-pod overlay survives across odoo restarts; this is fine for one-off sandbox dev and avoids needing the docker daemon locally.
- Refactoring the existing `_skip_trial` / `_skip_trial_estimate` methods on `subscription_lifecycle_mixin` (covered in `2026-04-28-sprint2-g2a-skip-trial-estimate.md`). They already work for any model that inherits the mixin; this PR just exposes them through SH controller routes.
- Frontend testing framework — none wired in this repo; backend tests + sandbox playwright cover the contract.
- Migrating `cloud_app_template` plan links to a `service_type` selection field. The two template models stay separate, since the helm-specific fields on `cloud_app_template` (helm_repo_url, mcp_*, etc.) don't apply to Smart Home.

---

## Files & responsibilities

### Backend (commit 1: `feat(smart-home): introduce SmartHomeTemplate with cross-type plan guard`)

| File | Change |
|---|---|
| `src/models/smart_home_template.py` | New: `name` / `slug` / `icon` / `description` / `full_description` / `subscription_plan_ids` M2M / `is_active` + `_check_no_cross_service_type_plans` constraint searching `cloud_app_template`. |
| `src/models/cloud_app_template.py` | Symmetric `_check_no_cross_service_type_plans` constraint searching `smart_home_template`. Triggered when admin edits CS-side. |
| `src/models/__init__.py` | Import new module. |
| `src/security/ir.model.access.csv` | `smart_home_template` access: `base.group_user` read-only; `base.group_system` full CRUD. |
| `src/views/smart_home_template_views.xml` | New: form (Catalog group, Subscription Plans notebook page, Description page) + list + action + menuitem. |
| `src/views/menu.xml` | Add `menu_smart_home` parent under `menu_woow_paas_platform_root` (sequence 40, after `menu_cloud` at 30). |
| `src/data/smart_home_templates.xml` | New: preload `smart_home_home_assistant` record (`noupdate="0"`). |
| `src/data/subscription_template_links.xml` | Append SH template ↔ `plan_smarthome_monthly`/`yearly` binding. Drop the obsolete `<!-- Smart Home plans are used directly by SmartHome model, not via template -->` comment. |
| `src/__manifest__.py` | Register the two new XML files in load order. |

### Backend (commit 2: `feat(smart-home): align controller endpoints with cloud-service shape`)

| File | Change |
|---|---|
| `src/controllers/smart_home.py` | Replace `api_smarthome_plans` with `api_smarthome_templates` (list) + `api_smarthome_template_plans` (per template). Both use `.sudo()` because portal users lack ACL on `sh.subscription.plan`. Add `api_smarthome_skip_trial_estimate` + `api_smarthome_skip_trial` mirroring `paas.py`. Extend `_create_smarthome` to require and validate `template_id`. Fix missing `.sudo()` on the existing `change-plan` plan read. |

### Frontend (commit 3: `refactor(modal): make SkipTrialModal service-type-agnostic`)

| File | Change |
|---|---|
| `src/static/src/paas/components/modal/SkipTrialModal.js` | Drop `cloudService` import. Add required `loadEstimate` / `confirmSkip` callback props. `service` prop stays for display data only. |
| `src/static/src/paas/pages/service/ServiceDetailPage.js` | Add `loadCloudServiceEstimate` / `confirmCloudServiceSkip` arrow-function fields binding `cloudService` methods to `this.props.serviceId`. |
| `src/static/src/paas/pages/service/ServiceDetailPage.xml` | Pass the two new callbacks to `<SkipTrialModal>`. |

### Frontend (commit 4: `feat(smart-home): wire frontend to template-bound subscription flow`)

| File | Change |
|---|---|
| `src/static/src/paas/services/workspace_service.js` | Add `fetchSmartHomeTemplates`, `fetchSmartHomePlans(templateId)`, `getSkipTrialEstimateSmartHome`, `skipTrialSmartHome`. Update `createSmartHome` to carry `template_id`. |
| `src/static/src/paas/components/smart-home/CreateSmartHomeModal.js` | Two-stage load: templates → plans-for-template. State adds `templates`, `selectedTemplateId`, `loadingTemplates`, `loadingPlans`. |
| `src/static/src/paas/components/smart-home/CreateSmartHomeModal.xml` | Conditional template `<select>` (`t-if="state.templates.length > 1"`); admin-targeted "no template" / "no plans" messages. |
| `src/static/src/paas/pages/smart-home/SmartHomeDetailPage.js` | Add `SkipTrialModal` import + `formatPoints`. State adds `showSkipTrialModal`. Getters: `subscription`, `isTrialActive`, `formattedPlanPrice`. Arrow-function fields `loadSmartHomeEstimate` / `confirmSmartHomeSkip` for callback wiring. |
| `src/static/src/paas/pages/smart-home/SmartHomeDetailPage.xml` | Skip Trial button (`t-if="isTrialActive"`) in header actions. New Subscription card (Plan / Trial Ends / Next Payment). Mount `<SkipTrialModal>` with the SH callbacks. |

### Tests (commit 5: `test(smart-home): cover template, controller flow, ACL, cross-type guard`)

| File | Change |
|---|---|
| `src/tests/test_smart_home_template.py` | New: 8 tests — minimal create, with-plans create, default-data XML preload, cross-type rejection from SH side, cross-type rejection from CS side, disjoint plans don't conflict, portal-user read access, portal-user write rejection. |
| `src/tests/test_smart_home_subscription_flow.py` | New: 5 tests — `_create_smarthome` rejects missing `template_id`, rejects plan-not-in-template, rejects inactive template, happy-path produces a confirmed trial subscription; skip-trial endpoint delegation to mixin. |
| `src/tests/__init__.py` | Register the two new modules. |

### Tooling (commit 6: `chore(sandbox): add seed-from-stg helper for k8s-dev-sandbox skill`)

| File | Change |
|---|---|
| `.claude/skills/k8s-dev-sandbox/scripts/seed-from-stg.sh` | New: `pg_dump` from `dev-sandbox-stg` postgres + `kubectl cp` round-trip to the target sandbox + filestore tar-pipe between odoo pods. The filestore step is what stops asset bundles, logos and favicons from `500`'ing on `FileNotFoundError` after a fresh DB restore. |

---

## API contract

### `POST /api/smart-home/templates` (auth=user)

Lists active Smart Home templates. Response shape:

```json
{
  "jsonrpc": "2.0",
  "result": {
    "success": true,
    "data": [
      {"id": 1, "name": "Home Assistant", "slug": "home-assistant",
       "description": "Connect Home Assistant via a secure managed tunnel"}
    ]
  }
}
```

### `POST /api/smart-home/templates/<int:template_id>/plans` (auth=user)

Plans bound to a template. Hard-fails (no fallback to name-search) when the template is missing, inactive, or has no plans linked:

```json
{"success": false, "error": "Template has no subscription plans configured"}
```

Successful shape mirrors `cloud_app_template.subscription_plan_ids`:

```json
{
  "success": true,
  "data": [
    {"id": 9, "name": "Smart Home Monthly", "price": 100.0,
     "duration": 1, "unit": "month",
     "has_trial": true, "trial_duration": 7, "trial_unit": "day"},
    {"id": 10, "name": "Smart Home Yearly", "price": 1000.0, ...}
  ]
}
```

### `POST /api/workspaces/<int:workspace_id>/smarthomes` (action=create)

Now requires `template_id` in addition to `plan_id`. `plan_id` must be in `template.subscription_plan_ids` or the call is rejected with `"Selected plan is not available for this template"`.

### `POST /api/workspaces/<int:workspace_id>/smarthomes/<int:smarthome_id>/skip-trial-estimate` and `/skip-trial`

Mirrors `controllers/paas.py:932` and `:960` exactly. Both delegate to the lifecycle mixin without adding any SH-specific math.

---

## Test plan

- [x] **Unit tests** (`kubectl exec` in sandbox): `0 failed, 0 errors of 13 tests` on the new SH suite. `test-tags '/woow_paas_platform:TestSmartHomeTemplate,/woow_paas_platform:TestSmartHomeSubscriptionFlow,/woow_paas_platform:TestSmartHomeSkipTrialEndpoints'`.
- [x] **Sandbox playwright E2E** (port-forward, no Cloudflare DNS):
  - Add Smart Home → modal renders with plan list (Monthly 100 pts / Yearly 1000 pts, both with 7-day trial).
  - Submit "Test SH E2E" → SH created; workspace detail shows Trial badge.
  - SH detail page: Subscription card visible (Plan = Smart Home Monthly @ 100.00 pts, Next Payment 2026-05-17), Skip Trial button visible.
  - Skip Trial → modal shows Subtotal 100 + Tax 15% = 115 pts; wallet has 50.10 pts; insufficient-balance warning + Top-up link rendered; Confirm button disabled.
  - Console: 0 errors, 0 warnings throughout.
- [ ] **Stg E2E (post-merge):** `/woow:deploy-stg` from `develop`, then run the same flow on `stg.woowtech.io` to confirm the addon bake into `paas-odoo-stg` carries cleanly.

---

## Migration notes

- `subscription_template_links.xml` is `noupdate=0`; on `-u` the SH template's `subscription_plan_ids` resets to `[plan_smarthome_monthly, plan_smarthome_yearly]`. Admins who add custom plans through the form view keep them only if they also add them through the data XML. Same convention as cloud-service template links.
- Legacy SH instances with `subscription_id IS NULL` (the "E2E Test Home" record on `dev-sandbox-stg` was the only one observed) hit the orphan branch in `_is_subscription_active()` and so are unaffected by the new `_create_smarthome` validation — they simply can't `change-plan` until they get a subscription. Recommendation: clean these up before this lands so the validation paths apply consistently.
- The cross-service-type constraint fires during `data` XML loading too. If admin had manually linked the SH plans to a cloud_app_template before this update, `-u` will fail. Audit the `woow_template_plan_rel` table before deploy and unlink any SH plans from cloud templates. (Verified clean on `dev-sandbox-stg`: `cs_template_count = 0` for both `plan_smarthome_*` rows.)

---

## Sandbox iteration workflow

The conventional `k8s-dev-sandbox` `local` mode requires hostPath access from cluster nodes; on `woow-k3s` (remote cluster) hostPath isn't reachable from your laptop. `ci-local` and `stg` modes both bake addon code into the image, so iteration normally needs a docker rebuild. Building locally was off the table for this PR (the requester explicitly didn't want a docker daemon running).

The workaround used: deploy the sandbox in `stg` mode pointing at the latest published `paas-odoo-stg` image, then patch the deployment's command to `sleep infinity` so the pod survives without odoo, `kubectl cp src/. <pod>:/opt/bitnami/odoo/custom-addons/woow_paas_platform/` to overlay branch code, and start odoo manually with `kubectl exec … nohup odoo -c /etc/odoo/odoo.conf --dev=reload,qweb,xml &`. To pick up new `@route` decorators, kill the in-pod odoo PID and re-exec; pod restart is avoided so the overlay isn't lost. This is documented here for the next person to iterate on a remote-cluster sandbox without local docker — the `seed-from-stg.sh` helper handles the DB + filestore portion of the same setup.
