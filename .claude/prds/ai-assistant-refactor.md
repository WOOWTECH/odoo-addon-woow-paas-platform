---
name: ai-assistant-refactor
description: Hybrid refactor - use ai_base_gt for Odoo integration layer, keep LangChain for AI client capabilities
status: backlog
created: 2026-02-20T15:27:59Z
updated: 2026-02-20T15:51:46Z
---

# PRD: AI Assistant Refactor - Hybrid Migration to ai_base_gt

## Executive Summary

採用**混合方案**重構 `woow_paas_platform` 的 AI 助手系統：

- **Odoo 整合層**使用 `ai_base_gt`：`ai.config`（配置管理）、`ai.assistant`（助手 + partner 身份）、`ai.data.source`（資料來源權限）
- **AI 呼叫層**保留 LangChain：streaming、多 provider 切換、未來 Agent framework 擴展

這個方案同時獲得兩邊的優勢：
- `ai_base_gt` 的 **Odoo 原生整合**（partner 身份、Discuss、權限控制、資料源管理）
- LangChain 的 **AI 呼叫彈性**（streaming、多 provider、豐富生態系）

具體來說：
- 移除 `ai_provider` → 改用 `ai.config`（從此讀取 API 配置）
- 移除 `ai_agent` → 改用 `ai.assistant`（含 partner 身份、context）
- **保留並重構** `ai_client` → 從 `ai.config` 讀取配置，用 LangChain 做實際 AI 呼叫

## Problem Statement

### 現狀問題

1. **Odoo 整合層重複造輪子**：`woow_paas_platform` 自建了 `ai_provider`（API 配置）和 `ai_agent`（助手管理）兩個模型，而 `ai_base_gt` 已提供了更完整的 `ai.config` 和 `ai.assistant`（含 partner 身份、權限控制、資料來源管理）。

2. **Discuss 整合不完整**：目前使用 `base.partner_root` 作為 AI 回覆的 author，缺乏獨立的 AI partner 身份。`ai_base_gt` 的 `ai.assistant` 繼承 `res.partner`，可自然作為 Discuss channel 成員參與對話。

3. **缺乏資料源管理**：`ai_base_gt` 提供了 RAG 語義搜尋、資料源索引、向量生成、欄位級權限控制等能力，自建系統完全缺少。

4. **維護成本**：兩套配置 / 助手模型並存增加了複雜度。

### ai_base_gt 的 AI Client 不足之處

`ai_base_gt` 的 ChatGPT connector 使用原生 OpenAI SDK 的同步 Responses API，**不支援 streaming**，且擴展彈性有限。而目前自建的 `ai_client`（LangChain）已具備：
- SSE streaming 支援
- LangChain 生態系的多 provider 切換能力（`ChatOpenAI` → `ChatAnthropic` 等）
- 未來可輕鬆整合 LangChain Agents / Tools / Retrievers

因此採用混合方案：**Odoo 整合用 ai_base_gt，AI 呼叫用 LangChain**。

### 為什麼現在做

- `ai_base_gt` 及其 connector 模組已部署在 `extra-addons` 中，可直接使用
- Portal AI Chat 功能尚在 alpha 階段（`alpha/ai-assistant` branch），是重構的最佳時機
- 清除自建模型可簡化模組結構，降低技術債

## User Stories

### US-1: 管理員配置 AI Provider

**身為** 系統管理員
**我希望** 在 Odoo 後台設定 AI 配置（API Key、模型名稱、溫度等）
**以便** AI 助手能正常運作

**Acceptance Criteria:**
- [ ] 可以透過 `ai.config` 建立 AI 配置（取代自建 `ai_provider`）
- [ ] 設定頁面 (`res.config.settings`) 改為關聯 `ai.config` 而非 `woow_paas_platform.ai_provider`
- [ ] 支援透過 connector 模組切換不同 AI provider（ChatGPT、Gemini 等）

### US-2: 管理員管理 AI 助手

**身為** 系統管理員
**我希望** 建立和管理 AI 助手，設定其系統指令和可存取的資料來源
**以便** 不同場景使用不同的 AI 助手

