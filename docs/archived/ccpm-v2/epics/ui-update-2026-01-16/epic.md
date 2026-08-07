---
name: ui-update-2026-01-16
status: completed
created: 2026-01-15T16:25:30Z
updated: 2026-01-15T17:48:41Z
progress: 100%
prd: .claude/prds/ui-update-2026-01-16.md
github: https://github.com/WOOWTECH/odoo-addons/issues/33
---

# Epic: UI 更新 2026-01-16

## 概覽

更新現有的 WoowTech OWL App Shell UI 元件以符合 2026-01-16 設計修訂版。這是一個**重構任務** - 沒有新頁面或重大功能，僅修改現有元件（Sidebar、Header、Dashboard）。

**主要變更：**
- Sidebar：移除「Deployments」選單，將使用者資訊移至頁首，新增「Log Out」連結
- Header：新增使用者名稱/方案、通知徽章、增強型點數顯示
- Dashboard：將系統指標替換為團隊/帳務/工作區概覽卡片

## 架構決策

### 1. 沿用現有元件結構
目前的程式碼庫已有組織良好的 OWL 元件。我們將**原地修改**而非建立新元件：
- `Sidebar.js/xml` - 更新 navItems 陣列，修改 footer 模板
- `Header.js/xml` - 擴展 header_actions 區塊
- `DashboardPage.js/xml` - 更新 stats 資料結構

### 2. 不需要新元件
PRD 建議新增元件（CreditsDisplay、NotificationBell、UserAvatar），但這些可以作為 **Header.xml 內的行內模板元素**實作 - 更簡單且檔案更少。

### 3. 暫時使用模擬資料
使用者資料（點數、通知、名稱、方案）將先使用硬編碼的模擬資料。根據 PRD，真實資料整合不在範圍內。

### 4. 僅在必要時更新 SCSS
大部分樣式已存在。僅更新以下 SCSS：
- Header 使用者資訊區塊（新元素）
- Dashboard 卡片變體（如顏色有變更）

## 技術方案

### 需修改的檔案

| 檔案 | 變更內容 |
|------|---------|
| `static/src/paas/layout/sidebar/Sidebar.js` | 從 navItems 移除 "deployments" |
| `static/src/paas/layout/sidebar/Sidebar.xml` | 將使用者資訊替換為 Log Out 連結 |
| `static/src/paas/layout/header/Header.xml` | 新增使用者名稱、方案、通知徽章 |
| `static/src/paas/pages/dashboard/DashboardPage.js` | 更新 stats 陣列為新資料 |
| `static/src/paas/styles/_header.scss` | 新頁首元素樣式 |

### 不需要新檔案
保持最小化變更可避免：
- 在 manifest 註冊新元件
- 額外的 import 語句
- 元件生命週期複雜度

## 實作策略

### 單次實作
所有變更都是純 UI 且使用模擬資料。可在一次專注的工作階段中完成：

1. **先做 Sidebar**（最簡單 - 只是移除項目）
2. **再做 Header**（在現有結構中新增元素）
3. **接著做 Dashboard**（更新資料陣列）
4. **最後做 SCSS**（修飾樣式）

### 測試方法
- 與設計截圖進行視覺比對
- 路由導航仍正常運作
- 無 console 錯誤

## 任務拆解預覽

高階任務（限制為 5 個以保持簡潔）：

- [ ] **任務 1**：更新 Sidebar - 移除 Deployments，新增 Log Out
- [ ] **任務 2**：更新 Header - 新增使用者資訊、通知、增強型點數
- [ ] **任務 3**：更新 Dashboard - 將 stats 替換為成員/帳務/工作區卡片
- [ ] **任務 4**：更新 SCSS - Header 使用者區塊、Dashboard 卡片調整
- [ ] **任務 5**：視覺 QA - 與設計規格比對，修正差異

## 相依性

### 內部（已完成）
- OWL App Shell（第 2 階段）- 已實作
- SCSS 主題系統 - 已就位
- Material Symbols - 已整合

### 外部
- 設計規格：`resource/stitch_paas_web_app_shell_global_navigation_2026-01-16/`

## 成功標準（技術面）

| 標準 | 驗證方式 |
|----------|--------------|
| Sidebar 顯示 4 個項目 | 視覺檢查 |
| Header 顯示使用者資訊 | 視覺檢查 |
| Dashboard 顯示 3 個新概覽卡片 | 視覺檢查 |
| 無 console 錯誤 | 瀏覽器 DevTools |
| 路由仍正常運作 | 點擊測試所有導航項目 |

## 預估工時

| 任務 | 預估時間 |
|------|----------|
| Sidebar 更新 | 15 分鐘 |
| Header 更新 | 30 分鐘 |
| Dashboard 更新 | 30 分鐘 |
| SCSS 調整 | 20 分鐘 |
| 視覺 QA | 15 分鐘 |
| **總計** | **約 2 小時** |

## 設計參考

主要參考畫面：
- `paas_web_app_shell_-_global_navigation_1/screen.png` - 主儀表板佈局
- `workspace_list_page_2/screen.png` - 有資料的側邊欄
- `settings_overview_page_1/screen.png` - 設定頁面頁首

## 已完成的任務

| Issue | 標題 | 狀態 | Commit |
|-------|------|------|--------|
| [#34](https://github.com/WOOWTECH/odoo-addons/issues/34) | 更新 Sidebar 元件 | ✅ Done | `cdb0f23` |
| [#35](https://github.com/WOOWTECH/odoo-addons/issues/35) | 更新 Header 元件 | ✅ Done | `922ac1a` |
| [#36](https://github.com/WOOWTECH/odoo-addons/issues/36) | 更新 Dashboard 頁面 | ✅ Done | `45baee8` |
| [#37](https://github.com/WOOWTECH/odoo-addons/issues/37) | 更新 SCSS 樣式 | ✅ Done | `da922fb` |
| [#38](https://github.com/WOOWTECH/odoo-addons/issues/38) | 視覺 QA 驗證 | ✅ Done | `621982d` |

**總任務數**：5
**已完成**：5/5 (100%)
**分支**：`epic/ui-update-2026-01-16`
