---
name: n8n-mcp-integration
description: Generic Cloud Service MCP auto-provision mechanism with n8n as first implementation via sidecar deployment
status: backlog
created: 2026-02-27T14:06:14Z
---

# PRD: n8n-mcp-integration

## Executive Summary

為 Cloud Service 建立通用的「MCP 自動配置」機制，讓應用模板（CloudAppTemplate）可以聲明式地定義 MCP sidecar 配置。當 Cloud Service 部署完成後，系統自動建立對應的 user-scope MCP Server 記錄並同步工具。

**第一個實作目標：n8n**。每個 n8n Cloud Service instance 部署時自動附帶 n8n-mcp sidecar container，並在 Odoo 端自動建立 MCP Server 記錄，讓 AI Chat 能透過 MCP 直接操作該使用者的 n8n workflows。

### 架構定位（混合架構）

```
系統共用工具 → MCPJungle (已有)     ← 不動

每個 n8n 的工具 → n8n-mcp sidecar   ← 本 PRD 範圍
                  (per-pod, 隨 n8n 部署)
```

- **MCPJungle** 繼續負責系統級共用工具（不在本 PRD 範圍）
- **n8n-mcp sidecar** 為每個 n8n instance 提供專屬的 MCP 端點
- Odoo 端透過現有的 user-scope MCP Server 機制整合

## Problem Statement

### 現況

- Cloud Service（如 n8n）部署後，使用者若想讓 AI 操作該服務，需手動：
  1. 進入 n8n UI 產生 API Key
  2. 在 Cloud Service 設定頁面手動新增 MCP Server（填 URL + API Key）
  3. 手動同步 tools
- 這個流程繁瑣且容易出錯，大多數使用者不會做

### 為什麼現在做

- MCP 基礎架構已完成（`ai-mcp-integration` epic 已交付）
- n8n-mcp 專案已成熟，原生支援 HTTP transport 和 sidecar 部署
- 使用者需要 AI 能直接操作 n8n（建立/管理 workflows 是高頻需求）
- 通用機制可以複用到其他 Cloud Service（AnythingLLM、PostgreSQL 等）

## User Stories

### US-1: Template 定義 MCP Sidecar（Admin）

**作為** 平台管理員
**我想要** 在 CloudAppTemplate 中聲明式地定義 MCP sidecar 配置
**以便** 新部署的 Cloud Service 自動擁有 MCP 能力

**Acceptance Criteria:**
- CloudAppTemplate 新增 MCP 相關欄位（sidecar 啟用、image、port、transport 等）
- 設定整合到 Helm values 中，部署時自動注入 sidecar container
- n8n 模板預設啟用 MCP sidecar
- 管理員可在模板中停用 MCP sidecar

### US-2: 部署時自動配置 MCP（系統）

**作為** 系統
**我想要** 在 Cloud Service 部署完成後自動建立 MCP Server 記錄
**以便** AI Chat 能立即使用該服務的工具

**Acceptance Criteria:**
- Cloud Service 狀態轉為 `running` 時，檢查模板是否啟用 MCP
- 自動建立 user-scope `mcp_server` 記錄（URL 指向 sidecar 端點）
- 自動觸發 `action_sync_tools()` 探索可用工具
- MCP Server 記錄隨 Cloud Service 刪除時 cascade delete（已有）
- n8n API Key 於部署時透過環境變數自動生成，注入 sidecar

### US-3: 使用者在 AI Chat 中操作 n8n（End User）

**作為** 擁有 n8n Cloud Service 的使用者
**我想要** 在 AI Chat 中直接操作我的 n8n（建立/查看/管理 workflows）
**以便** 不需要切換到 n8n UI 就能完成自動化設定

**Acceptance Criteria:**
- AI Chat 中可以使用 n8n-mcp 提供的工具（create/list/update/activate workflows 等）
- 工具操作的是使用者自己的 n8n instance（非其他人的）
- 工具執行結果在 AI Chat 中可視化（已有 tool call 顯示機制）
- n8n 文件查詢類工具（node docs、templates）也可使用

### US-4: 使用者管理 MCP Server 設定（End User）

**作為** Cloud Service 擁有者
**我想要** 查看和管理自動建立的 MCP Server 設定
**以便** 必要時可以重新同步、停用或更新設定

