---
name: support-crud-ui
status: backlog
created: 2026-02-10T06:42:15Z
progress: 0%
prd: .claude/prds/support-crud-ui.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/33
---

# Epic: Support Projects & Tasks Create UI

## Overview

為 Support Projects 和 My Support Tasks 兩個既有頁面補齊 **Create** 功能。後端 API 已完整支援 CRUD，前端 `supportService` 也已有 `createTask()` 方法，本 Epic 只需：

1. 新增 `createProject()` service 方法
2. 建立兩個 Create Modal 元件（參照 `CreateWorkspaceModal` 模式）
3. 在兩個頁面加入 Header 按鈕 + Empty State 引導按鈕

影響範圍小、風險低，所有基礎設施已就緒。

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| Modal 元件模式 | 參照 `CreateWorkspaceModal` 的 `onClose/onCreated` props | 與現有 codebase 一致，零學習成本 |
| 表單驗證 | 前端 inline validation | 簡單表單，不需要額外驗證框架 |
| Workspace/Project 下拉 | 從已有 service 載入 | `workspaceService` 和 `supportService` 已提供資料源 |
| 不共用 Generic Modal | 分別建 `CreateProjectModal` + `CreateTaskModal` | 表單欄位差異大，分開更清楚，避免過度抽象 |

## Technical Approach

### Frontend — Modal 元件

兩個 Modal 遵循相同模式（與 `CreateWorkspaceModal` 一致）：

```
CreateProjectModal
├── props: { onClose, onCreated }
├── state: { name, description, workspaceId, loading, error }
├── onMounted → 載入 workspace 列表
└── onSubmit → supportService.createProject()

CreateTaskModal
├── props: { onClose, onCreated, defaultProjectId? }
├── state: { name, description, projectId, priority, deadline, loading, error }
├── onMounted → 載入 project 列表
└── onSubmit → supportService.createTask()
```

### Frontend — 頁面修改

兩個頁面的修改模式一致：
1. 加入 `showCreateModal` state
2. Header 右側加 `WoowButton`（「+ New Project」/「+ New Task」）
3. Empty State 加 `WoowButton`（「Create your first project」/「Create your first task」）
4. 加入 `onCreated` callback 重新載入列表

### Frontend — Service 擴充

`support_service.js` 新增 `createProject(workspaceId, data)` 方法，模式與既有 `createTask()` 相同。

### Backend

不需任何後端改動。API 已就緒：
- `POST /api/support/projects/<workspace_id>` with `action: 'create'`
- `POST /api/support/tasks/<workspace_id>` with `action: 'create'`

## Implementation Strategy

**單一階段交付** — 所有改動在前端且相互關聯，一次完成：

1. 先擴充 `support_service.js`（加 `createProject()`）
2. 建立兩個 Modal 元件
3. 修改兩個頁面（加按鈕 + 整合 Modal）
4. 更新 `__manifest__.py` 註冊新檔案
5. 手動測試完整 create 流程

## Task Breakdown Preview

- [ ] Task 1: `support_service.js` 新增 `createProject()` 方法
- [ ] Task 2: `CreateProjectModal` 元件（JS + XML）+ `SupportProjectsPage` 整合（Header 按鈕 + Empty State 按鈕）
- [ ] Task 3: `CreateTaskModal` 元件（JS + XML）+ `SupportTasksPage` 整合（Header 按鈕 + Empty State 按鈕）
- [ ] Task 4: 更新 `__manifest__.py` 註冊新元件 + 手動驗證

## Dependencies

### Internal
- `CreateWorkspaceModal`（設計模式參考）
- `workspaceService`（Workspace 列表資料源）
- `supportService`（Project 列表資料源 + API 呼叫）
- 後端 `_create_project()` / `_create_task()`（已實作）

### External
- 無

## Success Criteria (Technical)

| Criteria | Target |
|----------|--------|
| Create Project 流程 | 填表 → 提交 → 新專案出現在列表，< 3 秒 |
| Create Task 流程 | 填表 → 提交 → 新任務出現在 Kanban，< 3 秒 |
| 表單驗證 | 必填欄位空白時阻擋提交並顯示錯誤 |
| Loading 狀態 | 提交期間按鈕 disabled + loading indicator |
| 錯誤處理 | API 失敗顯示具體錯誤訊息 |
| Modal 行為 | 點 backdrop / X 按鈕可關閉，ESC 鍵可關閉 |

## Tasks Created

- [ ] #34 - Add createProject() to support_service.js (parallel: false)
- [ ] #35 - CreateProjectModal + SupportProjectsPage integration (parallel: true)
- [ ] #36 - CreateTaskModal + SupportTasksPage integration (parallel: true)
- [ ] #37 - Register new components in __manifest__.py and verify (parallel: false)

Total tasks: 4
Parallel tasks: 2 (#35, #36 can run simultaneously after #34)
Sequential tasks: 2 (#34 first, #37 last)
Estimated total effort: 3 hours

### Dependency Graph

```
#34 (createProject service)
 ├── #35 (Project Modal + Page)  ─┐
 └── #36 (Task Modal + Page)     ─┴── #37 (manifest + verify)
```

## Estimated Effort

- **Overall**: S（Small）
- **Tasks**: 4 個
- **預估工時**: 2-3 小時
- **Critical Path**: #34 → (#35 || #36) → #37
