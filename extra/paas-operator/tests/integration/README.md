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

# 取得 operator Dockerfile 釘住的那顆 helm。⛔ 不要用 $(which helm)——理由見「helm v4
# 陷阱」那節。版本從 Dockerfile 讀（不寫死在本文件裡），平台依 uname 決定，已抓過就跳過。
# ⚠ 本段在本檔的兩個可執行區塊裡**逐字重複**，讓兩個區塊都能各自單獨整段複製；改一處
#   必須同步另一處（兩份必須 byte-for-byte 相同）。
cd "$(git rev-parse --show-toplevel)/extra/paas-operator" || echo "⛔ 這裡不是本 repo 的工作區" >&2
HELM_VERSION=$(sed -n 's/^ARG HELM_VERSION=//p' Dockerfile)
if [ -z "$HELM_VERSION" ]; then
    echo "⛔ 讀不到 Dockerfile 的 ARG HELM_VERSION（cwd=${PWD}）——這不是 helm 的問題，是路徑不對" >&2
    false   # 讓 $? 非 0。刻意不用 exit：這段是設計成貼進互動 shell 的，exit 會把視窗關掉
else
    HELM_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')"
    HELM_BINARY="/tmp/helm-${HELM_VERSION}/${HELM_PLATFORM}/helm"
    [ -x "$HELM_BINARY" ] || { mkdir -p "/tmp/helm-${HELM_VERSION}" && curl -fsSL \
        "https://get.helm.sh/helm-v${HELM_VERSION}-${HELM_PLATFORM}.tar.gz" \
        | tar xz -C "/tmp/helm-${HELM_VERSION}"; }
    "$HELM_BINARY" version --short   # 應與 Dockerfile 的 ARG HELM_VERSION 一致
fi

