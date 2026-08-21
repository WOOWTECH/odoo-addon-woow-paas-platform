---
title: Odoo Service Configuration (feature spec)
category: reference
status: active
created: 2026-05-19T05:54:44Z
updated: 2026-05-19T05:54:44Z
source: docs/archived/ccpm-v2/prds/odoo-service-config-enhancement.md
promoted-at: 2026-05-19T05:54:44Z
related:
  - docs/decisions/refactor/odoo-service-config-enhancement.md
  - docs/archived/ccpm-v2/epics/odoo-service-config-enhancement/epic.md
---

# Odoo Service Configuration

> 此檔於 2026-05-19 由 ccpm PRD 升級而來（ccpm 已於 2026-08-07 退役，原始 PRD 見 [`docs/archived/ccpm-v2/prds/odoo-service-config-enhancement.md`](../../archived/ccpm-v2/prds/odoo-service-config-enhancement.md)）。**本 feature 已實作落地**（epic 已 archived、對應 issues #5–#10 closed）；實作落點見 `src/data/cloud_app_templates.xml` 的 `helm_value_specs` 與 `customPostInitScripts`、`src/controllers/paas.py`。下方 Acceptance Criteria 為原始目標規格，保留作脈絡。

PaaS 內 Odoo Cloud Service 的可配置設定。涵蓋租戶層（admin credentials、SMTP）與系統層（`proxy_mode`、`list_db`）兩種類別。架構決策見 [`../../decisions/refactor/odoo-service-config-enhancement.md`](../../decisions/refactor/odoo-service-config-enhancement.md)。

## User Stories

### US-1：管理員帳密正確寫入

**As a** Workspace 擁有者
**I want** 在建立 Odoo 服務時填寫的 Administrator Email 和 Password 能在部署後生效
**So that** 我可以用自訂的帳密登入 Odoo，而不是預設的 admin/admin

**Acceptance Criteria**：

- [ ] 在建立 Odoo 服務時填入 Email: `test@example.com`、Password: `MySecure123!`
- [ ] 部署完成後，可用 `test@example.com` / `MySecure123!` 登入 Odoo
- [ ] 預設的 `admin/admin` 帳號不可用或密碼已被變更
- [ ] Password 欄位需有基本驗證（至少 8 字元）

### US-2：SMTP 郵件設定

**As a** Workspace 擁有者
**I want** 在建立或設定 Odoo 服務時能填入 SMTP 郵件設定
**So that** Odoo 的通知、密碼重設等郵件功能能正常運作

**Acceptance Criteria**：

- [ ] 建立 Odoo 服務的 UI 顯示 SMTP 設定區塊（作為 optional settings）
- [ ] 支援設定：SMTP Host、Port、User、Password、Protocol（SSL / TLS / None）
- [ ] SMTP 設定正確傳遞到 Bitnami chart 的 Helm values
- [ ] 部署後 Odoo 的 outgoing mail server 已自動配置
- [ ] 不填 SMTP 設定時，Odoo 正常部署（郵件功能未啟用）

### US-3：設定分級（系統層 vs 租戶層）

**As a** PaaS 管理員
**I want** 某些 `odoo.conf` 設定由系統固定，租戶無法修改
**So that** 確保平台安全性與一致性

**Acceptance Criteria**：

- [ ] `proxy_mode=True` 固定在 `helm_default_values` 中，租戶不可覆寫
- [ ] `list_db=False` 固定在 `helm_default_values` 中，租戶不可覆寫
- [ ] 系統層設定不出現在租戶 UI 上
- [ ] 租戶層設定（Email/Password/SMTP）透過 `helm_value_specs` 暴露

## SMTP 欄位規格

| 欄位 | Type | Label | Default | Required |
|------|------|-------|---------|----------|
| `smtpHost` | text | SMTP Server | — | optional |
| `smtpPort` | text | SMTP Port | `587` | optional |
| `smtpUser` | text | SMTP Username | — | optional |
| `smtpPassword` | password | SMTP Password | — | optional |
| `smtpProtocol` | select | SMTP Protocol | `starttls` | optional（選項：`ssl` / `starttls` / `none`） |

## 系統層強制設定

不暴露於 UI，透過 `helm_default_values` 內的 initContainer 注入：

```ini
[options]
proxy_mode = True
list_db = False
```

## Success Criteria（可量測）

1. **帳密 Bug 修復**：部署 Odoo 後可用自訂帳密登入，`admin/admin` 不可用
2. **SMTP 可用**：設定 SMTP 後，Odoo 可成功寄出測試信件
3. **系統層隔離**：租戶 UI 上看不到 `proxy_mode`、`list_db` 等系統設定
4. **既有服務不受影響**：模組升級後已部署的服務正常運作

## Non-Functional

- **向後相容**：已部署服務不受影響；SMTP 為 optional
- **安全性**：`odooPassword` 與 `smtpPassword` 在 API response 中不回傳明文；SMTP 密碼由 Bitnami 處理為 K8s Secret
- **可維護性**：新增欄位遵循現有 `helm_value_specs` JSON schema

## Out of Scope

- 方案 / 計畫機制（不同租戶差異化設定）
- `odoo.conf` 進階設定（workers、memory limits、`db_maxconn`）
- 已部署服務的設定更新（屬於獨立 feature）
- SMTP 連線測試功能（UI 上的「測試寄信」按鈕）

## Dependencies

- **PaaS Operator**：無需變更，`install_release()` 已支援任意 Helm values 傳遞
- **Bitnami Odoo Chart**：依賴其 SMTP values 與 entrypoint script 的正確行為（admin 帳號機制需研究）
- **HelmValueForm 組件**：經 audit 已支援 select type（見 [decision D3](../../decisions/refactor/odoo-service-config-enhancement.md#d3前端-helmvalueform-不需變更)）

## Constraints

- Bitnami Odoo chart 不支援直接透過 Helm values 設定 `odoo.conf`，必須依賴 initContainer
- Odoo 的 admin 使用者機制由 Bitnami entrypoint script 控制，需研究其確切行為
