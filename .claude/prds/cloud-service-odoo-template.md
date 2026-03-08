---
name: cloud-service-odoo-template
description: Add Odoo as a deployable Cloud Service template using Bitnami Helm chart
status: backlog
created: 2026-02-08T03:10:18Z
updated: 2026-02-08T03:10:18Z
---

# PRD: Cloud Service Odoo Template

## Executive Summary

在現有 Cloud Services 架構上新增 Odoo 應用模板，讓用戶可以從 Marketplace 一鍵部署獨立的 Odoo ERP 實例。使用 Bitnami 維護的官方 Helm chart（`oci://registry-1.docker.io/bitnamicharts/odoo`），透過現有的 PaaS Operator 進行部署。

**核心價值**：用戶無需了解 Kubernetes 即可在幾分鐘內獲得一個完整的 Odoo ERP 環境，包含內建 PostgreSQL 資料庫。

## Problem Statement

### 問題描述

目前 Cloud Service 的 Application Marketplace 只提供 PostgreSQL、n8n、Redis 三個應用模板。用戶若想在平台上部署 Odoo ERP，需要自行處理 Helm chart 配置、資料庫設定、Ingress 路由等複雜操作。

### 為什麼現在重要

- Odoo 是平台自身使用的技術，提供 Odoo-as-a-Service 是自然的延伸
- 許多中小企業有快速建立獨立 Odoo 環境的需求（測試、POC、客戶 Demo）
- Bitnami 提供了成熟的 Odoo Helm chart，降低實作風險
- 現有 Cloud Services 架構已完整，只需新增模板資料和配置

## User Stories

### US-001: 從 Marketplace 部署 Odoo

**As a** Workspace Owner/Admin
**I want to** 從 Marketplace 選擇 Odoo 並一鍵部署
**So that** 我可以快速獲得一個完整的 Odoo ERP 環境

**Acceptance Criteria:**

- [ ] Marketplace 中可以看到 Odoo 應用卡片（在 Web 分類下）
- [ ] 點擊 Odoo 卡片可以看到完整說明（功能介紹、資源需求）
- [ ] 可以設定管理員帳號（email）和密碼
- [ ] 點擊 Launch 後服務開始部署
- [ ] 部署完成後可透過 subdomain 訪問 Odoo Web UI

### US-002: 配置 Odoo 部署參數

**As a** Workspace Owner/Admin
**I want to** 在部署前配置 Odoo 的基本設定
**So that** 部署後可以直接使用而不需要額外設定

**Acceptance Criteria:**

- [ ] 可以設定管理員 email（預設值 `user@example.com`）
- [ ] 可以設定管理員密碼（必填）
- [ ] 可以選擇是否載入 Demo 資料
- [ ] 其他進階設定使用合理的預設值

### US-003: 訪問已部署的 Odoo

**As a** Workspace Member
**I want to** 透過 subdomain 訪問已部署的 Odoo 實例
**So that** 我可以使用 Odoo ERP 功能

**Acceptance Criteria:**

- [ ] 部署完成後，服務狀態顯示 Running
- [ ] 可以透過 `{subdomain}.woowtech.io` 訪問 Odoo Web UI
- [ ] 可以使用配置的 email/密碼登入
- [ ] Odoo Web UI 功能正常運作

## Requirements

### Functional Requirements

#### FR-1: Odoo 應用模板資料

- FR-1.1: 新增 `cloud_app_odoo` XML record 至 `cloud_app_templates.xml`
- FR-1.2: 使用 Bitnami OCI chart：`oci://registry-1.docker.io/bitnamicharts/odoo`
- FR-1.3: 分類為 `web`
- FR-1.4: 預設 port 為 `8069`（Odoo HTTP port）
- FR-1.5: 啟用 Ingress（`ingress_enabled = True`）

#### FR-2: Helm Values 配置

- FR-2.1: 預設 values 包含合理的資源限制
- FR-2.2: `helm_value_specs` 定義用戶可配置的欄位：
  - `odooEmail`（string, 管理員 email）
  - `odooPassword`（string, 管理員密碼，必填）
  - `loadDemoData`（boolean, 是否載入 Demo 資料）
- FR-2.3: 內建 PostgreSQL 使用 standalone 模式
- FR-2.4: Persistence 啟用，預設 10Gi 儲存空間

#### FR-3: Category 新增

- FR-3.1: `cloud_app_template.py` 的 category Selection 需新增 `web` 選項（若不存在）

### Non-Functional Requirements

#### NFR-1: 資源需求

- 最低 vCPU：2（Odoo 需要較多 CPU）
- 最低 RAM：2 GB
- 最低 Storage：10 GB（Odoo 資料 + PostgreSQL）

#### NFR-2: 部署時間