**Acceptance Criteria:**
- [ ] 使用 `ai.assistant` 模型管理助手（取代自建 `ai_agent`）
- [ ] 每個助手自動擁有 `res.partner` 身份，可直接在 Discuss 中使用
- [ ] 支援設定 `ai.context` 作為系統指令（取代自建 `system_prompt` 欄位）
- [ ] 支援資料來源權限配置

### US-3: Portal 使用者透過 AI Chat 對話

**身為** Portal 使用者
**我希望** 在 `/woow` 介面中與 AI 助手對話
**以便** 獲得即時的 AI 回覆協助

**Acceptance Criteria:**
- [ ] Portal AI Chat 前端 UI 保持不變（`ai_service.js`、chat 頁面元件等）
- [ ] 後端 API endpoints 改為從 `ai.assistant` / `ai.config` 取得配置
- [ ] SSE streaming 保持可用（使用重構後的 `ai_client` + LangChain streaming）
- [ ] 對話記錄持久化方式維持（Discuss channel messages），Phase 2 可考慮遷移至 `ai.thread` / `ai.message`

### US-4: Task Chat 中的 AI 自動回覆

**身為** 專案成員
**我希望** 在 Task Chat（Discuss channel）中 @mention AI 助手獲得回覆
**以便** 在協作對話中直接使用 AI 能力

**Acceptance Criteria:**
- [ ] `discuss.channel` 的 AI 回覆改用 `ai.assistant` 的 partner 身份（不再用 `base.partner_root`）
- [ ] AI 回覆邏輯改為使用重構後的 `ai_client`（從 `ai.assistant.config_id` 讀取配置，LangChain 呼叫）
- [ ] `@mention` 偵測改為比對 `ai.assistant` 的 partner name

### US-5: 支援 Function Calling（可選，Phase 2）

**身為** Portal 使用者
**我希望** AI 助手能查詢 Odoo 資料並執行操作
**以便** 完成更複雜的任務

**Acceptance Criteria:**
- [ ] 透過 `ai.data.source` 設定可存取的模型和資料
- [ ] AI 助手可使用 `_semantic_search`、`_model_search` 等內建工具
- [ ] 遵循 `ai_base_gt` 的權限控制機制

## Requirements

### Functional Requirements

#### FR-1: 混合架構 — Odoo 整合層用 ai_base_gt，AI 呼叫層用 LangChain

| 項目 | 動作 | 說明 |
|------|------|------|
| `woow_paas_platform.ai_provider` | **移除** → 用 `ai.config` | API 配置（key、model、temperature 等） |
| `woow_paas_platform.ai_agent` | **移除** → 用 `ai.assistant` | 助手管理（含 partner 身份、context、資料來源） |
| `src/models/ai_client.py` | **保留並重構** | 從 `ai.config` 讀取配置，LangChain 做實際 AI 呼叫（含 streaming） |

重構後的 `ai_client.py` 架構：

```python
# 概念示意
class AIClient:
    @classmethod
    def from_assistant(cls, assistant):
        """從 ai.assistant 建立 LangChain client"""
        config = assistant.config_id  # ai.config
        return cls(
            api_base_url=config.api_base_url,  # 需擴展 ai.config
            api_key=config.api_key,
            model_name=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )

    def chat_completion(self, messages):
        """同步呼叫（LangChain ChatOpenAI）"""
        ...

    def chat_completion_stream(self, messages):
        """SSE streaming（LangChain ChatOpenAI streaming=True）"""
        ...
```

#### FR-2: 擴展 ai.config 欄位

`ai_base_gt` 的 `ai.config` 缺少 `api_base_url` 欄位（自建 `ai_provider` 有此欄位用於 proxy / 自建 endpoint）。需透過繼承擴展：

```python
class AIConfig(models.Model):
    _inherit = 'ai.config'

    api_base_url = fields.Char(
        string='API Base URL',
        help='Custom API base URL for proxy or self-hosted endpoints',
    )
```

#### FR-3: 更新設定頁面

- `res.config.settings` 中的 `woow_ai_provider_id` → 改為 `woow_ai_config_id` 指向 `ai.config`
- 新增 `woow_ai_assistant_id` 欄位指向 `ai.assistant` 作為預設助手
- `action_test_ai_connection` 改為使用重構後的 `AIClient.from_assistant()` 測試

