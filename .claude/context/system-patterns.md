---
created: 2026-01-13T17:24:23Z
last_updated: 2026-01-13T17:24:23Z
version: 1.0
author: Claude Code PM System
---

# System Patterns

## Architecture Style

This module follows **Odoo's MVC-like Architecture**:
- **Models** - Business logic and data persistence
- **Views** - XML-defined UI layouts
- **Controllers** - HTTP routes (when needed)

## Design Patterns

### Model Inheritance
Odoo uses class-based inheritance for extending functionality:

```python
class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'
    # Extends existing model without replacing it
```

**Types of Inheritance:**
- `_inherit` - Add fields/methods to existing model
- `_inherits` - Delegate inheritance (composition)
- `_name` + `_inherit` - Create new model based on existing

### Configuration Parameter Pattern
System-wide settings use `config_parameter`:

```python
woow_api_key = fields.Char(
    string='API Key',
    config_parameter='woow_paas_platform.api_key',
)
```
- Stored in `ir.config_parameter` table
- Accessed via `self.env['ir.config_parameter'].sudo().get_param()`

### View Inheritance Pattern
XML views use XPath for extension:

```xml
<record id="view_inherit" model="ir.ui.view">
    <field name="inherit_id" ref="module.original_view"/>
    <field name="arch" type="xml">
        <xpath expr="//form" position="inside">
            <!-- New content here -->
        </xpath>
    </field>
</record>
```

## Data Flow

```
User Action → Controller/View → Model Method → ORM → PostgreSQL
                    ↓
            Response/Update UI
```

### ORM Patterns
- `self.env['model.name']` - Access models
- `record.write({})` - Update records
- `Model.create({})` - Create records
- `record.unlink()` - Delete records
- `Model.search([])` - Query records

## Security Patterns

### Access Control Layers
1. **Model-level** (`ir.model.access.csv`)
   - CRUD permissions per model per group
2. **Record-level** (`ir.rule`)
   - Domain-based record filtering
3. **Field-level** (`groups` attribute)
   - Show/hide fields per group

### CSV Format
```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
```

## Frontend Patterns (OWL)

### Component Structure
```
ComponentName/
├── ComponentName.js     # Component logic
├── ComponentName.xml    # QWeb template
└── ComponentName.scss   # Component styles
```

### Service Pattern
```javascript
// services/my_service.js
export const myService = {
    dependencies: ['rpc'],
    start(env, { rpc }) {
        return {
            async fetchData() { /* ... */ }
        };
    }
};
```

## Menu Pattern

Hierarchical menu with sequence ordering:
```xml
<menuitem
    id="menu_root"
    name="App Name"
    sequence="100"
    web_icon="module,static/description/icon.png"/>

<menuitem
    id="menu_child"
    name="Feature"
    parent="menu_root"
    action="action_id"
    sequence="10"/>
```

## Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Model name | `module.model` | `woow_paas_platform.subscription` |
| XML ID | `module.type_name` | `woow_paas_platform.view_subscription_form` |
| Menu ID | `menu_*` | `menu_woow_paas_platform_root` |
| Config param | `module.param_name` | `woow_paas_platform.api_key` |
