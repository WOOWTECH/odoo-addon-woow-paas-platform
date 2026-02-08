---
created: 2026-01-13T17:24:23Z
last_updated: 2026-02-08T00:35:24Z
version: 1.3
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

### PaaS Operator Service
- **FastAPI** - Async Python web framework for operator API
- **Helm 3.13+** - Kubernetes package manager
- **kubectl 1.28+** - Kubernetes CLI
- **Pydantic** - Data validation for API schemas

### Development Tools
- **Claude Code PM** - Project management system
- **Git** - Version control
- **Docker** - Development environment
- **pytest** - PaaS Operator testing

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

使用 Worktree Development 腳本來管理開發環境：

```bash
# 啟動開發環境（自動設定 .env 並啟動 Docker）
./scripts/start-dev.sh

# 執行測試
./scripts/test-addon.sh

# 清理環境
./scripts/cleanup-worktree.sh
```

手動操作（需先執行 `./scripts/start-dev.sh`）：

```bash
# Restart after changes
docker compose restart web

# Update module
docker compose exec web odoo -d ${POSTGRES_DB:-odoo} -u woow_paas_platform --dev xml

# View logs
docker compose logs -f web
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
| WorkspaceDetailPage | `pages/workspace/` | Workspace detail view ✅ NEW |
| WorkspaceTeamPage | `pages/workspace/` | Team member management ✅ NEW |
| EmptyState | `pages/empty/` | Placeholder page |

### Modal Components ✅ NEW
| Component | Location | Purpose |
|-----------|----------|---------|
| CreateWorkspaceModal | `components/modal/` | Create new workspace dialog |
| InviteMemberModal | `components/modal/` | Invite team member dialog |

### Service Components
| Service | Location | Purpose |
|---------|----------|---------|
| workspace_service | `services/` | Workspace API client |
| cloud_service | `services/` | Cloud services API client |

## API Patterns

### RESTful-style JSON-RPC Endpoints (Odoo 18)

API 路由使用 `/api/...` prefix（不含 `/woow`），並拆分為 collection 和 detail endpoints：

```python
# Collection endpoint
@http.route('/api/workspaces', type='json', auth='user')
def api_workspace(self, action='list', **kw):
    if action == 'list': ...
    elif action == 'create': ...

# Detail endpoint with URL parameter
@http.route('/api/workspaces/<int:workspace_id>', type='json', auth='user')
def api_workspace_detail(self, workspace_id, action='get', **kw):
    if action == 'get': ...
    elif action == 'update': ...
    elif action == 'delete': ...
```

**Key Notes:**
- Use `type='json'` for direct JSON response
- Use `action` parameter (not `method`) to determine operation
- Collection vs detail endpoints split for RESTful semantics
- URL parameters for resource IDs (`<int:workspace_id>`)

### Cloud Services Architecture
```
Odoo (Frontend) ──HTTP──▶ PaaS Operator (FastAPI) ──Helm──▶ Kubernetes
```

## Update History
- 2026-02-08: Updated API patterns for RESTful refactor, added cloud service components
- 2026-02-01: Added new page components, modal components, services, API patterns
- 2026-01-14: Added asset bundles, OWL components, Docker commands
