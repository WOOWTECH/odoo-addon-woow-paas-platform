# Issue #23 Analysis: Standalone App Infrastructure

## Summary
建立 Standalone OWL Application 的基礎設施。這是第一個任務，沒有依賴，且是 sequential (不可並行)。

## Work Streams

### Stream A: Backend Infrastructure (Single Stream)

由於此任務是 `parallel: false`，所有工作由單一 agent 順序完成：

**Files to create/modify:**
1. `controllers/__init__.py` - 新建
2. `controllers/paas.py` - 新建 (HTTP Controller)
3. `__init__.py` - 更新 (匯入 controllers)
4. `views/paas_app.xml` - 新建 (QWeb 模板)
5. `__manifest__.py` - 更新 (Asset Bundle 配置)
6. `static/src/paas/app.js` - 新建 (最小入口)

**Execution Order:**
1. 建立 controllers 目錄和 paas.py
2. 建立 views/paas_app.xml
3. 更新 __manifest__.py
4. 建立 static/src/paas/app.js
5. 驗證 /woow 路由可訪問

## Acceptance Criteria Checklist
- [ ] `/woow` 路由回應 200 OK
- [ ] QWeb 模板正確渲染 HTML
- [ ] Asset Bundle 載入成功
- [ ] Console 無錯誤
- [ ] 顯示 "Woow PaaS Platform" 測試訊息

## Risk Assessment
- **Low Risk**: 標準 Odoo 開發模式，有明確的程式碼範例

## Estimated Time
1-2 小時
