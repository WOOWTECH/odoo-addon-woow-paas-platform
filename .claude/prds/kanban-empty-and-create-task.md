---
name: kanban-empty-and-create-task
description: Fix empty kanban view for projects without stages and add create task functionality
status: backlog
created: 2026-02-10T15:36:00Z
---

# PRD: kanban-empty-and-create-task

## Executive Summary

Project Kanban Board 存在兩個關鍵問題：(1) 部分 project 進入後 kanban 顯示空白，沒有任何 columns 或 tasks；(2) 所有 project 的 kanban 頁面都缺少「建立任務」的功能。這兩個問題嚴重影響使用者體驗，導致 kanban 功能無法正常使用。

## Problem Statement

### 問題 1：Kanban 空白

當使用者進入如 `/woow#/ai-assistant/projects/3` 或 `/woow#/ai-assistant/projects/4` 時，kanban 完全空白。根因是這些 project 可能沒有配置 `project.task.type` stages。目前前端在 stages 為空時直接顯示 empty state，沒有提供任何引導或 fallback。

**影響路徑**：
1. `ProjectKanbanPage._loadData()` 呼叫 `supportService.fetchProjectStages(projectId)`
2. 後端 `_get_project_stages()` 查詢 `project.task.type` 中 `project_ids` 包含該 project 的 stages
3. 如果 project 沒有關聯任何 stage → 回傳空陣列
4. 前端 `columns.length === 0` → 顯示空白畫面

### 問題 2：缺少 Create Task 功能

`ProjectKanbanPage` 組件完全沒有實作任務建立功能——沒有按鈕、沒有 modal、沒有 API 呼叫。對比之下，`SupportTasksPage` 已有完整的 `CreateTaskModal` 整合。

## User Stories

### US-1：Project Manager 查看新建 Project 的 Kanban
**作為** project manager，**我希望** 當新建的 project 還沒有 stages 時看到友善的引導提示，**以便** 我知道需要先建立 stages 或自動使用預設 stages。

**Acceptance Criteria:**
- 當 project 沒有 stages 時，顯示引導訊息而非空白
- 提供「使用預設 Stages」按鈕，自動建立常見 stages（如 New, In Progress, Done）
- 或自動 fallback 到 workspace 級別的預設 stages

### US-2：使用者在 Kanban 中建立新任務
**作為** 團隊成員，**我希望** 能直接在 kanban 頁面建立新任務，**以便** 不需要切換到其他頁面。

**Acceptance Criteria:**
- 每個 kanban column 頂部有「+ 新增任務」按鈕
- 點擊後彈出 CreateTaskModal，預設 stage 為該 column 的 stage
- 建立成功後自動刷新 kanban，新任務顯示在對應 column

### US-3：使用者在空 Kanban 中建立任務
**作為** 團隊成員，**我希望** 即使 kanban 為空也能建立任務，**以便** 快速開始使用。

**Acceptance Criteria:**
- 空白狀態下提供明顯的「建立第一個任務」按鈕
- 自動建立預設 stages 後再建立任務

## Requirements

### Functional Requirements

#### FR-1：自動初始化 Project Stages
- 當 project 沒有 stages 時，後端自動建立預設 stages（New, In Progress, Done）
- 或在前端呼叫一個 API endpoint 來初始化 stages
- 確保 stages 與 `project.task.type` model 正確關聯

#### FR-2：Kanban 空狀態改善
- 空白狀態顯示友善訊息：「此專案尚未建立任何任務階段」
- 提供「初始化預設階段」按鈕
- 初始化完成後自動刷新 kanban

#### FR-3：Create Task 按鈕
- 在 kanban header 區域加入「+ 建立任務」按鈕
- 複用現有 `CreateTaskModal` 組件
- 預設 `project_id` 為當前 project
- 可選擇 stage（預設為第一個 stage 或指定 column）

#### FR-4：Column 內快速建立
- 每個 column 提供 inline 的「+ 新增」按鈕
- 點擊後以該 column 的 stage 作為預設值開啟 CreateTaskModal

### Non-Functional Requirements

#### NFR-1：效能
- 初始化 stages 操作應在 500ms 內完成
- 建立任務後刷新不應重新載入整頁

#### NFR-2：一致性
- UI 風格與現有 `SupportTasksPage` 的建立任務流程一致
- 使用相同的 `CreateTaskModal` 組件

## Success Criteria

- 所有 project 的 kanban 頁面都能正常顯示（不再出現空白）
- 使用者可以直接在 kanban 頁面建立任務
- 新建立的 project 自動擁有預設 stages

## Constraints & Assumptions

- 假設 `CreateTaskModal` 已支援 `project_id` 和 `stage_id` 參數
- 預設 stages 名稱參考 Odoo 標準：New, In Progress, Done
- 不修改現有 `project.task.type` model 結構

## Out of Scope

- Stage 的自定義管理（新增、刪除、重新命名 stages）
- Kanban 的拖拽排序改善（已在其他 epic 處理）
- 任務詳情編輯功能

## Dependencies

- `CreateTaskModal` 組件（已存在於 `src/static/src/paas/components/modal/`）
- `support_service.js` 的任務 CRUD API
- 後端 `ai_assistant.py` controller 的 stages 與 tasks endpoints
