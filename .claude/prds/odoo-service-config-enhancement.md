---
name: odoo-service-config-enhancement
description: Fix Odoo admin credential bug and add SMTP configuration for PaaS tenants
status: backlog
created: 2026-04-03T11:13:08Z
---

# PRD: Odoo Service Configuration Enhancement

## Executive Summary

修復 Cloud Service 部署 Odoo 時，使用者填寫的 Administrator Email / Password 未正確寫入的 bug（部署後仍為 admin/admin），並新增 SMTP 郵件設定作為租戶可配置項，讓 Odoo 租戶能在 PaaS UI 上設定寄信功能。

## Problem Statement

### Bug：Admin 帳密未生效

使用者在 `/woow` 前端 UI 建立 Odoo 服務時，於 Application Settings 頁面填入 Administrator Email 和 Administrator Password，但部署完成後 Odoo 的管理員帳號仍為預設的 `admin/admin`。

**根因分析：**

Bitnami Odoo chart 使用 `odooEmail` 和 `odooPassword` 作為 Helm values，在首次安裝時透過 entrypoint script 建立管理員帳號。目前的 value 傳遞流程：

```
Frontend UI (使用者填寫)
  → API: _create_service() 
    → _filter_allowed_helm_values() 過濾
      → _deep_merge(default_values, user_values) 合併
        → CloudService.helm_values 儲存
          → PaaS Operator → helm install --values
```

需要確認的根因候選：
1. **前端未正確傳送值** — `AppConfigurationPage.js` 的 `initializeHelmValues()` 可能未正確收集 password 欄位
2. **合併邏輯問題** — `_deep_merge()` 或 `_filter_allowed_helm_values()` 可能過濾掉了 `odooPassword`
3. **Helm values 格式問題** — Bitnami chart 對 `odooPassword` 有特殊處理（空值時使用隨機密碼，但 Odoo 的 admin user 可能仍以 admin/admin 存在）
4. **Bitnami chart 的 admin 帳號機制** — Odoo 的 `admin` 使用者是 database 初始化時自動建立的，Bitnami 的 `odooEmail`/`odooPassword` 可能是建立「額外」管理員而非取代 `admin`

### 功能缺失：SMTP 郵件設定

Odoo 租戶目前無法在 PaaS UI 上設定 SMTP 寄信，導致 Odoo 內建的通知、密碼重設等郵件功能無法使用。Bitnami chart 原生支援 SMTP Helm values（`smtpHost`, `smtpPort`, `smtpUser`, `smtpPassword`, `smtpProtocol`），但目前 template 的 `helm_value_specs` 沒有暴露這些欄位。

## User Stories

### US-1：管理員帳密正確寫入

**作為** Workspace 擁有者
**我希望** 在建立 Odoo 服務時填寫的 Administrator Email 和 Password 能在部署後生效
**以便於** 我可以用自訂的帳密登入 Odoo，而不是預設的 admin/admin

**驗收標準：**
- [ ] 在建立 Odoo 服務時填入 Email: `test@example.com`、Password: `MySecure123!`
- [ ] 部署完成後，可用 `test@example.com` / `MySecure123!` 登入 Odoo
- [ ] 預設的 `admin/admin` 帳號不可用或密碼已被變更
- [ ] Password 欄位需有基本驗證（至少 8 字元）

### US-2：SMTP 郵件設定

**作為** Workspace 擁有者
**我希望** 在建立或設定 Odoo 服務時能填入 SMTP 郵件設定
**以便於** Odoo 的通知、密碼重設等郵件功能能正常運作

**驗收標準：**
- [ ] 建立 Odoo 服務的 UI 顯示 SMTP 設定區塊（作為 optional settings）
- [ ] 支援設定：SMTP Host, Port, User, Password, Protocol (SSL/TLS/None)
- [ ] SMTP 設定正確傳遞到 Bitnami chart 的 Helm values
- [ ] 部署後 Odoo 的 outgoing mail server 已自動配置
- [ ] 不填 SMTP 設定時，Odoo 正常部署（郵件功能未啟用）

### US-3：設定分級（系統層 vs 租戶層）

**作為** PaaS 管理員
**我希望** 某些 odoo.conf 設定由系統固定，租戶無法修改
**以便於** 確保平台安全性與一致性

**驗收標準：**
- [ ] `proxy_mode=True` 固定在 `helm_default_values` 中，租戶不可覆寫
- [ ] `list_db=False` 固定在 `helm_default_values` 中，租戶不可覆寫
- [ ] 系統層設定不出現在租戶 UI 上
- [ ] 租戶層設定（Email/Password/SMTP）透過 `helm_value_specs` 暴露

## Requirements

### Functional Requirements

#### FR-1：修復 Admin 帳密 Bug

1. **Debug 根因**：追蹤從前端到 Helm install 的完整 value 傳遞鏈，找出 `odooPassword` 遺失的環節
2. **修復 value 傳遞**：確保 `odooEmail` 和 `odooPassword` 正確合併到 `helm_values` 並傳給 PaaS Operator
3. **Password 驗證**：新增 Odoo 密碼驗證規則（至少 8 字元），類似 n8n 的驗證邏輯
4. **驗證 Bitnami 行為**：確認 Bitnami chart 收到正確 values 後是否能正確設定 admin 帳號（非建立額外使用者）

#### FR-2：新增 SMTP 設定欄位

