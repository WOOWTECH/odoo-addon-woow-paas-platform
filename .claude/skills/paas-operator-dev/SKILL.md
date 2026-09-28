---
name: paas-operator-dev
description: PaaS Operator 本機開發與整合測試 recipe。改動 extra/paas-operator/ 要跑 integration tests（真 K8s、helm 3.13.3 陷阱、REQUIRE_KUBEVIRT）、或需要 port-forward 連本機 operator（取 API key、Odoo 設定、Docker 網路注意事項）時使用。
---

# PaaS Operator 本機開發與整合測試

## PaaS Operator Integration Tests (real K8s)

Catch contract drift that unit-test mocks hide (BUG-7/8/9 class — workload-type, HTTP
contract, RBAC). **No CI runner** — these must run locally before merging changes under
`extra/paas-operator/`.

```bash
cd extra/paas-operator

# 首次：抓 operator Dockerfile 釘的同一顆 helm 3.13.3（⚠ 不要用系統 helm，見下表）；平台字串依本機推算
HELM_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
mkdir -p /tmp/helm3 && curl -fsSL \
  "https://get.helm.sh/helm-v3.13.3-${HELM_PLATFORM}.tar.gz" | tar xz -C /tmp/helm3

# First time only: install deps（Python 版本以 Dockerfile 的 python:3.11 為準，用 repo 內 .venv）
.venv/bin/pip install -r requirements.txt

# 用只含目標 context 的 kubeconfig，免動使用者當前 context
kubectl config view --raw --minify --flatten --context=docker-desktop > /tmp/kubeconfig-dd.yaml

# Run
KUBECONFIG=/tmp/kubeconfig-dd.yaml HELM_BINARY="/tmp/helm3/${HELM_PLATFORM}/helm" \
    .venv/bin/pytest -m integration tests/integration/ -v
```

| 變數                           | 為什麼                                                                                                                |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| `HELM_BINARY`                  | ⚠ **不可用 `$(which helm)`**：Homebrew 的 helm 已是 **v4**，而 helm 4 的 `--post-renderer` **改吃 plugin 名、不再吃執行檔路徑**。本 repo 的 #317 PVC-keep post-renderer 傳的正是路徑，且 install/upgrade 兩條路都無條件帶上 → **整套會在第一步 install 就 500**（`Helm installation failed`），看起來像被測程式壞掉、其實是 helm 版本不相容。用 operator Dockerfile 釘的 **3.13.3**（`ARG HELM_VERSION=3.13.3`），不要為了跑測試把系統 helm 降版 |
| `API_KEY` / `NAMESPACE_PREFIX` | 不用設，`app_client` fixture 自動 set（`"integration-test-key"` + `"paas-ws-test-op-"`）                                                  |
| `REQUIRE_KUBEVIRT`             | 設 `1` 時，KubeVirt/CDI 相關測試由「無 CRD 就 skip」升級為 **fail**。驗收 VM 型 workload（#423）時必設，否則 skip 會被誤讀成通過 |

⚠ **KubeVirt/CDI 測試需要有 CRD 的叢集**：docker-desktop 沒有，那幾支會 skip。要真正驗到 VM 路徑，用 woow-k3s（#423 W0 已裝 KubeVirt v1.8.0 + CDI v1.64.0）並帶 `REQUIRE_KUBEVIRT=1`。

Cluster 殘留風險：測試用唯一 namespace `paas-ws-test-op-{uuid8}`，正常 teardown 會刪；中斷或 cleanup 失敗會留下，需要時手動 `kubectl delete ns paas-ws-test-op-<x>`。詳細請見 `extra/paas-operator/tests/integration/README.md`。

## PaaS Operator Local Development

When the PaaS Operator is running in Kubernetes without Ingress, use port-forward to access it locally:

```bash
# Port forward the paas-operator service to localhost:8000
kubectl port-forward -n paas-system svc/paas-operator 8000:80

# Verify connection
curl http://localhost:8000/health
```

**Get API Key from Kubernetes Secret:**

```bash
kubectl get secret -n paas-system paas-operator-secret -o jsonpath='{.data.api-key}' | base64 -d
```

**Odoo Configuration (Settings → Woow PaaS):**

- **PaaS Operator URL**: `http://localhost:8000` (本機 Odoo) 或 `http://host.docker.internal:8000` (Docker Odoo)
- **PaaS Operator API Key**: (value from secret above)

> **Note**: The port-forward session must remain active while testing. If you close the terminal, re-run the port-forward command.

**Docker 網路注意事項：**

當 Odoo 運行在 Docker 容器中時，容器內的 `localhost` 指向容器本身，而非宿主機。因此需要使用 `host.docker.internal` 來連接宿主機上的 port-forward：

| Odoo 運行環境 | PaaS Operator URL                  |
| ------------- | ---------------------------------- |
| 本機直接運行  | `http://localhost:8000`            |
| Docker 容器   | `http://host.docker.internal:8000` |

```bash
# 確認 port-forward 正在運行
kubectl port-forward -n paas-system svc/paas-operator 8000:80

# 從 Docker 容器內測試連線
docker exec -it <odoo-container> curl http://host.docker.internal:8000/health
```
