---
name: pvc-lifecycle-management
description: PVC lifecycle management with deferred cleanup and StorageClass injection to prevent orphaned storage accumulation
status: implemented
created: 2026-04-03T13:33:26Z
updated: 2026-04-03T13:33:26Z
epic: .claude/epics/pvc-lifecycle-management/epic.md
---

# PRD: PVC Lifecycle Management

## Executive Summary

Cloud Service 刪除後，Kubernetes PVC 和 PV 會殘留導致儲存空間浪費。本功能實作完整的 PVC 生命週期管理：使用 `reclaimPolicy: Delete` 的 StorageClass 確保 PVC 刪除時 PV 自動回收，並透過 grace period 機制保護用戶資料不被意外永久刪除。

**核心策略**：StorageClass Delete + 延遲 PVC 清理（label + CronJob）

## Problem Statement

### 問題描述

當用戶透過 UI 刪除 Cloud Service 時：

1. **StatefulSet PVC（PostgreSQL）**：`helm uninstall` 不會刪除 StatefulSet 產生的 PVC
2. **Deployment PVC（Odoo）**：helm 刪了 PVC，但 PV 因 Longhorn `reclaimPolicy: Retain` 卡在 Released 狀態
3. **累積效應**：STG 環境已累積 16 個孤兒 PV，佔用約 136Gi 儲存空間

### 為什麼現在重要

- 儲存空間持續被孤兒 PV 消耗，無法自動回收
- Longhorn 無內建 Released PV 自動清理功能
- 隨著用戶增加，問題會加速惡化
- 需要在資料保護（防誤刪）和資源回收之間取得平衡

## User Stories

### US-001: 服務刪除後儲存自動回收

**As a** Platform Administrator
**I want to** 刪除的服務的儲存資源在 grace period 後自動回收
**So that** 不需要手動清理孤兒 PVC/PV，節省維運成本

**Acceptance Criteria:**

- [x] 刪除服務後，殘留 PVC 被標記 `orphaned-at` timestamp
- [x] CronJob 每日掃描並刪除超過 grace period 的 PVC
- [x] PVC 刪除後 PV 自動回收（透過 `longhorn-delete` StorageClass）

### US-002: 誤刪資料恢復窗口

**As a** Workspace Owner
**I want to** 不小心刪除服務後有一段時間可以恢復資料
**So that** 不會因為一次誤操作永久失去所有資料

**Acceptance Criteria:**

- [x] Dev/STG 環境 grace period 為 7 天
- [x] Production 環境 grace period 為 90 天
- [x] Grace period 內 PVC 資料保留完整

### US-003: 新服務使用正確 StorageClass

**As a** Platform Developer
**I want to** 新部署的服務自動使用 `longhorn-delete` StorageClass
**So that** 後續刪除時 PV 能正確回收，不再產生新的孤兒 PV

**Acceptance Criteria:**

- [x] Odoo、PostgreSQL、n8n、Redis 模板自動注入 `longhorn-delete`
- [x] 不同 Helm chart 的 storageClass value path 正確對應

## Technical Design

### Architecture

```
服務刪除流程（改進後）：
┌──────────┐     ┌───────────────┐     ┌──────────────┐
│ helm     │     │ Label PVCs    │     │ CronJob      │
│ uninstall│────▶│ orphaned-at   │────▶│ (daily 2AM)  │
│          │     │ + grace days  │     │ delete if     │
│ Pods 刪除│     │ PVC 保留      │     │ expired      │
└──────────┘     └───────────────┘     └──────┬───────┘
                                              │
                                   ┌──────────▼───────┐
                                   │ PVC deleted       │
                                   │ ↓ (longhorn-delete│
                                   │   reclaimPolicy)  │
                                   │ PV auto-deleted   │
                                   │ Longhorn volume   │
                                   │ reclaimed          │
                                   └──────────────────┘
```

### 元件

| 元件 | 說明 |
|------|------|
| `longhorn-delete` StorageClass | 叢集既有，`reclaimPolicy: Delete` |
| PVC Label 機制 | paas-operator 刪除服務後標記殘留 PVC |
| PVC Cleanup CronJob | Helm chart 中的 K8s CronJob，每日清理過期 PVC + Released PV |
| StorageClass 注入 | Odoo `_execute_deploy()` 強制注入 `longhorn-delete` |

### Grace Period 配置

| 環境 | `pvcGracePeriodDays` | 說明 |
|------|---------------------|------|
| Dev/STG | 7 | 開發測試快速回收 |
| Production | 90 | 充足的資料恢復窗口 |

## Scope

### In Scope

- PVC orphan labeling after helm uninstall
- PVC cleanup CronJob (daily)
- Released PV cleanup (legacy orphans with Retain policy)
- StorageClass injection for all template types (odoo, postgresql, n8n, redis)
- RBAC expansion for PV management
- Configurable grace period per environment

### Out of Scope

- PVC 資料實際恢復功能（只保留 PVC，不提供 UI 還原）
- StorageClass 本身的建立（使用叢集既有 `longhorn-delete`）
- 既有服務的 StorageClass 遷移（只影響新部署的服務）

## Files Modified

| 檔案 | 變更 |
|------|------|
| `extra/paas-operator/helm/templates/clusterrole.yaml` | 新增 PV RBAC |
| `extra/paas-operator/src/config.py` | 新增 `pvc_grace_period_days` |
| `extra/paas-operator/helm/values.yaml` | 新增 `pvcCleanup` + `pvcGracePeriodDays` |
| `extra/paas-operator/helm/templates/deployment.yaml` | 新增 env var |
| `extra/paas-operator/src/services/helm.py` | 新增 `list_pvcs()` + `label_pvcs()` |
| `extra/paas-operator/src/api/releases.py` | `delete_release()` 加入 PVC labeling |
| `extra/paas-operator/helm/templates/pvc-cleanup-cronjob.yaml` | **新檔** CronJob |
| `src/models/cloud_service.py` | `_inject_storage_class()` + 注入邏輯 |