1. **擴充 `helm_value_specs`**：在 Odoo template 的 optional 區塊新增 SMTP 欄位：
   - `smtpHost`（text, label: "SMTP Server"）
   - `smtpPort`（text, label: "SMTP Port", default: "587"）
   - `smtpUser`（text, label: "SMTP Username"）
   - `smtpPassword`（password, label: "SMTP Password"）
   - `smtpProtocol`（select, label: "SMTP Protocol", options: ["ssl", "starttls", "none"], default: "starttls"）
2. **擴充 `helm_default_values`**：不包含 SMTP 預設值（空值 = 未啟用）
3. **前端 UI**：`HelmValueForm` 組件已支援動態渲染 `helm_value_specs`，需確認 `select` type 是否已實作

#### FR-3：系統層固定設定

1. **透過 initContainer 注入 odoo.conf**：沿用現有做法，在 `helm_default_values` 中設定 initContainer 來寫入固定的 odoo.conf 參數：
   - `proxy_mode = True`
   - `list_db = False`
2. **確保不被覆寫**：這些設定不在 `helm_value_specs` 中，租戶無法透過 UI 修改
3. **Migration**：需提供 data migration 更新現有 Odoo template 的 `helm_default_values`

### Non-Functional Requirements

#### NFR-1：向後相容
- 已部署的 Odoo 服務不受影響（只影響新建立的服務）
- SMTP 設定為 optional，不填不影響部署

#### NFR-2：安全性
- `odooPassword` 和 `smtpPassword` 在 API response 中不回傳明文（現有 `_format_service` 已處理）
- SMTP 密碼在 Helm values 中以 Kubernetes Secret 儲存（由 Bitnami chart 處理）

#### NFR-3：可維護性
- 新增的設定遵循現有的 `helm_value_specs` JSON schema 模式
- Migration script 確保模組升級時自動更新 template

## Technical Design

### 實作方式

#### SMTP 設定傳遞

Bitnami Odoo chart 原生支援以下 Helm values：

```yaml
smtpHost: "smtp.gmail.com"
smtpPort: "587"
smtpUser: "user@gmail.com"
smtpPassword: "app-password"
smtpProtocol: "starttls"
```

這些值直接作為 top-level Helm values 傳遞，無需 initContainer 或額外處理。

#### 系統層 odoo.conf 設定

沿用現有 initContainer 做法，在 `helm_default_values` 中配置 initContainer 來修改 `/etc/odoo/odoo.conf`，注入：

```ini
[options]
proxy_mode = True
list_db = False
```

#### 前端 HelmValueForm 組件

需確認 `select` type 的支援度（用於 SMTP Protocol 選擇）。若未支援，需擴充 `HelmValueForm` 組件新增 select/dropdown 渲染。

### 檔案變更清單

| 檔案 | 變更 |
|------|------|
| `src/data/cloud_app_templates.xml` | 更新 Odoo template: helm_value_specs 新增 SMTP, helm_default_values 新增系統層設定 |
| `src/migrations/18.0.x.x.x/post-migrate.py` | Migration 更新現有 template |
| `src/controllers/paas.py` | 新增 Odoo password 驗證邏輯 |
| `src/static/src/paas/components/helm-value-form/*` | 若需支援 select type |
| `src/static/src/paas/pages/configure/AppConfigurationPage.*` | Debug 前端 value 傳遞 |

## Success Criteria

1. **帳密 Bug 修復**：部署 Odoo 後可用自訂帳密登入，admin/admin 不可用
2. **SMTP 可用**：設定 SMTP 後，Odoo 可成功寄出測試信件
3. **系統層隔離**：租戶 UI 上看不到 `proxy_mode`、`list_db` 等系統設定
4. **既有服務不受影響**：模組升級後已部署的服務正常運作

## Constraints & Assumptions

### Constraints
- Bitnami Odoo chart 不支援直接透過 Helm values 設定 `odoo.conf`，需依賴 initContainer
- Odoo 的 admin 使用者機制由 Bitnami entrypoint script 控制，需研究其確切行為

### Assumptions
- Bitnami chart 的 `smtpHost`/`smtpPort` 等值能正確配置 Odoo 的 outgoing mail server
- 現有的 `_filter_allowed_helm_values()` 和 `_deep_merge()` 邏輯在修復後能正確處理所有欄位
- initContainer 方式可以正確修改 Bitnami 容器內的 odoo.conf

## Out of Scope

- **方案/計畫機制**：不同租戶差異化設定（未來 PRD）
- **odoo.conf 進階設定**：workers、memory limits、db_maxconn 等（未來按需添加）
- **已部署服務的設定更新**：本次只處理新建立的服務，existing service 的 config update 是獨立 feature
- **SMTP 連線測試功能**：UI 上的「測試寄信」按鈕

## Dependencies

- **PaaS Operator**：無需變更，現有的 `install_release()` 已支援任意 Helm values 傳遞
- **Bitnami Odoo Chart**：依賴其 SMTP values 和 entrypoint script 的正確行為
- **HelmValueForm 組件**：可能需擴充 select type 支援

---

> **Promoted to**: `docs/reference/features/odoo-service-config.md` (2026-05-19T05:54:44Z)
>
> User Stories（US-1/2/3 含 acceptance criteria）+ Success Criteria + SMTP 欄位規格升級為長期 feature spec。原 PRD 保留作為需求來源。
