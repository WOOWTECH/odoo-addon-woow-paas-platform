---
name: backend-views-cloud-models
status: completed
created: 2026-04-05T08:05:31Z
updated: 2026-04-05T08:05:31Z
progress: 100%
prd: .claude/prds/backend-views-cloud-models.md
---

# Epic: backend-views-cloud-models

## Overview

為 Cloud Service 和 Cloud Odoo Addon 兩個 model 新增 Odoo 後台 list/form views，並建立「Cloud」子選單群組整合所有 Cloud 相關功能。

## Architecture Decisions

- **子選單分組**：建立 `menu_cloud` parent menuitem，將 Templates、Services、Odoo Addons 整合在一起
- **命名策略**：Addons 命名為「Odoo Addons」以區分未來其他 cloud type addon
- **既有 menu 移動**：Cloud App Templates 的 menu 從根選單移至 Cloud 子選單下，name 簡化為「Templates」
- **不修改 model/security**：所有 model 和權限已存在，僅新增 XML views

## Technical Approach

### 變更範圍

| 檔案 | 變更 | 說明 |
|------|------|------|
| `src/views/menu.xml` | 修改 | 新增 `menu_cloud` parent menuitem |
| `src/views/cloud_app_template_views.xml` | 修改 | 將 menu parent 改為 `menu_cloud`，name 改為 "Templates" |
| `src/views/cloud_service_views.xml` | 新建 | list + form + action + menuitem |
| `src/views/cloud_odoo_addon_views.xml` | 新建 | list + form + action + menuitem |
| `src/__manifest__.py` | 修改 | 註冊新 view 檔案 |

### 不需要變更

- `src/models/` — 所有 model 已存在
- `src/security/ir.model.access.csv` — 權限已配置
- `src/controllers/` — 後台 views 不涉及 API

### Menu 結構

```
Woow PaaS (root, sequence=100)
├── Cloud (sequence=30)
│   ├── Templates (sequence=10)    ← 既有，移入
│   ├── Services (sequence=20)     ← 新增
│   └── Odoo Addons (sequence=30)  ← 新增
├── MCP Servers (sequence=50)
├── OAuth Clients (sequence=60)
└── ...
```

## Task Breakdown

- [x] Task 1: 新增 Cloud 子選單群組 + 移動 Templates menu + 註冊新 view 檔案
- [x] Task 2: 建立 Cloud Service views（list + form + action + menuitem）
- [x] Task 3: 建立 Cloud Odoo Addon views（list + form + action + menuitem）

## Dependencies

### Internal（全部已完成）
- Cloud Services MVP — model 和 security 已建立
- Cloud Odoo Addon model — 已透過 catalog sync 實作

## Estimated Effort

- **總工時**：約 30 分鐘
- **變更量**：~200 行 XML + 2 行 Python（manifest）
- **風險**：極低 — 純 view 層新增，不影響業務邏輯
