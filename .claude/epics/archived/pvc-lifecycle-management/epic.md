---
name: pvc-lifecycle-management
status: completed
created: 2026-04-03T13:33:26Z
updated: 2026-04-03T13:33:26Z
progress: 100%
prd: .claude/prds/pvc-lifecycle-management.md
---

# Epic: PVC Lifecycle Management

## Overview

實作 PVC 生命週期管理機制，解決 Cloud Service 刪除後 PVC/PV 殘留的問題。透過 `longhorn-delete` StorageClass + 延遲清理策略，在保護用戶資料（grace period）的同時確保儲存資源最終被回收。

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| StorageClass | 使用叢集既有 `longhorn-delete` | 已存在且 `reclaimPolicy: Delete`，不需額外建立 |
| PVC 清理策略 | 延遲刪除（label + CronJob） | 平衡資料保護與資源回收；用戶誤刪有恢復窗口 |
| Grace period | 環境變數可配置 | dev/stg 7 天快速回收，production 90 天充足保護 |
| CronJob 執行頻率 | 每日 UTC 2:00 AM | 離峰時段，不影響正常操作 |
| Released PV 處理 | CronJob 同時清理 | 處理歷史遺留問題，patch reclaimPolicy 後刪除 |
| StorageClass 注入點 | Odoo `_execute_deploy()` | 強制覆蓋確保所有新服務使用正確 StorageClass |

## Tasks

### Task 1: 擴充 RBAC — 新增 PV 管理權限

**Status:** completed
**File:** `extra/paas-operator/helm/templates/clusterrole.yaml`

新增 `persistentvolumes` 的 `get/list/patch/delete` 權限，供 CronJob 清理 Released PV。

### Task 2: 新增 pvc_grace_period_days 設定

**Status:** completed
**Files:**
- `extra/paas-operator/src/config.py` — 新增 `pvc_grace_period_days: int = 7`
- `extra/paas-operator/helm/values.yaml` — 新增 `config.pvcGracePeriodDays` + `pvcCleanup` section
- `extra/paas-operator/helm/templates/deployment.yaml` — 新增 `PVC_GRACE_PERIOD_DAYS` env var

### Task 3: KubernetesService 新增 PVC 操作方法

**Status:** completed
**File:** `extra/paas-operator/src/services/helm.py`

新增兩個方法：
- `list_pvcs(namespace)` — 列出 namespace 中所有 PVC 名稱
- `label_pvcs(namespace, pvc_names, labels)` — 批次標記 PVC（使用 `--overwrite`）

### Task 4: 刪除服務後標記孤兒 PVC

**Status:** completed
**File:** `extra/paas-operator/src/api/releases.py`

在 `delete_release()` 的 `helm_service.uninstall()` 成功後，標記殘留 PVC：
- `paas.woowtech.io/orphaned-at` — ISO 8601 timestamp
- `paas.woowtech.io/release-name` — 被刪除的 release 名稱
- `paas.woowtech.io/grace-period-days` — grace period 天數

失敗不阻斷刪除流程（warning log）。

### Task 5: 建立 PVC 清理 CronJob

**Status:** completed
**File:** `extra/paas-operator/helm/templates/pvc-cleanup-cronjob.yaml`（新檔）

CronJob 分兩部分：
1. **Part 1**：掃描有 `orphaned-at` label 的 PVC，刪除超過 grace period 的
2. **Part 2**：掃描 Released 狀態的 PV（`claimRef.namespace` 以 `paas-ws-` 開頭），先 patch `reclaimPolicy: Delete` 再刪除

### Task 6: 部署時注入 StorageClass

**Status:** completed
**File:** `src/models/cloud_service.py`

在 `_execute_deploy()` 中，value merge 後強制注入 `longhorn-delete` StorageClass。新增 `_inject_storage_class()` 靜態方法，依 template slug 對應不同 Helm chart 的 value path：

| Slug | Paths |
|------|-------|
| odoo | `persistence.storageClass`, `postgresql.primary.persistence.storageClass` |
| postgresql | `primary.persistence.storageClass` |
| n8n | `persistence.storageClass` |
| redis | `master.persistence.storageClass`, `replica.persistence.storageClass` |

## Deployment Notes

兩個 paas-operator instance 都需重新部署：

```bash
# STG (grace: 7 days)
helm upgrade paas-operator-stg -n paas-stg-system --set config.pvcGracePeriodDays=7

# Production (grace: 90 days)
helm upgrade paas-operator -n paas-system --set config.pvcGracePeriodDays=90
```

CronJob 首次執行會立即清理現有 16 個 Released PV（Part 2）。

## Verification

1. `kubectl get sc longhorn-delete` — 確認 StorageClass 存在
2. 部署新服務 → `kubectl get pvc -n paas-ws-* -o wide` — 確認使用 `longhorn-delete`
3. 刪除服務 → `kubectl get pvc --show-labels` — 確認 `orphaned-at` label
4. 手動觸發 CronJob → 確認 PVC 和 PV 被清理
