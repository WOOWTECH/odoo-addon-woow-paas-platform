---
created: 2026-01-13T17:24:23Z
last_updated: 2026-02-26T15:29:18Z
version: 1.4
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

## API Endpoint Pattern (RESTful-style JSON-RPC)

API endpoints 採用 collection/detail 分離模式：

```python
# Collection: /api/{resource}
@route("/api/workspaces", type="json", auth="user")
def api_workspace(self, action='list', ...):
    # action: list, create

# Detail: /api/{resource}/<int:id>
@route("/api/workspaces/<int:workspace_id>", type="json", auth="user")
def api_workspace_detail(self, workspace_id, action='get', ...):
    # action: get, update, delete

# Nested: /api/{resource}/<int:id>/{sub-resource}
@route("/api/workspaces/<int:workspace_id>/members", type="json", auth="user")
def api_workspace_members(self, workspace_id, action='list', ...):
    # action: list, invite
```

**Key conventions:**
- Route prefix: `/api/` (no `/woow/`)
- Operation selector: `action` parameter (not `method`)
- Resource IDs in URL path, not in request body

## External Service Integration Pattern

PaaS Operator 整合使用獨立的 Python service layer（非 ORM model）：

```python
# services/paas_operator.py
class PaasOperatorClient:
    """HTTP client for PaaS Operator Service."""

    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key

    def _request(self, method, endpoint, **kwargs):
        # Make HTTP request to PaaS Operator
        # Handle errors uniformly
```

Controller 透過 `ir.config_parameter` 取得設定後建立 client instance。

## Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Model name | `module.model` | `woow_paas_platform.subscription` |
| XML ID | `module.type_name` | `woow_paas_platform.view_subscription_form` |
| Menu ID | `menu_*` | `menu_woow_paas_platform_root` |
| Config param | `module.param_name` | `woow_paas_platform.api_key` |
| API route | `/api/{resource}` | `/api/workspaces` |

## Module Hooks Pattern

Odoo module hooks for initialization and cleanup logic:

```python
# hooks.py
def post_init_hook(env):
    """Called after module installation."""
    # Initialize default data, run setup tasks

def uninstall_hook(env):
    """Called before module uninstallation."""
    # Clean up resources
```

Referenced in `__manifest__.py`:
```python
'post_init_hook': 'post_init_hook',
'uninstall_hook': 'uninstall_hook',
```

## Migration Pattern

Database migrations for schema/data changes between versions:

```
src/migrations/
├── 18.0.1.0.1/
│   └── post-migrate.py    # Runs after upgrade to 1.0.1
└── 18.0.1.0.2/
    └── post-migrate.py    # Runs after upgrade to 1.0.2
```

Migration files use `def migrate(cr, version)` entry point.

## Helm Value Restriction Pattern

Templates define allowed configuration keys via `helm_value_specs` (JSON field):

```python
# cloud_app_template.py
helm_value_specs = fields.Text()  # JSON defining allowed keys + types + defaults
```

Frontend `HelmValueForm` component renders form inputs based on specs, and backend silently filters unauthorized keys on creation/update.

## AI Integration Pattern (ai_base_gt)

混合架構：使用 `ai_base_gt` 的 Odoo 層管理配置和助手身份，保留 LangChain 做 AI 呼叫。

```python
# ai_client.py - Factory method pattern
class AiClient:
    @classmethod
    def from_assistant(cls, assistant_record):
        """Build client from ai.assistant record."""
        config = assistant_record.ai_config_id
        return cls(
            api_key=config.api_key,
            model=config.model_id,
            base_url=config.api_base_url,
            system_prompt=assistant_record.system_prompt,
        )
```

Key models from `ai_base_gt`:
- `ai.config` — API provider 設定（key, model, base_url, type）
- `ai.assistant` — 助手定義（system prompt, linked config）

## Lazy Loading Pattern (Heavy Libraries)

大型 JS library 放在 `static/lib/`（非 `static/src/paas/`）避免被 `__manifest__.py` glob 自動打包：

```javascript
// mermaid_loader.js
class MermaidLoader {
    async load() {
        if (window.mermaid) return;
        return new Promise((resolve) => {
            const script = document.createElement('script');
            script.src = '/woow_paas_platform/static/lib/mermaid/mermaid.min.js';
            script.onload = () => resolve();
            document.head.appendChild(script);
        });
    }
}
```

適用於：mermaid.js (~2MB) 等按需載入的資源。

## Update History
- 2026-02-26: Added AI integration pattern (ai_base_gt), lazy loading pattern (mermaid)
- 2026-02-15: Added module hooks, migration, and helm value restriction patterns
- 2026-02-08: Updated external service pattern from ORM model to standalone service class
- 2026-02-08: Added RESTful API endpoint pattern and external service integration pattern