#### FR-4: 重構 Controller API Endpoints

| Endpoint | 變更 |
|---------|------|
| `/api/ai/providers` | 改為查詢 `ai.config`，或移除（前端不需要直接選擇 provider） |
| `/api/ai/agents` | 改為查詢 `ai.assistant` |
| `/api/ai/chat/history` | 保留 Discuss channel messages 方式 |
| `/api/ai/chat/post` | 保留 Discuss channel 方式，AI 呼叫改用 `AIClient.from_assistant()` |
| `/api/ai/stream/<channel_id>` | 保留 SSE streaming，改用 `AIClient.from_assistant()` + LangChain streaming |
| `/api/ai/connection-status` | 改為檢測 `ai.config` + `AIClient.from_assistant()` 連線狀態 |

#### FR-5: 重構 Discuss Channel AI 回覆

- `discuss_channel.py` 的 `_detect_ai_agent()` 改為搜尋 `ai.assistant`（比對 partner name）
- `_post_ai_reply()` 改用 `AIClient.from_assistant(assistant)` 呼叫 LangChain
- AI 回覆的 `author_id` 改為 `ai.assistant.partner_id`（不再用 `base.partner_root`）
- system prompt 從 `ai.assistant.context_id` 取得（取代自建 `ai_agent.system_prompt`）

#### FR-6: SSE Streaming 保持可用

由於保留了 LangChain，streaming 自然支援，無需額外方案選擇：

```python
# 重構後的 streaming 流程
assistant = ...  # ai.assistant record
client = AIClient.from_assistant(assistant)
for chunk in client.chat_completion_stream(messages):
    yield f"data: {json.dumps({'content': chunk})}\n\n"
```

### Non-Functional Requirements

#### NFR-1: 向後相容
- 前端 OWL 元件（`ai_service.js`、chat 頁面）的 API 介面盡量保持不變
- 如 API response 格式需變更，前端需同步調整

#### NFR-2: 模組相依性
- `__manifest__.py` 新增 `depends`: `ai_base_gt`
- 可選新增 `odoo_ai_assistant_chatgpt_connector`（如需 ChatGPT 支援）
- 可選新增 `odoo_ai_assistant_base`（如需 Discuss 深度整合）

#### NFR-3: 資料遷移
- 既有的 `woow_paas_platform.ai_provider` 資料需遷移至 `ai.config`
- 既有的 `woow_paas_platform.ai_agent` 資料需遷移至 `ai.assistant`
- Discuss channel 中的歷史訊息保留不動

#### NFR-4: 效能
- SSE streaming 回應延遲不應明顯增加
- Discuss channel AI 回覆延遲維持目前水準

## Success Criteria

| 指標 | 目標 |
|------|------|
| 移除的自建 model 數量 | 2 個（ai_provider, ai_agent） |
| 保留並重構的模組 | 1 個（ai_client — 改為從 ai.config 讀取配置） |
| Portal AI Chat 功能正常 | 100% 功能保留（含 SSE streaming） |
| Task Chat AI 回覆正常 | 100% 功能保留 |
| AI 回覆使用獨立 partner | AI 回覆 author 為 ai.assistant 的 partner（非 base.partner_root） |
| LangChain streaming 可用 | 保持原有 streaming 能力 |
| 新增 AI provider 支援 | LangChain 層可切換（ChatOpenAI → ChatAnthropic 等） |
| E2E 瀏覽器驗證 | 5 個測試案例全部通過（Portal Chat、多輪對話、連線狀態、Task Chat @mention、Settings 配置） |

## Constraints & Assumptions

### Constraints

1. **`ai_base_gt` 不可修改**：此為第三方模組（GT Apps），只能透過繼承擴展，不能直接改動原始碼
2. **`ai.config` 缺少 `api_base_url`**：需透過 `_inherit` 擴展新增此欄位
3. **前端改動最小化**：Portal UI 已完成，盡量只改後端 API，前端做最小必要調整
4. **Odoo 18 相容性**：所有變更需相容 Odoo 18.0
5. **LangChain 版本相容**：需確認 LangChain 與 Odoo 18 Python 環境的相容性

