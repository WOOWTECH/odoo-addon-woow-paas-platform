---
name: ai-chat-mermaid-rendering
status: backlog
created: 2026-02-26T07:07:49Z
progress: 0%
prd: .claude/prds/ai-chat-mermaid-rendering.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/90
---

# Epic: ai-chat-mermaid-rendering

## Overview

在 `parseMarkdown()` 中整合 mermaid.js，將 markdown 中的 mermaid code block 渲染為互動式 SVG 圖表。採用 lazy loading 策略（mermaid.js ~2MB），僅在首次偵測到 mermaid 內容時動態載入。透過 OWL `onPatched` lifecycle hook 在 DOM 更新後觸發渲染，並在 SSE 串流中追蹤 mermaid block 完整性。

## Architecture Decisions

### AD-1：mermaid.js 載入策略 — Local Bundle + Dynamic Script Tag

**決定**：將 `mermaid.min.js` 放置於 `src/static/lib/mermaid/`（注意：在 `static/src/paas/` 之外），避免被 `__manifest__.py` 的 `**/*.js` glob 自動載入。透過動態 `<script>` 標籤按需載入。

**理由**：
- 專案已有 `marked.min.js`、`purify.min.js` 的本地 bundle 模式，保持一致
- mermaid.js ~2MB，不能放入主要 asset bundle（會影響所有頁面載入）
- Local bundle 比 CDN 更適合自託管/離線場景
- `static/lib/` 路徑不在 `static/src/paas/**/*.js` 匹配範圍內，不會自動載入

### AD-2：渲染時機 — Post-DOM + onPatched Hook

**決定**：使用 OWL 的 `onPatched` lifecycle hook 在每次 DOM 更新後掃描新增的 `.o_woow_mermaid` 容器並觸發渲染。

**理由**：
- `t-out` 渲染 HTML 後不會觸發 callback
- `onPatched` 在每次響應式狀態變更導致 DOM 更新後執行
- 使用 `data-processed` 屬性標記已渲染容器，避免重複渲染

### AD-3：SSE 串流偵測 — 正則追蹤 Block 狀態

**決定**：在 `streamingHtml` getter 中偵測未關閉的 mermaid block，替換為 loading placeholder。完整的 mermaid block 正常輸出為 `<div class="o_woow_mermaid">`。

**理由**：
- 串流中 mermaid 語法不完整會導致 mermaid.js 解析失敗
- 使用簡單的正則計數 ` ```mermaid` 開/關標記即可判斷
- 不需要修改後端 SSE 邏輯

### AD-4：互動方式 — Pure CSS Transform + JS 事件

**決定**：使用 CSS `transform: scale() translate()` 搭配原生 JS 事件（wheel、mousedown/move/up）實作 zoom/pan，不引入額外庫。

**理由**：
- 功能需求簡單（zoom + pan + reset）
- 避免額外依賴（如 panzoom 庫）
- CSS Transform 效能優異，GPU 加速

### AD-5：DOMPurify 策略 — 允許 data 屬性，SVG 不經過 sanitize

**決定**：在 DOMPurify 白名單中僅增加 `data-mermaid` 和 `data-processed` 屬性。mermaid.js 直接操作 DOM 生成 SVG，不經過 DOMPurify。

**理由**：
- mermaid.js 的 `render()` API 直接產生 SVG 並插入 DOM
- SVG 標籤不需要加入白名單（不經過 sanitize 流程）
- mermaid 設定 `securityLevel: 'strict'` 確保 SVG 安全

## Technical Approach

### 修改範圍

| 檔案 | 變更類型 | 說明 |
|------|---------|------|
| `src/static/lib/mermaid/mermaid.min.js` | **新增** | Mermaid.js v11+ 壓縮檔 |
| `src/static/src/paas/services/mermaid_loader.js` | **新增** | Lazy loader 服務：動態載入、初始化、渲染 API |
| `src/static/src/paas/services/markdown_parser.js` | **修改** | 自定義 code renderer、DOMPurify data 屬性白名單 |
| `src/static/src/paas/components/ai-chat/AiChat.js` | **修改** | `onPatched` hook 觸發渲染、streaming block 偵測 |
| `src/static/src/paas/styles/components/_mermaid.scss` | **新增** | 全域 mermaid 容器樣式（zoom/pan、error、loading） |
| `src/__manifest__.py` | **修改** | 註冊新 SCSS 檔案（JS 已被 glob 自動載入） |

### 渲染流程

```
1. marked.js parse → 自定義 code renderer 偵測 lang=mermaid
   ↓
2. 輸出 <div class="o_woow_mermaid" data-mermaid="base64(content)">
        <pre><code>原始碼（fallback）</code></pre>
      </div>
   ↓
3. DOMPurify sanitize（允許 data-mermaid 屬性通過）
   ↓
4. OWL t-out → DOM 插入
   ↓
5. onPatched → 掃描未處理的 .o_woow_mermaid[data-mermaid]:not([data-processed])
   ↓
6. mermaid_loader.ensureLoaded()（首次時動態載入 script）
   ↓
7. mermaid.render(id, content) → SVG string
   ↓
8. 替換容器內容為 SVG + 互動控制
   ↓
9. 標記 data-processed，綁定 zoom/pan 事件
```

### SSE 串流中的 Mermaid 處理

```
串流文字: "Here is a diagram:\n```mermaid\ngraph TD\n  A-->B\n```\nDone!"

偵測邏輯（在 streamingHtml getter 中）：
1. 掃描文字中的 ```mermaid 標記
2. 如果有未關閉的 block → 該 block 替換為 placeholder
3. 如果 block 已關閉 → 正常交給 marked.js 渲染
4. 渲染後的 DOM 由 onPatched hook 處理
```