**Acceptance Criteria:**
- Cloud Service 設定頁面顯示已關聯的 MCP Servers 列表（已有 UI）
- 可手動觸發重新同步工具
- 可停用/啟用個別工具
- 自動建立的 MCP Server 有標記（與手動建立的區分）

## Requirements

### Functional Requirements

#### FR-1: CloudAppTemplate MCP 設定欄位

在 `woow_paas_platform.cloud_app_template` 新增：

| Field | Type | Description |
|-------|------|-------------|
| `mcp_enabled` | Boolean | 是否為此模板啟用 MCP sidecar（default: False） |
| `mcp_sidecar_image` | Char | Sidecar Docker image（如 `ghcr.io/czlonkowski/n8n-mcp:latest`） |
| `mcp_sidecar_port` | Integer | Sidecar 對外 port（default: 3001） |
| `mcp_transport` | Selection | MCP transport 類型：`sse` / `streamable_http`（default: `streamable_http`） |
| `mcp_endpoint_path` | Char | MCP 端點路徑（default: `/mcp`） |
| `mcp_sidecar_env` | Text | Sidecar 環境變數 JSON（如 `{"MCP_MODE": "http"}`） |
| `mcp_auto_sync` | Boolean | 部署後是否自動同步 tools（default: True） |

#### FR-2: Helm Values 注入 Sidecar 配置

當模板啟用 MCP 時，部署流程在 Helm values 中注入 sidecar 配置：

```json
{
  "mcp_sidecar": {
    "enabled": true,
    "image": "ghcr.io/czlonkowski/n8n-mcp:latest",
    "port": 3001,
    "env": {
      "MCP_MODE": "http",
      "N8N_API_URL": "http://localhost:5678",
      "N8N_API_KEY": "{{ auto_generated_api_key }}"
    },
    "resources": {
      "requests": { "memory": "128Mi", "cpu": "100m" },
      "limits": { "memory": "256Mi", "cpu": "200m" }
    }
  }
}
```

> **注意**：需要對應的 Helm chart 支援 sidecar container 的 template。n8n Helm chart 可能需要自定義或使用 custom chart 來支援 sidecar。

#### FR-3: n8n API Key 自動生成

部署 n8n 時自動生成 API Key：

- 方案：透過 n8n 環境變數 `N8N_API_KEY` 預設注入 API Key
- 生成：UUID v4 格式，存入 K8s Secret
- 傳遞：注入到 n8n container 環境變數 + n8n-mcp sidecar 環境變數
- 存儲：同時存入 Odoo 的 MCP Server 記錄（`api_key` 或 `headers_json`）

> **待驗證**：n8n 是否支援透過環境變數預設 API Key。如不支援，需研究替代方案（如 n8n init script 或 POST /api/v1/api-keys）。

#### FR-4: 部署後自動建立 MCP Server 記錄

Cloud Service 部署完成後的 hook：

1. 檢查 `template.mcp_enabled`
2. 構建 MCP endpoint URL（基於 service 的 subdomain + sidecar port + endpoint path）
3. 建立 `mcp_server` 記錄：
   - `scope = 'user'`
   - `cloud_service_id = service.id`
   - `transport = template.mcp_transport`
   - `url` = sidecar endpoint URL
   - `auto_created = True`（新增 Boolean 欄位）
4. 如果 `template.mcp_auto_sync`，排隊觸發 `action_sync_tools()`

#### FR-5: MCP Server 記錄標記

`mcp_server` model 新增：

| Field | Type | Description |
|-------|------|-------------|
| `auto_created` | Boolean | 是否由系統自動建立（readonly, default: False） |

自動建立的 MCP Server 在 UI 中有明確標記，使用者不可修改 URL 和 transport（由系統管理）。

#### FR-6: Sidecar 健康檢查與重試

- 部署後 MCP sidecar 可能需要時間啟動
- `action_sync_tools()` 需有重試機制（最多 3 次，間隔 10 秒）
- 如果初次同步失敗，記錄 `state = 'error'`，使用者可手動重試

### Non-Functional Requirements

#### NFR-1: Performance
- Sidecar container 資源限制：256Mi memory、200m CPU
- MCP endpoint 回應時間 < 2 秒（tool discovery）
- Sidecar 啟動時間 < 30 秒