### Assumptions

1. `ai_base_gt` 和相關 connector 模組已安裝並可用
2. 目前只需支援 ChatGPT（OpenAI API），其他 provider 為未來擴展
3. `extra-addons` 中的模組版本穩定，API 不會大幅變動
4. Portal 使用者不需要存取 `ai.data.source` 的 RAG 功能（Phase 1）

## Out of Scope

1. **不使用 ai_base_gt 的 AI client 呼叫邏輯** — 只用其 Odoo 整合層，AI 呼叫保留 LangChain
2. **不使用 ai_base_gt 的 `ai.thread` / `ai.message` 對話管理** — Phase 1 繼續用 Discuss channel
3. **不遷移歷史對話記錄** — Discuss channel 訊息保留原樣
4. **不改動 Portal 前端 UI 設計** — 只調整 API 呼叫層
5. **RAG / Function Calling 整合** — 列為 Phase 2 未來工作
6. **多助手切換 UI** — Phase 2 考慮
7. **`project.task` 和 `project.project` 模型重構** — 不在此 PRD 範圍
8. **不實作新的 AI connector for ai_base_gt** — LangChain 已提供多 provider 支援

## Dependencies

### External Dependencies

| 模組 | 版本 | 用途 |
|------|------|------|
| `ai_base_gt` | 0.1.8 | 核心 AI 框架 |
| `odoo_ai_assistant_chatgpt_connector` | - | ChatGPT provider 實作 |
| `odoo_ai_assistant_base` | - | Discuss + AI 整合（可選） |

### Internal Dependencies

| 檔案 | 影響 |
|------|------|
| `src/models/ai_provider.py` | **移除** |
| `src/models/ai_agent.py` | **移除** |
| `src/models/ai_client.py` | **保留並重構**（改為從 `ai.config` 讀取配置） |
| `src/models/ai_config.py` | **新增**（繼承 `ai.config` 擴展 `api_base_url` 欄位） |
| `src/models/discuss_channel.py` | 重構 AI 回覆邏輯（改用 `ai.assistant`） |
| `src/models/res_config_settings.py` | 更新配置欄位（指向 `ai.config` + `ai.assistant`） |
| `src/controllers/ai_assistant.py` | 重構 API endpoints（改用 `ai.assistant` + 重構後的 `ai_client`） |
| `src/static/src/paas/services/ai_service.js` | 可能需微調 API 呼叫格式 |
| `src/__manifest__.py` | 新增 `ai_base_gt` depends、更新檔案列表 |
| `src/security/ir.model.access.csv` | 移除舊模型的 ACL |
| `src/views/` | 移除舊模型的 views |

## Architecture Overview

### 混合架構圖

```
┌─────────────────────────────────────────────────────────┐
│                   woow_paas_platform                     │
│                                                          │
│  ┌──────────────────┐    ┌────────────────────────────┐ │
│  │  Controller       │    │  ai_client.py (LangChain)  │ │
│  │  (API Endpoints)  │───▶│  ├─ chat_completion()      │ │
│  │                   │    │  ├─ chat_completion_stream()│ │
│  │  discuss_channel  │───▶│  └─ from_assistant()       │ │
│  └──────────────────┘    └───────────┬────────────────┘ │
│           │                          │                   │
│           ▼                          │ 讀取配置          │
│  ┌──────────────────┐               │                   │
│  │  ai_config.py     │◀──────────────┘                   │
│  │  (_inherit)       │                                   │
│  │  + api_base_url   │                                   │
│  └──────────────────┘                                    │
└──────────────────────────────────────────────────────────┘
           │ depends
           ▼
┌──────────────────────────────────────────────────────────┐
│                     ai_base_gt                            │
│                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │ ai.config   │  │ ai.assistant  │  │ ai.data.source  │ │
│  │ (API 配置)  │  │ (助手管理)   │  │ (資料來源)      │ │
│  │             │  │ ↳ res.partner │  │                 │ │
│  └────────────┘  └──────────────┘  └─────────────────┘ │
│                                                          │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │ ai.context  │  │ ai.thread    │  │ ai.message      │ │
│  │ (系統指令)  │  │ (對話管理)   │  │ (訊息紀錄)      │ │
│  └────────────┘  └──────────────┘  └─────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

### 資料流

```
使用者輸入 → Controller → 取得 ai.assistant → AIClient.from_assistant()
                                                    │
                                                    ▼
                                            LangChain ChatOpenAI
                                            (streaming / 同步)
                                                    │
                                                    ▼
                                            SSE / Discuss message_post
