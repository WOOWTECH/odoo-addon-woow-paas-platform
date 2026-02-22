---
name: project-cloud-service-binding
description: Bind support projects to cloud services instead of workspaces, enabling AI context-aware technical support
status: backlog
created: 2026-02-22T05:01:26Z
---

# PRD: project-cloud-service-binding

## Executive Summary

將 Support Project 的關聯從 Workspace 改為 Cloud Service，讓每個 Project 對應一個具體的雲端服務實例。當使用者在 Project 中提出技術問題時，AI 可以根據關聯的 Cloud Service 資訊（template、helm values、狀態）提供精準的 system context，大幅提升技術支援品質。

## Problem Statement

目前 Support Project (`project.project`) 透過 `workspace_id` 與 Workspace 關聯，但實際使用場景是：使用者想針對某個**特定的 Cloud Service**（如 AnythingLLM、n8n）提出技術問題。Workspace 層級太高（一個 Workspace 可能有多個 Cloud Services），導致：

1. AI 回答時缺乏具體的服務 context（不知道使用者在問哪個 service）
2. 無法自動帶入 service 的配置資訊作為 system prompt
3. Project 與實際服務之間的關係模糊

## User Stories

### US-1: 建立 Cloud Service Support Project

**角色**：Workspace 成員
**情境**：我部署了一個 AnythingLLM service，想針對它建立 support project
**操作**：

1. 進入 Cloud Service 詳情頁
2. 點擊「建立 Support Project」按鈕
3. 填寫 Project 名稱
4. 系統建立 Project 並關聯到此 Cloud Service

**驗收條件**：
- [ ] Cloud Service 詳情頁有「建立 Support Project」按鈕
- [ ] 建立時自動關聯 `cloud_service_id`
- [ ] 一個 Cloud Service 只能有一個 Project（1:1）
- [ ] 已有 Project 時，按鈕改為「查看 Support Project」

### US-2: AI 帶入 Service Context 回答

**角色**：Workspace 成員
**情境**：我在 AnythingLLM 的 support project 中提問 "為什麼 LLM 回應很慢？"
**期望**：AI 能看到我的 AnythingLLM 配置（model、memory limit、replicas 等），給出針對性的建議

**驗收條件**：
- [ ] AI 回答時 system prompt 包含 Cloud Service 的 template 資訊
- [ ] AI 回答時 system prompt 包含當前 helm values 配置
- [ ] AI 回答時 system prompt 包含服務狀態（running/stopped/error）

### US-3: 從 Service 詳情頁進入 Support Project

**角色**：Workspace 成員
**情境**：我想看某個 service 的所有技術支援紀錄
**操作**：從 Cloud Service 詳情頁點擊進入對應的 Support Project

**驗收條件**：
- [ ] Cloud Service 詳情頁顯示關聯的 Project 連結
- [ ] 點擊後導航到 Project 的 Tasks/Kanban 頁面

## Requirements

### Functional Requirements

#### FR-1: Model 變更
- 移除 `project.project` 上的 `workspace_id` 欄位
- 新增 `cloud_service_id` (Many2one) 欄位，關聯到 `woow_paas_platform.cloud_service`
- 在 `cloud_service` 上新增 `project_id` (One2one/反向) 欄位
- 確保 1:1 關係（一個 service 最多一個 project）

#### FR-2: API 變更
- 修改 project 相關 API endpoints，改用 `cloud_service_id` 替代 `workspace_id`
- 新增建立 project 的 endpoint（帶 `cloud_service_id` 參數）
- 權限驗證改為透過 `cloud_service.workspace_id` 間接檢查

#### FR-3: AI System Context
- 當 Project 關聯了 Cloud Service 時，AI 回答自動帶入：
  - Template 資訊：app name、description、category
  - Helm values：當前配置參數
  - Service 狀態：state、URL、error message（如有）
- Context 格式化為 system prompt 附加段落

#### FR-4: 前端 UI
- Cloud Service 詳情頁新增「建立/查看 Support Project」按鈕
- 建立 Project 時帶入 `cloud_service_id`
- Support Projects 列表顯示關聯的 Cloud Service 名稱
- 移除 workspace 相關的 project 篩選邏輯

### Non-Functional Requirements

- 資料遷移：現有 project 的 `workspace_id` 需遷移到對應的 `cloud_service_id`（如果可以對應）
- 向下相容：無法對應的 project 保留為 `cloud_service_id = null`
- 效能：AI context 組裝不應增加超過 100ms 延遲

## Success Criteria

- 使用者可以從 Cloud Service 詳情頁建立 Support Project
- AI 回答時能正確帶入該 Cloud Service 的 template + helm values + 狀態
- 現有功能（task 建立、AI chat、kanban）不受影響
- 所有 project 的 workspace 篩選改為透過 cloud_service.workspace_id

## Constraints & Assumptions

- 假設 Cloud Service 已經存在才能建立 Project
- 假設一個 Cloud Service 最多對應一個 Support Project
- 現有沒有 Cloud Service 對應的 Project 將保留為 orphan（`cloud_service_id = null`）

## Out of Scope

- 自動建立 Project（使用者手動建立）
- 多個 Project 對應一個 Cloud Service
- Cloud Service 刪除時自動刪除 Project（改為 set null）
- 修改 Odoo backend views（僅影響 /woow 前端）

## Dependencies

- Cloud Service 模型已實作（Phase 4 已完成）
- AI Assistant 功能已實作（ai-assistant-refactor epic 已完成）
- `/woow` 前端應用已運作
