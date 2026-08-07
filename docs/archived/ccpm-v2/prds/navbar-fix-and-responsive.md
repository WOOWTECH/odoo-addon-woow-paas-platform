---
name: navbar-fix-and-responsive
description: Fix navigation bar clickability and responsive layout issues across desktop and mobile views
status: backlog
created: 2026-02-23T23:41:33Z
---

# PRD: Navigation Bar Fix and Responsive Layout

## Executive Summary

修復 Woow PaaS 前端應用的導航列（Header breadcrumb）和子頁面麵包屑的可點擊性問題，以及桌面版與手機版的 UI 跑版問題。目前 breadcrumb 中的路徑項目無法點擊跳轉，且在部分頁面（尤其是 Workspace/Service 子頁面）中排版錯亂。

## Problem Statement

### 問題描述

1. **Header Breadcrumb 不可點擊**：`Header.xml` 中的麵包屑使用 `<span>` 渲染，沒有綁定任何點擊事件，使用者無法透過 breadcrumb 回到上層頁面。
2. **ServiceDetailPage Breadcrumb 部分不可點擊**：`ServiceDetailPage.xml` 中的 "Services" 文字只是純文字，無法點擊回到 services 列表。
3. **Breadcrumb 結構過於簡單**：Header 的 breadcrumb 只有 "Home > {currentPageName}" 兩層，無法反映深層頁面結構（如 Workspaces > myworkspace > Services > service-name）。
4. **手機版 UI 跑版**：Header 在手機版隱藏 breadcrumb 但子頁面（如 ServiceDetailPage）的 breadcrumb 在小螢幕上排版錯亂。
5. **桌面版排版問題**：Header 和子頁面 breadcrumb 在某些狀態下排版不正確。

### 為什麼現在需要修復

- Navigation bar 是使用者操作最頻繁的 UI 元素
- 不可點擊的 breadcrumb 嚴重影響使用者體驗和效率
- 手機版 UI 跑版讓行動裝置使用者無法正常操作

## User Stories

### US-1: Header Breadcrumb 可點擊導航
**作為**使用者，**我希望**點擊 Header 中的 breadcrumb 路徑，**以便**快速回到上層頁面。

**Acceptance Criteria:**
- [ ] breadcrumb 的每一層都可以點擊（除了當前頁面）
- [ ] 點擊後正確導航到對應頁面
- [ ] hover 時有視覺回饋（cursor: pointer, 顏色變化）
- [ ] 當前頁面名稱不可點擊（顯示為 active 狀態）

### US-2: 動態 Breadcrumb 路徑
**作為**使用者，**我希望** breadcrumb 能顯示完整的頁面路徑層級，**以便**了解我目前在應用中的位置。

**Acceptance Criteria:**
- [ ] Dashboard 頁面：`Home > Dashboard`
- [ ] Workspace 列表：`Home > Workspaces`
- [ ] Workspace 詳情：`Home > Workspaces > {workspace-name}`
- [ ] Service 詳情：`Home > Workspaces > {workspace-name} > {service-name}`
- [ ] AI Assistant 頁面：`Home > AI Assistant`
- [ ] AI Project Kanban：`Home > AI Assistant > {project-name}`

### US-3: 子頁面 Breadcrumb 可點擊
**作為**使用者，**我希望** ServiceDetailPage 等子頁面的 breadcrumb 可以點擊，**以便**回到上層列表。

**Acceptance Criteria:**
- [ ] ServiceDetailPage 的 "Services" 文字可點擊，導航回 workspace services 列表
- [ ] 返回按鈕 (arrow_back) 正常運作
- [ ] breadcrumb 分隔符正確顯示

### US-4: 手機版 UI 不跑版
**作為**行動裝置使用者，**我希望**導航列和 breadcrumb 在手機上正確顯示，**以便**正常使用應用。

**Acceptance Criteria:**
- [ ] Header 在手機版（≤768px）正確顯示 mobile logo + 通知按鈕
- [ ] 子頁面 breadcrumb 在手機版正確排版（不溢出、不換行錯亂）
- [ ] Bottom Navigation 正確顯示且可點擊
- [ ] 所有按鈕的觸控區域足夠大（至少 44x44px）

