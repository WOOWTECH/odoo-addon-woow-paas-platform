---
name: ai-chat-mermaid-rendering
description: Enable Mermaid diagram rendering in parseMarkdown with lazy loading and interactive SVG
status: backlog
created: 2026-02-26T06:58:17Z
---

# PRD: AI Chat Mermaid Rendering

## Executive Summary

在 `parseMarkdown()` 服務中加入 Mermaid 圖表渲染能力，讓所有使用 Markdown 渲染的地方（包含 AI Chat 訊息、任務描述等）都能將 mermaid code block 自動轉換為互動式 SVG 圖表。採用 Lazy Loading 策略，僅在偵測到 mermaid 內容時才載入 mermaid.js 庫，避免影響整體效能。

## Problem Statement

### 問題描述

目前 AI Chat 回覆中包含的 mermaid code block（如 flowchart、sequence diagram 等）只會以純文字 `<pre><code>` 方式顯示，使用者無法直觀理解圖表內容。這降低了 AI 助理在技術溝通場景中的實用性。

### 為什麼現在要做

- AI 助理經常產生 mermaid 圖表來解釋架構、流程、關聯
- 純文字 mermaid 語法對非技術使用者不友善
- 市面上主流工具（GitHub、Notion、Obsidian）皆已原生支援 mermaid 渲染
- 此功能能顯著提升 AI Chat 的可讀性和專業度

## User Stories

### US-1：查看 AI 回覆中的 Mermaid 圖表

**作為**一位使用 Support Task AI Chat 的使用者
**我想要**看到 AI 回覆中的 mermaid 語法被渲染成圖表
**以便於**我能直觀理解 AI 解釋的流程、架構等資訊

**Acceptance Criteria:**
- mermaid code block 自動渲染為 SVG 圖表
- 支援所有 mermaid 圖表類型（flowchart、sequence、class、state、gantt、pie、er、mindmap 等）
- 非 mermaid 的 code block 不受影響，維持原有的 `<pre><code>` 樣式

### US-2：與圖表互動

**作為**使用者
**我想要**能夠對 mermaid 圖表進行基本互動（縮放、點擊）
**以便於**我能查看大型圖表的細節

**Acceptance Criteria:**
- 支援滑鼠滾輪縮放圖表
- 支援拖曳移動圖表（pan）
- 支援點擊圖表重置回原始大小
- 圖表有適當的容器邊界，不會溢出聊天氣泡

### US-3：SSE 流式傳輸中的圖表渲染

**作為**使用者
**我想要**在 AI 回覆串流中，等 mermaid block 完整後才渲染
**以便於**避免不完整的語法導致渲染失敗或閃爍

**Acceptance Criteria:**
- 串流中偵測到 ` ```mermaid` 開頭時，暫不渲染該 block
- 偵測到對應的 ` ``` ` 結束標記後，完整渲染
- 未關閉的 mermaid block 顯示為一般 code block（含載入提示）
- 渲染過程不影響串流中其他已渲染的內容

### US-4：語法錯誤處理

**作為**使用者
**我想要**在 mermaid 語法有錯誤時看到友善的錯誤提示
**以便於**我知道圖表無法渲染，而不是看到空白區域

**Acceptance Criteria:**
- 語法錯誤時顯示原始 mermaid 程式碼
- 在程式碼上方或下方顯示錯誤訊息（紅色提示框）
- 錯誤訊息應為使用者可理解的文字（不是 stack trace）

### US-5：效能感受

**作為**使用者
**我想要**mermaid 功能不影響聊天頁面的載入速度
**以便於**我在沒有圖表的對話中也能快速操作

**Acceptance Criteria:**
- mermaid.js 僅在首次偵測到 mermaid 內容時載入（lazy loading）
- 未使用 mermaid 的頁面完全不載入該庫
- 庫載入期間顯示 loading skeleton/spinner

## Requirements

### Functional Requirements

#### FR-1：marked.js Custom Renderer

在 `markdown_parser.js` 中增加自定義 code block renderer：

- 偵測語言標記為 `mermaid` 的 code block
- 將其標記為待渲染的 mermaid 容器（`<div class="o_woow_mermaid" data-mermaid="...">`）
- 其他語言的 code block 維持現有 `<pre><code>` 行為

#### FR-2：DOMPurify 白名單擴充

調整 DOMPurify 設定以允許 mermaid 渲染所需的元素：

- 允許 `<div>` 上的 `data-mermaid` 和 `data-processed` 屬性
- 渲染後的 SVG 因為是由 mermaid.js 在 DOM 中動態生成，不經過 DOMPurify，因此不需要額外放行 SVG 標籤

#### FR-3：Mermaid Lazy Loader

建立 `mermaid_loader.js` 服務：

- 動態載入 mermaid.js（CDN 或 bundled）
- 維護單例 Promise，避免重複載入
- 初始化 mermaid 設定（theme、securityLevel 等）
- 提供 `renderMermaid(container)` 方法

#### FR-4：Post-Render Hook

在訊息渲染後觸發 mermaid 渲染：

- 訊息 DOM 插入後，掃描 `.o_woow_mermaid` 容器
- 呼叫 mermaid API 將容器內容渲染為 SVG
- 處理渲染成功/失敗

#### FR-5：SSE 流式偵測

修改 `AiChat.js` 中的串流邏輯：

