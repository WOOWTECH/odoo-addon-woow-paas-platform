---
name: k8s-dev-sandbox
description: Kubernetes-based development sandbox skill replacing Docker-based setup-end-to-end
status: backlog
created: 2026-02-28T09:23:10Z
---

# PRD: k8s-dev-sandbox

## Executive Summary

開發一個新的 Claude Code skill，讓開發者能在 Kubernetes 叢集上一鍵部署完整的 Odoo 開發沙盒環境。此 skill 取代現有基於 Docker Compose 的 `setup-end-to-end`，利用 Helm Chart 在本地 K3s 或雲端 K8s 上建立隔離的開發/測試環境，同時支援手動管理與 TTL 自動清理，適用於本地開發與 CI/CD 自動化兩種場景。

## Problem Statement

### 現有問題

1. **環境一致性差距**：現有 Docker Compose 方案與生產環境（Kubernetes）差異大，開發者在本地測試通過的功能在 K8s 上可能出現問題（網路策略、資源限制、Ingress 行為等）。
2. **無法模擬真實部署**：Cloud Services 功能（PaaS Operator + Helm）在 Docker Compose 環境下無法完整測試，需要真實的 K8s 環境。
3. **CI/CD 缺乏臨時環境**：目前沒有自動化方式在 PR review 時建立臨時環境進行 E2E 測試。
4. **資源管理粗放**：Docker Compose 環境容易忘記清理，長時間佔用本地資源。

### 為什麼現在要做

- Phase 4 (Cloud Services) 已完成，需要在真實 K8s 環境中進行整合測試
- 團隊已建立 K3s 基礎設施，具備部署條件
- PaaS Operator 已運行在 K8s 中，開發環境應靠攏生產架構

## User Stories

### Persona 1: 後端開發者 (Eugene)

**US-1**: 作為後端開發者，我想要在我的本地 K3s 上快速建立一個完整的 Odoo 沙盒環境，這樣我可以在接近生產的環境中測試 Cloud Services 功能。

**Acceptance Criteria**:
- 執行單一 skill 命令即可建立完整環境
- 環境包含 Odoo + PostgreSQL + Nginx
- 本地程式碼自動掛載到 Pod 中（PVC 方式）
- 沙盒有唯一的 namespace 和 URL
- 建立時間 < 5 分鐘

**US-2**: 作為開發者，我想要能同時運行多個沙盒環境（對應不同 git branch），而且互不干擾。

**Acceptance Criteria**:
- 每個沙盒使用獨立的 K8s namespace
- namespace 名稱基於 branch 名稱自動生成
- 資料庫完全隔離
- 可列出所有活躍的沙盒

**US-3**: 作為開發者，我想要沙盒能在一段時間後自動清理，避免忘記刪除浪費資源。

**Acceptance Criteria**:
- 可設定 TTL（預設 24 小時）
- TTL 到期後自動刪除整個 namespace
- 可手動延長 TTL
- 可手動立即銷毀

### Persona 2: CI/CD Pipeline

**US-4**: 作為 CI/CD pipeline，我想要在 PR 建立時自動部署一個臨時沙盒環境進行 E2E 測試，測試完成後自動清理。

**Acceptance Criteria**:
- 支援透過 CLI 命令非互動式部署
- 可指定 Docker image tag 進行部署
- 測試完成後自動銷毀環境
- 部署結果可回報到 PR comment

**US-5**: 作為 CI/CD pipeline，我想要能在沙盒中自動執行 Odoo 測試套件。

**Acceptance Criteria**:
- 提供測試執行命令
- 可擷取測試結果和日誌
- 測試失敗時保留環境供除錯（可選）

## Requirements

### Functional Requirements

#### FR-1: Skill 命令結構

```
# 核心命令
/k8s-sandbox create [--branch <name>] [--ttl <duration>] [--image <tag>]
/k8s-sandbox list
/k8s-sandbox status [<sandbox-name>]
/k8s-sandbox destroy <sandbox-name>
/k8s-sandbox extend <sandbox-name> [--ttl <duration>]
/k8s-sandbox logs <sandbox-name> [--service <name>]
/k8s-sandbox test <sandbox-name>

# 快捷命令
/k8s-sandbox up     # = create with defaults
/k8s-sandbox down   # = destroy current branch sandbox
```

