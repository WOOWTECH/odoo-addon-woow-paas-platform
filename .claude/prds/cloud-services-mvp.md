---
name: cloud-services-mvp
description: MVP implementation of Cloud Services feature enabling one-click containerized app deployment via Helm
status: implemented
created: 2026-02-01T17:16:16Z
updated: 2026-02-04T16:06:28Z
epic: .claude/epics/cloud-services-mvp/epic.md
pr: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/pull/15
---

# PRD: Cloud Services MVP

## Executive Summary

Cloud Services 是 WoowTech PaaS 平台的核心功能，讓用戶能夠一鍵部署容器化應用程式。用戶可以從 Application Marketplace 選擇預設應用（如 AnythingLLM、n8n、PostgreSQL、Redis 等），設定必要參數後即可快速啟動服務。

**部署架構**：使用 Kubernetes + Helm Charts 進行服務編排與部署，透過獨立的 PaaS Operator Service (FastAPI) 執行 Helm 操作。

**MVP 範圍**：Phase 0 ~ Phase 5，涵蓋 PaaS Operator、Odoo 模型、Marketplace UI、應用配置、服務管理、端對端測試。

## Problem Statement

### 問題描述

目前用戶若要在 Kubernetes 上部署應用程式，需要：

1. 熟悉 Kubernetes 和 Helm 操作
2. 手動配置 namespace、RBAC、Ingress
3. 管理 DNS 和 TLS 憑證
4. 監控服務狀態

這對非技術用戶來說門檻過高，且即使是技術人員也需要重複性的手動操作。

### 為什麼現在重要

- 用戶期望 PaaS 平台提供一鍵部署能力
- 競爭對手（Railway、Render、Coolify）已提供類似功能
- 這是 Workspace 功能的自然延伸，能增加平台價值

## User Stories

### US-001: 首次部署應用

**As a** Workspace Owner
**I want to** 從 Marketplace 選擇並部署應用程式
**So that** 我可以快速獲得一個運行中的服務而無需了解 Kubernetes

**Acceptance Criteria:**

- [ ] 可以瀏覽 Application Marketplace
- [ ] 可以搜尋和篩選應用程式
- [ ] 可以設定應用名稱和 subdomain
- [ ] 可以配置必要的環境變數（如密碼）
- [ ] 點擊「Launch」後服務開始部署
- [ ] 部署完成後可透過 subdomain 訪問服務

### US-002: 管理運行中的服務

**As a** Workspace Member
**I want to** 查看和管理已部署的服務
**So that** 我可以監控服務狀態並進行必要的操作

**Acceptance Criteria:**

- [ ] 可以在 Workspace Dashboard 看到所有服務
- [ ] 可以查看服務詳情（URL、狀態、資源使用）
- [ ] 可以透過 Web UI 按鈕直接訪問服務
- [ ] 可以修改服務配置（觸發 helm upgrade）
- [ ] 可以刪除服務（觸發 helm uninstall）

### US-003: 回滾服務版本

**As a** Workspace Admin
**I want to** 將服務回滾到之前的版本
**So that** 當配置更新出問題時可以快速恢復

**Acceptance Criteria:**

- [ ] 可以查看 Helm revision history
- [ ] 可以選擇特定 revision 進行回滾
- [ ] 回滾後服務恢復到該 revision 的配置

### US-004: 設定自訂網域

**As a** Workspace Owner
**I want to** 為服務設定自訂網域
**So that** 我可以使用自己的網域名稱訪問服務

**Acceptance Criteria:**

- [ ] 可以在服務詳情頁編輯 custom domain
- [ ] 系統顯示 CNAME 設定指引
- [ ] 自訂網域設定後可正常訪問

## Requirements

### Functional Requirements

#### FR-1: PaaS Operator Service

- FR-1.1: 獨立的 FastAPI 服務，處理所有 Helm 操作
- FR-1.2: 提供 RESTful API endpoints (releases CRUD, namespaces)
- FR-1.3: 使用 API Key 進行身份驗證
- FR-1.4: 部署於 K8s cluster 內，使用 ServiceAccount + RBAC
- FR-1.5: 只能操作 `paas-ws-*` namespace（安全限制）

**API Endpoints:**
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

#### FR-2: Odoo Data Models

- FR-2.1: `CloudAppTemplate` - 應用程式目錄（Helm Chart 配置）
- FR-2.2: `CloudService` - 已部署的服務實例
- FR-2.3: Seed data for initial applications (PostgreSQL, n8n, Redis, etc.)

#### FR-3: Application Marketplace

