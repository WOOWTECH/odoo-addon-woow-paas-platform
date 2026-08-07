---
name: standalone-owl-app-shell
description: 建立 Standalone OWL Application 框架，將靜態 HTML 原型轉換為可維護的前端應用
status: complete
created: 2026-01-13T17:32:53Z
updated: 2026-01-14T06:51:44Z
---

# PRD: Standalone OWL App Shell

## Executive Summary

將現有 Stitch PaaS Web App Shell 靜態 HTML 原型（104 個頁面）轉換為 Odoo 18 的 Standalone OWL Application。建立良好的前端資料夾結構和應用框架，為後續功能開發奠定基礎。

**核心價值：**
- 為 PaaS 訂閱者提供現代化、響應式的用戶介面
- 建立可擴展的前端架構，支持持續開發
- 利用 OWL 框架的響應式特性提升用戶體驗

## Problem Statement

### 問題描述
目前有一套完整的靜態 HTML 原型（基於 Tailwind CSS），包含 Dashboard、Workspace、Billing、Settings 等模組，但這些只是靜態頁面，無法：
- 與後端互動
- 管理應用狀態
- 支持 SPA 路由
- 複用 UI 元件

### 為什麼現在重要
1. 原型已完成設計驗證，可進入開發階段
2. Odoo 18 提供了 Standalone OWL Application 的官方支持
3. 需要建立前端基礎架構以支持後續功能迭代

## User Stories

### Primary Persona: PaaS 訂閱者（終端客戶）
- **角色：** 使用 Woow PaaS 平台服務的客戶
- **目標：** 透過直觀的介面管理訂閱、部署、帳單
- **痛點：** 需要簡單易用的控制台，無需學習複雜操作

### User Story 1: 應用載入
**As a** PaaS 訂閱者
**I want** 訪問 `/woow` 路徑時能看到完整的應用介面
**So that** 我可以開始使用平台功能

**Acceptance Criteria:**
- [ ] 訪問 `/woow` 顯示應用 Shell（側邊欄 + 頂部導航 + 內容區）
- [ ] 頁面載入時間 < 3 秒
- [ ] 支持 Light/Dark 主題
- [ ] 響應式設計（Desktop/Tablet/Mobile）

### User Story 2: 頁面導航
**As a** PaaS 訂閱者
**I want** 點擊側邊欄選單時頁面內容切換，但不重新載入整個頁面
**So that** 我有流暢的操作體驗

**Acceptance Criteria:**
- [ ] 側邊欄包含：Dashboard、Workspaces、Deployments、Billing、Settings
- [ ] 點擊選單項目 URL 變化且內容區更新
- [ ] 當前選中項目有視覺高亮
- [ ] 支持瀏覽器前進/後退

### User Story 3: 元件複用
**As a** 開發者
**I want** 常用 UI 元素（按鈕、卡片、表格）作為獨立元件
**So that** 後續開發可快速組合頁面

**Acceptance Criteria:**
- [ ] 建立基礎元件庫（Button, Card, Badge, Avatar, Icon）
- [ ] 元件支持 props 配置
- [ ] 元件有統一的樣式主題

## Requirements

### Functional Requirements

#### FR-1: Standalone OWL Application 入口
- 建立 HTTP Controller 處理 `/woow` 路由
- 建立 QWeb 模板渲染應用 HTML
- 建立 Asset Bundle 打包前端資源

#### FR-2: 應用 Shell 結構
- **Sidebar 元件**：Logo、導航選單、用戶資訊
- **Header 元件**：麵包屑、Balance 顯示、通知、用戶頭像
- **Main Content 區域**：根據路由渲染不同頁面
- **Router 服務**：管理 URL 與頁面元件的對應

#### FR-3: 基礎頁面元件（第一階段）
- Dashboard Page（靜態展示）
- Workspace List Page（靜態展示）
- Empty State 元件（無內容時的佔位）

#### FR-4: UI 元件庫
| 元件 | 用途 |
|------|------|
| `WoowButton` | 主要/次要/危險按鈕 |
| `WoowCard` | 內容卡片容器 |
| `WoowBadge` | 狀態標籤 |
| `WoowAvatar` | 用戶頭像 |
| `WoowIcon` | Material Symbols 圖示封裝 |
| `WoowStatCard` | 統計數據卡片 |

### Non-Functional Requirements

#### NFR-1: 效能
- 首次載入 < 3 秒（無快取）
- 頁面切換 < 100ms
- Bundle 大小 < 500KB（壓縮後）

#### NFR-2: 可維護性
- 遵循 OWL 最佳實踐
- 元件單一職責
- 清晰的資料夾結構

#### NFR-3: 相容性
- 支持現代瀏覽器（Chrome, Firefox, Safari, Edge 最新兩個版本）
- 響應式設計（>= 320px 螢幕寬度）

#### NFR-4: 安全性
- CSRF Token 保護
- XSS 防護（OWL 模板自動轉義）

## Technical Design

### 前端資料夾結構

