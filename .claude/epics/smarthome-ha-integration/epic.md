---
name: smarthome-ha-integration
status: backlog
created: 2026-02-28T09:42:55Z
progress: 0%
prd: .claude/prds/smarthome-ha-integration.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/113
---

# Epic: Smart Home HA Integration

## Overview

在 woow_paas_platform 中實作 Smart Home 功能，讓使用者透過 Woow PaaS 管理 Home Assistant 的遠端存取。核心是為每個 Smart Home 建立獨立 Cloudflare Tunnel，並透過 OAuth 2.0 讓 HA Custom Component 安全取得 Tunnel Token。

## Architecture Decisions

### AD-1: 獨立 Tunnel 而非共享 Tunnel
- **決策**：每個 Smart Home 建立獨立的 Cloudflare Tunnel（而非在共享 Tunnel 上加 ingress route）
- **理由**：獨立 Tunnel 產生獨立 Token，HA 端可自行管理 cloudflared 進程；共享 Tunnel 無法讓 HA 端拿到可用的 token

### AD-2: OAuth 2.0 Provider 自建
- **決策**：在 Odoo 中自建完整 OAuth 2.0 Provider
- **理由**：Odoo 內建 `auth_oauth` 是 Client 而非 Provider；自建可完全控制 scope、token 生命週期、並整合 Odoo session 認證
- **規範**：遵循 RFC 6749，支援 Authorization Code Flow + Refresh Token + Client Credentials

### AD-3: 擴充現有 PaaS Operator
- **決策**：在現有 paas-operator 中新增 `/api/tunnels` endpoints
- **理由**：複用現有 Cloudflare API 整合（`cloudflare.py`），無需新建獨立服務
- **方式**：擴充 `CloudflareService` 新增 `create_tunnel()` / `delete_tunnel()` / `get_tunnel_status()` 方法

### AD-4: 沿用 Odoo 現有模式
- **SmartHome model**：參照 `CloudService` 模型結構（Many2one workspace_id, state machine, operator client 呼叫）
- **Controller**：參照 `paas.py` JSON-RPC 模式
- **OWL UI**：參照現有 Cloud Services 卡片 + Detail 頁面模式

## Technical Approach

### Backend (Odoo)

**新建模型：**
- `woow_paas_platform.smart_home` - Smart Home 實體（tunnel_id, tunnel_token, state 等）
- `woow_paas_platform.oauth_client` - OAuth 2.0 Client 註冊
- `woow_paas_platform.oauth_token` - 已發放 Token
- `woow_paas_platform.oauth_code` - Authorization Code（短期）

**新建 Controller：**
- `src/controllers/smart_home.py` - Smart Home CRUD API（auth='user'，給 OWL 前台用）
- `src/controllers/oauth2.py` - OAuth 2.0 Provider endpoints（authorize, token, introspect, revoke）
- `src/controllers/ha_api.py` - HA Integration API（OAuth Bearer token 認證）

**擴充：**
- `src/services/paas_operator.py` - 新增 tunnel CRUD client methods

### Backend (PaaS Operator)

**擴充 `cloudflare.py`：**
- `create_tunnel(name)` → Cloudflare API 建立 Tunnel → 回傳 tunnel_id + token
- `delete_tunnel(tunnel_id)` → 刪除 Tunnel + 清理 DNS
- `get_tunnel_status(tunnel_id)` → 查詢 Tunnel 連線狀態、connector 資訊

**新增 `src/api/tunnels.py`：**
- `POST /api/tunnels` - 建立獨立 Tunnel
- `GET /api/tunnels/{tunnel_id}` - 查詢狀態
- `DELETE /api/tunnels/{tunnel_id}` - 刪除 Tunnel
- `GET /api/tunnels/{tunnel_id}/token` - 取得 Token

### Frontend (OWL)

**新增元件：**
- `SmartHomeCard` - Smart Home 卡片（顯示名稱、狀態、Tunnel 資訊）
- `SmartHomeDetailPage` - 詳情頁（Tunnel 完整資訊 + 操作）
- `CreateSmartHomeModal` - 建立流程（名稱 + port 輸入）

**整合：**
- `WorkspaceDetailPage` - 新增 "Smart Home" 區塊（類似 "Cloud Services"）
- `workspace_service.js` - 新增 smart home API methods

## Task Breakdown