#### NFR-2: Security
- n8n API Key 不暴露在前端 UI（已有 `groups='base.group_system'`）
- Sidecar 只監聽 pod 內部或受 Ingress 保護的端點
- API Key 存儲在 K8s Secret 中，非 ConfigMap

#### NFR-3: Reliability
- Sidecar 故障不影響 n8n 主服務運行
- MCP Server 連線失敗時 AI Chat 仍能正常對話（已有 graceful fallback）
- Sidecar 支援 liveness/readiness probe

#### NFR-4: Extensibility
- 通用機制支援未來其他 Cloud Service 類型
- 模板欄位設計不綁定 n8n-mcp 的特定配置
- Sidecar image 和 env 完全由模板配置決定

## Architecture

### 部署架構

```
┌─────────── K8s Pod ────────────┐
│                                │
│  ┌──────────┐  ┌────────────┐  │
│  │   n8n    │  │  n8n-mcp   │  │
│  │ :5678    │←─│  (sidecar) │  │
│  │          │  │  :3001     │  │
│  └──────────┘  └────────────┘  │
│       ↑              ↑         │
│   N8N_API_KEY    N8N_API_URL   │
│   (shared K8s Secret)          │
└────────────────────────────────┘
         ↑                ↑
    User Browser      Odoo AI Client
    (n8n UI)          (MCP transport)
```

### 資料流

```
1. Admin 設定 n8n 模板（mcp_enabled=True）
2. 使用者部署 n8n Cloud Service
3. PaaS Operator 執行 Helm install（含 sidecar 配置）
4. K8s 建立 Pod（n8n + n8n-mcp containers）
5. n8n 啟動，API Key 透過 env var 生效
6. n8n-mcp sidecar 啟動，連接 localhost:5678
7. Odoo 收到部署成功回調
8. Odoo 自動建立 mcp_server 記錄（url = sidecar endpoint）
9. Odoo 觸發 action_sync_tools()
10. n8n-mcp 回傳 tool list（create/list/update/... workflows）
11. Odoo 建立 mcp_tool 記錄
12. 使用者在 AI Chat 中可使用 n8n 工具
```

### Model 關係（新增部分標記 ★）

```
cloud_app_template
    ├── mcp_enabled ★
    ├── mcp_sidecar_image ★
    ├── mcp_sidecar_port ★
    ├── mcp_transport ★
    ├── mcp_endpoint_path ★
    ├── mcp_sidecar_env ★
    └── mcp_auto_sync ★

cloud_service
    └── user_mcp_server_ids (O2M) ──→ mcp_server (已有)

mcp_server
    ├── auto_created ★
    └── tool_ids (O2M) ──→ mcp_tool (已有)
```

### 檔案變更清單

| 檔案 | 變更類型 | 說明 |
|------|---------|------|
| `src/models/cloud_app_template.py` | 修改 | 新增 `mcp_*` 欄位 |
| `src/models/mcp_server.py` | 修改 | 新增 `auto_created` 欄位 |
| `src/views/cloud_app_template_views.xml` | 修改 | 模板 form view 加入 MCP 設定區塊 |
| `src/views/mcp_server_views.xml` | 修改 | 顯示 auto_created 標記 |
| `src/data/cloud_app_templates.xml` | 修改 | n8n 模板加入 MCP sidecar 配置 |
| `src/controllers/paas.py` | 修改 | 部署後 hook：自動建立 MCP Server |
| `src/security/ir.model.access.csv` | 檢查 | 確認新欄位權限 |

### n8n-mcp Sidecar 部署（Helm Chart 層面）

n8n Helm chart 需支援 sidecar container。如果使用的 n8n Helm chart 不原生支援，需：

1. Fork/自定義 n8n Helm chart 加入 sidecar template
2. 或使用 `extraContainers` pattern（部分 Helm chart 支援）

```yaml
# Helm values 中的 sidecar 定義
extraContainers:
  - name: n8n-mcp
    image: ghcr.io/czlonkowski/n8n-mcp:latest
    ports:
      - containerPort: 3001
    env:
      - name: MCP_MODE
        value: "http"
      - name: N8N_API_URL
        value: "http://localhost:5678"
      - name: N8N_API_KEY
        valueFrom:
          secretKeyRef:
            name: {{ release }}-api-key
            key: api-key
    resources:
      requests:
        memory: "128Mi"
        cpu: "100m"
      limits:
        memory: "256Mi"
        cpu: "200m"
    livenessProbe:
      httpGet:
        path: /health
        port: 3001
      initialDelaySeconds: 15
      periodSeconds: 30
    readinessProbe:
      httpGet:
        path: /health
        port: 3001
      initialDelaySeconds: 10
      periodSeconds: 10
```

