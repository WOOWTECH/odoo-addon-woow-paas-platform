# Cloud Services Specification

## Overview

Cloud Services 是 WoowTech PaaS 平台的核心功能之一，讓用戶能夠一鍵部署容器化應用程式。用戶可以從 Application Marketplace 選擇預設應用（如 AnythingLLM、n8n、PostgreSQL 等），設定必要參數後即可快速啟動服務。

**部署架構**：使用 Kubernetes + Helm Charts 進行服務編排與部署。

### Service Types

平台提供三種主要服務類型：

| Service Type | Description | Use Case |
|-------------|-------------|----------|
| **Cloud Services** | Deploy containerized apps via Helm | AnythingLLM, n8n, PostgreSQL, Redis, etc. |
| **Security Access** | Zero Trust Tunnels via Podman/HAOS | Secure remote connections |
| **Smart Home Connect** | Home Assistant & Woow App integration | Remote access configuration |

本文件專注於 **Cloud Services** 的規格設計。

---

## Deployment Architecture

### System Architecture

由於 Odoo 運行在 K8s Pod 內，無法直接操作 Helm CLI，因此採用獨立的 **PaaS Operator** 服務架構：

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           K8s Cluster                                   │
│                                                                         │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐          │
│  │   Odoo Pod   │      │ PaaS Operator│      │ User Service │          │
│  │  (Frontend + │─────▶│   (FastAPI)  │─────▶│    Pods      │          │
│  │   Metadata)  │ HTTP │              │ Helm │              │          │
│  └──────────────┘      └──────────────┘      └──────────────┘          │
│         │                     │                                         │
│         ▼                     ▼                                         │
│  ┌──────────────┐      ┌──────────────┐                                │
│  │  PostgreSQL  │      │ ServiceAccount                                │
│  │  (Metadata)  │      │ + RBAC       │                                │
│  └──────────────┘      └──────────────┘                                │
└─────────────────────────────────────────────────────────────────────────┘
```

### PaaS Operator Service

獨立的 Python FastAPI 服務，負責執行所有 Helm 操作：

**技術棧**：
- Python 3.11+
- FastAPI
- Helm CLI (installed in container)
- kubectl (for status checks)

**Deployment**：
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paas-operator
  namespace: paas-system
spec:
  replicas: 1
  template:
    spec:
      serviceAccountName: paas-operator
      containers:
        - name: operator
          image: woowtech/paas-operator:latest
          ports:
            - containerPort: 8000
          env:
            - name: API_KEY
              valueFrom:
                secretKeyRef:
                  name: paas-operator-secrets
                  key: api-key
```

