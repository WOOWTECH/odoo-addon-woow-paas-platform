---
name: ai-assistant-refactor
status: backlog
created: 2026-02-21T16:07:11Z
progress: 0%
prd: .claude/prds/ai-assistant-refactor.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/71
---

# Epic: ai-assistant-refactor

## Overview

混合重構 `woow_paas_platform` 的 AI 助手系統：用 `ai_base_gt` 的 Odoo 整合層（`ai.config`、`ai.assistant`）取代自建的 `ai_provider` 和 `ai_agent` 模型，同時保留 LangChain 做 AI 呼叫（streaming、多 provider）。

核心策略：**替換配置/助手管理層，保留 AI 呼叫層**。

## Architecture Decisions

1. **混合架構**：Odoo 整合用 `ai_base_gt`（配置、助手、partner 身份），AI 呼叫用 LangChain（streaming、多 provider 切換）
2. **繼承擴展 `ai.config`**：透過 `_inherit` 新增 `api_base_url` 欄位，不修改第三方模組原始碼
3. **工廠方法模式**：`AIClient.from_assistant(assistant)` 從 `ai.assistant` → `ai.config` 讀取配置建立 LangChain client
4. **Discuss channel 維持不變**：Phase 1 繼續用 Discuss channel 管理對話，僅替換 AI 回覆的 author 身份（`ai.assistant.partner_id` 取代 `base.partner_root`）
5. **前端最小改動**：API response 格式盡量保持一致，`ai_service.js` 只做必要欄位名稱調整

## Technical Approach

### Backend Changes

**新增檔案：**
- `src/models/ai_config.py` — 繼承 `ai.config`，加入 `api_base_url` 欄位

**重構檔案：**
- `src/models/ai_client.py` — 新增 `AIClient.from_assistant()` 工廠方法，移除對 `ai_provider` 的直接依賴
- `src/models/discuss_channel.py` — `_detect_ai_agent()` 改搜尋 `ai.assistant`，`_post_ai_reply()` 改用 `ai.assistant.partner_id` 作為 author
- `src/models/res_config_settings.py` — `woow_ai_provider_id` → `woow_ai_config_id`（指向 `ai.config`），新增 `woow_ai_assistant_id`
- `src/controllers/ai_assistant.py` — 所有 endpoint 改用 `ai.assistant` + `AIClient.from_assistant()`

**移除檔案：**
- `src/models/ai_provider.py`
- `src/models/ai_agent.py`
- `src/views/ai_provider_views.xml`
- `src/views/ai_agent_views.xml`
- `src/data/ai_agents.xml`（改為建立 `ai.assistant` + `ai.config` + `ai.context` 的 data XML）

**更新檔案：**
- `src/models/__init__.py` — 移除 ai_provider/ai_agent import，新增 ai_config import
- `src/__manifest__.py` — depends 加入 `ai_base_gt`，更新 data 列表
- `src/security/ir.model.access.csv` — 移除 ai_provider/ai_agent 的 ACL

### Frontend Changes

- `src/static/src/paas/services/ai_service.js` — API response 欄位名稱適配（如 `agent_id` → `assistant_id`、`model_name` → `model` 等）

### Data Migration

- `pre_init_hook` 或 migration script：將既有 `woow_paas_platform.ai_provider` 資料映射到 `ai.config`，`woow_paas_platform.ai_agent` 映射到 `ai.assistant`

## Implementation Strategy

分 7 個 task 按順序執行。前 5 個為後端核心重構，第 6 個處理前端適配和清理，第 7 個為 E2E 驗證。

**風險緩解：**
- 每完成一個 task 部署測試，確保增量可用
- 先新增 `ai.config` 擴展和 `AIClient.from_assistant()`，確認配置讀取正常後再拆除舊模型
- 前端改動放在最後，確保後端 API 穩定後再調整

## Task Breakdown

