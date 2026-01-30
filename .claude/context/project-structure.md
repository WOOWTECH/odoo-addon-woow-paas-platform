---
created: 2026-01-13T17:24:23Z
last_updated: 2026-01-14T07:02:23Z
version: 1.1
author: Claude Code PM System
---

# Project Structure

## Directory Layout

```
woow_paas_platform/
├── __init__.py              # Package initializer - imports models, controllers
├── __manifest__.py          # Odoo module manifest (metadata, deps, assets)
├── CLAUDE.md                # Claude Code development guide
│
├── controllers/             # HTTP routes
│   ├── __init__.py
│   └── paas.py              # /woow endpoint controller
│
├── models/                  # Business logic layer
│   ├── __init__.py          # Model imports
│   └── res_config_settings.py  # System settings extension
│
├── views/                   # UI definitions (XML)
│   ├── menu.xml             # Menu structure
│   ├── res_config_settings_views.xml  # Settings page
│   └── paas_app.xml         # Standalone app QWeb template
│
├── security/                # Access control
│   └── ir.model.access.csv  # Model permissions
│
├── static/                  # Frontend assets
│   └── src/
│       ├── paas/            # Standalone OWL Application ✅ NEW
│       │   ├── app.js       # Mount entry point
│       │   ├── root.js/xml  # Root component + router
│       │   ├── core/
│       │   │   └── router.js  # Hash-based router service
│       │   ├── layout/
│       │   │   ├── app_shell/  # AppShell.js/xml
│       │   │   ├── sidebar/    # Sidebar.js/xml
│       │   │   └── header/     # Header.js/xml
│       │   ├── components/
│       │   │   ├── icon/       # WoowIcon.js/xml
│       │   │   ├── card/       # WoowCard.js/xml
│       │   │   └── button/     # WoowButton.js/xml
│       │   ├── pages/
│       │   │   ├── dashboard/  # DashboardPage.js/xml
│       │   │   ├── workspace/  # WorkspaceListPage.js/xml
│       │   │   └── empty/      # EmptyState.js/xml
│       │   └── styles/
│       │       ├── 00_variables.scss
│       │       ├── 10_base.scss
│       │       ├── 20_layout.scss
│       │       ├── 30_components.scss
│       │       ├── 99_main.scss
│       │       └── pages/
│       │           ├── 10_empty.scss
│       │           ├── 20_workspace.scss
│       │           └── 30_dashboard.scss
│       ├── scss/            # Backend asset styles
│       │   └── main.scss
│       ├── components/      # Backend OWL components (future)
│       └── services/        # Backend JS services (future)
│
├── resource/                # Static resources (HTML prototypes)
│
└── .claude/                 # Claude Code PM configuration
    ├── agents/              # Custom agent definitions
    ├── commands/            # Custom PM commands
    ├── context/             # Project context files (this directory)
    ├── epics/               # Epic tracking
    │   └── .archived/       # Completed epics
    ├── prds/                # Product requirement documents
    ├── rules/               # Development rules
    ├── scripts/             # Utility scripts
    └── templates/           # Document templates
```

## File Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Models | `{model_name}.py` | `subscription.py` |
| Views | `{model_name}_views.xml` | `subscription_views.xml` |
| Controllers | `{feature}.py` | `paas.py` |
| Security | `ir.model.access.csv` | N/A |
| SCSS | `{nn}_{name}.scss` | `20_layout.scss` |
| OWL Components | `{ComponentName}/` | `WoowCard/` |

## Module Organization

### Python Modules (`models/`)
- One file per model or related model group
- All models imported via `models/__init__.py`
- TransientModel for settings, AbstractModel for mixins

### Controllers (`controllers/`)
- HTTP route handlers
- Imported via `controllers/__init__.py`

### XML Views (`views/`)
- One file per model's views
- Menu definitions in `menu.xml`
- QWeb templates for standalone apps

### Frontend (`static/src/`)
- `paas/` - Standalone OWL application
- `scss/` - Backend stylesheets
- `components/` - Backend OWL components (future)
- `services/` - JS services (future)

## Key Files

| File | Purpose |
|------|---------|
| `__manifest__.py` | Module metadata, version, dependencies, assets |
| `controllers/paas.py` | `/woow` endpoint handler |
| `views/paas_app.xml` | QWeb template for standalone app |
| `static/src/paas/root.js` | OWL app root + router |
| `security/ir.model.access.csv` | CRUD permissions per model |

## Update History
- 2026-01-14: Added controllers/, paas/ frontend structure
