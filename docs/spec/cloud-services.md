# Cloud Services Specification

## Overview

Cloud Services 是 WoowTech PaaS 平台的核心功能之一，讓用戶能夠一鍵部署 Docker 容器化應用程式。用戶可以從 Application Marketplace 選擇預設應用（如 AnythingLLM、n8n、PostgreSQL 等），設定必要參數後即可快速啟動服務。

### Service Types

平台提供三種主要服務類型：

| Service Type | Description | Use Case |
|-------------|-------------|----------|
| **Cloud Services** | Deploy Docker apps with one click | AnythingLLM, n8n, PostgreSQL, Redis, etc. |
| **Security Access** | Zero Trust Tunnels via Podman/HAOS | Secure remote connections |
| **Smart Home Connect** | Home Assistant & Woow App integration | Remote access configuration |

本文件專注於 **Cloud Services** 的規格設計。

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
- docker_image: string
- default_port: integer
- required_env_vars: EnvVarSpec[]
- optional_env_vars: EnvVarSpec[]
- documentation_url: string
- min_resources: ResourceSpec
```

**EnvVarSpec**:
```yaml
- key: string
- label: string
- type: enum  # text, password, number, boolean
- default_value: string?
- placeholder: string?
- help_text: string?
- required: boolean
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
   - Application Name (required)
   - Reference ID (auto-generated, editable)

2. **Network & Domain**
   - Subdomain (e.g., `my-ai-assistant.houseoffoss.com`)
   - Private Network toggle (restrict to VPN only)

3. **Docker Variables** (dynamic based on app)
   - Required environment variables
   - Optional environment variables (Advanced toggle)

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
- Status Badge (Running, Stopped, Error, Deploying)
- Deployment ID
- Quick Actions:
  - "Open Web UI" button
  - Restart button
  - Stop button
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
  - Docker Tag + SHA
- **Live Traffic** chart (last 1 hour)
- **Support** links
- **Environment** info (Region, Instance Type)

#### Tab 3.2: Configuration
- General Settings (name, reference ID)
- Network & Domain
- Docker Variables (view/edit)
- Resource Allocation

#### Tab 3.3: Metrics
- **Performance Overview** (time range selector: Last Hour, 24h, 7d, 30d)
- vCPU Usage chart
- RAM Usage chart
- Disk I/O chart
- Network chart (IN/OUT)
- Active Connections count

#### Tab 3.4: Backups
- **Upload Volume for Deployment** (drag & drop, supports .tar.gz, .zip, .vol)
- **Current Backups** list with Restore action
- **Backup History** table (Date, Status, Size, Actions)
- Create Backup button

#### Tab 3.5: Activity Logs
- Search by keyword
- Filter by Type
- Date Range filter
- Export button
- **Recent Activity** list:
  - Application Started
  - Configuration Updated
  - Application Stopped
  - Service Alert: High Latency
- **Real-time Application Logs** panel:
  - Auto-scroll toggle
  - Stop Streaming button
  - Download Logs button
  - Log entries with timestamp, level ([INFO], [DEBUG], [ERROR]), message

---

### F4: Service Actions

**4.1 Start/Stop Service**
- Graceful shutdown with configurable timeout
- State persisted to database

**4.2 Restart Service**
- Rolling restart with health check

**4.3 Delete Service**
- Confirmation modal required
- Option to keep/delete backups
- Cleanup: container, volumes, domain records

**4.4 Edit Custom Domain**
- Modal with domain input
- DNS verification instructions
- SSL certificate auto-provisioning

---

## Data Models

### CloudAppTemplate (Application Catalog)

```python
class CloudAppTemplate(models.Model):
    _name = 'woow_paas_platform.cloud_app_template'
    _description = 'Cloud Application Template'

    name = fields.Char(required=True)
    slug = fields.Char(index=True)
    icon = fields.Binary()
    description = fields.Char()  # Short description
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
    tags = fields.Char()  # JSON array
    monthly_price = fields.Float()
    docker_image = fields.Char(required=True)
    default_port = fields.Integer(default=3000)
    env_var_specs = fields.Text()  # JSON schema
    documentation_url = fields.Char()
    min_vcpu = fields.Integer(default=1)
    min_ram_gb = fields.Float(default=1)
    min_storage_gb = fields.Integer(default=5)
    is_active = fields.Boolean(default=True)
```

### CloudService (Deployed Instance)

