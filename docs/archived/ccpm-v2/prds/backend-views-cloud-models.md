---
name: backend-views-cloud-models
description: Add backend list/form views for Cloud Service and Cloud Odoo Addon models with Cloud submenu grouping
status: complete
created: 2026-04-05T08:05:31Z
updated: 2026-04-05T08:05:31Z
---

# PRD: Backend Views for Cloud Service & Cloud Odoo Addon

## Executive Summary

為 Cloud Service 和 Cloud Odoo Addon 兩個已存在的 model 新增 Odoo 後台的 list/form views，並將 Cloud 相關功能整合到一個「Cloud」子選單群組下，方便管理員在後台瀏覽和管理部署的服務與 addon 目錄。

**核心價值**：管理員可以直接在 Odoo 後台查看所有已部署的 Cloud Service 狀態、Helm 配置、資源分配，以及管理 Odoo Addon 目錄。

## Problem Statement

### 問題描述

目前 Cloud Service 和 Cloud Odoo Addon 兩個 model 雖然已建立，但沒有對應的後台 views。管理員無法透過 Odoo 後台介面瀏覽或管理這些資料，只能透過技術手段（如 SQL、shell）存取。

此外，Cloud 相關的 menu items（Templates、Services、Addons）散落在 Woow PaaS 根選單下，缺乏組織結構。

### 為什麼現在重要

- Cloud Service 資料需要可視化監控（狀態、錯誤、Helm 配置）
- Cloud Odoo Addon 目錄需要管理介面（啟用/停用、設定預設值）
- 隨著 Cloud 功能增加，需要子選單分組以維持導航清晰度

## User Stories

1. **管理員查看服務狀態**：我想在後台看到所有已部署的 Cloud Service，了解它們的狀態、所屬 workspace 和 Helm 配置。
2. **管理員管理 Addon 目錄**：我想在後台瀏覽和管理 Odoo Addon 清單，設定哪些 addon 是預設選取的。
3. **管理員快速導航**：我想在「Cloud」子選單下找到所有 Cloud 相關功能（Templates、Services、Odoo Addons）。

## Scope

### In Scope

- Cloud Service list view + form view + action + menuitem
- Cloud Odoo Addon list view + form view + action + menuitem
- 「Cloud」子選單群組，整合 Templates、Services、Odoo Addons
- Odoo Addons 命名（非 Addons），以區分未來其他 cloud type addon

### Out of Scope

- Cloud Service 的操作按鈕（start/stop/delete — 透過 API 操作）
- Cloud Odoo Addon 的 sync 按鈕（透過 cron 自動同步）
- 權限變更（現有 ir.model.access.csv 已足夠）

## Success Criteria

| Criteria | Target |
|----------|--------|
| Woow PaaS 選單下出現「Cloud」子選單群組 | Pass |
| Cloud > Templates 顯示既有的 Cloud App Templates | Pass |
| Cloud > Services 顯示 Cloud Service list/form | Pass |
| Cloud > Odoo Addons 顯示 Cloud Odoo Addon list/form | Pass |
| Cloud Service form 包含 statusbar、Helm config、Resources、Errors 分頁 | Pass |
| Cloud Odoo Addon form 包含 Dependencies、Compatible Templates 分頁 | Pass |
