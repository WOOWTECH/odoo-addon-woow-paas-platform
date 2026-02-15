---
name: cloud-service-config-restriction
description: Restrict Cloud Service ConfigurationTab to only allow editing template-defined fields via structured form
status: backlog
created: 2026-02-14T16:06:43Z
updated: 2026-02-14T16:11:40Z
epic: .claude/epics/cloud-service-config-restriction/epic.md
---

# PRD: Cloud Service Configuration Restriction

## Executive Summary

將已部署 Cloud Service 的 Configuration Tab 從「開放式 JSON 編輯器」改為「結構化表單」，複用現有的 `HelmValueForm` 元件，確保使用者只能查看和修改 template 的 `helm_value_specs` 定義的合法設定項。後端同步加強驗證，拒絕非法 key 而非靜默丟棄。

**影響範圍**：前端 ConfigurationTab 元件 + 後端 update API + API response 結構

## Problem Statement

### 問題描述

目前 Service Detail 頁的 Configuration Tab 使用原始 JSON textarea 編輯器，暴露所有 `helm_values`，包括：

- 基礎設施設定（`image.registry`, `image.tag`, `resources.limits`）
- 資料庫內部設定（`postgresql.architecture`, `postgresql.auth`）
- 安全相關設定（`global.security.allowInsecureImages`）

使用者可以任意修改這些值，可能導致服務中斷或安全風險。

### 現狀 vs 期望

| 面向 | 現狀 | 期望 |
|------|------|------|
| **唯讀顯示** | 展示所有 helm values（含系統值） | 只顯示 spec 定義的欄位 |
| **編輯模式** | Raw JSON textarea | 結構化表單（HelmValueForm） |
| **後端驗證** | 靜默過濾非法 key（`_filter_allowed_helm_values`） | 明確拒絕非法 key，回傳 error |
| **API Response** | 不含 `helm_value_specs` | 包含 spec 供前端渲染表單 |

### 為什麼現在重要

- 初次部署（AppConfigurationPage）已使用結構化表單，但部署後編輯卻退化為 raw JSON，體驗不一致
- 使用者可能誤改系統值導致服務故障
- 以 Odoo 為首要使用場景，需在用量增加前修正

### 現有後端基礎

後端 `_filter_allowed_helm_values`（`src/controllers/paas.py:952`）已實作 key 白名單過濾邏輯，本次需求是在此基礎上加強為「拒絕而非靜默丟棄」。

## User Stories

### US-001: 查看服務設定

**As a** Workspace Member
**I want to** 在 Configuration Tab 看到簡潔的設定清單
**So that** 我可以快速了解可調整的設定項目，而不被系統層級的 Helm values 干擾

**Acceptance Criteria:**

- [ ] 唯讀模式只顯示 `helm_value_specs` 中 required + optional 定義的欄位
- [ ] 每個欄位顯示 label（非 raw key）和當前值
- [ ] Password 類型欄位顯示為 `********`
- [ ] 不顯示 image、resources、postgresql 等系統值

### US-002: 編輯服務設定

**As a** Workspace Owner
**I want to** 透過結構化表單編輯服務設定
**So that** 我只能修改安全合法的設定，不用擔心改壞系統設定

**Acceptance Criteria:**

- [ ] 點擊 "Edit Configuration" 後顯示 `HelmValueForm` 表單
- [ ] 表單欄位來自 template 的 `helm_value_specs`
- [ ] 欄位初始值從 `service.helm_values` 中提取
- [ ] Required 欄位有驗證，不可為空
- [ ] 儲存後只傳送 spec 定義的 key/value
- [ ] 支援 required 和 optional（advanced）兩層

### US-003: 後端阻擋非法設定

**As a** System
**I want to** 拒絕任何非 spec 白名單中的 key
**So that** 即使繞過前端也無法修改危險的系統設定

**Acceptance Criteria:**

- [ ] `_update_service` 收到非法 key 時回傳明確 error
- [ ] Error message 列出被拒絕的 key
- [ ] 不含非法 key 的請求正常處理

## Requirements

### Functional Requirements

#### FR-1: API Response 擴充 `helm_value_specs`

`_format_service` 的 `include_details=True` 回應需增加：

