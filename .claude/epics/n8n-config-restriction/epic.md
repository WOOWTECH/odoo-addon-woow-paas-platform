---
name: n8n-config-restriction
status: backlog
created: 2026-02-15T10:40:37Z
progress: 0%
prd: .claude/prds/n8n-config-restriction.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/65
---

# Epic: n8n Configuration Restriction

## Overview

為 n8n Cloud Service template 定義完整的 `helm_value_specs`，新增 basic auth 必填欄位和時區/log level 選填欄位。這是純 data 層面的變更，完全複用已完成的 Config Restriction 框架（PR #64），不需修改任何前後端程式碼。

## Architecture Decisions

1. **不修改框架** — Config Restriction 框架（HelmValueForm + 後端白名單）已支援所有需要的 field type（text, password, select, boolean），直接定義 spec 即可
2. **強制 basic auth** — `N8N_BASIC_AUTH_ACTIVE` 寫入 `helm_default_values` 且不列入 `helm_value_specs`，使用者無法關閉
3. **Dot-path key** — n8n chart 使用 `main.config.*` 和 `main.secret.*` 結構，需驗證現有框架對 dot-path key 的相容性
4. **Migration 策略** — 只更新 template 記錄的 spec/defaults，不影響已部署的 service 實例

## Technical Approach

### 變更範圍

此 epic 只涉及 3 個檔案：

| 檔案 | 類型 | 說明 |
|------|------|------|
| `src/data/cloud_app_templates.xml` | 修改 | 更新 n8n template 的 `helm_value_specs` + `helm_default_values` |
| `src/migrations/18.0.1.0.3/post-migrate.py` | 新增 | Migration 腳本更新已存在的 n8n template 記錄 |
| `src/__manifest__.py` | 修改 | 版本號 `18.0.1.0.2` → `18.0.1.0.3` |

### 無需修改的部分

以下部分已由 Config Restriction 框架處理，無需變更：

- `HelmValueForm` 元件 — 自動根據 spec 渲染表單
- `ConfigurationTab` — 已用 HelmValueForm 替代 textarea
- `AppConfigurationPage` — 已根據 spec 顯示部署表單
- `_filter_allowed_helm_values` — 已實作白名單過濾
- `_parse_helm_value_specs` — 已實作 spec 解析
- API response — 已包含 `helm_value_specs`

## Implementation Strategy

### Phase 1: Dot-path key 驗證（風險最高）

在實作前，先驗證 dot-path key（如 `main.secret.N8N_BASIC_AUTH_USER`）在以下場景的行為：

1. `HelmValueForm` 讀取初始值 — 是否能從 `service.helm_values` 中正確取得 dot-path key 的值？
2. `HelmValueForm` 提交 — 提交的 values 是否保持 dot-path key 格式？
3. `_filter_allowed_helm_values` 比對 — 是否正確辨識 dot-path key？
4. Helm chart 渲染 — dot-path key 作為頂層 key（shallow merge）是否能被 n8n chart 正確解析？

如果 dot-path key 不相容，需要調整 key 格式（如改用 flat key）。

### Phase 2: 實作與測試

更新 XML data + 建立 migration + 版本升級。

## Task Breakdown Preview

- [ ] Task 1: 驗證 dot-path key 在 HelmValueForm 和後端的行為
- [ ] Task 2: 更新 n8n template data（helm_value_specs + helm_default_values + migration + version bump）
- [ ] Task 3: E2E 驗證（部署新 n8n + 編輯已部署 n8n 設定）

## Dependencies

| Dependency | Status |
|-----------|--------|
| Config Restriction 框架（PR #64） | ✅ 已完成 |
| HelmValueForm 元件 | ✅ 已存在 |
| 後端白名單驗證 | ✅ 已存在 |
| 8gears n8n Helm chart | ✅ 已整合 |

## Success Criteria (Technical)

| 測試項目 | 預期結果 |
|----------|----------|
| 部署新 n8n 時，AppConfigurationPage 顯示 2 required + 3 optional 欄位 | Pass |
| 部署後 n8n 啟用 basic auth，需登入才能使用 | Pass |
| ConfigurationTab 以 HelmValueForm 顯示 5 個欄位 | Pass |
| 修改 timezone 後儲存並重啟，n8n 反映新時區 | Pass |
| 送出非法 key（如 `main.resources.limits.cpu`）被後端拒絕 | Pass |
| 已部署的 n8n 服務在 module 升級後正常運作 | Pass |

## Tasks Created

- [ ] #66 - Verify dot-path key compatibility in Config Restriction framework (parallel: false)
- [ ] #67 - Update n8n template data and create migration (parallel: false, depends on: #66)
- [ ] #68 - E2E verification for n8n config restriction (parallel: false, depends on: #67)

Total tasks: 3
Parallel tasks: 0
Sequential tasks: 3
Estimated total effort: 3-6 hours

## Estimated Effort

- **整體時程**: 0.5-1 天
- **風險**: 低（純 data 變更），中等風險在 dot-path key 相容性
- **Critical Path**: dot-path key 驗證 → 如不相容需調整 key 策略
