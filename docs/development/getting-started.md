# Developer Getting Started Guide

Welcome to the WoowTech PaaS Platform development guide! This document will help you set up your local development environment and understand the project structure.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Local Development Setup](#local-development-setup)
- [Project Structure](#project-structure)
- [Running Tests](#running-tests)
- [Development Workflow](#development-workflow)
- [Adding New Features](#adding-new-features)
- [Code Style Guidelines](#code-style-guidelines)

---

## Prerequisites

Before you begin, ensure you have the following installed:

### Required
- **Python 3.10+** - For PaaS Operator development
- **Docker & Docker Compose** - For running Odoo
- **Node.js 18+** - For frontend development (OWL components)
- **Git** - Version control
- **kubectl** - Kubernetes CLI (for testing K8s integration)
- **Helm 3.13+** - For testing Helm operations

### Optional (for full stack development)
- **K3s/K3d** - Local Kubernetes cluster
- **PostgreSQL client** - Database debugging
- **curl/httpie** - API testing

### Verify Installation

```bash
# Check versions
python --version    # Should be 3.10+
docker --version
node --version      # Should be 18+
kubectl version --client
helm version

# Test Docker
docker run hello-world
```

---

## Local Development Setup

### 1. Clone Repository

```bash
git clone https://github.com/WOOWTECH/odoo-addon-woow-paas-platform.git
cd odoo-addon-woow-paas-platform
```

### 2. Set Up PaaS Operator (Optional - for full stack)

The PaaS Operator is optional for frontend development but required for testing Cloud Services features.

```bash
cd extra/paas-operator

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install development dependencies
pip install pytest pytest-asyncio pytest-cov black flake8

# Create environment file
cp .env.example .env
```

Edit `.env` file:
```bash
# Generate a secure API key
openssl rand -hex 32

# Add to .env
API_KEY=<generated-key>
LOG_LEVEL=debug
NAMESPACE_PREFIX=paas-ws-
HELM_BINARY=/usr/local/bin/helm
HELM_TIMEOUT=300
```

Run the PaaS Operator:
```bash
# From extra/paas-operator directory
uvicorn src.main:app --reload --port 8000
```

Access:
- API Documentation: http://localhost:8000/docs
- Health Check: http://localhost:8000/health

### 3. Set Up Odoo

```bash
# Return to project root
cd ../..

# 使用 Worktree Development 腳本啟動開發環境（推薦）
./scripts/start-dev.sh

# 或查看日誌
docker compose logs -f web
```

Access Odoo:
- URL: http://localhost (NOT :8069, to enable websocket)
- Email: admin
- Password: admin
- Database: odoo

### 4. Configure PaaS Integration (if running PaaS Operator)

1. Login to Odoo: http://localhost
2. Go to **Settings → General Settings**
3. Scroll to **Woow PaaS** section
4. Configure:
   - PaaS Operator URL: `http://host.docker.internal:8000` (Docker Desktop) or `http://172.17.0.1:8000` (Linux)
   - API Key: (the key from .env file)
5. Click **Save**

> **Note**: Docker Desktop users should use `host.docker.internal`, Linux users should use the Docker bridge IP (`172.17.0.1`)

### 5. Verify Setup

1. Navigate to: http://localhost/woow
2. You should see the PaaS dashboard
3. Create a test workspace
4. If PaaS Operator is running, test the Marketplace feature

---

## Project Structure

Understanding the codebase structure:

```
woow_paas_platform/
├── src/                          # Odoo module source code
│   ├── __manifest__.py           # Module metadata, dependencies, assets
│   ├── __init__.py               # Python package init
│   │
│   ├── controllers/              # HTTP endpoints
│   │   ├── paas.py              # Main /woow endpoint & JSON API
│   │   └── cloud_services.py    # Cloud services API endpoints
│   │
│   ├── models/                   # Odoo ORM models
│   │   ├── res_config_settings.py       # PaaS settings
│   │   ├── workspace.py                 # Workspace model
│   │   ├── workspace_access.py          # Workspace members
│   │   ├── cloud_app_template.py        # Application templates
│   │   ├── cloud_service.py             # Deployed services
│   │   └── paas_operator_client.py      # HTTP client for operator
│   │
│   ├── views/                    # QWeb templates & XML views
│   │   ├── paas_app.xml         # SPA entry point template
│   │   ├── res_config_settings_views.xml
│   │   ├── workspace_views.xml
│   │   └── menu.xml
│   │
│   ├── security/                 # Access control
│   │   └── ir.model.access.csv  # Model permissions
│   │
│   ├── data/                     # Initial data
│   │   └── cloud_app_templates.xml      # Default app templates
│   │
│   ├── static/src/               # Frontend assets
│   │   ├── paas/                # Standalone OWL App
│   │   │   ├── app.js           # Mount entry point
│   │   │   ├── root.js/xml      # Root component + router
│   │   │   ├── core/            # Router, utils
│   │   │   ├── layout/          # AppShell, Sidebar, Header
│   │   │   ├── components/      # Reusable UI components
│   │   │   ├── pages/           # Page components
│   │   │   ├── services/        # API clients
│   │   │   └── styles/          # SCSS theme system
│   │   └── scss/                # Backend SCSS
│   │
│   └── tests/                    # Odoo Python tests
│       ├── test_workspace.py
│       ├── test_cloud_service.py
│       └── test_paas_operator_client.py
│
├── extra/paas-operator/          # PaaS Operator service (FastAPI)
│   ├── src/
│   │   ├── main.py              # FastAPI app + middleware
│   │   ├── config.py            # Settings management
│   │   ├── api/                 # API endpoints
│   │   │   ├── releases.py      # Helm release endpoints
│   │   │   └── namespaces.py    # Namespace management
│   │   ├── services/
│   │   │   └── helm.py          # Helm CLI wrapper
│   │   └── models/
│   │       └── schemas.py       # Pydantic models
│   │
│   ├── tests/                   # Operator tests
│   │   ├── test_helm.py
│   │   ├── test_releases.py
│   │   └── test_namespaces.py
│   │
│   ├── helm/                    # Helm chart for K8s deployment
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   └── templates/
│   │       ├── deployment.yaml
│   │       ├── service.yaml
│   │       ├── secret.yaml
│   │       ├── serviceaccount.yaml
│   │       ├── clusterrole.yaml
│   │       └── clusterrolebinding.yaml
│   │
│   ├── Dockerfile               # Container image
│   └── requirements.txt         # Python dependencies
│
├── docs/                        # Documentation
│   ├── deployment/              # Deployment guides
│   ├── development/             # Developer guides
│   └── spec/                    # Feature specifications
│
├── scripts/                     # Development scripts
│   ├── start-dev.sh            # Start development environment
│   ├── test-addon.sh           # Run Odoo tests
│   └── setup-worktree-env.sh   # Worktree configuration
│
├── docker-compose.yml           # Local Odoo development
└── CLAUDE.md                    # AI assistant context
```

### Key Components

#### Frontend (OWL)
- **Entry Point**: `src/static/src/paas/app.js`
- **Router**: `src/static/src/paas/core/router.js` (hash-based)
- **Services**: `src/static/src/paas/services/` (API clients)
- **Pages**: `src/static/src/paas/pages/` (main views)

#### Backend (Odoo)
- **Models**: `src/models/` (PostgreSQL ORM)
- **Controllers**: `src/controllers/` (HTTP endpoints)
- **Views**: `src/views/` (QWeb templates)

#### PaaS Operator (FastAPI)
- **API**: `extra/paas-operator/src/api/` (REST endpoints)
- **Services**: `extra/paas-operator/src/services/` (Helm wrapper)
- **Models**: `extra/paas-operator/src/models/` (Pydantic schemas)

---

## Running Tests

### Odoo Tests

```bash
# 使用 Worktree Development 腳本執行測試（推薦）
./scripts/test-addon.sh

# 或手動執行（需先啟動開發環境）
docker compose exec web \
  odoo -d ${POSTGRES_DB:-odoo} --test-enable --test-tags woow_paas_platform --stop-after-init

# Run specific test file
docker compose exec web \
  odoo -d ${POSTGRES_DB:-odoo} --test-enable --test-tags woow_paas_platform.test_workspace --stop-after-init

# Run with verbose output
docker compose exec web \
  odoo -d ${POSTGRES_DB:-odoo} --test-enable --test-tags woow_paas_platform --log-level=test --stop-after-init
```

### PaaS Operator Tests

```bash
cd extra/paas-operator

# Activate virtual environment
source venv/bin/activate

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=src --cov-report=html

# Run specific test file
pytest tests/test_helm.py -v

# Run specific test
pytest tests/test_releases.py::test_create_release -v
```

### Frontend Tests (Future)

Currently, the frontend doesn't have automated tests. To add them:

```bash
# Install dependencies
npm install --save-dev @odoo/owl-test-helpers jest

# Run tests (once implemented)
npm test
```

---

## Development Workflow

### Making Changes to Odoo Module

1. **Edit Python/JavaScript/SCSS files**
2. **Restart Odoo** (picks up Python changes):
   ```bash
   docker compose restart web
   ```
3. **Update module** (picks up XML/data changes):
   ```bash
   docker compose exec web \
     odoo -d ${POSTGRES_DB:-odoo} -u woow_paas_platform --stop-after-init
   ```
4. **Test changes**: http://localhost/woow
5. **Run tests**:
   ```bash
   ./scripts/test-addon.sh
   ```

### Making Changes to PaaS Operator

1. **Edit Python files** in `extra/paas-operator/src/`
2. **uvicorn auto-reloads** (if running with `--reload`)
3. **Test changes**: http://localhost:8000/docs
4. **Run tests**:
   ```bash
   cd extra/paas-operator
   pytest tests/ -v
   ```

### Making Changes to Frontend (OWL)

1. **Edit JavaScript/XML files** in `src/static/src/paas/`
2. **Hard refresh browser** (Ctrl+Shift+R / Cmd+Shift+R)
3. **No build step required** (Odoo handles asset bundling)
4. **Check browser console** for JavaScript errors

---

## Adding New Features

### Adding a New Odoo Model

1. **Create model file**: `src/models/my_model.py`
   ```python
   from odoo import models, fields, api

   class MyModel(models.Model):
       _name = 'woow_paas_platform.my_model'
       _description = 'My Model Description'

       name = fields.Char(string='Name', required=True)
       description = fields.Text(string='Description')
   ```

2. **Add import** in `src/models/__init__.py`:
   ```python
   from . import my_model
   ```

3. **Add security rules** in `src/security/ir.model.access.csv`:
   ```csv
   id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
   access_my_model_user,my_model_user,model_woow_paas_platform_my_model,base.group_user,1,1,1,1
   ```

4. **Create views** in `src/views/my_model_views.xml`

5. **Update manifest** `src/__manifest__.py`:
   ```python
   'data': [
       # ...
       'views/my_model_views.xml',
   ],
   ```

6. **Update module**:
   ```bash
   docker compose exec web odoo -d odoo -u woow_paas_platform --stop-after-init
   ```

### Adding a New OWL Component

1. **Create component file**: `src/static/src/paas/components/MyComponent.js`
   ```javascript
   /** @odoo-module **/

   import { Component } from "@odoo/owl";

   export class MyComponent extends Component {
       static template = "woow_paas_platform.MyComponent";
       static props = {
           title: String,
       };
   }
   ```

2. **Create template file**: `src/static/src/paas/components/MyComponent.xml`
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <templates xml:space="preserve">
       <t t-name="woow_paas_platform.MyComponent">
           <div class="my-component">
               <h2><t t-esc="props.title"/></h2>
           </div>
       </t>
   </templates>
   ```

3. **Register in manifest** `src/__manifest__.py`:
   ```python
   'woow_paas_platform.assets_paas': [
       # ...
       'woow_paas_platform/static/src/paas/components/MyComponent.js',
       'woow_paas_platform/static/src/paas/components/MyComponent.xml',
   ],
   ```

4. **Use in parent component**:
   ```javascript
   import { MyComponent } from "../components/MyComponent";

   export class ParentComponent extends Component {
       static components = { MyComponent };
   }
   ```

### Adding a New PaaS Operator Endpoint

1. **Define schema** in `extra/paas-operator/src/models/schemas.py`:
   ```python
   from pydantic import BaseModel

   class MyRequest(BaseModel):
       name: str
       value: int
   ```

2. **Create endpoint** in `extra/paas-operator/src/api/my_endpoint.py`:
   ```python
   from fastapi import APIRouter, Depends
   from ..models.schemas import MyRequest
   from ..config import get_api_key

   router = APIRouter()

   @router.post("/my-endpoint")
   async def my_endpoint(request: MyRequest, _: str = Depends(get_api_key)):
       # Implementation
       return {"status": "success"}
   ```

3. **Register router** in `extra/paas-operator/src/main.py`:
   ```python
   from .api import my_endpoint
   app.include_router(my_endpoint.router, prefix="/api", tags=["my"])
   ```

4. **Add tests** in `extra/paas-operator/tests/test_my_endpoint.py`

---

## Code Style Guidelines

### Python (Odoo)

Follow Odoo coding conventions:

```python
# Good
class WorkspaceService(models.Model):
    _name = 'woow_paas_platform.workspace_service'
    _description = 'Workspace Service'

    name = fields.Char(
        string='Name',
        required=True,
        help='Service display name',
    )

    def action_deploy(self):
        """Deploy the service to Kubernetes."""
        self.ensure_one()
        # Implementation
```

**Key Rules**:
- Use 4 spaces for indentation
- Max line length: 120 characters
- Use docstrings for all public methods
- Follow PEP 8 naming conventions

### Python (PaaS Operator)

Follow FastAPI best practices:

```python
# Good
from typing import Optional
from pydantic import BaseModel, Field

class ReleaseRequest(BaseModel):
    """Request model for creating a Helm release."""

    namespace: str = Field(..., pattern=r'^paas-ws-\d+$')
    name: str = Field(..., min_length=1, max_length=63)
    chart: str
    version: Optional[str] = None
```

**Key Rules**:
- Use type hints everywhere
- Use Pydantic for validation
- Add docstrings to all routes
- Use async/await consistently

### JavaScript/OWL

Follow Odoo OWL conventions:

```javascript
// Good
/** @odoo-module **/

import { Component } from "@odoo/owl";

export class ServiceCard extends Component {
    static template = "woow_paas_platform.ServiceCard";

    static props = {
        service: Object,
        onDeploy: Function,
    };

    setup() {
        // Component setup
    }

    handleDeploy() {
        this.props.onDeploy(this.props.service);
    }
}
```

**Key Rules**:
- Use `/** @odoo-module **/` at top of each file
- Define `static props` for all components
- Use camelCase for methods and variables
- Use PascalCase for component names

### SCSS

Follow BEM-like naming:

```scss
// Good
.o_woow_service_card {
    &__header {
        display: flex;
        justify-content: space-between;
    }

    &__title {
        font-size: 1.2rem;
        font-weight: 600;
    }

    &--deployed {
        border-color: var(--woow-color-success);
    }
}
```

**Key Rules**:
- Use `.o_woow_` prefix for isolation
- Use CSS variables for colors
- Use rem units for sizing
- Keep specificity low

---

## Debugging Tips

### Odoo Debugging

```python
# Add breakpoints
import pdb; pdb.set_trace()

# Log messages
import logging
_logger = logging.getLogger(__name__)
_logger.info("Debug message: %s", variable)

# View SQL queries
self.env.cr.execute("SELECT * FROM woow_paas_platform_workspace")
results = self.env.cr.dictfetchall()
_logger.info("Results: %s", results)
```

### PaaS Operator Debugging

```python
# Add breakpoints
import pdb; pdb.set_trace()

# Log messages
import logging
logger = logging.getLogger(__name__)
logger.debug(f"Processing request: {request}")

# Test with curl
curl -X POST http://localhost:8000/api/releases \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"namespace":"paas-ws-1","name":"test"}'
```

### Frontend Debugging

```javascript
// Browser console
console.log("Debug:", this.state);
debugger;  // Breakpoint

// OWL component tree
// Open browser DevTools → Components tab (with OWL extension)
```

---

## Next Steps

- Read [K8s Deployment Guide](../deployment/k8s-setup.md) to deploy to production
- Check [Troubleshooting Guide](../deployment/troubleshooting.md) for common issues
- Review [Cloud Services Spec](../spec/cloud-services.md) to understand the architecture
- Explore the codebase and start contributing!

## Getting Help

- **Documentation**: `docs/` directory
- **GitHub Issues**: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues
- **Odoo Docs**: https://www.odoo.com/documentation/18.0/
- **OWL Docs**: https://github.com/odoo/owl
- **FastAPI Docs**: https://fastapi.tiangolo.com/