```python
class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _description = 'Cloud Service Instance'

    # Relationships
    workspace_id = fields.Many2one('woow_paas_platform.workspace', required=True, ondelete='cascade')
    template_id = fields.Many2one('woow_paas_platform.cloud_app_template', required=True)

    # Identity
    name = fields.Char(required=True)
    reference_id = fields.Char(index=True)  # e.g., "anything-llm-01"
    deployment_id = fields.Char()  # e.g., "#8291"

    # State
    state = fields.Selection([
        ('pending', 'Pending'),
        ('deploying', 'Deploying'),
        ('running', 'Running'),
        ('stopped', 'Stopped'),
        ('error', 'Error'),
    ], default='pending')

    # Network
    subdomain = fields.Char()
    custom_domain = fields.Char()
    internal_port = fields.Integer()
    is_private_network = fields.Boolean(default=False)

    # Configuration
    env_vars = fields.Text()  # JSON encrypted

    # Resources
    allocated_vcpu = fields.Integer()
    allocated_ram_gb = fields.Float()
    allocated_storage_gb = fields.Integer()

    # Infrastructure
    region = fields.Char(default='us-east-1')
    instance_type = fields.Char(default='t3.medium')
    container_id = fields.Char()
    docker_tag = fields.Char()
    docker_sha = fields.Char()

    # Timestamps
    deployed_at = fields.Datetime()
    last_started_at = fields.Datetime()
    last_stopped_at = fields.Datetime()
```

### CloudServiceBackup

```python
class CloudServiceBackup(models.Model):
    _name = 'woow_paas_platform.cloud_service_backup'
    _description = 'Cloud Service Backup'

    service_id = fields.Many2one('woow_paas_platform.cloud_service', required=True, ondelete='cascade')
    name = fields.Char()  # e.g., "backup_v221_full.tar.gz"
    size_bytes = fields.Integer()
    status = fields.Selection([
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ])
    backup_type = fields.Selection([
        ('manual', 'Manual'),
        ('scheduled', 'Scheduled'),
        ('pre_update', 'Pre-Update'),
    ])
    storage_path = fields.Char()
    created_by = fields.Many2one('res.users')
```

### CloudServiceLog

```python
class CloudServiceLog(models.Model):
    _name = 'woow_paas_platform.cloud_service_log'
    _description = 'Cloud Service Activity Log'

    service_id = fields.Many2one('woow_paas_platform.cloud_service', required=True, ondelete='cascade')
    event_type = fields.Selection([
        ('started', 'Application Started'),
        ('stopped', 'Application Stopped'),
        ('config_updated', 'Configuration Updated'),
        ('alert', 'Service Alert'),
        ('backup_created', 'Backup Created'),
        ('backup_restored', 'Backup Restored'),
    ])
    message = fields.Text()
    details = fields.Text()  # JSON
    initiated_by = fields.Many2one('res.users')
    logged_at = fields.Datetime(default=fields.Datetime.now)
```

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
       Body: { template_id, name, subdomain, env_vars, ... }

GET    /api/v1/workspaces/{workspace_id}/services/{service_id}
PATCH  /api/v1/workspaces/{workspace_id}/services/{service_id}
DELETE /api/v1/workspaces/{workspace_id}/services/{service_id}

POST   /api/v1/workspaces/{workspace_id}/services/{service_id}/start
POST   /api/v1/workspaces/{workspace_id}/services/{service_id}/stop
POST   /api/v1/workspaces/{workspace_id}/services/{service_id}/restart
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

#### Backups

```
GET    /api/v1/workspaces/{workspace_id}/services/{service_id}/backups
POST   /api/v1/workspaces/{workspace_id}/services/{service_id}/backups
POST   /api/v1/workspaces/{workspace_id}/services/{service_id}/backups/{backup_id}/restore
DELETE /api/v1/workspaces/{workspace_id}/services/{service_id}/backups/{backup_id}
```

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
| LogViewer | `components/logs/LogViewer.js` | 即時日誌查看器 |
| BackupList | `components/backup/BackupList.js` | 備份列表 |
| StatusBadge | `components/common/StatusBadge.js` | 狀態標籤 |

### New Modals

| Modal | Description |
|-------|-------------|
| EditDomainModal | 編輯自訂網域 |
| DeleteServiceModal | 確認刪除服務 |
| CreateBackupModal | 建立備份 |
| RestoreBackupModal | 還原備份確認 |

---

## Implementation Phases

### Phase 1: Foundation
- [ ] CloudAppTemplate model + seed data
- [ ] CloudService model
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

### Phase 5: Metrics & Logs
- [ ] Metrics tab + charts
- [ ] Activity Logs tab
- [ ] Real-time log streaming

### Phase 6: Backups
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

## Open Questions

1. **Docker orchestration**: 使用 Docker Swarm 還是 Kubernetes？
2. **Metrics collection**: 整合 Prometheus 還是自建方案？
3. **Log aggregation**: 整合 Loki/ELK 還是簡化方案？
4. **Billing integration**: 如何與帳單系統整合計費？

---

## References

- Design mockups: `resource/stitch_paas_web_app_shell_global_navigation_2026-01-16/`
- Existing workspace model: `src/models/workspace.py`
- Router implementation: `src/static/src/paas/core/router.js`
