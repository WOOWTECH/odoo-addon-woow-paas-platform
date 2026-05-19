# Addon Deselection Safe Removal

## Problem

When a user deselects an addon from a Cloud Odoo service and triggers helm upgrade:

1. **Bug 1 (main)**: `ir.module.module.update_list()` only adds/updates modules but never uninstalls removed ones. DB artifacts (menus, views, actions) persist even after addon files are removed from the filesystem.
2. **Bug 2**: When ALL addons are deselected, `paas.py:1542` condition `addon_changed and service.odoo_addon_ids` evaluates to False, leaving old initContainer values in the helm release.

## Design: Pre-check + Conditional Flow

Query the target Odoo instance for module states before proceeding, then branch based on results.

### Data Flow

```
User clicks "Save" (with addons removed)
    │
    ▼
Frontend computes removedAddonIds = oldIds - newIds
    │
    ├─ No removals → updateServiceAddons() (existing flow)
    │
    └─ Has removals → POST check-addon-removal
         │
         ▼
    Backend → paas-operator → kubectl exec → query ir.module.module
         │
         ▼
    Response: { addon_id: { state, dependents[] } }
         │
    ┌────┴──────────┬───────────────────┐
    all uninstalled  has installed       has dependency conflict
    │                │                   │
    updateService    ConfirmDialog:      BlockedDialog:
    Addons()         [Open Odoo Apps]    "Module X is required
                     [Force uninstall]   by module Y, cannot
                     [Cancel]            remove"
                          │              [OK] → back to edit
                     Force uninstall:
                     POST uninstall-addons
                          │
                     Success → updateServiceAddons() (helm upgrade)
```

### Execution Order

When force-uninstalling an installed addon:

1. **First**: Uninstall module in Odoo via `button_immediate_uninstall()` (files still present)
2. **Then**: Helm upgrade to remove files from selected-addons

This order ensures Odoo can execute cleanup hooks (remove views, menus, data) while the module files are still on the filesystem.

## Changes by Layer

### 1. PaaS Operator — New Endpoints

**File**: `extra/paas-operator/src/api/init.py` (or new `odoo_module.py`)
**Service**: `extra/paas-operator/src/services/odoo_module.py` (new)

#### 1.1 `POST /api/odoo/{namespace}/{release_name}/module-states`

Query module installation states and reverse dependencies.

```python
# Request
{ "module_names": ["sh_subscription", "sh_crm"] }

# Response
{
  "modules": {
    "sh_subscription": {
      "state": "installed",
      "dependents": ["sh_billing"]
    },
    "sh_crm": {
      "state": "uninstalled",
      "dependents": []
    }
  }
}
```

Implementation: `kubectl exec` Python script using ORM to query `ir.module.module` state and `ir.module.module.dependency` for reverse deps.

#### 1.2 `POST /api/odoo/{namespace}/{release_name}/uninstall-modules`

Uninstall modules in the target Odoo instance.

```python
# Request
{ "module_names": ["sh_subscription"] }

# Response (success)
{ "success": true, "uninstalled": ["sh_subscription"] }

# Response (dependency conflict)
{ "success": false, "error": "Cannot uninstall sh_subscription: module sh_billing depends on it" }
```

Implementation: `kubectl exec` to call `module.button_immediate_uninstall()`, catching `UserError` for dependency conflicts.

### 2. PaaS Platform Backend — Operator Client

**File**: `src/services/paas_operator.py`

New methods:

```python
def get_odoo_module_states(self, namespace, release_name, module_names):
    return self._request('POST', f'/api/odoo/{namespace}/{release_name}/module-states',
                         data={'module_names': module_names})

def uninstall_odoo_modules(self, namespace, release_name, module_names):
    return self._request('POST', f'/api/odoo/{namespace}/{release_name}/uninstall-modules',
                         data={'module_names': module_names})
```

### 3. PaaS Platform Backend — Controller API

**File**: `src/controllers/paas.py`

#### 3.1 `POST /api/cloud-services/services/<id>/check-addon-removal`

```python
# Request
{ "removed_addon_ids": [5, 8] }

# Response
{
  "success": true,
  "modules": {
    "sh_subscription": { "addon_id": 5, "state": "installed", "dependents": ["sh_billing"] },
    "sh_crm": { "addon_id": 8, "state": "uninstalled", "dependents": [] }
  }
}
```

Converts `removed_addon_ids` to `technical_name` list, calls `get_odoo_module_states()`, maps results back with addon IDs.

#### 3.2 `POST /api/cloud-services/services/<id>/uninstall-addons`

```python
# Request
{ "addon_technical_names": ["sh_subscription"] }

# Response
{ "success": true, "uninstalled": ["sh_subscription"] }
```

