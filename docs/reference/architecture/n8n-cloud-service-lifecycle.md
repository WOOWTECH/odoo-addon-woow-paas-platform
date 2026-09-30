---
title: n8n Cloud Service 生命週期
category: reference
implementation_status: implemented
---

# n8n Cloud Service 生命週期

本文件完整說明使用者在 Woow PaaS Platform 上建立一個 n8n Cloud Service 時，從前端操作到最終產生可從外部連線的 FQDN URL，整個系統在背後執行的完整流程。

---

## 目錄

1. [高層架構概覽](#1-高層架構概覽)
2. [系統元件介紹](#2-系統元件介紹)
3. [建立流程：完整序列圖](#3-建立流程完整序列圖)
4. [Phase 1：前端觸發與 Odoo Controller](#4-phase-1前端觸發與-odoo-controller)
5. [Phase 2：Cron 驅動部署](#5-phase-2cron-驅動部署)
6. [Phase 3：Helm Release 安裝與 Cloudflare Tunnel](#6-phase-3helm-release-安裝與-cloudflare-tunnel)
7. [Phase 4：MCP Sidecar 注入](#7-phase-4mcp-sidecar-注入)
8. [Phase 5：n8n 初始化（Post-Deploy Init）](#8-phase-5n8n-初始化post-deploy-init)
9. [Phase 6：MCP Server 自動註冊](#9-phase-6mcp-server-自動註冊)
10. [狀態機與生命週期](#10-狀態機與生命週期)
11. [網路拓撲與 FQDN 產生邏輯](#11-網路拓撲與-fqdn-產生邏輯)
12. [API Key 的產生與同步機制](#12-api-key-的產生與同步機制)
13. [刪除流程](#13-刪除流程)
14. [關鍵檔案索引](#14-關鍵檔案索引)

---

## 1. 高層架構概覽

整個 n8n Cloud Service 的部署涉及三個主要層級：Odoo（應用層）、PaaS Operator（基礎設施控制層）、Kubernetes（執行層）。

```mermaid
graph TB
    subgraph "使用者層"
        User["使用者瀏覽器"]
    end

    subgraph "應用層 — Odoo"
        OWL["OWL Frontend<br/>/woow"]
        Controller["PaasController<br/>paas.py"]
        CloudService["CloudService Model<br/>cloud_service.py"]
        Template["CloudAppTemplate<br/>cloud_app_template.py"]
        McpServerModel["McpServer Model<br/>mcp_server.py"]
    end

    subgraph "控制層 — PaaS Operator (FastAPI)"
        OperatorClient["PaaSOperatorClient<br/>paas_operator.py"]
        ReleasesAPI["Releases API<br/>releases.py"]
        InitAPI["Init API<br/>init.py"]
        HelmSvc["HelmService<br/>helm.py"]
        K8sSvc["KubernetesService<br/>helm.py"]
        CfSvc["CloudflareService<br/>cloudflare.py"]
        N8nInit["N8nInitService<br/>n8n_init.py"]
    end

    subgraph "執行層 — Kubernetes"
        NS["Namespace<br/>paas-ws-{ws_hash}"]
        HelmRelease["Helm Release<br/>svc-{uuid}"]
        N8nPod["n8n Pod"]
        N8nContainer["n8n Container<br/>:5678"]
        McpSidecar["MCP Sidecar<br/>:3000"]
        K8sSecret["K8s Secret<br/>N8N_API_KEY"]
        McpK8sSvc["K8s Service<br/>{release}-mcp"]
    end

    subgraph "外部服務"
        CF["Cloudflare Tunnel"]
        DNS["Cloudflare DNS"]
    end

    User --> OWL
    OWL --> Controller
    Controller --> CloudService
    Controller --> Template
    Controller --> OperatorClient
    OperatorClient --> ReleasesAPI
    OperatorClient --> InitAPI
    ReleasesAPI --> HelmSvc
    ReleasesAPI --> K8sSvc
    ReleasesAPI --> CfSvc
    InitAPI --> N8nInit
    N8nInit --> K8sSvc
    HelmSvc --> HelmRelease
    K8sSvc --> N8nPod
    CfSvc --> CF
    CfSvc --> DNS
    HelmRelease --> N8nPod
    N8nPod --> N8nContainer
    N8nPod --> McpSidecar
    McpSidecar -.->|secretKeyRef| K8sSecret
    CF --> McpK8sSvc
    CF --> N8nContainer
    Controller --> McpServerModel

    style User fill:#e3f2fd
    style OWL fill:#e3f2fd
    style Controller fill:#dcedc8
    style ReleasesAPI fill:#fff3e0
    style N8nPod fill:#fce4ec
```

---

## 2. 系統元件介紹

### 2.1 Odoo 模組（應用層）

| 元件 | 檔案 | 職責 |
|------|------|------|
| PaasController | `src/controllers/paas.py` | 處理前端 API 請求，協調部署流程 |
| CloudService Model | `src/models/cloud_service.py` | 服務實例的資料模型（狀態、Helm 資訊、密鑰等） |
| CloudAppTemplate Model | `src/models/cloud_app_template.py` | 應用模板（Helm chart 設定、MCP sidecar 設定） |
| PaaSOperatorClient | `src/services/paas_operator.py` | 與 PaaS Operator 通訊的 HTTP Client |
| McpServer Model | `src/models/mcp_server.py` | MCP Server 記錄與工具發現 |
| ResConfigSettings | `src/models/res_config_settings.py` | 系統設定（Operator URL、API Key、Domain） |

### 2.2 PaaS Operator（控制層）

| 元件 | 檔案 | 職責 |
|------|------|------|
| FastAPI App | `extra/paas-operator/src/main.py` | API 入口、認證中介層 |
| Releases API | `extra/paas-operator/src/api/releases.py` | Helm Release CRUD + Sidecar Patch |
| Init API | `extra/paas-operator/src/api/init.py` | n8n 初始化端點 |
| HelmService | `extra/paas-operator/src/services/helm.py` | Helm CLI 包裝器 |
| KubernetesService | `extra/paas-operator/src/services/helm.py` | kubectl 操作（Pod、Secret、Deployment） |
| CloudflareService | `extra/paas-operator/src/services/cloudflare.py` | Cloudflare Tunnel 與 DNS 管理 |
| N8nInitService | `extra/paas-operator/src/services/n8n_init.py` | n8n Owner 設定 + API Key 產生 |

### 2.3 n8n 模板設定

n8n 的 CloudAppTemplate 預設配置（定義在 `src/data/cloud_app_templates.xml`）：

| 欄位 | 值 | 說明 |
|------|-----|------|
| `helm_chart_name` | `oci://8gears.container-registry.com/library/n8n` | OCI Helm chart |
| `default_port` | `5678` | n8n 預設 HTTP port |
| `ingress_enabled` | `True` | 建立 Cloudflare Tunnel route |
| `mcp_enabled` | `True` | 啟用 MCP sidecar |
| `mcp_sidecar_image` | `ghcr.io/czlonkowski/n8n-mcp:latest` | MCP sidecar Docker image |
| `mcp_sidecar_port` | `3000` | Sidecar 監聽 port |
| `mcp_transport` | `streamable_http` | MCP 通訊協定 |
| `mcp_endpoint_path` | `/mcp` | MCP 端點路徑 |
| `mcp_api_key_helm_path` | `main.secret.N8N_API_KEY` | API Key 在 Helm values 中的路徑 |
| `post_deploy_init_type` | `n8n` | 部署後自動初始化類型 |

---

## 3. 建立流程：完整序列圖

```mermaid
sequenceDiagram
    actor User as 使用者
    participant FE as OWL Frontend
    participant Ctrl as PaasController
    participant DB as CloudService DB
    participant Cron as ir.cron
    participant Client as PaaSOperatorClient
    participant Operator as PaaS Operator
    participant Helm as HelmService
    participant K8s as Kubernetes
    participant CF as Cloudflare
    participant N8nInit as N8nInitService

    User->>FE: 點擊「部署 n8n」
    FE->>Ctrl: POST /api/workspaces/{id}/services<br/>{action: 'create', template_id, name, values}

    Note over Ctrl: Phase 1：前端驗證與記錄建立
    Ctrl->>Ctrl: 驗證密碼規則（8字元+大小寫+數字）
    Ctrl->>Ctrl: 產生 reference_id (UUID)
    Ctrl->>Ctrl: 產生 subdomain: paas-cs-{ws_hash}-{svc_hash}
    Ctrl->>Ctrl: 產生 helm_release_name: svc-{uuid[:8]}
    Ctrl->>Ctrl: 產生 mcp_api_key (UUID placeholder)
    Ctrl->>Ctrl: 合併 helm_default_values + user_values + API key
    Ctrl->>DB: CREATE CloudService (state=pending)
    Ctrl->>DB: 寫入 deploy_params (expose_config + mcp_api_key)
    Ctrl->>Cron: ir_cron_deploy_cloud_services._trigger()
    Ctrl-->>FE: 回傳 {success, data: {state: 'pending'}}
    FE-->>User: 顯示「部署中」

    Note over Cron: Phase 2：Cron 驅動部署
    Cron->>Cron: _cron_process_pending_deploys() 撈一筆 pending
    Cron->>Client: create_namespace(paas-ws-{ws_hash})
    Client->>Operator: POST /api/namespaces
    Operator->>K8s: kubectl apply namespace + ResourceQuota
    K8s-->>Operator: OK
    Operator-->>Client: OK

    Note over Cron: Phase 3：Helm Install + Cloudflare
    Cron->>Client: install_release(chart, values, expose)
    Client->>Operator: POST /api/releases
    Operator->>Helm: helm install svc-{uuid} oci://...n8n
    Helm->>K8s: 建立 Deployment, Service, Secret, PVC
    K8s-->>Helm: Release deployed
    Helm-->>Operator: ReleaseInfo
    Operator->>CF: create_route(subdomain, service_url)
    CF->>CF: 建立 CNAME DNS + Tunnel ingress rule
    CF-->>Operator: OK
    Operator-->>Client: ReleaseInfo + RouteInfo

    Note over Cron: Phase 4：MCP Sidecar 注入
    Cron->>Client: patch_sidecar(sidecar_config)
    Client->>Operator: POST /api/releases/{ns}/{name}/sidecar
    Operator->>K8s: kubectl patch deployment (JSON Patch)
    K8s->>K8s: 在 Pod 中加入 mcp-sidecar container
    Operator->>K8s: kubectl apply Service ({release}-mcp)
    Operator->>CF: create_route({subdomain}-mcp, svc_url)
    CF-->>Operator: MCP endpoint URL
    Operator-->>Client: SidecarPatchResponse
    Cron->>DB: UPDATE state='deploying', mcp_auth_token

    Note over Cron: Phase 5：狀態輪詢 + n8n Init
    Note over Cron: _cron_check_deploying_services()<br/>每分鐘輪詢 transitional 狀態<br/>（FE get 端點亦會觸發同邏輯）
    Cron->>Client: get_status(ns, release)
    Client->>Operator: GET /api/releases/{ns}/{name}/status
    Operator-->>Client: pods Running, release deployed
    Cron->>DB: UPDATE state='initializing'
    Cron->>Client: init_n8n(ns, release, email, password)
    Client->>Operator: POST /api/releases/{ns}/{name}/init/n8n
    Operator->>N8nInit: initialize()
    N8nInit->>K8s: kubectl exec (n8n container)
    Note over N8nInit: 1. 等待 n8n API ready<br/>2. POST /rest/owner/setup<br/>3. POST /rest/login<br/>4. POST /rest/api-keys
    N8nInit->>K8s: patch Secret (real API key)
    N8nInit->>K8s: rollout restart deployment
    N8nInit-->>Operator: {success, api_key}
    Operator-->>Client: N8nInitResponse
    Cron->>DB: UPDATE state='running', n8n_api_key, helm_values

    Note over Ctrl: Phase 6：MCP Server（已退場：原為自動註冊）
    Note over Ctrl: 現行由使用者於 UI 手動建立<br/>（見 §9 banner）
```

---

## 4. Phase 1：前端觸發與 Odoo Controller

### 4.1 使用者操作

使用者在 OWL Frontend (`/woow`) 的 Marketplace 頁面選擇 n8n 模板，填寫以下資訊：

- **Service Name**：自訂服務名稱
- **n8n Admin Email**：管理員帳號 Email
- **n8n Admin Password**：管理員密碼（至少 8 字元，含大小寫字母與數字）
- **可選設定**：Database Type、Timezone、Log Level

### 4.2 API 請求

前端發送 JSON-RPC 請求：

```
POST /api/workspaces/{workspace_id}/services
{
    "action": "create",
    "template_id": <n8n template ID>,
    "name": "my-n8n",
    "values": {
        "_init.owner_email": "user@example.com",
        "_init.owner_password": "MyP@ssw0rd",
        "config.database.type": "sqlite",
        "main.config.GENERIC_TIMEZONE": "Asia/Taipei"
    }
}
```

### 4.3 Controller 處理邏輯

`_create_service()` 方法（`src/controllers/paas.py:1969`）執行以下步驟：

**4.3.1 驗證**

- 檢查 template 是否存在且啟用
- 驗證密碼規則（n8n 特有）

**4.3.2 身份與命名產生**

```python
reference_id = str(uuid.uuid4())                                        # 不可變唯一識別碼
subdomain = make_service_subdomain(workspace.ws_hash, reference_id, name) # paas-cs-{ws_hash}-{svc_hash}
helm_release_name = f"svc-{reference_id[:8]}"                            # Helm release 名稱
helm_namespace = make_namespace(workspace.ws_hash, prefix=ns_prefix)     # paas-ws-{ws_hash}
```

> 命名工具集中在 `src/services/naming.py`：`make_namespace()`（`paas-ws-` 前綴 + workspace 的 `ws_hash`）、`make_service_subdomain()`（`paas-cs-{ws_hash}-{svc_hash}`，`svc_hash` 對 `reference_id + name` 加鹽雜湊）。namespace 前綴可由 `ir.config_parameter` `woow_paas_platform.namespace_prefix` 覆寫。
>
> 🔴 **`ws_hash` 是 workspace 上的 stored 欄位，不是 slug 的雜湊（#677）。** 修法前它是 `md5(workspace.slug)[:8]`，而 `slug` 由租戶提供的 `name` 逐字推導、`[:8]` 是 32-bit 截斷 ⇒ 租戶離線搜一個 name 就能讓自己的 Helm release 落進別人的 namespace。現在 `ws_hash` 由伺服器產生（`secrets.token_hex(4)`）＋ DB `UNIQUE(ws_hash)`；既有 workspace 由 migration `18.0.1.0.131` 回填成舊公式的值——既有列的 `helm_namespace` / `subdomain` 是存起來的、本來就不會被重推，回填守的是**下一次推導**（同一個 workspace 新建的服務仍落在既有 namespace，不會分家）。`naming.py` 的 maker 收到非 8-hex 一律 raise（漏改呼叫點的絆線）。

**4.3.3 值處理與 API Key 注入**

1. 分離 `_init.*` 前綴的參數（用於 post-deploy init，不傳入 Helm）
2. 將使用者可配置的值與 template 預設值深度合併
3. 產生 `mcp_api_key`（UUID placeholder），注入到 Helm values 的 `main.secret.N8N_API_KEY` 路徑

合併後的 Helm values 範例：

```json
{
    "config": {"database": {"type": "sqlite"}},
    "main": {
        "config": {
            "GENERIC_TIMEZONE": "Asia/Taipei",
            "N8N_LOG_LEVEL": "info",
            "N8N_PUBLIC_API_ENABLED": "true"
        },
        "secret": {
            "N8N_API_KEY": "a1b2c3d4-...-placeholder-uuid"
        },
        "resources": { ... },
        "persistence": {"enabled": true, "type": "dynamic", "size": "2Gi"}
    },
    "updateStrategy": {"type": "Recreate"}
}
```

**4.3.4 建立 DB 記錄並觸發部署 Cron**

- 建立 `CloudService` 記錄，`state='pending'`
- 將部署所需參數（`expose_config`、`mcp_api_key`）序列化寫入 `deploy_params` 欄位
- 呼叫 `ir_cron_deploy_cloud_services._trigger()` 讓 cron 立即接手部署
- 立即回傳 HTTP response 給前端（非阻塞；實際部署在 cron 交易中進行）

---

## 5. Phase 2：Cron 驅動部署

部署改由 Odoo `ir.cron` 驅動（早期的 `threading.Thread` 背景緒做法已移除，原因見 `src/controllers/paas.py:2145` 註解：避免 Odoo 18 `workers=0` 模式下的 ConnectionPool 鎖競爭）。流程：

1. `_create_service()` 建立 `state='pending'` 記錄後，呼叫 `ir_cron_deploy_cloud_services._trigger()`。
2. Cron `_cron_process_pending_deploys()`（`src/models/cloud_service.py:342`）每分鐘執行一次，每輪撈一筆 `pending` 服務交給 `_execute_deploy()`（`src/models/cloud_service.py:369`）；若仍有 pending 則 re-trigger 立即接續。
3. `_execute_deploy()` 在 Odoo 自身的 cron 交易中運作，不受 HTTP request 生命週期影響。

> Cron 定義於 `src/data/cloud_service_cron.xml`：`ir_cron_deploy_cloud_services`（部署 pending）與 `ir_cron_check_deploying_services`（輪詢 deploying/upgrading/initializing 狀態），兩者皆 1 分鐘間隔。

```mermaid
flowchart TD
    A[Cron _execute_deploy 啟動] --> B{取得 PaaS Operator Client}
    B -->|未設定| C[state = error<br/>Error: Operator not configured]
    B -->|OK| D[建立 Namespace]
    D -->|失敗（非 409）| C
    D -->|成功/已存在| E[Helm Install]
    E -->|失敗| F[state = error<br/>Deployment failed]
    E -->|成功| G{MCP 啟用？}
    G -->|否| H[state = deploying]
    G -->|是| I[Patch MCP Sidecar]
    I -->|失敗| J[Warning 但繼續]
    I -->|成功| K[儲存 mcp_auth_token]
    J --> H
    K --> H
    H --> L[寫入 release info<br/>清空 deploy_params<br/>cron 交易 commit]
```

> `_execute_deploy()` 開頭即先 `write({'state': 'deploying'})`，Helm install 成功後寫回 `helm_revision` / `deployed_at` 並清空 `deploy_params`；後續 deploying → initializing → running 的轉換交由 `_cron_check_deploying_services` 輪詢處理（見 §8）。

### 5.1 Namespace 建立

透過 `PaaSOperatorClient.create_namespace()` 呼叫 PaaS Operator：

```
POST /api/namespaces
{
    "name": "paas-ws-9a8b7c6d",
    "cpu_limit": "24",
    "memory_limit": "24Gi",
    "storage_limit": "300Gi"
}
```

PaaS Operator 執行 `kubectl apply` 建立 Namespace + ResourceQuota。若 Namespace 已存在（HTTP 409），則忽略錯誤繼續。

### 5.2 Helm Release 安裝

透過 `PaaSOperatorClient.install_release()` 呼叫 PaaS Operator：

```
POST /api/releases
{
    "namespace": "paas-ws-9a8b7c6d",
    "name": "svc-a1b2c3d4",
    "chart": "oci://8gears.container-registry.com/library/n8n",
    "create_namespace": true,
    "values": { ... merged helm values ... },
    "expose": {
        "enabled": true,
        "subdomain": "paas-cs-9a8b7c6d-f8e3a1b2"
    }
}
```

---

## 6. Phase 3：Helm Release 安裝與 Cloudflare Tunnel

### 6.1 Helm Install 流程

PaaS Operator 的 `HelmService.install()` 執行：

```bash
helm install svc-a1b2c3d4 oci://8gears.container-registry.com/library/n8n \
    --namespace paas-ws-9a8b7c6d \
    --create-namespace \
    --values /tmp/values-xxxxx.yaml \
    --output json
```

n8n Helm chart 在 Kubernetes 中建立：

| 資源 | 名稱格式 | 說明 |
|------|---------|------|
| Deployment | `svc-a1b2c3d4-n8n` | n8n 主要 Deployment |
| Service | `svc-a1b2c3d4-n8n` | ClusterIP Service（port 5678） |
| Secret | `svc-a1b2c3d4-n8n-app-secret` | 存放 N8N_API_KEY 等 secret |
| PVC | `svc-a1b2c3d4-n8n` | 2Gi persistent volume（SQLite 資料） |

### 6.2 Cloudflare Tunnel Route 建立

Helm install 成功後，`_create_cloudflare_route()` 執行：

```mermaid
sequenceDiagram
    participant API as Releases API
    participant CF as CloudflareService
    participant CFAPI as Cloudflare API
    participant DNS as Cloudflare DNS

    API->>API: 等待 3 秒（K8s Service 建立）
    API->>CF: create_route(subdomain, service_url)
    CF->>DNS: 建立 CNAME record<br/>paas-cs-9a8b7c6d-f8e3a1b2.woowtech.io<br/>→ {tunnel_id}.cfargotunnel.com
    DNS-->>CF: OK
    CF->>CFAPI: GET tunnel config (現有 ingress rules)
    CFAPI-->>CF: 現有 rules
    CF->>CF: 加入新 rule:<br/>hostname → http://svc.ns.svc:5678
    CF->>CFAPI: PUT tunnel config (更新 ingress rules)
    CFAPI-->>CF: OK
    CF-->>API: RouteInfo
```

**產生的 FQDN**：`https://paas-cs-9a8b7c6d-f8e3a1b2.woowtech.io`

路由邏輯：
1. 使用者存取 `https://paas-cs-9a8b7c6d-f8e3a1b2.woowtech.io`
2. Cloudflare DNS 解析 CNAME 到 Tunnel
3. Tunnel ingress rule 轉發到 `http://svc-a1b2c3d4-n8n.paas-ws-9a8b7c6d.svc.cluster.local:5678`
4. n8n container 回應

---

## 7. Phase 4：MCP Sidecar 注入

### 7.1 為什麼不用 Helm 直接部署 Sidecar？

n8n Helm chart 不原生支援 sidecar container。因此系統採用「先 Helm install，再 kubectl patch」的策略，透過 JSON Patch 將 MCP sidecar container 注入到現有 Deployment 中。

### 7.2 Sidecar 配置建構

`_build_mcp_sidecar_config()` 方法（`src/models/cloud_service.py:673`，已從 controller 移至 model；controller 端僅留 `src/controllers/paas.py:1883` 移轉註解）建構 sidecar 規格：

```json
{
    "container": {
        "name": "mcp-sidecar",
        "image": "ghcr.io/czlonkowski/n8n-mcp:latest",
        "ports": [{"containerPort": 3000}],
        "env": [
            {"name": "MCP_MODE", "value": "http"},
            {"name": "AUTH_TOKEN", "value": "<generated-uuid>"},
            {"name": "N8N_API_URL", "value": "http://localhost:5678"},
            {"name": "PORT", "value": "3000"},
            {"name": "AUTH_RATE_LIMIT_MAX", "value": "500"},
            {"name": "AUTH_RATE_LIMIT_WINDOW", "value": "900000"},
            {
                "name": "N8N_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "svc-a1b2c3d4-n8n-app-secret",
                        "key": "N8N_API_KEY"
                    }
                }
            }
        ],
        "resources": {
            "requests": {"memory": "128Mi", "cpu": "100m"},
            "limits": {"memory": "256Mi", "cpu": "200m"}
        },
        "livenessProbe": {
            "httpGet": {"path": "/health", "port": 3000},
            "initialDelaySeconds": 15,
            "periodSeconds": 30
        },
        "readinessProbe": {
            "httpGet": {"path": "/health", "port": 3000},
            "initialDelaySeconds": 10,
            "periodSeconds": 10
        }
    }
}
```

**重點設計**：`N8N_API_KEY` 使用 `secretKeyRef` 而非直接值。這確保 Pod 重啟時永遠從 K8s Secret 讀取最新的 API Key。

### 7.3 Sidecar Patch 流程

PaaS Operator 的 `patch_sidecar` endpoint（`extra/paas-operator/src/api/releases.py:375`）執行三步驟：

```mermaid
flowchart LR
    A["1. kubectl patch<br/>JSON Patch 注入 container"] --> B["2. kubectl apply<br/>建立 ClusterIP Service<br/>{release}-mcp"]
    B --> C["3. Cloudflare<br/>建立 MCP Tunnel route"]
```

**Step 1：kubectl JSON Patch**

```bash
kubectl patch deployment svc-a1b2c3d4-n8n \
    --namespace paas-ws-9a8b7c6d \
    --type=json \
    --patch='[{"op":"add","path":"/spec/template/spec/containers/-","value":{...}}]'
```

**Step 2：建立 MCP K8s Service**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: svc-a1b2c3d4-mcp
  namespace: paas-ws-9a8b7c6d
  labels:
    app.kubernetes.io/instance: svc-a1b2c3d4
    app.kubernetes.io/component: mcp-sidecar
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/instance: svc-a1b2c3d4
  ports:
    - port: 3000
      targetPort: 3000
      protocol: TCP
      name: mcp
```

**Step 3：建立 MCP Cloudflare Route**

- Subdomain: `paas-cs-9a8b7c6d-f8e3a1b2-mcp`
- 產生的 FQDN: `https://paas-cs-9a8b7c6d-f8e3a1b2-mcp.woowtech.io`
- 轉發到: `http://svc-a1b2c3d4-mcp.paas-ws-9a8b7c6d.svc.cluster.local:3000`
- MCP Endpoint: `https://paas-cs-9a8b7c6d-f8e3a1b2-mcp.woowtech.io/mcp`

### 7.4 Patch 後的 Pod 結構

```mermaid
graph LR
    subgraph "Pod: svc-a1b2c3d4-n8n-xxxxx"
        direction TB
        N8N["n8n Container<br/>:5678<br/>主要應用"]
        MCP["mcp-sidecar Container<br/>:3000<br/>MCP Bridge"]
    end

    subgraph "K8s Secrets"
        Secret["svc-a1b2c3d4-n8n-app-secret<br/>N8N_API_KEY=<placeholder>"]
    end

    subgraph "K8s Services"
        SvcN8n["svc-a1b2c3d4-n8n<br/>:5678"]
        SvcMcp["svc-a1b2c3d4-mcp<br/>:3000"]
    end

    MCP -->|"localhost:5678<br/>(shared network)"| N8N
    MCP -.->|"secretKeyRef"| Secret
    N8N -.->|"secretKeyRef"| Secret
    SvcN8n --> N8N
    SvcMcp --> MCP
```

> 同一 Pod 中的 containers 共享 network namespace，因此 MCP sidecar 可以透過 `localhost:5678` 直接存取 n8n API。

---

## 8. Phase 5：n8n 初始化（Post-Deploy Init）

### 8.1 觸發時機

`ir_cron_check_deploying_services`（每分鐘）呼叫 `_cron_check_deploying_services()`（`src/models/cloud_service.py:744`），對每筆 transitional 服務跑 `_poll_and_update_status()`（`src/models/cloud_service.py:771`）。前端 get 端點的 `_update_service_status()`（`src/controllers/paas.py:2414`）走相同判定邏輯，因此不論有無前端輪詢都會推進。當偵測到：
- Helm release status = `deployed`
- 所有 Pod 的 `phase` = `Running` 且 `ready` = 全部就緒
- 原始狀態 = `deploying`
- Template 有 `post_deploy_init_type = 'n8n'`

系統將 state 切換為 `initializing`。init 只由 cron 經 model 的單一入口 `_run_post_deploy_init()`（`src/models/cloud_service.py:4308`）執行——#1257 起前端 list/get 只推進 state、不 dispatch init；claim／lease／失敗分類見 [post-deploy init 執行模型 ADR](../../decisions/refactor/post-deploy-init-execution-model.md)。

### 8.2 n8n Init 完整流程

`N8nInitService.initialize()` （`extra/paas-operator/src/services/n8n_init.py:121`）執行以下 6 步驟：

```mermaid
sequenceDiagram
    participant Ctrl as PaasController
    participant Client as OperatorClient
    participant Init as N8nInitService
    participant K8s as KubernetesService
    participant N8n as n8n Container

    Ctrl->>Client: init_n8n(ns, release, email, pwd)
    Client->>Init: POST /api/releases/{ns}/{name}/init/n8n

    Note over Init: Step 1: 找到 n8n Pod
    Init->>K8s: get_pod_name(label=instance=svc-xxx)
    K8s-->>Init: pod-name

    Note over Init: Step 2: 等待 n8n API Ready
    loop 最多 15 次，每次間隔 4s
        Init->>K8s: kubectl exec pod -- node -e "GET /rest/settings"
        K8s->>N8n: HTTP GET localhost:5678/rest/settings
        N8n-->>K8s: 200 OK
        K8s-->>Init: ready
    end

    Note over Init: Step 3: 檢查 Owner 是否已存在
    Init->>K8s: kubectl exec -- node -e "GET /rest/settings"
    K8s->>N8n: 檢查 userManagement.showSetupOnFirstLoad

    Note over Init: Step 4: 建立 Owner
    Init->>K8s: kubectl exec -- node -e "POST /rest/owner/setup"
    K8s->>N8n: {email, password, firstName, lastName}
    N8n-->>K8s: 200 OK + user data

    Note over Init: Step 5: 登入取得 Cookie
    Init->>K8s: kubectl exec -- node -e "POST /rest/login"
    K8s->>N8n: {emailOrLdapLoginId, password}
    N8n-->>K8s: 200 OK + Set-Cookie

    Note over Init: Step 6: 建立 API Key
    Init->>K8s: kubectl exec -- node -e "GET /rest/api-keys/scopes"
    K8s->>N8n: 取得可用 scopes
    N8n-->>K8s: scopes 列表
    Init->>K8s: kubectl exec -- node -e "POST /rest/api-keys"
    K8s->>N8n: {label: "mcp-sidecar", scopes: [...]}
    N8n-->>K8s: 200 OK + {rawApiKey: "n8n_api_..."}
    K8s-->>Init: real API key

    Note over Init: Step 7: 更新 K8s Secret
    Init->>K8s: kubectl patch secret svc-xxx-n8n-app-secret<br/>data.N8N_API_KEY = real_api_key
    K8s-->>Init: OK

    Note over Init: Step 8: Rollout Restart
    Init->>K8s: kubectl rollout restart deployment/svc-xxx-n8n
    K8s-->>Init: OK (Pod 重建中)

    Init-->>Client: {success, api_key, secret_patched, pod_restarted}
    Client-->>Ctrl: N8nInitResponse
```

### 8.3 為什麼要用 Node.js 做 HTTP 請求？

n8n container 內建 Node.js runtime 但沒有 `curl`。`wget` 在處理 n8n body parser 的 encoding 時有問題。因此系統使用 `kubectl exec` 在 n8n container 內直接執行 Node.js 一行式腳本來做 HTTP 請求。

### 8.4 失敗重試機制

#1257 起依「遠端副作用是否可能已發生」分類（完整表見 [ADR](../../decisions/refactor/post-deploy-init-execution-model.md) §3）：
- 確定失敗（operator `error_kind=failed`）：`init_retries` +1，每一代第 5 次轉 `error`。
- 結果未知／部分成功（exec timeout、HTTP 500、read timeout、帶回 `api_key` 的失敗……）：`init_unknown_count` +1，每一代第 2 次轉 `error`；重送前要等 lease 到期（n8n 900s）。部分成功時 `api_key` 先存。
- 等待（`find_pod`／`wait_ready`、connect 失敗）：不耗額度，受等待時限（預設 30 分）約束。
- 重試由 `ir_cron_check_deploying_services` 每分鐘輪詢驅動；前端 get 端點**不再**觸發 init。

---

## 9. Phase 6：MCP Server 自動註冊

> **⚠️ 已退場（DEPRECATED）**：本節描述的「服務轉 `running` 後自動建立 `McpServer` 記錄」機制已於 cycle 4（issue #49 / #51）的 MCP URL 重設計中整段拆除，`_auto_create_mcp_server()` 及其 6 個 call site 已刪除（現僅存在於 `src/tests/test_mcp_integration.py` 的 removal 斷言中）。
>
> 現行設計改為**使用者在 UI 透過 Add Sidecar MCP shortcut 手動建立**，由 sidecar-hint endpoint 預填 URL / transport / api_key。設計脈絡與決策見 [mcp-url-redesign](../../decisions/refactor/mcp-url-redesign.md)。
>
> 以下 9.1–9.3 保留作歷史對照，敘述已就現行 code 校正；標示「（已退場）」者代表該流程不再自動觸發。

### 9.1 建立 MCP Server 記錄（已退場：原為自動建立）

**（已退場）** 原本服務成功轉為 `running` 狀態後，`_auto_create_mcp_server()` 方法自動建立 `McpServer` 記錄。該方法現已移除（見本節開頭 banner）。

現行 `McpServer` 記錄改由使用者手動建立（controller 的 create payload，`src/controllers/paas.py:1397`、`src/controllers/paas.py:1725`）。注意 payload 已改用 `project_id`（綁定 support project），`McpServer` model 本身沒有 `cloud_service_id` 欄位——服務關聯改透過 `server.project_id.cloud_service_id` 取得：

```python
McpServer.create({
    'name': name,
    'url': url,
    'transport': params.get('transport', 'sse'),
    'api_key': params.get('api_key', ''),
    'scope': 'user',
    'project_id': project.id,
})
```

### 9.2 MCP Endpoint URL 優先順序

controller 端 `_compute_sidecar_url_hint()`（`src/controllers/paas.py:2695`，原名 `_build_mcp_endpoint_url`，現為 UI 預填 helper）依以下優先順序建構 URL；model context 另有 twin `cloud_service._model_build_mcp_endpoint_url()`（`src/models/cloud_service.py:965`，供 cron / 非 request caller）：

1. **K8s Internal URL**（最可靠）：`http://{release}-mcp.{namespace}.svc.cluster.local:{port}/mcp`
2. **Subdomain URL**（需要 Cloudflare）：`https://{subdomain}.{paas_domain}/mcp`
3. **Localhost fallback**：`http://localhost:{port}/mcp`

### 9.3 Tool 發現與 Cron 重試（已退場觸發路徑）

- **（已退場）** 原本自動建立後立即嘗試 `action_sync_tools_safe()` 做 tool 發現；此自動觸發隨 §9.1 一併移除。
- Cron job `_cron_retry_mcp_sync`（`src/models/mcp_server.py:237`，註冊於 `src/data/mcp_server_cron.xml`）仍存在，篩選 `auto_created=True` 且 `state='draft'` 的 server 定期重試 tool sync，最多重試 10 次，超過後標記為 `error`。
- 但現行 production code 已無任何處設定 `auto_created=True`，故此 cron 已無對象、實質為 dormant legacy；手動建立的 server（`auto_created=False`）由使用者在 UI 觸發 sync，不走此 cron。

---

## 10. 狀態機與生命週期

### 10.1 CloudService 狀態轉換

```mermaid
stateDiagram-v2
    [*] --> pending: 使用者建立

    pending --> deploying: deploy cron<br/>_execute_deploy 開始
    deploying --> error: Operator 未設定/<br/>Namespace 失敗

    deploying --> initializing: Pods Ready +<br/>需要 Post-Deploy Init
    deploying --> running: Pods Ready +<br/>不需 Init
    deploying --> error: Helm release failed

    initializing --> running: n8n Init 成功
    initializing --> error: Init 重試 5 次仍失敗

    running --> upgrading: 使用者觸發升級
    running --> deleting: 使用者觸發刪除

    upgrading --> running: 升級成功
    upgrading --> error: 升級失敗

    error --> deploying: 重新部署
    error --> deleting: 使用者觸發刪除

    deleting --> [*]: Helm uninstall 完成

    note right of initializing
        n8n 特有狀態
        Owner setup + API key
    end note
```

### 10.2 各狀態說明

| 狀態 | 說明 | 持續時間 |
|------|------|---------|
| `pending` | DB 記錄已建，等待 deploy cron 接手（建立時即 `_trigger()`，通常即時，最差等 1 分鐘整點輪詢） | < 1 秒 ~ 1 分鐘 |
| `deploying` | Helm install 完成，等待 Pods Ready | 30-120 秒 |
| `initializing` | n8n 特有：Owner 設定 + API Key 產生中 | 30-90 秒 |
| `running` | 服務正常運作 | 持續 |
| `upgrading` | Helm upgrade 執行中 | 30-120 秒 |
| `error` | 部署/初始化/升級失敗 | 等待使用者處理 |
| `deleting` | Helm uninstall 執行中 | 10-30 秒 |

---

## 11. 網路拓撲與 FQDN 產生邏輯

### 11.1 完整網路路徑

```mermaid
graph TB
    subgraph "Internet"
        User["使用者"]
    end

    subgraph "Cloudflare"
        DNS1["DNS: paas-cs-9a8b7c6d-f8e3a1b2.woowtech.io<br/>CNAME → tunnel.cfargotunnel.com"]
        DNS2["DNS: paas-cs-9a8b7c6d-f8e3a1b2-mcp.woowtech.io<br/>CNAME → tunnel.cfargotunnel.com"]
        Tunnel["Cloudflare Tunnel<br/>(cloudflared connector)"]
        Rule1["Ingress Rule 1:<br/>paas-cs-9a8b7c6d-f8e3a1b2.woowtech.io<br/>→ http://svc-a1b2c3d4-n8n.paas-ws-9a8b7c6d.svc:5678"]
        Rule2["Ingress Rule 2:<br/>paas-cs-9a8b7c6d-f8e3a1b2-mcp.woowtech.io<br/>→ http://svc-a1b2c3d4-mcp.paas-ws-9a8b7c6d.svc:3000"]
    end

    subgraph "Kubernetes Cluster"
        SvcN8n["Service<br/>svc-a1b2c3d4-n8n:5678"]
        SvcMcp["Service<br/>svc-a1b2c3d4-mcp:3000"]

        subgraph "Pod"
            N8n["n8n :5678"]
            Sidecar["MCP Sidecar :3000"]
        end
    end

    User -->|"HTTPS"| DNS1
    User -->|"HTTPS"| DNS2
    DNS1 --> Tunnel
    DNS2 --> Tunnel
    Tunnel --> Rule1
    Tunnel --> Rule2
    Rule1 -->|"HTTP"| SvcN8n
    Rule2 -->|"HTTP"| SvcMcp
    SvcN8n --> N8n
    SvcMcp --> Sidecar
    Sidecar -->|"localhost:5678"| N8n
```

### 11.2 FQDN 產生規則

| 用途 | FQDN 格式 | 範例 |
|------|-----------|------|
| n8n 主應用 | `{subdomain}.{paas_domain}` | `paas-cs-9a8b7c6d-f8e3a1b2.woowtech.io` |
| MCP Sidecar | `{subdomain}-mcp.{paas_domain}` | `paas-cs-9a8b7c6d-f8e3a1b2-mcp.woowtech.io` |
| MCP Endpoint | `{subdomain}-mcp.{paas_domain}/mcp` | `paas-cs-9a8b7c6d-f8e3a1b2-mcp.woowtech.io/mcp` |

### 11.3 Subdomain 產生演算法

實作集中在 `src/services/naming.py:19`（`make_service_subdomain`）：

```python
# src/services/naming.py
def generate_resource_hash(reference_id, name):   # 加鹽 → 8 碼
    return hashlib.md5((reference_id + name).encode()).hexdigest()[:8]

def make_service_subdomain(ws_hash, reference_id, name):
    return f"paas-cs-{_require_ws_hash(ws_hash)}-{generate_resource_hash(reference_id, name)}"

# models/workspace.py —— ws_hash 的產生點
def _generate_ws_hash(self):
    return secrets.token_hex(4)          # 8 個小寫 hex；DB UNIQUE(ws_hash) 保證跨租戶唯一
```

- `ws_hash`：workspace 上的 **stored 欄位**，伺服器產生的 8 個小寫 hex。
  🔴 **不是 slug 的雜湊（#677）**：修法前是 `md5(workspace.slug)[:8]`，而 slug 由租戶提供的 `name` 逐字推導、`[:8]` 是 32-bit 截斷 ⇒ 租戶搜得出一個 name 讓自己的服務落進別人的 namespace（目標值逐字印在受害者每個公開子網域上）。既有 workspace 由 migration `18.0.1.0.131` 回填成舊公式的結果 ⇒ 該 workspace **下一次**推導（新建服務）算出的 namespace 與修復前 byte-identical；既有列存下來的名字不受影響，它們不會被重推。
- `svc_hash`：以 `reference_id`（UUID）+ 服務名稱加鹽取 md5 前 8 碼，確保相同服務名稱在不同場景下不會產生相同的 subdomain。
- 對應的 K8s namespace 由 `make_namespace(ws_hash, prefix='paas-ws-')` 產生為 `paas-ws-{ws_hash}`（前綴可由 `woow_paas_platform.namespace_prefix` 覆寫）。smart home / security access 另有 `paas-sm-` / `paas-sa-` 前綴的 twin。

---

## 12. API Key 的產生與同步機制

整個 API Key 的生命週期涉及三次變更，這是系統中最精密的同步邏輯之一。

### 12.1 API Key 時間軸

```mermaid
timeline
    title N8N_API_KEY 生命週期
    section Phase 1 - 建立
        Odoo Controller : 產生 UUID placeholder
                        : 注入到 Helm values (main.secret.N8N_API_KEY)
    section Phase 3 - Helm Install
        Helm : 建立 K8s Secret
             : Secret 內容 = UUID placeholder
    section Phase 4 - Sidecar
        MCP Sidecar : 透過 secretKeyRef 讀取
                    : 使用 placeholder 值（尚無法連接 n8n API）
    section Phase 5 - Init
        N8nInitService : 呼叫 n8n API 產生真正的 API Key
                       : kubectl patch Secret = real API key
                       : kubectl rollout restart
    section 重啟後
        新 Pod 啟動 : n8n container 使用 real key
                    : MCP sidecar 從 Secret 讀取 real key
                    : MCP sidecar 可以正常連接 n8n API
```

### 12.2 各層的 API Key 狀態

| 階段 | K8s Secret | n8n DB | Sidecar Env | Odoo helm_values |
|------|-----------|--------|-------------|-----------------|
| Helm install 後 | UUID placeholder | N/A | UUID placeholder | UUID placeholder |
| n8n init 後 | real API key | real API key | UUID placeholder (舊 Pod) | real API key |
| rollout restart 後 | real API key | real API key | real API key (新 Pod) | real API key |

### 12.3 為什麼需要 secretKeyRef？

如果 API Key 是直接寫入 sidecar 的 env 中（plain value），則 n8n init 更新 Secret 後，sidecar 仍然使用舊的 placeholder 值。必須整個 Pod 重建才能載入新值。

使用 `secretKeyRef` 的設計確保：
1. Pod 重啟後，container 自動從 K8s Secret 讀取最新值
2. 不需要再次 patch deployment 的 env
3. `rollout restart` 就足以讓新 key 生效

---

## 13. 刪除流程

```mermaid
sequenceDiagram
    actor User as 使用者
    participant Ctrl as Controller
    participant Client as OperatorClient
    participant Operator as PaaS Operator
    participant Helm as HelmService
    participant K8s as Kubernetes
    participant CF as Cloudflare

    User->>Ctrl: DELETE service
    Ctrl->>Ctrl: state = 'deleting'
    Ctrl->>Client: uninstall_release(ns, name, subdomain)
    Client->>Operator: DELETE /api/releases/{ns}/{name}?subdomain=xxx

    Operator->>Helm: helm uninstall svc-a1b2c3d4
    Helm->>K8s: 刪除 Deployment, Service, Secret 等
    K8s-->>Helm: OK

    Note over Operator: 清理 Cloudflare routes
    Operator->>CF: delete_route(paas-cs-9a8b7c6d-f8e3a1b2)
    CF-->>Operator: OK (n8n route 已刪除)
    Operator->>CF: delete_route(paas-cs-9a8b7c6d-f8e3a1b2-mcp)
    CF-->>Operator: OK (MCP route 已刪除)

    Note over Operator: 清理 MCP K8s Service
    Operator->>K8s: kubectl delete service svc-a1b2c3d4-mcp
    K8s-->>Operator: OK

    Operator-->>Client: OK
    Ctrl->>Ctrl: service.unlink()（刪除 DB 記錄）
    Ctrl-->>User: {success: true}
```

刪除操作清理的資源：

1. **Helm Release**：所有由 Helm 管理的 K8s 資源（Deployment, Service, Secret, PVC 等）
2. **MCP K8s Service**：`{release}-mcp` Service（非 Helm 管理，需額外刪除）
3. **Cloudflare Routes**：主應用 route + MCP sidecar route
4. **Cloudflare DNS**：對應的 CNAME records
5. **Odoo DB 記錄**：CloudService + 關聯的 McpServer（cascade delete）

---

## 14. 關鍵檔案索引

### Odoo 模組

| 檔案 | 說明 |
|------|------|
| `src/controllers/paas.py:1969` | `_create_service()` - 服務建立入口（建 pending + 觸發 deploy cron） |
| `src/models/cloud_service.py:342` | `_cron_process_pending_deploys()` - deploy cron 入口（撈 pending） |
| `src/models/cloud_service.py:369` | `_execute_deploy()` - 實際部署（namespace + helm install + sidecar patch） |
| `src/models/cloud_service.py:744` | `_cron_check_deploying_services()` - 輪詢 transitional 狀態 cron 入口 |
| `src/models/cloud_service.py:771` | `_poll_and_update_status()` - 單筆服務狀態轉換（model context） |
| `src/models/cloud_service.py:673` | `_build_mcp_sidecar_config()` - Sidecar 配置（已從 controller 移至 model） |
| `src/controllers/paas.py:2414` | `_update_service_status()` - 狀態輪詢（request 路徑，與 cron 同邏輯） |
| `src/models/cloud_service.py:4308` | `_run_post_deploy_init()` - Post-deploy init 的單一入口（cron 路徑；#1257 起 controller 不再有 init 流程） |
| `src/controllers/paas.py:2695` | `_compute_sidecar_url_hint()` - MCP URL 建構（原 `_build_mcp_endpoint_url`，UI 預填 helper） |
| `src/models/cloud_service.py:965` | `_model_build_mcp_endpoint_url()` - MCP URL 建構 twin（cron / 非 request caller） |
| `src/services/naming.py:19` | `make_service_subdomain()` / `make_namespace()` - subdomain 與 namespace 命名 |
| `src/data/cloud_service_cron.xml` | deploy / check-deploying 兩個 ir.cron 定義 |
| `src/models/cloud_service.py` | CloudService 資料模型 |
| `src/models/cloud_app_template.py` | CloudAppTemplate 資料模型 |
| `src/models/mcp_server.py` | McpServer 資料模型 + tool 發現 |
| `src/services/paas_operator.py` | PaaS Operator HTTP Client |
| `src/data/cloud_app_templates.xml` | n8n 模板設定（Helm values, MCP config） |

### PaaS Operator

| 檔案 | 說明 |
|------|------|
| `extra/paas-operator/src/main.py` | FastAPI app 入口 + 認證 middleware |
| `extra/paas-operator/src/api/releases.py` | Helm Release CRUD + Sidecar Patch |
| `extra/paas-operator/src/api/init.py` | n8n Init API endpoint |
| `extra/paas-operator/src/services/helm.py` | HelmService + KubernetesService |
| `extra/paas-operator/src/services/n8n_init.py` | N8nInitService（Owner + API Key） |
| `extra/paas-operator/src/services/cloudflare.py` | Cloudflare Tunnel + DNS 管理 |
| `extra/paas-operator/src/models/schemas.py` | Pydantic request/response schemas |
| `extra/paas-operator/src/config.py` | 環境變數設定 |
