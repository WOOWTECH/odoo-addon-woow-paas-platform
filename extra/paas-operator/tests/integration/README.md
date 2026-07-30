# Operator Integration Tests

Real K8s + real `kubectl` + real HTTP through FastAPI. Companion to the
mocked unit tests in `tests/test_*.py`.

## What these catch that unit tests do not

- **BUG-7 class**: workload-type bugs (`scale_release` only handling
  Deployment, missing StatefulSet)
- **BUG-8 class**: client/server HTTP contract drift (body vs. query string)
- **BUG-9 class**: ServiceAccount RBAC subresource permissions
  (`deployments/scale`, `statefulsets/scale`) — exercised when run with the
  real operator ServiceAccount in CI

Pure unit tests with `mock_k8s` cannot expose any of the above because the
mock layer hides exactly the boundary that drifts.

## Running locally

### macOS quickstart

```bash
cd extra/paas-operator

# Pick a cluster context (once per shell)
kubectl config use-context docker-desktop   # or your woow-k3s context

# First time only: install Python deps
pip install -r requirements.txt

# Run (⚠ see "helm v4 陷阱" below — `$(which helm)` is probably wrong now)
HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v
```

39 tests（其中 2 個 KubeVirt CRD 契約測試在沒有 CDI 的叢集會自我 skip，例如
docker-desktop）。The `app_client` fixture sets a real
`API_KEY` (default `integration-test-key`) + `NAMESPACE_PREFIX="paas-ws-test-op-"`
automatically and sends the matching `X-API-Key` header — no need to export.
(Since F-PC-4 the operator fails closed with 503 on an unset key, so the suite
authenticates rather than relying on the old empty-key pass-through.)

Cluster choice:

| Cluster         | Notes                                                          |
| --------------- | -------------------------------------------------------------- |
| docker-desktop  | Recommended for dev; fully isolated from shared infra.         |
| woow-k3s        | Cross-env validation; tests create/delete unique namespaces.   |
| kind (ephemeral)| `kind create cluster --name op-int` then run; delete after.    |

### Generic invocation

```bash
# Any reachable cluster
HELM_BINARY=$(which helm) KUBECONFIG=~/.kube/config \
    pytest -m integration tests/integration/

# Against a kind cluster you've already created
kind create cluster --name op-int
HELM_BINARY=$(which helm) pytest -m integration tests/integration/
```

If `kubectl cluster-info` fails the suite skips itself (no setup penalty
for a default `pytest` run, which also defaults to `-m "not integration"`
via `pytest.ini`).

`HELM_BINARY` is required because the operator's FastAPI lifespan refuses
to start when helm is missing. macOS Homebrew puts helm at
`/opt/homebrew/bin/`, so the default `/usr/local/bin/helm` is wrong there.

### ⚠ helm v4 陷阱：`$(which helm)` 現在多半是錯的

Homebrew 的 `helm` formula 已經是 **v4**（2026-07-28 實測 `v4.2.3`；`helm@3` /
`helm@4` 兩個 keg 也都 symlink 到同一顆 4.2.3）。helm 4 改了 `--post-renderer`
的語意——**改吃 plugin 名稱，不再吃執行檔路徑**：

```
Error: invalid argument "…/.venv/bin/python" for "--post-renderer" flag:
plugin: {Name:…/.venv/bin/python Type:postrenderer/v1} not found
```

本 repo 的 #317 PVC-keep post-renderer（`_pvc_keep_post_renderer_args()`）傳的
正是執行檔路徑，且 **install 與 upgrade 兩條路都無條件帶上**，所以在 helm 4 下
整個 integration 套件會在**第一步 install 就 500**（`Helm installation failed`），
看起來像被測程式壞掉，其實是 helm 版本不相容。

**做法：用 operator `Dockerfile` 釘的同一個 helm 3.13.3 跑**（`ARG
HELM_VERSION=3.13.3`）。不要為了跑測試把系統 helm 降版：

```bash
# 抓一份 helm 3.13.3 放到暫存目錄（不動系統 helm）
mkdir -p /tmp/helm3 && curl -fsSL \
  https://get.helm.sh/helm-v3.13.3-darwin-arm64.tar.gz | tar xz -C /tmp/helm3
/tmp/helm3/darwin-arm64/helm version --short   # 應為 v3.13.3+gc8b9489

# 跑測試時指過去（Linux 換成 helm-v3.13.3-linux-amd64.tar.gz）
HELM_BINARY=/tmp/helm3/darwin-arm64/helm \
    pytest -m integration tests/integration/ -v
```