Calls `uninstall_odoo_modules()`, returns result.

### 4. Bug 2 Fix — All Addons Deselected

**File**: `src/controllers/paas.py` (line 1542) + `src/models/cloud_service.py`

Change condition from:
```python
if addon_changed and service.odoo_addon_ids:
```
To:
```python
if addon_changed:
```

New method `_remove_addon_helm_values()` on CloudService model for precise cleanup:

```python
def _remove_addon_helm_values(self, values):
    """Remove only addon-selector related entries from helm values."""
    if 'initContainers' in values:
        values['initContainers'] = [
            c for c in values['initContainers']
            if c.get('name') != 'addon-selector'
        ]
    if 'extraVolumes' in values:
        values['extraVolumes'] = [
            v for v in values['extraVolumes']
            if v.get('name') != 'selected-addons'
        ]
    if 'extraVolumeMounts' in values:
        values['extraVolumeMounts'] = [
            m for m in values['extraVolumeMounts']
            if m.get('name') != 'selected-addons'
        ]
    if 'customPostInitScripts' in values:
        values['customPostInitScripts'].pop('00-addons-path.sh', None)
```

Updated flow:
```python
if addon_changed:
    if service.odoo_addon_ids:
        addon_values = service._build_odoo_addon_helm_values(image_ref)
        merged_values = self._deep_merge(merged_values, addon_values)
    else:
        service._remove_addon_helm_values(merged_values)
```

### 5. PaaS Platform Frontend — AddonsTab

**File**: `src/static/src/paas/pages/service/tabs/AddonsTab.js`

Rewrite `saveAddons()`:

```
saveAddons()
  ├─ Compute removedAddonIds = oldIds - newIds
  ├─ No removals → updateServiceAddons() (existing)
  └─ Has removals → checkAddonRemoval(serviceId, removedAddonIds)
       ├─ All uninstalled → updateServiceAddons()
       ├─ Has dependency conflict → show BlockedDialog → back to edit
       └─ Has installed (no conflict) → show ConfirmDialog
            [Open Odoo Apps]  → window.open(odooAppsUrl) → back to edit
            [Force uninstall] → uninstallAddons() → on success → updateServiceAddons()
            [Cancel]          → back to edit
```

### 6. PaaS Platform Frontend — AddonRemovalDialog

**File**: `src/static/src/paas/components/addon-removal-dialog/AddonRemovalDialog.js` (new)

Shared dialog component with two modes:

- `mode: 'blocked'` — Shows dependency conflict message + [OK] button
- `mode: 'confirm'` — Lists installed modules + three buttons:
  - "到 Odoo 手動卸載" → opens Odoo Apps page in new tab
  - "強制卸載並移除" → triggers uninstall API
  - "取消" → closes dialog

### 7. PaaS Platform Frontend — OdooAddonSelector Dependency Protection

**File**: `src/static/src/paas/components/odoo-addon-selector/OdooAddonSelector.js`

In `toggleAddon()`: when deselecting, check if any other selected addon has this one in its `depends_on`. If so, prevent deselection and show inline tooltip: "此 addon 被 Y 依賴，無法單獨取消".

Pure frontend check, no API call needed.

### 8. PaaS Platform Frontend — cloud_service.js

**File**: `src/static/src/paas/services/cloud_service.js`

New API methods:

```javascript
async checkAddonRemoval(serviceId, removedAddonIds) { ... }
async uninstallServiceAddons(serviceId, addonTechnicalNames) { ... }
```

## Summary Table

| # | Component | File | Type |
|---|-----------|------|------|
| 1 | OdooModuleService | `extra/paas-operator/src/services/odoo_module.py` | New |
| 2 | module-states endpoint | `extra/paas-operator/src/api/init.py` or new | New |
| 3 | uninstall-modules endpoint | `extra/paas-operator/src/api/init.py` or new | New |
| 4 | `get_odoo_module_states()` | `src/services/paas_operator.py` | New method |
| 5 | `uninstall_odoo_modules()` | `src/services/paas_operator.py` | New method |
| 6 | `check-addon-removal` route | `src/controllers/paas.py` | New route |
| 7 | `uninstall-addons` route | `src/controllers/paas.py` | New route |
| 8 | `_remove_addon_helm_values()` | `src/models/cloud_service.py` | New method |
| 9 | Bug 2 fix (line 1542) | `src/controllers/paas.py` | Fix |
| 10 | `saveAddons()` rewrite | `AddonsTab.js` | Modify |
| 11 | AddonRemovalDialog | `src/static/src/paas/components/` | New |
| 12 | Dependency deselection guard | `OdooAddonSelector.js` | Modify |
| 13 | API methods | `cloud_service.js` | New methods |
