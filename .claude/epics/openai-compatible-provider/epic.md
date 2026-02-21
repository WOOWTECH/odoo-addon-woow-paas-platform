---
name: openai-compatible-provider
status: backlog
created: 2026-02-21T15:24:27Z
updated: 2026-02-21T15:33:30Z
progress: 0%
prd: .claude/prds/openai-compatible-provider.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/79
---

# Epic: openai-compatible-provider

## Overview

在 `woow_paas_platform` addon 中擴充 `ai.config` 模型，新增 "OpenAI Compatible" type 選項，並在 form view 中加入 `api_base_url` 欄位。改動量極小，因為大部分基礎設施已經就位：

- `ai_config.py` 已有 `api_base_url` 欄位（只缺 type selection）
- `ai_client.py` 的 `from_assistant()` 已讀取 `config.api_base_url`
- LangChain `ChatOpenAI` 已支援 `base_url` 參數

## Architecture Decisions

1. **在 woow_paas_platform 中 `_inherit ai.config`** — 不建新 addon，不改第三方程式碼
2. **沿用 OpenAI SDK + LangChain** — `ChatOpenAI(base_url=...)` 天然支援所有 OpenAI 相容 API
3. **保留 ChatGPT type** — `openai_compatible` 與現有 `chatgpt` 並存，不互相影響
4. **繼承 form view** — 用 `xpath` 在 `ai_base_gt` 的 form view 中插入 `api_base_url` 欄位

## Technical Approach

### Backend (Python)

**修改 `src/models/ai_config.py`：**
- 加入 `selection_add=[('openai_compatible', 'OpenAI Compatible')]`
- 加入 `ondelete={'openai_compatible': 'cascade'}`
- `api_base_url` 欄位已存在，無需新增

**不需修改：**
- `ai_client.py` — `from_assistant()` 已讀取 `config.api_base_url`，fallback 到 `https://api.openai.com/v1`
- `res_config_settings.py` — 不需改，Settings 選 ai.config 時已可選所有 type
- `discuss_channel.py` — 不需改，AI 回覆流程不受影響

### Frontend (XML Views)

**新增 `src/views/ai_config_views.xml`：**
- 繼承 `ai_base_gt.view_ai_config_form`
- 在 `api_key` 欄位後加入 `api_base_url` 欄位
- `invisible` 條件：`type not in ('openai_compatible',)` 或任何非 odooai type 皆可見

**更新 `__manifest__.py`：**
- 在 `data` 列表中加入 `views/ai_config_views.xml`

### Infrastructure

無基礎設施變更，純 Odoo addon 程式碼。

## Implementation Strategy

**單一 task**，因為改動很小：
1. 修改 `ai_config.py`（加 selection_add）
2. 新增 `ai_config_views.xml`（繼承 form view）
3. 更新 `__manifest__.py`
4. 部署測試

## Task Breakdown Preview

- [ ] Task 1: Add OpenAI Compatible type + form view（模型擴充 + 視圖繼承 + 部署驗證）

## Dependencies

- `ai_base_gt` 模組已安裝（提供 `ai.config` 基礎模型和 form view）
- `langchain-openai` Python 套件已安裝
- Docker 開發環境可用

## Success Criteria (Technical)

- AI Configurations 列表中可選 "OpenAI Compatible" type
- 選擇後可填入 API Base URL 和 API Key
- `AIClient.from_assistant()` 正確使用自訂 URL
- Settings 中 "Test Connection" 按鈕可正常測試連線
- 現有 ChatGPT / OdooAI 設定不受影響

## Estimated Effort

- **Size**: XS（< 2 小時）
- **Tasks**: 1
- **Risk**: 低（純 Odoo model inherit + view inherit）

## Tasks Created

- [ ] #80 - Add OpenAI Compatible type and form view (parallel: true)

Total tasks: 1
Parallel tasks: 1
Sequential tasks: 0
Estimated total effort: 1 hour
