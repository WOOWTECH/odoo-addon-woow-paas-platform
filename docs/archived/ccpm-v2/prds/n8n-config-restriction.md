---
name: n8n-config-restriction
description: Define and restrict n8n Cloud Service configuration to safe, user-facing fields via helm_value_specs
status: backlog
created: 2026-02-15T10:14:55Z
updated: 2026-02-15T10:40:37Z
epic: .claude/epics/n8n-config-restriction/epic.md
---

# PRD: n8n Configuration Restriction

## Executive Summary

為 n8n Cloud Service template 定義完整的 `helm_value_specs`，讓使用者在部署和管理 n8n 時，只能透過結構化表單配置安全合法的設定項。複用已完成的 Config Restriction 框架（PR #64），確保 n8n 與 Odoo template 享有一致的配置體驗。

**影響範圍**：n8n template 的 `helm_value_specs` 欄位定義 + `helm_default_values` 調整 + 資料庫 migration

## Problem Statement

### 問題描述

n8n template 目前的 `helm_value_specs` 只定義了 1 個 optional 欄位（`config.database.type`），但 n8n 有多項使用者層級的配置需求：

- **時區設定** — 影響 workflow 排程和 log 時間
- **基本認證** — n8n 預設無登入保護，需要 basic auth
- **加密金鑰** — 用於加密儲存的第三方 credentials

目前這些設定要嘛不存在（使用者無法配置），要嘛需要透過 raw JSON 編輯（已被 Config Restriction 框架阻擋）。

### 現狀 vs 期望

| 面向           | 現狀                           | 期望                      |
| -------------- | ------------------------------ | ------------------------- |
| **可配置欄位** | 1 個 optional（database type） | 3+ required + 3+ optional |
| **安全性**     | 無 basic auth 預設保護         | 部署時強制設定 basic auth |
| **時區**       | 預設 UTC，無法從 UI 調整       | 可選擇時區                |
| **加密金鑰**   | 系統自動產生或空值             | 部署時可自訂加密金鑰      |

### 為什麼現在重要

- Config Restriction 框架已上線，但 n8n 的 spec 定義不完整，使用者配置體驗不佳
- n8n 無 basic auth 保護，部署後任何知道 URL 的人都能存取
- Odoo template 已完成完整的 spec 定義，n8n 應該跟進以維持一致性

### 現有基礎

- Config Restriction 框架已完成（PR #64）：`HelmValueForm` + 後端白名單驗證
- n8n template 已存在於 `src/data/cloud_app_templates.xml`
- 8gears n8n Helm chart 使用 `main.config` 和 `main.secret` 傳遞環境變數

## User Stories

### US-001: 部署 n8n 時設定必要參數

**As a** Workspace Owner
**I want to** 在部署 n8n 時被要求填寫 basic auth 帳密
**So that** 我的 n8n 實例從一開始就有基本的存取保護

**Acceptance Criteria:**

- [ ] AppConfigurationPage 顯示 required 欄位：Basic Auth User、Basic Auth Password
- [ ] 必填欄位不可為空，否則無法部署
- [ ] 部署後 n8n 啟動時即啟用 basic auth

### US-002: 部署後調整 n8n 設定

**As a** Workspace Owner
**I want to** 在 ConfigurationTab 查看和修改 n8n 的可配置項目
**So that** 我可以調整時區、log level 等設定，而不會碰到系統層級的值

**Acceptance Criteria:**

- [ ] ConfigurationTab 唯讀模式顯示所有 spec 定義的欄位 + 當前值
- [ ] 編輯模式使用 `HelmValueForm` 表單
- [ ] Password 類型欄位（basic auth password）顯示為 `********`
- [ ] 不顯示 `main.resources`、`image` 等系統值

### US-003: 確保 n8n 設定安全

**As a** System
**I want to** 只允許 spec 白名單中的 key 被修改
**So that** 使用者無法修改 n8n 的 image、resources、database connection 等系統設定

**Acceptance Criteria:**

- [ ] 後端拒絕 spec 之外的 key（如 `main.resources.limits.cpu`）
- [ ] 只有 `helm_value_specs` 中定義的 key 可被建立和更新
- [ ] 已部署的 n8n 服務在更新 template spec 後，ConfigurationTab 自動反映新欄位

## Requirements

### Functional Requirements

#### FR-1: 定義 n8n helm_value_specs

更新 `cloud_app_templates.xml` 中 n8n template 的 `helm_value_specs`：

```json
{
  "required": [
    {
      "key": "main.secret.N8N_BASIC_AUTH_USER",
      "label": "Basic Auth Username",
      "type": "text",
      "default": "admin"
    },
    {
      "key": "main.secret.N8N_BASIC_AUTH_PASSWORD",
      "label": "Basic Auth Password",
      "type": "password"
    }
  ],
  "optional": [
    {
      "key": "config.database.type",
      "label": "Database Type",
      "type": "select",
      "default": "sqlite",
      "options": ["sqlite", "postgres"]
    },
    {
      "key": "main.config.GENERIC_TIMEZONE",
      "label": "Timezone",
      "type": "select",
      "default": "Asia/Taipei",
      "options": [
        "Asia/Taipei",
        "Asia/Tokyo",
        "Asia/Shanghai",
        "Asia/Hong_Kong",
        "America/New_York",
        "America/Los_Angeles",
        "Europe/London",
        "UTC"
      ]
    },
    {
      "key": "main.config.N8N_LOG_LEVEL",
      "label": "Log Level",
      "type": "select",
      "default": "info",
      "options": ["info", "warn", "error", "debug"]
    }
  ]
}
```

