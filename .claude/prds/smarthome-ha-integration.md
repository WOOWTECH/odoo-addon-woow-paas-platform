---
name: smarthome-ha-integration
description: Smart Home 功能模組 - Cloudflare Tunnel 管理、OAuth 2.0 Provider、HA Custom Component API 串接
status: backlog
created: 2026-02-28T09:40:46Z
updated: 2026-02-28T09:40:46Z
---

# PRD: Smart Home HA Integration

## Executive Summary

在 woow_paas_platform 模組中新增 **Smart Home** 功能，讓使用者可以透過 Woow PaaS 平台管理 Home Assistant 的遠端存取。每個 Smart Home 實體建立獨立的 Cloudflare Tunnel，使用者在 HA 端安裝 Custom Component 後，透過 OAuth 2.0 認證取得 Tunnel Token，自動在本機部署 cloudflared 進程，實現 HA 的安全遠端存取。

**核心價值**：讓 Home Assistant 使用者無需複雜的網路設定（port forwarding、DDNS），即可透過 Cloudflare Tunnel 安全地遠端存取 HA。

## Problem Statement

### 問題

Home Assistant 使用者要實現遠端存取，通常需要：
1. 設定路由器 port forwarding
2. 購買/設定 DDNS 服務
3. 管理 SSL 憑證
4. 處理 ISP 的 CGNAT 問題

這些步驟對非技術使用者來說門檻很高，且存在安全風險。

### 為什麼現在做

- Woow PaaS 平台已有 Cloudflare Tunnel 整合能力（paas-operator）
- Smart Home 卡片已在 Workspace Detail 頁面的 "Explore More Services" 中預留
- 市場上缺乏整合 PaaS + Smart Home 遠端存取的解決方案

## User Stories

### Persona: Smart Home 使用者（HA 用戶）

**US-1: 建立 Smart Home 實體**
> 身為一個 Workspace 擁有者，我想在 Workspace 中建立一個 Smart Home 實體，以便管理 HA 的遠端存取。

驗收標準：
- 在 Workspace Detail 頁面點擊 "Smart Home → Get Started" 可建立 Smart Home
- Smart Home 建立時自動透過 paas-operator 建立獨立的 Cloudflare Tunnel
- 建立完成後顯示 Tunnel 狀態資訊

**US-2: 查看 Tunnel 狀態**
> 身為一個 Smart Home 擁有者，我想看到 Cloudflare Tunnel 的即時狀態，包括連接器類型、連接器 ID、Tunnel ID、路由、狀態、運作時間。

驗收標準：
- Smart Home 詳情頁顯示完整的 Tunnel 資訊
- 狀態即時反映 Tunnel 連線狀況（connected / disconnected / error）
- 顯示 Tunnel 運作時間

**US-3: HA 端 OAuth 2.0 登入**
> 身為一個 HA 使用者，我想在 HA 中新增 Woow PaaS integration 時，用我的 Woow PaaS 帳號透過 OAuth 2.0 登入。

驗收標準：
- HA config flow 中輸入 Woow PaaS URL
- 跳轉到 Odoo OAuth 2.0 授權頁面
- 使用者授權後自動回到 HA，完成 integration 設定

**US-4: HA 端選擇 Workspace 和 Smart Home**
> 身為一個 HA 使用者，登入後我想選擇一個 Workspace 和對應的 Smart Home，取得 Tunnel Token 自動部署 cloudflared。

驗收標準：
- OAuth 登入後，HA 透過 API 列出使用者的 Workspaces
- 選擇 Workspace 後列出該 Workspace 的 Smart Home 實體
- 選擇 Smart Home 後取得 Tunnel Token
- HA 自動用 Token 啟動 cloudflared 進程
- HA (port 8123) 透過 Cloudflare Tunnel 可遠端存取

**US-5: 管理 Smart Home**
> 身為一個 Workspace 擁有者，我想在 Woow 前台管理我的 Smart Home，包括查看、編輯、刪除。

驗收標準：
- Workspace Detail 中的 Smart Home 區域顯示所有 Smart Home 卡片
- 可查看每個 Smart Home 的 Tunnel 狀態
- 可刪除 Smart Home（同時清理 Cloudflare Tunnel）

## Requirements

### Functional Requirements

#### FR-1: Smart Home Model（Odoo 後端）

新建 `woow_paas_platform.smart_home` 模型：

| 欄位 | 類型 | 說明 |
|------|------|------|
| `name` | Char | Smart Home 名稱 |
| `workspace_id` | Many2one | 所屬 Workspace |
| `state` | Selection | 狀態: pending / provisioning / active / error / deleting |
| `tunnel_id` | Char | Cloudflare Tunnel ID |
| `tunnel_token` | Char | Cloudflare Tunnel Token（加密儲存） |
| `tunnel_name` | Char | Tunnel 名稱 |
| `connector_id` | Char | 連接器 ID |
| `connector_type` | Char | 連接器類型 |
| `tunnel_route` | Char | Tunnel 路由 URL |
| `tunnel_status` | Selection | Tunnel 狀態: connected / disconnected / error |
| `tunnel_uptime` | Char | 運作時間 |
| `ha_port` | Integer | HA 服務 Port（預設 8123） |
| `subdomain` | Char | 分配的子網域 |
| `created_by` | Many2one | 建立者 |
| `error_message` | Text | 錯誤訊息 |

