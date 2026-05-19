# Subscription Integration — Implementation Workflow

Based on: `docs/spec/subscription-integration-design.md`

## Phase Overview

```
Phase 1: Backend Foundation        ← Models, Mixin, Settings, Migration
    │
    ▼
Phase 2: Data & Products           ← XML Products, Plans, Template Links
    │
    ├───────────────┬───────────────┐
    ▼               ▼               ▼
Phase 3:        Phase 4:        Phase 5:
Controller      Lifecycle       PaaS Operator
Integration     Cron            Scale API
    │               │               │
    ├───────────────┘               │
    ▼                               │
Phase 6: Plan Change Wizard    ◄───┘
    │
    ▼
Phase 7: Frontend (OWL)
    │
    ▼
Phase 8: Testing & Verification
```

---

## Phase 1: Backend Foundation

**Goal**: 建立所有新的 model、mixin、欄位變更、settings、migration。
**Success Criteria**: `odoo-bin -u woow_paas_platform --stop-after-init` 無錯誤。

### Step 1.1 — SubscriptionLifecycleMixin

**File**: `src/models/subscription_lifecycle_mixin.py` (NEW)

Create abstract model with:
- `subscription_id` Many2one field
- Computed convenience fields (subscription_state, plan_name, end_date, etc.)
- `_create_and_confirm_subscription(partner, plan)` method
- `_is_subscription_active()`, `_is_in_grace_period()`, `_is_past_grace_period()`
- `_check_operation_allowed(operation)` with admin bypass
- Abstract `_do_suspend()`, `_do_resume()`, `_do_cleanup()`

### Step 1.2 — CloudService Model Changes

**File**: `src/models/cloud_service.py` (MODIFY)

- Add `_inherit` list to include mixin
- Add `suspended` state to selection
- Add `pre_suspend_state` field
- Implement `_do_suspend()` — scale replicas to 0
- Implement `_do_resume()` — scale replicas back
- Implement `_do_cleanup()` — helm uninstall

### Step 1.3 — SmartHome Model Changes

**File**: `src/models/smart_home.py` (MODIFY)

- Add `_inherit` list to include mixin
- Add `suspended` state to selection
- Add `pre_suspend_state`, `original_service_url` fields
- Implement `_do_suspend()` — ingress → 503
- Implement `_do_resume()` — ingress → original service_url
- Implement `_do_cleanup()` — delete tunnel

### Step 1.4 — CloudAppTemplate Changes

**File**: `src/models/cloud_app_template.py` (MODIFY)

- Remove `monthly_price` field
- Add `subscription_product_id` Many2one to product.product

### Step 1.5 — Settings & Hooks

**Files**: `src/models/res_config_settings.py`, `src/hooks.py` (MODIFY)

- Add `grace_period_days` config_parameter (default 7)
- Add `cleanup_after_months` config_parameter (default 6)
- Update `_CONFIG_PARAMETERS` list in hooks.py
- Add settings fields to `views/res_config_settings_views.xml`

### Step 1.6 — Module Init & Manifest

**Files**: `src/models/__init__.py`, `src/__manifest__.py` (MODIFY)

- Import new modules (mixin, cron, wizard)
- Add `sh_subscription`, `sh_subscription_wallet` to depends
- Add new data files to manifest

### Step 1.7 — Migration Script

**File**: `src/migrations/18.0.1.0.7/pre-migrate.py` (NEW)

- Add `subscription_id` column to cloud_service and smart_home tables

**File**: `src/migrations/18.0.1.0.7/post-migrate.py` (NEW)

- Drop `monthly_price` column from cloud_app_template

### Checkpoint 1

```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
# Expected: No errors, module updates successfully
```

---

## Phase 2: Data & Products

**Goal**: 建立訂閱用的 Products、Plans、Reminders，並連結到 Templates。
**Success Criteria**: Odoo backend 可以看到 Products 和 Plans，Templates 有 subscription_product_id。

### Step 2.1 — Subscription Products & Plans XML

**File**: `data/subscription_products.xml` (NEW)

