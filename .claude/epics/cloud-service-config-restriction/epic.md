---
name: cloud-service-config-restriction
status: completed
created: 2026-02-14T16:11:40Z
updated: 2026-02-14T17:03:47Z
progress: 100%
prd: .claude/prds/cloud-service-config-restriction.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/59
---

# Epic: cloud-service-config-restriction

## Overview

將 ConfigurationTab 從 raw JSON 編輯器改為基於 `helm_value_specs` 的結構化表單。核心策略：**複用現有 `HelmValueForm` 元件**，只需擴充 API response 並重構 ConfigurationTab，後端加強驗證從「靜默丟棄」升級為「明確拒絕」。

變更量小，共涉及 4 個檔案，無新增檔案。

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| 前端表單元件 | 複用 `HelmValueForm` | 已在 AppConfigurationPage 驗證，支援所有 field type |
| 後端驗證策略 | 修改 `_filter_allowed_helm_values` | 已有白名單邏輯，只需改回傳行為 |
| API 擴充方式 | 在 `_format_service` 的 template 欄位加入 specs | 最小改動，不新增 API endpoint |
| Merge 策略 | 維持 shallow merge | 目前所有 template 的 spec key 都是頂層 key，shallow merge 足夠 |

## Technical Approach

### Task 1: 後端 — API Response 擴充 + 驗證加強

**檔案**: `src/controllers/paas.py`

**1a. `_format_service` 擴充**（~5 行）

在 `include_details=True` 時，template 區塊加入 `helm_value_specs`：

```python
# 現有
'template': {
    'id': service.template_id.id,
    'name': service.template_id.name,
    'category': service.template_id.category,
}
# 新增
'template': {
    ...
    'helm_value_specs': json.loads(service.template_id.helm_value_specs)
        if service.template_id.helm_value_specs else {'required': [], 'optional': []},
}
```

**1b. `_filter_allowed_helm_values` 加強**（~10 行）

從靜默過濾改為拒絕：

```python
# 現有：靜默丟棄
_logger.debug("Filtered out non-allowed Helm value key '%s'", key)

# 改為：收集非法 key 並 raise 或回傳
rejected_keys = [k for k in values if k not in allowed_keys]
if rejected_keys:
    return None, rejected_keys  # 呼叫端檢查
```

**1c. `_update_service` 調整**（~5 行）

檢查 filter 結果，有非法 key 時回傳 error。

### Task 2: 前端 — ConfigurationTab 重構

**檔案**: `ConfigurationTab.js` + `ConfigurationTab.xml`

**2a. 唯讀模式**

- 移除 `flattenedValues` getter 和 `formatKey`/`isObject`/`isArray`/`formatValue` 輔助方法
- 新增：從 `service.template.helm_value_specs` 取 specs，從 `service.helm_values` 取值
- 用 label + value 方式呈現（password 顯示 `********`）

**2b. 編輯模式**

- 移除 `<textarea>`、`editedValuesJson`、`onValuesChange` 相關邏輯
- 引入 `HelmValueForm` 元件
- 新增 `editedValues` state（Object 型態，非 JSON string）
- 新增 `onHelmValueUpdate(key, value)` handler
- 新增 `showAdvanced` state + toggle
- `saveChanges` 改為傳送 `editedValues` Object（非 JSON.parse）

**2c. XML template**

- 唯讀：spec 欄位清單取代 `flattenedValues` loop
- 編輯：`<HelmValueForm>` 取代 `<textarea>`
- 保留 advanced toggle 和 error/saving 狀態

### Task 3: 前端 — cloud_service.js 確認

**檔案**: `src/static/src/paas/services/cloud_service.js`

確認 `updateService` 已接受 Object 型態的 values（預計不需修改，因為目前已是 `JSON.parse` 後傳送）。

## Implementation Strategy

**單一開發階段**，不需分 phase：

1. 後端改動先做（API response + 驗證）— 向下相容，不影響現有前端
2. 前端 ConfigurationTab 重構 — 一次性替換
3. 手動測試 Odoo template 的 3 個欄位：建立、查看、編輯、儲存

**風險與緩解**：

| 風險 | 緩解 |
|------|------|
| 無 spec 的舊 template 顯示空白 | Fallback：spec 為空時顯示「No configurable settings」 |
| 後端拒絕 breaking existing clients | 保留向下相容：無 spec 時維持允許全部 key |

## Task Breakdown Preview

- [ ] Task 1: 後端 — `_format_service` 擴充 template 回應含 `helm_value_specs`
- [ ] Task 2: 後端 — `_filter_allowed_helm_values` 改為拒絕非法 key + `_update_service` 調整
- [ ] Task 3: 前端 — ConfigurationTab 唯讀模式重構（spec-based 顯示）
- [ ] Task 4: 前端 — ConfigurationTab 編輯模式重構（HelmValueForm 取代 textarea）

## Dependencies

| Dependency | Status |
|-----------|--------|
| `HelmValueForm` 元件（`src/static/src/paas/components/config/HelmValueForm.js`） | ✅ 已存在 |
| `_filter_allowed_helm_values`（`src/controllers/paas.py:952`） | ✅ 已存在 |
| `cloudService.updateService`（`src/static/src/paas/services/cloud_service.js`） | ✅ 已存在 |
| `_format_service`（`src/controllers/paas.py:1420`） | ✅ 已存在 |

## Success Criteria (Technical)

| Criteria | Verification |
|----------|-------------|
| ConfigurationTab 唯讀模式只顯示 spec 欄位 | Odoo service 只顯示 3 個欄位 |
| 編輯模式使用 HelmValueForm | 無 textarea 出現 |
| 後端拒絕非法 key | `curl` 傳送 `{"image.tag": "..."}` 回傳 error |
| 無 spec template 向下相容 | 顯示「No configurable settings」，不 crash |

## Estimated Effort

- **總量**：~3-4 小時
- **後端**：~1 小時（API response 擴充 + 驗證修改）
- **前端**：~2-3 小時（ConfigurationTab JS + XML 重構）
- **測試**：~30 分鐘（手動驗證 Odoo template flow）
- **Critical Path**：後端 API 擴充 → 前端重構（序列依賴）

## Tasks Created

- [ ] #60 - Backend: Extend _format_service to include helm_value_specs (parallel: false)
- [ ] #61 - Backend: Reject unauthorized helm value keys (parallel: false, depends: #60)
- [ ] #62 - Frontend: ConfigurationTab read-only mode refactor (parallel: false, depends: #60)
- [ ] #63 - Frontend: ConfigurationTab edit mode with HelmValueForm (parallel: false, depends: #60, #62)

Total tasks: 4
Parallel tasks: 0
Sequential tasks: 4
Estimated total effort: 4 hours

```
#60 → #61 (same file: paas.py)
#60 → #62 → #63 (same files: ConfigurationTab.js/xml)
```
