# Code Review Fixes Design

## Context

Code review of commits `8681757..3c8214b` (80+ commits) identified 21 issues across 5 areas: backend Python, error handling, frontend OWL, PaaS Operator, and infrastructure. All findings verified against source code.

## Approach

8 atomic commits directly on `develop`, grouped by subsystem. Each commit is independently revertable.

## Commits

### 1. fix: operator client params/json TypeError (C1, C2)
- `src/services/paas_operator.py:397` — `params=` → `data=`
- `src/services/paas_operator.py:690` — `json=` → `data=`

### 2. fix: suspend/resume should set error state on operator failure (C3)
- `src/models/cloud_service.py` `_do_suspend`/`_do_resume` — move state write into try success path, set error on failure
- `src/models/smart_home.py` `_do_suspend`/`_do_resume` — same pattern

### 3. fix: add workspace access check to check-addon-removal (C4)
- `src/controllers/paas.py` `api_check_addon_removal` — add `workspace.check_user_access()` after service lookup

### 4. fix: use savepoint in subscription lifecycle cron (C7)
- `src/models/subscription_lifecycle_cron.py:34-43` — replace `cr.commit()`/`cr.rollback()` with `cr.savepoint()` context manager

### 5. fix: controller error handling and null checks (I1-I4)
- `src/hooks.py` — add `storage_class_name`, `registry_secret_name` to `_CONFIG_PARAMETERS`
- `src/controllers/smart_home.py:250` — replace `str(e)` with generic error message
- `src/controllers/smart_home.py:131` + `src/controllers/paas.py:879` — add date null checks
- `src/models/cloud_odoo_addon.py:158-160` — add `source_type='third_party'` filter

### 6. fix: frontend loading states and button guard (I5, I6, S4-S6)
- `OdooAddonSelector.js` — add try/finally in `loadAddons`
- `AppConfigurationPage.xml` — add `disabled="state.launching"` to launch button
- `cloud_service.js` — improve error handling in `getConfig`/`fetchPlans`, remove orphaned JSDoc

### 7. fix: operator input validation and sync endpoints (C5, I7, I8)
- `extra/paas-operator/src/api/*.py` — change `async def` → `def` for subprocess-calling endpoints
- `releases.py` — add `Query(ge=0, le=10)` for replicas
- `odoo_module.py` — add module name regex validation

### 8. fix: infra - build script and helm chart improvements (C6, S1-S3)
- `build-operator.sh` — remove Chart.yaml dependency, use timestamp versioning
- `odoo-configmap.yaml` — add nonzero STG resource limits
- `create.sh` — use temp file for registry credentials
- `odoo-filestore-pvc.yaml` — add `helm.sh/resource-policy: keep`
