# AI Chat End-to-End UI Testing Guide

## 概述

本指南提供 AI Chat 功能的完整 end-to-end UI 測試流程。

## 前置條件

✅ **已完成**：
- Docker 環境已啟動（Port: 8262）
- 資料庫已建立：`woow_alpha_ai_assistant`
- `woow_paas_platform` 模組已安裝
- LangChain 依賴已安裝（`langchain-openai`, `langchain-core`）

## 測試環境資訊

```
URL: http://localhost:8262
Database: woow_alpha_ai_assistant
Username: admin
Password: admin
Branch: alpha/ai-assistant
```

## AI Chat 架構概述

### 後端 API 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/api/ai/agents` | POST (JSON) | 獲取 AI agents 列表 |
| `/api/ai/providers` | POST (JSON) | 獲取 AI providers 列表 |
| `/api/ai/chat/history` | POST (JSON) | 獲取聊天歷史記錄 |
| `/api/ai/chat/post` | POST (JSON) | 發送訊息到 channel |
| `/api/ai/stream/<channel_id>` | GET (SSE) | SSE 串流 AI 回應 |
| `/api/ai/connection-status` | POST (JSON) | 檢查 AI 連線狀態 |

### 前端服務

- **Location**: `src/static/src/paas/services/ai_service.js`
- **功能**: 封裝所有 AI 相關的 API 呼叫
- **Key Methods**:
  - `fetchAgents()` - 獲取可用的 AI agents
  - `fetchChatHistory(channelId, limit, beforeId)` - 載入聊天歷史
  - `postMessage(channelId, body, agentId)` - 發送訊息
  - `getStreamUrl(channelId)` - 取得 SSE 串流 URL
  - `fetchConnectionStatus()` - 檢查連線狀態

### 資料模型

**Project Task** 擴展：
- `chat_enabled` (Boolean) - 是否啟用聊天
- `ai_auto_reply` (Boolean) - 是否自動回覆
- `channel_id` (Many2one: `discuss.channel`) - 關聯的聊天頻道

## 測試步驟

### Phase 1: 環境驗證 ✅

#### 1.1 啟動並訪問 Odoo

```bash
# 服務應已運行
docker compose ps

# 訪問 Odoo
open http://localhost:8262
```

**預期結果**：
- 看到 Odoo 登入頁面
- 資料庫自動選擇為 `woow_alpha_ai_assistant`

#### 1.2 登入系統

```
Username: admin
Password: admin
```

**預期結果**：
- 成功登入到 Odoo 後台
- 看到 "Woow PaaS" 主選單

### Phase 2: AI Provider 配置

#### 2.1 配置 AI Provider

1. 導航至：**Settings → General Settings → Woow PaaS**
2. 找到 **AI Provider Configuration** 區域
3. 點擊 **Create a new provider**（或編輯現有的）

**配置項目**：
```
Name: OpenAI GPT-4
Model Name: gpt-4
API Base URL: https://api.openai.com/v1
API Key: sk-xxxxxxxxxxxx (你的 OpenAI API Key)
Max Tokens: 4096
Temperature: 0.7
Is Active: ✓ (勾選)
```

4. 點擊 **Save**

**預期結果**：
- Provider 成功儲存
- 在 AI Provider 列表中看到新建立的 provider

#### 2.2 驗證 AI Agent

1. 導航至：**Woow PaaS → AI Agents**
2. 檢查是否存在預設的 AI Agent

**預期資訊**：
```
Name: ai_assistant
Display Name: AI 助理
Provider: (剛剛建立的 OpenAI GPT-4)
Is Default: ✓
```

如果不存在，需要手動建立一個 Agent。

### Phase 3: Workspace & Project 設定

#### 3.1 建立 Workspace

1. 導航至：**Woow PaaS → Workspaces**
2. 點擊 **Create**

**填寫資訊**：
```
Name: AI Chat Testing Workspace
Description: Workspace for testing AI chat functionality
```

3. 點擊 **Save**

**預期結果**：
- Workspace 成功建立
- 可以在列表中看到新 workspace

#### 3.2 建立 Project

1. 在 Workspace 詳細頁面，點擊 **Projects** tab
2. 點擊 **Create**

**填寫資訊**：
```
Name: AI Chat Test Project
Workspace: AI Chat Testing Workspace
```

