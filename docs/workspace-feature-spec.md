# Workspace Feature Specification

> 版本: 1.0.0
> 最後更新: 2026-01-31
> 狀態: **Phase 1 完成**

## 概述

Workspace 是 Woow PaaS Platform 的核心功能，提供多租戶工作空間管理能力。每個 Workspace 可以包含多個成員，並透過角色權限控制存取。

---

## 功能進度總覽

| 功能模組 | 後端 API | 前端 UI | E2E 測試 | 狀態 |
|---------|----------|---------|----------|------|
| Workspace CRUD | ✅ | ✅ | ✅ | **完成** |
| 成員管理 | ✅ | ✅ | ⬜ | **部分完成** |
| 角色權限 | ✅ | ✅ | ⬜ | **部分完成** |
| 所有權轉移 | ✅ | ⬜ | ⬜ | **後端完成** |
| Workspace 設定 | ⬜ | ⬜ | ⬜ | **未開始** |

---

## 資料模型

### Workspace (`woow_paas_platform.workspace`)

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `name` | Char | ✅ | 工作空間名稱 |
| `description` | Text | ⬜ | 工作空間描述 |
| `slug` | Char | 自動 | URL-friendly 識別碼（自動產生） |
| `owner_id` | Many2one → res.users | ✅ | 擁有者（預設為建立者） |
| `state` | Selection | ✅ | 狀態：`active`、`archived` |
| `access_ids` | One2many | - | 存取控制記錄 |
| `member_count` | Integer | 計算 | 成員數量（自動計算） |

**檔案位置**: `src/models/workspace.py`

### WorkspaceAccess (`woow_paas_platform.workspace_access`)

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `workspace_id` | Many2one | ✅ | 關聯的 Workspace |
| `user_id` | Many2one → res.users | ✅ | 使用者 |
| `role` | Selection | ✅ | 角色：`owner`、`admin`、`user`、`guest` |
| `user_name` | Char | Related | 使用者名稱 |
| `user_email` | Char | Related | 使用者 Email |
| `invited_by_id` | Many2one | ⬜ | 邀請者 |
| `invited_date` | Datetime | ⬜ | 邀請日期 |

**檔案位置**: `src/models/workspace_access.py`

---

## 角色權限矩陣

| 權限 | Owner | Admin | User | Guest |
|------|:-----:|:-----:|:----:|:-----:|
| 檢視 Workspace | ✅ | ✅ | ✅ | ✅ |
| 編輯 Workspace | ✅ | ✅ | ✅ | ⬜ |
| 刪除 Workspace | ✅ | ⬜ | ⬜ | ⬜ |
| 管理成員 | ✅ | ✅ | ⬜ | ⬜ |
| 管理 Workspace 設定 | ✅ | ✅ | ⬜ | ⬜ |
| 轉移所有權 | ✅ | ⬜ | ⬜ | ⬜ |

**檔案位置**: `src/models/workspace_access.py:94-130`

---

## API 端點

### Workspace CRUD

| 方法 | 端點 | 說明 | 狀態 |
|------|------|------|------|
| GET | `/api/workspaces` | 取得使用者所有 Workspace | ✅ |
| POST | `/api/workspaces` | 建立新 Workspace | ✅ |
| GET | `/api/workspaces/<id>` | 取得單一 Workspace | ✅ |
| PUT | `/api/workspaces/<id>` | 更新 Workspace | ✅ |
| DELETE | `/api/workspaces/<id>` | 刪除（封存）Workspace | ✅ |

### 成員管理

| 方法 | 端點 | 說明 | 狀態 |
|------|------|------|------|
| GET | `/api/workspaces/<id>/members` | 取得成員列表 | ✅ |
| POST | `/api/workspaces/<id>/members` | 邀請成員 | ✅ |
| PUT | `/api/workspaces/<id>/members/<access_id>` | 更新成員角色 | ✅ |
| DELETE | `/api/workspaces/<id>/members/<access_id>` | 移除成員 | ✅ |

**檔案位置**: `src/controllers/paas.py`

---

## 前端頁面

### 1. Workspace 列表頁 (`#/workspaces`)

**功能**:
- 顯示使用者所有 Workspace 卡片
- 每張卡片顯示：名稱、描述、角色徽章、成員數、建立日期
- 「Create Workspace」按鈕開啟建立 Modal
- 點擊卡片導航到詳情頁

**元件**: `WorkspaceListPage`
**檔案位置**: `src/static/src/paas/pages/workspace/WorkspaceListPage.js`

**狀態**: ✅ 完成

### 2. Workspace 詳情頁 (`#/workspace/<id>`)

**功能**:
- 顯示 Workspace 詳細資訊
- 統計數據卡片（預估成本、App 數量、健康狀態、警報）
- 服務類型區塊（Cloud Services、Security Access、Smart Home）
- 「Team」按鈕導航到成員管理頁
- 「Settings」按鈕（待實作）
- 返回按鈕

**元件**: `WorkspaceDetailPage`
**檔案位置**: `src/static/src/paas/pages/workspace/WorkspaceDetailPage.js`