#### FR-2: Helm Chart 架構

建立專屬的 Helm Chart（`charts/odoo-dev-sandbox/`）包含以下元件：

| 元件 | 類型 | 說明 |
|------|------|------|
| Odoo | Deployment | 開發模式的 Odoo 18 實例 |
| PostgreSQL | StatefulSet | 獨立的資料庫 |
| Nginx | Deployment | 反向代理 + WebSocket 支援 |
| PgAdmin | Deployment (optional) | 資料庫管理工具 |

#### FR-3: Namespace 管理

- 命名格式：`dev-sandbox-{branch-slug}`（如 `dev-sandbox-epic-cloud-services`）
- 自動建立 namespace 並設定 ResourceQuota
- 自動建立 NetworkPolicy 隔離沙盒
- 自動配置 Ingress（`{branch-slug}.dev.woow.tw`）

#### FR-4: 程式碼同步策略

**本地開發模式（PVC）**：
- 將本地 worktree 目錄掛載為 PVC
- 使用 hostPath 或 local PV（K3s 本地環境）
- 支援 Odoo dev mode 即時重載

**CI/CD 模式（Docker Image）**：
- 從 git ref 建置 Docker image
- 將 addon 程式碼包含在 image 中
- image tag 格式：`woow-odoo-dev:{branch-slug}-{commit-short}`

#### FR-5: 自動初始化

首次啟動時自動：
1. 建立 PostgreSQL 資料庫
2. 安裝 base module + woow_paas_platform + extra-addons
3. 載入繁體中文語言包
4. 設定 admin 密碼
5. 建立初始資料

#### FR-6: TTL 與生命週期管理

- 使用 K8s CronJob 或 annotation + controller 實現 TTL
- 預設 TTL：7 天（可配置）
- TTL 到期前 1 天發送通知（stdout log）
- 支援手動延長和立即銷毀
- `--ttl 0` 表示永不自動刪除

#### FR-7: 狀態追蹤

Skill 需追蹤以下狀態：
- Sandbox 名稱與 namespace
- 建立時間與 TTL 到期時間
- 存取 URL
- 服務健康狀態
- 使用的 git branch 和 commit

### Non-Functional Requirements

#### NFR-1: 效能
- 沙盒建立時間 < 5 分鐘（不含 image build）
- Image build 時間 < 3 分鐘
- 銷毀時間 < 30 秒

#### NFR-2: 資源限制
- 每個沙盒 ResourceQuota：
  - CPU: 2 cores request / 4 cores limit
  - Memory: 2Gi request / 4Gi limit
  - Storage: 10Gi
- 最多同時運行 5 個沙盒（可配置）

#### NFR-3: 安全性
- Namespace 之間網路隔離（NetworkPolicy）
- 資料庫密碼使用 K8s Secret 管理
- 不暴露到公網（除非明確配置 Ingress）
- RBAC 限制 sandbox namespace 的權限

#### NFR-4: 可靠性
- 部署失敗時自動回滾並清理資源
- PostgreSQL 使用 PVC 持久化
- 支援沙盒重啟不丟失資料

#### NFR-5: 相容性
- 支援 K3s 1.28+（本地開發）
- 支援標準 K8s 1.28+（雲端）
- 支援 Helm 3.13+
- 支援 macOS (Apple Silicon) 和 Linux

## Success Criteria

| 指標 | 目標 |
|------|------|
| 一鍵建立成功率 | > 95% |
| 平均建立時間 | < 5 分鐘 |
| 與 Docker 方案功能對等 | 100% |
| CI/CD 整合測試覆蓋率 | > 80% |
| 資源自動回收率 | 100%（TTL 到期後） |

## Technical Design Overview

### 目錄結構

