---
name: decision-agent-runtime-operator-chart-install-contract
status: accepted
created: 2026-05-25
updated: 2026-05-25
phase: agent-runtime-pod epic — gap C
team: feature-agent
deciders:
  - agent-pod-leader (phase-leader)
  - paas-platform (operator source code)
  - paas-charts (helm chart 發佈)
related:
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
  - docs/plans/2026-Q2/agent-runtime-pod-plan.md
---

# Decision: agent-runtime chart 的 operator install 契約（gap C）

> 適用：paas-operator 動態 spawn per-project agent-runtime pod 時，要如何 `helm install` 一個發佈在**私有 Gitea classic Helm repo** 的 chart。這份記錄 install 機制、operator code 改動、env / values 契約，以及跨 lane 分工。

## Status

Accepted（2026-05-25，使用者拍板放行）。operator code 已落地（主 clone `develop`，102 unit tests green，未 commit）；端到端 stg 驗證待 gap B（chart 發佈）+ read-cred secret 就位。

## Context

epic 要讓 operator 動態 spawn agent-runtime pod（image 已 build `jcr-prod.woowtech.io/woow-paas-docker-local/agent-runtime:1.0.0`）。pilot 在 stg（`woow-k3s` / operator@`paas-stg-system`）卡住：operator 裝不到 chart。盤點出 4 個 gap C 子問題：

1. `helm.py` 的 `install()` 把 chart 字串原封丟給 `helm install <chart>`，無 `helm repo add`、無 registry 機制。
2. `config.py` 的 `agent_runtime_chart` 預設 `agent-runtime`（plain name）、`agent_runtime_image_tag` 預設 `latest`（但只 build 了 `1.0.0`，`latest` 不存在）。
3. operator pod Dockerfile/lifespan 只裝 helm binary、無 repo add / registry login。
4. chart 發佈在 Gitea Helm registry（in-cluster `gitea-http.gitea.svc.cluster.local:3000`，純 HTTP）。

### 關鍵勘查結論（paas-charts 提供鐵證）

- **OCI 在本環境不可用**：`woow-paas-charts` 的 `publish-charts.yaml` 只走 classic Helm repo API（`POST /api/packages/woow-paas/helm/api/charts`，ChartMuseum-style）。OCI 曾加過（commit `03171dd`）但 redirect loop 失敗（試過 internal/external URL + `--insecure` / `--insecure-skip-tls-verify`，**未試 `--plain-http`**），最終 commit `5079cc1` 停用；README 宣稱支援 OCI 是 stale。
- **pull 需 auth**：`woow-paas` 是 private org，實測匿名拉 `index.yaml` = HTTP 401。
- **既有 marketplace** 全用 public HTTPS 匿名 OCI ref（bitnami / 8gears，見 `src/data/cloud_app_templates.xml`）— operator 對 OCI ref install 已驗證，但**從未**做過 repo add / registry login / `--plain-http`。

## Decision

**走 classic Helm repo + 無狀態 `helm install --repo`**（不走 `helm repo add`）：

```
helm install agent-pa-{hash} agent-runtime \
  --repo http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm \
  --version 0.1.0 \
  --username <gitea-user> --password <read:package-token> \
  -n paas-ws-pa-{hash} --create-namespace
```

理由：

- **stateless** 最契合 operator 的動態 per-project spawn — 無 `repositories.yaml` / index 快取狀態；每次 spawn 抓 fresh index → chart 一發佈即可見，pilot 迭代 chart 免 `helm repo update`。
- 對 Gitea 啟動時宕機有韌性（不卡 lifespan repo add，個別 spawn 失敗自然重試）。
- classic repo over 純 HTTP 直接可用，**不需 `--plain-http`**（那是 OCI-only）。
- auth 相容：`--repo` 也吃 `--username/--password`，私有 repo 也能 stateless。
- 改動最小且向後相容。

### operator code 改動（3 source + 2 test 檔；lifespan / Dockerfile 零變動）

- `src/services/helm.py`：`install()` / `upgrade()` 加 optional `repo` / `username` / `password` 參數，**僅在設值時** append `--repo` / `--username` / `--password`（`--version` 既有）。預設 `None` → 既有 marketplace OCI install 行為完全不變。
- `src/config.py`：新增 `agent_runtime_repo_url` / `agent_runtime_chart_version` / `agent_runtime_repo_username` / `agent_runtime_repo_password`（預設空、env 驅動）；`agent_runtime_image_tag` `latest`→`1.0.0`。
- `src/api/agent_runtimes.py`：`_chart_source_kwargs()` helper（單處記錄此契約），`spawn-or-attach` install + `sync-workspaces` upgrade 兩處帶入。
- 測試：`tests/test_helm.py` 3 RED（`--repo`/`--version`/auth）+ 2 向後相容 GUARD（不帶 repo → args 不含 `--repo`/`--username`/`--password`）；`tests/test_agent_runtimes.py` 2 wiring（settings → install/upgrade kwargs）。

### operator deployment env 契約（gap A 須提供）