## Success Criteria

| Metric | Target |
|--------|--------|
| 使用者部署 n8n 後，MCP Server 記錄自動建立 | ✅ |
| AI Chat 能列出 n8n-mcp 提供的所有工具（~20 個） | ✅ |
| AI 能成功建立一個 n8n workflow | ✅ |
| AI 能列出使用者的所有 workflows | ✅ |
| MCP sidecar 故障不影響 n8n 主服務 | ✅ |
| 刪除 Cloud Service 時 MCP Server 記錄自動清除 | ✅ |
| 通用機制可套用到其他 Cloud Service 模板 | ✅ |

## Constraints & Assumptions

### Constraints
- 依賴 n8n Helm chart 支援 `extraContainers` 或需自定義 chart
- n8n API Key 預設注入方式需驗證（n8n 環境變數 `N8N_API_KEY` 支援度）
- Sidecar 與 n8n 必須在同一個 Pod 中（共享 localhost 網路）
- n8n-mcp image 由第三方維護（`czlonkowski/n8n-mcp`），需評估穩定性

### Assumptions
- n8n-mcp 的 HTTP transport 端點穩定可用
- Odoo 能存取 sidecar 的 MCP endpoint（透過 Ingress 或 internal service）
- `langchain-mcp-adapters` 能正確連接 n8n-mcp 的 streamable HTTP transport
- n8n 支援透過環境變數或啟動腳本預設 API Key

## Out of Scope

- **MCPJungle 整合** — MCPJungle 作為系統級閘道獨立運作，不在本 PRD 範圍
- **n8n-mcp 的 stdio 模式** — 只使用 HTTP transport
- **n8n-mcp 的 multi-tenant 模式** — Sidecar 部署不需要 multi-tenant
- **MCP Resources / Prompts** — 只使用 MCP Tools 功能
- **n8n-mcp 自定義 image 構建** — 使用官方 image
- **其他 Cloud Service 的 MCP sidecar 實作** — 本期只做 n8n，通用機制留待擴展
- **Helm chart 改造** — 假設 chart 已支援 extraContainers 或由運維團隊處理
- **MCP Server 的定時自動同步** — 本期只做手動 + 部署時自動同步

## Dependencies

### External
- `ghcr.io/czlonkowski/n8n-mcp` Docker image
- n8n Helm chart（需支援 sidecar/extraContainers）
- `langchain-mcp-adapters` >= 0.2.0（已安裝）

### Internal（已完成，本 PRD 依賴）
- `ai-mcp-integration` PRD — MCP Server/Tool model、AI Client tool calling、Tool call 可視化
- `cloud-services-mvp` PRD — Cloud Service 部署流程、PaaS Operator
- `project-cloud-service-binding` PRD — AI Chat ↔ Cloud Service 關聯

## Implementation Notes

### Phase 建議

**Phase A: 通用 MCP 自動配置機制**
- CloudAppTemplate 新增 MCP 設定欄位
- 模板 form view 更新
- 部署後 hook 邏輯（自動建立 MCP Server 記錄）
- `mcp_server` 新增 `auto_created` 欄位
- 預估：3-4 tasks

**Phase B: n8n MCP Sidecar 配置**
- n8n 模板更新（加入 MCP sidecar 配置）
- n8n API Key 自動生成邏輯
- Helm values 注入 sidecar 配置
- n8n Helm chart sidecar template（如需自定義）
- 預估：3-4 tasks

**Phase C: 端到端整合測試**
- 部署 n8n → 自動建立 MCP → AI Chat 使用工具
- Sidecar 健康檢查與重試機制
- 錯誤處理與 fallback
- 預估：2-3 tasks

### 待驗證項目

1. **n8n API Key 環境變數**：確認 n8n 是否支援 `N8N_API_KEY` 環境變數預設 API Key
2. **n8n Helm chart extraContainers**：確認目前使用的 n8n Helm chart 是否支援
3. **n8n-mcp health endpoint**：確認 `/health` 端點是否存在
4. **Sidecar MCP endpoint 的外部可達性**：Ingress 配置是否需要額外 path routing