```

## Implementation Phases

### Phase 1: Core Hybrid Migration (本 PRD 範圍)

1. **新增模組依賴** — `__manifest__.py` 加入 `ai_base_gt` 依賴
2. **擴展 ai.config** — 新增 `ai_config.py` 繼承 `ai.config` 加入 `api_base_url`
3. **重構 ai_client** — 新增 `AIClient.from_assistant()` 工廠方法，從 `ai.config` 讀取配置
4. **更新 Settings** — `res.config.settings` 改用 `ai.config` 和 `ai.assistant`
5. **重構 Controller** — API endpoints 改用 `ai.assistant` + 重構後的 `ai_client`
6. **重構 Discuss AI Reply** — 改用 `ai.assistant` partner 身份 + `AIClient.from_assistant()`
7. **移除自建模型** — 刪除 `ai_provider.py`、`ai_agent.py`
8. **資料遷移腳本** — 提供 migration hook 將 `ai_provider` → `ai.config`、`ai_agent` → `ai.assistant`
9. **前端微調** — `ai_service.js` 適配新 API response 格式（如有變更）
10. **E2E 瀏覽器驗證測試** — 開啟瀏覽器實際操作，確認 AI 對話功能端到端正常

### E2E 瀏覽器驗證測試 (Phase 1 最終步驟)

重構完成後，必須透過瀏覽器實際操作驗證 AI 對話功能完整可用。

#### 測試前置條件
- Odoo 已安裝 `ai_base_gt` + `woow_paas_platform`（含重構後的程式碼）
- 已在後台建立 `ai.config`（配置 API Key、model）和 `ai.assistant`（配置 context）
- 已在 Settings 設定預設 AI assistant

#### 測試案例

**TC-1: Portal AI Chat — 發送訊息並收到 AI 回覆**
1. 登入 Odoo，進入 `/woow` Portal
2. 導航至 AI Chat 頁面
3. 在輸入框輸入「你好，請自我介紹」
4. 送出訊息
5. **驗證**：AI 回覆正常顯示（SSE streaming 逐字出現）
6. **驗證**：回覆的 author 為 `ai.assistant` 的 partner name（非 OdooBot）

**TC-2: Portal AI Chat — 多輪對話**
1. 延續 TC-1 的對話
2. 再送出「你記得我剛才問了什麼嗎？」
3. **驗證**：AI 回覆包含上一輪對話的上下文（歷史記錄正常傳遞）

**TC-3: Portal AI Chat — 連線狀態檢測**
1. 進入 AI Chat 頁面
2. **驗證**：連線狀態指示器顯示已連線（`/api/ai/connection-status` 正常回應）

**TC-4: Task Chat — @mention AI 助手回覆**
1. 進入一個已啟用 chat 的 Task（或建立新的）
2. 在 Task Chat 中輸入 `@{assistant_name} 你好`
3. **驗證**：AI 助手在 channel 中回覆
4. **驗證**：回覆的 author 為 `ai.assistant` 的 partner（非 OdooBot / partner_root）

**TC-5: 後台 Settings — AI 配置正常**
1. 進入 Settings → Woow PaaS
2. **驗證**：可以選擇 `ai.config` 作為預設配置
3. **驗證**：可以選擇 `ai.assistant` 作為預設助手
4. 點擊「Test AI Connection」
5. **驗證**：連線測試成功通知

### Phase 2: Advanced Features (未來)

- RAG 語義搜尋整合（透過 `ai.data.source` + `ai_base_gt` 的 `_semantic_search`）
- Function Calling 整合（Odoo CRUD，透過 `ai_base_gt` 的 `@ai_tool` 或 LangChain Tools）
- 多助手切換 UI
- `ai_chat` channel type 支援（`odoo_ai_assistant_discuss`）
- 對話記錄遷移至 `ai.thread` / `ai.message`
