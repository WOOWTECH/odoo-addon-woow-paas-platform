# project cloud odoo addon selection

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_cloud_odoo_addon_selection`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Odoo Cloud Service 自訂 addon 選擇功能的架構決策和跨 repo 分工

## Odoo Cloud Service 自訂 Addon 選擇

**日期**: 2026-04-04 ~ 2026-04-05

### 架構決策

- 兩個獨立 image: `paas-odoo`（平台自用）和 `cloud-odoo`（使用者 Cloud Service）
- `cloud-odoo` image 包含所有 candidate addons 在 `/all-addons/`
- `paas-odoo` image 只包含 `addon-catalog.json` metadata（無實際 addon 檔案）
- initContainer 用 cloud-odoo image 本身（不是 busybox），因為 `/all-addons/` 在裡面
- 不自動 `odoo -i` 安裝 addon，使用者在 Odoo Apps UI 自行啟用
- `addons_path` 修改放在 initContainer（mount odoo-data PVC）+ customPostInitScripts 雙保險
- `CloudOdooAddon` records 從 `/all-addons/catalog.json` 動態 sync，不用靜態 XML seed data
- sync 時不在 catalog 的 addon 會被 deactivate

### 跨 Repo 分工

| Repo | 職責 |
|------|------|
| paas-ci | Dockerfile + Dockerfile.cloud，candidate-addons/，addon-catalog.json 自動產生 |
| paas-platform | CloudOdooAddon model，sync API，helm values 組裝（initContainers + extraVolumes），前端 AddonSelector |
| paas-charts | 不需改動（Bitnami chart 原生支援 initContainers/extraVolumes） |
| paas-cd | 不需改動（Cloud Service 走 paas-operator 不經 Fleet） |

**Why:** 讓使用者建立 Odoo Cloud Service 時可選擇需要的 addon，而非所有人用同一套固定 addon。
**How to apply:** 新增 candidate addon 時只需放到 paas-ci 的 `candidate-addons/` 目錄，CI 自動產生 catalog，平台 sync API 自動建立 records。
