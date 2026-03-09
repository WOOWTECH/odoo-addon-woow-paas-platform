---
name: n8n-mcp-integration
status: backlog
created: 2026-02-27T14:14:58Z
progress: 0%
prd: .claude/prds/n8n-mcp-integration.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/104
---

# Epic: n8n-mcp-integration

## Overview

為 Cloud Service 建立通用的 MCP 自動配置機制，當 Cloud Service 部署完成後，系統自動建立 user-scope MCP Server 記錄。n8n 作為第一個實作目標，透過 sidecar container 方式部署 n8n-mcp。

**核心思路：最大限度利用現有基礎設施，最小化新增代碼。**

現有基礎設施（已完成，不需改動）：
- `mcp_server` model（scope=system/user、cloud_service_id、tool_ids、action_sync_tools）
- `mcp_tool` model（name、schema、active）
- `cloud_service.user_mcp_server_ids` (O2M) 關聯
- AI Chat → cloud_service → user MCP tools 的完整資料流
- Tool calling + 可視化（LangGraph agent loop + SSE streaming）
- `_create_mcp_server` / `_sync_mcp_server` API 方法（paas.py:1010-1099）

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| MCP sidecar 部署方式 | Per-pod sidecar（與 n8n 同 Pod） | 共享 localhost 網路，無需額外 ingress/service；隨 n8n Pod 生命周期 |
| n8n API Key | 部署時自動生成（UUID），透過 K8s Secret 共享 | 避免使用者手動操作；n8n 和 sidecar 都從同一 Secret 讀取 |
| 模板配置方式 | CloudAppTemplate 新增 `mcp_*` 欄位 | 聲明式、通用、不綁定特定 MCP server |
| 自動建立時機 | `_update_service_status()` 中 state 轉為 `running` 時 | 確保 Pod 已就緒、sidecar 可連線 |
| MCP endpoint 可達性 | 透過 sidecar 的 Ingress path routing（`/mcp`） | 與主服務共用 subdomain，PaaS Operator 的 Cloudflare 路由支援 |
| MCPJungle 整合 | 不整合，直接連線 | MCPJungle 用於系統級工具，per-service 工具直接連 sidecar |

## Technical Approach

### 新增 Model 欄位

**CloudAppTemplate**（`src/models/cloud_app_template.py`）：
```python
mcp_enabled = fields.Boolean(default=False)
mcp_sidecar_image = fields.Char()       # e.g. ghcr.io/czlonkowski/n8n-mcp:latest
mcp_sidecar_port = fields.Integer(default=3001)
mcp_transport = fields.Selection([('sse', 'SSE'), ('streamable_http', 'Streamable HTTP')], default='streamable_http')
mcp_endpoint_path = fields.Char(default='/mcp')
mcp_sidecar_env = fields.Text()         # JSON: {"MCP_MODE": "http", ...}
```

**McpServer**（`src/models/mcp_server.py`）：
```python
auto_created = fields.Boolean(default=False, readonly=True)
```

### 部署流程整合

**值注入**（`paas.py:_create_service`，在 merged_values 之後）：

如果 `template.mcp_enabled`，向 merged_values 注入：
```json
{
  "extraContainers": [{
    "name": "mcp-sidecar",
    "image": "ghcr.io/czlonkowski/n8n-mcp:latest",
    "ports": [{"containerPort": 3001}],
    "env": [
      {"name": "MCP_MODE", "value": "http"},
      {"name": "N8N_API_URL", "value": "http://localhost:5678"},
      {"name": "N8N_API_KEY", "valueFrom": {"secretKeyRef": {"name": "...", "key": "api-key"}}}
    ]
  }]
}
```

**自動建立 MCP Server**（`paas.py:_update_service_status`，state 轉 running 時）：

```python
if service.state == 'running' and original_state != 'running':
    self._auto_create_mcp_servers(service)
```

### Helm Chart 相容性

n8n 使用 `oci://8gears.container-registry.com/library/n8n` chart。需驗證：
1. 是否支援 `extraContainers` 或 `sidecars` values key
2. 如不支援，可能需自定義 chart 或用 PaaS Operator 層面注入 sidecar

**備選方案**：如果 Helm chart 不支援 extraContainers，可透過 PaaS Operator 的 K8s API 在 deployment 完成後 patch sidecar container。

### MCP Endpoint URL 構建

