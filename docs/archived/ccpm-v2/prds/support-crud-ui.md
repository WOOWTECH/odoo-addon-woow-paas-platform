---
name: support-crud-ui
description: 為 Support Projects 和 My Support Tasks 頁面新增建立（Create）功能的 UI，包含 Modal 表單、前端 service 方法、以及 Empty State 引導。
status: backlog
created: 2026-02-10T06:36:06Z
updated: 2026-02-10T06:36:06Z
---

# PRD: Support Projects & Tasks Create UI

## Executive Summary

目前 Support Projects 和 My Support Tasks 兩個頁面為**純唯讀瀏覽**模式，使用者無法在 `/woow` 前台建立新專案或新任務。後端 API 已支援 `create` action，前端 `support_service.js` 也已有 `createTask()` 方法，但 UI 層完全缺少建立入口。

本 PRD 旨在補齊這兩個頁面的 CRUD 中的 **Create** 功能，讓使用者可以直接在前台完成專案和任務的建立操作。

## Problem Statement

### 現狀問題

1. Support Projects 頁面（`#/ai-assistant/projects`）**沒有「New Project」按鈕**，Empty State 僅顯示提示文字
2. My Support Tasks 頁面（`#/ai-assistant/tasks`）**沒有「New Task」按鈕**，Empty State 提示使用者去建立 Project 但沒有實際操作入口
3. 使用者若要建立 Project 或 Task，需切換到 Odoo 後台操作，體驗斷裂
4. 後端 API 和前端 Service 已具備 create 能力，僅差 UI 層整合

### 為什麼現在做

- 兩個頁面的 UI 和 API 都已就緒，缺的只是「最後一哩路」的建立表單
- 不補齊 Create 功能，這兩個頁面對使用者而言形同擺設
- 實作成本低（參考既有的 `CreateWorkspaceModal` 模式即可）

## User Stories

### US-001: 在前台建立 Support Project

**作為** Workspace 成員，**我想要** 在 Support Projects 頁面直接建立新專案，**以便** 不需要切換到 Odoo 後台。

**驗收標準**：
- [ ] Support Projects Header 右側有「New Project」按鈕
- [ ] 點擊按鈕彈出 Modal 表單
- [ ] 表單欄位：Project Name（必填）、Description（選填）、Workspace（必填下拉選單）
- [ ] 提交成功後，新專案即時出現在列表中
- [ ] 提交失敗時顯示錯誤訊息
- [ ] Empty State 增加「Create your first project」按鈕，同樣觸發 Modal

### US-002: 在前台建立 Support Task

**作為** Workspace 成員，**我想要** 在 My Support Tasks 頁面直接建立新任務，**以便** 快速建立待辦事項。

**驗收標準**：
- [ ] My Support Tasks Header 右側有「New Task」按鈕
- [ ] 點擊按鈕彈出 Modal 表單
- [ ] 表單欄位：Task Name（必填）、Description（選填）、Project（必填下拉選單）、Priority（選填）、Deadline（選填）
- [ ] 提交成功後，新任務即時出現在 Kanban 對應的 Project 欄位中
- [ ] 提交失敗時顯示錯誤訊息
- [ ] Empty State 增加「Create your first task」按鈕，同樣觸發 Modal

## Requirements

### Functional Requirements

#### FR-1: Create Project Modal

- **FR-1.1**: Header 右側新增 `WoowButton`「+ New Project」
- **FR-1.2**: Modal 元件 `CreateProjectModal`，參考 `CreateWorkspaceModal` 的 props 模式（`onClose`, `onCreated`）
- **FR-1.3**: 表單欄位：
  | 欄位 | 類型 | 必填 | 說明 |
  |------|------|------|------|
  | name | text input | Y | 專案名稱 |
  | description | textarea | N | 專案描述 |
  | workspace_id | select dropdown | Y | 所屬 Workspace（從已有 workspace 列表載入） |
- **FR-1.4**: 呼叫 `POST /api/support/projects/<workspace_id>` with `action: 'create'`
- **FR-1.5**: 成功後呼叫 `onCreated` callback，重新整理專案列表
- **FR-1.6**: Empty State 新增「Create your first project」`WoowButton`，點擊觸發同一個 Modal

#### FR-2: Create Task Modal

- **FR-2.1**: Header 右側新增 `WoowButton`「+ New Task」
- **FR-2.2**: Modal 元件 `CreateTaskModal`，同樣使用 `onClose`, `onCreated` props
- **FR-2.3**: 表單欄位：
  | 欄位 | 類型 | 必填 | 說明 |
  |------|------|------|------|
  | name | text input | Y | 任務標題 |
  | description | textarea | N | 任務描述 |
  | project_id | select dropdown | Y | 所屬專案（從已有 project 列表載入） |
  | priority | select / radio | N | 優先級（0=Low, 1=Normal, 2=High, 3=Urgent），預設 0 |
  | date_deadline | date input | N | 截止日期 |
- **FR-2.4**: 呼叫 `supportService.createTask(workspaceId, data)`（已存在的方法）
- **FR-2.5**: 成功後重新整理任務列表
- **FR-2.6**: Empty State 新增「Create your first task」`WoowButton`

#### FR-3: support_service.js 擴充

- **FR-3.1**: 新增 `createProject(workspaceId, data)` 方法（目前缺少）
- **FR-3.2**: `createTask()` 已存在，無需修改

### Non-Functional Requirements

