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
- **Python 3.11+** - For PaaS Operator development
- **kubectl** 1.28+ + 一個 cluster context（本機 `docker-desktop` k8s，或 VPN 後的 `woow-k3s`） - 跑 Odoo dev sandbox
- **Helm 3.13+** - dev sandbox 用 Helm 部署
- **Node.js 18+** - For frontend development (OWL components)
- **Git** - Version control
- **`woowprd` plugin** - 提供 `/woowprd:k8s-dev-sandbox`（本機開發沙盒）

### Optional (for full stack development)
- **K3s/K3d** - Local Kubernetes cluster
- **PostgreSQL client** - Database debugging
- **curl/httpie** - API testing

### Verify Installation

```bash
# Check versions
python --version    # Should be 3.11+
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
# 主來源為 Gitea（github.com 上的 odoo-addon-woow-paas-platform 僅為鏡像）
git clone https://git-prod.woowtech.io/odoo-addons/woow_paas_platform.git
cd woow_paas_platform
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

### 3. Set Up Odoo（K8s Dev Sandbox）

本機開發走 `woowprd` plugin 的 K8s Dev Sandbox（取代舊 docker-compose 流程；詳見 [CLAUDE.md](../../CLAUDE.md) §K8s Dev Sandbox）。

```bash
# Return to project root
cd ../..

# 一次性 onboarding（記錄執行期 env；之後 create/up 可不帶 --context）
/woowprd:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" --context docker-desktop

# 建立 / 啟動當前 branch 的沙盒
/woowprd:k8s-dev-sandbox up --context docker-desktop

# 查看日誌
/woowprd:k8s-dev-sandbox logs <sandbox-name> --service odoo --follow
```

Access Odoo（`<slug>` 見 `up` / `list` 輸出，woow repo 為 `woow_<branch>` DB）:
```bash
kubectl port-forward -n dev-sandbox-<slug> svc/<slug>-odoo-dev-sandbox-nginx 8080:80
```
- URL: http://localhost:8080
- Email: admin
- Password: admin

### 4. PaaS Operator Integration

K8s Dev Sandbox 的 `post-create.sh` hook 會**自動探索 in-cluster 的 PaaS Operator**（`paas-operator.paas-system.svc.cluster.local`）並寫進 Odoo 設定，通常不需手動配置。

若要改接本機自跑的 Operator（`uvicorn ... --port 8000`），在 **Settings → Woow PaaS** 手動覆寫 URL / API Key（從 pod 連回宿主機的細節見 [CLAUDE.md](../../CLAUDE.md) §PaaS Operator Local Development）。

### 5. Verify Setup

1. Navigate to: http://localhost:8080/woow（先 port-forward）
2. You should see the PaaS dashboard
3. Create a test workspace
4. If PaaS Operator is running, test the Marketplace feature

---

## Project Structure

完整且權威的目錄結構單一維護於專案根的 `README.md` 與 `CLAUDE.md`「Directory Structure」章節，避免在此重複手寫而漂移。請以那兩處為準。以下只列開發最常碰到的幾個切入點：

- `src/controllers/` — HTTP / JSON API（共 10 個 controller，依範圍拆分；對照表見 `CLAUDE.md` 的「API Endpoints」controller 表，或直接 grep `@route`）
- `src/models/` — Odoo ORM models
- `src/static/src/paas/` — Standalone OWL App（`core/router.js` 為 hash-based router 入口）
- `extra/paas-operator/` — PaaS Operator（FastAPI 包 Helm CLI，含 helm chart）
- `docs/` — 文件，採 6 類混合結構（規則見 `docs/README.md`，非舊的 `spec/` `deployment/` `development/` 三分法）

### Key Components

#### Frontend (OWL)
- **Entry Point**: `src/static/src/paas/app.js`
- **Router**: `src/static/src/paas/core/router.js` (hash-based)
- **Services**: `src/static/src/paas/services/` (API clients)
- **Pages**: `src/static/src/paas/pages/` (main views)

#### Backend (Odoo)
- **Models**: `src/models/` (PostgreSQL ORM)
- **Controllers**: `src/controllers/` — 10 個 controller 依範圍拆分（非單一檔），對照表見 `CLAUDE.md`「API Endpoints」controller 表
- **Views**: `src/views/` (QWeb templates)

#### PaaS Operator (FastAPI)
- **API**: `extra/paas-operator/src/api/` (REST endpoints)
- **Services**: `extra/paas-operator/src/services/` (Helm wrapper)
- **Models**: `extra/paas-operator/src/models/` (Pydantic schemas)
- **完整角色**：Operator 不只包 Helm CLI，還負責 agent-runtime spawn 與 JWT signing 等職責；全貌見 [`docs/guides/paas-skill-system/README.md`](paas-skill-system/README.md)

---

## Running Tests

### Odoo Tests

```bash
# 在沙盒內跑模組測試（推薦）
/woowprd:k8s-dev-sandbox test <sandbox-name> --module woow_paas_platform

