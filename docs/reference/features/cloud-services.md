---
title: Cloud Services Specification
category: reference
implementation_status: implemented
---

# Cloud Services Specification

## Overview

Cloud Services 是 WoowTech PaaS 平台的核心功能之一，讓用戶能夠一鍵部署容器化應用程式。用戶可以從 Application Marketplace 選擇預設應用（如 AnythingLLM、n8n、PostgreSQL 等），設定必要參數後即可快速啟動服務。

**部署架構**：使用 Kubernetes + Helm Charts 進行服務編排與部署。

### Service Types

平台提供三種主要服務類型：

| Service Type           | Description                           | Use Case                                  |
| ---------------------- | ------------------------------------- | ----------------------------------------- |
| **Cloud Services**     | Deploy containerized apps via Helm    | AnythingLLM, n8n, PostgreSQL, Redis, etc. |
| **Security Access**    | Zero Trust Tunnels via Podman/HAOS    | Secure remote connections                 |
| **Smart Home Connect** | Home Assistant & Woow App integration | Remote access configuration               |

本文件專注於 **Cloud Services** 的規格設計。

### 共用資源觀測

所有 Cloud Services 的工作區卡片與服務詳情共用資源觀測，顯示 CPU、記憶體與持久磁碟的「使用量／實際生效上限或容量」，每 30 秒批次更新。離開頁面即停止，頁面隱藏時暫停，返回時立即更新。此功能依服務的 Helm namespace/release 與實際 Kubernetes 資源辨識，不需要逐一修改應用範本。

CPU 是最近五分鐘平均使用的核心數（`window_seconds: 300`）；記憶體是 working set；磁碟是 PVC 的檔案系統用量。多副本、sidecar 與同一 release 的資料庫會合計，共用 PVC 只計一次。畫面仍每 30 秒更新，來源樣本超過 120 秒即標示過期；延長 CPU 平均窗口不放寬新鮮度判斷，也不以舊 container／cgroup 的數據補新容器。數值旁保留採樣時間；監控缺值、資料過期或只有部分容器／磁碟取得資料時會明確標示，不將缺值顯示為零，也不計算不完整的使用率。

「申請配置」仍保留原訂閱的資源規格。即時觀測不改寫配額、計費或服務狀態。VM 可顯示 guest CPU／記憶體及磁碟配置；現有監控未提供的 VM guest 使用量與暫存磁碟使用量會標示不可用。Smart Home Connect 與 Security Access 連到使用者端設備，沒有平台代管的 CPU／記憶體／PVC，因此沿用其連線狀態顯示。

