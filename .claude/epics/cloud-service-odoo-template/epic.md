---
name: cloud-service-odoo-template
status: completed
created: 2026-02-08T05:37:43Z
updated: 2026-02-14T15:50:56Z
progress: 100%
prd: .claude/prds/cloud-service-odoo-template.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/16
---

# Epic: cloud-service-odoo-template

## Overview

在現有 Cloud Services 架構上新增 Odoo 應用模板。這是一個純資料層變更 — 新增一筆 XML seed record 到 `cloud_app_templates.xml`，讓 Odoo 出現在 Marketplace 中供用戶部署。

現有架構（PaaS Operator、Controller、Frontend、CloudService model）已完全支援新模板的部署流程，無需任何程式碼修改。

## Architecture Decisions

- **使用 Bitnami OCI chart**：`oci://registry-1.docker.io/bitnamicharts/odoo`，Bitnami 商業維護、定期更新安全補丁
- **內建 PostgreSQL**：使用 chart 自帶的 PostgreSQL（standalone 模式），降低部署複雜度
- **Service Type ClusterIP**：搭配現有 Cloudflare Tunnel expose 機制對外暴露服務
- **不修改任何程式碼**：`web` category 已存在於 model selection，只需新增 XML data record
- **資源配置保守**：requests 200m CPU / 512Mi RAM，limits 2000m CPU / 2Gi RAM，平衡效能與資源利用

## Technical Approach

### 變更範圍

| 檔案 | 變更 | 說明 |
|------|------|------|
| `src/data/cloud_app_templates.xml` | 新增 record | 新增 `cloud_app_odoo` template record（~35 行 XML） |

### 不需要變更

- `src/models/cloud_app_template.py` — `web` category 已存在（行 39）
- `src/controllers/paas.py` — 通用 template/service CRUD，自動支援新模板
- `src/services/paas_operator.py` — 已支援 OCI chart
- Frontend OWL 元件 — Marketplace UI 動態渲染，無需改動

### Odoo Template Record 設計

```xml
<record id="cloud_app_odoo" model="woow_paas_platform.cloud_app_template">
    <field name="name">Odoo</field>
    <field name="slug">odoo</field>
    <field name="category">web</field>
    <field name="helm_chart_name">oci://registry-1.docker.io/bitnamicharts/odoo</field>
    <field name="default_port">8069</field>
    <field name="ingress_enabled" eval="True"/>
    <field name="min_vcpu">2</field>
    <field name="min_ram_gb">2.0</field>
    <field name="min_storage_gb">10</field>
</record>
```

Key Helm values:
- `odooEmail`、`odooPassword`、`loadDemoData` 為用戶可配置欄位
- `service.type: ClusterIP` 搭配 Cloudflare Tunnel
- 內建 PostgreSQL standalone，自動建立 `bitnami_odoo` 資料庫

## Task Breakdown Preview

- [x] Task 1: 新增 Odoo template XML record 到 `cloud_app_templates.xml`
- [x] Task 2: 部署驗證 — 更新模組並確認 Marketplace 顯示正確、可成功部署

## Dependencies

### External
- Bitnami Odoo Helm chart 可從 Docker Hub OCI registry 拉取
- K8s cluster 有足夠資源（至少 2 vCPU、2GB RAM、10GB storage 可用）

### Internal（全部已完成）
- Cloud Services MVP（PR #15 已合併）
- PaaS Operator 已部署
- Marketplace UI 已實作

## Success Criteria (Technical)

| Criteria | Target |
|----------|--------|
| 模組更新後 Marketplace 顯示 Odoo 卡片 | Pass |
| 點擊 Odoo 可看到正確的配置表單（email、密碼、demo data） | Pass |
| 透過 Marketplace 成功部署 Odoo 實例 | Pass |
| 部署完成後可透過 subdomain 訪問 Odoo Web UI | Pass |
| 可使用設定的 email/密碼登入 | Pass |

## Estimated Effort

- **總工時**：約 1-2 小時
- **變更量**：~35 行 XML（1 個檔案）
- **風險**：極低 — 純資料新增，不影響現有功能
- **關鍵路徑**：部署驗證取決於 K8s cluster 可用性和 OCI registry 連線

## Tasks Created

- [x] #17 - Add Odoo template XML record (parallel: false)
- [x] #18 - Deployment verification (parallel: false, depends_on: #17)

Total tasks: 2
Parallel tasks: 0
Sequential tasks: 2
Estimated total effort: 1.5 hours