3. 點擊 **Save**

### Phase 4: Task 與 AI Chat 設定

#### 4.1 建立 Task

1. 導航至：**Projects** → **AI Chat Test Project**
2. 點擊 **Create** 建立新任務

**填寫資訊**：
```
Name: Test AI Chat Functionality
Description: Testing the AI chat feature end-to-end
Project: AI Chat Test Project
```

3. 點擊 **Save**

#### 4.2 啟用 AI Chat

1. 在 Task 表單視圖，切換到 **AI Chat** tab
2. 勾選 **Chat Enabled**
3. 勾選 **AI Auto Reply**（自動回覆）
4. 點擊 **Save**

**預期結果**：
- `chat_enabled` 欄位已勾選
- `ai_auto_reply` 欄位已勾選
- `channel_id` 欄位自動建立並顯示 channel ID

### Phase 5: 前端 Chat UI 測試

#### 5.1 訪問 PaaS 應用

1. 導航至：`http://localhost:8262/woow`
2. 應該會看到獨立的 OWL 應用

**預期結果**：
- 載入 PaaS 應用首頁
- 看到 Sidebar 和 Dashboard

#### 5.2 導航到 Task 詳細頁面

1. 點擊 **Workspaces** (在 Sidebar)
2. 選擇 **AI Chat Testing Workspace**
3. 找到 **Test AI Chat Functionality** task
4. 點擊進入 Task 詳細頁面

**預期結果**：
- 看到 Task 詳細資訊
- 看到 **Chat** 或 **AI Chat** 區域

#### 5.3 測試聊天功能

**測試案例 1: 載入聊天歷史**

```javascript
// 在瀏覽器 Console 執行
const aiService = require('woow_paas_platform/static/src/paas/services/ai_service').aiService;
const channelId = 1; // 替換為實際的 channel_id

// 測試載入歷史
const history = await aiService.fetchChatHistory(channelId, 50);
console.log('Chat history:', history);
```

**預期結果**：
- 成功返回聊天歷史（可能為空陣列）
- `success: true`

**測試案例 2: 發送訊息**

```javascript
// 發送測試訊息
const result = await aiService.postMessage(channelId, 'Hello, AI assistant!');
console.log('Post message result:', result);
```

**預期結果**：
- 訊息成功發送
- 返回 `success: true` 和訊息資料

**測試案例 3: SSE 串流測試**

```javascript
// 取得串流 URL
const streamUrl = aiService.getStreamUrl(channelId);
console.log('Stream URL:', streamUrl);

// 建立 EventSource
const eventSource = new EventSource(streamUrl);

eventSource.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Received chunk:', data);

  if (data.done) {
    console.log('AI response complete:', data.full_response);
    eventSource.close();
  }
};

eventSource.onerror = (error) => {
  console.error('SSE error:', error);
  eventSource.close();
};
```

**預期結果**：
- 成功建立 SSE 連線
- 收到 AI 回應的 chunks
- 最後收到 `done: true` 訊號

**測試案例 4: 檢查連線狀態**

```javascript
// 檢查 AI 連線狀態
const status = await aiService.fetchConnectionStatus();
console.log('Connection status:', status);
```

**預期結果**：
```json
{
  "success": true,
  "data": {
    "connected": true,
    "provider_name": "OpenAI GPT-4",
    "model_name": "gpt-4"
  }
}
```

### Phase 6: 手動 UI 互動測試

#### 6.1 在 UI 中發送訊息

1. 在 Task 詳細頁面的 Chat 區域
2. 輸入訊息：`你好，請介紹一下這個專案。`
3. 點擊發送按鈕

**預期結果**：
- 訊息立即顯示在聊天視窗
- 看到「AI 正在輸入...」的指示器
- AI 回應逐字串流顯示（Markdown 格式）
- 回應完成後，訊息完整顯示

#### 6.2 測試多輪對話

1. 繼續發送訊息：`可以幫我總結一下功能嗎？`
2. 等待 AI 回應
3. 再次發送：`有什麼需要改進的地方？`

**預期結果**：
- 每次回應都能正確顯示
- AI 能夠理解上下文（使用歷史訊息）
- 聊天歷史正確累積

#### 6.3 測試檔案上傳（如果實作）