Create:
- 5 products (AnythingLLM, n8n, PostgreSQL, Odoo, Smart Home)
- 10 plans (monthly + yearly for each)
- 2 reminder templates (3 days, 1 day before expiry)
- Link reminders to all plans

Pricing:
| Product | Monthly | Yearly |
|---------|---------|--------|
| AnythingLLM | 299 | 2,990 |
| n8n | 199 | 1,990 |
| PostgreSQL | 99 | 990 |
| Odoo | 499 | 4,990 |
| Smart Home | 100 | 1,000 |

All plans: 7-day trial, no credit card required, never expire (-1 billing cycles).

### Step 2.2 — Link Templates to Products

**File**: `data/subscription_template_links.xml` (NEW)

Update existing `cloud_app_template` records to set `subscription_product_id`.
Requires confirming existing template XML IDs from `data/cloud_app_templates.xml`.

### Step 2.3 — Security Rules

**File**: `security/ir.model.access.csv` (MODIFY)

Add access rules for:
- `woow_paas_platform.change_plan_wizard` (admin only)
- Any new models

### Checkpoint 2

```
Odoo Backend:
  Settings → Technical → Products → verify 5 subscription products
  sh.subscription.plan → verify 10 plans
  Cloud App Templates → verify subscription_product_id linked
```

---

## Phase 3: Controller Integration

**Goal**: 部署 service / 建立 smart home 時自動建立 subscription，操作前檢查訂閱權限。
**Success Criteria**: 從 OWL 前端部署服務後，Odoo 後台可看到對應的 subscription。

### Step 3.1 — Create Service with Subscription

**File**: `src/controllers/paas.py` (MODIFY)

- `_create_service()`: Accept `plan_id` parameter
- After `CloudService.create()`, call `service._create_and_confirm_subscription()`
- Return subscription info in response

### Step 3.2 — Create Smart Home with Subscription

**File**: `src/controllers/smart_home.py` (MODIFY)

- Accept `plan_id` parameter in create action
- After `SmartHome.create()`, call `smart_home._create_and_confirm_subscription()`

### Step 3.3 — Operation Permission Checks

**Files**: `src/controllers/paas.py`, `src/controllers/smart_home.py` (MODIFY)

Add `_check_operation_allowed('write')` before:
- Service upgrade/redeploy/delete
- Smart Home provision/delete

Add `_check_operation_allowed('read')` is implicit (grace period allows read).

### Step 3.4 — API Response: Subscription Info

**File**: `src/controllers/paas.py` (MODIFY)

- Update `_format_service()` to include subscription data
- Add `subscription` object to service detail response

**File**: `src/controllers/smart_home.py` (MODIFY)

- Update `to_dict()` or add subscription data to response

### Step 3.5 — New Endpoint: Template Plans

**File**: `src/controllers/paas.py` (MODIFY)

Add route:
```
GET /api/cloud-services/templates/<int:template_id>/plans
```
Returns available plans for the template's product.

### Step 3.6 — New Endpoint: Smart Home Plans

**File**: `src/controllers/smart_home.py` (MODIFY)

Add route for fetching Smart Home plans (single product, multiple plans).

### Checkpoint 3

```
1. Deploy a service via API with plan_id
2. Check Odoo backend: sh.subscription.subscription created, state=in_progress
3. Verify subscription info in service detail API response
4. Test operation block when subscription is expired (manual state change for testing)
```

---

## Phase 4: Lifecycle Cron

**Goal**: 每日 cron 自動執行 suspend / resume / cleanup。
**Success Criteria**: 手動觸發 cron 後，expired subscription 的 service 會被 suspend。

### Step 4.1 — Lifecycle Cron Model

**File**: `src/models/subscription_lifecycle_cron.py` (NEW)

Implement `_cron_enforce_subscription_lifecycle()`:
- Follow renewal chain
- Active → resume if suspended
- Expired > grace → suspend
- Cancelled → suspend + check cleanup

### Step 4.2 — Cron XML Definition

**File**: `data/subscription_lifecycle_cron.xml` (NEW)

Daily cron registration.

### Checkpoint 4

