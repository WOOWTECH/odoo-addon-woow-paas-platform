# project cloud odoo addon selection

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_cloud_odoo_addon_selection`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Odoo Cloud Service 自訂 addon 選擇功能的架構決策和跨 repo 分工

## Odoo Cloud Service 自訂 Addon 選擇

> ⚠ 已被 #1130／#1187 取代（2026-10）：下方 2026-04 決策與分工保留為歷史脈絡；現行 woow-odoo chart 的做法見「現況」段。

**日期**: 2026-04-04 ~ 2026-04-05

### 架構決策

- 兩個獨立 image: `paas-odoo`（平台自用）和 `cloud-odoo`（使用者 Cloud Service）
- `cloud-odoo` image 包含所有 candidate addons 在 `/all-addons/`
- `paas-odoo` image 只包含 `addon-catalog.json` metadata（無實際 addon 檔案）
- initContainer 用 cloud-odoo image 本身（不是 busybox），因為 `/all-addons/` 在裡面（已取代，見下方現況）
- 不自動 `odoo -i` 安裝 addon，使用者在 Odoo Apps UI 自行啟用
- `addons_path` 修改放在 initContainer（mount odoo-data PVC）+ customPostInitScripts 雙保險（已取代，見下方現況）
- `CloudOdooAddon` records 從 `/all-addons/catalog.json` 動態 sync，不用靜態 XML seed data
- sync 時不在 catalog 的 addon 會被 deactivate

### 跨 Repo 分工

| Repo | 職責 |
|------|------|
| paas-ci | Dockerfile + Dockerfile.cloud，candidate-addons/，addon-catalog.json 自動產生 |
| paas-platform | CloudOdooAddon model，sync API，helm values 組裝（initContainers + extraVolumes），前端 AddonSelector（已取代，見下方現況） |
| paas-charts | 不需改動（Bitnami chart 原生支援 initContainers/extraVolumes）（已取代，見下方現況） |
| paas-cd | 不需改動（Cloud Service 走 paas-operator 不經 Fleet） |

### 現況（2026-10，#1130／#1187／PR #1324）

- **選擇機制**：woow-odoo chart 原生接收 `addons.selected`（純 addon 名稱 list）。平台的 `_woow_odoo_selected_addons()` 每次從 `odoo_addon_ids` 重算完整清單，只取 `third_party`／`custom` 的 `technical_name` 並排序（`src/models/cloud_service.py:4255-4268`）；每次組裝 helm values 都寫入 `values['addons'] = {'selected': ...}`，包含取消全部勾選後的空 list，不沿用舊值（`src/models/cloud_service.py:4142-4151`）。
- **落點**：selected addons 放在 `emptyDir`，每次 pod 啟動都從 image 的 `/all-addons` 重新複製。#1160 Q6 已修訂 #1141 裁決 6 的「落 PVC」方案；不再使用平台注入的 initContainer＋odoo-data PVC＋customPostInitScripts 組合。addon initContainer 與 `addons_path` 由 chart 管理，平台只傳名稱清單（`src/models/cloud_service.py:4142-4151`）。
- **升級閘**：依 #1141，對 state 為 `installed` 或 `to upgrade`、但 image 內找不到原始碼的模組採 fail-closed，阻止帶著缺失的模組原始碼啟動。
- **語意不變**：勾選不自動執行 `odoo -i`，使用者仍須在 Odoo Apps UI 自行啟用；移除只從 selected 清單拿掉，不代為 uninstall（`src/models/cloud_service.py:4148-4149`）。`_assess_addon_removal()` 只放行 `uninstalled`／`uninstallable`／`not_found`，仍 `installed` 的模組會被擋下，無法查證狀態也拒絕移除；寫入路徑依裁決還原選擇並回傳錯誤（`src/controllers/paas.py:209-210`、`:3121-3218`、`:4804-4815`）。

現行跨 Repo 分工（paas-ci、paas-cd 沿用原列，本輪未另行查證）：

| Repo | 職責 |
|------|------|
| paas-ci | Dockerfile + Dockerfile.cloud，candidate-addons/，addon-catalog.json 自動產生 |
| paas-platform | CloudOdooAddon model、sync API、組 addons.selected、前端 AddonsTab |
| paas-charts | 有改動：woow-paas/woow-paas-charts#81 交付 addons.selected 與升級閘 |
| paas-cd | 不需改動（Cloud Service 走 paas-operator 不經 Fleet） |

**Why:** 讓使用者建立 Odoo Cloud Service 時可選擇需要的 addon，而非所有人用同一套固定 addon。
**How to apply:** 新增 candidate addon 後，須確認租戶 image 的 `/all-addons` 與平台 sync 所用 catalog 一致，再同步 `CloudOdooAddon` records；woow-odoo 部署時由平台依勾選組裝完整 `addons.selected`，chart 負責複製與升級閘，使用者在 Odoo Apps UI 自行啟用。
