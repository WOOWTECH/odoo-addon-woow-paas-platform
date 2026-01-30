---
created: 2026-01-13T17:24:23Z
last_updated: 2026-01-14T07:02:23Z
version: 1.1
author: Claude Code PM System
---

# Technical Context

## Technology Stack

### Core Framework
- **Odoo 18.0** - Business application framework
- **Python** - Backend language (Odoo uses Python 3.10+)
- **PostgreSQL** - Database (required by Odoo)

### Frontend
- **OWL Framework** - Odoo's reactive component framework
- **SCSS** - Stylesheet preprocessor
- **XML** - View and template definitions
- **Google Fonts** - Manrope, Outfit typography
- **Material Symbols** - Icon font

### Development Tools
- **Claude Code PM** - Project management system
- **Git** - Version control
- **Docker** - Development environment

## Dependencies

### Odoo Module Dependencies
```python
'depends': ['base', 'web']
```

| Module | Purpose |
|--------|---------|
| `base` | Core Odoo functionality, models, access control |
| `web` | Web client, assets, OWL framework |

### External Dependencies (CDN)
| Resource | Purpose |
|----------|---------|
| Google Fonts | Manrope (body), Outfit (headings) |
| Material Symbols | Outlined icons |

## Module Metadata

```python
{
    'name': 'Woow PaaS Platform',
    'version': '18.0.1.0.0',
    'category': 'WOOW',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'auto_install': False,
}
```

### Version Format
`{odoo_version}.{major}.{minor}.{patch}` = `18.0.1.0.0`

## Asset Bundles

### `web.assets_backend`
Backend assets for Odoo web client:
```python
'web.assets_backend': [
    'woow_paas_platform/static/src/scss/main.scss',
]
```

### `woow_paas_platform.assets_paas` ✅ NEW
Standalone OWL application assets:
```python
'woow_paas_platform.assets_paas': [
    # OWL Core
    ('include', 'web._assets_helpers'),
    ('include', 'web._assets_backend_helpers'),
    'web/static/src/scss/pre_variables.scss',
    'web/static/lib/bootstrap/scss/_variables.scss',
    ('include', 'web._assets_bootstrap_backend'),
    ('include', 'web.assets_backend'),

    # Application
    'woow_paas_platform/static/src/paas/**/*',
]
```

## Development Environment

### Docker Commands (Recommended)
```bash
# Restart after changes
docker compose -f docker-compose-18.yml restart web

# Update module
docker compose -f docker-compose-18.yml exec web odoo -d odoo -u woow_paas_platform --dev xml

# View logs
docker compose -f docker-compose-18.yml logs -f web
```

### Direct Commands
```bash
# Run Odoo with module
./odoo-bin -c odoo.conf -u woow_paas_platform

# Update module
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init

# Run tests
./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init
```

## Repository Information

- **Origin:** https://github.com/WOOWTECH/odoo-addons.git
- **Branch:** main
- **Location:** `data/18/addons/woow_paas_platform/`

## Integration Points

### Odoo Core Integration
- `res.config.settings` - System settings extension
- Menu system via `ir.ui.menu`
- Access control via `ir.model.access`
- HTTP Controller for `/woow` endpoint

### Future Integrations (Planned)
- External API services
- Payment gateways
- Customer management systems

## OWL Components

### Layout Components
| Component | Location | Purpose |
|-----------|----------|---------|
| AppShell | `layout/app_shell/` | Main shell structure |
| Sidebar | `layout/sidebar/` | Navigation menu |
| Header | `layout/header/` | Top bar with user info |

### Base Components
| Component | Location | Purpose |
|-----------|----------|---------|
| WoowIcon | `components/icon/` | Material symbol wrapper |
| WoowCard | `components/card/` | Card container |
| WoowButton | `components/button/` | Styled button |

### Page Components
| Component | Location | Purpose |
|-----------|----------|---------|
| DashboardPage | `pages/dashboard/` | Main dashboard |
| WorkspaceListPage | `pages/workspace/` | Workspace listing |
| EmptyState | `pages/empty/` | Placeholder page |

## Update History
- 2026-01-14: Added asset bundles, OWL components, Docker commands