- Odoo chart 較大（包含 PostgreSQL），預期部署時間 3-8 分鐘
- PaaS Operator 的 `HELM_OPERATION_TIMEOUT`（120s）應足夠

#### NFR-3: 安全性

- 管理員密碼不可為空
- Helm values 中的密碼透過 K8s Secret 管理（Bitnami chart 內建支援）
- 用戶只能配置 `helm_value_specs` 中定義的欄位

## Technical Design

### Helm Chart 資訊

| 項目 | 值 |
|------|-----|
| OCI 路由 | `oci://registry-1.docker.io/bitnamicharts/odoo` |
| 預設 Port | 8069 (HTTP) |
| 內建 PostgreSQL | 是（standalone 模式） |
| Persistence | 預設 10Gi |
| Service Type | LoadBalancer（由 PaaS Operator 處理） |
| Resources Preset | `large` |

### 預設 Helm Values

```json
{
  "odooEmail": "user@example.com",
  "odooSkipInstall": false,
  "loadDemoData": false,
  "service": {
    "type": "ClusterIP"
  },
  "resources": {
    "requests": {
      "cpu": "200m",
      "memory": "512Mi"
    },
    "limits": {
      "cpu": "2000m",
      "memory": "2Gi"
    }
  },
  "postgresql": {
    "enabled": true,
    "architecture": "standalone",
    "auth": {
      "username": "bn_odoo",
      "database": "bitnami_odoo"
    }
  },
  "persistence": {
    "enabled": true,
    "size": "10Gi"
  }
}
```

### Helm Value Specs（用戶可配置欄位）

```json
{
  "properties": {
    "odooEmail": {
      "type": "string",
      "description": "Administrator email"
    },
    "odooPassword": {
      "type": "string",
      "description": "Administrator password"
    },
    "loadDemoData": {
      "type": "boolean",
      "description": "Load demo data during initialization"
    }
  }
}
```

### 變更範圍

| 檔案 | 變更類型 | 說明 |
|------|----------|------|
| `src/data/cloud_app_templates.xml` | 修改 | 新增 Odoo template record |
| `src/models/cloud_app_template.py` | 修改 | category selection 確認有 `web` 選項 |

### 不需要變更的部分

- PaaS Operator：已支援 OCI chart 安裝，無需修改
- Controller（`paas.py`）：已有通用的 template/service CRUD，無需修改
- Frontend：Marketplace UI 已支援動態顯示 templates，無需修改
- CloudService model：通用設計，無需修改

## Success Criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| 模板正確顯示 | 100% | Marketplace 中顯示 Odoo 卡片且資訊正確 |
| 部署成功率 | > 90% | 成功部署數 / 總部署數 |
| 部署時間 | < 8 分鐘 | 從 Launch 到 Running 狀態 |
| Odoo Web UI 可訪問 | 100% | 部署完成後可透過 subdomain 正常登入 |

## Constraints & Assumptions

### Constraints

- Bitnami Odoo chart 包含內建 PostgreSQL，會額外消耗資源
- Odoo chart 映像檔較大（~1GB），首次拉取可能較慢
- 用戶無法自訂 Odoo 模組安裝（需使用預設模組集）

### Assumptions

- 現有 PaaS Operator 已正確部署並可訪問 OCI registry
- K8s cluster 有足夠資源運行 Odoo + PostgreSQL pods
- Cloudflare Tunnel 正確配置，可以路由到 port 8069 的服務
- 現有的 `_filter_allowed_helm_values` 機制可以正確過濾 Odoo 的 values

## Out of Scope

| Feature | Reason |
|---------|--------|
| Odoo 模組市集整合 | 複雜度過高，MVP 不需要 |
| 多版本選擇（Odoo 17/18） | 先固定使用 Bitnami 最新版 |
| 外部 PostgreSQL 連接 | 使用內建 PostgreSQL 即可 |
| SMTP/Email 配置 | 可透過 Odoo Web UI 自行設定 |
| 自訂 Odoo 配置檔 | 安全性考量 |
| Odoo Worker 數量調整 | 使用預設值 |

## Dependencies

### External Dependencies

- Bitnami Odoo Helm chart（`oci://registry-1.docker.io/bitnamicharts/odoo`）
- Docker Hub OCI registry 可用性

### Internal Dependencies

- Cloud Services MVP（已完成，PR #15 已合併）
- PaaS Operator Service（已部署）
- Marketplace UI（已實作）

## Implementation Estimate

此 PRD 的實作範圍非常小，主要是資料變更：

1. **新增 Odoo template XML record**（~30 行 XML）
2. **確認 category `web` 存在**（可能 0 行變更）
3. **測試部署流程**

預計 1 個 Epic、2-3 個 Issues 即可完成。
