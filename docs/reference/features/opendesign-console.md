---
title: OpenDesign Console — 架構與安全模型
category: reference
implementation_status: implemented
---

# OpenDesign Console — 架構與安全模型

> **範圍**：說明 OpenDesign cloud service 的 **console 網頁終端**（`/console`）如何運作，以及它的
> **安全模型**（RBAC / ServiceAccount token / NetworkPolicy 隔離）。這份是 platform 側的錨點文件；
> chart 實作細節（Pod 拓樸、NP 規則、RBAC Role、`connect.sh`、entrypoint）在 charts repo 的對應
> 文件，避免雙寫。OpenDesign 的整體上架決策與定價見 issue #380 與
> [`cloud-services.md`](cloud-services.md)。

## Overview

OpenDesign 是一個「完整應用」型 cloud service（`slug=open-design`，template `cloud_app_opendesign`）。
一個部署由**兩個 Deployment / 三個容器**組成：

| 元件 | Pod | Port | 角色 |
| --- | --- | --- | --- |
| **daemon** | `<release>-open-design` | 7457（loopback）| OpenDesign 本體。跑 AI agent CLI（`claude` 等）。綁 loopback，不對外。 |
| **authproxy**（sidecar）| 同 daemon pod | 8080 | nginx basic-auth 閘 + 反向代理。對外流量的唯一入口。 |
| **console** | `<release>-open-design-console` | 7681 / 18790 | ttyd 網頁終端（7681）+ Flask 管理儀表板（18790）。 |

認證採 **C 案**（#357）：authproxy 與 ttyd 都用 `config.sensitive.admin_password`（＝租戶密碼、`has_admin_gui`
注入）做 basic-auth，daemon 自身的 API token 由 chart 自產、內部化不外露。

## `/console` 如何運作（端到端）

對外 URL（`https://<subdomain>.<paas_domain>`）走 Cloudflare tunnel → **daemon pod 的 authproxy**。
authproxy 依路徑分流：

```
瀏覽器
  │ HTTPS（Cloudflare tunnel）
  ▼
authproxy（daemon pod 內的 sidecar，:8080）
  ├── location /          → 127.0.0.1:7457   （daemon 本體，同 pod）
  └── location /console/  → console Service:7681（ttyd，另一個 pod）
                              │
                              │ ttyd 啟動 /app/connect.sh（跑在 console pod 內）
                              │   connect.sh: kubectl exec -it <daemon-pod> -c open-design -- /bin/bash
                              ▼
                            open-design daemon 容器  ← 使用者「打字的 shell」在這
```

**關鍵拓樸事實**（安全推論的基礎）：

1. `/console/` 的**連線終點是 console pod 的 ttyd**，不是 daemon。ttyd 再用 `kubectl exec`
   把使用者**轉送**進 daemon 容器。
2. 因此使用者互動的 shell 在 **daemon** pod；但握有 kubectl 權限的 token 在 **console** pod。兩者是不同 pod。
3. `connect.sh` 是無限迴圈：終端打 `exit` 只會 `Session ended. Reconnecting in 2s...` 後重新 exec，
   **不會**掉出一個 console pod 的本地 shell。

訊號判讀（除錯用）：

