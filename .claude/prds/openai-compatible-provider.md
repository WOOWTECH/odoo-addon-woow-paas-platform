---
name: openai-compatible-provider
description: Add OpenAI Compatible type to ai.config with configurable host and API key
status: backlog
created: 2026-02-21T15:21:24Z
---

# PRD: openai-compatible-provider

## Executive Summary

在 `woow_paas_platform` addon 中擴充 `ai.config` 模型，新增 "OpenAI Compatible" type，支援使用者自訂 API endpoint URL 和 API key，使其可以連接任何 OpenAI 相容的 API（如 OpenAI、Azure OpenAI、Ollama、vLLM、LiteLLM 等）。

## Problem Statement

目前 `ai.config` 只有兩個 type：
- `odooai` — Odoo 內建 AI（由 `ai_base_gt` 提供）
- `chatgpt` — 硬編碼連接 `api.openai.com`（由 `odoo_ai_assistant_chatgpt_connector` 提供），且強制要求 API key 以 `sk-` 開頭

使用者無法：
1. 連接自建的 OpenAI 相容服務（如 Ollama、vLLM）
2. 使用非 OpenAI 發行的 API key（如 Azure OpenAI 的 key 不是 `sk-` 開頭）
3. 自訂 API endpoint URL

而 `woow_paas_platform` 自己的 `AIClient`（使用 LangChain `ChatOpenAI`）已經支援 `base_url` 參數，只是 `ai.config` 模型缺少 `api_base_url` 欄位讓使用者設定。

## User Stories

### US-1: 系統管理員設定 OpenAI Compatible Provider
**As** 系統管理員
**I want** 在 AI Configurations 中建立一個 "OpenAI Compatible" 設定，填入自定義的 host URL 和 API key
**So that** 我可以連接任何 OpenAI 相容的 API 服務

**Acceptance Criteria:**
- [ ] `ai.config` 新增 `openai_compatible` type 選項
- [ ] 選擇此 type 時，顯示 "API Base URL" 欄位
- [ ] API Base URL 預設值為 `https://api.openai.com/v1`
- [ ] API key 不限制格式（不強制 `sk-` 開頭）
- [ ] 可正常儲存和讀取設定

### US-2: 透過 OpenAI Compatible Provider 進行 AI 對話
**As** portal 使用者
**I want** 在 AI Chat 中使用 OpenAI Compatible 設定的 AI 助手對話
**So that** 我可以使用任何 OpenAI 相容的模型

**Acceptance Criteria:**
- [ ] `AIClient.from_assistant()` 正確讀取 `api_base_url` 欄位
- [ ] 對話功能正常（發送訊息、接收 AI 回覆）
- [ ] 串流回覆正常運作

## Requirements

### Functional Requirements

1. **擴充 ai.config 模型**
   - 在 `woow_paas_platform` 中 `_inherit 'ai.config'`
   - 新增 `api_base_url` 欄位（Char, 可選）
   - 新增 `openai_compatible` type（via `selection_add`）
   - `openai_compatible` type 時，顯示 `api_base_url` 欄位

2. **更新 Settings 表單 View**
   - 在 ai.config form view 中加入 `api_base_url` 欄位
   - 使用 `attrs` 控制 visible 條件（`type == 'openai_compatible'` 時顯示）

3. **連線測試**
   - `AIClient.from_assistant()` 已支援 `config.api_base_url`，無需修改
   - Settings 中的「Test AI Connection」按鈕已可正常運作

### Non-Functional Requirements

- 不修改 `ai_base_gt` 或 `odoo_ai_assistant_chatgpt_connector` 原始程式碼
- 與現有 ChatGPT type 共存，不影響其功能
- API key 以 `groups='base.group_system'` 保護，僅系統管理員可見

## Success Criteria

- 可在 AI Configurations 中建立 "OpenAI Compatible" type 的設定
- 填入任意 OpenAI 相容的 host URL 和 API key 後，AI Chat 對話正常
- 現有 ChatGPT / OdooAI type 的設定不受影響

## Constraints & Assumptions

- **假設**：使用者填入的 URL 指向一個支援 OpenAI Chat Completions API 的服務
- **假設**：API key 以 Bearer token 方式傳送（OpenAI SDK 預設行為）
- **限制**：不支援非 OpenAI 相容的 API 格式（如 Anthropic Claude API 需要不同的 client）
- **限制**：使用 LangChain `ChatOpenAI` 作為底層，不直接呼叫 HTTP

## Out of Scope

- 不修改 `ai_base_gt` 原始碼
- 不修改 `odoo_ai_assistant_chatgpt_connector` 原始碼
- 不建立獨立的新 addon
- 不處理 non-OpenAI-compatible 的 API（如 Anthropic、Google Gemini 原生 API）
- 不處理 OAuth 2.0 認證流程

## Dependencies

- `ai_base_gt` 模組已安裝（提供 `ai.config` 基礎模型）
- `langchain-openai` Python 套件已安裝（`AIClient` 使用）
- `openai` Python 套件已安裝（OpenAI SDK）