- **NFR-1**: Modal 開啟/關閉動畫須與現有 Modal（如 `CreateWorkspaceModal`）一致
- **NFR-2**: 表單提交時顯示 loading 狀態，防止重複提交
- **NFR-3**: SCSS 命名遵循 `o_woow_` prefix 慣例
- **NFR-4**: 在各螢幕尺寸下 Modal 需正常顯示（響應式）

## Technical Architecture

### 現有基礎設施（已就緒）

| 層級 | 狀態 | 說明 |
|------|------|------|
| 後端 API（Project Create） | ✅ 已有 | `ai_assistant.py:497-500` — `_create_project(workspace, kwargs)` |
| 後端 API（Task Create） | ✅ 已有 | `ai_assistant.py:535-538` — `_create_task(workspace, kwargs)` |
| 前端 Service（Task Create） | ✅ 已有 | `support_service.js:213-232` — `createTask()` |
| 前端 Service（Project Create） | ❌ 缺少 | 需新增 `createProject()` 方法 |
| UI — Create Project Modal | ❌ 缺少 | 需新增元件 |
| UI — Create Task Modal | ❌ 缺少 | 需新增元件 |
| UI — Header 按鈕 | ❌ 缺少 | 兩個頁面都需加按鈕 |
| UI — Empty State 按鈕 | ❌ 缺少 | 兩個頁面都需加按鈕 |

### 需要新增/修改的檔案

| 檔案 | 操作 | 說明 |
|------|------|------|
| `src/static/src/paas/components/modal/CreateProjectModal.js` | create | Modal 元件 JS |
| `src/static/src/paas/components/modal/CreateProjectModal.xml` | create | Modal 元件 Template |
| `src/static/src/paas/components/modal/CreateTaskModal.js` | create | Modal 元件 JS |
| `src/static/src/paas/components/modal/CreateTaskModal.xml` | create | Modal 元件 Template |
| `src/static/src/paas/pages/support-projects/SupportProjectsPage.js` | edit | 加入 Modal 狀態、按鈕 handler |
| `src/static/src/paas/pages/support-projects/SupportProjectsPage.xml` | edit | Header 加按鈕、Empty State 加按鈕 |
| `src/static/src/paas/pages/support-tasks/SupportTasksPage.js` | edit | 加入 Modal 狀態、按鈕 handler |
| `src/static/src/paas/pages/support-tasks/SupportTasksPage.xml` | edit | Header 加按鈕、Empty State 加按鈕 |
| `src/static/src/paas/services/support_service.js` | edit | 新增 `createProject()` 方法 |
| `src/__manifest__.py` | edit | 註冊新元件檔案（如果需要） |

### 元件架構

```
CreateProjectModal (new)
├── props: { onClose: Function, onCreated: Function }
├── state: { name, description, workspaceId, loading, error }
├── 載入 workspace 列表（從 workspaceService）
└── 呼叫 supportService.createProject()

CreateTaskModal (new)
├── props: { onClose: Function, onCreated: Function, defaultProjectId?: Number }
├── state: { name, description, projectId, priority, deadline, loading, error }
├── 載入 project 列表（從 supportService）
└── 呼叫 supportService.createTask()
```

### API 呼叫

**Create Project**:
```javascript
// support_service.js — 新增方法
async createProject(workspaceId, { name, description }) {
    const result = await jsonRpc(`/api/support/projects/${workspaceId}`, {
        action: "create",
        name,
        description,
    });
    // ...
}
```

**Create Task**（已存在）:
```javascript
// support_service.js:213 — 已有此方法
await supportService.createTask(workspaceId, {
    name, description, project_id, priority, date_deadline
});
```

## Success Criteria

| Metric | Target |
|--------|--------|
| 建立 Project 流程 | 從點擊按鈕到專案出現在列表 < 3 秒 |
| 建立 Task 流程 | 從點擊按鈕到任務出現在 Kanban < 3 秒 |
| 表單驗證 | 必填欄位未填時顯示錯誤，不送出請求 |
| 錯誤處理 | API 失敗時顯示明確錯誤訊息 |

## Constraints & Assumptions

### 技術限制

- Modal 設計參照 `CreateWorkspaceModal` 現有模式，不引入新的 UI 框架
- Workspace 下拉選單的資料來源為 `workspaceService`（已有）
- Project 下拉選單的資料來源為 `supportService.projects`（已有）

### 假設

- 使用者至少有一個 Workspace 才能建立 Project
- 使用者至少有一個 Project 才能建立 Task
- 後端 `_create_project()` 和 `_create_task()` 已正確實作並可正常運作

## Out of Scope

1. **編輯（Update）UI** — 本 PRD 僅涵蓋 Create，Update/Delete UI 留待後續
2. **批次建立** — 一次只建一個 Project 或 Task
3. **拖曳排序** — Kanban 的拖曳排序功能不在本次範圍
4. **進階表單欄位** — 如 Assignee 指派、Tags、Milestone 等進階欄位留待後續
5. **表單中直接建立 Workspace / Project** — 如果下拉選單為空，只引導使用者先去建立

## Dependencies

### Internal Dependencies

- `CreateWorkspaceModal` 元件模式（作為參考）
- `workspaceService`（提供 Workspace 列表給 Project Modal）
- `supportService`（提供 Project 列表給 Task Modal + API 呼叫）
- 後端 `ai_assistant.py` 的 `_create_project()` 和 `_create_task()`

### External Dependencies

- 無
