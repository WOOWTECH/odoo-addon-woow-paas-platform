# WoowTech PaaS Platform - API 串接規格文件

> 基於 Stitch Design System 設計稿分析
> 版本: 1.0.0
> 日期: 2026-01-22

---

## 目錄

1. [專案概述](#1-專案概述)
2. [系統架構](#2-系統架構)
3. [功能模組分析](#3-功能模組分析)
4. [API 端點規格](#4-api-端點規格)
5. [資料模型設計](#5-資料模型設計)
6. [開發優先順序](#6-開發優先順序)
7. [前後端分離架構建議](#7-前後端分離架構建議)

---

## 1. 專案概述

### 1.1 產品定位

WoowTech PaaS Platform 是一個多租戶雲端服務管理平台，提供：
- **Cloud Services**: 雲端應用程式部署與管理 (Odoo, n8n, PostgreSQL 等)
- **Smart Home Connect**: 智慧家庭設備遠端連線服務 (Home Assistant, Woow Hub)
- **Security Access**: 安全通道與 VPN 服務管理
- **Team Management**: 團隊成員與權限管理

### 1.2 技術堆疊

| 層級 | 技術選擇 |
|------|----------|
| 前端框架 | OWL (Odoo Web Library) + Tailwind CSS |
| 後端框架 | Odoo 18 (Python) |
| 資料庫 | PostgreSQL |
| 認證機制 | Odoo Session-based (內建) |
| API 風格 | JSON-RPC (Odoo 標準) |

---

## 2. 系統架構

### 2.1 前後端分離架構圖

```
┌─────────────────────────────────────────────────────────────────┐
│                        Frontend (OWL App)                       │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐           │
│  │Dashboard│  │Workspace│  │ Billing │  │Settings │           │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘           │
│       └────────────┴────────────┴────────────┘                 │
│                         │ HTTP/JSON                            │
└─────────────────────────┼──────────────────────────────────────┘
                          │
┌─────────────────────────┼──────────────────────────────────────┐
│                    API Gateway Layer                           │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │  /paas/api/v1/*  - Authentication, Rate Limiting, CORS        │ │
│  └──────────────────────────────────────────────────────────┘ │
└─────────────────────────┼──────────────────────────────────────┘
                          │
┌─────────────────────────┼──────────────────────────────────────┐
│                   Backend Services (Odoo)                      │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐           │
│  │  Auth   │  │Workspace│  │ Billing │  │  Team   │           │
│  │ Service │  │ Service │  │ Service │  │ Service │           │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘           │
│       └────────────┴────────────┴────────────┘                 │
│                         │                                      │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              PostgreSQL Database                         │ │
│  └──────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 API 基礎路徑

```
Base URL: /paas/api/v1
Authentication: Odoo Session Cookie (自動處理)
Content-Type: application/json
Request Type: JSON-RPC (Odoo 標準)
```

**Controller 類型說明:**
| type 參數 | Content-Type | 適用情境 |
|-----------|-------------|----------|
| `'json'` | application/json | API 端點 (推薦) |
| `'http'` | form/multipart | 檔案上傳、傳統表單 |

---

## 3. 功能模組分析

根據設計稿分析，系統包含以下核心功能模組：

### 3.1 Dashboard 模組
- 總覽統計卡片 (Members, Billing, Workspace)
- 最近活動時間軸
- 快速存取區塊
- 系統狀態指標

### 3.2 Workspace 模組
| 子功能 | 說明 |
|--------|------|
| Workspace List | 工作區清單管理 |
| Cloud Services | 雲端應用部署 (Marketplace) |
| Smart Home Connect | 智慧家庭 Hub 連線 |
| Security Access | 安全通道管理 |
| App Configuration | 應用程式配置與啟動 |
| App Metrics | 效能監控儀表板 |

### 3.3 Billing 模組
| 子功能 | 說明 |
|--------|------|
| Credits Overview | 餘額與用量統計 |
| Add Funds | 儲值功能 |
| Payment Methods | 付款方式管理 |
| Invoice History | 發票歷史記錄 |
| Auto-reload | 自動儲值設定 |

### 3.4 Settings 模組
| 子功能 | 說明 |
|--------|------|
| User Profile | 個人資料與頭像 |
| Team Management | 團隊成員管理 |
| Notification Preferences | 通知偏好設定 |
| System Logs | 系統活動日誌 |

---

## 4. API 端點規格

### 4.1 認證機制 (使用 Odoo 內建)

> **Note**: 本系統使用 Odoo 原生的 Session-based 認證機制，不需額外實作 JWT。

#### 認證方式

**1. Session 認證 (Web 前端)**
```yaml
# Odoo 原生登入端點
POST /web/session/authenticate
Content-Type: application/json
Request:
  jsonrpc: "2.0"
  method: "call"
  params:
    db: "database_name"
    login: "user@example.com"
    password: "password"
Response:
  - 成功後會設置 session cookie
  - 後續請求自動帶入 cookie 進行認證
```

**2. Controller 認證裝飾器**
```python
from odoo import http

class PaaSController(http.Controller):

    # 需要登入才能存取
    @http.route('/paas/api/v1/workspaces', type='json', auth='user')
    def get_workspaces(self):
        # request.env.user 可取得當前用戶
        pass

    # 公開存取 (不需登入)
    @http.route('/paas/api/v1/marketplace/apps', type='json', auth='public')
    def get_marketplace(self):
        pass
```

**3. 認證類型說明**
| auth 參數 | 說明 |
|-----------|------|
| `'user'` | 必須登入，否則回傳 401 |
| `'public'` | 公開存取，可選擇性登入 |
| `'none'` | 無認證檢查 |

#### 用戶資訊 API

```yaml
# 取得當前用戶資訊
GET /paas/api/v1/user/me
Auth: user (需登入)
Response (success):
  success: true
  data:
    id: number
    name: string
    email: string
    avatar_url: string
    plan: "free" | "pro" | "enterprise"
    credits_balance: number
    role: "owner" | "admin" | "user" | "guest"
```

### 4.2 Dashboard API

```yaml
# 取得儀表板統計
GET /paas/api/v1/dashboard/stats
Response (success):
  success: true
  data:
    members:
      total: number
      active: number
      pending_invites: number
    billing:
      month_usage: number
      credits_left: number
      next_bill_date: string
    workspace:
      cloud_services: { used: number, limit: number }
      secure_tunnels: { used: number, limit: number }
      workspaces: { used: number, limit: number }

# 取得最近活動
GET /paas/api/v1/dashboard/activities
Query:
  - limit: number (default: 10)
  - offset: number (default: 0)
Response (success):
  success: true
  data:
    items:
      - id: number
        action: string
        user: { id: number, name: string, avatar_url: string }
        target: string
        timestamp: string
    total: number
```

### 4.3 Workspace API

```yaml
# 工作區清單
GET /paas/api/v1/workspaces
Query:
  - page: number
  - per_page: number
  - search: string
Response (success):
  success: true
  data:
    items:
      - id: number
        name: string
        description: string
        created_at: string
        updated_at: string
        apps_count: number
        status: "active" | "inactive"
    total: number
    page: number
    per_page: number

# 建立工作區
POST /paas/api/v1/workspaces
Request:
  - name: string (required)
  - description: string
Response (success):
  success: true
  data:
    id: number
    name: string
    description: string
    created_at: string
    message: "Workspace created successfully"

# 取得單一工作區
GET /paas/api/v1/workspaces/{workspace_id}
Response (success):
  success: true
  data:
    id: number
    name: string
    description: string
    apps: App[]
    stats: WorkspaceStats

# 更新工作區
PUT /paas/api/v1/workspaces/{workspace_id}
Request:
  - name: string
  - description: string
Response (success):
  success: true
  data:
    id: number
    name: string
    description: string
    message: "Workspace updated successfully"

# 刪除工作區
DELETE /paas/api/v1/workspaces/{workspace_id}
Response (success):
  success: true
  data:
    message: "Workspace deleted successfully"
```

### 4.4 Cloud Application API

```yaml
# 應用程式市集
GET /paas/api/v1/marketplace/apps
Query:
  - category: "ai" | "automation" | "database" | "analytics" | "devops" | "cms"
  - search: string
Response (success):
  success: true
  data:
    items:
      - id: string
        name: string
        description: string
        icon_url: string
        categories: string[]
        monthly_price: number
        specs: { cpu: string, ram: string, storage: string }

# 部署應用程式
POST /paas/api/v1/workspaces/{workspace_id}/apps
Request:
  - app_template_id: string
  - name: string
  - config: object
Response (success):
  success: true
  data:
    id: number
    name: string
    status: "deploying" | "running" | "stopped" | "error"
    public_url: string
    deployment_id: string
    message: "App deployment started"

# 取得已部署應用程式
GET /paas/api/v1/workspaces/{workspace_id}/apps
Response (success):
  success: true
  data:
    items:
      - id: number
        name: string
        template: AppTemplate
        status: string
        public_url: string
        created_at: string
        metrics: { cpu: number, ram: number, storage: number }

# 應用程式詳情
GET /paas/api/v1/workspaces/{workspace_id}/apps/{app_id}
Response (success):
  success: true
  data:
    id: number
    name: string
    status: string
    public_url: string
    deployment_id: string
    config: object
    volumes: Volume[]
    metrics: AppMetrics

# 應用程式效能指標
GET /paas/api/v1/workspaces/{workspace_id}/apps/{app_id}/metrics
Query:
  - period: "1h" | "24h" | "7d" | "30d"
Response (success):
  success: true
  data:
    cpu: TimeSeriesData[]
    ram: TimeSeriesData[]
    disk_io: { read: TimeSeriesData[], write: TimeSeriesData[] }
    network: { in: TimeSeriesData[], out: TimeSeriesData[] }
    connections: TimeSeriesData[]

# 應用程式操作
POST /paas/api/v1/workspaces/{workspace_id}/apps/{app_id}/actions
Request:
  - action: "start" | "stop" | "restart" | "delete"
Response (success):
  success: true
  data:
    message: "App {action} completed successfully"

# 設定自訂網域
PUT /paas/api/v1/workspaces/{workspace_id}/apps/{app_id}/domain
Request:
  - domain: string (e.g., "app.example.com")
  - ssl_enabled: boolean
Response (success):
  success: true
  data:
    domain: string
    verification_record: string
    ssl_status: "pending" | "active"
    status: "pending" | "verified" | "active"
    message: "Domain configuration saved"

# 取得網域驗證狀態
GET /paas/api/v1/workspaces/{workspace_id}/apps/{app_id}/domain
Response (success):
  success: true
  data:
    domain: string
    verification_record: string
    ssl_status: "pending" | "active"
    status: "pending" | "verified" | "active"

# 刪除自訂網域
DELETE /paas/api/v1/workspaces/{workspace_id}/apps/{app_id}/domain
Response (success):
  success: true
  data:
    message: "Domain removed"
```

### 4.5 Smart Home Connect API

```yaml
# 取得 Hub 清單
GET /paas/api/v1/workspaces/{workspace_id}/hubs
Response (success):
  success: true
  data:
    items:
      - id: number
        name: string
        type: "haos" | "woow-hub-os"
        status: "online" | "offline"
        public_url: string
        volume_size: string
        last_backup: string

# 連接新 Hub
POST /paas/api/v1/workspaces/{workspace_id}/hubs
Request:
  - name: string
  - type: "haos" | "woow-hub-os"
Response (success):
  success: true
  data:
    id: number
    tunnel_token: string
    setup_instructions: string
    message: "Hub created successfully"

# Hub 詳情
GET /paas/api/v1/workspaces/{workspace_id}/hubs/{hub_id}
Response (success):
  success: true
  data:
    id: number
    name: string
    type: string
    status: string
    public_url: string
    proxy_targets: ProxyTarget[]
    volumes: Volume[]
    api_tokens: ApiToken[]

# 更新 Hub 配置
PUT /paas/api/v1/workspaces/{workspace_id}/hubs/{hub_id}
Request:
  - name: string
  - proxy_targets: ProxyTarget[]
Response (success):
  success: true
  data:
    message: "Hub updated successfully"

# Hub 備份操作
POST /paas/api/v1/workspaces/{workspace_id}/hubs/{hub_id}/backup
Response (success):
  success: true
  data:
    backup_id: string
    status: "pending" | "in_progress" | "completed"
    message: "Backup started"

# Hub 還原操作
POST /paas/api/v1/workspaces/{workspace_id}/hubs/{hub_id}/restore
Request:
  - backup_id: string
Response (success):
  success: true
  data:
    message: "Restore started"
```

### 4.6 Security Access API

```yaml
# 取得安全通道清單
GET /paas/api/v1/workspaces/{workspace_id}/tunnels
Response (success):
  success: true
  data:
    items:
      - id: number
        name: string
        type: "podman" | "docker" | "vm"
        status: "online" | "offline"
        public_url: string

# 建立安全通道
POST /paas/api/v1/workspaces/{workspace_id}/tunnels
Request:
  - name: string
  - type: string
Response (success):
  success: true
  data:
    id: number
    tunnel_token: string
    public_url: string
    message: "Tunnel created successfully"

# 通道詳情
GET /paas/api/v1/workspaces/{workspace_id}/tunnels/{tunnel_id}
Response (success):
  success: true
  data:
    id: number
    name: string
    type: string
    status: string
    public_url: string
    proxy_targets: ProxyTarget[]
    tunnel_token: string (masked)
    volumes: Volume[]
    api_tokens: ApiToken[]

# 更新通道配置
PUT /paas/api/v1/workspaces/{workspace_id}/tunnels/{tunnel_id}
Request:
  - proxy_targets: ProxyTarget[]
Response (success):
  success: true
  data:
    message: "Tunnel updated successfully"

# 重新產生 Tunnel Token
POST /paas/api/v1/workspaces/{workspace_id}/tunnels/{tunnel_id}/regenerate-token
Response (success):
  success: true
  data:
    tunnel_token: string
    message: "Token regenerated successfully"

# 取得部署腳本
GET /paas/api/v1/workspaces/{workspace_id}/tunnels/{tunnel_id}/script
Query:
  - platform: "linux" | "macos" | "windows" | "docker"
Response (success):
  success: true
  data:
    script: string
    platform: string
    instructions: string
    tunnel_token: string (masked)
```

### 4.7 Billing API

```yaml
# 取得帳單概覽
GET /paas/api/v1/billing/overview
Response (success):
  success: true
  data:
    credits_balance: number
    current_usage: number
    estimated_total: number
    auto_reload: { enabled: boolean, threshold: number, amount: number }
    default_payment_method: PaymentMethod

# 取得用量明細
GET /paas/api/v1/billing/usage
Query:
  - start_date: string
  - end_date: string
Response (success):
  success: true
  data:
    items:
      - date: string
        service: string
        workspace: string
        amount: number
        description: string
    total: number

# 取得發票清單
GET /paas/api/v1/billing/invoices
Query:
  - page: number
  - per_page: number
Response (success):
  success: true
  data:
    items:
      - id: string
        invoice_number: string
        date: string
        amount: number
        status: "paid" | "processing" | "failed"
        download_url: string
    total: number

# 下載發票
GET /paas/api/v1/billing/invoices/{invoice_id}/download
Response:
  - Content-Type: application/pdf
  - Content-Disposition: attachment

# 儲值 (Add Funds)
POST /paas/api/v1/billing/add-funds
Request:
  - amount: number
  - payment_method_id: string
Response (success):
  success: true
  data:
    transaction_id: string
    new_balance: number
    status: "success" | "pending" | "failed"
    message: "Funds added successfully"

# 付款方式清單
GET /paas/api/v1/billing/payment-methods
Response (success):
  success: true
  data:
    items:
      - id: string
        type: "visa" | "mastercard" | "amex"
        last_four: string
        expiry: string
        is_default: boolean

# 新增付款方式
POST /paas/api/v1/billing/payment-methods
Request:
  - card_number: string
  - expiry_month: string
  - expiry_year: string
  - cvc: string
  - set_as_default: boolean
Response (success):
  success: true
  data:
    id: string
    type: string
    last_four: string
    message: "Payment method added successfully"

# 設定預設付款方式
PUT /paas/api/v1/billing/payment-methods/{payment_method_id}/default
Response (success):
  success: true
  data:
    message: "Default payment method updated"

# 刪除付款方式
DELETE /paas/api/v1/billing/payment-methods/{payment_method_id}
Response (success):
  success: true
  data:
    message: "Payment method deleted"

# 設定自動儲值
PUT /paas/api/v1/billing/auto-reload
Request:
  - enabled: boolean
  - threshold: number
  - amount: number
Response (success):
  success: true
  data:
    message: "Auto-reload settings updated"
```

### 4.8 Team Management API

```yaml
# 取得團隊成員清單
GET /paas/api/v1/team/members
Query:
  - page: number
  - per_page: number
  - role: "owner" | "admin" | "user" | "guest"
  - status: "active" | "pending" | "offline"
Response (success):
  success: true
  data:
    items:
      - id: number
        name: string
        email: string
        avatar_url: string
        role: string
        status: "active" | "pending" | "offline"
        workspace_access: string[]
    total: number

# 邀請成員
POST /paas/api/v1/team/invite
Request:
  - email: string
  - role: "admin" | "user" | "guest"
  - workspace_ids: number[]
Response (success):
  success: true
  data:
    invitation_id: string
    status: "sent"
    message: "Invitation sent successfully"

# 重新發送邀請
POST /paas/api/v1/team/invite/{invitation_id}/resend
Response (success):
  success: true
  data:
    message: "Invitation resent"

# 取消邀請
DELETE /paas/api/v1/team/invite/{invitation_id}
Response (success):
  success: true
  data:
    message: "Invitation cancelled"

# 更新成員權限
PUT /paas/api/v1/team/members/{member_id}
Request:
  - role: string
  - workspace_access: { workspace_id: number, enabled: boolean }[]
Response (success):
  success: true
  data:
    message: "Member updated successfully"

# 移除成員
DELETE /paas/api/v1/team/members/{member_id}
Response (success):
  success: true
  data:
    message: "Member removed"
```

### 4.9 User Settings API

```yaml
# 取得用戶設定
GET /paas/api/v1/settings/profile
Response (success):
  success: true
  data:
    id: number
    name: string
    email: string
    avatar_url: string
    two_factor_enabled: boolean

# 更新用戶資料
PUT /paas/api/v1/settings/profile
Request:
  - name: string
  - avatar: File (multipart)
Response (success):
  success: true
  data:
    avatar_url: string
    message: "Profile updated successfully"

# 變更密碼
PUT /paas/api/v1/settings/password
Request:
  - current_password: string
  - new_password: string
Response (success):
  success: true
  data:
    message: "Password changed successfully"

# 取得通知設定
GET /paas/api/v1/settings/notifications
Response (success):
  success: true
  data:
    email_alerts: boolean
    push_notifications: boolean
    billing_alerts: boolean
    security_alerts: boolean
    weekly_report: boolean

# 更新通知設定
PUT /paas/api/v1/settings/notifications
Request:
  - email_alerts: boolean
  - push_notifications: boolean
  - billing_alerts: boolean
  - security_alerts: boolean
  - weekly_report: boolean
Response (success):
  success: true
  data:
    message: "Notification settings updated"
```

---

## 5. 資料模型設計

### 5.1 核心實體關係圖 (ERD)

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│    User     │────<│  TeamMember │>────│   Team      │
├─────────────┤     ├─────────────┤     ├─────────────┤
│ id          │     │ id          │     │ id          │
│ name        │     │ user_id     │     │ name        │
│ email       │     │ team_id     │     │ plan        │
│ password    │     │ role        │     │ credits     │
│ avatar_url  │     │ status      │     │ created_at  │
│ plan        │     │ created_at  │     └──────┬──────┘
└─────────────┘     └─────────────┘            │
                                               │
┌─────────────┐     ┌─────────────┐     ┌──────┴──────┐
│  AppTemplate│     │  CloudApp   │>────│  Workspace  │
├─────────────┤     ├─────────────┤     ├─────────────┤
│ id          │────<│ id          │     │ id          │
│ name        │     │ template_id │     │ name        │
│ category    │     │ workspace_id│     │ team_id     │
│ description │     │ name        │     │ description │
│ price       │     │ status      │     │ created_at  │
│ specs       │     │ public_url  │     └──────┬──────┘
└─────────────┘     │ config      │            │
                    └─────────────┘            │
                                               │
┌─────────────┐     ┌─────────────┐     ┌──────┴──────┐
│  HubConnect │>────│   Tunnel    │>────│  Workspace  │
├─────────────┤     ├─────────────┤     └─────────────┘
│ id          │     │ id          │
│ workspace_id│     │ workspace_id│
│ name        │     │ name        │
│ type        │     │ type        │
│ status      │     │ public_url  │
│ public_url  │     │ tunnel_token│
│ tunnel_token│     │ status      │
└─────────────┘     └─────────────┘

┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Invoice    │────<│ Transaction │>────│PaymentMethod│
├─────────────┤     ├─────────────┤     ├─────────────┤
│ id          │     │ id          │     │ id          │
│ team_id     │     │ team_id     │     │ team_id     │
│ invoice_num │     │ type        │     │ type        │
│ amount      │     │ amount      │     │ last_four   │
│ status      │     │ status      │     │ expiry      │
│ date        │     │ created_at  │     │ is_default  │
└─────────────┘     └─────────────┘     └─────────────┘
```

### 5.2 Odoo Model 定義

```python
# models/workspace.py
class WoowWorkspace(models.Model):
    _name = 'woow.workspace'
    _description = 'PaaS Workspace'

    name = fields.Char(required=True)
    description = fields.Text()
    team_id = fields.Many2one('woow.team', required=True)
    status = fields.Selection([
        ('active', 'Active'),
        ('inactive', 'Inactive'),
    ], default='active')
    app_ids = fields.One2many('woow.cloud.app', 'workspace_id')
    hub_ids = fields.One2many('woow.hub.connect', 'workspace_id')
    tunnel_ids = fields.One2many('woow.tunnel', 'workspace_id')

# models/cloud_app.py
class WoowCloudApp(models.Model):
    _name = 'woow.cloud.app'
    _description = 'Deployed Cloud Application'

    name = fields.Char(required=True)
    workspace_id = fields.Many2one('woow.workspace', required=True)
    template_id = fields.Many2one('woow.app.template', required=True)
    status = fields.Selection([
        ('deploying', 'Deploying'),
        ('running', 'Running'),
        ('stopped', 'Stopped'),
        ('error', 'Error'),
    ])
    public_url = fields.Char()
    deployment_id = fields.Char()
    config = fields.Json()
```

---

## 6. 開發優先順序

### 6.1 優先順序矩陣

根據 **業務價值 (Business Value)** 與 **技術複雜度 (Technical Complexity)** 分析：

| 優先級 | 模組 | 業務價值 | 複雜度 | 依賴性 |
|--------|------|----------|--------|--------|
| 🔴 P0 | Authentication | 高 | 低 | 無 |
| 🔴 P0 | User Profile | 高 | 低 | Auth |
| 🟠 P1 | Workspace CRUD | 高 | 中 | Auth |
| 🟠 P1 | Dashboard Overview | 高 | 中 | Auth, Workspace |
| 🟡 P2 | Billing Overview | 高 | 中 | Auth |
| 🟡 P2 | Team Management | 中 | 中 | Auth |
| 🟢 P3 | Cloud App Marketplace | 高 | 高 | Workspace |
| 🟢 P3 | Cloud App Deployment | 高 | 高 | Marketplace |
| 🔵 P4 | Smart Home Connect | 中 | 高 | Workspace |
| 🔵 P4 | Security Access | 中 | 高 | Workspace |
| 🔵 P4 | App Metrics | 中 | 高 | Cloud App |
| ⚪ P5 | Payment Integration | 高 | 高 | Billing |
| ⚪ P5 | Notification System | 低 | 中 | Auth |

### 6.2 開發階段規劃

```
Phase 1: Foundation (2-3 週)
├── Authentication API
├── User Profile API
├── Session Management
└── API Error Handling Framework

Phase 2: Core Features (3-4 週)
├── Workspace CRUD
├── Dashboard Statistics
├── Team Member CRUD
└── Basic Billing Overview

Phase 3: Service Deployment (4-5 週)
├── App Marketplace Catalog
├── Cloud App Deployment Engine
├── App Status & Lifecycle
└── Basic Metrics Collection

Phase 4: Advanced Features (3-4 週)
├── Smart Home Hub Connect
├── Security Tunnel Management
├── Volume & Backup Management
└── Advanced Metrics Dashboard

Phase 5: Payment & Polish (2-3 週)
├── Payment Gateway Integration (Stripe)
├── Invoice Generation
├── Notification System
└── UI/UX Refinements
```

### 6.3 每階段 MVP 定義

#### Phase 1 MVP
- 用戶可以登入/登出
- 用戶可以查看自己的 Profile
- API Token 認證機制運作正常

#### Phase 2 MVP
- 用戶可以建立/編輯/刪除 Workspace
- Dashboard 顯示基本統計
- 可以邀請團隊成員

#### Phase 3 MVP
- 顯示應用程式市集
- 可以部署一個應用程式
- 可以啟動/停止/重啟應用程式

#### Phase 4 MVP
- 可以連接 Home Assistant Hub
- 可以建立安全通道
- 顯示應用程式效能指標

#### Phase 5 MVP
- 可以新增信用卡
- 可以儲值 Credits
- 自動扣款機制運作

---

## 7. 前後端分離架構建議

### 7.1 API 設計原則

1. **RESTful 設計**
   - 使用 HTTP 動詞 (GET, POST, PUT, DELETE)
   - 資源導向的 URL 設計
   - 適當使用 HTTP 狀態碼

2. **統一回應格式 (MANDATORY)**

所有 API 回應 **必須** 遵循以下標準格式，資料一律放在 `data` 欄位中：

```json
// Success Response
{
  "success": true,
  "data": {
    // 實際回傳資料放這裡
  }
}

// Success Response with pagination
{
  "success": true,
  "data": {
    "items": [...],
    "total": 100,
    "page": 1,
    "per_page": 20
  }
}

// Success Response with message
{
  "success": true,
  "data": {
    "message": "Operation completed successfully",
    // 其他資料...
  }
}

// Error Response
{
  "success": false,
  "error": "Error message description"
}
```

**實作範例 (Python/Odoo):**
```python
def _standardize_response(self, response_data):
    """統一回應格式包裝器"""
    return response_data

# 成功回應
return self._standardize_response({
    'success': True,
    'data': {
        'workspace_id': workspace.id,
        'workspace_name': workspace.name,
        'message': 'Workspace created successfully'
    }
})

# 錯誤回應
return self._standardize_response({
    'success': False,
    'error': str(e)
})
```

3. **版本控制**
   - URL 路徑版本: `/paas/api/v1/...`
   - 向後相容性保證

### 7.2 認證機制 (Odoo Session)

```
Frontend (OWL App)                 Backend (Odoo)
   │                                 │
   │ POST /web/session/authenticate  │
   │ { db, login, password }         │
   │────────────────────────────────>│
   │                                 │ Validate credentials
   │                                 │ Create session
   │   Set-Cookie: session_id=xxx    │
   │<────────────────────────────────│
   │                                 │
   │ 瀏覽器自動儲存 session cookie    │
   │                                 │
   │ GET /paas/api/v1/workspaces          │
   │ Cookie: session_id=xxx          │
   │────────────────────────────────>│
   │                                 │ Verify session
   │                                 │ request.env.user
   │      { success, data }          │
   │<────────────────────────────────│
```

**OWL 前端存取 API:**
```javascript
// OWL App 中使用 rpc 服務
import { rpc } from "@web/core/network/rpc";

// 自動帶入 session cookie
const result = await rpc("/paas/api/v1/workspaces", {});
```

### 7.3 前端狀態管理建議

```javascript
// OWL App State Structure
const appState = {
  auth: {
    isAuthenticated: boolean,
    user: User | null,
    token: string | null,
  },
  workspaces: {
    items: Workspace[],
    current: Workspace | null,
    loading: boolean,
  },
  billing: {
    balance: number,
    usage: number,
    paymentMethods: PaymentMethod[],
  },
  team: {
    members: TeamMember[],
    pendingInvites: Invitation[],
  },
  ui: {
    sidebarCollapsed: boolean,
    currentPage: string,
    notifications: Notification[],
  }
};
```

### 7.4 錯誤處理策略

| HTTP 狀態碼 | 情境 | 前端處理 |
|-------------|------|----------|
| 400 | 請求參數錯誤 | 顯示欄位錯誤訊息 |
| 401 | 未認證/Token 過期 | 重導向到登入頁 |
| 403 | 權限不足 | 顯示權限不足訊息 |
| 404 | 資源不存在 | 顯示 404 頁面 |
| 422 | 業務邏輯錯誤 | 顯示業務錯誤訊息 |
| 500 | 伺服器錯誤 | 顯示通用錯誤訊息 |

### 7.5 API 速率限制

```yaml
Rate Limits:
  - Authentication: 5 requests/minute
  - General API: 100 requests/minute
  - File Upload: 10 requests/minute
  - Billing Operations: 20 requests/minute

Response Headers:
  - X-RateLimit-Limit: 100
  - X-RateLimit-Remaining: 95
  - X-RateLimit-Reset: 1640000000
```

---

## 附錄 A: API 錯誤碼對照表

| 錯誤碼 | 說明 |
|--------|------|
| AUTH_INVALID_CREDENTIALS | 帳號或密碼錯誤 |
| AUTH_TOKEN_EXPIRED | Token 已過期 |
| AUTH_INSUFFICIENT_PERMISSIONS | 權限不足 |
| WORKSPACE_NOT_FOUND | 工作區不存在 |
| WORKSPACE_LIMIT_REACHED | 已達工作區上限 |
| APP_DEPLOYMENT_FAILED | 應用部署失敗 |
| BILLING_INSUFFICIENT_CREDITS | Credits 餘額不足 |
| BILLING_PAYMENT_FAILED | 付款失敗 |
| TEAM_MEMBER_LIMIT_REACHED | 團隊成員已達上限 |
| VALIDATION_ERROR | 輸入驗證錯誤 |

---

## 附錄 B: 設計稿頁面對照表

| 頁面名稱 | 設計稿目錄 | 對應 API |
|----------|------------|----------|
| Dashboard | paas_web_app_shell_-_global_navigation_1 | Dashboard API |
| Workspace List | workspace_list_page_1, workspace_list_page_2 | Workspace API |
| Workspace Dashboard | workspace_dashboard_1~4 | Workspace API, App API |
| App Marketplace | paas_application_marketplace | Marketplace API |
| App Detail | cloud_app_detail_page_1~7 | Cloud App API |
| App Configuration | app_configuration_&_launch_1~3 | Cloud App API |
| Smart Home Intro | smart_home_connect_intro_page | Hub Connect API |
| Smart Home Settings | smart_home_hub_settings_1~2 | Hub Connect API |
| Security Access | security_access_intro_page, security_access_detail_1~5 | Tunnel API |
| Billing Overview | billing_&_finance_overview_1~7 | Billing API |
| Add Funds Modal | add_funds_/_payment_modal_1~2 | Billing API |
| Team Management | team_management_page_1~5 | Team API |
| Invite Member Modal | invite_new_member_modal | Team API |
| Settings Overview | settings_overview_page_1~2 | Settings API |
| User Profile | user_profile_settings | Settings API |
| Notifications | notification_preferences_page | Settings API |
| Create Workspace Modal | create_workspace_modal | Workspace API |
| Custom Domain Modal | edit_custom_domain_modal | Cloud App API (Domain) |
| Deployment Script Modal | deployment_script_modal | Security Access API (Script) |
| Service Selection | service_selection_page | Workspace API (導航頁) |

---

**文件結束**

本文件將隨著開發進度持續更新，如有任何問題請聯繫開發團隊。