### US-5: 桌面版 UI 排版正確
**作為**桌面使用者，**我希望**導航列所有元素正確排版，**以便**有一致的使用體驗。

**Acceptance Criteria:**
- [ ] Header breadcrumb、Credits badge、通知按鈕、使用者資訊水平排列
- [ ] 在不同螢幕寬度下不溢出或重疊
- [ ] Material Symbols 圖示正確渲染為圖標（不是文字）

## Requirements

### Functional Requirements

#### FR-1: Header Breadcrumb 重構
- 將 Header.js 的 `currentPageName` 改為完整的 breadcrumb 路徑陣列
- 根據當前路由 (`router.current`) 和路由參數動態生成 breadcrumb 項目
- 每個 breadcrumb 項目（除最後一個）綁定 `router.navigate()` 事件
- breadcrumb 項目需要包含：`{ label, path, isActive }`

#### FR-2: 子頁面 Breadcrumb 修復
- ServiceDetailPage breadcrumb 的文字項目（如 "Services"）綁定導航事件
- 統一所有子頁面 breadcrumb 的交互行為

#### FR-3: Responsive Layout 修復
- 修復手機版 Header 的排版問題
- 確保子頁面 breadcrumb 在小螢幕上正確換行或截斷
- 測試 768px 斷點前後的 UI 一致性

### Non-Functional Requirements

#### NFR-1: Performance
- breadcrumb 路徑計算不應阻塞渲染（使用 getter 而非 watcher）
- 導航響應時間 < 100ms

#### NFR-2: Accessibility
- breadcrumb 使用 `<nav aria-label="breadcrumb">` 語義化標籤
- 可點擊項目有 hover/focus 狀態
- 支援鍵盤 Tab 導航

#### NFR-3: Consistency
- 所有頁面的 breadcrumb 風格統一（字體、顏色、間距）
- 遵循既有 SCSS 命名規範（`.o_woow_` prefix + BEM）

## Success Criteria

| Metric | Target |
|--------|--------|
| Breadcrumb 可點擊率 | 100%（所有非 active 項目可點擊） |
| 手機版 UI 測試通過率 | 100%（無跑版問題） |
| 桌面版 UI 測試通過率 | 100%（無跑版問題） |
| Lighthouse Mobile Score | ≥ 90 |
| 所有主要路由 breadcrumb 正確 | 100% |

## Constraints & Assumptions

### Constraints
- 必須使用 OWL framework 和現有 router.js 架構
- SCSS 必須遵循 `.o_woow_` 命名規範
- 不能破壞現有路由和頁面功能
- Material Symbols 圖示需從 Google Fonts CDN 載入

### Assumptions
- Google Fonts CDN 可正常訪問
- 使用者使用 Chrome/Firefox/Safari 最新版本
- 手機版斷點維持 768px

## Out of Scope

- Sidebar 導航的重構（不在本次範圍）
- Bottom Navigation 的功能擴充
- 新增頁面或路由
- 後端 API 變更
- 多語系 breadcrumb 支援

## Dependencies

- OWL Framework（已存在）
- `router.js`（已存在，需讀取路由參數）
- Material Symbols（已存在，Google Fonts CDN）
- SCSS 變數系統（已存在，`00_variables.scss`）

## Affected Files

| File | Change Type |
|------|-------------|
| `src/static/src/paas/layout/header/Header.js` | 重構 breadcrumb 邏輯 |
| `src/static/src/paas/layout/header/Header.xml` | 更新 breadcrumb 模板 |
| `src/static/src/paas/pages/service/ServiceDetailPage.xml` | 修復 breadcrumb 點擊 |
| `src/static/src/paas/styles/20_layout.scss` | 修復 responsive 排版 |
| `src/static/src/paas/styles/30_components.scss` | breadcrumb 組件樣式（可能） |

## Testing Requirements

- **瀏覽器測試**：使用 Playwright 或手動開啟瀏覽器進行 navigation 跳轉測試
- **桌面版測試**：在 1280px+ 寬度下測試所有頁面 breadcrumb
- **手機版測試**：在 375px（iPhone SE）和 768px 寬度下測試 UI 排版
- **路由測試**：驗證所有主要路由的 breadcrumb 路徑正確性