- [ ] Task 1: 新增 `ai_base_gt` 依賴 + 擴展 `ai.config` + 建立初始 data — 更新 `__manifest__.py`，建立 `ai_config.py`（`_inherit = 'ai.config'` + `api_base_url`），建立 `data/ai_assistant_data.xml`（預設的 `ai.config`、`ai.context`、`ai.assistant` 記錄取代原本的 `ai_agents.xml`）
- [ ] Task 2: 重構 `ai_client.py` — 新增 `AIClient.from_assistant(assistant)` 工廠方法，從 `ai.config` 讀取 api_base_url/api_key/model/max_tokens/temperature，保留原有 `chat_completion()` 和 `chat_completion_stream()` 介面不變
- [ ] Task 3: 重構 `res_config_settings.py` — 將 `woow_ai_provider_id` 改為 `woow_ai_config_id`（M2O → `ai.config`），新增 `woow_ai_assistant_id`（M2O → `ai.assistant`），更新 `action_test_ai_connection` 使用 `AIClient.from_assistant()`，更新 `res_config_settings_views.xml`
- [ ] Task 4: 重構 `controllers/ai_assistant.py` — 所有 endpoint 改用 `ai.assistant` 和 `AIClient.from_assistant()`。`/api/ai/agents` 改查 `ai.assistant`，`/api/ai/stream` 和 `/api/ai/chat/post` 改用 `AIClient.from_assistant()`，`/api/ai/connection-status` 改讀 `ai.config`
- [ ] Task 5: 重構 `discuss_channel.py` — `_detect_ai_agent()` 改搜尋 `ai.assistant`（比對 partner name），`_post_ai_reply()` 改用 `AIClient.from_assistant()` + `ai.assistant.partner_id` 作為 author，`_get_auto_reply_agent()` 改從 settings 取得預設 `ai.assistant`
- [ ] Task 6: 移除舊模型 + 前端適配 + 清理 — 刪除 `ai_provider.py`、`ai_agent.py`、相關 views/data/ACL，更新 `__init__.py` 和 `__manifest__.py`，`ai_service.js` 適配新 API response 格式
- [ ] Task 7: E2E 瀏覽器驗證測試 — 部署到測試環境，透過 Playwright 執行 5 個測試案例（Portal AI Chat 對話 + streaming、多輪對話、連線狀態、Task Chat @mention、Settings 配置）

## Dependencies

### External
- `ai_base_gt` 0.1.8（已在 `extra-addons` 中）
- LangChain（已透過 `pre_init_hook` 安裝）

### Internal
- Task 1 → Task 2（`ai_config.py` 存在後才能建立 `from_assistant()`）
- Task 2 → Task 3-5（`AIClient.from_assistant()` 就緒後才能重構 controller/settings/discuss）
- Task 3-5 可並行
- Task 6 依賴 Task 3-5 全部完成
- Task 7 依賴 Task 6 完成 + 部署

## Success Criteria (Technical)

| 項目 | 驗收條件 |
|------|---------|
| `ai_provider` / `ai_agent` 模型 | 完全移除，無殘留引用 |
| `ai.config` 擴展 | `api_base_url` 欄位可用 |
| `AIClient.from_assistant()` | 正確從 `ai.config` 讀取配置建立 LangChain client |
| SSE streaming | `/api/ai/stream` 正常回應，逐字輸出 |
| AI 回覆 author | 所有 AI 回覆使用 `ai.assistant.partner_id`，非 `base.partner_root` |
| Settings 頁面 | 可選擇 `ai.config` 和 `ai.assistant`，連線測試正常 |
| E2E 測試 | 5 個測試案例全部通過 |

## Estimated Effort

- **Task 1**: S（新增擴展 + data XML）
- **Task 2**: S（工廠方法，邏輯簡單）
- **Task 3**: M（settings + views 更新）
- **Task 4**: L（controller 998 行，多 endpoint 重構）
- **Task 5**: M（discuss channel 重構）
- **Task 6**: M（刪除 + 清理 + 前端適配）
- **Task 7**: M（E2E 瀏覽器測試）

**總計**: 7 個 tasks

## Tasks Created

- [ ] #73 - Add ai_base_gt dependency + extend ai.config + create initial data (parallel: false)
- [ ] #77 - Refactor ai_client.py with from_assistant() factory method (parallel: false)
- [ ] #78 - Refactor res_config_settings to use ai.config + ai.assistant (parallel: true)
- [ ] #72 - Refactor controller API endpoints to use ai.assistant (parallel: true)
- [ ] #75 - Refactor discuss_channel.py AI reply to use ai.assistant (parallel: true)
- [ ] #74 - Remove old models + frontend adaptation + cleanup (parallel: false)
- [ ] #76 - E2E browser validation tests (parallel: false)

Total tasks: 7
Parallel tasks: 3 (#78, #72, #75 can run in parallel)
Sequential tasks: 4 (#73, #77, #74, #76)