Sidecar 在 Pod 內 port 3001 運行。對外暴露有兩種方式：

1. **透過主服務的 Ingress path routing**：`https://{subdomain}.{domain}/mcp` → sidecar:3001
2. **K8s 內部 Service**：`http://svc-{ref}.paas-ws-{id}.svc.cluster.local:3001/mcp`

由於 Odoo 可能在 Docker 中或在 K8s 外部，方案 1 更通用。需在 PaaS Operator 層面支援 sidecar port 的 Ingress routing。

## Implementation Strategy

### 開發順序

1. **Model 欄位 + UI** → 2. **部署值注入** → 3. **自動建立 Hook** → 4. **n8n 模板配置** → 5. **Helm chart + 端到端測試**

### 風險緩解

| 風險 | 緩解 |
|------|------|
| n8n Helm chart 不支援 extraContainers | 提前驗證；備選：PaaS Operator patch |
| n8n 不支援 env var API Key | 提前驗證；備選：n8n init container 或 startup script |
| Sidecar endpoint 不可達 | 先用 K8s internal URL 測試；再加 Ingress routing |
| n8n-mcp image 不穩定 | Pin 到特定版本 tag，不用 latest |

### 測試策略

- Unit：model 欄位、值注入邏輯
- Integration：部署流程 → 自動建立 MCP Server
- E2E：部署 n8n → AI Chat 使用 n8n 工具

## Tasks Created

- [ ] #105 - Verify n8n Helm chart and API Key support (parallel: true, **critical path**)
- [ ] #106 - Add MCP configuration fields to CloudAppTemplate (parallel: true)
- [ ] #107 - Inject MCP sidecar config into Helm values during deployment (depends: #105, #106)
- [ ] #108 - Update n8n template with MCP sidecar configuration (depends: #105, #106, parallel: true)
- [ ] #109 - Auto-create MCP Server record on deployment completion (depends: #106, #107)
- [ ] #110 - Handle MCP sidecar endpoint accessibility (depends: #105, #107, parallel: true)
- [ ] #111 - Add MCP sidecar health check and sync retry mechanism (depends: #109)
- [ ] #112 - End-to-end integration test (depends: #107, #109, #108, #110, #111)

Total tasks: 8
Parallel tasks: 4 (#105, #106, #108, #110)
Sequential tasks: 4 (#107, #109, #111, #112)
Estimated total effort: 34 hours

### Dependency Graph

```
#105 (驗證) ──┬──→ #107 (注入) ──→ #109 (Hook) ──→ #111 (重試) ──→ #112 (E2E)
              │                       ↑                               ↑
#106 (欄位) ──┤──→ #108 (n8n 模板) ──────────────────────────────────┘
              │                                                       ↑
              └──→ #110 (Endpoint) ──────────────────────────────────┘
```

## Dependencies

### External
- `ghcr.io/czlonkowski/n8n-mcp` Docker image（需 pin 版本）
- n8n Helm chart `oci://8gears.container-registry.com/library/n8n`（需驗證 extraContainers）
- n8n API Key env var 支援（需驗證 `N8N_API_KEY`）

### Internal（已完成）
- `ai-mcp-integration` epic — MCP Server/Tool model + AI Client tool calling
- `cloud-services-mvp` epic — Cloud Service 部署流程 + PaaS Operator
- `project-cloud-service-binding` epic — AI Chat ↔ Cloud Service 關聯

## Success Criteria (Technical)

| Criteria | Measure |
|----------|---------|
| MCP 欄位可在模板 form view 設定 | Admin 可啟用/停用 MCP sidecar |
| 部署 n8n 後 MCP Server 記錄自動建立 | state=connected, tools 已同步 |
| AI Chat 工具列表包含 n8n-mcp tools | ≥15 個 n8n 工具可用 |
| Tool calling 成功操作 n8n | 可建立/列出 workflows |
| 刪除 Cloud Service 時清理 | MCP Server + Tools cascade delete |
| 通用機制可套用其他模板 | 改 mcp_* 欄位即可，無硬編碼 |

## Estimated Effort

- **Total**: 8 tasks, 34 hours
- **Critical path**: 001 → 003 → 004 → 007 → 008
- **建議先做**: 001（驗證）+ 002（欄位）並行，結果決定後續技術方向
- **Resource**: 1 developer
