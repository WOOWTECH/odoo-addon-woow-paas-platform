---
name: standalone-owl-app-shell
status: completed
created: 2026-01-13T17:37:21Z
updated: 2026-01-14T06:51:44Z
completed: 2026-01-14T06:51:44Z
progress: 100%
prd: .claude/prds/standalone-owl-app-shell.md
github: https://github.com/WOOWTECH/odoo-addons/issues/22
---

# Epic: Standalone OWL App Shell

## Overview

建立 Woow PaaS Platform 的 Standalone OWL Application 框架。此 Epic 將靜態 HTML 原型轉換為可維護的 OWL 前端應用，包含完整的 App Shell（Sidebar + Header + Content）、前端路由、以及基礎 UI 元件庫。

**技術目標：**
- 建立獨立於 Odoo Web Client 的前端應用入口 (`/woow`)
- 實作 SPA 路由機制
- 建立可複用的元件架構

## Architecture Decisions

### AD-1: 使用 Odoo 18 Standalone OWL Application 模式
**決策：** 採用官方 Standalone OWL Application 架構，而非嵌入 Odoo Web Client。
**理由：**
- 獨立的 Asset Bundle 避免與 Odoo backend 衝突
- 更靈活的路由控制
- 更乾淨的用戶體驗（無 Odoo 導航干擾）

### AD-2: 使用 Hash-based 路由作為第一版
**決策：** 使用 `#/path` 格式的 hash 路由，而非 History API。
**理由：**
- 不需要額外的 Odoo Controller 處理子路徑
- 單一入口點更簡單
- 後續可升級為 History API

### AD-3: 樣式策略 - SCSS + Tailwind 變數
**決策：** 使用 SCSS 並從原型提取 Tailwind 主題變數，不直接引入 Tailwind CDN。
**理由：**
- 避免與 Odoo Bootstrap 衝突
- 更好的打包控制
- 可利用 Odoo 現有的 SCSS 編譯流程

### AD-4: 簡化元件數量
**決策：** 第一階段只建立必要的 3 個基礎元件（Icon, Card, Button），其餘樣式直接寫在頁面中。
**理由：**
- 減少過度抽象
- 快速交付可用版本
- 根據實際複用需求再拆分

## Technical Approach

### Backend (Odoo)
1. **HTTP Controller** (`controllers/paas.py`)
   - 單一路由 `/woow` 渲染 QWeb 模板
   - 傳遞 session_info 給前端

2. **QWeb Template** (`views/paas_app.xml`)
   - 初始化 `odoo` 全域物件（CSRF, debug, session）
   - 載入 `woow_paas_platform.assets_paas` bundle

3. **Asset Bundle** (`__manifest__.py`)
   - 獨立的 `assets_paas` bundle
   - 包含 OWL core 和應用程式碼

### Frontend (OWL)

#### 核心結構
```
static/src/paas/
├── app.js          # 掛載入口
├── root.js/xml     # Root 元件 + Router 邏輯
├── core/router.js  # 簡易 hash 路由服務
├── layout/         # AppShell, Sidebar, Header
├── components/     # Icon, Card, Button
├── pages/          # Dashboard, WorkspaceList, EmptyState
└── styles/         # SCSS 主題
```

#### 路由機制
```javascript
// 監聽 hashchange，根據 hash 切換 currentPage
window.addEventListener('hashchange', () => {
    this.currentPage = this.getPageFromHash();
});
```

## Implementation Strategy

### 開發順序
1. **基礎設施** → Controller + Template + Bundle（可測試載入）
2. **Root + Router** → 基本路由切換（可測試導航）
3. **AppShell 佈局** → Sidebar + Header + Main（可見 UI）
4. **頁面內容** → Dashboard + WorkspaceList（完整功能）

### 風險緩解
- **風險：OWL 版本相容** → 使用 `@odoo/owl` 官方匯出
- **風險：樣式衝突** → 使用 `.o_woow_` 前綴隔離
- **風險：字體載入** → 使用 Google Fonts CDN

### 測試方法
- 手動測試：訪問 `/woow` 驗證載入
- 手動測試：點擊導航驗證路由
- 瀏覽器測試：Chrome DevTools 響應式模式

## Task Breakdown Preview