#### FR-2: 更新 helm_default_values

確保 `helm_default_values` 包含所有 required 欄位的合理預設值：

```json
{
  "config": { "database": { "type": "sqlite" } },
  "main": {
    "config": {
      "GENERIC_TIMEZONE": "Asia/Taipei",
      "N8N_LOG_LEVEL": "info",
      "N8N_BASIC_AUTH_ACTIVE": "true"
    },
    "secret": {
      "N8N_BASIC_AUTH_USER": "admin"
    },
    "resources": {
      "requests": { "cpu": "100m", "memory": "256Mi" },
      "limits": { "cpu": "1000m", "memory": "1Gi" }
    }
  }
}
```

注意：`N8N_BASIC_AUTH_ACTIVE` 強制為 `true`，不開放使用者關閉。

#### FR-3: 資料庫 Migration

新增 migration 腳本更新已存在的 n8n template 記錄：

- 更新 `helm_value_specs` 為新定義
- 更新 `helm_default_values` 為新結構
- 不影響已部署的 n8n 服務實例（已部署的 helm_values 保持不變）

#### FR-4: 驗證 dot-path key 在 HelmValueForm 中的行為

- `main.secret.N8N_BASIC_AUTH_USER` 是 dot-path key
- 確認 `HelmValueForm` 正確處理 dot-path key 的讀取和寫入
- 確認後端 `_filter_allowed_helm_values` 正確比對 dot-path key

### Non-Functional Requirements

- **安全性**: 部署時強制設定 basic auth，防止未授權存取
- **一致性**: 與 Odoo template 使用相同的 Config Restriction 框架和 UX
- **向下相容**: 已部署的 n8n 服務不受影響；新的 spec 只影響新部署和後續編輯

## Success Criteria

| Metric                                      | Target                   |
| ------------------------------------------- | ------------------------ |
| n8n template 的 `helm_value_specs` 定義完整 | required: 2, optional: 3 |
| 部署 n8n 時必須設定 basic auth              | 100%                     |
| ConfigurationTab 正確顯示所有 spec 欄位     | Pass                     |
| 後端拒絕 spec 之外的 key                    | Pass                     |
| 已部署 n8n 服務不受 migration 影響          | Pass                     |

## Constraints & Assumptions

### Constraints

- 複用現有 Config Restriction 框架，不修改框架本身
- Helm values 維持 shallow merge 策略
- Dot-path key（如 `main.secret.N8N_BASIC_AUTH_USER`）在 shallow merge 下作為頂層 key 存在
- n8n Helm chart 來自 8gears（`oci://8gears.container-registry.com/library/n8n`），環境變數透過 `main.config` 和 `main.secret` 傳遞

### Assumptions

- `HelmValueForm` 已支援 dot-path key 的正確讀取和顯示
- `_filter_allowed_helm_values` 已正確比對 dot-path key
- n8n 的 `N8N_BASIC_AUTH_ACTIVE` + `N8N_BASIC_AUTH_USER` + `N8N_BASIC_AUTH_PASSWORD` 環境變數組合可正常啟用 basic auth

## Out of Scope

- 修改 Config Restriction 框架本身
- n8n 進階設定（SMTP、webhook URL、外部 database connection）
- n8n worker/webhook 水平擴展設定
- n8n 版本升級 UI
- 其他 template（Redis、PostgreSQL）的 config restriction

## Dependencies

| Dependency                             | Type     | Status    |
| -------------------------------------- | -------- | --------- |
| Config Restriction 框架（PR #64）      | Internal | ✅ 已完成 |
| `HelmValueForm` 元件                   | Internal | ✅ 已存在 |
| `_filter_allowed_helm_values` 後端方法 | Internal | ✅ 已存在 |
| 8gears n8n Helm chart                  | External | ✅ 已整合 |

## Technical Notes

### 關鍵檔案

| 檔案                                        | 變更類型                                                         |
| ------------------------------------------- | ---------------------------------------------------------------- |
| `src/data/cloud_app_templates.xml`          | 修改 n8n template 的 `helm_value_specs` 和 `helm_default_values` |
| `src/migrations/18.0.1.0.3/post-migrate.py` | 新增：更新已存在的 n8n template 記錄                             |
| `src/__manifest__.py`                       | 版本號升級至 `18.0.1.0.3`                                        |

### n8n 環境變數參考

| 環境變數                  | 用途            | Helm Path                          |
| ------------------------- | --------------- | ---------------------------------- |
| `N8N_BASIC_AUTH_ACTIVE`   | 啟用 basic auth | `main.config` （系統強制，不開放） |
| `N8N_BASIC_AUTH_USER`     | Basic auth 帳號 | `main.secret`                      |
| `N8N_BASIC_AUTH_PASSWORD` | Basic auth 密碼 | `main.secret`                      |
| `GENERIC_TIMEZONE`        | 時區設定        | `main.config`                      |
| `N8N_LOG_LEVEL`           | Log 等級        | `main.config`                      |