**狀態**: ✅ 完成（Settings 功能待實作）

### 3. 團隊成員頁 (`#/workspace/<id>/team`)

**功能**:
- 顯示 Workspace 所有成員
- 每位成員顯示：頭像、名稱、Email、角色徽章、邀請資訊
- 「Invite Member」按鈕開啟邀請 Modal（僅 Owner/Admin 可見）
- 角色下拉選單變更角色（僅 Owner/Admin 可用）
- 移除成員按鈕（僅 Owner/Admin 可用，不可移除 Owner）
- 返回按鈕

**元件**: `WorkspaceTeamPage`
**檔案位置**: `src/static/src/paas/pages/workspace/WorkspaceTeamPage.js`

**狀態**: ✅ 完成

### 4. 建立 Workspace Modal

**功能**:
- 輸入 Workspace 名稱（必填）
- 輸入描述（選填）
- 驗證與錯誤提示
- 建立成功後導航到詳情頁

**元件**: `CreateWorkspaceModal`
**檔案位置**: `src/static/src/paas/components/modal/CreateWorkspaceModal.js`

**狀態**: ✅ 完成

### 5. 邀請成員 Modal

**功能**:
- 輸入成員 Email（必填）
- 選擇角色（Admin、User、Guest）
- 驗證與錯誤提示
- 邀請成功後更新成員列表

**元件**: `InviteMemberModal`
**檔案位置**: `src/static/src/paas/components/modal/InviteMemberModal.js`

**狀態**: ✅ 完成

---

## 前端服務

### workspaceService

Reactive 服務物件，管理所有 Workspace 相關 API 呼叫與狀態。

**方法**:

| 方法 | 參數 | 回傳 | 說明 |
|------|------|------|------|
| `fetchWorkspaces()` | - | void | 取得所有 Workspace 並更新 state |
| `createWorkspace(payload)` | `{name, description}` | `{success, data/error}` | 建立 Workspace |
| `getWorkspace(id)` | `number` | `{success, data/error}` | 取得單一 Workspace |
| `updateWorkspace(id, payload)` | `number, {name?, description?}` | `{success, data/error}` | 更新 Workspace |
| `deleteWorkspace(id)` | `number` | `{success, error?}` | 刪除 Workspace |
| `getMembers(id)` | `number` | `{success, data/error}` | 取得成員列表 |
| `inviteMember(id, payload)` | `number, {email, role}` | `{success, data/error}` | 邀請成員 |
| `updateMemberRole(wsId, accessId, role)` | `number, number, string` | `{success, data/error}` | 更新角色 |
| `removeMember(wsId, accessId)` | `number, number` | `{success, error?}` | 移除成員 |

**狀態屬性**:
- `workspaces: []` - Workspace 列表
- `loading: boolean` - 載入狀態
- `error: string | null` - 錯誤訊息

**檔案位置**: `src/static/src/paas/services/workspace_service.js`

---

## 路由

| 路由 | 頁面 | 參數 |
|------|------|------|
| `#/workspaces` | WorkspaceListPage | - |
| `#/workspace/<id>` | WorkspaceDetailPage | `workspaceId: number` |
| `#/workspace/<id>/team` | WorkspaceTeamPage | `workspaceId: number` |

**檔案位置**: `src/static/src/paas/root.js`

---

## 已知問題與修復記錄

### 已修復 (2026-01-31)

| 問題 | 原因 | 解決方案 |
|------|------|----------|
| API 500 錯誤 | `check_access` 與 Odoo ORM 內建方法衝突 | 重新命名為 `check_user_access` |
| 錯誤訊息顯示 `[object Object]` | 前端未處理物件型錯誤 | 加入型別檢查並轉換為字串 |
| Logout 無功能 | 只是空連結 `href="#"` | 實作 `logout()` 方法 |
| Help 無功能 | 只是空連結 `href="#"` | 實作 `openHelp()` 方法 |

---

## 待辦事項

### 高優先

- [ ] Workspace Settings 頁面實作
- [ ] 所有權轉移 UI 實作
- [ ] 成員管理 E2E 測試

### 中優先

- [ ] Workspace 搜尋與過濾
- [ ] 成員邀請 Email 通知
- [ ] Workspace 活動日誌

### 低優先

- [ ] Workspace 匯出/匯入
- [ ] 批次成員管理
- [ ] Workspace 模板

---

## 相關檔案索引

```
src/
├── models/
│   ├── workspace.py           # Workspace 資料模型
│   └── workspace_access.py    # 存取控制模型
├── controllers/
│   └── paas.py                # API 控制器
└── static/src/paas/
    ├── services/
    │   └── workspace_service.js  # 前端 API 服務
    ├── pages/workspace/
    │   ├── WorkspaceListPage.js    # 列表頁
    │   ├── WorkspaceDetailPage.js  # 詳情頁
    │   └── WorkspaceTeamPage.js    # 團隊頁
    └── components/modal/
        ├── CreateWorkspaceModal.js # 建立 Modal
        └── InviteMemberModal.js    # 邀請 Modal
```