| env | 值（stg） | 對應 helm |
|-----|----------|-----------|
| `AGENT_RUNTIME_CHART` | `agent-runtime` | chart name |
| `AGENT_RUNTIME_REPO_URL` | `http://gitea-http.gitea.svc.cluster.local:3000/api/packages/woow-paas/helm` | `--repo` |
| `AGENT_RUNTIME_CHART_VERSION` | `0.1.0` | `--version`（**chart version**，非 image tag） |
| `AGENT_RUNTIME_REPO_USERNAME` | Gitea user | `--username` |
| `AGENT_RUNTIME_REPO_PASSWORD` | `read:package` token | `--password` |
| `AGENT_RUNTIME_IMAGE_TAG` | `1.0.0` | values `image.tag`（appVersion） |
| `ODOO_BASE_URL` | `http://stg-odoo-dev-sandbox-odoo.dev-sandbox-stg.svc.cluster.local:8069` | values `env.ODOO_BASE_URL` |

> ⚠️ read-cred（`REPO_USERNAME`/`PASSWORD`）是 operator deployment 的**獨立新 secret**，**不可**與 Phase G 那 5 個注入 Odoo 的凍結 env（`AGENT_RUNTIME_AUTH_SECRET_*` / `WEBHOOK_*` / `NAMESPACE_SECRET`）混為一談 — 不同目的地、不同時程、scope 只需 `read:package`。

### agent-runtime chart values 契約（gap B chart 須接收，design §2.11）

operator 的 `_build_install_values` 送：

```yaml
image:
  tag: <AGENT_RUNTIME_IMAGE_TAG>      # e.g. 1.0.0
projectHash: <hash>
env:                                  # 須渲染成 pod 環境變數
  ODOO_BASE_URL: <ODOO_BASE_URL>
  PROJECT_HASH: <hash>
  SKILL_BUNDLE_TOKEN: <signed jwt>
networkPolicy:
  egressWorkspaceIds: [<int>, ...]
```

`sync-workspaces` 的 `helm upgrade` 只送 `networkPolicy.egressWorkspaceIds`（`--reuse-values`）。

## 測試與環境

- operator 測試須用 `extra/paas-operator/.venv`（**Python 3.11**）跑：
  `cd extra/paas-operator && .venv/bin/python -m pytest tests/ --ignore=tests/integration`
- ⚠️ 本機預設 `python` 若是 3.9，**無法 collect** 任何 import `src.main` 的測試（`test_agent_runtimes` / `test_api` / `test_auth_fail_closed` …）：既有 `src/services/odoo_init.py` 用 PEP 604 `X | None` 型別語法，需 Python 3.10+。這是**既有環境限制、非 gap C 改動**；用 3.9 會誤判測試壞掉（collection error）。`test_helm.py` 因只 import `src.services.helm`（用 `Optional[...]`）在 3.9 也能跑。
- gap C 驗證結果：完整 unit suite **102 passed**（含新增 5 個 `test_helm` + 2 個 `test_agent_runtimes` wiring 測試）。

## Consequences

- ✅ 向後相容：marketplace OCI install 路徑零影響（GUARD test 鎖死）。
- ✅ lifespan / Dockerfile 零變動，保留 operator「就緒」狀態。
- 🔗 **NetworkPolicy ns 修正**（gap B chart）：pod 的網路對端只有 **Odoo（`dev-sandbox-stg`）** — pod egress→Odoo（skill-bundle fetch + webhook callback）、ingress←Odoo（WS proxy 連 `:8080`）。operator（`paas-stg-system`）用 k8s API 管 pod、**不是** pod 網路對端。原 chart 寫死 `paas-system` 應改成 `dev-sandbox-stg`（兩 ns 都有穩定 `kubernetes.io/metadata.name` label 可選）。
- 🔗 **部署機制**：`paas-operator-stg` = Fleet 管 Helm chart（`managed-by=Helm` + `objectset.rio.cattle.io/hash` + `paas-operator-0.1.8`）→ gap A bump `0.1.8`→`0.1.9` 加 env，Fleet GitRepo 同步到 `paas-stg-system`。
- ⛔ **e2e 前置依賴**：(1) gap B — agent-runtime chart 目前 untracked、**從沒發佈過**，paas-charts 須 commit→merge main→實測發佈成功；(2) read-cred secret 須在 operator deployment 就位。

## 跨 lane 分工

| gap | owner | 內容 |
|-----|-------|------|
| C（本決策） | paas-platform | operator install code（stateless `--repo` + auth） |
| A | paas-charts / woow-paas-cd | `paas-operator` chart 加 7 env + read-cred secret + writable helm cache（若 `readOnlyRootFilesystem`） |
| B | paas-charts | `agent-runtime` chart 發佈到 Gitea classic repo + 補 values（env / odoo url / image / networkPolicy）+ NetworkPolicy ns 參數化 |
| D | (待 leader 分派) | values / ODOO_BASE_URL / egress 實測 |

## 替代方案（未採用）

- **OCI `oci://`**：本環境 push 端已驗證壞掉（redirect loop，commit `5079cc1`），pilot 不賭未驗證的修復。`--plain-http` 救活可能性留作後續優化。
- **lifespan `helm repo add`（stateful）**：需維護 `repositories.yaml` + index 快取、chart 升版要 `helm repo update`、Gitea 啟動時宕機卡 startup — 對動態 spawn 不利。