```
static/src/paas/
├── app.js                    # 應用入口，掛載 Root
├── root.js                   # Root 元件
├── root.xml                  # Root 模板
│
├── core/                     # 核心服務
│   ├── router.js             # 路由服務
│   └── theme.js              # 主題服務（Light/Dark）
│
├── components/               # 可複用 UI 元件
│   ├── button/
│   │   ├── WoowButton.js
│   │   └── WoowButton.xml
│   ├── card/
│   ├── badge/
│   ├── avatar/
│   ├── icon/
│   └── stat_card/
│
├── layout/                   # 佈局元件
│   ├── sidebar/
│   │   ├── Sidebar.js
│   │   └── Sidebar.xml
│   ├── header/
│   │   ├── Header.js
│   │   └── Header.xml
│   └── app_shell/
│       ├── AppShell.js
│       └── AppShell.xml
│
├── pages/                    # 頁面元件
│   ├── dashboard/
│   │   ├── DashboardPage.js
│   │   └── DashboardPage.xml
│   ├── workspace/
│   │   ├── WorkspaceListPage.js
│   │   └── WorkspaceListPage.xml
│   └── empty/
│       ├── EmptyState.js
│       └── EmptyState.xml
│
└── styles/                   # 樣式
    ├── variables.scss        # 主題變數（顏色、圓角等）
    ├── base.scss             # 基礎樣式
    ├── components.scss       # 元件樣式
    └── main.scss             # 主入口（@import 所有）
```

### Asset Bundle 配置

```python
'assets': {
    'woow_paas_platform.assets_paas': [
        ('include', 'web._assets_helpers'),
        'web/static/src/scss/pre_variables.scss',
        'web/static/lib/bootstrap/scss/_variables.scss',
        ('include', 'web._assets_bootstrap'),
        ('include', 'web._assets_core'),
        'woow_paas_platform/static/src/paas/**/*',
    ]
}
```

### 主題配置（從原型提取）

```scss
// variables.scss
$primary: #5f81fc;
$background-light: #f5f6f8;
$background-dark: #0f1323;
$card-light: #ffffff;
$card-dark: #1a1f36;
$border-radius-default: 1rem;
$border-radius-lg: 2rem;
$font-display: 'Manrope', sans-serif;
$font-body: 'Outfit', sans-serif;
```

### 路由配置

```javascript
// core/router.js
const routes = [
    { path: '/woow', component: DashboardPage, name: 'dashboard' },
    { path: '/woow/workspaces', component: WorkspaceListPage, name: 'workspaces' },
    { path: '/woow/deployments', component: EmptyState, name: 'deployments' },
    { path: '/woow/billing', component: EmptyState, name: 'billing' },
    { path: '/woow/settings', component: EmptyState, name: 'settings' },
];
```

## Success Criteria

| 指標 | 目標 | 驗證方式 |
|------|------|----------|
| 應用載入成功 | 訪問 `/woow` 顯示完整 Shell | 手動測試 |
| 路由工作正常 | 5 個主選單可切換 | 手動測試 |
| 響應式設計 | Desktop/Mobile 正常顯示 | 瀏覽器測試 |
| 元件複用 | 至少 3 個元件被多處使用 | 程式碼審查 |
| 程式碼品質 | 無 ESLint 錯誤 | CI 檢查 |

## Constraints & Assumptions

### 技術限制
- 必須使用 Odoo 18 的 OWL 框架
- 不能使用 React/Vue 等第三方框架
- Asset Bundle 需與現有 Odoo backend assets 分開

### 假設
- 原型設計已通過 UX 驗證
- 字體（Manrope, Outfit）可透過 Google Fonts 載入
- Material Symbols 圖示可正常使用

### 資源限制
- 第一階段只做 UI 框架，不實作業務邏輯
- 後端 API 整合在後續階段

## Out of Scope

以下功能**不**在本 PRD 範圍內：

1. **後端 API 整合** - 所有數據暫時使用靜態 mock
2. **用戶認證流程** - Login/Logout 頁面
3. **表單提交功能** - Create/Edit 等操作
4. **即時通知** - WebSocket 推送
5. **國際化 (i18n)** - 多語言支持
6. **單元測試** - 自動化測試

## Dependencies

### 外部依賴
- Odoo 18.0 OWL 框架
- Google Fonts（Manrope, Outfit）
- Material Symbols Outlined

### 內部依賴
- `woow_paas_platform` 模組已安裝
- Odoo Web Client 可正常運行

## Milestones

### Phase 1: 基礎框架（本 PRD）
- [ ] Controller + QWeb + Asset Bundle 設置
- [ ] App Shell 結構（Sidebar + Header + Main）
- [ ] Router 服務實作
- [ ] 基礎 UI 元件（Button, Card, Badge）
- [ ] Dashboard 頁面靜態展示
- [ ] Workspace List 頁面靜態展示

### Phase 2: 完整頁面（後續 PRD）
- 所有 104 個原型頁面轉換
- Modal 對話框元件
- 表單元件

### Phase 3: 後端整合（後續 PRD）
- Odoo RPC 服務封裝
- 真實數據載入
- 狀態管理

## References

- [Odoo 18 Standalone OWL Application](https://www.odoo.com/documentation/18.0/developer/howtos/standalone_owl_application.html)
- 靜態原型位置：`resource/stitch_paas_web_app_shell_global_navigation/`
- 原型頁面數量：104 個
