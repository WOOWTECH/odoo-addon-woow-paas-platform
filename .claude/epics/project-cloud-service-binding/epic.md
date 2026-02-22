---
name: project-cloud-service-binding
status: backlog
created: 2026-02-22T05:10:34Z
updated: 2026-02-22T06:53:17Z
progress: 0%
prd: .claude/prds/project-cloud-service-binding.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/81
---

# Epic: project-cloud-service-binding

## Overview

將 Support Project 的關聯從 Workspace 改為 Cloud Service，實現 1:1 對應。改動涵蓋 model 欄位替換、API endpoint 參數調整、AI system prompt 注入 Cloud Service context（template + helm values + 狀態），以及前端 UI 從 Cloud Service 詳情頁建立/查看 Project 的流程。

## Architecture Decisions

1. **直接替換 workspace_id → cloud_service_id** — 在 `project.project` 上移除 `workspace_id`，新增 `cloud_service_id`（Many2one, ondelete='set null'）。透過 `cloud_service_id.workspace_id` 間接取得 workspace 以維持權限檢查。

2. **Cloud Service 端加 project_id 反向關聯** — 在 `cloud_service` 上新增 `project_ids`（One2many），並加 SQL constraint 確保 1:1 關係。

3. **AI Context 注入在 discuss_channel._post_ai_reply()** — 擴充現有的 system prompt 組裝邏輯，當 project 有關聯的 cloud service 時，附加 template 資訊、helm values、服務狀態到 system prompt。

4. **API endpoint 路由保持 RESTful** — 將 `/api/support/projects/<workspace_id>` 改為 `/api/support/cloud-services/<cloud_service_id>/projects`，權限透過 cloud_service.workspace_id 檢查。

5. **前端從 Cloud Service 詳情頁建立 Project** — 在 WorkspaceDetailPage 的 Cloud Service 卡片上新增「建立/查看 Support Project」按鈕，CreateProjectModal 改為接收 cloud_service_id。

## Technical Approach

### Backend (Python Models)

**修改 `src/models/project_project.py`：**
- 移除 `workspace_id` 欄位
- 新增 `cloud_service_id = fields.Many2one('woow_paas_platform.cloud_service', ondelete='set null', index=True)`
- 新增 `workspace_id` computed field（透過 `cloud_service_id.workspace_id`，用於權限檢查相容性）

**修改 `src/models/cloud_service.py`：**
- 新增 `project_ids = fields.One2many('project.project', 'cloud_service_id')`
- 新增 SQL constraint：一個 cloud service 最多一個 project

**修改 `src/models/discuss_channel.py`：**
- 在 `_post_ai_reply()` 中，當 channel 對應的 project 有 cloud_service_id 時，組裝額外 context：
  - Template: app name, description, category
  - Helm values: 當前配置
  - Service state: running/stopped/error + URL + error message

### Backend (Controllers/API)

**修改 `src/controllers/ai_assistant.py`：**
- `_check_project_access()`: 改為透過 `project.cloud_service_id.workspace_id` 檢查
- `/api/support/projects/<workspace_id>` 路由：改為 `/api/support/cloud-services/<cloud_service_id>/projects`
- `_create_project()`: 接收 `cloud_service_id` 替代 `workspace_id`
- `_list_projects()`: 改為依 `cloud_service_id` 或透過 workspace 間接篩選
- `api_support_stats()`: 改為透過 `cloud_service_id.workspace_id` 篩選

### Frontend (JavaScript/OWL)

**修改 `src/static/src/paas/services/support_service.js`：**
- `fetchProjects()`: 參數改為 `cloudServiceId`
- `createProject()`: 帶入 `cloud_service_id`
- 其他 API call 對應調整

**修改 `src/static/src/paas/components/modal/CreateProjectModal.js`：**
- 移除 workspace 選擇邏輯
- 改為接收 `cloudServiceId` prop，直接關聯

**修改 `src/static/src/paas/pages/`：**
- Cloud Service 詳情頁新增「建立/查看 Support Project」按鈕
- SupportProjectsPage 顯示關聯的 Cloud Service 名稱

### Security

**修改 `src/security/ir_rules.xml`：**
- project.project 的 access rule 改為透過 `cloud_service_id.workspace_id.access_ids.user_id` 檢查

### Infrastructure

無基礎設施變更，純 Odoo addon 程式碼。

## Implementation Strategy

分為 4 個 tasks，按依賴順序執行：

1. **Model 變更** (Task 1) — 修改 project_project.py + cloud_service.py + security rules。這是基礎，其他 task 都依賴它。
2. **API + AI Context** (Task 2, 依賴 Task 1) — 修改 ai_assistant.py controller + discuss_channel.py AI context。
3. **Frontend UI** (Task 3, 依賴 Task 2) — 修改 support_service.js + CreateProjectModal + 相關頁面。
4. **整合測試 + 部署驗證** (Task 4, 依賴 Task 2 & 3) — 端到端驗證所有功能。

## Task Breakdown Preview

- [ ] Task 1: Model 變更 — project.project 加 cloud_service_id、cloud_service 加 project_ids + constraint、security rules
- [ ] Task 2: API + AI Context — controller endpoint 調整、權限檢查、discuss_channel AI context 注入
- [ ] Task 3: Frontend UI — support_service.js、CreateProjectModal、Cloud Service 頁面按鈕
- [ ] Task 4: 整合測試 + 部署驗證 — 部署到本地 Odoo、驗證所有 user stories

## Dependencies

- Cloud Service 模型已實作（Phase 4 完成）
- AI Assistant 功能已實作（ai-assistant-refactor epic 完成）
- `/woow` 前端應用已運作

## Success Criteria (Technical)

- `project.project` 有 `cloud_service_id` 欄位，workspace_id 移除或改為 computed
- 一個 Cloud Service 最多對應一個 Project（SQL constraint）
- API endpoints 使用 `cloud_service_id` 參數
- AI 回答時 system prompt 包含 template + helm values + 服務狀態
- Cloud Service 詳情頁有「建立/查看 Support Project」按鈕
- 現有 task 建立、AI chat、kanban 功能不受影響

## Estimated Effort

- **Size**: M（中等）
- **Tasks**: 4
- **Risk**: 中（涉及 model 欄位替換 + API 路由變更 + 前端調整，但結構清晰）

## Tasks Created

- [ ] #82 - Model changes - cloud_service_id on project + project_ids on cloud_service (parallel: true)
- [ ] #83 - API endpoints + AI context injection (parallel: false)
- [ ] #84 - Frontend UI - CreateProjectModal + Cloud Service detail page (parallel: false)
- [ ] #85 - Integration testing + deployment verification (parallel: false)

Total tasks: 4
Parallel tasks: 1
Sequential tasks: 3
Estimated total effort: 15 hours