**ServiceAccount RBAC**：
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: paas-operator
rules:
  - apiGroups: [""]
    resources: ["namespaces", "secrets", "configmaps", "services", "persistentvolumeclaims"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

**API Endpoints**：

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/releases` | helm install |
| GET | `/api/releases/{namespace}/{name}` | Get release status |
| PATCH | `/api/releases/{namespace}/{name}` | helm upgrade |
| DELETE | `/api/releases/{namespace}/{name}` | helm uninstall |
| POST | `/api/releases/{namespace}/{name}/rollback` | helm rollback |
| GET | `/api/releases/{namespace}/{name}/revisions` | helm history |
| GET | `/api/releases/{namespace}/{name}/status` | Get pod/deployment status |
| POST | `/api/namespaces` | Create namespace with quota |

**Request/Response Example**：

```json
// POST /api/releases
{
  "namespace": "paas-ws-123",
  "release_name": "paas-ws-123-anythingllm-01",
  "chart": {
    "repo_url": "https://charts.bitnami.com/bitnami",
    "name": "postgresql",
    "version": "15.5.0"
  },
  "values": {
    "auth": {
      "postgresPassword": "secret123",
      "database": "app_db"
    }
  }
}

// Response
{
  "status": "deployed",
  "revision": 1,
  "namespace": "paas-ws-123",
  "release_name": "paas-ws-123-anythingllm-01"
}
```

**Security**：
- API Key authentication (Odoo ↔ Operator)
- Internal ClusterIP service (不對外暴露)
- RBAC 限制只能操作 `paas-ws-*` namespace
- YAML injection prevention using `yaml.safe_dump()`
- Error message sanitization (防止敏感資訊外洩)
- Fail-fast startup (Helm 不可用時拒絕啟動)
- Configurable CORS origins for cross-origin requests
- Request timeout handling (前端 30 秒超時)
- Optimistic locking for race condition prevention
- Helm values whitelist filtering based on template specs

### Namespace Strategy

每個 Workspace 對應一個 Kubernetes Namespace：

```
Namespace: paas-ws-{workspace_id}
├── Deployment: {release_name}-{app}
├── Service: {release_name}-{app}
├── Ingress: {release_name}-ingress
├── PVC: {release_name}-data
├── Secret: {release_name}-secrets
└── ConfigMap: {release_name}-config
```

### Helm Release Naming Convention

```
Release Name: paas-ws-{workspace_id}-{reference_id}
Example: paas-ws-123-anythingllm-01
```

這樣 Release Name 與 Namespace 有相同的前綴 `paas-ws-{workspace_id}`，方便識別和管理。

### Deployment Flow

```
1. User clicks "Launch Application"
           │
           ▼
2. Odoo API validates input & creates CloudService record (state=pending)
           │
           ▼
3. Odoo calls PaaS Operator API: POST /api/namespaces (if not exists)
           │
           ▼
4. Odoo calls PaaS Operator API: POST /api/releases
           │
           ▼
5. PaaS Operator executes: helm install {release} {chart} -n {namespace}
           │
           ▼
6. Odoo polls PaaS Operator API: GET /api/releases/{ns}/{name}/status
           │
           ▼
7. When ready, Odoo updates CloudService record (state=running)
```

### Supported Operations

| Operation | Helm Command | Description |
|-----------|--------------|-------------|
| Deploy | `helm install` | Create new release |
| Upgrade | `helm upgrade` | Update release (config changes, version upgrade) |
| Delete | `helm uninstall` | Remove release and cleanup resources |
| Rollback | `helm rollback {revision}` | Revert to previous revision |

> **Note**: 不提供 Start/Stop 操作。若需要暫停服務，請使用 Delete 移除；需要時再重新 Deploy。這樣設計是因為一個 Helm Chart 可能包含多種 workloads（Deployment、StatefulSet、CronJob 等），無法用單一 scale 指令控制。

### DNS & TLS Architecture (Cloudflare)

域名 `*.woowtech.com` 由 Cloudflare 管理，採用 **Cloudflare Proxy** 模式處理 TLS：

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│    User      │      │  Cloudflare  │      │   K3s Node   │      │   Service    │
│   Browser    │─────▶│  Proxy (CDN) │─────▶│   Traefik    │─────▶│    Pod       │
│              │ TLS  │              │ HTTP │              │      │              │
└──────────────┘      └──────────────┘  or  └──────────────┘      └──────────────┘
                                      Origin
                                       TLS
```

**Cloudflare 設定**：

| 設定項目 | 值 | 說明 |
|----------|-----|------|
| DNS Record | `*.woowtech.com` → K3s Node IP | Wildcard A record |
| Proxy Status | Proxied (Orange cloud) | 啟用 Cloudflare CDN + DDoS 保護 |
| SSL/TLS Mode | Full (Strict) | Cloudflare ↔ Origin 使用 TLS |
| Origin Certificate | Cloudflare Origin CA | 15 年有效期，免費 |

**流量路徑**：
1. User 訪問 `https://my-app.woowtech.com`
2. Cloudflare 終止 TLS，驗證憑證
3. Cloudflare 使用 Origin CA 與 K3s 建立加密連線
4. Traefik 路由到對應的 Service

### Ingress Configuration

使用 K3s 預設的 **Traefik** Ingress Controller：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: paas-ws-123-anythingllm-01
  namespace: paas-ws-123
  annotations:
    kubernetes.io/ingress.class: traefik
spec:
  tls:
    - hosts:
        - my-ai-assistant.woowtech.com
      secretName: cloudflare-origin-tls  # Shared Origin CA cert
  rules:
    - host: my-ai-assistant.woowtech.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: paas-ws-123-anythingllm-01
                port:
                  number: 3001
```

**Origin CA Secret（整個 cluster 共用）**：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-origin-tls
  namespace: default  # 或建立在每個 workspace namespace
type: kubernetes.io/tls
data:
  tls.crt: <base64-encoded-origin-cert>
  tls.key: <base64-encoded-origin-key>
```

> **Note**:
> - Cloudflare Origin CA 憑證為 wildcard (`*.woowtech.com`)，可供所有子網域共用
> - K3s 預設使用 Traefik 並開放 port 80/443
> - 如需自訂 Traefik 設定，應建立 HelmChartConfig 於 `/var/lib/rancher/k3s/server/manifests/`

### Resource Quotas

每個 Workspace namespace 設定資源配額：

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: workspace-quota
  namespace: paas-ws-123
spec:
  hard:
    requests.cpu: "8"
    requests.memory: 16Gi
    limits.cpu: "16"
    limits.memory: 32Gi
    persistentvolumeclaims: "10"
    requests.storage: 100Gi
```

---

## User Flows

### Flow 1: First Service Deployment (Empty Workspace)

```
Workspace Detail → Service Selection → Application Marketplace → App Configuration → Launch
```

1. 用戶進入 Workspace Detail Page
2. 顯示 "Get Started: Deploy Your First Service" 引導畫面
3. 用戶選擇 "Cloud Services" 服務卡片
4. 導向 Application Marketplace 頁面
5. 用戶搜尋/篩選並選擇應用程式
6. 進入 App Configuration 頁面
7. 設定應用名稱、subdomain、環境變數等
8. 點擊 "Launch Application" 開始部署

### Flow 2: Add Additional Service (Existing Services)

```
Workspace Dashboard → "+ Add Service" → Application Marketplace → App Configuration → Launch
```

### Flow 3: Manage Running Service

```
Workspace Dashboard → Service Card → Service Detail Page → [Overview | Configuration | Metrics | Backups | Activity Logs]
```

---

## Feature Specifications

### F1: Application Marketplace

**Purpose**: 提供可部署的應用程式目錄

**Functional Requirements**:

- FR1.1: 搜尋應用程式（by name, tags）
- FR1.2: 依分類篩選（All, AI & LLM, Automation, Database, Analytics, DevOps）
- FR1.3: 顯示應用卡片資訊（icon, name, description, tags, monthly_price）
- FR1.4: 點擊「+」按鈕進入配置頁面

**Application Card Data**:
```yaml
- id: string
- name: string
- icon_url: string
- description: string (short, ~100 chars)
- full_description: text
- tags: string[]  # e.g., ["AI", "Chatbot"]
- category: enum  # AI_LLM, Automation, Database, Analytics, DevOps, Web, Container
- monthly_price: decimal
- helm_chart: HelmChartSpec
- default_port: integer
- required_values: HelmValueSpec[]  # Helm values that user must provide
- optional_values: HelmValueSpec[]  # Optional Helm values with defaults
- documentation_url: string
- min_resources: ResourceSpec
```

**HelmChartSpec**:
```yaml
- repository: string      # e.g., "https://charts.bitnami.com/bitnami"
- chart_name: string      # e.g., "postgresql"
- chart_version: string   # e.g., "12.5.8"
- default_values: object  # Base values.yaml overrides
```

**HelmValueSpec**:
```yaml
- key: string            # Helm value path, e.g., "auth.postgresPassword"
- label: string          # UI display label
- type: enum             # text, password, number, boolean, select
- default_value: any?
- placeholder: string?
- help_text: string?
- required: boolean
- options: string[]?     # For select type
```


**ResourceSpec**:
```yaml
- vcpu: integer  # e.g., 2
- ram_gb: decimal  # e.g., 4
- storage_gb: integer  # e.g., 10
```

---

### F2: App Configuration & Launch

**Purpose**: 設定應用程式參數並啟動部署

**Configuration Sections**:

1. **General Settings**
   - Application Name (required, 可修改)
   - Reference ID (auto-generated, **建立後不可修改**，用於 Helm Release 命名)

2. **Network & Domain**
   - Subdomain (e.g., `my-ai-assistant.woowtech.com`)
   - ~~Private Network toggle (restrict to VPN only)~~ *(Future Phase)*

3. **Helm Values** (dynamic based on app template)
   - Required values (user must provide)
   - Optional values (Advanced toggle, with defaults)

4. **Resource Allocation** (future phase)
   - Instance Type selection
   - Storage allocation

**Deployment Note**:
> Initial setup may take up to 5 minutes. Your instance will be available at your custom subdomain.

**Actions**:
- Cancel / Discard
- Launch Application

**Estimated Monthly Cost**: 顯示於頁面底部

---

### F3: Service Detail Page

**Purpose**: 管理已部署的服務實例

**Header Section**:
- App Icon + Name (e.g., "AnythingLLM_221")
- Status Badge (Running, Error, Deploying, Upgrading)
- Deployment ID
- Quick Actions:
  - "Open Web UI" button
  - Rollback button (dropdown with revision history)
  - Delete button

**Tab Navigation**:

#### Tab 3.1: Overview
- **Connection**
  - Status (ONLINE/OFFLINE)
  - Public URL
  - Custom Domain (with Edit)
  - Internal Port
- **Resources**
  - CPU Usage (percentage + status)
  - RAM Usage (current / allocated)
  - Helm Release info (chart version, revision)
- **Live Traffic** chart (last 1 hour)
- **Support** links
- **Environment** info (Region, Instance Type)

#### Tab 3.2: Configuration
- General Settings (name 可改, reference ID 唯讀)
- Network & Domain (subdomain 唯讀, custom domain 可改)
- Helm Values (view/edit, 修改後觸發 `helm upgrade`)
- Resource Allocation

#### Tab 3.3: Metrics
- **Performance Overview** (time range selector: Last Hour, 24h, 7d, 30d)
- vCPU Usage chart
- RAM Usage chart
- Disk I/O chart
- Network chart (IN/OUT)
- Active Connections count

#### Tab 3.4: Backups *(Future Phase)*

#### Tab 3.5: Activity Logs *(Future Phase)*

---

### F4: Service Actions

**4.1 Upgrade Service**
- 更新 Helm values（環境變數、資源配置等）
- 升級 Chart 版本
- 自動建立新的 Helm revision

**4.2 Delete Service**
- Confirmation modal required
- Option to keep/delete backups (PVCs)
- Cleanup: `helm uninstall`, 移除 Ingress rules

**4.3 Rollback Service**
- 選擇要回滾的 revision
- 執行 `helm rollback`
- 顯示 revision history

**4.4 Edit Custom Domain**
- Modal with domain input
- 用戶需自行在其 DNS 設定 CNAME 指向 `{subdomain}.woowtech.com`
- 或直接使用平台提供的 `*.woowtech.com` 子網域（已含 TLS）

---

## Data Models

### CloudAppTemplate (Application Catalog)

```python
class CloudAppTemplate(models.Model):
    _name = 'woow_paas_platform.cloud_app_template'
    _description = 'Cloud Application Template'

    # Basic Info
    name = fields.Char(required=True)
    slug = fields.Char(index=True)
    icon = fields.Binary()
    description = fields.Char()  # Short description (~100 chars)
    full_description = fields.Text()
    category = fields.Selection([
        ('ai_llm', 'AI & LLM'),
        ('automation', 'Automation'),
        ('database', 'Database'),
        ('analytics', 'Analytics'),
        ('devops', 'DevOps'),
        ('web', 'Web'),
        ('container', 'Container'),
    ])
    tags = fields.Char()  # JSON array, e.g., '["AI", "Chatbot"]'
    monthly_price = fields.Float()
    documentation_url = fields.Char()

    # Helm Chart Configuration
    helm_repo_url = fields.Char(required=True)  # e.g., "https://charts.bitnami.com/bitnami"
    helm_chart_name = fields.Char(required=True)  # e.g., "postgresql"
    helm_chart_version = fields.Char(required=True)  # e.g., "12.5.8"
    helm_default_values = fields.Text()  # JSON: base values.yaml overrides
    helm_value_specs = fields.Text()  # JSON: schema for user-configurable values

    # Service Configuration
    default_port = fields.Integer(default=80)
    ingress_enabled = fields.Boolean(default=True)

    # Resource Requirements
    min_vcpu = fields.Integer(default=1)
    min_ram_gb = fields.Float(default=1)
    min_storage_gb = fields.Integer(default=5)

    is_active = fields.Boolean(default=True)
```

**helm_value_specs JSON Schema Example**:
```json
{
  "required": [
    {
      "key": "auth.postgresPassword",
      "label": "PostgreSQL Password",
      "type": "password",
      "required": true,
      "help_text": "Password for the postgres admin user"
    }
  ],
  "optional": [
    {
      "key": "primary.persistence.size",
      "label": "Storage Size",
      "type": "select",
      "default_value": "8Gi",
      "options": ["8Gi", "16Gi", "32Gi", "64Gi"]
    }
  ]
}
```

### CloudService (Deployed Instance)

```python
class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _description = 'Cloud Service Instance'

    _sql_constraints = [
        ('unique_subdomain', 'UNIQUE(subdomain)',
         'Subdomain must be unique across all services.'),
        ('unique_reference_id', 'UNIQUE(reference_id)',
         'Reference ID must be unique.'),
    ]

    # Relationships
    workspace_id = fields.Many2one('woow_paas_platform.workspace', required=True, ondelete='cascade')
    template_id = fields.Many2one('woow_paas_platform.cloud_app_template', required=True)

    # Identity
    name = fields.Char(required=True)  # Display name, can be changed
    reference_id = fields.Char(index=True, readonly=True)  # Immutable after creation, used in Helm release name
    deployment_id = fields.Char()  # Auto-generated sequence, e.g., "#8291"

    # State
    state = fields.Selection([
        ('pending', 'Pending'),
        ('deploying', 'Deploying'),
        ('running', 'Running'),
        ('error', 'Error'),
        ('upgrading', 'Upgrading'),
        ('deleting', 'Deleting'),
    ], default='pending')
    error_message = fields.Text()  # Error details when state='error'

    # Network
    subdomain = fields.Char()  # e.g., "my-app" → my-app.woowtech.com
    custom_domain = fields.Char()  # Optional custom domain
    internal_port = fields.Integer()
    # is_private_network = fields.Boolean(default=False)  # Future Phase

    # Helm Release Info
    helm_release_name = fields.Char()  # K8s release name, e.g., "paas-ws-123-myapp"
    helm_namespace = fields.Char()  # K8s namespace, e.g., "paas-ws-123"
    helm_values = fields.Text()  # JSON: merged values used for deployment
    helm_revision = fields.Integer(default=1)  # Current Helm revision number
    helm_chart_version = fields.Char()  # Deployed chart version

    # Resources
    allocated_vcpu = fields.Integer()
    allocated_ram_gb = fields.Float()
    allocated_storage_gb = fields.Integer()

    # Infrastructure
    k8s_cluster = fields.Char(default='default')  # Target K8s cluster
    region = fields.Char(default='us-east-1')

    # Timestamps
    deployed_at = fields.Datetime()
    last_upgraded_at = fields.Datetime()
```

### CloudServiceBackup *(Future Phase)*

### CloudServiceLog *(Future Phase)*

---

## API Design

### Endpoints

#### Application Templates

```
GET  /api/v1/cloud/templates
     ?category={category}
     &search={query}
     &tags={tag1,tag2}

GET  /api/v1/cloud/templates/{id}
```

#### Cloud Services

```
GET    /api/v1/workspaces/{workspace_id}/services
POST   /api/v1/workspaces/{workspace_id}/services
       Body: { template_id, name, subdomain, helm_values, ... }

GET    /api/v1/workspaces/{workspace_id}/services/{service_id}
PATCH  /api/v1/workspaces/{workspace_id}/services/{service_id}
       → Triggers helm upgrade
DELETE /api/v1/workspaces/{workspace_id}/services/{service_id}
       → Triggers helm uninstall

POST   /api/v1/workspaces/{workspace_id}/services/{service_id}/rollback
       Body: { revision: number }
GET    /api/v1/workspaces/{workspace_id}/services/{service_id}/revisions
       → List helm revision history
```

#### Metrics

```
GET  /api/v1/workspaces/{workspace_id}/services/{service_id}/metrics
     ?range={1h|24h|7d|30d}

GET  /api/v1/workspaces/{workspace_id}/services/{service_id}/logs
     ?type={event_type}
     &from={datetime}
     &to={datetime}
     &search={query}
```

#### Backups *(Future Phase)*

---

## UI Components

### New Pages

| Page | Route | Description |
|------|-------|-------------|
| ServiceSelectionPage | `#/workspaces/:id/services/new` | 服務類型選擇（首次部署引導） |
| AppMarketplacePage | `#/workspaces/:id/services/marketplace` | 應用程式市集 |
| AppConfigurationPage | `#/workspaces/:id/services/configure/:templateId` | 應用配置頁面 |
| ServiceDetailPage | `#/workspaces/:id/services/:serviceId` | 服務詳情（含五個 Tab） |

### New Components

| Component | Location | Description |
|-----------|----------|-------------|
| ServiceCard | `components/service/ServiceCard.js` | 服務卡片（顯示於 Dashboard） |
| AppCard | `components/marketplace/AppCard.js` | 應用程式卡片（市集用） |
| CategoryFilter | `components/marketplace/CategoryFilter.js` | 分類篩選器 |
| EnvVarForm | `components/config/EnvVarForm.js` | 動態環境變數表單 |
| MetricsChart | `components/metrics/MetricsChart.js` | 效能指標圖表 |
| ~~LogViewer~~ | ~~`components/logs/LogViewer.js`~~ | ~~即時日誌查看器~~ *(Future Phase)* |
| ~~BackupList~~ | ~~`components/backup/BackupList.js`~~ | ~~備份列表~~ *(Future Phase)* |
| StatusBadge | `components/common/StatusBadge.js` | 狀態標籤 |

### New Modals

| Modal | Description |
|-------|-------------|
| EditDomainModal | 編輯自訂網域 |
| DeleteServiceModal | 確認刪除服務 |
| ~~CreateBackupModal~~ | ~~建立備份~~ *(Future Phase)* |
| ~~RestoreBackupModal~~ | ~~還原備份確認~~ *(Future Phase)* |

---

## Implementation Phases

### Phase 0: PaaS Operator Service
- [ ] FastAPI project setup
- [ ] Helm CLI integration (subprocess)
- [ ] API endpoints: releases CRUD, namespaces
- [ ] API Key authentication
- [ ] Dockerfile + K8s manifests (Deployment, Service, RBAC)
- [ ] Health check endpoint

### Phase 1: Foundation (Odoo)
- [ ] CloudAppTemplate model + seed data
- [ ] CloudService model
- [ ] PaaS Operator client service (HTTP calls)
- [ ] Basic CRUD APIs

### Phase 2: Marketplace UI
- [ ] AppMarketplacePage
- [ ] AppCard component
- [ ] Category filter
- [ ] Search functionality

### Phase 3: Configuration & Launch
- [ ] AppConfigurationPage
- [ ] EnvVarForm component
- [ ] Service creation API
- [ ] Subdomain validation

### Phase 4: Service Management
- [ ] ServiceDetailPage (Overview tab)
- [ ] Service start/stop/restart APIs
- [ ] Status polling

### Phase 5: Metrics *(Future Phase)*
- [ ] Metrics tab + charts

### Phase 6: Activity Logs *(Future Phase)*
- [ ] Activity Logs tab
- [ ] Real-time log streaming

### Phase 7: Backups *(Future Phase)*
- [ ] Backup model
- [ ] Backup APIs
- [ ] Backups tab UI

---

## Non-Functional Requirements

### Performance
- Marketplace page load < 500ms
- Metrics chart update interval: 5s
- Log streaming latency < 1s

### Security
- Environment variables encrypted at rest
- HTTPS required for all public URLs
- Role-based access (workspace member roles)

### Scalability
- Support up to 50 services per workspace
- Support up to 30-day metrics retention

---

## Helm Chart Examples

### Example 1: PostgreSQL (Bitnami)

```yaml
# CloudAppTemplate seed data
name: PostgreSQL
slug: postgresql
helm_repo_url: https://charts.bitnami.com/bitnami
helm_chart_name: postgresql
helm_chart_version: "15.5.0"
helm_default_values: |
  {
    "primary": {
      "persistence": {
        "enabled": true,
        "size": "8Gi"
      }
    },
    "metrics": {
      "enabled": true
    }
  }
helm_value_specs: |
  {
    "required": [
      {
        "key": "auth.postgresPassword",
        "label": "Admin Password",
        "type": "password",
        "required": true
      },
      {
        "key": "auth.database",
        "label": "Database Name",
        "type": "text",
        "default_value": "app_db"
      }
    ],
    "optional": [
      {
        "key": "primary.persistence.size",
        "label": "Storage Size",
        "type": "select",
        "default_value": "8Gi",
        "options": ["8Gi", "16Gi", "32Gi"]
      }
    ]
  }
```

### Example 2: n8n (Custom/Community Chart)

```yaml
name: n8n Workflow
slug: n8n
helm_repo_url: https://8gears.github.io/n8n-helm-chart
helm_chart_name: n8n
helm_chart_version: "0.23.0"
helm_default_values: |
  {
    "n8n": {
      "encryption_key": "auto-generated"
    },
    "persistence": {
      "enabled": true,
      "size": "5Gi"
    }
  }
helm_value_specs: |
  {
    "required": [],
    "optional": [
      {
        "key": "n8n.basicAuth.active",
        "label": "Enable Basic Auth",
        "type": "boolean",
        "default_value": false
      },
      {
        "key": "n8n.basicAuth.user",
        "label": "Basic Auth Username",
        "type": "text"
      },
      {
        "key": "n8n.basicAuth.password",
        "label": "Basic Auth Password",
        "type": "password"
      }
    ]
  }
```

### Example 3: Redis (Bitnami)

```yaml
name: Redis
slug: redis
helm_repo_url: https://charts.bitnami.com/bitnami
helm_chart_name: redis
helm_chart_version: "19.0.0"
helm_default_values: |
  {
    "architecture": "standalone",
    "auth": {
      "enabled": true
    }
  }
helm_value_specs: |
  {
    "required": [
      {
        "key": "auth.password",
        "label": "Redis Password",
        "type": "password",
        "required": true
      }
    ],
    "optional": [
      {
        "key": "master.persistence.size",
        "label": "Storage Size",
        "type": "select",
        "default_value": "4Gi",
        "options": ["4Gi", "8Gi", "16Gi"]
      }
    ]
  }
```

---

## Technical Decisions

| 項目 | 決定 | 說明 |
|------|------|------|
| **K8s/Helm 操作** | 獨立 PaaS Operator 服務 | Odoo Pod 無法直接執行 Helm，需透過獨立服務 |
| **PaaS Operator 技術棧** | Python FastAPI + Helm CLI | FastAPI 輕量快速，subprocess 調用 Helm CLI |
| **Odoo ↔ Operator 通訊** | HTTP REST + API Key | Internal ClusterIP，不對外暴露 |
| **Helm Chart Repository** | 公開 repos + 自建 | 使用 Bitnami/ArtifactHub，未來可自建 Chart Museum |
| **Metrics collection** | Prometheus + Grafana | K8s metrics-server 不保留歷史，無法支援 24h/7d/30d 查詢 |
| **Log aggregation** | Loki + Promtail | 整合 Grafana 生態系 |
| **Billing integration** | *(Future Phase)* | 暫不實作 |
| **Multi-cluster support** | 不支援 | 單一 K8s cluster |

---

## References

- Design mockups: `resource/stitch_paas_web_app_shell_global_navigation_2026-01-16/`
- Existing workspace model: `src/models/workspace.py`
- Router implementation: `src/static/src/paas/core/router.js`