- 偵測串流文字中的 ` ```mermaid` 開始標記
- 追蹤 mermaid block 的開啟/關閉狀態
- block 未關閉時，在渲染層標示為「rendering pending」
- block 關閉後觸發 mermaid 渲染

#### FR-6：互動式圖表容器

為 mermaid 渲染結果提供互動容器：

- 滑鼠滾輪縮放（zoom in/out，有上下限）
- 拖曳平移（pan）
- 雙擊或按鈕重置至原始大小（reset）
- 容器有最大高度限制，超出時可滾動

### Non-Functional Requirements

#### NFR-1：效能
- mermaid.js lazy load 不阻塞頁面渲染
- 單個圖表渲染時間 < 2 秒
- 一次頁面中渲染 10 個圖表仍保持流暢

#### NFR-2：安全性
- mermaid 設定 `securityLevel: 'strict'`（禁止執行 JavaScript）
- 使用者輸入的 mermaid 內容經過 sanitize
- 不使用 `sandbox: false` 模式

#### NFR-3：相容性
- 支援現有的 Odoo 18 asset bundle 系統
- 與 OWL 的 `t-out` / `markup()` 渲染機制相容
- 不破壞現有的 markdown 渲染功能

#### NFR-4：可維護性
- mermaid.js 版本可獨立更新
- 渲染邏輯與聊天組件解耦
- 統一在 `parseMarkdown()` 入口處理

## Technical Design Overview

### 核心修改檔案

| 檔案 | 修改內容 |
|------|---------|
| `src/static/src/paas/services/markdown_parser.js` | 新增 mermaid code block 自定義 renderer、調整 DOMPurify 白名單 |
| `src/static/src/paas/services/mermaid_loader.js` | **新建** - Mermaid lazy loader 和渲染服務 |
| `src/static/src/paas/components/ai-chat/AiChat.js` | 新增 post-render hook 觸發 mermaid 渲染、SSE mermaid block 偵測 |
| `src/static/src/paas/components/ai-chat/AiChat.xml` | 可能需調整 streaming 中的 mermaid placeholder 顯示 |
| `src/static/src/paas/components/ai-chat/AiChat.scss` | 新增 mermaid 容器樣式（縮放、錯誤狀態） |
| `src/static/src/paas/styles/components/_mermaid.scss` | **新建** - Mermaid 全域樣式（所有使用 parseMarkdown 的地方共用） |
| `src/__manifest__.py` | 註冊新的 JS/SCSS 資產 |

### 渲染流程

```
Markdown 輸入
    ↓
marked.js parse (custom renderer)
    ↓ 偵測 ```mermaid ... ```
    ↓
輸出: <div class="o_woow_mermaid" data-mermaid="encoded-content">
         <pre><code>原始 mermaid 碼（fallback）</code></pre>
       </div>
    ↓
DOMPurify sanitize（允許 data-mermaid 屬性）
    ↓
OWL markup() → DOM 插入
    ↓
Post-render hook 掃描 .o_woow_mermaid
    ↓
Lazy load mermaid.js（首次）
    ↓
mermaid.render() → SVG 取代 <pre> 內容
    ↓
加入互動控制（zoom/pan）
```

### mermaid.js 載入策略

**方案：CDN Lazy Load + Local Fallback**

```javascript
// 優先從 CDN 載入（效能佳，有 cache）
// 失敗時從 local bundle 載入（離線可用）
```

或可考慮將 `mermaid.min.js` 放入 `src/static/src/paas/lib/`（如同現有的 `marked.min.js`），透過 `__manifest__.py` 註冊但標記為 lazy。

## Success Criteria

| 指標 | 目標 |
|------|------|
| Mermaid 圖表正確渲染率 | 所有標準 mermaid 語法 100% 支援 |
| 載入效能影響 | 無 mermaid 內容時，頁面載入時間增加 < 5ms |
| 首次 mermaid 渲染時間 | < 3 秒（含庫載入） |
| 後續 mermaid 渲染時間 | < 1 秒 |
| 錯誤處理覆蓋率 | 100% 的語法錯誤顯示友善提示 |

## Constraints & Assumptions

### Constraints
- 必須相容 Odoo 18 的 asset bundle 機制（`__manifest__.py` 管理）
- mermaid.js 體積約 1.5-2MB（minified），需 lazy load
- OWL 的 `t-out` 在 DOM 更新後不會自動觸發 callback，需手動 hook
- DOMPurify 會過濾 SVG 標籤，mermaid 渲染必須在 sanitize 之後進行

### Assumptions
- AI 回覆中使用標準 ` ```mermaid ` 語法（GitHub Flavored Markdown 格式）
- 使用者瀏覽器支援 SVG 渲染（所有現代瀏覽器皆支援）
- mermaid.js CDN 可用性足夠（或採用 local bundle 方案）

## Out of Scope

- **Mermaid 編輯器**：不提供所見即所得的 mermaid 編輯功能
- **圖表匯出**：不支援將渲染後的圖表匯出為 PNG/PDF
- **自定義主題**：不支援使用者自訂 mermaid 主題，使用預設主題
- **Server-side 渲染**：不在後端渲染 mermaid，完全前端處理
- **舊訊息回填**：不需要重新處理已儲存的歷史訊息，僅在前端渲染時處理

## Dependencies

### 外部依賴
- **mermaid.js** (v11+) - 核心渲染庫，MIT License
  - CDN: `https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js`
  - 或 local bundle

### 內部依賴
- `markdown_parser.js` - 需修改自定義 renderer
- `AiChat.js` - 需加入 post-render hook
- `__manifest__.py` - 需註冊新資產

### 無依賴
- 後端模型無需修改
- API 端點無需修改
- 資料庫 schema 無需修改
