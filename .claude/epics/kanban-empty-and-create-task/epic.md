---
name: kanban-empty-and-create-task
status: backlog
created: 2026-02-10T15:41:44Z
progress: 0%
prd: .claude/prds/kanban-empty-and-create-task.md
updated: 2026-02-10T15:57:34Z
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/52
---

# Epic: kanban-empty-and-create-task

## Overview

修復 Project Kanban Board 兩個核心問題：(1) 沒有 stages 的 project 顯示空白；(2) 缺少建立任務功能。採用最小化改動策略：後端新增一個 ensure-stages API 端點，前端在 `ProjectKanbanPage` 整合現有的 `CreateTaskModal` 組件。

## Architecture Decisions

- **自動初始化 stages 而非手動建立**：當 project 沒有 stages 時，後端自動建立預設 stages（New, In Progress, Done），減少使用者操作步驟
- **複用 `CreateTaskModal`**：已有完整的任務建立 modal，直接在 kanban 頁面引入，不重複造輪子
- **後端驅動 stage 初始化**：由後端判斷並建立預設 stages，確保資料一致性
- **`CreateTaskModal` 增加 `defaultStageId` prop**：讓 kanban column 的 "+" 按鈕可以指定預設 stage

## Technical Approach

### Backend Changes

**`src/controllers/ai_assistant.py`**:
- 修改 `_get_project_stages()` 或新增 `_ensure_project_stages()`：當 stages 為空時自動建立預設 stages（New seq=1, In Progress seq=5, Done seq=10）並關聯到該 project
- 在 `api_support_project_stages()` 中呼叫 ensure 邏輯，讓前端無需額外 API 呼叫

### Frontend Changes

**`src/static/src/paas/pages/project-kanban/ProjectKanbanPage.js`**:
- Import 並註冊 `CreateTaskModal`
- 新增 `state.showCreateModal` 和 `state.defaultStageId`
- 新增 `openCreateModal(stageId?)` 和 `closeCreateModal()` 方法
- 新增 `onTaskCreated()` callback，建立成功後重新載入 kanban

**`src/static/src/paas/pages/project-kanban/ProjectKanbanPage.xml`**:
- Header 區域新增 "+" 建立任務按鈕
- 每個 column header 新增 "+" 按鈕（帶 stageId 參數）
- 空白狀態改善文案
- 底部加入 `CreateTaskModal` 條件渲染

**`src/static/src/paas/components/modal/CreateTaskModal.js`**:
- 新增 optional prop `defaultStageId`
- 在 `onSubmit()` 中若有 `defaultStageId` 則帶入 `stage_id`

## Implementation Strategy

分 3 個 task 實作，由簡到複雜：
1. 後端：自動初始化 stages（確保 kanban 不再空白）
2. 前端：ProjectKanbanPage 整合 CreateTaskModal
3. 前端：CreateTaskModal 支援 defaultStageId + column 內快速建立

## Task Breakdown Preview

- [ ] Task 1: 後端自動初始化 project stages（修改 `_get_project_stages` 或新增 ensure 邏輯）
- [ ] Task 2: ProjectKanbanPage 整合 CreateTaskModal（header 按鈕 + 空白狀態改善）
- [ ] Task 3: Column 內快速建立任務（CreateTaskModal 新增 defaultStageId prop + column "+" 按鈕）

## Dependencies

- `CreateTaskModal` 組件（已存在，需小幅擴展）
- `supportService.createTask()` API（已存在）
- `supportService.fetchProjectStages()` API（已存在）
- Odoo `project.task.type` model（Odoo 標準 model）

## Success Criteria (Technical)

- 所有 project 的 kanban 頁面都能顯示 columns（stages 自動初始化）
- 使用者可從 header 按鈕建立任務
- 使用者可從 column 內的 "+" 按鈕建立任務（預設 stage）
- 建立任務後 kanban 自動刷新

## Estimated Effort

- Task 1（後端）：30 分鐘
- Task 2（前端整合）：45 分鐘
- Task 3（column 快速建立）：30 分鐘
- 總計：約 2 小時

## Tasks Created

- [ ] #53 - Auto-initialize project stages in backend (parallel: false)
- [ ] #54 - Integrate CreateTaskModal into ProjectKanbanPage (parallel: false, depends: #53)
- [ ] #55 - Add per-column quick-create with defaultStageId (parallel: false, depends: #54)

Total tasks: 3
Parallel tasks: 0
Sequential tasks: 3
Estimated total effort: 1.75 hours