高層級任務分類（限制在 10 個以內）：

- [ ] **Task 1**: 建立 Standalone App 基礎設施（Controller + QWeb + Bundle）
- [ ] **Task 2**: 實作 Root 元件和 Hash Router 服務
- [ ] **Task 3**: 建立 AppShell 佈局（Sidebar + Header + Main）
- [ ] **Task 4**: 實作 SCSS 主題系統（變數 + 基礎樣式）
- [ ] **Task 5**: 建立基礎 UI 元件（Icon, Card, Button）
- [ ] **Task 6**: 實作 Dashboard 頁面（靜態展示）
- [ ] **Task 7**: 實作 Workspace List 頁面（靜態展示）
- [ ] **Task 8**: 實作 Empty State 佔位頁面
- [ ] **Task 9**: 整合測試與調整

## Dependencies

### 外部依賴
| 依賴 | 用途 | 狀態 |
|------|------|------|
| Odoo 18 OWL | 前端框架 | ✅ 內建 |
| Google Fonts | Manrope, Outfit 字體 | ✅ CDN |
| Material Symbols | 圖示字體 | ✅ CDN |

### 內部依賴
| 依賴 | 用途 | 狀態 |
|------|------|------|
| `woow_paas_platform` 模組 | 基礎模組 | ✅ 已存在 |
| 靜態原型 | UI 參考 | ✅ `resource/` 目錄 |

### 前置條件
- Odoo 18 環境可正常運行
- `woow_paas_platform` 模組已安裝

## Success Criteria (Technical)

### 功能驗收
| 項目 | 標準 |
|------|------|
| 應用載入 | `/woow` 顯示完整 Shell |
| 路由切換 | 5 個選單項可切換，URL 變化 |
| 響應式 | 320px - 1920px 正常顯示 |
| 主題 | Light 模式正確顯示 |

### 程式碼品質
| 項目 | 標準 |
|------|------|
| 結構 | 符合 PRD 定義的資料夾結構 |
| 命名 | 遵循 Odoo OWL 慣例 |
| 樣式 | 使用 `.o_woow_` 前綴 |

### 效能指標
| 項目 | 目標 |
|------|------|
| 首次載入 | < 3 秒 |
| 頁面切換 | < 100ms |

## Estimated Effort

### 時間估算
| 任務 | 預估工時 |
|------|----------|
| 基礎設施 (Task 1) | 1-2 小時 |
| Router (Task 2) | 1-2 小時 |
| AppShell (Task 3) | 2-3 小時 |
| 主題系統 (Task 4) | 1-2 小時 |
| UI 元件 (Task 5) | 2-3 小時 |
| Dashboard (Task 6) | 2-3 小時 |
| Workspace (Task 7) | 2-3 小時 |
| Empty State (Task 8) | 0.5-1 小時 |
| 整合測試 (Task 9) | 1-2 小時 |
| **總計** | **13-21 小時** |

### 關鍵路徑
Task 1 → Task 2 → Task 3 → Task 4 → Task 5 → Task 6/7/8 → Task 9

Tasks 6, 7, 8 可並行開發。

## Tasks Created

- [x] #23 - Standalone App Infrastructure (parallel: false) ✅
- [x] #24 - Root Component and Hash Router (parallel: false) ✅
- [x] #25 - AppShell Layout Components (parallel: false) ✅
- [x] #26 - SCSS Theme System (parallel: true) ✅
- [x] #27 - Base UI Components (parallel: true) ✅
- [x] #28 - Dashboard Page (parallel: true) ✅
- [x] #29 - Workspace List Page (parallel: true) ✅
- [x] #30 - Empty State Page (parallel: true) ✅
- [x] #31 - Integration Testing and Adjustments (parallel: false) ✅

**Summary:**
- Total tasks: 9
- Parallel tasks: 5
- Sequential tasks: 4
- Estimated total effort: 13-21 hours

**Dependency Graph:**
```
#23 (Infrastructure)
 └── #24 (Router)
      └── #25 (AppShell)
           ├── #26 (SCSS) ──┐
           │                ├── #28 (Dashboard) ──┐
           └── #27 (UI) ────┼── #29 (Workspace) ──┼── #31 (Testing)
                            └── #30 (Empty) ──────┘
```