| 觀察 | 意義 |
| --- | --- |
| `/console` → **301** → `/console/` | chart ≥ 0.1.2 的路由存在 |
| `/console/` → 401，realm=**`ttyd`** | 已穿過 authproxy（`auth_basic off` 生效），是 ttyd 自己的 auth（帳密＝`admin` + `admin_password`）|
| `/console/` → 401，realm=**`Open Design`** | 落到 authproxy 預設 location＝**路由不存在**（chart 還是舊版）|
| `/console/` → **502** | console pod not-ready，Service endpoint 被摘 |
| ttyd 內無限印 `Waiting for open-design pod to be ready...` | `connect.sh` 找不到 daemon pod（見 [Known gap 1](#gap-1-console-連不到-apiserverpost-dnat)）|

## 安全模型

### token ＝認證，Role ＝授權（兩件事）

console pod 之所以能 `kubectl exec`，是因為掛了一張 **ServiceAccount token**：

- **身分**（你是誰）：ServiceAccount `<release>-open-design-console`
- **憑證**：該 SA 的 bearer token（JWT），自動掛在
  `/var/run/secrets/kubernetes.io/serviceaccount/token`，會輪替。`sub` =
  `system:serviceaccount:<workspace-ns>:<release>-open-design-console`。
- **權限**：由 **`Role` + `RoleBinding`** 決定（**不是 `ClusterRole`**）。

**token 本身不帶任何權限**——它只證明「我是這個 SA」。能做什麼完全由 RoleBinding 綁的 Role 決定。
所以「危險的」不是 token，是這個 SA 被授予的 Role。

### Role 的權限（namespace 範圍）

console SA 的 Role（chart 定義，per-release）：

| 資源 | verbs | resourceNames | 範圍 |
| --- | --- | --- | --- |
| `pods` | get, list | *(無)* | **整個 workspace namespace** |
| `pods/exec` | create, get | *(無)* | **整個 workspace namespace** |
| `pods/log` | get | *(無)* | 整個 namespace |
| `deployments` (apps) | get, patch | `<release>-open-design` | 只有自己那個 |
| `secrets` | get, patch, update | `<release>-open-design-secrets` | 只有自己那個 |

綁定用 **`RoleBinding` → `Role`**（namespace scope），**指向此 SA 的 `ClusterRoleBinding` = 0 筆**。
這是「blast radius 限於一個 workspace」的技術根源：

- ✅ 只能在自己的 workspace namespace 內動作
- ❌ 碰不到其他 namespace / 其他租戶 / 叢集節點 / kube-system；不能建 ClusterRoleBinding

### blast radius：workspace 內是「全部 pod」

注意 `pods` / `pods/exec` **沒有 `resourceNames` 限制**——原因是 Kubernetes RBAC 的 `resourceNames`
**對 `list` 動作無效**，而且 pod 名帶 deployment hash（每次部署都變）無法預先綁死。因此：

> **console SA 的 token 可以 `kubectl exec` 進同 workspace 內的任何 pod**（含租戶在同 workspace 的其他
> 服務，如 n8n）。secrets / deployments 有 `resourceNames` 綁死只能碰自己的，但 **pods/exec 是
> namespace-wide**。

### 但這個能力「不易被觸及」——三道結構性防線

raw RBAC 看起來很寬，但實際可達性被三件事壓低：

1. **console pod 沒有互動入口**：唯一的外部入口 ttyd 永遠 `kubectl exec` **進 daemon**，
   使用者從頭到尾拿不到 console pod 自己的 shell → 拿不到那張 token 去用。
2. **daemon pod 是「無憑證」的**：daemon（跑任意 agent 程式碼、最可能被塞入 untrusted code 的 pod）
   `automountServiceAccountToken: false`、容器內**沒有 kubectl**。就算 daemon 被打下來，也沒有 k8s 憑證可用。
3. **NetworkPolicy 隔開 daemon↔console 的危險面**：daemon → console:7681（ttyd）通（反代需要），
   但 daemon → console:18790（Flask，會直接呼叫 kubectl 的那半）**被 NP 擋**。所以就算 daemon 被打下來，
   也碰不到 console 的 kubectl 端點。

**淨結論**：能力很大的 token 坐在一個**沒有互動入口、且與唯一 untrusted pod 之間有 NP 隔離**的 pod 裡。
raw RBAC 的名目風險 ≫ 實際可觸及風險。要留意的長期風險是「有人日後把 Flask 儀表板接出去而沒配好認證」——
那會把 kubectl 端點暴露成 HTTP surface。

> ⚠️ 診斷方法論教訓：驗證 pod 間連通性用 `python3 socket.connect()`（拿乾淨的
> `ConnectionRefusedError`）比 `kubectl --server=<ip>` 可靠——後者無憑證時會印「Please enter Username」，
> 看起來像連上了其實只是互動提示，會誤導方向。woow-k3s 的 CNI 對 NP-deny 回 **REJECT**（`connection
> refused`，非 timeout），這也是辨認「被 NP 擋」而非「對方沒起來」的訊號。

## Known gaps（platform 責任）

<a id="gap-1-console-連不到-apiserverpost-dnat"></a>
### Gap 1：console 連不到 apiserver（post-DNAT NetworkPolicy）

**症狀**：ttyd 內無限印 `Waiting for open-design pod to be ready...`——`connect.sh` 的
`kubectl get pod -l <label> ... 2>/dev/null`（吞掉 stderr）拿不到 daemon pod 名。

**根因**：console 的 NP egress 只開了 chart 預設的 `apiServerCidrs: [10.43.0.1/32]`（kubernetes Service
ClusterIP）。但 **woow-k3s 的 CNI 在 kube-proxy DNAT «之後» 比對 egress**：連 `10.43.0.1:443` 會被
DNAT 成 apiserver 的真實 endpoint（control-plane node IP），那個 IP 不在允許清單 → deny → `connection
refused`。chart values 註解其實已預告此雷（「add the control-plane node endpoint if your CNI matches
egress post-DNAT」）。

**這是 platform 注入缺口**，性質同 `config.allowedOrigins`（見下）：cluster-specific 的值不該寫死進
chart 或 git seed，應由 platform 在部署時算好注入 `networkPolicy.apiServerCidrs`。woow-k3s 的實際值：
`kubectl -n default get endpoints kubernetes` → `192.168.10.21/22/23:6443`（把這三個 `/32` 加進去後，
ClusterIP 與 node IP 兩者同時變通，即 post-DNAT 的鐵證）。

**建議修法**：比照 `cloud_service._inject_allowed_origins` 新增一個 slug-scoped injector，值放
`ir.config_parameter`（如 `woow_paas_platform.apiserver_cidrs`），部署時注入。**尚未實作**。

### Gap 2：`console.externalPath` 的 reuse_values 陷阱（升級路徑）

`cloud_service.action_redeploy()` 用 `reuse_values=True` 呼叫 operator 升級。chart 新增的**預設值**
（如 `console.externalPath=/console`）在 reuse_values 路徑下**不會被吃到**（Helm 語意）→ 升級舊 release
時 `/console` 靜默不生效。**解**：把值顯式寫進 `svc.helm_values`（顯式傳的 values 等同 `-f` override，
會蓋過 reused）再 redeploy。fresh install 不受影響（吃 chart 預設）。

### Gap 3：Flask 管理儀表板無對外入口

console pod 的 Flask（:18790）寫了 `/api/status`、`/api/logs`、`/api/restart`（rollout restart daemon）等
功能，但 **authproxy 沒有任何 location 指向 18790**，且 daemon→console:18790 被 NP 擋 → 目前**完全沒有入口**。
RBAC 裡的 `pods/log: get` 與 `deployments: patch` 就是為這個目前到不了的功能開的。屬「功能寫了但路由沒接」，
不是安全洞（現況安全），但重接時要留意（見上「長期風險」）。

## Platform 側整合點

| 關注 | 位置 |
| --- | --- |
| seed template（chart 版本 pin、helm_default_values）| `src/data/cloud_app_templates.xml`（`cloud_app_opendesign`）|
| 部署時注入公開 Origin 白名單 | `cloud_service._inject_allowed_origins`（#399，比照 `_inject_storage_class`）|
| workspace ns quota（多容器記憶體帳）| `cloud_service._compute_ns_quota`；`min_ram_gb` 需蓋過 daemon limits（#398）|
| in-place 升級（保 PVC）| `cloud_service.action_redeploy()`（`reuse_values=True`，見 Gap 2）|

## 相關

- [`cloud-services.md`](cloud-services.md) — Cloud Services 整體規格
- [`../../decisions/patterns/`](../../decisions/patterns/) — `_inject_*` static injector 家族（`_inject_storage_class` / `_inject_image_pull_secrets` / `_inject_allowed_origins`）
- OpenDesign 上架全案：issue #380（含定價、C 案 auth、4-PR quota/origin/chart 修復鏈）
- chart 實作細節（Pod 拓樸 / NP 規則 / RBAC Role / `connect.sh` / entrypoint supervisor / values knobs / 版本史）：
  [woow-paas-charts → `charts/open-design/README.md`](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/src/branch/main/charts/open-design/README.md)