同場加映：想跑 docker-desktop 又不想動使用者當前的 kubectl context（例如
context 停在 `woow-k3s`），可以匯出一份只含該 context 的 kubeconfig 再指過去，
比 `kubectl config use-context` 少一個「忘了切回去」的風險：

```bash
kubectl config view --raw --minify --flatten --context=docker-desktop \
    > /tmp/kubeconfig-dd.yaml
KUBECONFIG=/tmp/kubeconfig-dd.yaml HELM_BINARY=/tmp/helm3/darwin-arm64/helm \
    pytest -m integration tests/integration/ -v
```

> 長期解（未做）：讓 `_pvc_keep_post_renderer_args()` 依 helm major 版本切換
> 語法，或在 lifespan 啟動時斷言 helm major == 3 並 fail-loud，把「helm 4 下
> 全套 500」從偵錯題降級成一行啟動錯誤。Refs #449。

## Test isolation

Each test gets its own namespace (`paas-ws-test-op-{uuid8}`) created and torn
down by the `test_namespace` fixture. Failed cleanup never bleeds across
tests because namespaces are always uniquely named.

### ⚠ 在 woow-k3s 上跑會留下 Released PV 孤兒（docker-desktop 不會）

刪 namespace **不會**回收 PV。woow-k3s 的預設 StorageClass 是 `longhorn`，
其 `reclaimPolicy` 是 **`Retain`**——所以測試建的每顆 PVC 在 ns 消失後都會
留下一顆 `Released` PV ＋ 一顆 Longhorn volume。這正是 #319 那批孤兒的來源。

docker-desktop 上看不到這個問題（預設 SC 是 Delete 政策），所以**只在跨叢集
驗證時才會踩到**。

跑完務必收尾（先 patch 成 `Delete` 再刪，讓 CSI 連後端 volume 一起回收；
直接刪 PV 只會移掉 k8s 物件、Longhorn volume 仍在）：

```bash
# 列出本次測試留下的（只認 paas-ws-test-op-* 的 claimRef，不要誤傷他人）
kubectl get pv -o json | python3 -c "
import sys,json
for p in json.load(sys.stdin)['items']:
    ns=(p['spec'].get('claimRef') or {}).get('namespace','')
    if ns.startswith('paas-ws-test-op-'):
        print(p['metadata']['name'])"

# 逐顆 patch 成 Delete —— PV controller 隨即自動回收（之後 delete 會 NotFound，正常）
kubectl patch pv <name> -p '{"spec":{"persistentVolumeReclaimPolicy":"Delete"}}'
```

驗收：`kubectl get volumes.longhorn.io -n longhorn-system | wc -l` 應回到跑測試前的數字。
（2026-07-29 #423 W4 實測：一次全套跑完留下 8 顆，patch 後全數自動回收、Longhorn volume 數回到基線。）

## Known gap: Cloudflare-backed endpoints have no integration coverage

The Security Access tunnel/route endpoints
(`PUT /api/tunnels/{id}/routes`, `POST /api/routes/availability`,
tunnel create/delete) are currently exercised **only by mocked unit tests**
(`tests/test_tunnel_routes_sync.py`, `tests/test_route_availability.py`).
They are not in `tests/integration/`, which today is K8s/Helm-oriented
(`test_scale.py`) and has no Cloudflare credentials.

This is the exact BUG-8-class blind spot the section above warns about:
a change in the Cloudflare API response shape (e.g. the fields
`configure_tunnel_ingress` / `reconcile_tunnel_dns` parse out of
`/cfd_tunnel/.../configurations` or `/dns_records`) would pass the mocked
suite and only surface in production.

Follow-up to close it: add a contract test that runs against a disposable
Cloudflare **sandbox zone** + a throwaway tunnel, gated behind a
`CLOUDFLARE_*` env / pytest marker so a default `pytest` run still skips it
(same pattern as the `integration` marker skipping when `kubectl
cluster-info` fails). Until that exists, treat any change under
`src/services/cloudflare.py` or `src/api/{tunnels,routes}.py` as
contract-risky and verify the live CF response shape by hand.

## Adding a test

1. Pick or write a fixture in `conftest.py` that lays down the K8s state
   you want to exercise (a new workload kind, a different label scheme, …).
2. Use the `app_client` fixture to send the real HTTP request.
3. Assert the response body **and** the resulting K8s state via
   `kubectl get -o jsonpath`.

Avoid mocks. The whole point is to catch the things the mocks hide.