# Run
HELM_BINARY="$HELM_BINARY" pytest -m integration tests/integration/ -v
```

**45 支收集、其中 4 支**在沒有 KubeVirt/CDI CRD 的叢集（例如 docker-desktop）會自我
skip。數字會漂，要重新確認就跑 `pytest -m integration tests/integration/ --collect-only -q`
（純收集，不碰叢集）。今天在 docker-desktop 上實測 skip 的逐支是：

- `test_kubevirt_crd_contract.py::TestVirtualMachineLifecycle::test_patch_run_strategy_round_trip`（KubeVirt CRD）
- `test_kubevirt_crd_contract.py::TestDataVolumeOrphanLabeling::test_list_and_label_datavolumes`（CDI CRD）
- `test_scale.py::TestScaleMixedTopology::test_vm_only_release_halts_via_endpoint`（KubeVirt CRD）
- `test_scale.py::TestScaleMixedTopology::test_mixed_deployment_and_vm_scales_both`（KubeVirt CRD）

⚠ **本段原本寫「41 tests，其中 2 支 skip」——兩個數字都不對。** 漏數的是 `test_scale.py`
那兩支：它們從 `test_kubevirt_crd_contract` **import** `_skip_or_fail` 來用
（`test_scale.py:169` 的 import、`:263` 與 `:290` 的呼叫），所以不住在
`test_kubevirt_crd_contract.py` 這個檔名底下，用「掃檔案」的方式數就會漏掉。

⚠⚠ **比數字錯更危險的是那個 41 的口徑**：它是**總數**，而 docker-desktop 上的實際
結果是 `41 passed, 4 skipped`——**passed 數剛好等於舊敘述的總數**。照舊敘述讀的人看到
`41 passed` 會直接結論「全部跑完、沒有東西被 skip」，實際上有 4 支從未執行。兩個 41 是
不同口徑的巧合，不是對帳成功。⇒ **不要用總數相減、也不要拿 passed 數去對這行敘述來判斷
有沒有 skip**；要知道 skip 了哪幾支就看 `-v` 的逐支輸出（`SKIPPED`），要知道原因就加
`-rs`。skip 被誤讀成通過是這裡的主要風險。

要真正驗到 VM 路徑，用有 CRD 的叢集並帶 `REQUIRE_KUBEVIRT=1`（見下方表格）：那會把 skip
升級成 fail，正是為了不讓「驗收 VM 路徑」被一個安靜的 skip 冒充通過。

#461 新增兩支：`test_helm_secret_reset_returns_before_pods_are_ready`
量「reset 不等 rollout」的牆鐘；`test_atomic_failure_rolls_secret_back_within_hard_cap`
強制一次 `--atomic` 失敗，驗 rollback 真的跑完而非被 subprocess 上限腰斬——這兩件事
只能在真叢集量到，mock 掉 kubectl 之後「有沒有等」只剩「有沒有呼叫某方法」。
The `app_client` fixture sets a real
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
# 取得 operator Dockerfile 釘住的那顆 helm。⛔ 不要用 $(which helm)——理由見「helm v4
# 陷阱」那節。版本從 Dockerfile 讀（不寫死在本文件裡），平台依 uname 決定，已抓過就跳過。
# ⚠ 本段在本檔的兩個可執行區塊裡**逐字重複**，讓兩個區塊都能各自單獨整段複製；改一處
#   必須同步另一處（兩份必須 byte-for-byte 相同）。
cd "$(git rev-parse --show-toplevel)/extra/paas-operator" || echo "⛔ 這裡不是本 repo 的工作區" >&2
HELM_VERSION=$(sed -n 's/^ARG HELM_VERSION=//p' Dockerfile)
if [ -z "$HELM_VERSION" ]; then
    echo "⛔ 讀不到 Dockerfile 的 ARG HELM_VERSION（cwd=${PWD}）——這不是 helm 的問題，是路徑不對" >&2
    false   # 讓 $? 非 0。刻意不用 exit：這段是設計成貼進互動 shell 的，exit 會把視窗關掉
else
    HELM_PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')"
    HELM_BINARY="/tmp/helm-${HELM_VERSION}/${HELM_PLATFORM}/helm"
    [ -x "$HELM_BINARY" ] || { mkdir -p "/tmp/helm-${HELM_VERSION}" && curl -fsSL \
        "https://get.helm.sh/helm-v${HELM_VERSION}-${HELM_PLATFORM}.tar.gz" \
        | tar xz -C "/tmp/helm-${HELM_VERSION}"; }
    "$HELM_BINARY" version --short   # 應與 Dockerfile 的 ARG HELM_VERSION 一致
fi

# Any reachable cluster
HELM_BINARY="$HELM_BINARY" KUBECONFIG=~/.kube/config \
    pytest -m integration tests/integration/

# Against a kind cluster you've already created
kind create cluster --name op-int
HELM_BINARY="$HELM_BINARY" pytest -m integration tests/integration/
```

If `kubectl cluster-info` fails the suite skips itself (no setup penalty
for a default `pytest` run, which also defaults to `-m "not integration"`
via `pytest.ini`).

`HELM_BINARY` is required because the operator's FastAPI lifespan refuses
to start when helm is missing. macOS Homebrew puts helm at
`/opt/homebrew/bin/`, so the default `/usr/local/bin/helm` is wrong there.
⚠ 但**不要因此把它指向 Homebrew 那顆**——那顆已經是 v4（見下一節），指過去會壞得
很難認。要指的是上面區塊抓下來的那顆 3.13.3。

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

上面「macOS quickstart」與「Generic invocation」兩個區塊**已經內建這個做法**（從
`Dockerfile` 的 `ARG HELM_VERSION` 讀版本、依 `uname` 決定平台、抓到暫存目錄後把
`HELM_BINARY` 指過去），照抄那兩個區塊即可。這裡刻意**不再重複第三份指令**——同一份
文件裡有兩套寫法，正是本節在講的那類問題。

同場加映：想跑 docker-desktop 又不想動使用者當前的 kubectl context（例如
context 停在 `woow-k3s`），可以匯出一份只含該 context 的 kubeconfig 再指過去，
比 `kubectl config use-context` 少一個「忘了切回去」的風險：

```bash
kubectl config view --raw --minify --flatten --context=docker-desktop \
    > /tmp/kubeconfig-dd.yaml
# $HELM_BINARY 由上面 quickstart／Generic invocation 區塊設好
KUBECONFIG=/tmp/kubeconfig-dd.yaml HELM_BINARY="$HELM_BINARY" \
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
