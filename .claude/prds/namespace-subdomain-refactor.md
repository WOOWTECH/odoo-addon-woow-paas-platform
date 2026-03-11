---
name: namespace-subdomain-refactor
description: Refactor namespace and subdomain naming to use hash-based identifiers for cross-sandbox uniqueness
status: backlog
created: 2026-03-01T14:35:16Z
---

# PRD: Namespace & Subdomain Naming Refactor

## Executive Summary

統一所有 PaaS 平台的 Kubernetes namespace、service subdomain、smart home subdomain 和 Cloudflare tunnel 的命名規則，從基於流水號 (sequential ID) 改為基於 hash 的命名方式，以消除多 K8s sandbox 環境之間的命名衝突。

## Problem Statement

### 問題描述

目前的命名方式使用 Odoo 資料庫的流水號 (`workspace.id`)，導致：

1. **Namespace 衝突**：`paas-ws-5` 這類命名在不同 sandbox 環境中會重複（每個 sandbox 都有自己的 Odoo DB，ID 都從 1 開始）
2. **Subdomain 衝突**：`paas-5-abc12345` 中的 `5` 同樣是 workspace ID，在共享同一個 Cloudflare DNS 的場景下會衝突
3. **Smart Home 命名不一致**：使用 `sh-{slug}-{random}` 格式，與 cloud service 的 `paas-` prefix 風格不統一
4. **Tunnel 命名冗贅**：`smarthome-sh-{slug}-{random}` 命名過長且格式不統一

### 為什麼現在需要解決

- 已開始使用 K8s dev sandbox 進行並行開發，多 sandbox 共用同一個 K8s cluster
- Smart Home HA Integration 即將合併到 main，需要在合併前修正命名

### 目前的命名方式

| 資源 | 目前格式 | 範例 | 來源 |
|------|---------|------|------|
| K8s Namespace | `paas-ws-{workspace.id}` | `paas-ws-5` | `src/controllers/paas.py:1099` |
| Service Subdomain | `paas-{workspace.id}-{hash[:8]}` | `paas-5-abc12345` | `src/controllers/paas.py:1097` |
| SmartHome Subdomain | `sh-{slug}-{random_hex}` | `sh-myhome-a1b2c3d4` | `src/models/smart_home.py:112` |
| Tunnel Name | `smarthome-{subdomain}` | `smarthome-sh-myhome-a1b2c3d4` | `src/models/smart_home.py:135` |

## User Stories

### US-001: 開發者並行使用多個 Sandbox

**As a** 開發者，**I want** 每個 sandbox 部署的 namespace 名稱不會衝突，**so that** 我可以在同一個 K8s cluster 上同時運行多個 sandbox 而不互相干擾。

**Acceptance Criteria:**
- 不同 sandbox 環境中的相同 workspace name 產生不同的 namespace
- Namespace 名稱在全域範圍內唯一
- Namespace 名稱符合 K8s 命名規範（lowercase, alphanumeric + hyphen, max 63 chars）

### US-002: Service Subdomain 全域唯一

**As a** PaaS 使用者，**I want** 我部署的 service 有一個全域唯一的 subdomain，**so that** 不會因為其他環境的使用者使用了相同 workspace ID 而產生 DNS 衝突。

**Acceptance Criteria:**
- Subdomain 在所有環境中不會衝突
- 命名格式統一，帶有明確的資源類型 prefix

### US-003: Smart Home Subdomain 統一命名風格

**As a** 平台管理員，**I want** 所有 subdomain 使用一致的 `paas-` prefix 命名風格，**so that** 方便管理和識別 Cloudflare DNS records。

**Acceptance Criteria:**
- Smart Home subdomain 遵循 `paas-sm-` prefix
- Tunnel name 與 subdomain 一致
- 舊格式的 subdomain 不會在新建時產生

## Requirements

### Functional Requirements

#### FR-001: 統一命名公式

所有命名使用 workspace slug 的 MD5 hash 前 8 字元作為 workspace 標識：

```
ws_hash = md5(workspace.slug)[:8]
```

**新命名規則：**

| 資源 | 新格式 | 範例 |
|------|--------|------|
| K8s Namespace | `paas-ws-{ws_hash}` | `paas-ws-a3f2b1c4` |
| Service Subdomain | `paas-cs-{ws_hash}-{svc_hash}` | `paas-cs-a3f2b1c4-e7d6f5a8` |
| SmartHome Subdomain | `paas-sm-{ws_hash}-{sm_hash}` | `paas-sm-a3f2b1c4-b9c8d7e6` |
| Tunnel Name | `paas-sm-{ws_hash}-{sm_hash}` | `paas-sm-a3f2b1c4-b9c8d7e6` |

