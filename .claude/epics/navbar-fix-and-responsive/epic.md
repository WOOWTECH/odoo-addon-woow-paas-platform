---
name: navbar-fix-and-responsive
status: completed
created: 2026-02-23T23:47:59Z
updated: 2026-02-24T09:32:51Z
progress: 100%
prd: .claude/prds/navbar-fix-and-responsive.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/86
---

# Epic: navbar-fix-and-responsive

## Overview

修復全站導航 breadcrumb 的可點擊性與響應式排版問題。涉及兩類 breadcrumb：Header 全域 breadcrumb（僅顯示 "Home > 頁面名"，不可點擊）和 6 個子頁面內嵌 breadcrumb（parent 標籤不可點擊）。

## Architecture Decisions

### AD-1: 保留雙層 Breadcrumb 架構
- **Header breadcrumb**：顯示路由層級路徑，所有項目可點擊（除 active 項）
- **Page-level breadcrumb**：保留各子頁面的 `← / Parent / Current` 結構，parent 改為可點擊
- **理由**：避免跨元件傳遞資料（如 workspace/service 名稱），保持現有架構簡潔

### AD-2: Header Breadcrumb 動態路徑使用路由映射
- 在 `Header.js` 新增 `breadcrumbItems` getter，基於 `router.current` 產生路徑陣列
- 使用路由定義的 `name` 欄位作為 label，不需要從子頁面取得實體名稱
- 深層頁面（如 service-detail）在 Header 顯示為 `Home > Workspaces > Service Detail`

### AD-3: Page-level Breadcrumb Parent 加上 click handler
- 將 `<span class="o_woow_breadcrumb_parent">` 改為可點擊元素
- 綁定 `router.navigate()` 到對應的父頁面路徑
- 最小改動：只需在各 XML 加上 `t-on-click` 和 cursor style

### AD-4: Responsive 修復使用 CSS-only 方案
- 子頁面 breadcrumb 加上 `overflow`, `text-overflow`, `white-space` 控制
- 手機版加上 `flex-wrap` 和最大寬度限制防止溢出
- 不需要 JS 邏輯變更

## Technical Approach

### 受影響的 Breadcrumb 位置（共 7 處）

| # | File | Breadcrumb Type | Issue |
|---|------|----------------|-------|
| 1 | `layout/header/Header.xml` + `.js` | Header 全域 | "Home" 不可點擊，無動態路徑 |
| 2 | `pages/workspace/WorkspaceDetailPage.xml` | Page-level | "Workspaces" 不可點擊 |
| 3 | `pages/workspace/WorkspaceTeamPage.xml` | Page-level | workspace.name 不可點擊 |
| 4 | `pages/service/ServiceDetailPage.xml` | Page-level | "Services" 不可點擊 |
| 5 | `pages/marketplace/AppMarketplacePage.xml` | Page-level | "Workspace" 不可點擊 |
| 6 | `pages/configure/AppConfigurationPage.xml` | Page-level | "Marketplace" 不可點擊 |
| 7 | `pages/task-detail/TaskDetailPage.xml` | Page-level | "AI Tasks" 不可點擊 |

### 導航目標映射

| Page-level Parent | Click Target |
|-------------------|-------------|
| "Workspaces" (WorkspaceDetail) | `router.navigate('workspaces')` |
| workspace.name (WorkspaceTeam) | `router.navigate('workspace/{id}')` |
| "Services" (ServiceDetail) | `router.navigate('workspace/{id}')` |
| "Workspace" (Marketplace) | `router.navigate('workspace/{id}')` |
| "Marketplace" (Configure) | `router.navigate('workspace/{id}/services/marketplace')` |
| "AI Tasks" (TaskDetail) | `router.navigate('ai-assistant/tasks')` |

### Header Breadcrumb 路由映射

```javascript
get breadcrumbItems() {
    const route = this.props.router.current;
    const params = this.props.router.params;

    const items = [{ label: "Home", path: "dashboard" }];

    // 根據 route 加入中間層
    if (route.startsWith("workspace") || route === "marketplace" || ...) {
        items.push({ label: "Workspaces", path: "workspaces" });
    }
    if (route === "ai-assistant" || route.startsWith("ai-")) {
        items.push({ label: "AI Assistant", path: "ai-assistant" });
    }
    // ... 最後一項為 active（不可點擊）
    return items;
}
```

## Implementation Strategy

分為 3 個 task，按順序執行：

1. **Header breadcrumb 重構** — 最核心的改動，讓全域 breadcrumb 有動態路徑且可點擊
2. **Page-level breadcrumb parent 可點擊** — 6 個子頁面加上 click handler
3. **Responsive 排版修復 + 瀏覽器驗證** — CSS 修復 + 開瀏覽器測試桌面/手機版

## Task Breakdown Preview

- [ ] Task 1: **Header breadcrumb 動態路徑與可點擊** — 重構 `Header.js` 加入 `breadcrumbItems` getter，更新 `Header.xml` 模板使用 `t-foreach` 渲染可點擊項目
- [ ] Task 2: **Page-level breadcrumb parent 可點擊** — 在 6 個子頁面的 breadcrumb parent `<span>` 加上 `t-on-click` 導航事件和對應的 JS method
- [ ] Task 3: **Responsive CSS 修復與瀏覽器驗證** — 修復 `20_layout.scss` 的 breadcrumb responsive 樣式，加上 overflow 處理；開瀏覽器測試桌面版和手機版（375px, 768px）所有頁面

## Dependencies

- **router.js** — 已存在，提供 `navigate()`、`current`、`params`，無需改動
- **Material Symbols** — 已存在（`chevron_right` 圖示）
- **SCSS 變數** — 已存在（`$woow-primary`、`$woow-text-secondary` 等）
- **無後端依賴** — 純前端改動

## Success Criteria (Technical)

| Criteria | Target |
|----------|--------|
| Header breadcrumb 可點擊項目 | 所有非 active 項目可點擊導航 |
| Page-level breadcrumb parent 可點擊 | 6 個子頁面全部修復 |
| 桌面版 (1280px+) 排版 | 無溢出、無重疊 |
| 手機版 (375px) 排版 | 無跑版、breadcrumb 正確截斷或換行 |
| 平板版 (768px) 排版 | 斷點前後 UI 一致 |
| hover/cursor 視覺回饋 | 所有可點擊項目有 pointer cursor |

## Estimated Effort

- **Total**: 3 tasks
- **Task 1** (Header breadcrumb): ~30 min — 修改 2 個檔案 (Header.js + Header.xml)
- **Task 2** (Page-level clickable): ~30 min — 修改 6 個 XML + 可能需修改對應 JS
- **Task 3** (Responsive + testing): ~30 min — 修改 SCSS + 瀏覽器測試驗證
- **Critical path**: Task 1 → Task 3（Task 2 可與 Task 1 並行）

## Tasks Created

- [ ] 001.md - Header breadcrumb dynamic path and clickable navigation (parallel: true)
- [ ] 002.md - Page-level breadcrumb parent clickable navigation (parallel: true)
- [ ] 003.md - Responsive CSS fix and browser verification (parallel: false, depends on 001, 002)

Total tasks: 3
Parallel tasks: 2 (001, 002)
Sequential tasks: 1 (003)
Estimated total effort: 3 hours