```
1. Create a test subscription, manually set state=expire, sh_end_date=8 days ago
2. Run cron manually: model._cron_enforce_subscription_lifecycle()
3. Verify service state changed to 'suspended'
4. Change subscription state to in_progress
5. Run cron again, verify service state changed back to 'running'
```

---

## Phase 5: PaaS Operator — Scale API

**Goal**: Operator 新增 scale release endpoint。
**Success Criteria**: `POST /api/releases/{name}/scale?replicas=0` 成功 scale down。

### Step 5.1 — Scale Endpoint

**File**: `extra/paas-operator/src/api/releases.py` (MODIFY)

Add `POST /api/releases/{name}/scale` endpoint.

### Step 5.2 — HelmService.scale_release

**File**: `extra/paas-operator/src/services/helm.py` (MODIFY)

Implement `scale_release(name, namespace, replicas)` using kubectl.

### Step 5.3 — PaaSOperatorClient Update

**File**: `src/services/paas_operator.py` (or equivalent) (MODIFY)

Add `scale_release(release_name, namespace, replicas)` method to Odoo-side client.

### Step 5.4 — Verify Tunnel Ingress Update

Confirm `configure_tunnel()` in operator supports `http_status:503` as service_url.
If not, add handling in `cloudflare.py`.

### Step 5.5 — Operator Tests

**File**: `extra/paas-operator/tests/test_scale.py` (NEW)

Unit tests for scale endpoint.

### Checkpoint 5

```bash
# Port-forward operator
kubectl port-forward -n paas-system svc/paas-operator 8000:80

# Test scale down
curl -X POST "http://localhost:8000/api/releases/svc-xxx/scale?namespace=paas-ws-xxx&replicas=0" \
  -H "X-API-Key: <key>"

# Verify pods scaled to 0
kubectl get pods -n paas-ws-xxx
```

---

## Phase 6: Plan Change

**Goal**: 使用者可以從前端或後台變更訂閱方案。
**Success Criteria**: 變更方案後，舊 subscription close，新 subscription in_progress，退點正確。

### Step 6.1 — Plan Change Wizard (Backend)

**File**: `src/wizard/change_subscription_plan_wizard.py` (NEW)
**File**: `src/wizard/__init__.py` (NEW)
**File**: `views/change_plan_wizard_views.xml` (NEW)

Transient model with refund calculation and execution logic.

### Step 6.2 — Plan Change API Endpoint

**File**: `src/controllers/paas.py` (MODIFY)

Add:
- `POST /api/cloud-services/services/<id>/change-plan`
- `POST /api/workspaces/<id>/smarthomes/<id>/change-plan`

### Checkpoint 6

```
1. Deploy service with monthly plan
2. Call change-plan API with yearly plan_id
3. Verify: old subscription closed, new subscription in_progress
4. Verify: wallet refund points correct (prorated)
```

---

## Phase 7: Frontend (OWL)

**Goal**: OWL 前端顯示訂閱狀態、plan 選擇、警告橫幅。
**Success Criteria**: 使用者可以在 /woow 介面看到訂閱資訊並操作。

### Step 7.1 — Cloud Service JS: Plan & Subscription Methods

**File**: `static/src/paas/services/cloud_service.js` (MODIFY)

Add: `fetchPlans()`, `changePlan()`

### Step 7.2 — Workspace Service JS: Smart Home Plan

**File**: `static/src/paas/services/workspace_service.js` (MODIFY)

Add plan_id to `createSmartHome()`.

### Step 7.3 — Plan Selector Component

**Files**: `static/src/paas/components/PlanSelector.js`, `.xml` (NEW)

Reusable plan selection cards (monthly/yearly toggle).

### Step 7.4 — Subscription Badge Component

**Files**: `static/src/paas/components/SubscriptionBadge.js`, `.xml` (NEW)

Status badge: Trial / Active / Expired / Cancelled / Suspended.

### Step 7.5 — Deploy Modal: Add Plan Selection

**Files**: `static/src/paas/components/cloud-service/CreateServiceModal.js`, `.xml` (MODIFY)

