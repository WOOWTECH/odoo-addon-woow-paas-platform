# Code Style and Conventions

## Python (Odoo Backend)

### Model Naming
```python
class WoowSubscription(models.Model):
    _name = 'woow_paas_platform.subscription'
    _description = 'PaaS Subscription'
```

### Field Definitions
- Use keyword arguments
- Include string, required, help as needed

### Extending Models
```python
class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'
```

## JavaScript/OWL (Frontend)

### Module Declaration
```javascript
/** @odoo-module **/
import { Component } from "@odoo/owl";
```

### Component Pattern
```javascript
export class MyComponent extends Component {
    static template = "woow_paas_platform.MyComponent";
    static props = { ... };
}
```

## SCSS
- Use `.o_woow_` prefix for isolation
- BEM naming: `&__element`, `&--modifier`
- Ordered SCSS loading: variables → base → layout → components → pages → main

## XML IDs
- Pattern: `{module}.{type}_{model}_{view_type}`

## Git Commits
- Format: `{prefix}: {message}` (English only)
- Prefixes: feat, fix, docs, style, refactor, perf, test, chore, revert, release
- No AI-generated attribution lines

## File Structure
- Models in `src/models/`
- Controllers in `src/controllers/`
- OWL components in `src/static/src/paas/components/`
- Pages in `src/static/src/paas/pages/`
- Services in `src/static/src/paas/services/`
- Views in `src/views/`
- Data files in `src/data/`
- Security in `src/security/`
