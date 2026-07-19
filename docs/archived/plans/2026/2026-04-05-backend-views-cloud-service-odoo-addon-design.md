---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-05-backend-views-cloud-service-odoo-addon-design.md
archived: 2026-07-19
archived-reason: "已落地；規格現行權威=docs/reference/features/cloud-services.md"
---

# Backend Views: Cloud Service & Odoo Addon

**Date**: 2026-04-05
**Status**: Approved

## Goal

Add backend list/form views and menu items for Cloud Service and Cloud Odoo Addon models, organized under a "Cloud" submenu group in the Woow PaaS root menu.

## Menu Structure

```
Woow PaaS (root)
├── Cloud                          ← New submenu group
│   ├── Templates                  ← Existing Cloud App Templates (moved here)
│   ├── Services                   ← New Cloud Service list/form
│   └── Odoo Addons                ← New Cloud Odoo Addon list/form
├── MCP Servers
├── OAuth Clients
└── ...
```

"Odoo Addons" (not just "Addons") to distinguish from potential future cloud-type addons.

## Cloud Service Views

### List View

Columns: Name, Workspace, Template, State, Subdomain, Helm Namespace, Deployed At

### Form View

- Basic Info: name, workspace, template, state, reference_id, subdomain, custom_domain
- Helm Config (notebook tab): release_name, namespace, revision, chart_version, values
- Resources (notebook tab): vcpu, ram, storage
- Odoo Addons (notebook tab): odoo_addon_ids
- Errors (notebook tab): error_message, init_error, init_retries

## Cloud Odoo Addon Views

### List View

Columns: Name, Technical Name, Category, Source Type, Is Default, Active

### Form View

- Basic Info: name, technical_name, description, category, icon
- Config: source_type, is_default, active
- Relations: depends_on, template_ids

## Files to Modify

| File | Action |
|------|--------|
| `src/views/cloud_service_views.xml` | Create (list + form + action + menu) |
| `src/views/cloud_odoo_addon_views.xml` | Create (list + form + action + menu) |
| `src/views/menu.xml` | Add Cloud submenu group |
| `src/views/cloud_app_template_views.xml` | Move menu to Cloud submenu |
| `src/__manifest__.py` | Add new view files to data list |

## Security

No changes needed. `ir.model.access.csv` already has entries for both models.