#### FR-2: Cloudflare Tunnel 管理（PaaS Operator）

在 paas-operator 新增獨立 Tunnel 管理能力：

- `POST /api/tunnels` - 建立新的 Cloudflare Tunnel
  - 呼叫 Cloudflare API 建立 Tunnel
  - 取得 Tunnel Token
  - 設定 ingress rule（目標為 `http://localhost:{port}`）
  - 回傳 Tunnel ID + Token + 路由資訊
- `GET /api/tunnels/{tunnel_id}` - 取得 Tunnel 狀態
  - 連線狀態、連接器資訊、運作時間
- `DELETE /api/tunnels/{tunnel_id}` - 刪除 Tunnel
  - 清理 Cloudflare Tunnel + DNS record
- `GET /api/tunnels/{tunnel_id}/token` - 取得 Tunnel Token
  - 需驗證呼叫者權限

#### FR-3: OAuth 2.0 Provider（Odoo）

在 Odoo 中實作完整的 OAuth 2.0 Provider：

- **Authorization Endpoint**: `GET /oauth2/authorize`
  - 支援 Authorization Code Flow
  - 顯示授權頁面（允許/拒絕）
  - 回傳 authorization code
- **Token Endpoint**: `POST /oauth2/token`
  - 支援 grant_type: `authorization_code`, `refresh_token`, `client_credentials`
  - 回傳 access_token, refresh_token, expires_in
- **Token Introspection**: `POST /oauth2/introspect`
- **Token Revocation**: `POST /oauth2/revoke`
- **Client Registration**: Odoo 後台管理 OAuth Client（client_id, client_secret, redirect_uri, scopes）

新建模型：
- `woow_paas_platform.oauth_client` - OAuth 2.0 Client 應用
- `woow_paas_platform.oauth_token` - 已發放的 Token
- `woow_paas_platform.oauth_code` - Authorization Code（短期）

#### FR-4: HA Integration API（Odoo Controller）

提供給 HA Custom Component 的 API endpoints：

- `GET /api/smarthome/workspaces` - 列出使用者的 Workspaces（需 OAuth token）
- `GET /api/smarthome/workspaces/{id}/homes` - 列出 Workspace 的 Smart Homes
- `GET /api/smarthome/homes/{id}` - 取得 Smart Home 詳情
- `GET /api/smarthome/homes/{id}/tunnel-token` - 取得 Tunnel Token
- `GET /api/smarthome/homes/{id}/status` - 取得 Tunnel 即時狀態

所有端點需透過 OAuth 2.0 Bearer Token 認證。

#### FR-5: OWL 前台 UI

在 `/woow` 前台新增 Smart Home 管理功能：

1. **Workspace Detail 整合**
   - "Explore More Services" 中的 Smart Home 卡片 → 點擊 "Get Started" 建立 Smart Home
   - Workspace Detail 新增 "Smart Home" 區塊（類似 Cloud Services）
   - 顯示 Smart Home 卡片列表

2. **Smart Home Detail 頁面**
   - 顯示 Tunnel 完整資訊：連接器類型、連接器 ID、Tunnel ID、路由、狀態、運作時間
   - 狀態指示燈（connected = 綠色、disconnected = 灰色、error = 紅色）
   - 操作按鈕：刪除 Smart Home

3. **建立 Smart Home 流程**
   - 輸入名稱
   - 設定 HA Port（預設 8123）
   - 提交後顯示建立進度
   - 建立完成後顯示連線指引（引導使用者安裝 HA Component）

### Non-Functional Requirements

#### NFR-1: 安全性
- Tunnel Token 在 Odoo DB 中加密儲存
- OAuth 2.0 遵循 RFC 6749 規範
- Authorization Code 有效期 10 分鐘
- Access Token 有效期 1 小時
- Refresh Token 有效期 30 天
- Token 傳輸必須透過 HTTPS
- API 端點需做 rate limiting

#### NFR-2: 效能
- Tunnel 建立完成時間 < 30 秒
- API 回應時間 < 500ms
- Tunnel 狀態查詢可快取 30 秒

#### NFR-3: 可靠性
- Tunnel 建立失敗時需有重試機制
- Smart Home 刪除時確保 Cloudflare 資源完整清理
- OAuth Token 過期時 HA 自動 refresh

#### NFR-4: 相容性
- Home Assistant 2024.1+
- Python 3.11+（HA Component）
- cloudflared binary（HA 端需安裝）

## Success Criteria

| 指標 | 目標 |
|------|------|
| Smart Home 建立成功率 | > 95% |
| Tunnel Token 交付至 HA 的端到端時間 | < 60 秒 |
| OAuth 2.0 授權流程完成率 | > 90% |
| Tunnel 連線可用性 | > 99.5% |
| HA Component 首次設定完成時間 | < 5 分鐘 |

## Architecture

### 整體流程

