---
created: 2026-01-13T17:24:23Z
last_updated: 2026-02-26T15:29:18Z
version: 1.3
author: Claude Code PM System
---

# Product Context

## Product Definition

**Woow PaaS Platform** is a base module that provides the foundation for building PaaS (Software as a Service) applications within the Odoo ecosystem.

## Target Users

### Primary Users
1. **PaaS Administrators**
   - Configure platform settings
   - Manage subscriptions and tenants
   - Monitor platform health

2. **End Users (PaaS Customers)**
   - Access PaaS features
   - Manage their accounts
   - Use platform services

### Secondary Users
1. **Developers**
   - Build features on top of the platform
   - Extend functionality
   - Customize for specific needs

## User Personas

### Platform Administrator
- **Role:** Manages the PaaS platform
- **Goals:** Configure services, monitor usage, manage customers
- **Pain Points:** Complex setup, lack of visibility into usage

### PaaS Customer
- **Role:** End user of PaaS services
- **Goals:** Use platform features, manage subscription
- **Pain Points:** Unclear pricing, feature limitations

## Core Use Cases

### UC-001: Platform Configuration
Administrator configures platform-wide settings through Odoo's Settings interface.

### UC-002: Feature Access
End users access PaaS features through the "Woow PaaS" menu.

### UC-003: Subscription Management (Future)
Customers manage their subscription plans and billing.

## Requirements (High-Level)

### Functional
- Platform configuration interface
- Menu structure for PaaS features
- Integration with Odoo's user management
- (Future) Subscription management
- (Future) Multi-tenant support

### Non-Functional
- **Performance:** Fast loading within Odoo
- **Security:** Follow Odoo security patterns
- **Scalability:** Support multiple tenants
- **Maintainability:** Clean, documented code

## Current Feature State

| Feature | Status |
|---------|--------|
| Basic scaffold | Complete |
| Settings extension | Complete |
| Menu structure | Complete |
| Standalone OWL App | Complete |
| Workspace management | Complete |
| Cloud Services (Templates + Deploy) | Complete |
| PaaS Operator (Helm wrapper) | Complete |
| Cloud Service Config Restriction | Complete |
| AI Assistant (Agent + Chat) | Complete |
| AI Refactor (ai_base_gt) | Complete |
| OpenAI Compatible Provider | Complete |
| Support Projects / Tasks | Complete |
| Project-Cloud Service Binding | Complete |
| Navbar Fix + Responsive | Complete |
| Mermaid Diagram Rendering | Complete |
| External integrations | Not started |
| Billing integration | Not started |

## Success Metrics (Proposed)

1. **Platform Adoption**
   - Number of active tenants
   - Daily active users

2. **Feature Usage**
   - Feature engagement rates
   - Time spent in platform

3. **Business Health**
   - Subscription retention rate
   - Revenue per customer
