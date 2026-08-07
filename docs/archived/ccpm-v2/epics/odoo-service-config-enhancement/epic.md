---
name: odoo-service-config-enhancement
status: backlog
created: 2026-04-03T11:18:56Z
progress: 0%
prd: .claude/prds/odoo-service-config-enhancement.md
github: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/5
---

# Epic: Odoo Service Configuration Enhancement

## Overview

修復 Odoo Cloud Service 部署時 admin 帳密未生效的 bug，新增 SMTP 郵件設定作為租戶可配置選項，並注入系統層 odoo.conf 固定設定（`proxy_mode`, `list_db`）。

## Architecture Decisions

### 1. SMTP 設定直接使用 Bitnami 原生 Helm Values

Bitnami Odoo chart 原生支援 `smtpHost`, `smtpPort`, `smtpUser`, `smtpPassword`, `smtpProtocol` 作為 top-level Helm values。直接在 `helm_value_specs` 新增這些欄位，無需 initContainer 或 extraEnvVars。

### 2. 系統層 odoo.conf 透過 initContainer 注入

沿用現有做法，在 `helm_default_values` 配置 Bitnami chart 的 `initContainers` 來修改 `/opt/bitnami/odoo/conf/odoo.conf`，注入 `proxy_mode=True` 和 `list_db=False`。這些值不在 `helm_value_specs` 中，租戶無法覆寫。

### 3. 前端不需變更

`HelmValueForm` 組件**已完整支援** `select` type（JS: `isSelectField`, `onSelectChange`, `getSelectOptions`；XML: select 渲染模板），SMTP Protocol dropdown 可直接使用。前端 value 傳遞邏輯經代碼審查後邏輯正確，bug 根因更可能在 Bitnami chart 行為或 value 格式層面。

## Technical Approach

### Bug 根因分析

經完整代碼審查，前端→後端→Operator 的 value 傳遞鏈邏輯正確：

1. **前端** `AppConfigurationPage.js:148` — `onHelmValueUpdate()` 正確設定 password 值
2. **前端** `AppConfigurationPage.js:231` — `values: this.state.helmValues` 送出完整 values
3. **後端** `paas.py:1202` — `_filter_allowed_helm_values()` allowed_keys 包含 `odooPassword`
4. **後端** `paas.py:1342` — `_deep_merge()` 正確合併 user values 到 defaults
5. **Operator** `releases.py` — 直接將 values 寫入 YAML 傳給 helm install

**最可能根因**：Bitnami Odoo chart 的 `odooEmail`/`odooPassword` 是建立「額外管理員帳號」而非取代內建的 `admin/admin` 帳號。Odoo 的 `admin` user (login=admin, password=admin) 在 database 初始化時自動建立，Bitnami entrypoint 另外建立 email-based 使用者。需實際部署驗證並決定修復策略（可能需透過 `customPostInitScripts` 禁用/修改 admin 帳號）。

### 檔案變更

| 檔案 | 變更 |
|------|------|
| `src/data/cloud_app_templates.xml` | Odoo template: helm_value_specs 新增 SMTP 欄位, helm_default_values 加入 initContainer + proxy_mode + list_db |
| `src/controllers/paas.py` | `_create_service()` 新增 Odoo password 驗證 |
| `src/migrations/18.0.x.x.x/post-migrate.py` | Migration 更新現有 template data |

## Implementation Strategy

分 3 階段順序執行：

1. **Debug & Fix**：先部署測試確認 bug 根因，再修復
2. **Template Update**：SMTP + 系統設定 + 密碼驗證（一次性更新 template）
3. **Migration & Test**：確保既有環境升級後正確

## Task Breakdown Preview

- [ ] Task 1: Debug 並修復 Odoo admin 帳密 bug — 部署測試 Odoo 服務，檢查 Helm values 是否正確傳遞，確認 Bitnami 行為，實作修復
- [ ] Task 2: 更新 Odoo template — helm_value_specs 新增 SMTP 5 個欄位（smtpHost/Port/User/Password/Protocol），helm_default_values 新增 initContainer 注入 proxy_mode=True + list_db=False
- [ ] Task 3: 新增 Odoo password 驗證 — 在 `_create_service()` 加入 Odoo template 的密碼規則（至少 8 字元），複用現有 n8n 驗證模式
- [ ] Task 4: Data migration — 建立 post-migrate script 更新既有 Odoo template 的 helm_value_specs 和 helm_default_values
- [ ] Task 5: E2E 驗證 — 部署 Odoo 服務驗證：(1) 自訂帳密可登入 (2) SMTP 設定生效 (3) proxy_mode/list_db 已注入

## Dependencies

- **PaaS Operator**：無需變更
- **Bitnami Odoo Chart**：依賴其 SMTP values 和 initContainers 支援（已確認支援）
- **HelmValueForm**：已支援 select type，無需變更
- **cloud_service.js**：API 呼叫邏輯正確，無需變更

## Success Criteria (Technical)

1. Helm values 中包含正確的 `odooPassword` 值（可透過 `helm get values` 驗證）
2. 部署後 Odoo admin 帳號使用自訂 email/password
3. SMTP Helm values 正確傳遞（`smtpHost` 等 top-level keys 存在於 release values）
4. `odoo.conf` 包含 `proxy_mode = True` 和 `list_db = False`
5. 模組升級後既有 template 自動更新

## Estimated Effort

- **Task 1 (Bug Fix)**：需要實際部署測試，估計 2-4h（取決於根因）
- **Task 2-4 (Template + Validation + Migration)**：1-2h（主要是 XML/Python 修改）
- **Task 5 (E2E)**：1h
- **Total**：4-7h

## Tasks Created

- [ ] #6 - Debug 並修復 Odoo admin 帳密 bug (parallel: false, M)
- [ ] #7 - 更新 Odoo template — SMTP 欄位 + 系統層設定 (parallel: false, S, depends: #6)
- [ ] #8 - 新增 Odoo password 驗證 (parallel: true, XS)
- [ ] #9 - Data migration 更新既有 Odoo template (parallel: false, S, depends: #6, #7)
- [ ] #10 - E2E 驗證 (parallel: false, S, depends: #6-#9)

Total tasks: 5
Parallel tasks: 1 (#8 可與 #6 並行)
Sequential tasks: 4
Estimated total effort: 5.5-8.5h

---

> **Promoted to**: `docs/decisions/refactor/odoo-service-config-enhancement.md` (2026-05-19T05:54:44Z)
>
> Architecture Decisions section（3 個決策含 trade-off）升級為長期文件。原 epic 保留作為實作參考。
