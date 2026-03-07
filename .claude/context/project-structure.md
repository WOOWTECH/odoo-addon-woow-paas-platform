---
created: 2026-01-13T17:24:23Z
last_updated: 2026-03-01T14:30:52Z
version: 1.6
author: Claude Code PM System
---

# Project Structure

## Directory Layout

```
woow_paas_platform/
├── src/                         # Odoo module source code
│   ├── __init__.py              # Package initializer - imports models, controllers, hooks
│   ├── __manifest__.py          # Odoo module manifest (metadata, deps, assets)
│   ├── hooks.py                 # Odoo module hooks (post_init, uninstall)
│   ├── requirements.txt         # Python external dependencies
│   │
│   ├── controllers/             # HTTP routes (JSON API)
│   │   ├── __init__.py
│   │   ├── paas.py              # /woow endpoint + workspace/cloud/members API routes
│   │   ├── ai_assistant.py      # AI assistant API endpoints
│   │   ├── ha_api.py            # HA API endpoints (Bearer token auth)
│   │   ├── oauth2.py            # OAuth 2.0 authorization endpoints
│   │   └── smart_home.py        # Smart Home management API
│   │
│   ├── models/                  # Business logic layer
│   │   ├── __init__.py          # Model imports
│   │   ├── res_config_settings.py       # PaaS Operator + AI configuration
│   │   ├── workspace.py                 # Workspace model
│   │   ├── workspace_access.py          # Workspace access/member model
│   │   ├── cloud_app_template.py        # Application marketplace templates
│   │   ├── cloud_service.py             # Deployed service instances
│   │   ├── ai_agent.py                  # AI Agent model
│   │   ├── ai_client.py                 # AI Client (API integration)
│   │   ├── ai_provider.py               # AI Provider model
│   │   ├── smart_home.py                # Smart Home model (Cloudflare Tunnel)
│   │   ├── oauth_client.py              # OAuth Client model
│   │   ├── oauth_token.py               # OAuth Token model
│   │   ├── oauth_code.py                # OAuth Authorization Code model
│   │   ├── discuss_channel.py           # Discuss channel extensions
│   │   ├── project_project.py           # Project model extensions
│   │   └── project_task.py              # Project task model extensions
│   │
│   ├── services/                # Python service layer (non-ORM)
│   │   ├── __init__.py
│   │   └── paas_operator.py     # HTTP client for PaaS Operator
│   │
│   ├── migrations/              # Database migrations
│   │   ├── 18.0.1.0.1/
│   │   │   └── post-migrate.py  # Migration to v1.0.1
│   │   └── 18.0.1.0.2/
│   │       └── post-migrate.py  # Migration to v1.0.2
│   │
│   ├── tests/                   # Odoo module tests
│   │   ├── __init__.py
│   │   ├── test_cloud_app_template.py
│   │   ├── test_cloud_service.py
│   │   ├── test_cloud_api.py
│   │   ├── test_paas_operator.py
│   │   ├── test_smart_home.py         # Smart Home model tests (15)
│   │   ├── test_oauth2.py             # OAuth 2.0 tests (14)
│   │   └── test_ha_api.py             # HA API tests (12)
│   │
│   ├── data/                    # Default data files
│   │   └── cloud_app_templates.xml  # Default app templates
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
│           │   ├── lib/           # Third-party libraries
│           │   │   ├── marked.min.js   # Markdown parser
│           │   │   └── purify.min.js   # DOMPurify HTML sanitizer
│           │   ├── layout/
│           │   │   ├── app_shell/  # AppShell.js/xml
│           │   │   ├── sidebar/    # Sidebar.js/xml
│           │   │   └── header/     # Header.js/xml
│           │   ├── components/
│           │   │   ├── icon/       # WoowIcon
│           │   │   ├── card/       # WoowCard
│           │   │   ├── button/     # WoowButton
│           │   │   ├── modal/      # CreateWorkspaceModal, InviteMemberModal, DeleteServiceModal, RollbackModal, EditDomainModal, CreateProjectModal, CreateTaskModal
│           │   │   ├── marketplace/  # AppCard, CategoryFilter
│           │   │   ├── common/     # StatusBadge
│           │   │   ├── config/     # HelmValueForm
│           │   │   ├── service-card/  # ServiceCard
│           │   │   ├── ai-chat/    # AiChat (chat UI component)
│           │   │   └── ai-mention/ # AiMentionDropdown
│           │   ├── pages/
│           │   │   ├── dashboard/        # DashboardPage
│           │   │   ├── workspace/        # WorkspaceListPage, WorkspaceDetailPage, WorkspaceTeamPage
│           │   │   ├── marketplace/      # AppMarketplacePage
│           │   │   ├── service/          # ServiceDetailPage + tabs/ (OverviewTab, ConfigurationTab)
│           │   │   ├── configure/        # AppConfigurationPage
│           │   │   ├── ai-assistant/     # AiAssistantPage
│           │   │   ├── ai-chat/          # AiChatPage
│           │   │   ├── project-kanban/   # ProjectKanbanPage
│           │   │   ├── support-projects/ # SupportProjectsPage
│           │   │   ├── support-tasks/    # SupportTasksPage
│           │   │   ├── task-detail/      # TaskDetailPage
│           │   │   ├── smart-home/      # SmartHomePage
│           │   │   └── empty/            # EmptyState
│           │   ├── services/
│           │   │   ├── workspace_service.js  # Workspace API client
│           │   │   ├── cloud_service.js      # Cloud services API client
│           │   │   ├── ai_service.js         # AI assistant API client
│           │   │   ├── support_service.js    # Support/project API client
│           │   │   ├── rpc.js                # RPC utility
│           │   │   ├── html_sanitize.js      # HTML sanitization service
│           │   │   ├── markdown_parser.js    # Markdown parsing service
│           │   │   └── utils.js              # Shared utility functions
│           │   └── styles/
│           │       ├── 00_variables.scss
│           │       ├── 10_base.scss
│           │       ├── 20_layout.scss
│           │       ├── 30_components.scss
│           │       ├── 99_main.scss
│           │       └── pages/
│           │           ├── 10_empty.scss
│           │           ├── 20_workspace.scss
│           │           ├── 30_dashboard.scss
│           │           ├── 40_marketplace.scss
│           │           └── 40_configure.scss
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
├── extra/paas-operator/          # PaaS Operator Service (FastAPI)
│   ├── src/
│   │   ├── main.py              # FastAPI app + middleware
│   │   ├── config.py            # Settings management
│   │   ├── api/                 # API endpoints
│   │   │   ├── releases.py      # Helm release operations
│   │   │   └── namespaces.py    # Namespace management
│   │   ├── services/
│   │   │   └── helm.py          # Helm CLI wrapper
│   │   ├── models/
│   │   │   └── schemas.py       # Pydantic models
│   │   └── api/
│   │       ├── releases.py      # Helm release operations
│   │       ├── namespaces.py    # Namespace management
│   │       ├── routes.py        # Cloudflare DNS route operations
│   │       └── tunnels.py       # Cloudflare Tunnel CRUD API
│   ├── tests/                   # Operator unit tests
│   ├── helm/                    # Helm chart for K8s deployment
│   ├── Dockerfile
│   └── requirements.txt
│
├── charts/                      # Helm charts
│   └── odoo-dev-sandbox/        # K8s dev sandbox chart
│       ├── Chart.yaml
│       ├── values.yaml
│       ├── values-local.yaml    # K3s local overrides
│       ├── values-ci.yaml       # CI/CD overrides
│       └── templates/           # K8s resource templates
│
├── docs/                        # Documentation
│   ├── deployment/              # K8s setup, troubleshooting
│   ├── development/             # Developer guides
│   ├── testing/                 # Testing guides (AI chat E2E)
│   └── spec/                    # Feature specifications
│
├── CLAUDE.md                    # Claude Code development guide
│
├── .serena/                     # Serena IDE integration
│   ├── project.yml              # Project configuration
│   └── memories/                # Serena knowledge base
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
- `paas.py` handles workspace, cloud services, members routes
- `ai_assistant.py` handles AI assistant API routes
- `ha_api.py` handles HA API endpoints (Bearer token auth)
- `oauth2.py` handles OAuth 2.0 authorization endpoints
- `smart_home.py` handles Smart Home management API
- Using `type="json"` for Odoo 18 JSON responses
- Imported via `src/controllers/__init__.py`

### XML Views (`src/views/`)
- One file per model's views
- Menu definitions in `menu.xml`
- QWeb templates for standalone apps

### Frontend (`src/static/src/`)
- `paas/` - Standalone OWL application
  - `lib/` - Third-party libraries (marked.js, DOMPurify)
  - `services/` - API client services + utilities (workspace, cloud, AI, support, rpc, markdown, sanitize)
  - `components/` - UI components (modal, marketplace, common, config, service-card, ai-chat, ai-mention)
  - `pages/` - Page components (dashboard, workspace, marketplace, service, configure, ai-assistant, ai-chat, project-kanban, support-projects, support-tasks, task-detail, smart-home, empty)
- `scss/` - Backend stylesheets
- `components/` - Backend OWL components (future)
- `services/` - JS services (future)

## Key Files

| File | Purpose |
|------|---------|
| `src/__manifest__.py` | Module metadata, version, dependencies, assets |
| `src/hooks.py` | Module hooks (post_init, uninstall) |
| `src/controllers/paas.py` | `/woow` endpoint + workspace/cloud/members API routes |
| `src/controllers/ai_assistant.py` | AI assistant API endpoints |
| `src/controllers/ha_api.py` | HA API (Bearer token auth) |
| `src/controllers/oauth2.py` | OAuth 2.0 authorization |
| `src/controllers/smart_home.py` | Smart Home management API |
| `src/models/workspace.py` | Workspace model with CRUD |
| `src/models/workspace_access.py` | Workspace member access control |
| `src/models/cloud_app_template.py` | Application marketplace templates |
| `src/models/cloud_service.py` | Deployed service instances |
| `src/models/ai_agent.py` | AI Agent model |
| `src/models/ai_provider.py` | AI Provider model |
| `src/models/ai_client.py` | AI Client API integration |
| `src/models/smart_home.py` | Smart Home (Cloudflare Tunnel) |
| `src/models/oauth_client.py` | OAuth Client model |
| `src/models/oauth_token.py` | OAuth Token model |
| `src/models/oauth_code.py` | OAuth Authorization Code model |
| `src/services/paas_operator.py` | HTTP client for PaaS Operator |
| `src/static/src/paas/services/cloud_service.js` | Frontend cloud services API client |
| `src/static/src/paas/services/ai_service.js` | Frontend AI assistant API client |
| `extra/paas-operator/src/main.py` | PaaS Operator FastAPI app |
| `src/views/paas_app.xml` | QWeb template for standalone app |
| `src/static/src/paas/root.js` | OWL app root + router |
| `src/static/src/paas/services/workspace_service.js` | Frontend workspace API client |
| `src/security/ir.model.access.csv` | CRUD permissions per model |

## Update History
- 2026-03-01: Added Smart Home HA Integration (controllers: ha_api, oauth2, smart_home; models: smart_home, oauth_client/token/code; tests: 3 suites; pages: smart-home; paas-operator: tunnels/routes API; charts/odoo-dev-sandbox)
- 2026-02-15: Added AI Assistant feature (models, controller, pages, components, services, lib/), migrations/, hooks.py, .serena/, docs/testing/
- 2026-02-08: Full frontend structure update - added marketplace, service, configure pages; new component groups; removed hash.js, paas_operator_client.py, cloud_services.py controller
- 2026-02-08: Added extra/paas-operator/, cloud models, cloud_service.js, expanded docs/
- 2026-02-01: Updated for src/ directory structure, added models and services
- 2026-01-14: Added controllers/, paas/ frontend structure