```
┌───────────────┐     OAuth 2.0      ┌───────────────┐
│  Home         │ ◄───────────────── │    Odoo       │
│  Assistant    │ ───────────────── ▶│  woow_paas    │
│               │   API (Bearer)     │   _platform   │
│  ┌──────────┐ │                    └───────┬───────┘
│  │cloudflared│ │                            │
│  │ (tunnel)  │ │                     HTTP API
│  └─────┬────┘ │                            │
└────────┼──────┘                    ┌───────▼───────┐
         │                           │  PaaS Operator │
         │ Tunnel Connection         │   (FastAPI)    │
         │                           └───────┬───────┘
         │                                   │
    ┌────▼───────────────────────────────────▼────┐
    │              Cloudflare Network              │
    │    ┌─────────────────────────────────┐       │
    │    │  Dedicated Tunnel per SmartHome │       │
    │    └─────────────────────────────────┘       │
    └─────────────────────────────────────────────┘
```

### 建立 Smart Home 流程

```
User (OWL UI)  →  Odoo Controller  →  PaaS Operator  →  Cloudflare API
     │                  │                    │                   │
     │  Create SmartHome│                    │                   │
     │─────────────────▶│                    │                   │
     │                  │  POST /api/tunnels │                   │
     │                  │───────────────────▶│                   │
     │                  │                    │  Create Tunnel    │
     │                  │                    │─────────────────▶│
     │                  │                    │  Tunnel ID+Token  │
     │                  │                    │◀─────────────────│
     │                  │  Tunnel Info       │                   │
     │                  │◀───────────────────│                   │
     │  SmartHome Created                    │                   │
     │◀─────────────────│                    │                   │
```

### HA 串接流程

```
HA Config Flow  →  Odoo OAuth  →  HA Component  →  Odoo API
      │                │               │               │
      │  1. Start      │               │               │
      │  OAuth Flow    │               │               │
      │───────────────▶│               │               │
      │  2. User Login │               │               │
      │  + Authorize   │               │               │
      │◀───────────────│               │               │
      │  3. Auth Code  │               │               │
      │───────────────▶│               │               │
      │  4. Tokens     │               │               │
      │◀───────────────│               │               │
      │                │  5. List WS   │               │
      │                │──────────────▶│               │
      │                │  6. Select WS │               │
      │                │  + SmartHome  │               │
      │                │──────────────▶│               │
      │                │  7. Get Token │               │
      │                │──────────────▶│  Tunnel Token │
      │                │◀──────────────│◀──────────────│
      │                │  8. Run cloudflared           │
      │                │  with token   │               │
```

## Constraints & Assumptions

### Constraints
- paas-operator 已部署在 K8s cluster 中，有 Cloudflare API 存取權限
- Cloudflare 帳號需有 Tunnel 建立權限（可能需要升級 plan）
- HA 本機需能安裝 cloudflared binary

### Assumptions
- 使用者已有 Woow PaaS 帳號和 Workspace
- HA 環境有網路連線能力
- Cloudflare API 穩定可用
- 每個 Cloudflare 帳號的 Tunnel 數量限制足夠使用

## Out of Scope

- HA Custom Component 本身的開發（只提供 API，Component 另案處理）
- cloudflared 的自動安裝（HA 端需預先安裝）
- Tunnel 流量監控和計費
- 多個 HA instance 共用同一個 Tunnel
- Smart Home 裝置控制（只做遠端存取 Tunnel）
- Cloudflare Access Policy 管理

## Dependencies

### 外部依賴
- Cloudflare Tunnel API（建立/管理/刪除 Tunnel）
- Cloudflare DNS API（管理子網域）
- Home Assistant 的 config flow 機制

### 內部依賴
- `woow_paas_platform` 模組（Workspace model）
- `paas-operator` FastAPI 服務
- OWL App 前台框架

### 需要擴充的現有元件
- `paas-operator/src/services/cloudflare.py` - 新增獨立 Tunnel CRUD
- `paas-operator/src/api/` - 新增 tunnels endpoints
- `src/controllers/` - 新增 OAuth 2.0 + SmartHome API controllers
- `src/static/src/paas/` - 新增 Smart Home 相關 OWL components

## Implementation Phases（建議）

### Phase 1: Smart Home Model + PaaS Operator Tunnel API
- 建立 SmartHome model
- paas-operator 新增 Tunnel CRUD endpoints
- Odoo controller 串接 operator

### Phase 2: OAuth 2.0 Provider
- 實作 OAuth 2.0 Provider（Client, Token, Authorization Code models）
- Authorization / Token / Introspect / Revoke endpoints
- OAuth Client 管理介面

### Phase 3: HA Integration API
- 建立 OAuth-protected API endpoints
- Workspace / Smart Home 列表和 Token 交付 API

### Phase 4: OWL 前台 UI
- Smart Home 區塊整合到 Workspace Detail
- Smart Home Detail 頁面
- 建立 Smart Home 流程 UI

### Phase 5: 整合測試
- 端到端測試：建立 Smart Home → OAuth 登入 → 取得 Token → cloudflared 連線
- 錯誤處理和邊界條件測試
