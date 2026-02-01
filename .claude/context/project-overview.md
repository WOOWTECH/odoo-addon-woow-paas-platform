---
created: 2026-01-13T17:24:23Z
last_updated: 2026-02-01T13:22:40Z
version: 1.2
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

6. **Workspace Management** ✅ NEW (Phase 3 In Progress)
   - Workspace model with CRUD operations
   - WorkspaceAccess model for member roles
   - JSON API endpoints (`type="json"`)
   - Frontend service layer
   - Workspace list, detail, team pages
   - Create workspace modal
   - Invite member modal

### Not Yet Implemented
- Additional business models (subscriptions, billing)
- External API integrations
- Backend OWL components
- Unit tests
- Multi-tenant logic
- Dark mode theme

## Module Information

| Property | Value |
|----------|-------|
| Technical Name | `woow_paas_platform` |
| Version | 18.0.1.0.0 |
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
Phase 1: Foundation    [████████] 100%
Phase 2: OWL App Shell [████████] 100%
Phase 3: Core Models   [████    ]  50%  ← In Progress (Workspace + WorkspaceAccess)
Phase 4: Integrations  [        ]   0%
Phase 5: Testing       [        ]   0%
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
- 2026-02-01: Added Workspace management (Phase 3 in progress)
- 2026-01-14: Added standalone OWL application (Phase 2 complete)