- FR-3.1: 顯示可部署的應用程式卡片
- FR-3.2: 支援分類篩選（AI & LLM, Automation, Database, etc.）
- FR-3.3: 支援關鍵字搜尋
- FR-3.4: 顯示應用資訊（icon, name, description, tags, price）

#### FR-4: App Configuration & Launch

- FR-4.1: 配置應用名稱和 Reference ID
- FR-4.2: 配置 subdomain（`*.woowtech.io`）
- FR-4.3: 動態表單顯示必填/選填的 Helm values
- FR-4.4: 驗證輸入並顯示錯誤訊息
- FR-4.5: 啟動部署並輪詢狀態

#### FR-5: Service Detail Page

- FR-5.1: Overview Tab - 連線資訊、資源使用、Helm release info
- FR-5.2: Configuration Tab - 編輯設定（觸發 helm upgrade）
- FR-5.3: Rollback 功能 - 選擇 revision 並回滾
- FR-5.4: Delete 功能 - 確認後刪除服務

#### FR-6: Workspace Integration

- FR-6.1: 每個 Workspace 對應一個 K8s namespace (`paas-ws-{workspace_id}`)
- FR-6.2: Workspace Dashboard 顯示所有服務
- FR-6.3: 首次部署引導畫面

### Non-Functional Requirements

#### NFR-1: Performance

- Marketplace page load < 500ms
- Service status polling interval: 5s during deployment
- API response time < 200ms (excluding Helm operations)

#### NFR-2: Security

- Helm values 中的敏感資料（密碼）不明文儲存於 Odoo
- PaaS Operator 使用 API Key 認證
- PaaS Operator 只接受 ClusterIP 內部請求
- RBAC 限制 Operator 只能操作 `paas-ws-*` namespace

#### NFR-3: Reliability

- Helm 操作失敗時正確記錄錯誤訊息
- 服務狀態準確反映實際 Pod 狀態
- 部署失敗時提供明確的錯誤資訊

#### NFR-4: Scalability

- 支援每個 Workspace 最多 50 個服務
- 支援多個 Workspace 同時部署

## Technical Architecture

### System Components

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

**Location:** `extra/paas-operator/`

```
extra/paas-operator/
├── src/
│   ├── main.py           # FastAPI app entry point
│   ├── api/
│   │   ├── releases.py   # Helm release endpoints
│   │   └── namespaces.py # Namespace management
│   ├── services/
│   │   └── helm.py       # Helm CLI wrapper
│   └── models/
│       └── schemas.py    # Pydantic models
├── Dockerfile
├── requirements.txt
├── k8s/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── rbac.yaml
│   └── secret.yaml
└── README.md
```

### DNS & TLS (Cloudflare)

- Wildcard domain: `*.woowtech.io`
- SSL/TLS Mode: Full (Strict)
- Origin Certificate: Cloudflare Origin CA (15 年有效)
- Ingress Controller: Traefik (K3s default)

### Namespace Strategy

```
Namespace: paas-ws-{workspace_id}
├── Deployment: {release_name}-{app}
├── Service: {release_name}-{app}
├── Ingress: {release_name}-ingress
├── PVC: {release_name}-data
├── Secret: {release_name}-secrets
└── ConfigMap: {release_name}-config
```

### Helm Release Naming

```
Release Name: paas-ws-{workspace_id}-{reference_id}
Example: paas-ws-123-anythingllm-01
```

## UI/UX Components

### New Pages

| Page                 | Route                                             | Description                  |
| -------------------- | ------------------------------------------------- | ---------------------------- |
| ServiceSelectionPage | `#/workspaces/:id/services/new`                   | 服務類型選擇（首次部署引導） |
| AppMarketplacePage   | `#/workspaces/:id/services/marketplace`           | 應用程式市集                 |
| AppConfigurationPage | `#/workspaces/:id/services/configure/:templateId` | 應用配置頁面                 |
| ServiceDetailPage    | `#/workspaces/:id/services/:serviceId`            | 服務詳情（含 Tabs）          |

### New Components

| Component          | Description                    |
| ------------------ | ------------------------------ |
| ServiceCard        | 服務卡片（Dashboard 用）       |
| AppCard            | 應用程式卡片（Marketplace 用） |
| CategoryFilter     | 分類篩選器                     |
| HelmValueForm      | 動態 Helm values 表單          |
| StatusBadge        | 狀態標籤                       |
| EditDomainModal    | 編輯自訂網域                   |
| DeleteServiceModal | 確認刪除服務                   |

## Success Criteria