# 或手動在 Odoo pod 內執行（NS=dev-sandbox-<slug>、DB=woow_<branch>）
NS=dev-sandbox-<slug>; ODOO=$(kubectl get deploy -n $NS -o name | grep odoo | head -1)
kubectl exec -n $NS $ODOO -- \
  odoo -d woow_<branch> --test-enable --test-tags woow_paas_platform --stop-after-init

# 指定測試檔（清單見 src/tests/，<test_file> 換實際檔名）
kubectl exec -n $NS $ODOO -- \
  odoo -d woow_<branch> --test-enable --test-tags woow_paas_platform.<test_file> --stop-after-init

# verbose
kubectl exec -n $NS $ODOO -- \
  odoo -d woow_<branch> --test-enable --test-tags woow_paas_platform --log-level=test --stop-after-init
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

# Run specific test file（實際測試檔清單見 extra/paas-operator/tests/，勿在此硬列）
pytest tests/<test_file>.py -v

# Run specific test
pytest tests/<test_file>.py::<test_name> -v
```

### Frontend Tests (Future)

Currently, the frontend doesn't have automated tests. Odoo 18 的前端測試使用內建的 **Hoot** 測試框架（`@odoo/hoot`，隨 `web` 模組提供，**非**外部 npm 套件 / jest）。要加入時：

- 測試檔放 `static/tests/`，以 Hoot API 撰寫，並掛進 `web.assets_tests` bundle。
- 透過 Odoo 測試基礎設施執行（瀏覽器 `/web/tests`，或 `odoo-bin --test-enable --test-tags` 跑 QUnit/Hoot suite），不需 `npm install` / `npm test`。

---

## Development Workflow

### Making Changes to Odoo Module

local 模式以 hostPath 即時掛載 `src/`，故檔案編輯立即反映在 pod 內。
（`NS=dev-sandbox-<slug>; ODOO=$(kubectl get deploy -n $NS -o name | grep odoo | head -1)`）

1. **Edit Python/JavaScript/SCSS files**
2. **Restart Odoo**（Python 變更需重啟 pod）：
   ```bash
   kubectl rollout restart -n $NS $ODOO
   ```
3. **Update module**（XML/data 變更）：
   ```bash
   kubectl exec -n $NS $ODOO -- odoo -d woow_<branch> -u woow_paas_platform --stop-after-init
   ```
4. **Test changes**: http://localhost:8080/woow（先 port-forward）
5. **Run tests**:
   ```bash
   /woowprd:k8s-dev-sandbox test <sandbox-name> --module woow_paas_platform
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
   kubectl exec -n dev-sandbox-<slug> deploy/<slug>-odoo-dev-sandbox-odoo -- \
     odoo -d woow_<branch> -u woow_paas_platform --stop-after-init
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

4. **Add tests** in `extra/paas-operator/tests/`（既有測試檔可參考該目錄內容）

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

- Read [K8s Deployment Guide](deployment/k8s-setup.md) to deploy to production
- Check [Troubleshooting Guide](deployment/troubleshooting.md) for common issues
- Review [Cloud Services Spec](../reference/features/cloud-services.md) to understand the architecture
- Read [PaaS Skill System 知識庫](paas-skill-system/README.md) — agent-runtime / skill-bundle JWT / pod 等子系統的漸進式載入起點
- Explore the codebase and start contributing!

## Getting Help

- **Documentation**: `docs/` directory
- **Issue Tracker（主）**: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues （Gitea；github.com 上的 mirror issues 非主追蹤）
- **Odoo Docs**: https://www.odoo.com/documentation/18.0/
- **OWL Docs**: https://github.com/odoo/owl
- **FastAPI Docs**: https://fastapi.tiangolo.com/
