# Cloudflare Tunnel 部署指南

本文件說明如何在 Kubernetes 中部署 Cloudflare Tunnel，讓 PaaS 平台的服務可以透過公開網址存取。

## 架構

```
用戶訪問 https://xxx.woowtech.io
         ↓
    Cloudflare Edge (自動 HTTPS + DDoS 防護)
         ↓
    Cloudflare Tunnel (加密連線)
         ↓
    cloudflared Pod (K8S 內)
         ↓
    K8S Service (ClusterIP)
```

## 前置準備

### 1. 建立 Cloudflare Tunnel

1. 登入 [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/)
2. 左側選單：**Networks** → **Tunnels**
3. 點擊 **Create a tunnel**
4. 選擇 **Cloudflared** 類型
5. 輸入 Tunnel 名稱（如 `woow-paas-tunnel`）
6. **重要**：複製 `tunnel_token`（只會顯示一次！）

### 2. 取得必要資訊

| 參數         | 說明      | 取得方式                           |
| ------------ | --------- | ---------------------------------- |
| Tunnel Token | 連線憑證  | 建立 Tunnel 時取得                 |
| Account ID   | 帳號 ID   | Dashboard URL 或 Domain Overview   |
| Tunnel ID    | Tunnel ID | Zero Trust → Tunnels → 你的 tunnel |

## 部署步驟

### Step 1：建立 Namespace

```bash
kubectl create namespace cloudflare-system
```

### Step 2：建立設定檔

編輯 `cloudflare-values.yml`：

```yaml
cloudflare:
  tunnel_token: "你的_tunnel_token"

replicaCount: 2

resources:
  limits:
    cpu: 100m
    memory: 128Mi
  requests:
    cpu: 50m
    memory: 64Mi
```

> **注意**：此檔案包含敏感資訊，已在 `.gitignore` 中排除。

### Step 3：安裝 Helm Chart

```bash
# 新增 Cloudflare Helm repo
helm repo add cloudflare https://cloudflare.github.io/helm-charts
helm repo update

# 安裝
helm install cloudflare-tunnel cloudflare/cloudflare-tunnel-remote \
  --namespace cloudflare-system \
  --values extra/cloudflare/cloudflare-values.yml
```

### Step 4：驗證部署

```bash
# 檢查 Pod 狀態
kubectl get pods -n cloudflare-system

# 查看 logs 確認連線成功
kubectl logs -n cloudflare-system -l app.kubernetes.io/name=cloudflare-tunnel-remote --tail=30
```

成功的 logs 應該顯示：

```
INF Starting tunnel tunnelID=xxx
INF Registered tunnel connection connIndex=0 location=tpe01 protocol=quic
```

## 設定路由

### 手動設定（Dashboard）

1. 進入 [Zero Trust Dashboard](https://one.dash.cloudflare.com/) → **Networks** → **Tunnels**
2. 點擊你的 Tunnel → **Public Hostname** tab
3. 點擊 **Add a public hostname**

| 欄位      | 範例                                               |
| --------- | -------------------------------------------------- |
| Subdomain | `myapp`                                            |
| Domain    | `woowtech.io`                                      |
| Type      | `HTTP`                                             |
| URL       | `myapp-service.paas-ws-123.svc.cluster.local:8080` |

### 自動設定（PaaS Operator）

當 PaaS Operator 啟用 Cloudflare 整合後，部署服務時會自動建立路由：

```json
POST /api/releases
{
  "namespace": "paas-ws-123",
  "name": "myapp",
  "chart": "oci://...",
  "expose": {
    "enabled": true,
    "subdomain": "myapp-123"
  }
}
```

## 管理指令

### 升級

```bash
helm upgrade cloudflare-tunnel cloudflare/cloudflare-tunnel-remote \
  --namespace cloudflare-system \
  --values extra/cloudflare/cloudflare-values.yml
```

### 解除安裝

```bash
helm uninstall cloudflare-tunnel --namespace cloudflare-system
kubectl delete namespace cloudflare-system
```

### 查看狀態

```bash
# Pod 狀態
kubectl get pods -n cloudflare-system

# 即時 logs
kubectl logs -n cloudflare-system -l app.kubernetes.io/name=cloudflare-tunnel-remote -f
```

## 故障排除

### Tunnel 無法連線

1. 檢查 token 是否正確
2. 檢查網路是否能連到 Cloudflare

```bash
kubectl exec -n cloudflare-system -it <pod-name> -- curl -I https://api.cloudflare.com
```

### 路由無法存取

1. 確認 Tunnel 在 Dashboard 顯示 **HEALTHY**
2. 確認 Service URL 格式正確：`http://<service>.<namespace>.svc.cluster.local:<port>`
3. 測試內部連線：

```bash
kubectl run test --rm -it --image=curlimages/curl -- \
  curl http://<service>.<namespace>.svc.cluster.local:<port>
```

### DNS 無法解析

本機 DNS 快取問題：

```bash
# macOS 清除 DNS 快取
sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder
```

## 相關資源

- [Cloudflare Tunnel 文檔](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
- [Cloudflare Helm Charts](https://github.com/cloudflare/helm-charts)
- [Zero Trust Dashboard](https://one.dash.cloudflare.com/)