```
woow_paas_platform/
├── charts/
│   └── odoo-dev-sandbox/           # Helm Chart
│       ├── Chart.yaml
│       ├── values.yaml
│       ├── values-local.yaml       # K3s 本地覆蓋
│       ├── values-ci.yaml          # CI/CD 覆蓋
│       └── templates/
│           ├── namespace.yaml
│           ├── odoo-deployment.yaml
│           ├── odoo-service.yaml
│           ├── postgres-statefulset.yaml
│           ├── postgres-service.yaml
│           ├── nginx-deployment.yaml
│           ├── nginx-configmap.yaml
│           ├── pgadmin-deployment.yaml  # optional
│           ├── ingress.yaml
│           ├── resource-quota.yaml
│           ├── network-policy.yaml
│           ├── secrets.yaml
│           ├── pvc.yaml
│           ├── ttl-cronjob.yaml
│           └── _helpers.tpl
├── scripts/
│   ├── k8s-sandbox-create.sh       # 建立沙盒
│   ├── k8s-sandbox-destroy.sh      # 銷毀沙盒
│   ├── k8s-sandbox-list.sh         # 列出沙盒
│   ├── k8s-sandbox-status.sh       # 檢查狀態
│   └── k8s-sandbox-test.sh         # 執行測試
├── .claude/
│   └── skills/
│       └── k8s-dev-sandbox.md      # Skill 定義
└── Dockerfile.dev                   # 開發用 Docker image
```

### 部署流程

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Skill CMD   │────▶│ Shell Script │────▶│  Helm CLI    │
│ /k8s-sandbox │     │  (Logic)     │     │  (Deploy)    │
└──────────────┘     └──────────────┘     └──────────────┘
                                                │
                     ┌──────────────────────────┘
                     ▼
              ┌──────────────┐
              │ K8s Cluster  │
              │ ┌──────────┐ │
              │ │Namespace │ │
              │ │┌────────┐│ │
              │ ││ Odoo   ││ │
              │ ││ PG     ││ │
              │ ││ Nginx  ││ │
              │ │└────────┘│ │
              │ └──────────┘ │
              └──────────────┘
```

## Constraints & Assumptions

### 技術限制
- 本地 PVC 掛載僅適用於 K3s 或支援 hostPath 的叢集
- CI/CD 模式需要 Docker registry 推送權限
- Helm 和 kubectl 必須預先安裝在開發機器上

### 假設
- 開發者已有可用的 K8s cluster（K3s 或雲端）
- kubectl 已配置正確的 context
- Helm 3.13+ 已安裝
- 本地開發時 K3s 運行在同一台機器上

### 時間限制
- 第一階段（核心功能）：2 週
- 第二階段（CI/CD 整合）：1 週
- 第三階段（TTL + 進階功能）：1 週

## Out of Scope

- **生產環境部署** - 此 skill 僅用於開發/測試，不用於生產
- **多叢集管理** - 一次只針對一個 K8s cluster 操作
- **自動化 K3s 安裝** - 假設叢集已就緒
- **資料庫備份/還原** - 沙盒環境為臨時性質
- **HTTPS 證書自動化** - 開發環境使用 HTTP 或自簽憑證
- **GPU 資源支援** - 不在 MVP 範圍內
- **Windows 支援** - 僅支援 macOS 和 Linux

## Dependencies

### 外部依賴
- Kubernetes cluster (K3s 1.28+ 或 K8s 1.28+)
- Helm 3.13+
- kubectl 1.28+
- Docker registry（CI/CD 模式需要）

### 內部依賴
- 現有 `docker-compose.yml` 作為服務配置參考
- `odoo.conf.template` 作為 Odoo 配置基礎
- `extra/paas-operator/` 的 Helm chart 模式作為參考
- 現有 `scripts/` 作為自動化邏輯參考

### 相關功能
- PaaS Operator - 沙盒可選擇性地使用 PaaS Operator 進行服務管理
- Cloud Services - 沙盒環境可用於測試 Cloud Services 部署流程

## Migration Plan

### 從 Docker Compose 遷移

此 skill **不取代** 現有 Docker Compose 方案，而是提供額外選擇：

| 場景 | 建議方案 |
|------|---------|
| 快速本地開發（無 K8s） | Docker Compose (setup-end-to-end) |
| 接近生產環境的測試 | K8s Sandbox (k8s-dev-sandbox) |
| Cloud Services 功能開發 | K8s Sandbox (k8s-dev-sandbox) |
| CI/CD E2E 測試 | K8s Sandbox (k8s-dev-sandbox) |
| 新開發者 Onboarding | Docker Compose（門檻較低） |

兩套方案並存，開發者根據需求選擇。