1. 點擊附件按鈕
2. 選擇一個檔案上傳
3. 檢查檔案是否成功上傳並顯示

**預期結果**：
- 檔案上傳成功
- 在聊天中看到檔案附件
- 可以下載附件

### Phase 7: 錯誤處理測試

#### 7.1 測試無效的 API Key

1. 修改 AI Provider 的 API Key 為無效值
2. 嘗試發送訊息

**預期結果**：
- 顯示錯誤訊息：「AI provider not configured」或類似
- 不會導致前端崩潰

#### 7.2 測試網路中斷

1. 在 DevTools 中模擬 Offline 模式
2. 嘗試發送訊息

**預期結果**：
- 顯示網路錯誤訊息
- 訊息不會遺失（如果有重試機制）

## 測試檢查清單

### 後端 API 測試 ✅

- [ ] `/api/ai/agents` 返回正確的 agents 列表
- [ ] `/api/ai/providers` 返回正確的 providers 列表
- [ ] `/api/ai/chat/history` 正確載入歷史訊息
- [ ] `/api/ai/chat/post` 成功發送訊息
- [ ] `/api/ai/stream/<channel_id>` SSE 串流正常工作
- [ ] `/api/ai/connection-status` 返回正確的連線狀態

### 前端服務測試 ✅

- [ ] `aiService.fetchAgents()` 正確載入 agents
- [ ] `aiService.fetchChatHistory()` 正確載入歷史
- [ ] `aiService.postMessage()` 成功發送訊息
- [ ] `aiService.getStreamUrl()` 返回正確的 URL
- [ ] `aiService.fetchConnectionStatus()` 返回正確狀態

### UI 互動測試 ✅

- [ ] 聊天介面正確顯示
- [ ] 發送訊息功能正常
- [ ] AI 回應串流顯示正常
- [ ] Markdown 渲染正確
- [ ] 歷史訊息載入正常
- [ ] 多輪對話上下文正確
- [ ] 錯誤訊息顯示正確

### 整合測試 ✅

- [ ] Task 與 Channel 正確關聯
- [ ] AI Agent 與 Provider 正確配置
- [ ] 自動回覆機制正常工作
- [ ] 權限檢查正確（channel access）

## 常見問題排解

### 問題 1: AI 不回應

**可能原因**：
- AI Provider 未配置或 API Key 無效
- `ai_auto_reply` 未啟用
- AI Agent 未設定為 default

**解決方案**：
1. 檢查 Settings → AI Provider 配置
2. 確認 Task 的 `ai_auto_reply` 已勾選
3. 檢查 AI Agent 的 `is_default` 欄位

### 問題 2: SSE 連線失敗

**可能原因**：
- CSRF token 無效
- Channel access 權限問題
- 網路問題

**解決方案**：
1. 檢查 `odoo.csrf_token` 是否存在
2. 確認使用者有 channel 存取權限
3. 查看瀏覽器 Console 和 Odoo 日誌

### 問題 3: 訊息不顯示

**可能原因**：
- 前端沒有正確監聽 bus 事件
- Channel ID 不正確
- 權限問題

**解決方案**：
1. 檢查 `task.channel_id` 是否正確
2. 確認使用者是 channel member
3. 查看 Network tab 確認 API 回應

## 自動化測試腳本（未來）

建議使用 Playwright 或 Selenium 進行自動化測試：

```javascript
// tests/e2e/ai-chat.spec.js
describe('AI Chat E2E Tests', () => {
  test('should send message and receive AI response', async () => {
    // 1. 登入
    // 2. 導航到 Task 詳細頁面
    // 3. 發送訊息
    // 4. 等待 AI 回應
    // 5. 驗證回應內容
  });
});
```

## 結論

完成以上所有測試步驟後，AI Chat 功能應該已經完整驗證。如果遇到任何問題，請參考常見問題排解章節，或查看 Odoo 日誌：

```bash
docker compose logs -f web | grep -i "ai\|chat"
```

## 相關檔案

- **Controller**: `src/controllers/ai_assistant.py`
- **Models**:
  - `src/models/ai_agent.py`
  - `src/models/ai_provider.py`
  - `src/models/ai_client.py`
- **Frontend Service**: `src/static/src/paas/services/ai_service.js`
- **Views**: `src/views/project_task_views.xml`
- **Data**: `src/data/ai_agents.xml`