Integrate PlanSelector into deploy flow.

### Step 7.6 — Smart Home Modal: Add Plan Selection

**Files**: `static/src/paas/components/smart-home/CreateSmartHomeModal.js`, `.xml` (MODIFY)

Integrate PlanSelector.

### Step 7.7 — Service Card: Subscription Info

**Files**: `static/src/paas/pages/workspace/WorkspaceDetailPage.js`, `.xml` (MODIFY)

Show subscription badge and next payment date on service/smart home cards.

### Step 7.8 — Service Detail: Subscription Section

**Files**: `static/src/paas/pages/cloud-service/ServiceDetailPage.js`, `.xml` (MODIFY)

Add subscription info section: plan, status, dates, change plan button.

### Step 7.9 — Smart Home Detail: Subscription Section

**Files**: `static/src/paas/pages/smart-home/SmartHomeDetailPage.js`, `.xml` (MODIFY)

Same as 7.8 for Smart Home.

### Step 7.10 — Warning & Suspended Banners

**Files**: Multiple page files (MODIFY)

- Grace period warning banner with "Renew Now" button
- Suspended state banner
- Operation buttons disabled during grace/suspended

### Step 7.11 — Subscription Styles

**File**: `static/src/paas/styles/components/_subscription.scss` (NEW)

Badge colors, banner styles.

### Checkpoint 7

```
Browser test at http://localhost/woow:
1. Deploy new service → plan selector shows monthly/yearly
2. Service card shows subscription badge + next payment
3. Service detail shows subscription section
4. Manually expire subscription → grace warning appears
5. Past grace period → suspended banner, buttons disabled
```

---

## Phase 8: Testing & Verification

### Step 8.1 — Backend Unit Tests

**File**: `src/tests/test_subscription_lifecycle.py` (NEW)

- Test mixin methods (active, grace, past grace)
- Test create subscription flow
- Test plan change (refund calculation)
- Test cron enforcement logic

### Step 8.2 — PaaS Operator Tests

**File**: `extra/paas-operator/tests/test_scale.py` (NEW)

- Test scale endpoint
- Test error handling

### Step 8.3 — Integration Test

Manual or scripted end-to-end:
1. Deploy service with plan → subscription created
2. Verify trial period active
3. Simulate trial expiry → billing starts
4. Simulate payment failure → subscription expires
5. Grace period behavior
6. Cron suspends after grace
7. Renew → cron resumes
8. Change plan → refund + new subscription
9. Cancel → 6 months → cleanup

### Step 8.4 — Version Bump

**File**: `src/__manifest__.py` (MODIFY)

Bump version to `18.0.1.0.7`.

---

## Execution Strategy

### Recommended Parallel Tracks

```
Track A (Backend):   Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 6
Track B (Operator):  Phase 5 (independent, can start after Phase 1)
Track C (Frontend):  Phase 7 (starts after Phase 3 API is stable)
Track D (Testing):   Phase 8 (starts after all other phases)
```

### Estimated Scope

| Phase | New Files | Modified Files | Complexity |
|-------|-----------|----------------|------------|
| 1 | 3 | 6 | Medium |
| 2 | 2 | 1 | Low |
| 3 | 0 | 3 | Medium-High |
| 4 | 2 | 0 | Medium |
| 5 | 1 | 3 | Medium |
| 6 | 3 | 1 | Medium |
| 7 | 5 | 8 | High |
| 8 | 2 | 1 | Medium |
| **Total** | **18** | **23** | |

### Risk Areas

| Risk | Mitigation |
|------|-----------|
| sh_subscription onchange chain 不如預期 | Phase 1 先測試 `_create_and_confirm_subscription` 方法 |
| Product/Plan XML data 格式問題 | Phase 2 先手動在 Odoo UI 建立一組驗證 |
| Operator scale 影響 PVC | Phase 5 用 dev sandbox 測試 |
| 前端 plan 選擇 UX 複雜 | Phase 7 先做最小 MVP (dropdown)，再美化 |
| 現有 service 向後相容 | Mixin 已處理 (subscription_id=null → unrestricted) |