| Metric         | Target   | Measurement                   |
| -------------- | -------- | ----------------------------- |
| 部署成功率     | > 95%    | 成功部署數 / 總部署數         |
| 部署時間       | < 5 分鐘 | 從點擊 Launch 到 Running 狀態 |
| UI 回應時間    | < 500ms  | Marketplace 頁面載入          |
| 用戶滿意度     | > 4/5    | 用戶調查                      |
| 測試覆蓋率     | > 80%    | PaaS Operator + Odoo models   |
| E2E 測試通過率 | 100%     | 所有關鍵用戶流程              |

## Constraints & Assumptions

### Constraints

- Odoo Pod 無法直接執行 Helm CLI，需透過獨立服務
- 單一 K8s cluster（不支援 multi-cluster）
- 不提供 Start/Stop 操作（只有 Deploy/Delete）
- Helm Chart 必須是公開可存取的 repository

### Assumptions

- Cloudflare DNS 和 Origin CA 已配置完成
- K3s cluster 已安裝並運行
- Traefik Ingress Controller 已啟用
- 用戶已有 Workspace

## Out of Scope

以下功能明確排除在 MVP 範圍外：

| Feature                | Reason                       |
| ---------------------- | ---------------------------- |
| Metrics Tab            | 需要 Prometheus/Grafana 整合 |
| Activity Logs Tab      | 需要 Loki 整合               |
| Backups Tab            | 需要 Volume Snapshot 支援    |
| Billing Integration    | 商業模式尚未確定             |
| Private Network Toggle | 需要 VPN 整合                |
| Multi-cluster Support  | 複雜度過高                   |
| Custom Helm Charts     | 安全性考量                   |

## Dependencies

### External Dependencies

- Kubernetes cluster (K3s)
- Helm CLI (installed in PaaS Operator container)
- Cloudflare DNS & Origin CA
- Public Helm Chart repositories (Bitnami, etc.)

### Internal Dependencies

- Workspace model (已完成)
- WorkspaceAccess model (已完成)
- OWL frontend framework (已建立)
- Router system (已建立)

## Implementation Phases

### Phase 0: PaaS Operator Service

- [ ] FastAPI project setup (`extra/paas-operator/src/`)
- [ ] Helm CLI integration (subprocess wrapper)
- [ ] API endpoints: releases CRUD, namespaces
- [ ] API Key authentication middleware
- [ ] Dockerfile + K8s manifests
- [ ] README with deployment instructions
- [ ] Health check endpoint

### Phase 1: Foundation (Odoo)

- [ ] CloudAppTemplate model
- [ ] CloudService model
- [ ] Security rules (ir.model.access.csv)
- [ ] Seed data for initial apps (PostgreSQL, n8n, Redis)
- [ ] PaaS Operator client service (HTTP calls)
- [ ] Basic CRUD APIs

### Phase 2: Marketplace UI

- [ ] AppMarketplacePage
- [ ] AppCard component
- [ ] CategoryFilter component
- [ ] Search functionality
- [ ] Marketplace service

### Phase 3: Configuration & Launch

- [ ] AppConfigurationPage
- [ ] HelmValueForm component (dynamic form)
- [ ] Service creation API
- [ ] Subdomain validation
- [ ] Deployment status polling

### Phase 4: Service Management

- [ ] ServiceDetailPage
- [ ] Overview Tab
- [ ] Configuration Tab
- [ ] Rollback functionality
- [ ] Delete confirmation modal
- [ ] ServiceCard for Dashboard

### Phase 5: End-to-End Testing

- [ ] PaaS Operator unit tests (pytest)
- [ ] PaaS Operator integration tests (mock Helm CLI)
- [ ] Odoo model unit tests
- [ ] API endpoint tests (Odoo test framework)
- [ ] Frontend component tests (OWL testing utilities)
- [ ] E2E user flow tests:
  - [ ] Browse Marketplace → Select App → Configure → Launch
  - [ ] View Service Detail → Edit Configuration → Verify Upgrade
  - [ ] Service Rollback flow
  - [ ] Service Delete flow
- [ ] Integration tests (Odoo ↔ PaaS Operator communication)
- [ ] Error handling & edge case tests
- [ ] Performance benchmarks (API response time, page load)

## Appendix

### Sample Helm Chart Configurations

See `docs/spec/cloud-services.md` for detailed examples of:

- PostgreSQL (Bitnami)
- n8n (Community Chart)
- Redis (Bitnami)

### Reference Documents

- Design mockups: `resource/stitch_paas_web_app_shell_global_navigation_2026-01-16/`
- Technical spec: `docs/spec/cloud-services.md`
- Existing workspace model: `src/models/workspace.py`
