---
created: 2026-01-13T17:24:23Z
last_updated: 2026-02-01T13:22:40Z
version: 1.2
author: Claude Code PM System
---

# Project Structure

## Directory Layout

```
woow_paas_platform/
├── src/                         # Odoo module source code
│   ├── __init__.py              # Package initializer - imports models, controllers
│   ├── __manifest__.py          # Odoo module manifest (metadata, deps, assets)
│   │
│   ├── controllers/             # HTTP routes (JSON API)
│   │   ├── __init__.py
│   │   └── paas.py              # /woow endpoint + workspace API
│   │
│   ├── models/                  # Business logic layer
│   │   ├── __init__.py          # Model imports
│   │   ├── res_config_settings.py  # System settings extension
│   │   ├── workspace.py         # Workspace model ✅ NEW
│   │   └── workspace_access.py  # Workspace access/member model ✅ NEW
│   │
│   ├── views/                   # UI definitions (XML)
│   │   ├── menu.xml             # Menu structure
│   │   ├── res_config_settings_views.xml  # Settings page
│   │   └── paas_app.xml         # Standalone app QWeb template
│   │
│   ├── security/                # Access control
│   │   └── ir.model.access.csv  # Model permissions
│   │
│   └── static/                  # Frontend assets
│       ├── description/
│       │   └── icon.png         # Module icon
│       └── src/
│           ├── paas/            # Standalone OWL Application
│           │   ├── app.js       # Mount entry point
│           │   ├── root.js/xml  # Root component + router
│           │   ├── core/
│           │   │   └── router.js  # Hash-based router service
│           │   ├── layout/
│           │   │   ├── app_shell/  # AppShell.js/xml
│           │   │   ├── sidebar/    # Sidebar.js/xml
│           │   │   └── header/     # Header.js/xml
│           │   ├── components/
│           │   │   ├── icon/       # WoowIcon.js/xml
│           │   │   ├── card/       # WoowCard.js/xml
│           │   │   ├── button/     # WoowButton.js/xml
│           │   │   └── modal/      # CreateWorkspaceModal, InviteMemberModal ✅ NEW
│           │   ├── pages/
│           │   │   ├── dashboard/  # DashboardPage.js/xml
│           │   │   ├── workspace/  # WorkspaceListPage, WorkspaceDetailPage, WorkspaceTeamPage ✅ UPDATED
│           │   │   └── empty/      # EmptyState.js/xml
│           │   ├── services/       # ✅ NEW
│           │   │   └── workspace_service.js  # Workspace API client
│           │   └── styles/
│           │       ├── 00_variables.scss
│           │       ├── 10_base.scss
│           │       ├── 20_layout.scss
│           │       ├── 30_components.scss
│           │       ├── 99_main.scss
│           │       └── pages/
│           │           ├── 10_empty.scss
│           │           ├── 20_workspace.scss
│           │           └── 30_dashboard.scss
│           ├── scss/            # Backend asset styles
│           │   └── main.scss
│           ├── components/      # Backend OWL components (future)
│           └── services/        # Backend JS services (future)
│
├── scripts/                     # Development scripts
│   ├── setup-worktree-env.sh
│   ├── start-dev.sh
│   ├── test-addon.sh
│   └── cleanup-worktree.sh
│
├── docs/                        # Documentation
│   └── workspace-feature-spec.md  # Workspace feature specification
│
├── CLAUDE.md                    # Claude Code development guide
│
└── .claude/                     # Claude Code PM configuration
    ├── agents/                  # Custom agent definitions
    ├── commands/                # Custom PM commands
    ├── context/                 # Project context files (this directory)
    ├── epics/                   # Epic tracking
    │   └── .archived/           # Completed epics
    ├── prds/                    # Product requirement documents
    ├── rules/                   # Development rules
    ├── scripts/                 # Utility scripts
    └── templates/               # Document templates
```

## File Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Models | `{model_name}.py` | `workspace.py` |
| Views | `{model_name}_views.xml` | `subscription_views.xml` |
| Controllers | `{feature}.py` | `paas.py` |
| Security | `ir.model.access.csv` | N/A |
| SCSS | `{nn}_{name}.scss` | `20_layout.scss` |
| OWL Components | `{ComponentName}/` | `WoowCard/` |
| OWL Services | `{name}_service.js` | `workspace_service.js` |

## Module Organization

### Python Modules (`src/models/`)
- One file per model or related model group
- All models imported via `src/models/__init__.py`
- TransientModel for settings, AbstractModel for mixins

### Controllers (`src/controllers/`)
- HTTP route handlers with JSON API
- Using `type="json"` for Odoo 18 JSON responses
- Imported via `src/controllers/__init__.py`

### XML Views (`src/views/`)
- One file per model's views
- Menu definitions in `menu.xml`
- QWeb templates for standalone apps

### Frontend (`src/static/src/`)
- `paas/` - Standalone OWL application
  - `services/` - API client services
  - `components/modal/` - Modal dialogs
- `scss/` - Backend stylesheets
- `components/` - Backend OWL components (future)
- `services/` - JS services (future)

## Key Files

| File | Purpose |
|------|---------|
| `src/__manifest__.py` | Module metadata, version, dependencies, assets |
| `src/controllers/paas.py` | `/woow` endpoint + workspace JSON API |
| `src/models/workspace.py` | Workspace model with CRUD |
| `src/models/workspace_access.py` | Workspace member access control |
| `src/views/paas_app.xml` | QWeb template for standalone app |
| `src/static/src/paas/root.js` | OWL app root + router |
| `src/static/src/paas/services/workspace_service.js` | Frontend workspace API client |
| `src/security/ir.model.access.csv` | CRUD permissions per model |

## Update History
- 2026-02-01: Updated for src/ directory structure, added models and services
- 2026-01-14: Added controllers/, paas/ frontend structure