```json
{
  "template": {
    "id": 1,
    "name": "Odoo",
    "category": "web",
    "helm_value_specs": {
      "required": [
        {"key": "odooEmail", "label": "Administrator Email", "type": "text", "default": "user@example.com"},
        {"key": "odooPassword", "label": "Administrator Password", "type": "password"}
      ],
      "optional": [
        {"key": "loadDemoData", "label": "Load Demo Data", "type": "boolean", "default": false}
      ]
    }
  },
  "helm_values": { ... }
}
```

#### FR-2: ConfigurationTab 唯讀模式

- 移除 `flattenedValues` 邏輯和 raw key/value 列表
- 從 `service.template.helm_value_specs` 取得欄位定義
- 從 `service.helm_values` 中以 spec key 取得對應值
- 以 label + formatted value 呈現

#### FR-3: ConfigurationTab 編輯模式

- 移除 `<textarea>` JSON 編輯器
- 引入 `HelmValueForm` 元件
- 編輯時初始化 `editedValues` 為目前的 spec 欄位值
- 支援 required/optional 分區和 advanced toggle
- 儲存時呼叫 `cloudService.updateService` 傳送 spec key/value

#### FR-4: 後端驗證加強

- 修改 `_update_service` 或 `_filter_allowed_helm_values`
- 當偵測到非法 key 時，回傳 `{'success': False, 'error': 'Unauthorized keys: ...'}`
- Required 欄位空值驗證（可選，Phase 2）

#### FR-5: Odoo Template 維持現有設定

- `odooEmail`（text, required）
- `odooPassword`（password, required）
- `loadDemoData`（boolean, optional）
- 不新增額外欄位

### Non-Functional Requirements

- **安全性**: 後端驗證確保即使繞過前端也無法修改系統設定
- **一致性**: 部署前（AppConfigurationPage）和部署後（ConfigurationTab）使用相同的 HelmValueForm 元件
- **向下相容**: 已部署的 service 仍能正常顯示和編輯；無 spec 的 template 保持原有行為

## Success Criteria

| Metric | Target |
|--------|--------|
| ConfigurationTab 不再顯示 raw JSON 編輯器 | 100% |
| 所有 template 的 Configuration Tab 只顯示 spec 欄位 | 100% |
| 後端拒絕非法 key 的 API 請求 | 100% |
| Odoo template 的 3 個欄位可正常編輯和儲存 | Pass |

## Constraints & Assumptions

### Constraints

- 必須複用現有 `HelmValueForm` 元件，不另建新元件
- Helm values 的 merge 策略維持 shallow merge（`{...existing, ...user}`）
- 已知限制：dot-path key（如 `auth.username`）在 shallow merge 下會作為頂層 key 存在，與 nested 結構並存。此問題不在本 PRD 範圍內

### Assumptions

- `_format_service` 回傳 `include_details=True` 的場景已涵蓋 ServiceDetailPage
- `HelmValueForm` 已支援 required/optional 分區、所有 field type（text, password, boolean, select, number）

## Out of Scope

- 新增 Odoo template 的可配置欄位
- Dot-path key 的 deep merge 機制
- 欄位層級的後端 type 驗證（如 boolean 不接受 string）
- Template 管理介面（admin 修改 helm_value_specs）
- 版本升級 UI

## Dependencies

| Dependency | Type | Status |
|-----------|------|--------|
| `HelmValueForm` 元件 | Internal | ✅ 已存在 |
| `_filter_allowed_helm_values` 後端方法 | Internal | ✅ 已存在（需修改） |
| `cloudService.updateService` 前端 API | Internal | ✅ 已存在 |
| Service Detail API（`_format_service`） | Internal | ✅ 已存在（需擴充） |

## Technical Notes

### 關鍵檔案

| 檔案 | 變更類型 |
|------|----------|
| `src/controllers/paas.py` | 修改 `_format_service`、`_filter_allowed_helm_values` |
| `src/static/src/paas/pages/service/tabs/ConfigurationTab.js` | 重構：引入 HelmValueForm |
| `src/static/src/paas/pages/service/tabs/ConfigurationTab.xml` | 重構：替換 textarea 為 HelmValueForm |
| `src/static/src/paas/services/cloud_service.js` | 可能需調整 `updateService` 參數 |
