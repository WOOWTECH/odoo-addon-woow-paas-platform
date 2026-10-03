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
一個部署由**一個 StatefulSet（daemon，chart 0.3.0 起）＋一個 Deployment（console）/ 三個容器**組成：

| 元件 | Pod | Port | 角色 |
| --- | --- | --- | --- |
| **daemon** | `<release>-open-design-0`（StatefulSet，名稱固定）| 7457（loopback）| OpenDesign 本體。跑 AI agent CLI（`claude` 等）。綁 loopback，不對外。 |
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

### Role 的權限（每條規則都綁名稱，chart 0.3.0 起，#1360）

console SA 的 Role（chart 定義，per-release）：

| 資源 | verbs | resourceNames |
| --- | --- | --- |
| `pods` | get | `<release>-open-design-0` |
| `pods/exec` | create, get | `<release>-open-design-0` |
| `pods/log` | get | `<release>-open-design-0` |
| `statefulsets` (apps) | get, patch | `<release>-open-design` |
| `secrets` | get, patch, update | `<release>-open-design-secrets` |

沒有任何 list／watch。綁定用 **`RoleBinding` → `Role`**（namespace scope），**指向此 SA 的
`ClusterRoleBinding` = 0 筆**。

### 為什麼 daemon 是 StatefulSet（#1360）

RBAC 不能用 label 選 pod，`resourceNames` 只能寫完整名稱。chart 0.2.x 的 daemon 是 Deployment，pod 名
`<rs>-<random>` 無法預先寫進 Role，所以 `pods`／`pods/exec`／`pods/log` 只能開整個 namespace——
**console SA 可以 exec 進同工作區所有服務的 pod**（Odoo＋PostgreSQL、n8n、其他服務的 token）。沙盒以
0.2.2 實測可從 console 讀到同 ns 另一個 pod 的檔案。0.3.0 把 daemon 改成 StatefulSet（pod 固定
`<fullname>-0`），每條規則都綁名稱；同樣的操作回 `Forbidden`。

連帶影響：

- **重設密碼**：operator 的 `_reset_via_helm_secret` 只 `rollout restart` Deployment（現在只有 console）。
  daemon 的 auth-proxy htpasswd 改成 chart Secret 的 `htpasswd` key、以 Secret volume 掛載（nginx 每個
  請求重讀），kubelet 同步後生效（約 1–2 分鐘，沙盒實測 97 秒），OpenDesign 本身不重啟。
- **Redeploy 0.2.x → 0.3.0**：Deployment 換成 StatefulSet 一次（約 1 分鐘），PVC 名稱不變、資料保留；
  daemon 的 podAntiAffinity 讓新 pod 不會跟舊 Deployment pod 同 node，RWO PVC 等舊 pod 結束才掛得上，
  兩個 daemon 不會同時寫 `/app/.od`。
- operator 的 `patch_sidecar`（自動取 `deployments[0]`）對 OpenDesign 會選到 console，不要對 OpenDesign 用。
- hermes-agent chart 的 console Role 仍是同型的 namespace 範圍 exec（另案處理）。

### 其他結構性防線（仍然有效）

1. **console pod 沒有互動入口**：唯一的外部入口 ttyd 永遠 `kubectl exec` **進 daemon**，
   使用者從頭到尾拿不到 console pod 自己的 shell。
2. **daemon pod 是「無憑證」的**：`automountServiceAccountToken: false`、容器內**沒有 kubectl**。
3. **NetworkPolicy**：daemon → console:7681（ttyd）通，daemon → console:18790（Flask）被 NP 擋。

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

**已修復（#404，平台 18.0.1.0.75）**：比照 `_inject_allowed_origins` 新增 slug-scoped injector
`cloud_service._inject_apiserver_cidrs`（`src/models/cloud_service.py`），部署時把白名單寫進
`networkPolicy.apiServerCidrs`（unknown slug 一律 no-op）。值來源是 `ir.config_parameter`
**`woow_paas_platform.apiserver_cidrs`**：逗號分隔的 CIDR 字串，由 `_get_apiserver_cidrs()` parse 成
list。code 內預設為 `10.43.0.1/32`（與 chart 預設一致、cluster-agnostic）。

**ops 設定方式**（cluster-specific 值不寫死進 code / git seed，各環境自行設定）：

```
Settings → Technical → Parameters → System Parameters
key:   woow_paas_platform.apiserver_cidrs
value: 10.43.0.1/32,192.168.10.21/32,192.168.10.22/32,192.168.10.23/32   # woow-k3s
```

node IP 取得：`kubectl -n default get endpoints kubernetes`。未設定時退回 `10.43.0.1/32` 單值，等同
chart 預設 —— 在 post-DNAT 叢集上 console 仍會壞，故 woow-k3s 的 stg / prod **必須**設定此參數。

**生效範圍＝install-time**：injector 家族（storageClass / allowedOrigins / apiServerCidrs）都只在
`cloud_service._execute_deploy()` 跑，`action_redeploy()` 是把 `svc.helm_values` 原樣重送 +
`reuse_values=True`，**不重跑 injector**。所以：設定參數後**新建**的服務自動帶 NP 洞；**本 fix 之前已存在
的 release** 不會因為 redeploy 就補上，需把 `networkPolicy.apiServerCidrs` 顯式寫進 `svc.helm_values`
再 redeploy（同 Gap 2 的手法）。

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
| 部署時注入 apiserver egress 白名單 | `cloud_service._inject_apiserver_cidrs`（#404；值＝`ir.config_parameter` `woow_paas_platform.apiserver_cidrs`，見 Gap 1）|
| workspace ns quota（多容器記憶體帳）| `cloud_service._compute_ns_quota`；`min_ram_gb` 需蓋過 daemon limits（#398）|
| in-place 升級（保 PVC）| `cloud_service.action_redeploy()`（`reuse_values=True`，見 Gap 2）|

## 相關

- [`cloud-services.md`](cloud-services.md) — Cloud Services 整體規格
- [`../../decisions/patterns/`](../../decisions/patterns/) — `_inject_*` static injector 家族（`_inject_storage_class` / `_inject_image_pull_secrets` / `_inject_allowed_origins`）
- OpenDesign 上架全案：issue #380（含定價、C 案 auth、4-PR quota/origin/chart 修復鏈）
- chart 實作細節（Pod 拓樸 / NP 規則 / RBAC Role / `connect.sh` / entrypoint supervisor / values knobs / 版本史）：
  [woow-paas-charts → `charts/open-design/README.md`](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/src/branch/main/charts/open-design/README.md)
