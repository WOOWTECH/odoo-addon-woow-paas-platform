---
created: 2026-01-13T17:24:23Z
last_updated: 2026-03-01T14:27:01Z
version: 1.5
author: Claude Code PM System
---

# Project Overview

## Summary

Woow PaaS Platform is an Odoo 18 addon module providing the foundation for a multi-tenant PaaS application. The module now includes a complete standalone OWL frontend application with dashboard, workspace management, and responsive UI.

## Current Features

### Implemented
1. **Module Infrastructure**
   - Odoo 18 compatible manifest
   - Proper dependency declarations
   - Multiple asset bundles (backend + standalone)

2. **Standalone OWL Application** ✅ NEW
   - Independent frontend at `/woow`
   - Hash-based SPA routing
   - AppShell layout (Sidebar + Header + Content)
   - Dashboard with stats and activity feed
   - Workspace list page
   - Empty state placeholders
   - SCSS theme system with CSS variables
   - Base UI components (Icon, Card, Button)

3. **Settings Framework**
   - Extended `res.config.settings` model
   - Settings page in Odoo configuration
   - Ready for configuration fields

4. **Menu Structure**
   - Root menu "Woow PaaS" with icon
   - Category: WOOW
   - Ready for child menu items

5. **Project Management**
   - Claude Code PM integration
   - Command set for PRDs, Epics, Issues
   - Rules for code standards

6. **Workspace Management** ✅ Complete (Phase 3)
   - Workspace model with CRUD operations
   - WorkspaceAccess model for member roles
   - RESTful-style JSON API endpoints
   - Frontend service layer
   - Workspace list, detail, team pages
   - Create workspace modal
   - Invite member modal

7. **Cloud Services** ✅ Complete (Phase 4)
   - CloudAppTemplate model - Application marketplace templates
   - CloudService model - Deployed service instances with lifecycle management
   - PaaS Operator Service (FastAPI at `extra/paas-operator/`)
   - Marketplace UI components
   - Service deploy, start, stop, delete, rollback, revisions API
   - Helm chart for K8s deployment of operator

8. **Cloud Service Config Restriction** ✅ Complete
   - Helm value key 白名單限制（`helm_value_specs`）
   - HelmValueForm 元件取代 textarea
   - 靜默過濾未授權 keys

9. **AI Assistant** 🔄 In Progress
   - AI Agent / AI Provider / AI Client models
   - AI Assistant controller + API endpoints
   - AiAssistantPage / AiChatPage 前端頁面
   - AiChat / AiMentionDropdown 元件
   - Markdown parsing (marked.js) + HTML sanitization (DOMPurify)
   - Support projects / tasks 管理頁面
   - Project Kanban 頁面
   - Module hooks (`hooks.py`) + 2 database migrations

10. **Smart Home HA Integration** ✅ Complete
    - Smart Home model（Cloudflare Tunnel 生命週期管理）
    - `action_provision()` / `action_delete()` / `action_refresh_status()`
    - Cloudflare Tunnel 資訊（tunnel_id, tunnel_token, subdomain, route, connector）
    - PaaS Operator Tunnel API（create/get/delete/token endpoints）

11. **OAuth 2.0 系統** ✅ Complete
    - OAuthClient model（client credentials, redirect URIs）
    - OAuthToken model（access/refresh tokens, scopes, expiry）
    - OAuthAuthorizationCode model（PKCE support）
    - Token validation（has_scope, is_access_token_valid, revoke）

12. **HA API Endpoints** ✅ Complete
    - `GET /api/ha/workspaces` - 使用者可存取的 workspace 列表
    - `GET /api/ha/workspaces/<id>/smarthomes` - workspace 內的 smart home 列表
    - `GET /api/ha/smarthomes/<id>/tunnel-token` - 取得 Cloudflare Tunnel Token
    - Bearer token 認證（OAuth 2.0 access token）
    - Scope-based 權限控制（smarthome:read, smarthome:tunnel, workspace:read）

13. **K8s Dev Sandbox** ✅ Complete
    - Helm chart `odoo-dev-sandbox`（Odoo + PostgreSQL + Nginx）
    - Management scripts（create, destroy, list, status, logs, test, build）
    - extra-addons hostPath 掛載支援
    - pip install 等待機制（避免 DB init race condition）

### Not Yet Implemented
- External integrations (Phase 5)
- Multi-tenant logic
- Dark mode theme

## Module Information

| Property | Value |
|----------|-------|
| Technical Name | `woow_paas_platform` |
| Version | 18.0.1.0.2 |
| Category | WOOW |
| Application | Yes |
| License | LGPL-3 |

## Integration Points

### Current
- `base` - Core Odoo models and security
- `web` - Frontend framework and assets
- Google Fonts (Manrope, Outfit)
- Material Symbols icons

### Potential Future
- `sale` - Subscription sales
- `account` - Billing and invoicing
- `portal` - Customer self-service
- `mail` - Notifications
- External APIs (TBD)

## Development Status

```
Phase 1: Foundation      [████████] 100%
Phase 2: OWL App Shell   [████████] 100%
Phase 3: Core Models     [████████] 100%  ✓ Complete (Workspace + WorkspaceAccess)
Phase 4: Cloud Services  [████████] 100%  ✓ Complete (Templates + Services + Operator)
Phase 4b: Config Restrict[████████] 100%  ✓ Complete (Helm value whitelist)
Phase 4c: AI Assistant   [██████  ]  75%  🔄 In Progress
Phase 4d: Smart Home HA  [████████] 100%  ✓ Complete (Smart Home + OAuth2 + HA API)
Phase 5: Integrations    [        ]   0%
```

## Quick Links

| Resource | Location |
|----------|----------|
| Module manifest | `__manifest__.py` |
| Development guide | `CLAUDE.md` |
| PM commands | `/pm:help` |
| Standalone app | `/woow` |
| OWL components | `static/src/paas/` |

## Getting Started

```bash
# Install/update module
./odoo-bin -c odoo.conf -u woow_paas_platform

# Access standalone app
http://localhost/woow

# Access settings
Settings → General Settings → Woow PaaS
```

## Update History
- 2026-03-01: Added Smart Home HA Integration, OAuth 2.0, HA API, K8s Dev Sandbox features
- 2026-02-15: Added AI Assistant feature, Cloud Service Config Restriction, version bump to 18.0.1.0.2
- 2026-02-08: Phase 3 & 4 complete, added Cloud Services feature
- 2026-02-01: Added Workspace management (Phase 3 in progress)
- 2026-01-14: Added standalone OWL application (Phase 2 complete)