## Implementation Strategy

### 開發順序

按依賴關係排序，每個 task 完成後可獨立驗證：

1. **Task 1** — 基礎設施：mermaid.js 庫 + lazy loader 服務
2. **Task 2** — markdown_parser.js 擴展（自定義 renderer + DOMPurify）
3. **Task 3** — AiChat 整合（onPatched hook + SSE 偵測）
4. **Task 4** — 互動式容器（zoom/pan/reset + SCSS 樣式）
5. **Task 5** — 錯誤處理與 polish（error UI + loading state + edge cases）

### 風險緩解

| 風險 | 機率 | 緩解措施 |
|------|------|---------|
| mermaid.js 體積影響載入 | 低 | Lazy load，不在 asset bundle 中 |
| onPatched 頻繁觸發重複渲染 | 中 | `data-processed` 標記 + debounce |
| SSE 串流中 block 偵測不準 | 低 | 簡單正則，僅需偵測 ` ```mermaid` 和 ` ``` ` |
| mermaid.js 與 DOMPurify 衝突 | 低 | SVG 在 sanitize 之後生成，不經過 DOMPurify |

## Task Breakdown Preview

- [ ] Task 1：Mermaid Lazy Loader 服務 — 下載 mermaid.min.js 至 `static/lib/mermaid/`，建立 `mermaid_loader.js` 服務（動態 script 載入、singleton promise、mermaid.initialize、renderMermaid API）
- [ ] Task 2：Markdown Parser Mermaid 擴展 — 自定義 marked.js code renderer（偵測 `mermaid` 語言標記）、DOMPurify 允許 `data-mermaid`/`data-processed` 屬性、輸出 `<div class="o_woow_mermaid">` 容器
- [ ] Task 3：AiChat Mermaid 渲染整合 — `onPatched` hook 掃描並觸發渲染、SSE 串流中偵測未關閉 mermaid block 顯示 placeholder、`_createAiMessage` 和 `loadHistory` 中觸發渲染
- [ ] Task 4：互動式 Mermaid 容器 — SCSS 容器樣式（max-height、overflow）、zoom/pan/reset JS 邏輯（CSS transform）、toolbar（放大/縮小/重置按鈕）、`__manifest__.py` 註冊新 SCSS
- [ ] Task 5：錯誤處理與 Loading 狀態 — mermaid 語法錯誤時顯示原始碼 + 錯誤提示框、庫載入中顯示 skeleton/spinner、edge cases（空 mermaid block、超大圖表）

## Dependencies

### 外部依賴
- **mermaid.js v11+**（MIT License）— 需下載 `mermaid.min.js` 約 2MB

### 內部依賴
- Task 2 依賴 Task 1（需要 loader 服務來渲染）
- Task 3 依賴 Task 2（需要 parser 輸出正確的 mermaid 容器）
- Task 4 依賴 Task 3（需要渲染後的 SVG 來加入互動）
- Task 5 可與 Task 4 並行

### 無需變更
- 後端 Python models / controllers
- API 端點
- 資料庫 schema

## Success Criteria (Technical)

| 指標 | 目標 | 驗證方式 |
|------|------|---------|
| 基本圖表渲染 | flowchart, sequence, class, state, gantt, pie, er, mindmap 全部正確 | 手動測試每種類型 |
| 無 mermaid 效能影響 | 頁面載入不增加額外 JS 請求 | Network tab 確認無 mermaid.js 請求 |
| Lazy load 首次渲染 | < 3 秒（含庫載入） | Performance timing |
| 後續渲染 | < 500ms | Performance timing |
| SSE 串流穩定 | 串流中 mermaid block 不導致錯誤或閃爍 | 手動測試 SSE 回覆含 mermaid |
| 非 mermaid code block | 完全不受影響 | 確認其他語言 code block 正常 |
| Zoom/Pan 互動 | 滾輪縮放、拖曳移動、重置功能正常 | 手動測試 |
| 語法錯誤 | 顯示原始碼 + 友善錯誤提示 | 測試故意錯誤的 mermaid 語法 |

## Estimated Effort

| Task | 預估 | 說明 |
|------|------|------|
| Task 1：Lazy Loader | 小 | 下載庫 + 單一服務檔案 |
| Task 2：Parser 擴展 | 小 | 修改現有檔案，加入 custom renderer |
| Task 3：AiChat 整合 | 中 | 需處理 OWL lifecycle 和 SSE 邏輯 |
| Task 4：互動容器 | 中 | CSS Transform + 事件處理 |
| Task 5：錯誤處理 | 小 | UI 顯示邏輯 |
| **總計** | **中** | 5 個 tasks，純前端，無後端改動 |

## Tasks Created

- [ ] #91 - Mermaid Lazy Loader 服務 (parallel: false)
- [ ] #92 - Markdown Parser Mermaid 擴展 (parallel: false)
- [ ] #93 - AiChat Mermaid 渲染整合 (parallel: false)
- [ ] #94 - 互動式 Mermaid 容器與樣式 (parallel: true)
- [ ] #95 - 錯誤處理與 Loading 狀態 (parallel: true)

Total tasks: 5
Parallel tasks: 2 (#94, #95 可與 #93 並行)
Sequential tasks: 3 (#91 → #92 → #93)
Estimated total effort: 11-16 hours

### 執行順序圖

```
#91 (Lazy Loader)
    ↓
    ├── #92 (Parser 擴展)
    │       ↓
    │       └── #93 (AiChat 整合)
    │
    ├── #94 (互動容器) ←─ 可並行
    │
    └── #95 (錯誤處理) ←─ 可並行（依賴 #91+#92）
```
