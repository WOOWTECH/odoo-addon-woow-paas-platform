---
created: 2026-01-13T17:24:23Z
last_updated: 2026-01-13T17:24:23Z
version: 1.0
author: Claude Code PM System
---

# Project Style Guide

## Python Conventions

### Imports
```python
# Standard library
import logging
from datetime import datetime

# Third-party (minimal)
# import requests

# Odoo
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
```

### Model Naming
```python
class WoowSubscription(models.Model):
    _name = 'woow_paas_platform.subscription'
    _description = 'PaaS Subscription'
    _order = 'create_date desc'
```

- Class name: PascalCase with `Woow` prefix
- Technical name: `woow_paas_platform.{model}`
- Description: Human-readable, no technical jargon

### Field Definitions
```python
name = fields.Char(
    string='Name',
    required=True,
    help='Brief description for users',
)

state = fields.Selection(
    selection=[
        ('draft', 'Draft'),
        ('active', 'Active'),
        ('cancelled', 'Cancelled'),
    ],
    string='Status',
    default='draft',
    tracking=True,
)
```

- Use keyword arguments
- One field per logical block
- Include `string` and `help` for user clarity

### Methods
```python
def action_confirm(self):
    """Confirm the subscription and activate services."""
    self.ensure_one()
    self.write({'state': 'active'})
    return True

@api.depends('line_ids.amount')
def _compute_total(self):
    for record in self:
        record.total = sum(record.line_ids.mapped('amount'))
```

- Docstrings for public methods
- Use `ensure_one()` for single-record operations
- Loop through `self` for computed fields

## XML Conventions

### View IDs
```xml
<!-- Pattern: {module}.{type}_{model}_{view_type} -->
<record id="view_subscription_form" model="ir.ui.view">
<record id="view_subscription_tree" model="ir.ui.view">
<record id="action_subscription" model="ir.actions.act_window">
```

### Form View Structure
```xml
<form string="Subscription">
    <header>
        <button name="action_confirm" string="Confirm" type="object"/>
        <field name="state" widget="statusbar"/>
    </header>
    <sheet>
        <group>
            <group>
                <field name="name"/>
                <field name="partner_id"/>
            </group>
            <group>
                <field name="date_start"/>
                <field name="date_end"/>
            </group>
        </group>
        <notebook>
            <page string="Lines" name="lines">
                <field name="line_ids"/>
            </page>
        </notebook>
    </sheet>
    <chatter/>
</form>
```

### Menu IDs
```xml
<!-- Pattern: menu_{module}_{feature} -->
<menuitem id="menu_woow_paas_subscriptions" .../>
```

## JavaScript/OWL Conventions

### Component Structure
```javascript
/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";

export class SubscriptionCard extends Component {
    static template = "woow_paas_platform.SubscriptionCard";
    static props = {
        subscription: Object,
        onSelect: { type: Function, optional: true },
    };

    setup() {
        // Component setup logic
    }
}
```

### Template Naming
```xml
<!-- Pattern: {module}.{ComponentName} -->
<t t-name="woow_paas_platform.SubscriptionCard">
```

## SCSS Conventions

### Naming
```scss
// Component prefix: .o_woow_
.o_woow_subscription_card {
    &__header { }
    &__body { }
    &--active { }
}
```

### Variables
```scss
// Use Odoo variables when possible
$woow-primary: $o-main-color-muted;
$woow-spacing: $o-grid-gutter-width;
```

## File Organization

### Python Files
- One model per file (or closely related models)
- Max ~300 lines per file
- Split large models into mixins

### XML Files
- One model's views per file
- Separate security rules in `security/`
- Keep templates near their views

## Comments

### When to Comment
- Complex business logic
- Non-obvious workarounds
- API contracts

### When NOT to Comment
- Self-documenting code
- Field definitions (use `help` attribute)
- Standard Odoo patterns

## Commit Messages

Follow the format: `{type}: {description}`

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Formatting
- `refactor`: Code restructure
- `test`: Tests
- `chore`: Maintenance
