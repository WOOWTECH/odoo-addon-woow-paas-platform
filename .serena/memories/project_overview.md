# Project Overview

## Purpose
`woow_paas_platform` is an Odoo 18 addon module providing a multi-tenant PaaS (Platform as a Service) application. It includes a standalone OWL frontend, cloud service deployment via Kubernetes/Helm, AI assistant features, and workspace management.

## Tech Stack
- **Backend**: Python 3.11+ (Odoo 18 framework)
- **Frontend**: OWL (Odoo Web Library) - standalone app at `/woow` with hash-based routing
- **Styling**: SCSS with BEM naming (`.o_woow_` prefix)
- **Cloud Services**: FastAPI (PaaS Operator at `extra/paas-operator/`), Helm, Kubernetes
- **Database**: PostgreSQL 14+ (via Odoo ORM)
- **Testing**: Odoo test framework (Python), pytest (PaaS Operator)

## Key Modules
- **Odoo Models**: workspace, workspace_access, cloud_app_template, cloud_service, ai_agent, ai_provider, ai_client, project_project, project_task, discuss_channel
- **Controllers**: paas.py (main /woow endpoint), ai_assistant.py (AI features)
- **Frontend**: Standalone OWL app in `src/static/src/paas/` with pages, components, services, layout
- **PaaS Operator**: FastAPI service in `extra/paas-operator/` wrapping Helm CLI

## Architecture
```
Odoo (Frontend OWL) → Odoo Backend (Python) → PaaS Operator (FastAPI) → Kubernetes (Helm)
```

## Dependencies
- Odoo modules: base, web, project, mail, bus
- External: Google Fonts, Material Symbols
- PaaS Operator: FastAPI, Helm 3.13+, kubectl 1.28+