- [ ] **Task 1: PaaS Operator - Dedicated Tunnel CRUD API**
  擴充 cloudflare.py 新增 create/delete/status tunnel 方法；新增 tunnels.py API endpoints；新增 Pydantic schemas；更新 main.py router
  **涉及檔案**: `extra/paas-operator/src/services/cloudflare.py`, `extra/paas-operator/src/api/tunnels.py`, `extra/paas-operator/src/models/schemas.py`, `extra/paas-operator/src/main.py`

- [ ] **Task 2: Smart Home Model + Security + Odoo Controller**
  建立 SmartHome model；新增 access rules；擴充 operator client；建立 smart_home controller（CRUD for OWL）；新增到 __manifest__.py
  **涉及檔案**: `src/models/smart_home.py`, `src/models/__init__.py`, `src/security/ir.model.access.csv`, `src/services/paas_operator.py`, `src/controllers/smart_home.py`, `src/controllers/__init__.py`, `src/__manifest__.py`

- [ ] **Task 3: OAuth 2.0 Provider**
  建立 OAuth Client/Token/Code models；實作 authorize/token/introspect/revoke endpoints；建立 OAuth 授權頁面 QWeb template；新增 OAuth middleware for Bearer token 驗證
  **涉及檔案**: `src/models/oauth_client.py`, `src/models/oauth_token.py`, `src/models/oauth_code.py`, `src/controllers/oauth2.py`, `src/views/oauth2_templates.xml`

- [ ] **Task 4: HA Integration API**
  建立 OAuth-protected API endpoints：list workspaces、list smart homes、get smart home detail、get tunnel token、get tunnel status
  **涉及檔案**: `src/controllers/ha_api.py`

- [ ] **Task 5: OWL UI - Smart Home Components**
  建立 SmartHomeCard、SmartHomeDetailPage、CreateSmartHomeModal 元件；整合到 WorkspaceDetailPage；擴充 workspace_service.js；新增路由 `#/workspaces/:id/smarthome/:homeId`
  **涉及檔案**: `src/static/src/paas/pages/`, `src/static/src/paas/components/`, `src/static/src/paas/services/`, `src/static/src/paas/root.js`

- [ ] **Task 6: End-to-End Testing**
  PaaS Operator tunnel API 單元測試；Odoo Smart Home model 測試；OAuth 2.0 flow 測試；HA API endpoint 測試
  **涉及檔案**: `extra/paas-operator/tests/`, `src/tests/`

## Dependencies

```
Task 1 (Operator Tunnel API)
  ↓
Task 2 (Smart Home Model) ← 依賴 Task 1 的 API
  ↓
Task 3 (OAuth 2.0) ← 可與 Task 2 並行
  ↓
Task 4 (HA API) ← 依賴 Task 2 + Task 3
  ↓
Task 5 (OWL UI) ← 依賴 Task 2
  ↓
Task 6 (Testing) ← 依賴全部
```

**可並行**：Task 2 和 Task 3 可同時進行（無互相依賴）

## Success Criteria (Technical)

| 指標 | 目標 |
|------|------|
| Cloudflare Tunnel 建立成功（Operator → Cloudflare API） | API 回傳 tunnel_id + token |
| OAuth 2.0 Authorization Code Flow 完整走通 | authorize → code → token → refresh |
| HA API 透過 Bearer Token 取得 Tunnel Token | 200 + valid token response |
| OWL 前台建立 Smart Home 端到端 | 點擊 → 建立 → 顯示 Tunnel 狀態 |
| Tunnel Token 加密儲存在 DB | 不可明文存取 |

## Estimated Effort

| Task | 預估 |
|------|------|
| 1. Operator Tunnel API | S |
| 2. Smart Home Model + Controller | M |
| 3. OAuth 2.0 Provider | L（最複雜） |
| 4. HA Integration API | S |
| 5. OWL UI | M |
| 6. E2E Testing | M |

**關鍵路徑**：Task 1 → Task 2 → Task 4（OAuth 2.0 是最大的獨立工作項）

## Tasks Created

- [ ] #114 - PaaS Operator - Dedicated Tunnel CRUD API (parallel: true, S)
- [ ] #115 - Smart Home Model + Security + Odoo Controller (parallel: true, M, depends: #114)
- [ ] #116 - OAuth 2.0 Provider (parallel: true, L)
- [ ] #118 - HA Integration API (parallel: false, S, depends: #115+#116)
- [ ] #119 - OWL UI - Smart Home Components (parallel: true, M, depends: #115)
- [ ] #120 - End-to-End Testing (parallel: false, M, depends: all)

Total tasks: 6
Parallel tasks: 4 (#114, #115+#116 可並行, #119)
Sequential tasks: 2 (#118, #120)
Estimated total effort: 58-80 hours