前端呼叫 `POST /api/workspaces/<workspace_id>/service-resource-observations`，只傳工作區內的服務 ID，每批最多 100 筆。後端逐筆套用與服務詳情相同的存取權檢查，再按 namespace 批次呼叫 operator；瀏覽器不能指定 namespace、release 或查詢式。詳細採樣設定及部署順序見 [PaaS Operator 資源觀測](../../../extra/paas-operator/README.md#resource-observations)。

---

## Deployment Architecture

### System Architecture

由於 Odoo 運行在 K8s Pod 內，無法直接操作 Helm CLI，因此採用獨立的 **PaaS Operator** 服務架構：

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           K8s Cluster                                   │
│                                                                         │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐          │
│  │   Odoo Pod   │      │ PaaS Operator│      │ User Service │          │
│  │  (Frontend + │─────▶│   (FastAPI)  │─────▶│    Pods      │          │
│  │   Metadata)  │ HTTP │              │ Helm │              │          │
│  └──────────────┘      └──────────────┘      └──────────────┘          │
│         │                     │                                         │
│         ▼                     ▼                                         │
│  ┌──────────────┐      ┌──────────────┐                                │
│  │  PostgreSQL  │      │ ServiceAccount                                │
│  │  (Metadata)  │      │ + RBAC       │                                │
│  └──────────────┘      └──────────────┘                                │
└─────────────────────────────────────────────────────────────────────────┘
```

### PaaS Operator Service

獨立的 Python FastAPI 服務，負責執行所有 Helm 操作：

**技術棧**：

- Python 3.11+
- FastAPI
- Helm CLI (installed in container)
- kubectl (for status checks)

**Deployment**：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: paas-operator
  namespace: paas-system
spec:
  replicas: 1
  template:
    spec:
      serviceAccountName: paas-operator
      containers:
        - name: operator
          image: woowtech/paas-operator:latest
          ports:
            - containerPort: 8000
          env:
            - name: API_KEY
              valueFrom:
                secretKeyRef:
                  name: paas-operator-secrets
                  key: api-key
```

**ServiceAccount RBAC**：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: paas-operator
rules:
  - apiGroups: [""]
    resources:
      [
        "namespaces",
        "secrets",
        "configmaps",
        "services",
        "persistentvolumeclaims",
      ]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

**API Endpoints**：

| Method | Endpoint                                     | Description                 |
| ------ | -------------------------------------------- | --------------------------- |
| POST   | `/api/releases`                              | helm install                |
| GET    | `/api/releases/{namespace}/{name}`           | Get release status          |
| PATCH  | `/api/releases/{namespace}/{name}`           | helm upgrade                |
| DELETE | `/api/releases/{namespace}/{name}`           | helm uninstall              |
| POST   | `/api/releases/{namespace}/{name}/rollback`  | helm rollback               |
| GET    | `/api/releases/{namespace}/{name}/revisions` | helm history                |
| GET    | `/api/releases/{namespace}/{name}/status`    | Get pod/deployment status   |
| GET    | `/api/releases/{namespace}/{name}/public-address` | 對外通道位址回讀（ngrok sidecar：`tcp://` 或 `https://`，見下方「對外通道位址回讀」） |
| GET    | `/api/releases/{namespace}/{name}/mqtt-address` | 舊端點（平台 ≤ 18.0.1.0.205 呼叫）：同一份契約、只回 `tcp://` |
| GET    | `/api/releases/{namespace}/{name}/vpn-node-status` | VPN 節點（charts/tailscale）即時狀態：BackendState、登入入口、serve 轉發結果（見下方「VPN 節點即時狀態」） |
| POST   | `/api/namespaces`                            | Create namespace with quota |

**Request/Response Example**：

```json
// POST /api/releases
{
  "namespace": "paas-ws-123",
  "release_name": "paas-ws-123-anythingllm-01",
  "chart": {
    "repo_url": "https://charts.bitnami.com/bitnami",
    "name": "postgresql",
    "version": "15.5.0"
  },
  "values": {
    "auth": {
      "postgresPassword": "secret123",
      "database": "app_db"
    }
  }
}

// Response
{
  "status": "deployed",
  "revision": 1,
  "namespace": "paas-ws-123",
  "release_name": "paas-ws-123-anythingllm-01"
}
```

**Security**：

- API Key authentication (Odoo ↔ Operator)
- Internal ClusterIP service (不對外暴露)
- RBAC 限制只能操作 `paas-ws-*` namespace
- YAML injection prevention using `yaml.safe_dump()`
- Error message sanitization (防止敏感資訊外洩)
- Fail-fast startup (Helm 不可用時拒絕啟動)
- Configurable CORS origins for cross-origin requests
- Request timeout handling (前端 30 秒超時)
- Optimistic locking for race condition prevention
- Helm values whitelist filtering based on template specs

### Namespace Strategy

每個 Workspace 對應一個 Kubernetes Namespace：

```
Namespace: paas-ws-{workspace_id}
├── Deployment: {release_name}-{app}
├── Service: {release_name}-{app}
├── Ingress: {release_name}-ingress
├── PVC: {release_name}-data
├── Secret: {release_name}-secrets
└── ConfigMap: {release_name}-config
```

### Helm Release Naming Convention

```
Release Name: paas-ws-{workspace_id}-{reference_id}
Example: paas-ws-123-anythingllm-01
```

這樣 Release Name 與 Namespace 有相同的前綴 `paas-ws-{workspace_id}`，方便識別和管理。

### Deployment Flow

```
1. User clicks "Launch Application"
           │
           ▼
2. Odoo API validates input & creates CloudService record (state=pending)
           │
           ▼
3. Odoo calls PaaS Operator API: POST /api/namespaces (if not exists)
           │
           ▼
4. Odoo calls PaaS Operator API: POST /api/releases
           │
           ▼
5. PaaS Operator executes: helm install {release} {chart} -n {namespace}
           │
           ▼
6. Background poll cron（每分鐘）calls PaaS Operator API: GET /api/releases/{ns}/{name}/status
           │
           ▼
7. When ready, the poll updates CloudService (state=running; or state=initializing
   when a post-deploy init is owed → the separate init cron runs it → running)
```

> #1159：由觀測推進的狀態轉移（transition 完成、init 分派、deleting 404→unlink）**只由背景
> 短 poll cron 寫**；list/get 端點純讀，回 DB 的最近觀測（`state`＋`observed_at`＋
> `observation_pending`＋`init_phase`），前端 5 秒輪詢只是讀 DB。新的轉移以持久化 episode
> 等待第一次真 status，並不覆寫 `observed_at`；payload 與 monitor 共用這個 freshness clock。
> post-deploy init 在獨立的 init cron 執行。設計見
> [post-deploy init 執行模型 ADR](../../decisions/refactor/post-deploy-init-execution-model.md)。

> **部署逾時與自動恢復（#489／#1416／#1417）**：`deploying`／`upgrading` 超過門檻（容器 30 分、VM 120 分，
> `ir.config_parameter` 可調）仍未就緒就轉 `error`，同時記下 `transition_timeout_at`。`error_message` 先給租戶一句
> 服務建立者語言的說明（發生什麼、通常不需要他做事、問題排除後會自動恢復），再附「技術細節（給管理員）」：
> 就緒判定的 detail（Pending 的 Pod 與 Running 未就緒分開列，附 operator 報的卡住容器 `waiting` 與最近一筆
> Warning 事件 `last_warning`）＋依原因分型的檢查方向。逾時只代表「等太久」：背景 cron
> `_cron_recover_timed_out_services`（每 5 分）在時間窗內（`woow_paas_platform.timeout_recovery_window_hours`，
> 預設 168 小時）重查有標記、未停權的 error 服務，release 就緒就走同一條 poll 推進（要 init 的進
> `initializing`，否則 `running`），清掉訊息與標記並在 PaaS Platform Monitor 留紀錄。其他來源的 error
> （operator 回 failed、刪除／停權失敗、init 額度出口…）沒有標記，永遠不會被這條自癒碰到。

> **running 之後 Pod 不見（#1647）**：`running` 的服務每 10 分鐘（`_cron_check_running_workloads`；另每日對帳時
> 順帶）向 operator 取 `status?include_workloads=true`。任一 Deployment／StatefulSet 期望 ≥1 個 Pod 而實際 0 個
> （例：ReplicaSet `FailedCreate: exceeded quota`）就寫 `workload_fault_*`、在 PaaS Platform Monitor 通知系統管理員
> （有 `FailedCreate` 證據立即，沒有則要持續 5 分鐘以上），並在 `/woow` 詳情頁與服務卡片顯示固定文案。
> 異常對租戶確認期間，`/woow` 其他原本只看 `state` 的訊號一起讓路：詳情頁 Connection Info 的連線狀態顯示 OFFLINE、
> 工作區頁統計卡把這台算進 Alerts 而不是 Healthy；狀態徽章仍是 Running。
> **不改 `state`／`error_message`**；Pod 恢復或服務離開 `running` 後自動清除。舊 operator 不回報 workloads 時什麼都不寫。
> 觀測欄位**值真的變了才寫**：健康的服務、內容沒變的異常都不寫服務列（`write_date` 不動，不與每日對帳／生命週期
> cron 的長交易互撞），所以 `workload_checked_at` 是「最後一次寫入觀測結果的時間」，不是「最後一次檢查的時間」。
> 10 分鐘 cron 的輪替位置記在 PaaS Platform Monitor 單例的 `workload_check_cursor`（最後處理的 service id；成功、
> 失敗、略過都推進，提前中止後下一趟從中止處之後開始）。
> **#1456 起也看「Pod 都在、卻沒有一顆就緒」**：期望 ≥1、Pod ≥1、就緒 0 的 workload 寫 `oom_killed`（沒就緒的
> workload Pod 上一次是 `OOMKilled`，operator `PodInfo.last_terminated_reason`）／`crash_loop`（容器卡在
> `CrashLoopBackOff`）／`not_ready`（都沒有）。三者都要「首次看到滿 5 分鐘後又看到一次」才確認（不是第一次就確認，
> 見 `services/workload_health.py` 檔頭）；「一顆都沒有」同時存在時沿用 #1647 的代碼。租戶文案沿用一般文案。

### Supported Operations

| Operation | Helm Command               | Description                                      |
| --------- | -------------------------- | ------------------------------------------------ |
| Deploy    | `helm install`             | Create new release                               |
| Upgrade   | `helm upgrade`             | Update release (config changes, version upgrade) |
| Delete    | `helm uninstall`           | Remove release and cleanup resources             |
| Rollback  | `helm rollback {revision}` | Revert to previous revision                      |

> **Note**: 不提供 Start/Stop 操作。若需要暫停服務，請使用 Delete 移除；需要時再重新 Deploy。這樣設計是因為一個 Helm Chart 可能包含多種 workloads（Deployment、StatefulSet、CronJob 等），無法用單一 scale 指令控制。

> **欠費停權／復權（生命週期 cron，#1563）**：停權＝operator `POST …/scale?replicas=0`，成功後再呼叫
> `POST …/route/park` 把服務的對外 hostname（同 hostname 的所有 tunnel 規則）指到共用的停權說明頁
> （paas-app-bundle-cd 的 `paas-suspended-page`，任何 path 都回 503＋說明，不帶租戶資訊）；park 失敗只記
> log，停權照樣成功。復權＝`scale?replicas=1` 後呼叫 `POST …/route/unpark`（operator 用安裝時同一段邏輯
> 重算 service_url）；unpark 失敗寫 `error`＋說明並保留 `pre_suspend_state`，cron 下一輪重試。只做有對外
> 網址的 Cloud Service；Security Access／Smart Home 另議。停權中的 redeploy／reconfigure 走 helm upgrade、
> 不碰 tunnel 規則，網址維持說明頁。

> **付款後立即復權（#1599）與自助續訂（#1611）**：訂閱付清（`account.move._pay_from_wallet`）或轉回
> in_progress 時只寫 `resume_requested`＋`_trigger()`，commit 後由 `ir_cron_resume_paid_cloud_services`
> 走同一段 `_enforce_single(resume_only=True)` 復權。訂閱被取消（欠費）或到期、服務還沒被清除時，owner／admin
> 可在 /woow 自助續訂（服務頁橫幅、帳務總覽「待續訂的服務」；端點 `/api/cloud-services/services/<id>/renew-estimate`、
> `/renew`、`/api/cloud-services/renewals`）：沿用上游續約鏈（`_sh_renew_subscription` → 子訂閱 `sh_subscription_confirm`）、
> 從續訂當天起算新週期（不補繳欠費期間、不給試用）、錢包付清才改綁，復權交給 #1599 的 cron。判準集中在
> `cloud_service._renewal_status()`（`src/models/cloud_service_renewal.py`）；cancel 的保留期與每日 cron 清除共用
> `_cancel_cleanup_date`，每日 cron 停權／清除前也先取同一把列鎖，不與續訂交易交錯。只做 Cloud Service。

### DNS & TLS Architecture (Cloudflare)

> ⚠️ **早期設計、未在生產採用**：以下 Traefik Ingress + wildcard A record（`*.woowtech.io` → K3s Node IP）的架構是早期設計方案，**未在生產環境採用**。現行生產的 DNS/TLS 實際走 **Cloudflare Tunnel**（cloudflared + 每服務 CNAME → `{tunnel_id}.cfargotunnel.com`），不對外暴露 K3s Node IP、也不依賴 Traefik Ingress 入口。權威說明請見 [n8n-cloud-service-lifecycle.md](../architecture/n8n-cloud-service-lifecycle.md)。以下內容保留作早期設計考古。

域名 `*.woowtech.io` 由 Cloudflare 管理，採用 **Cloudflare Proxy** 模式處理 TLS：

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│    User      │      │  Cloudflare  │      │   K3s Node   │      │   Service    │
│   Browser    │─────▶│  Proxy (CDN) │─────▶│   Traefik    │─────▶│    Pod       │
│              │ TLS  │              │ HTTP │              │      │              │
└──────────────┘      └──────────────┘  or  └──────────────┘      └──────────────┘
                                      Origin
                                       TLS
```

**Cloudflare 設定**：

| 設定項目           | 值                            | 說明                            |
| ------------------ | ----------------------------- | ------------------------------- |
| DNS Record         | `*.woowtech.io` → K3s Node IP | Wildcard A record               |
| Proxy Status       | Proxied (Orange cloud)        | 啟用 Cloudflare CDN + DDoS 保護 |
| SSL/TLS Mode       | Full (Strict)                 | Cloudflare ↔ Origin 使用 TLS    |
| Origin Certificate | Cloudflare Origin CA          | 15 年有效期，免費               |

**流量路徑**：

1. User 訪問 `https://my-app.woowtech.io`
2. Cloudflare 終止 TLS，驗證憑證
3. Cloudflare 使用 Origin CA 與 K3s 建立加密連線
4. Traefik 路由到對應的 Service

### Ingress Configuration

使用 K3s 預設的 **Traefik** Ingress Controller：

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: paas-ws-123-anythingllm-01
  namespace: paas-ws-123
  annotations:
    kubernetes.io/ingress.class: traefik
spec:
  tls:
    - hosts:
        - my-ai-assistant.woowtech.io
      secretName: cloudflare-origin-tls # Shared Origin CA cert
  rules:
    - host: my-ai-assistant.woowtech.io
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: paas-ws-123-anythingllm-01
                port:
                  number: 3001
```

**Origin CA Secret（整個 cluster 共用）**：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cloudflare-origin-tls
  namespace: default # 或建立在每個 workspace namespace
type: kubernetes.io/tls
data:
  tls.crt: <base64-encoded-origin-cert>
  tls.key: <base64-encoded-origin-key>
```

> **Note**:
>
> - Cloudflare Origin CA 憑證為 wildcard (`*.woowtech.io`)，可供所有子網域共用
> - K3s 預設使用 Traefik 並開放 port 80/443
> - 如需自訂 Traefik 設定，應建立 HelmChartConfig 於 `/var/lib/rancher/k3s/server/manifests/`

### Resource Quotas

每個 Workspace namespace 有一個名為 `<namespace>-quota` 的 ResourceQuota。數值不是固定的，由平台依 workspace 內的服務計算後交給 operator 套用：

- CPU／記憶體／磁碟：workspace 內所有未刪除服務的 `allocated_vcpu`／`allocated_ram_gb`／`allocated_storage_gb` 加總後乘 3，下限 8 vCPU／8Gi／100Gi（公式在 `src/models/cloud_service.py` 的 `_compute_ns_quota_formula`）。
- Workspace 最低保障（#1679）：系統管理員可分別設定 CPU（cores）、記憶體與磁碟（Gi）的最低保障。實際目標是每項 max(公式, 最低保障)，未設定就沿用公式（`_compute_ns_quota`）。最低保障不是固定上限：公式變大時目標跟著變大。它不改服務的 `allocated_*`、方案或計費。
- `requests` 與 `limits` 是同一個值（operator `extra/paas-operator/src/services/helm.py` 的 `_build_quota_manifest`）。
- ephemeral-storage 依服務數計算（`_compute_ns_ephemeral_quota`）；PVC 數量上限固定為 10。

只有一台 4 vCPU／8GB／60GB 服務的 workspace 會得到：

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: paas-ws-1a2b3c4d-quota
  namespace: paas-ws-1a2b3c4d
spec:
  hard:
    requests.cpu: "12"
    limits.cpu: "12"
    requests.memory: 24Gi
    limits.memory: 24Gi
    requests.storage: 180Gi
    persistentvolumeclaims: "10"     # #1781：max(10, 叢集現值)，見下方
    requests.ephemeral-storage: 4Gi
    limits.ephemeral-storage: 80Gi
```

平台在開通、換方案、刪除服務，以及管理員變更最低保障時重算並推送。所有路徑共用同一個政策（`src/models/workspace_quota_policy.py`、`src/services/ns_quota_policy.py`）：

- 推送前先讀叢集上的受管 quota（operator `GET /api/namespaces/{name}/quota?include_managed=true`），每一項取 max(目標, 叢集現況)，再以 operator 同形的完整 hard（含 `persistentvolumeclaims` 與 ephemeral-storage）逐項檢查，**不會降低叢集任何一項**。
- 目標低於叢集的項目記為「待處理降額」（後台 Pending Quota Decrease），叢集保留較高的值，直到有安全降額的路徑把它收回。
- 安全降額判斷只有一份（#1681，`src/services/ns_quota_policy.py` 的 `plan_safe_lower`）：以完整目標對叢集 hard（spec 與 status 取較大者）找出會降的項，逐項比對 `requests.cpu`、`limits.cpu`、`requests.memory`、`limits.memory`、`requests.storage` 的 used（已佔用的配額，不是即時使用率）。只要有一項降額，5 項 used 都必須讀得到；任何一項 used 高於目標、缺項、看不懂、受管 quota 讀不到或 operator 太舊（沒有 `sync_token`）就一項都不降，只送增額。ephemeral-storage 與 `persistentvolumeclaims` 不在降額範圍。通過檢查也不保證未來 Pod 重建或 rollout 有空間，它不是容量保留，也不與 K8s admission 原子化。
- 管理員主動降低或清除最低保障（#1681）：只有讓政策目標下降的變更才檢查。目標下降但叢集本來就不高於新目標時直接保存；會降低叢集時，Set Quota Minimums 對話框先列出每項「叢集值 → 目標（used）」與風險警示，按「Confirm Decrease and Apply」才保存。確認碼由伺服器簽章，綁定 Workspace、操作者、政策版本、新的最低保障、完整目標與 namespace 集合，10 分鐘內有效；確認時在工作區鎖內再檢查一次用量。讀取失敗、缺項、舊 operator、低於任一 used、或會降低 pvc 都拒絕，不保存。RPC 入口是 `preview_quota_minimums`（預覽與取得確認碼）與 `set_quota_minimums(..., confirmation=...)`，僅限 `base.group_system`。
- 確認保存後記下 `quota_lower_authorization`（政策版本＋目標）。推送失敗時設定保留並照常有限重試，每次重試都重讀 used 重檢；用量變多讓降額不安全時保留叢集額度，狀態「已被安全檢查擋下」、原因 `unsafe_decrease: …`，仍會自動重試。政策版本或目標一變（例如新增服務），這次確認就不再適用；整個工作區同步成功後授權即清除。確認只套用在管理員主動降低／清除，服務生命週期不經過確認。
- 生命週期自動收回（#1682）：刪除服務、清理到期服務、換方案降級，以及之後的背景重試、對帳、6 小時讀回與管理員手動重試，都以同一份判斷（`plan_safe_lower`）免確認降額——每次重讀 used，5 項全部容得下才降到目標，否則一項都不降、只送增額，狀態記為「已被安全檢查擋下」、原因 `unsafe_decrease: <資源> <原因>`，並照有限次重試（用量釋放後就收回）。最低保障（override）不變，目標一律是 max(公式, 最低保障)。quota 讀取或推送失敗不回捲也不阻斷刪除，逾時記為「結果待確認」。免確認只屬於伺服器內部的生命週期與背景入口（底線方法、RPC 呼叫不到），租戶與管理員都不能用 context 或參數把人工降額偽裝成自動事件。管理員只提高（不需確認）或開通留下 CPU／memory／storage 的待處理降額時，標記 `quota_reconcile_pending` 並立刻排背景收回，不必等 6 小時讀回。換方案扣款前的升級預檢照舊（以換完之後的目標檢查）；降級時 quota 在 helm upgrade 之前推送，舊 pod 還佔著用量時這次不降，升級完成、用量釋放後由背景重試收回。
- PVC 上限（#1781，#1189 依 template 計算 PVC 數之前的止血）：operator 回報支援 `pvc_limit`（GET `managed_quota.supports_pvc_limit`）時，平台以 10 為基準、與叢集現值取大送出，PVC 上限高於 10 的工作區不再整筆擋下；叢集高於 10 的部分不算待處理降額，也不會降。operator 不支援時不送這個欄位（舊 operator 會忽略它、照樣寫 10），照舊因 PVC 高於 10 擋下。送了 `pvc_limit` 卻沒在回應拿到相同的值（operator 滾動更新時 PUT 落在舊 pod，PVC 已被寫成 10）時記為「已被安全檢查擋下」、原因 `pvc_limit_not_confirmed`，不自動重試。
- 讀不到現況、資料不完整、或某一項非降不可（例如舊 operator 下叢集的 pvc 上限大於 10）時不推送並記錄原因。開通路徑：讀不到現況或資料不完整時開通失敗待重試，因為無法證明不降額；某一項非降不可時跳過這次 quota 套用（namespace 與基線已存在），開通照常繼續。刪除服務永遠不會因 quota 推送失敗而被擋下。
- 同一個工作區同時只有一筆交易推送 quota（工作區列鎖，所有平台 quota 寫入都經過它：管理員設定、手動重試、背景重試、生命週期重算、開通建立 namespace）。管理員入口會等鎖；背景、生命週期與開通路徑不等，拿不到鎖就不推送也不寫工作區狀態：生命週期不擋刪除，改排背景同步一分鐘後重來（#1680，換方案剛好撞上推送時增額不再遺失）；開通失敗待重試。
- 遠端版本防護（#1680）：每次推送帶新的 sync token 與鎖內讀到的 resourceVersion（受管 quota 尚不存在時改為「只在不存在時建立」），operator 以條件寫入套用，物件被改過就回 409 不覆寫；開通建立 `<ns>-quota` 走同一套。只有叢集上的 token 與平台預期的相符才算已同步：逾時、5xx、斷線先讀回，落地了就算成功，沒落地就記下在途 token，下一次同步以新 token 重推，晚到的舊請求因 resourceVersion 已變而落不下。叢集上出現平台不認得的 token（例如推送成功後 DB 交易回捲）時同樣重推，值仍取 max(目標, 叢集)，不會因此降額，多出來的部分顯示在 Pending Quota Decrease。operator 太舊（GET 沒有 `sync_token`）時退回無防護推送，後台標示「Operator 不支援版本防護」。
- 有限次自動重試（#1680，背景 cron `ir_cron_reconcile_workspace_quota`，每個工作區各自一筆交易）：暫時性失敗（operator 未設定、斷線、5xx、逾時、版本衝突、讀不到叢集）在失敗後 1、5、15、60、240 分鐘各重試一次，共 5 次，之後轉為「重試已用完」，只能由系統管理員按「Retry Quota Sync」手動重試。永久阻擋（會降低某一項，例如舊 operator 下叢集 pvc 上限大於 10；operator 不支援；資料看不懂）不自動重試。重試額度以目標（namespace 集合＋目標值）計：目標改變、管理員保存最低保障、手動重試、或同步成功時重置；背景重試與目標不變的生命週期事件不重置。生命週期與開通只同步自己的 namespace：工作區不只一個 namespace 時，這次子集同步不記目標指紋、不重置也不消耗額度，改以 `quota_reconcile_pending` 排一次全集對帳（#1682，修正 #1680 讓同目標事件重新拿到 5 次重試的問題）。已同步的工作區每 6 小時讀回驗證一次。手動與背景重試都不寫最低保障的異動紀錄；手動重試只在監控 chatter 留下操作者。
- 管理員在後台「Woow PaaS → Workspace Quotas」查看公式、最低保障、目標、最後成功推送值與時間、同步狀態；目標不是叢集現況，叢集現況看同一頁的觀測（#1678）。尚未建立 namespace 時，最低保障先保存，首次開通時套用。

排查與調整方式見 `docs/guides/deployment/troubleshooting.md` 的「Namespace Quota Exceeded」。

---

## User Flows

### Flow 1: First Service Deployment (Empty Workspace)

```
Workspace Detail → Service Selection → Application Marketplace → App Configuration → Launch
```

1. 用戶進入 Workspace Detail Page
2. 顯示 "Get Started: Deploy Your First Service" 引導畫面
3. 用戶選擇 "Cloud Services" 服務卡片
4. 導向 Application Marketplace 頁面
5. 用戶搜尋/篩選並選擇應用程式
6. 進入 App Configuration 頁面
7. 設定應用名稱、subdomain、環境變數等
8. 點擊 "Launch Application" 開始部署

### Flow 2: Add Additional Service (Existing Services)

```
Workspace Dashboard → "+ Add Service" → Application Marketplace → App Configuration → Launch
```

### Flow 3: Manage Running Service

```
Workspace Dashboard → Service Card → Service Detail Page → [Overview | Configuration | Metrics | Backups | Activity Logs]
```

---

## Feature Specifications

### F1: Application Marketplace

**Purpose**: 提供可部署的應用程式目錄

**Functional Requirements**:

- FR1.1: 搜尋應用程式（by name, tags）
- FR1.2: 依分類篩選（All, AI & LLM, Automation, Database, Analytics, DevOps）
- FR1.3: 顯示應用卡片資訊（icon, name, description, tags, monthly_price）
- FR1.4: 點擊「+」按鈕進入配置頁面

**Application Card Data**:

```yaml
- id: string
- name: string
- icon_url: string
- description: string (short, ~100 chars)
- full_description: text
- tags: string[] # e.g., ["AI", "Chatbot"]
- category: enum # AI_LLM, Automation, Database, Analytics, DevOps, Web, Container
- monthly_price: decimal
- helm_chart: HelmChartSpec
- default_port: integer
- required_values: HelmValueSpec[] # Helm values that user must provide
- optional_values: HelmValueSpec[] # Optional Helm values with defaults
- documentation_url: string
- min_resources: ResourceSpec
```

**HelmChartSpec**:

```yaml
- repository: string # e.g., "https://charts.bitnami.com/bitnami"
- chart_name: string # e.g., "postgresql"
- chart_version: string # e.g., "12.5.8"
- default_values: object # Base values.yaml overrides
```

**HelmValueSpec**:

```yaml
- key: string # Helm value path, e.g., "auth.postgresPassword"
- label: string # UI display label
- type: enum # text, password, number, boolean, select
- default_value: any?
- placeholder: string?
- help_text: string?
- required: boolean
- options: string[]? # For select type
- format: enum? # git_hosts, git_name, email, domain — 純文字欄位的形態驗證（存檔時擋下）＋部署組裝時轉成 chart 形態
                #（git_hosts：逗號清單 → https URL list；domain：FQDN 正規化成小寫，不收 scheme／路徑／埠／IP）；
                # 空值一律放行（選填）；helm_values 保留租戶原文。見 cloud_app_template.SPEC_FORMAT_PARSERS
```

**ResourceSpec**:

```yaml
- vcpu: integer # e.g., 2
- ram_gb: decimal # e.g., 4
- storage_gb: integer # e.g., 10
```

---

### F2: App Configuration & Launch

**Purpose**: 設定應用程式參數並啟動部署

**Configuration Sections**:

1. **General Settings**
   - Application Name (required, 可修改)
   - Reference ID (auto-generated, **建立後不可修改**，用於 Helm Release 命名)

2. **Network & Domain**
   - Subdomain (e.g., `my-ai-assistant.woowtech.io`)
   - ~~Private Network toggle (restrict to VPN only)~~ _(Future Phase)_

3. **Helm Values** (dynamic based on app template)
   - Required values (user must provide)
   - Optional values (Advanced toggle, with defaults)

4. **Resource Allocation** (future phase)
   - Instance Type selection
   - Storage allocation

**Deployment Note**:

> Initial setup may take up to 5 minutes. Your instance will be available at your custom subdomain.

**Actions**:

- Cancel / Discard
- Launch Application

**Estimated Monthly Cost**: 顯示於頁面底部

---

### F3: Service Detail Page

**Purpose**: 管理已部署的服務實例

**Header Section**:

- App Icon + Name (e.g., "AnythingLLM_221")
- Status Badge (Running, Error, Deploying, Upgrading, Initializing)
  - Initializing 依 `init_phase` 細分：Queued／Waiting for app／Retrying（#1159 Q40）
  - 轉移中且背景最近一次觀測超過 3 分鐘時顯示「Last updated X min ago」
- Deployment ID
- Quick Actions:
  - "Open Web UI" button
  - Rollback button (dropdown with revision history)
  - Delete button

**Tab Navigation**:

#### Tab 3.1: Overview

- **Connection**
  - Status (ONLINE/OFFLINE)
  - Public URL
  - Custom Domain (with Edit)
  - Internal Port
- **Resources**
  - CPU Usage (percentage + status)
  - RAM Usage (current / allocated)
  - Helm Release info (chart version, revision)
- **Live Traffic** chart (last 1 hour)
- **Support** links
- **Environment** info (Region, Instance Type)

#### Tab 3.2: Configuration

- General Settings (name 可改, reference ID 唯讀)
- Network & Domain (subdomain 唯讀, custom domain 可改)
- Helm Values (view/edit, 修改後觸發 `helm upgrade`)
- Resource Allocation

#### Tab 3.3: Metrics

- **Performance Overview** (time range selector: Last Hour, 24h, 7d, 30d)
- vCPU Usage chart
- RAM Usage chart
- Disk I/O chart
- Network chart (IN/OUT)
- Active Connections count

#### Tab 3.4: Backups _(Future Phase)_

#### Tab 3.5: Activity Logs _(Future Phase)_

---

### F4: Service Actions

**4.1 Upgrade Service**

- `error` 狀態只有 init 額度出口（`init_reconfigure_allowed`）可以改設定並重新 init；
  helm 失敗、等待逾時、fast-fail 的 error 不開放（#1159 Q30）
- 背景 upgrade（修改配置／換方案）沒有確認套用時（operator 連線中斷、逾時、503 `operator_busy`、
  其他 operator 錯誤）轉 `error`、記下 `upgrade_failed_at`，`error_message` 用服務建立者的語言說明
  「新設定有沒有套用、下一步」；這種 error 可以直接重新送出修改配置（`upgrade_retry_allowed`，#1552）。
  背景 poll 先一步把服務推回 `running` 的情形一樣記錯（只認同一次升級：`last_upgraded_at` 沒被更新），
  不再「DB 是新值、畫面執行中、叢集沒套用」。`upgrade_failed_at` 與逾時標記同一套生命週期（改寫
  `error_message` 或離開 error 就清掉，停權除外），只有平台寫得進去。
- 更新 Helm values（環境變數、資源配置等）
- 升級 Chart 版本
- 自動建立新的 Helm revision

**4.2 Delete Service**

- Confirmation modal required
- `initializing` 期間也可刪除；確認框註明已送出的初始化仍可能在叢集內完成（#1159 Q30）
- Option to keep/delete backups (PVCs)
- Cleanup: `helm uninstall`, 移除 Ingress rules

**4.3 Rollback Service**

- 選擇要回滾的 revision
- 執行 `helm rollback`
- 顯示 revision history

**4.4 Edit Custom Domain**

- Modal with domain input
- 用戶需自行在其 DNS 設定 CNAME 指向 `{subdomain}.woowtech.io`
- 或直接使用平台提供的 `*.woowtech.io` 子網域（已含 TLS）

---

## Data Models

### CloudAppTemplate (Application Catalog)

```python
class CloudAppTemplate(models.Model):
    _name = 'woow_paas_platform.cloud_app_template'
    _description = 'Cloud Application Template'

    # Basic Info
    name = fields.Char(required=True)
    slug = fields.Char(index=True)
    icon = fields.Binary()
    description = fields.Char()  # Short description (~100 chars)
    full_description = fields.Text()
    category = fields.Selection([
        ('ai_llm', 'AI & LLM'),
        ('automation', 'Automation'),
        ('database', 'Database'),
        ('analytics', 'Analytics'),
        ('devops', 'DevOps'),
        ('web', 'Web'),
        ('container', 'Container'),
    ])
    tags = fields.Char()  # JSON array, e.g., '["AI", "Chatbot"]'
    monthly_price = fields.Float()
    documentation_url = fields.Char()

    # Helm Chart Configuration
    helm_repo_url = fields.Char(required=True)  # e.g., "https://charts.bitnami.com/bitnami"
    helm_chart_name = fields.Char(required=True)  # e.g., "postgresql"
    helm_chart_version = fields.Char(required=True)  # e.g., "12.5.8"
    helm_default_values = fields.Text()  # JSON: base values.yaml overrides
    helm_value_specs = fields.Text()  # JSON: schema for user-configurable values

    # Service Configuration
    default_port = fields.Integer(default=80)
    ingress_enabled = fields.Boolean(default=True)

    # Resource Requirements
    min_vcpu = fields.Integer(default=1)
    min_ram_gb = fields.Float(default=1)
    min_storage_gb = fields.Integer(default=5)

    is_active = fields.Boolean(default=True)
```

**helm_value_specs JSON Schema Example**:

```json
{
  "required": [
    {
      "key": "auth.postgresPassword",
      "label": "PostgreSQL Password",
      "type": "password",
      "required": true,
      "help_text": "Password for the postgres admin user"
    }
  ],
  "optional": [
    {
      "key": "primary.persistence.size",
      "label": "Storage Size",
      "type": "select",
      "default_value": "8Gi",
      "options": ["8Gi", "16Gi", "32Gi", "64Gi"]
    }
  ]
}
```

`required`／`optional` 的 `key` 同時是租戶 helm values 的**白名單**（`CloudAppTemplate._filter_allowed_helm_values`；開通時被拒的鍵靜默丟棄、儲存設定時整筆拒絕）。比對單位是租戶送來的頂層鍵（扁平 dot-path）對 `key` 字串的精確比對。**沒有任何 `key`（欄位空白、`{}`、`{"required": [], "optional": []}`）＝租戶什麼都不能設**（#1488 起 fail-closed；之前是全部放行）——需要租戶參數的範本必須明列 key。

### CloudService (Deployed Instance)

```python
class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _description = 'Cloud Service Instance'

    _sql_constraints = [
        ('unique_subdomain', 'UNIQUE(subdomain)',
         'Subdomain must be unique across all services.'),
        ('unique_reference_id', 'UNIQUE(reference_id)',
         'Reference ID must be unique.'),
    ]

    # Relationships
    workspace_id = fields.Many2one('woow_paas_platform.workspace', required=True, ondelete='cascade')
    template_id = fields.Many2one('woow_paas_platform.cloud_app_template', required=True)

    # Identity
    name = fields.Char(required=True)  # Display name, can be changed
    reference_id = fields.Char(index=True, readonly=True)  # Immutable after creation, used in Helm release name
    deployment_id = fields.Char()  # Auto-generated sequence, e.g., "#8291"

    # State
    state = fields.Selection([
        ('pending', 'Pending'),
        ('deploying', 'Deploying'),
        ('running', 'Running'),
        ('error', 'Error'),
        ('upgrading', 'Upgrading'),
        ('deleting', 'Deleting'),
    ], default='pending')
    error_message = fields.Text()  # Error details when state='error'

    # Network
    subdomain = fields.Char()  # e.g., "my-app" → my-app.woowtech.io
    custom_domain = fields.Char()  # Optional custom domain
    internal_port = fields.Integer()
    # is_private_network = fields.Boolean(default=False)  # Future Phase

    # Helm Release Info
    helm_release_name = fields.Char()  # K8s release name, e.g., "paas-ws-123-myapp"
    helm_namespace = fields.Char()  # K8s namespace, e.g., "paas-ws-123"
    helm_values = fields.Text()  # JSON: merged values used for deployment
    helm_revision = fields.Integer(default=1)  # Current Helm revision number
    helm_chart_version = fields.Char()  # Deployed chart version

    # Resources
    allocated_vcpu = fields.Integer()
    allocated_ram_gb = fields.Float()
    allocated_storage_gb = fields.Integer()

    # Timestamps
    deployed_at = fields.Datetime()
    last_upgraded_at = fields.Datetime()
```

#### Plan 規格 overlay（cs_\*，#332）

> 2026-07 補錄；機制出自 Wave A A6，來源：
> `docs/archived/superpowers/plans/2026-07-12-wave-a-reseed-mechanisms.md`。

`sh.subscription.plan` 上有四個規格欄位 `cs_vcpu` / `cs_ram_gb` /
`cs_storage_gb` / `cs_odoo_workers`（定義在
`src/models/sh_subscription_plan_cs_spec.py:14`）。建立服務時，
`_create_service` 組完 `merged_values` 後呼叫
`CloudService._apply_plan_spec_overlay`（`src/controllers/paas.py:2314`、
實作 `src/models/cloud_service.py:1547`）把選定 plan 的規格疊進 helm values：

- `cs_vcpu` → `resources.{requests,limits}.cpu`
- `cs_ram_gb` → `resources.{requests,limits}.memory`（整數 GB 用 `Gi`、否則換算 `Mi`）
- `cs_storage_gb` → `persistence.size`
- `cs_odoo_workers` → woow-odoo chart 的 template（#1130 起的 `cloud_app_odoo`）
  寫 typed `conf.workers`，並依 `cs_ram_gb` 換算 `conf.limitMemoryHard`
  （0.85／worker）與 `conf.limitMemorySoft`（0.70／worker）；values 沒有 `conf`
  區塊時 raise（fail-loud）。Bitnami 形的 template 仍改寫 `customPostInitScripts`
  內的 `workers = 0`（同一組公式）

**行為邊界**：

- **cs_\* 為 0 的維度一律不碰**＝與 template 預設 bit-for-bit 相同；
  刻意不改 template seed（避開 sticky noupdate），見
  `src/models/cloud_service.py:1550` docstring。
- **（Bitnami 形）workers 替換是精確字串 replace、非正則**：逐 marker 斷言全部 script
  合計恰 1 處，count != 1 直接 raise（fail-loud，不靜默 no-op），見
  `src/models/cloud_service.py:1585-1591`。
- **上方 `allocated_*` 欄位改讀選定 plan 規格**，template `min_*` 退為
  fallback（`_allocated_vals_from`，`src/models/cloud_service.py:1541-1543`：
  `plan.cs_vcpu or template.min_vcpu` 依此類推）。
- **既有服務也套目前方案（#1158，Closes #342）**：`_assemble_deploy_values` 在
  `helm_values` 之後疊**目前訂閱方案**的 overlay（`existing=True`：儲存只擴不縮、
  不做 Bitnami 字串替換），所以 deploy／redeploy／reconfigure 都收斂到目前方案；
  reset 密碼那條（`--atomic` 15 秒）傳 `apply_plan=False` 不疊（規格改 pod template，
  會撞 F-461-2）。
- **換方案立即生效（#1158）**：`api_service_change_plan` 在第一個寫入（退點）之前算出
  新 values（`_plan_values_for`）；values 有變才觸發 upgrade，並先擋「非 running／
  suspended」「init lease 在途」「Bitnami 存量」「operator 未設定」「overlay
  fail-loud」。帳務完成後同一交易寫 `helm_values`＋`allocated_*`（營運手調過的維度
  保留，`_plan_change_allocated_vals`）、push ns quota，再走 reconfigure 同一支
  `_start_background_upgrade`（釘現行 chart、`reset_values=True`、不 refresh image；
  停權服務 upgrade 後 scale 回 0）。背景失敗不回捲帳務：服務翻 `error` 並寫
  「方案已變更，但資源調整失敗」，之後任何一次成功的 redeploy／reconfigure 收斂。
- **扣款前擴容預檢（#1513）**：規格有變時，退點之前以真正 upgrade 的同一組參數呼叫 operator
  `POST /api/releases/{ns}/{name}/upgrade-preflight`（`_change_plan_preflight_denial`）。operator 對每顆
  要變大的 PVC 先看 Longhorn volume 的 replica 排滿沒、再 `kubectl patch --dry-run=server` 交給 Longhorn
  validator 判斷；擴不了回 409（`storage_expansion_rejected`）⇒ 整筆擋下、不扣款，並開 activity 告警
  系統管理員（`platform_monitor._report_storage_expansion_rejection`）。5xx／連不上／逾時也擋；只有舊版
  operator 的 404／405 放行。
- **儲存只擴不縮（暫定，討論票 #1512）**：PVC 不能縮，降級時保留目前
  `persistence.size`，`allocated_storage_gb` 記實際大小。規則只在
  `CloudService._plan_storage_size` 一處。

template（產品形狀）與 plan（規格檔位）的屬性歸屬邊界見
`../../decisions/patterns/template-vs-plan-property-boundary.md`。

#### 對外通道位址回讀（ngrok sidecar；EMQX、Headscale）

平台的 Cloudflare 通道只載 HTTP/WS（而且會剝掉 Tailscale 控制協議的 `Upgrade`
header），所以 EMQX 的原生 MQTT（TCP）與 Headscale 的控制面（HTTPS）走租戶自己的
ngrok agent（chart 的 sidecar）。agent 實際拿到的位址只有它自己知道，平台從它的
container log 讀回來顯示在服務頁。

- **能力旗標**：`cloud_app_template.has_public_tunnel_address`（唯一事實源；
  18.0.1.0.205 以前叫 `has_public_mqtt_address`）。前端不得用 slug 判（repo-lint 第 7 條）。
- **顯示標籤**：`cloud_app_template.public_tunnel_label`（範本 record 的資料值：seed 直接寫
  中文原文、不可翻譯、不進 `.po`，見 `docs/decisions/patterns/i18n-scope-data-values-vs-code-strings.md`；
  例 EMQX「對外 MQTT 位址」）；空值時前端用程式碼字串 `_t("Public address")`（走 `.po`，「對外位址」）。
- **帳本**：`cloud_service.public_tunnel_url`（唯讀；18.0.1.0.205 以前叫
  `mqtt_public_url`，升級時 `CloudService.init()` 把舊值搬過來）。轉移狀態 poll 每分鐘、
  running 服務由每日對帳 cron 回寫；三態（沒探／探過沒有／位址）見
  `_probe_public_tunnel_address`。
- **payload**（service detail）：`has_public_tunnel_address`、`public_tunnel_label`、
  `public_tunnel_url`。
- **chart 端 log 契約**（operator `GET …/public-address` 讀）：sidecar 容器名 `ngrok`；
  每 60 秒在**行首**印一行 `WOOW_PUBLIC_URL=<url>`，`<url>` 是 `https://<host>[:<port>]`
  或 `tcp://<host>:<port>`；沒有位址時印 `WOOW_PUBLIC_URL=`（空值＝撤回）。舊 marker
  `WOOW_MQTT_PUBLIC_URL=` 繼續認。取最後一筆有效值；格式不符的行丟掉（不算撤回）；
  marker 不在行首不認（ngrok 自己的 log 可能帶訪客可控字串）。
- **部署順序**：operator 先上（新端點），平台後上；平台遇到舊 operator 的 404 會退回
  `/mqtt-address`（只有 `tcp://`）。

#### VPN 節點即時狀態（INFRA-FIX-005）

pod Ready ≠ 節點在線：charts/tailscale 的 readiness 把 `NeedsLogin` 也當可用（讓「用 auth key 登入中」的第二次滾動等得到登入完成），
所以節點被控制面到期／刪除後，平台 `state` 仍是 `running`、/woow 仍顯示 ONLINE（INFRA-TS-005／TS-007）。另外 `tailscale.serve` 被
entrypoint 略過的項目只印在容器 stderr，Service 不存在的項目不會被略過（建成指向 NXDOMAIN 的轉發）。

- **能力 gate**：`cloud_app_template.has_vpn_node_status`（唯一事實源；seed 只有 tailscale 為 True）→ `CloudService._chart_has_vpn_node_status()`
  → service detail payload 的 `has_vpn_node_status` → 前端卡片。前端不得用 slug 判斷。
- **不進帳本**：沒有欄位、沒有 cron、不碰 `state`。租戶打開服務頁（Overview，running／upgrading）時，前端每 30 秒（頁面可見才輪詢）
  `POST /api/workspaces/<ws>/services/<id>/node-status`，後端 → operator `GET /api/releases/{ns}/{release}/vpn-node-status`（唯讀）。
  授權與 detail 同一條；`available:false` 是正常降級（不是 running、operator 沒設定／太舊／沒回應），不顯示錯誤。
- **operator 做什麼**：對 `tailscale` 容器 exec 一支**固定**的 sh 腳本（沒有任何插值）：`tailscale status --json`、`tailscale serve status --json`，
  以及 **entrypoint 自己的 `parse_serve`**（`WOOW_TS_SOURCE_ONLY=1` source `/entrypoint.sh`；source 前先 `grep` 確認該旗標存在，舊映像的 entrypoint
  沒有它就退回「不知道期望清單」，不會整支跑起來）。再 `kubectl get services` 對帳轉發目標是否存在。
- **回應是封閉集合**：`probe`（ok／no_pod／pod_not_running／exec_failed／no_output）、`backend_state`、`login.kind`（none／external_url／cluster_internal）、
  轉發 `status`（applied／not_applied／service_missing／mismatch／unverified）、略過原因代碼（bad_format／port_range／other_namespace／not_service_name／
  duplicate_port／too_many／namespace_unknown／other）。**永不回原始 log／stderr**；叢集內部的登入網址不回，只回 `/register/<id>` 的註冊碼。
  平台 `services/vpn_node_status.public_report` 再驗一次（不信任形狀），並把登入入口（外部登入網址、註冊碼）只給 owner／admin。
- **UI**（Overview「VPN 節點狀態」卡）：節點狀態（已連線／需要重新登入／等待核可／未連線…）、需要登入時的說明（工作區 Headscale 用 Headplane 的 pre-auth key，
  因為它的登入頁是瀏覽器打不開的內部位址）、每個轉發有沒有生效，以及被略過的項目與原因。節點需要登入／等待核可／離線／停止時，Connection Info 的
  狀態也不再寫 ONLINE。
- **登入失敗原因與被忽略的 key（INFRA-TS-005-5）**：operator 另回 `login_error`（authkey_expired／authkey_used／authkey_invalid／
  control_unreachable／other；從 `tailscale status --json` 的 `Health` 裡「The last login error was: …」對應，原文不回；節點 Running 時一律 null）
  與 `authkey_configured`（腳本只判斷 `TS_AUTHKEY_FILE` 有沒有非空白內容，回 set／unset，不讀出 key）。平台轉成 `login_error` 與
  `authkey_ignored`（已登入＋設定裡有 key ⇒ 提示「這把 key 不會被使用」，對應 entrypoint 的 "not using it"）。
- **節流**：`/node-status` 同一台服務 10 秒內共用上一份 operator 回應（每個 worker 一份表；operator 失敗不快取；角色過濾逐次重做）。
- **存檔前驗證（INFRA-TS-005-5／TS-007-1）**：helm_value_specs 的 `validate`（只在存檔時跑、不碰部署組裝，所以收緊規則不會讓既有服務
  Redeploy 時少掉設定；與 `format` 不同）。`tailscale_serve`：與 entrypoint `parse_serve` 同一套規則（格式、埠 1–65535、只接受同工作區
  namespace 的 Service、DNS label、tailnet 埠不重複、最多 32 筆）；`tailscale_login`：控制平面與 key 種類（`tskey-` 只配官方 Tailscale）、
  「自訂網址」必填且是 http(s) URL、key 不得含空白。登入三欄都沒動時不重驗已存的組合。權威在 `services/tailscale_settings.py`，
  前端 `utils/tailscale_settings.js` 是即時提示的鏡像。Service 是否存在不在存檔時判斷（租戶可能先設轉發再開服務），由卡片的
  `service_missing` 回報。
- **部署順序**：operator 先上（新端點），平台後上；舊 operator 回 404 → 卡片不顯示內容（`available:false`），其餘不受影響。
- **範圍外**：「填新 key 後 tailscaled 進入 crash loop」（INFRA-TS-005-1）與「升級中鎖住所有動作」（-2）是 chart／entrypoint 與 operator rollout 逾時的問題，
  不在這裡。

#### MCP 尚未設定連線的提示（2026-10-27）

pod 在、管理頁在、`/healthz` 也是 200 ≠ MCP 在提供工具：新開的 mcp-server 家族服務（第三輪 staging 實測 Nextcloud）在管理頁 → Connection
存好之前，`initialize`／`tools/list` 一律 503（子行程缺連線設定 exit 2），平台 `state` 卻是 `running`、/woow 顯示「運作中」。

- **狀態來源**：woow-mcp-server（mcp-admin-core）的 `/healthz` 多回 `mcp`＝`running`｜`not_configured`｜`not_running`（字串、封閉集合；
  舊映像沒有這個欄位，只回 `{"status":"ok"}`）。第五輪起宣告了後端連線欄位的產品（odoo、litellm、hermes、n8n 等，以 woow-mcp-server
  的 `LaunchPolicy.backend_connection` 為準）另回 `backend`＝`configured`｜`missing`（沒有後端連線也起得來、列得出工具，`mcp` 是
  `running`，但用到後端的工具會失敗；其他產品與舊映像沒有這個欄位）。兩個欄位各算各的：`mcp` 不是 `running` 時以 `mcp` 的提示為主。
- **能力 gate**：`CloudService._chart_has_mcp_setup_status()`＝`_admin_page_public_url()` 非空（has_admin_gui＋`admin_password_carrier=config_json`
  ＋有 subdomain，恰好是 mcp-server chart 家族）→ service detail payload 與開通回應的 `has_mcp_setup_status` → 服務頁／一次性憑證視窗。
  前端不得用 slug 判斷。
- **不進帳本**：沒有欄位、沒有 cron、不碰 `state`（服務是正常的，只是要設定；徽章照樣 Running）。租戶打開服務頁（服務 running）時，服務頁
  （一頁一個 poller，頁首提示與 Overview 的 Connection Info 共用）每 30 秒（頁面可見才輪詢；提示顯示中改為每 10 秒；切回分頁、或提示顯示中
  視窗重新取得焦點時立刻再問）`POST /api/workspaces/<ws>/services/<id>/mcp-status`；後端（`CloudService._mcp_setup_status`）對**該服務自己的對外根位址**
  （`_admin_page_public_url()`，平台算出、不是使用者輸入；呼叫端多送任何參數整個拒絕）`GET /healthz`：自訂 User-Agent、不跟隨轉址、
  body 最多 4 KiB、只解析 `mcp` 與 `backend`。網址形狀只收 `https://<一個 label>.<paas_domain>`（縱深防禦）。授權與 detail 同一條
  （工作區成員皆可讀）；回應只有 `state`、`backend`、`admin_url`。
- **時限**：呼叫端最多等 3 秒（含排隊）；探測本身連上之後由看門狗在 3 秒總時限到時切斷 socket，慢速逐 byte 的 header／body 拖不住執行緒
  （`http.client`，TLS 照常驗憑證；涵蓋不到的只有 DNS）。同一網址同時只探測一次（後到的請求等同一個），探測完成時結果照樣寫進快取。
- **降級**：不是 running、讀不到、逾時、轉址（例：開了 Cloudflare Access）、舊映像沒有欄位，一律回 `state: unknown`（正常降級，不顯示）。
- **快取**（每個 worker 一份表）：要租戶動手的結果（`not_configured`、`not_running`、`running`＋`backend=missing`）只留 2 秒——它們會因為
  租戶在管理頁按下 Save 而改變，存好連線、切回分頁就看得到新狀態；`running`、`unknown` 留 10 秒（讀不到的那台不會讓每次輪詢都付滿 3 秒）。
- **網路路徑**：走公開網址（Cloudflare tunnel）而不是叢集內：租戶 namespace 的 ingress 只收 tunnel、同 namespace 與 reach 標籤的工作區
  namespace，平台 namespace 不在其中；平台 egress 對公網放行（staging 有 egress policy、私網段排除；正式站沒有 egress policy）。
- **UI**（文案依 `viewer_can_manage` 分兩種說法：owner／admin 叫他到管理頁設定並附管理頁網址；guest、Portal 的 user 拿不到管理密碼，
  請他找工作區擁有者或管理員，不附連結）：
  - `not_configured` → 黃底「MCP 尚未設定連線：請到管理頁 → Connection 填好並儲存，MCP 才會開始提供工具。」；Connection Info 狀態「待設定」。
  - `not_running` → 黃底「MCP 服務程式沒有在運作，請到管理頁查看 Logs。」；Connection Info 狀態「MCP 未運作」。
  - `running`＋`backend=missing` → 藍底（較輕）「尚未設定後端連線，需要後端的工具無法使用：請到管理頁 → Connection 設定。」；
    Connection Info 狀態「後端未設定」。
  - `running`（`backend` 是 configured 或沒有欄位）、`unknown` → 不顯示，Connection Info 照舊（ONLINE）。workload_fault、VPN 節點等更嚴重的
    狀況仍優先顯示 OFFLINE。
  - 開通 mcp-server 家族時的一次性憑證視窗（#1542）多一句「請先到管理頁 → Connection 存好連線，MCP 才會開始提供工具。」（只在開通；
    Reset 不顯示）。
  元件 `components/mcp-setup-status/McpSetupStatusNotice`（純呈現），呈現邏輯 `utils/mcp_setup_status.js`，輪詢
  `services/mcp_setup_status.js`（ServiceDetailPage 呼叫），後端 `services/mcp_setup_status.py`。
- **部署順序**：兩邊獨立。平台先上：舊映像沒有 `mcp`／`backend` 欄位 ⇒ 全部 `unknown`、不顯示，與現況相同；woow-mcp-server 先上：
  平台不讀，與現況相同。

#### 一次性憑證視窗的 gateway 提示（P3）

有 gateway 的模板（`helm_default_values.gateway.externalPath`，#530）在建立與 Reset
時一次性交付 `gateway_api_key`＋`gateway_url`。兩格的說明文字依範本：
`cloud_app_template.gateway_api_key_hint`／`gateway_url_hint`（資料值，seed 直接寫中文原文、不進 `.po`），有值時隨
`_gateway_credentials` 帶出（同名 payload 鍵）；空值時前端沿用 hermes-agent 的內建
文案（MCP 連線設定的 Gateway API Server 區）。只是顯示字，交付判準不變。

### CloudServiceBackup _(Future Phase)_

### CloudServiceLog _(Future Phase)_

---

## Agent Context 揭露白名單

task agent chat 會把綁定 cloud service 的資訊注入 AI system context
（spec `docs/plans/2026-Q2/2026-06-04-agent-context-enrichment-design.md`）。

**基礎集（永遠注入）**：`template.name` / `template.category` /
`template.description`、`service.name` / `state` / 外部 URL
（`subdomain` + `paas_domain`）。

**Configuration（fail-closed opt-in）**：`cloud_app_template.agent_context_keys`
（JSON list，dot-notation key paths）指定可揭露的 `helm_values` 子集；未設定
即完全不注入。取出子集仍過 `_sanitize_helm_values` 第二層遮罩。

**永不注入**：`helm_values`（全文）、`error_message`、`helm_namespace`、
`helm_release_name`、`mcp_auth_token`、`n8n_api_key`、`n8n_owner_password`、
`n8n_owner_email`、`deploy_params`。

---

## API Design

> **Implementation Note**: 實際實作使用 Odoo JSON-RPC (POST + `action` 參數) 而非 RESTful endpoints。
> 所有請求皆為 `POST` 方法，透過 `action` 參數區分操作。以下為實際端點。

### Endpoints (Implemented)

#### Application Templates

```
POST /api/cloud/templates          # action 參數不適用，直接 list
     Body: { category, search }

POST /api/cloud/templates/<id>     # 取得單一 template
```

#### Cloud Services

```
POST /api/workspaces/<workspace_id>/services
     Body: { action: "list" | "create", template_id, name, values }

POST /api/workspaces/<workspace_id>/services/<service_id>
     Body: { action: "get" | "update" | "delete", values, version }

POST /api/workspaces/<workspace_id>/services/<service_id>/rollback
     Body: { revision }

POST /api/workspaces/<workspace_id>/services/<service_id>/revisions
     # 取得 revision history
```

#### Metrics _(Future Phase)_

#### Backups _(Future Phase)_

---

## UI Components

### New Pages

| Page                 | Route                                             | Description                  |
| -------------------- | ------------------------------------------------- | ---------------------------- |
| ServiceSelectionPage | `#/workspaces/:id/services/new`                   | 服務類型選擇（首次部署引導） |
| AppMarketplacePage   | `#/workspaces/:id/services/marketplace`           | 應用程式市集                 |
| AppConfigurationPage | `#/workspaces/:id/services/configure/:templateId` | 應用配置頁面                 |
| ServiceDetailPage    | `#/workspaces/:id/services/:serviceId`            | 服務詳情（含五個 Tab）       |

### New Components

| Component      | Location                                   | Description                         |
| -------------- | ------------------------------------------ | ----------------------------------- |
| ServiceCard    | `components/service/ServiceCard.js`        | 服務卡片（顯示於 Dashboard）        |
| AppCard        | `components/marketplace/AppCard.js`        | 應用程式卡片（市集用）              |
| CategoryFilter | `components/marketplace/CategoryFilter.js` | 分類篩選器                          |
| EnvVarForm     | `components/config/EnvVarForm.js`          | 動態環境變數表單                    |
| MetricsChart   | `components/metrics/MetricsChart.js`       | 效能指標圖表                        |
| ~~LogViewer~~  | ~~`components/logs/LogViewer.js`~~         | ~~即時日誌查看器~~ _(Future Phase)_ |
| ~~BackupList~~ | ~~`components/backup/BackupList.js`~~      | ~~備份列表~~ _(Future Phase)_       |
| StatusBadge    | `components/common/StatusBadge.js`         | 狀態標籤                            |

### New Modals

| Modal                  | Description                       |
| ---------------------- | --------------------------------- |
| EditDomainModal        | 編輯自訂網域                      |
| DeleteServiceModal     | 確認刪除服務                      |
| ~~CreateBackupModal~~  | ~~建立備份~~ _(Future Phase)_     |
| ~~RestoreBackupModal~~ | ~~還原備份確認~~ _(Future Phase)_ |

---

## Implementation Phases

### Phase 0: PaaS Operator Service ✅

- [x] FastAPI project setup
- [x] Helm CLI integration (subprocess)
- [x] API endpoints: releases CRUD, namespaces
- [x] API Key authentication
- [x] Dockerfile + K8s manifests (Deployment, Service, RBAC)
- [x] Health check endpoint

### Phase 1: Foundation (Odoo) ✅

- [x] CloudAppTemplate model + seed data
- [x] CloudService model
- [x] PaaS Operator client service (HTTP calls)
- [x] Basic CRUD APIs

### Phase 2: Marketplace UI

- [ ] AppMarketplacePage
- [ ] AppCard component
- [ ] Category filter
- [ ] Search functionality

### Phase 3: Configuration & Launch

- [ ] AppConfigurationPage
- [ ] EnvVarForm component
- [x] Service creation API
- [x] Subdomain validation

### Phase 4: Service Management

- [ ] ServiceDetailPage (Overview tab)
- [x] Service upgrade/delete/rollback APIs
- [x] Status polling

### Phase 5: Metrics _(Future Phase)_

- [ ] Metrics tab + charts

### Phase 6: Activity Logs _(Future Phase)_

- [ ] Activity Logs tab
- [ ] Real-time log streaming

### Phase 7: Backups _(Future Phase)_

- [ ] Backup model
- [ ] Backup APIs
- [ ] Backups tab UI

---

## Non-Functional Requirements

### Performance

- Marketplace page load < 500ms
- Metrics chart update interval: 5s
- Log streaming latency < 1s

### Security

- Environment variables encrypted at rest
- HTTPS required for all public URLs
- Role-based access (workspace member roles)

### Scalability

- Support up to 50 services per workspace
- Support up to 30-day metrics retention

---

## Helm Chart Examples

### Example 1: PostgreSQL (Bitnami)

```yaml
# CloudAppTemplate seed data
name: PostgreSQL
slug: postgresql
helm_repo_url: https://charts.bitnami.com/bitnami
helm_chart_name: postgresql
helm_chart_version: "15.5.0"
helm_default_values: |
  {
    "primary": {
      "persistence": {
        "enabled": true,
        "size": "8Gi"
      }
    },
    "metrics": {
      "enabled": true
    }
  }
helm_value_specs: |
  {
    "required": [
      {
        "key": "auth.postgresPassword",
        "label": "Admin Password",
        "type": "password",
        "required": true
      },
      {
        "key": "auth.database",
        "label": "Database Name",
        "type": "text",
        "default_value": "app_db"
      }
    ],
    "optional": [
      {
        "key": "primary.persistence.size",
        "label": "Storage Size",
        "type": "select",
        "default_value": "8Gi",
        "options": ["8Gi", "16Gi", "32Gi"]
      }
    ]
  }
```

### Example 2: n8n (Custom/Community Chart)

```yaml
name: n8n Workflow
slug: n8n
helm_repo_url: https://8gears.github.io/n8n-helm-chart
helm_chart_name: n8n
helm_chart_version: "0.23.0"
helm_default_values: |
  {
    "n8n": {
      "encryption_key": "auto-generated"
    },
    "persistence": {
      "enabled": true,
      "size": "5Gi"
    }
  }
helm_value_specs: |
  {
    "required": [],
    "optional": [
      {
        "key": "n8n.basicAuth.active",
        "label": "Enable Basic Auth",
        "type": "boolean",
        "default_value": false
      },
      {
        "key": "n8n.basicAuth.user",
        "label": "Basic Auth Username",
        "type": "text"
      },
      {
        "key": "n8n.basicAuth.password",
        "label": "Basic Auth Password",
        "type": "password"
      }
    ]
  }
```

### Example 3: Redis (Bitnami)

```yaml
name: Redis
slug: redis
helm_repo_url: https://charts.bitnami.com/bitnami
helm_chart_name: redis
helm_chart_version: "19.0.0"
helm_default_values: |
  {
    "architecture": "standalone",
    "auth": {
      "enabled": true
    }
  }
helm_value_specs: |
  {
    "required": [
      {
        "key": "auth.password",
        "label": "Redis Password",
        "type": "password",
        "required": true
      }
    ],
    "optional": [
      {
        "key": "master.persistence.size",
        "label": "Storage Size",
        "type": "select",
        "default_value": "4Gi",
        "options": ["4Gi", "8Gi", "16Gi"]
      }
    ]
  }
```

---

## 服務資訊與日誌分頁

服務詳情的共通分頁順序為「總覽／配置／資訊／日誌」。原有依能力顯示的
Addons、MCP Servers、Access 分頁接在後面，既有深連結維持有效；新增
`/woow#/workspace/:workspaceId/services/:serviceId/information` 與 `/logs`。

### 資訊（相關文件）

比照 Home Assistant App 的 `DOCS.md`，模板新增 `documentation` Markdown 欄位，
存放服務使用、設定及疑難排解指南；管理員在模板的 Service Documentation 頁維護。
尚未填寫時依序使用既有 `full_description`、`description`，皆空則顯示空狀態。
不從任意遠端網址抓取內容。`documentation_url` 僅接受 HTTP(S) 連結。

`POST /api/workspaces/:workspaceId/services/:serviceId/documentation` 使用既有工作區
成員及服務可見性檢查，回傳 `{title, content, documentation_url}`。前端使用現有
Markdown + DOMPurify renderer，並在頁內呈現卡片。

### 日誌

`POST /api/workspaces/:workspaceId/services/:serviceId/logs` 僅供工作區 owner/admin。
前端送 `pod`、`container`、`tail_lines`、`previous`；namespace/release 一律由已授權的
服務記錄決定。錯工作區、隱藏服務及非管理成員在呼叫 Operator 前即拒絕。
讀取不修改服務／訂閱，也不套用只適合寫入的訂閱狀態閘，因此失敗服務仍可診斷。

Operator 的 `GET /api/releases/:namespace/:release/logs` 每次重新查核 exact
`app.kubernetes.io/instance` 標籤、namespace 及容器歸屬，再呼叫 `kubectl logs`。
回傳 Pod／一般與 init container 選項、帶時間戳的文字與狀態碼；不需要 exec 或增加
租戶 Kubernetes 權限。VM 服務回覆不支援，避免將虛擬機基礎設施日誌誤當訪客 OS 日誌。

每次預設 500 行、最多 2000 行及 256 KiB；不是永久日誌儲存或全量下載。
前端每次請求結束後等 5 秒更新，暫停、切到背景或離開分頁即停止後續排程。
搜尋只過濾已載入的行；下載包含完整的已載入快照，不受搜尋條件影響。
支援換行切換、上次容器日誌，以及只在讀者原本位於底部時自動捲動。

部署順序：先發布含 logs API 的 Operator，再升級平台 addon（新增資料庫欄位）。
舊 Operator 或尚未部署的服務會顯示不可用／等待狀態，不回傳底層錯誤或憑證。

參考：[HA App 文件檔案](https://contributors.home-assistant.io/apps/reference/files/)、
[Supervisor App API](https://contributors.home-assistant.io/apps/supervisor-api/apps/)。

## Technical Decisions

| 項目                      | 決定                      | 說明                                                    |
| ------------------------- | ------------------------- | ------------------------------------------------------- |
| **K8s/Helm 操作**         | 獨立 PaaS Operator 服務   | Odoo Pod 無法直接執行 Helm，需透過獨立服務              |
| **PaaS Operator 技術棧**  | Python FastAPI + Helm CLI | FastAPI 輕量快速，subprocess 調用 Helm CLI              |
| **Odoo ↔ Operator 通訊**  | HTTP REST + API Key       | Internal ClusterIP，不對外暴露                          |
| **Helm Chart Repository** | 公開 repos + 自建         | 使用 Bitnami/ArtifactHub，未來可自建 Chart Museum       |
| **Metrics collection**    | Prometheus + Grafana      | K8s metrics-server 不保留歷史，無法支援 24h/7d/30d 查詢 |
| **Log aggregation**       | Loki + Promtail           | 整合 Grafana 生態系                                     |
| **Billing integration**   | _(Future Phase)_          | 暫不實作                                                |
| **Multi-cluster support** | 不支援                    | 單一 K8s cluster                                        |

---

## References

- Design mockups: `resource/stitch_paas_web_app_shell_global_navigation_2026-01-16/`
- Existing workspace model: `src/models/workspace.py`
- Router implementation: `src/static/src/paas/core/router.js`
