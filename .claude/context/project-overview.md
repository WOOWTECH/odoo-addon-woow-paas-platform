---
created: 2026-01-13T17:24:23Z
last_updated: 2026-02-26T15:29:18Z
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

9. **AI Assistant** ✅ Complete
   - AI models refactored to use `ai_base_gt` (`ai.config` + `ai.assistant`)
   - LangChain AI client with `from_assistant()` factory method
   - AI Assistant controller + API endpoints + SSE streaming
   - AiAssistantPage / AiChatPage 前端頁面
   - AiChat / AiMentionDropdown 元件
   - Markdown parsing (marked.js) + HTML sanitization (DOMPurify)
   - Support projects / tasks 管理頁面
   - Project Kanban 頁面
   - Module hooks (`hooks.py`) + 2 database migrations
   - OpenAI Compatible provider type 支援

10. **Project-Cloud Service Binding** ✅ Complete
    - Project 關聯從 Workspace 改為 Cloud Service（1:1 對應）
    - AI system prompt 注入 Cloud Service context

11. **Navbar Fix and Responsive** ✅ Complete
    - Header + 子頁面 breadcrumb 可點擊導航
    - Responsive CSS 修復

12. **AI Chat Mermaid Rendering** ✅ Complete
    - Mermaid.js lazy loading（~2MB, dynamic script tag）
    - Markdown parser mermaid code block 偵測
    - 互動式 SVG 圖表（zoom/pan + 原始碼切換）
    - SSE 串流中 mermaid block 完整性追蹤

### Not Yet Implemented
- External integrations (Phase 5)
- Unit tests
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
Phase 4c: AI Assistant   [████████] 100%  ✓ Complete (Chat + Mermaid + Refactor)
Phase 4d: UI Polish      [████████] 100%  ✓ Complete (Navbar + Responsive)
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
- 2026-02-26: All phases through 4d complete. AI Assistant fully done (refactored to ai_base_gt, mermaid rendering, OpenAI compatible). Added navbar fix, project-cloud binding.
- 2026-02-15: Added AI Assistant feature, Cloud Service Config Restriction, version bump to 18.0.1.0.2
- 2026-02-08: Phase 3 & 4 complete, added Cloud Services feature
- 2026-02-01: Added Workspace management (Phase 3 in progress)
- 2026-01-14: Added standalone OWL application (Phase 2 complete)