其中：
- `ws_hash` = `md5(workspace.slug).hexdigest()[:8]`
- `svc_hash` = `md5(reference_id + name).hexdigest()[:8]`（沿用現有 salted hash 邏輯）
- `sm_hash` = `md5(reference_id + name).hexdigest()[:8]`（Smart Home 需新增 `reference_id` 欄位）

#### FR-002: Smart Home 新增 `reference_id` 欄位

Smart Home model 需新增 `reference_id` field（UUID），用於 salted hash 產生 subdomain，與 CloudService 保持一致。

#### FR-003: 共用 Hash 工具函式

提取一個共用的 hash 工具函式，供 namespace、service subdomain、smart home subdomain 統一使用：

```python
def generate_ws_hash(workspace) -> str:
    """Generate 8-char hash from workspace slug."""
    return hashlib.md5(workspace.slug.encode()).hexdigest()[:8]

def generate_resource_hash(reference_id: str, name: str) -> str:
    """Generate 8-char salted hash for a resource."""
    return hashlib.md5((reference_id + name).encode()).hexdigest()[:8]
```

#### FR-004: Helm Release Name 保持不變

`helm_release_name` 沿用現有的 `svc-{reference_id[:8]}` 格式，不在此 PRD 範圍內修改。

### Non-Functional Requirements

#### NFR-001: K8s 命名限制
- Namespace 名稱 max 63 chars（`paas-ws-` = 7 chars + 8 hash = 15 chars，OK）
- Subdomain labels max 63 chars（`paas-cs-` = 7 chars + 8 + 1 + 8 = 24 chars，OK）

#### NFR-002: Hash 碰撞風險
- 8 字元 hex = 32 bit = ~43 億種組合
- 對於平台規模（< 10,000 workspaces），碰撞概率極低（birthday problem ~0.001%）
- 若發生碰撞，Odoo UNIQUE constraint 會阻擋並報錯

#### NFR-003: 向後相容
- 現有已部署的 services 和 smart homes 不受影響（不修改現有資料）
- 僅新建的資源使用新命名
- 測試中的硬編碼命名需更新

## Success Criteria

| 指標 | 目標 |
|------|------|
| 多 sandbox 命名衝突 | 0 |
| 命名格式一致性 | 100%（所有新資源遵循 `paas-{type}-` prefix） |
| 現有功能回歸 | 0 test failures（除去已知的 4 個 pre-existing failures） |
| 測試覆蓋 | 新命名邏輯有單元測試 |

## Constraints & Assumptions

### Constraints
- Workspace 必須有 `slug` 欄位（已有，且有 UNIQUE constraint）
- K8s namespace 和 DNS subdomain 只允許 lowercase alphanumeric + hyphen
- 不能中斷已部署的 services（不修改現有 namespace/subdomain 值）

### Assumptions
- 每個 workspace 都有 non-null slug（需驗證 create/update flow）
- 不同 Odoo DB 中的 workspace slug 不會完全相同（跨 sandbox 使用不同 slug）
- Cloudflare DNS 是所有環境共用的（這也是需要全域唯一的原因）

## Out of Scope

- 遷移現有已部署資源的 namespace/subdomain（保持不變）
- 修改 Helm release name 格式（`svc-{uuid[:8]}` 已夠好）
- PaaS Operator 端的命名驗證變更
- 前端顯示格式調整（前端不需要知道命名規則細節）

## Dependencies

### 內部相依
- Smart Home model 需先新增 `reference_id` 欄位
- 需要新的 database migration（`18.0.1.0.3`）

### 受影響的檔案

| 檔案 | 變更內容 |
|------|---------|
| `src/controllers/paas.py` | namespace + service subdomain 命名邏輯 |
| `src/models/smart_home.py` | 新增 `reference_id`，重構 `_generate_subdomain()` |
| `src/tests/test_cloud_service.py` | 更新硬編碼的 namespace/subdomain 值 |
| `src/tests/test_cloud_api.py` | 更新硬編碼的 namespace/subdomain 值 |
| `src/tests/test_paas_operator.py` | 更新硬編碼的 namespace 值 |
| `src/tests/test_smart_home.py` | 更新 subdomain 格式驗證 |
| `src/tests/test_ha_api.py` | 更新 subdomain 格式驗證 |
| `src/migrations/18.0.1.0.3/` | 新 migration for smart_home.reference_id |

### 外部相依
- 無（PaaS Operator 對命名格式無硬性要求，只要符合 K8s 規範）
