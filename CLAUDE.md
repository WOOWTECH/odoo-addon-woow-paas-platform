# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`woow_paas_platform` is an Odoo 18 addon module providing the foundation for a multi-tenant PaaS application. The module includes:

- **Standalone OWL Application** - Independent frontend at `/woow` with hash-based routing
- **Configuration Settings** - System settings via `res.config.settings`
- **Menu Structure** - Root menu "Woow PaaS" ready for child items
- **Claude Code PM** - Project management integration

### Cloud Services

允許使用者透過 Kubernetes 與 Helm 部署容器化應用。

**Key Components**:

- **CloudAppTemplate** model - Application marketplace templates (AnythingLLM, n8n, PostgreSQL, etc.)
- **CloudService** model - Deployed service instances with lifecycle management
- **PaaS Operator Service** - FastAPI service at `extra/paas-operator/` that wraps Helm CLI operations
- **Marketplace UI** - OWL components for browsing and deploying applications

**Architecture**:

```
┌─────────────┐      ┌──────────────┐      ┌──────────────┐
│   Odoo      │─────▶│ PaaS Operator│─────▶│ Kubernetes   │
│  (Frontend) │ HTTP │   (FastAPI)  │ Helm │ + User Pods  │
└─────────────┘      └──────────────┘      └──────────────┘
```

## paas-platform 開發

- 平台開發是在 Odoo 框架上開發 addon，加速 PaaS 平台。**儘量不使用 Odoo 原生網頁**，以 `/woow` 為入口（如 `/my` 等 Odoo 原生頁面租戶不會用到）。
- 已有 addon dependencies 不要重造輪子，除非要修 bug 或補原始 addon 沒有的功能。
- Odoo ERP 與 PaaS 共存（Odoo 同時供官方網站使用）。
- `k8s-dev-sandbox` skill 可設定在 `woow-k3s` 或 `docker-desktop` context 管理 sandbox 環境。

## kubectl

兩個 context：

- `woow-k3s`：woow prod / stg cluster（需要 tailscale vpn 才可連線）
- `docker-desktop`：本機測試、開發用

**IMPORTANT**: kubectl 請明確指定 `--context`。

## git

PR / Issue / Comment 描述一律使用 zh-tw 繁體中文。

## Browser Automation

E2E 測試依場景選工具（皆 `--headed`，看得到瀏覽器）：

1. **UX 流程 / 操作類**（點擊、填表、導航）→ `playwright-cli`（`open --headed`）
2. **UI 畫面渲染 / 視覺驗證**（截圖、CSS）→ `agent-browser --headed`

本地 e2e / 整合測試一律走 `k8s-dev-sandbox` skill，不要用 docker compose。

## Development Commands

### Run Odoo with this addon

```bash
# From odoo-server root
./odoo-bin -c odoo.conf -u woow_paas_platform
```

### Update module after changes

```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```

### Run tests for this module

```bash
# Odoo module tests
./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init

# PaaS Operator unit tests (mocked k8s, default pytest skips -m integration)
cd extra/paas-operator
pytest tests/ -v --cov=src
```

#### PaaS Operator Integration Tests (real K8s)

Catch contract drift that unit-test mocks hide (BUG-7/8/9 class — workload-type, HTTP
contract, RBAC). **No CI runner** — these must run locally before merging changes under
`extra/paas-operator/`.

```bash
cd extra/paas-operator

# Pick a cluster context (docker-desktop for dev, woow-k3s for cross-env validation)
kubectl config use-context docker-desktop

# First time only: install deps
pip install -r requirements.txt

# Run (~10s on a clean cluster, 7 tests)
HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v
```

| 變數                           | 為什麼                                                                                                                |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| `HELM_BINARY`                  | macOS Homebrew 把 helm 放在 `/opt/homebrew/bin/`，operator 預設找 `/usr/local/bin/helm`，沒給就 lifespan startup 失敗 |
| `API_KEY` / `NAMESPACE_PREFIX` | 不用設，`app_client` fixture 自動 set（`""` + `"paas-ws-test-op-"`）                                                  |

Cluster 殘留風險：測試用唯一 namespace `paas-ws-test-op-{uuid8}`，正常 teardown 會刪；中斷或 cleanup 失敗會留下，需要時手動 `kubectl delete ns paas-ws-test-op-<x>`。詳細請見 `extra/paas-operator/tests/integration/README.md`。

### K8s Dev Sandbox (Recommended)

本機開發與測試的首選方式（namespace 隔離、可跑測試、產出可存取環境）。

> **已移植成 `woowprd` plugin 通用工具**（2026-06；支援任意 Odoo addon repo 三樣態：src 單一 / repo 根即 addon / 多 addon 子目錄）。
> 有 `woowprd` plugin 時**優先用 `/woowprd:k8s-dev-sandbox <command>`**（需要被測 repo 的子命令要帶 `--repo "$(git rev-parse --show-toplevel)"`，且 `create`/`destroy` 必帶 `--context`；詳見 plugin 的 SKILL.md）。
> 平台特化 seed 已外置成 repo 自帶 `.sandbox/{pre-install,post-create}.sh` opt-in hook。
> 設計與實作見 `docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-{design,implementation}.md`。

部署獨立的開發沙盒環境到 Kubernetes（plugin 通用版，推薦）：

```bash
# 建立 / 快速建立當前 branch（--context 必填）
/woowprd:k8s-dev-sandbox create --context woow-k3s [--ttl 48h --pgadmin]
/woowprd:k8s-dev-sandbox up --context docker-desktop

# 列出 / 狀態 / 日誌 / 測試 / 延長 TTL / 銷毀 / 建構映像
/woowprd:k8s-dev-sandbox list
/woowprd:k8s-dev-sandbox status [<sandbox-name>]
/woowprd:k8s-dev-sandbox logs <sandbox-name> [--service odoo|postgres|nginx] [--follow]
/woowprd:k8s-dev-sandbox test <sandbox-name> [--module <module>]
/woowprd:k8s-dev-sandbox extend <sandbox-name> [--ttl 336h]
/woowprd:k8s-dev-sandbox destroy <sandbox-name> --context woow-k3s [--keep-data]
/woowprd:k8s-dev-sandbox build [--registry <registry>] [--push]
```

<details><summary><b>in-repo 保留的 woow 專屬 e2e / 測試工具</b>（沙盒生命週期已全移至 plugin；<code>deploy-stg</code> 已改委派 plugin create.sh）</summary>

完整沙盒管理（create/destroy/list/status/logs/build）已移除 → 用 `/woowprd:k8s-dev-sandbox`。in-repo 僅保留未進通用 plugin 的 woow 專屬工具：

```bash
# 沙盒內跑模組測試（綁 woow_paas_platform）
./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> [--module <module>] [--keep-on-fail]
# 延長 TTL（供 test --keep-on-fail）
./.claude/skills/k8s-dev-sandbox/scripts/extend.sh <sandbox-name> [--ttl 336h]
# 內部 helper：e2e skill fixture seeder / 從 stg clone DB+filestore
./.claude/skills/k8s-dev-sandbox/scripts/e2e-seed.sh <slug> [--reset|--purge]
./.claude/skills/k8s-dev-sandbox/scripts/seed-from-stg.sh
```

</details>

### PaaS Operator Local Development

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

### Release & Build paas-odoo Image

發版時會自動觸發 `paas-odoo-ci` repo 打包含有最新 addon 的 Odoo Docker image。

**Cross-repo CI 流程：**

```
woow_paas_platform (push v* tag)
  → .gitea/workflows/trigger-odoo-build.yml
    → Gitea API dispatch
      → paas-odoo-ci/.gitea/workflows/build-image.yml
        → clone addon at tag → build & push paas-odoo image
```

**發版步驟：**

使用 Claude Code 可直接執行 `/woow:deploy`，會自動讀取版號、檢查 tag、確認後打 tag 並 push。

手動發版：

```bash
# 1. 更新版本號 src/__manifest__.py 中的 version
#    格式: {odoo_version}.{major}.{minor}.{patch}

# 2. Commit & push
git add src/__manifest__.py
git commit -m "release: 18.0.1.0.24"
git push

# 3. 打 tag 並 push（自動觸發 paas-odoo-ci build）
git tag v18.0.1.0.24
git push origin v18.0.1.0.24
```

**Tag 命名規則：**

| 項目                              | 格式                                     | 範例            |
| --------------------------------- | ---------------------------------------- | --------------- |
| Addon version (`__manifest__.py`) | `{odoo_version}.{major}.{minor}.{patch}` | `18.0.1.0.24`   |
| Git tag (woow_paas_platform)      | `v{完整 version}`                        | `v18.0.1.0.24`  |
| paas-odoo image tag               | `YYYYMMDD.HHmm`（自動產生）              | `20260328.1430` |

> **Note**: Git tag 直接使用 `__manifest__.py` 的完整版號加 `v` 前綴，不做拆解。paas-odoo image tag 使用時間戳，因為它是多個 addon 的組合打包，不應綁定單一 addon 的版本。

**前置條件：**

`woow_paas_platform` repo 的 Gitea Settings → Actions → Secrets 需設定 `CI_GITEA_TOKEN`，該 token 的擁有者須同時具備 `odoo-addons` 和 `woow-paas` org 的 Write 權限（scope: `repository: Read and Write`）。

### Deploy Staging (deploy-stg)

部署 staging 環境到 K8s dev sandbox。完整流程為：`tea` CLI 建立 tag 觸發 CI → 輪詢 CI 狀態等待完成 → 呼叫 `create.sh` 部署 sandbox。

**前置條件：**

```bash
# 設定 tea login（需有 repo + write:packages scope）
# Token 產生方式：Gitea UI → Settings → Applications → Generate New Token
tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>
```

**使用方式：**

```bash
# 方法 1：使用 Claude Code skill（推薦）
/woow:deploy-stg                         # 使用最新 git tag 部署
/woow:deploy-stg v18.0.1.0.24             # 指定 addon ref
/woow:deploy-stg --slug stg-test         # 自訂 sandbox 名稱（namespace: dev-sandbox-stg-test）

# 方法 2：直接執行腳本
./.claude/skills/deploy-stg/scripts/deploy-stg.sh
./.claude/skills/deploy-stg/scripts/deploy-stg.sh v18.0.1.0.24 --slug stg
```

**跳過 CI 建置（用已存在的 image）：**

```bash
# 直接指定完整 image（docker tag 格式，不含 stg- 前綴）
/woow:deploy-stg --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:20260329.1500

# 跳過建立 tag，僅輪詢已存在 git tag 的 CI 狀態 + 部署
./.claude/skills/deploy-stg/scripts/deploy-stg.sh --skip-build --tag stg-20260329.1500
```

**驗證與管理：**

```bash
# 查看 staging pod 狀態
kubectl get pods -n dev-sandbox-stg

# 查看日誌（plugin 通用版；無 plugin 時用 ./.claude/skills/k8s-dev-sandbox/scripts/logs.sh）
/woowprd:k8s-dev-sandbox logs stg --service odoo --follow

# 銷毀 staging（plugin 通用版；無 plugin 時用 ./.claude/skills/k8s-dev-sandbox/scripts/destroy.sh）
/woowprd:k8s-dev-sandbox destroy stg --context woow-k3s
```

**CI 流程圖：**

```
deploy-stg.sh
  → Gitea API: GET .../branches/main → commit SHA
  → Gitea API: POST .../tags → create stg-* git tag
    → paas-odoo-ci: build-image.yml (triggered by stg-* tag)
      → push jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:{docker_tag}
  → Gitea API: GET .../actions/tasks → poll CI status (30s interval, 15min timeout)
  → create.sh --branch stg --image {full_image}
    → helm install → dev-sandbox-stg namespace
```

**Image 命名規則：**

| 項目               | 格式                                   | 範例                |
| ------------------ | -------------------------------------- | ------------------- |
| STG image repo     | `woow-paas-docker-local/paas-odoo-stg` | -                   |
| Git tag（觸發 CI） | `stg-YYYYMMDD.HHMM`                    | `stg-20260329.1500` |
| Docker image tag   | `YYYYMMDD.HHMM`（CI strip `stg-`）     | `20260329.1500`     |
| Sandbox namespace  | `dev-sandbox-{slug}`                   | `dev-sandbox-stg`   |

## Architecture

### Directory Structure

```
woow_paas_platform/
├── src/                          # Odoo addon 源碼（models / controllers / OWL app at /woow，數量見各目錄）
│   ├── __manifest__.py
│   ├── controllers/              # HTTP / JSON API（paas、billing、dashboard、ai_assistant、smart_home、profile、ha_api、oauth2、agent_runtime_skill、agent_runtime_webhook）
│   ├── models/                   # Workspace、CloudService、Wallet/Billing、AI、MCP、OAuth2、SmartHome、Subscription、Project/Account...
│   ├── views/                    # backend XML views + menu
│   ├── data/                     # demo data、cron、subscription products、templates
│   ├── security/                 # ir.model.access.csv、ir_rules.xml
│   └── static/src/paas/          # Standalone OWL App（layout / pages / components / services / core/router.js / styles）
├── extra/
│   ├── paas-operator/            # FastAPI service 包 Helm CLI（含 helm chart）
│   └── cloudflare/               # Cloudflare 整合
├── charts/odoo-dev-sandbox/      # K8s dev sandbox Helm chart
├── scripts/                      # 本機輔助腳本（保留；本機環境請改用 k8s-dev-sandbox）
├── docs/                         # 6 類 docs（guides / reference / decisions / plans / tracking / archived + superpowers）
└── .claude/skills/               # Project skills（ccpm、k8s-dev-sandbox、deploy-stg、deploy-local、build-operator、ecpay、playwright-cli、setup-end-to-end）
```

詳細結構與檔案列表已交由其他文件維護，避免雙寫漂移：

- 模組總覽：`README.md`
- 子系統 spec：`docs/reference/features/`
- API 規格與路由：`docs/reference/api/`（或直接 grep `@route` / 看 `src/static/src/paas/core/router.js`）
- 架構決策：`docs/decisions/`
- docs 6 類規則：`docs/README.md`

### Standalone OWL Application (`/woow`)

The module provides an independent frontend application:

**Entry Point**: `src/controllers/paas.py` → `/woow`
**Template**: `src/views/paas_app.xml`
**Assets**: `woow_paas_platform.assets_paas` bundle

### API Endpoints

實際 endpoints 分散在 `src/controllers/` 多個檔案：

| Controller                 | 範圍                                                                                    |
| -------------------------- | --------------------------------------------------------------------------------------- |
| `paas.py`                  | 主入口 `/woow`、cloud-services 端點（templates、services、plans、trial、addon-removal） |
| `billing.py`               | wallet、payment-methods、subscriptions、invoices、topup                                 |
| `dashboard.py`             | 首頁 summary                                                                            |
| `ai_assistant.py`          | AI providers / agents、chat、support                                                    |
| `smart_home.py`            | smart home 模板與裝置                                                                   |
| `profile.py`               | `/api/me` partner self-update                                                           |
| `ha_api.py`                | Home Assistant API proxy                                                                |
| `oauth2.py`                | OAuth2 server endpoints                                                                 |
| `agent_runtime_skill.py`   | agent-runtime skill bundle（`/api/agent-runtimes/skill-bundle`）                        |
| `agent_runtime_webhook.py` | agent-runtime 狀態回呼（`/api/agent-runtimes/webhook/state-update`）                    |

完整端點與 OWL hash-based 路由詳見 `docs/reference/api/`，或直接 grep `@route` 與 `src/static/src/paas/core/router.js`。

### K8s Dev Sandbox

透過 Helm 部署獨立的 Odoo 開發環境到 Kubernetes。

**Architecture**:

```
┌─────────────────────────────┐
│   K8s Namespace              │
│   (dev-sandbox-{branch})     │
│                              │
│  ┌──────┐  ┌──────┐        │
│  │ Odoo │  │  PG  │        │
│  │ :8069│  │ :5432│        │
│  └──┬───┘  └──────┘        │
│     │                       │
│  ┌──┴───┐  ┌──────┐        │
│  │Nginx │  │PgAdmin│(opt)  │
│  │ :80  │  │ :5050 │       │
│  └──────┘  └───────┘       │
└─────────────────────────────┘
```

**兩種模式**：

- **Local 模式**：使用 hostPath mount 實現即時程式碼編輯
- **CI-Local 模式**：程式碼打包進 CI image，addons 路徑同 local（`/mnt/extra-addons/`）
- **STG 模式**：程式碼打包進 Bitnami image，addons 路徑為 `/opt/bitnami/odoo/custom-addons/`

**主要功能**：

- 每個 branch 獨立的 namespace 隔離
- TTL 自動清理（預設 7 天）
- ResourceQuota 與 NetworkPolicy 限制
- 自動化模組安裝

### Odoo Module Patterns

**Adding a new model:**

1. Create `src/models/your_model.py` with class inheriting `models.Model`
2. Add import in `src/models/__init__.py`
3. Add security rules in `src/security/ir.model.access.csv`
4. Create views in `src/views/your_model_views.xml`
5. Update `src/__manifest__.py` data list

**Adding frontend assets:**

1. Put OWL components in `src/static/src/paas/components/`
2. Register in `src/__manifest__.py` under `woow_paas_platform.assets_paas`

**Extending existing models:**

```python
class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'
    # Add fields with config_parameter for system settings
```

**使用 `config_parameter` 時的卸載清理：**

Odoo 卸載 addon 時**不會**自動清除 `ir.config_parameter` 記錄。若欄位使用了 `config_parameter`，必須同步更新 `src/hooks.py` 中的 `_CONFIG_PARAMETERS` 列表，確保卸載時能正確清理。

```python
# src/hooks.py
_CONFIG_PARAMETERS = [
    'woow_paas_platform.operator_url',
    'woow_paas_platform.operator_api_key',
    # 新增 config_parameter 時，務必加到這裡
]
```

## Known patterns

改動以下 model 與 ACL 前必看 invariants 與 pattern docs，避免 B1-class data leak 重炸（完整 18 條 pattern 索引見 [`docs/decisions/patterns/README.md`](docs/decisions/patterns/README.md)）：

- **CloudService / Project decoupling**：見 `docs/decisions/refactor/cloud-service-project-decouple.md`（含 7 條 Invariants）
- **Odoo addon override std ir.rule**（任何在 `ir.model.access.csv` 給 `base.group_user` 加 project / task / Odoo std model 的 RWCU 時）：見 `docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md`
- **PaaS workspace member ACL 收緊**：見 `docs/decisions/patterns/paas-workspace-member-acl-tightening.md`
- **Odoo addon 不 override std field 型態**（任何想動 Odoo std model 欄位型態 / shape 的 PR）：見 `docs/decisions/patterns/odoo-std-field-no-override.md`
- **m2m write constraint grantor authority check**（model 上有 attacker-writable Many2many + ir.rule / `@api.constrains` 用該 m2m 做 subset / authority 推導時）：見 `docs/decisions/patterns/constraint-input-trust-expansion.md`
- **Bundle attachment defense in depth**（自家 model 透過 `ir.attachment` 存敏感 / RCE-class binary payload 時）：見 `docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md`（anchor stamping + ir.attachment ir.rule 兩層）
- **ORM-level test 綠 ≠ endpoint-level secure**（任何改 controller endpoint 的 list / get / stats / aggregation 路徑 + ir.rule-protected model 時；新增 / 改動 ir.rule 收緊某 model 可見性時；寫 backend `TestCase` + `with_user()` 守 ir.rule 的 PR）：見 `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md`（controller layer 不可 `.sudo()` 繞 ir.rule；對等補 HttpCase cross-user negative；含 34 處 `sudo().*` audit grep checklist + 5 instance critical mass + **anti-pattern 三變體 family**：A = over-access leak / B = under-access own-data invisibility / C = same-method 半套 sudo（同 method input drop sudo 但 output 留 sudo，pattern lint future-proof 失效）+ perf justification 章節（sudo() 是 security primitive 不是 perf primitive）)
- **兩層 ACL lock-step**（同一 model 被 ir.rule + controller carve-out，或多個 endpoint 各自 access resolver 守時；任一層改 domain / 收緊，另一層必須同步，否則 silent divergence 比未修更糟）：見 `docs/decisions/patterns/two-layer-acl-lockstep.md`
- **Unbound（可選綁定）model 的 dual-leg ir.rule**（model 有 nullable M2O 到 workspace-bearing model、unbound row 仍須對部分 user〔creator / follower〕可見時；leg 1 bound walk + leg 2 unbound carve-out，跨 workspace 仍 deny）：見 `docs/decisions/patterns/unbound-project-acl-dual-leg.md`
- **Mixed-scope model 的 6-rule split**（同 model 混 platform public-read + workspace-scoped 兩種 scope、且需在 `base.group_user` 開 CRUD 時；按 perm direction 拆 3 條 + sub-model 鏡像，single dual-leg 不夠〔create direction 有提權面〕）：見 `docs/decisions/patterns/mcp-acl-6-rule-split.md`
- **Scope / tenancy flip 必靠 write() guard（非 ir.rule）**（model 有 scope（system/user）/ project_id / public 等決定可見範圍的欄位、且對 `base.group_user` 可寫時；要擋「窄 scope→寬 scope flip」提權 / 跨租戶外洩，ir.rule 對 **pre-write** 狀態判定擋不住，必須在 model `write()` 攔 vals + 放行 sudo/admin；與 6-rule split 互補：split 守 create+可見性、guard 守 flip）：見 `docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md`
- **Identity / 不可變 invariant guard 別用 env.su / group 豁免**（守不可變 identity anchor〔`subdomain_prefix` / `canonical_name` / `reference_id`，hostname / DNS / tunnel / FQDN 從它推導、無合法事後改值路徑〕的 model `create()` / `write()` guard 時）：**不可**寫 `not env.su` / `not has_group()` 豁免——`TransactionCase` 是 su env，deny 分支跑不到 → **假綠** + 任何 `sudo()` 路徑靜默繞過；改 context-key escape hatch（如 `sa_internal_identity_write`）無條件擋所有人，且 create（pop 注入）＋ write（擋改值）雙向對稱覆蓋。與 scope-flip 分野：那條值可 gated 改（`env.su`+admin 放行對的），本條值不可變（任何 su 豁免都是洞）。見 `docs/decisions/patterns/su-env-exempt-guard-false-green.md`
- **多段式 remote-resource provision 必須冪等**（provision method 先建遠端資源〔CF tunnel / 雲端 DNS / SaaS 帳號 / k8s ns〕再跑後續步驟、且 UI 把 retry 掛在 `error` 上時）：第二段失敗讓遠端資源孤兒掛著、retry 重跑第一段在雲端再建一條 = 永久孤兒 / 計費外洩；遠端建立步驟必須被 **stored-id guard** 包住（method 開頭判 remote id 已存在 → 只重跑後續不重建），每條失敗路徑保留 stored id 讓 retry 安全，`AccessError` 安全訊號 propagate 不降級。單段 method 的免-guard 安全是單段性換來的，加段就要買回來。見 `docs/decisions/patterns/two-stage-remote-provision-idempotency.md`
- **Phase leader brief 是 pointer，不是 source-of-truth**（適用 maestri 多 phase agent pod 工作流；非 paas-platform 直接相關，是 agent hand-off 通用紀律）：見 `docs/decisions/patterns/phase-leader-brief-as-pointer-not-source.md`（agent 接 brief 撞到與 plan signed-off / decision doc invariant / schema 實際 shape 不符 → trust source + 回報 leader 校正 brief，不照 brief 字面開工；與 [[plan-gap-stop-and-report]] 區分：那條 agent 執行中主動發現 gap、本條接任務時被動 detect brief drift）
- **Seeded 平台預設 agent_template 的 tenant-neutral invariant**（動 `src/data/agent_template_default.xml`、新增任何 seeded platform-default template、或改 `src/hooks.py:_ensure_default_agent_template` 時必看）：`agent_template_default`（`is_public=True` + `state='published'` + 不綁 project / 不綁任何 user-scope skill·mcp_server）是平台 cluster-wide fallback；`post_init_hook` 開機時把 `ir.config_parameter['woow_paas_platform.default_agent_template_id']` **未設才指、不覆寫 admin 已選**。它經 `_resolve_agent_template` 的 `.sudo()` 廣播給**所有沒有自己 template 的 project、跨所有 tenant**，故**任何 seeded default 必須維持 tenant-neutral**（unbound + 無 user-scope 綁定），否則把某租戶綁定外洩給全體。與 `agent_template_demo`（`state='draft'`、inert、註解明文 must-stay-draft、stg 上被手動 publish+綁 project）**分工不同、勿互相 repurpose**。設計依據見 `docs/superpowers/specs/2026-06-01-seed-agent-template-design.md`

Pitfall 持久化紀錄（debug 撞牆時對照）：

- `docs/tracking/pitfalls/cross-workspace-task-data-leak.md` — CAQA B1 incident
- `docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md` — plan 引用不存在欄位的教訓
- `docs/tracking/pitfalls/orm-test-green-endpoint-leak-blindness.md` — plan §X「後端已完整支援」掃描盲區 + backend ORM test 全綠 → 遮蔽 controller layer sudo bypass；直到 playwright cross-user TC4 / CAQA grep audit 才暴露。**ORM 綠 ≠ endpoint secure** 守線（cycle 6 decouple-project frontend：`_list_projects` + `_list_tasks` + `_get_project_stages` + `api_support_stats` 4 instance 同 family 在 controller layer 漏修；是 [[orm-vs-endpoint-acl-test-coverage]] pattern 的 symptom-side 敘事）
- `docs/tracking/pitfalls/ci-workflow-container-missing-node.md` — CI `container: <lang-image>` + `actions/checkout` → node not found (exit 127) → pytest 從沒跑到（false green）。**cross-repo CI infra learning**（agent-runtime-pod epic Phase B，`paas-agent-runtime` repo Gitea Actions；任何用 container image 跑 GitHub/Gitea Actions workflow 的 repo 都適用，本 repo Odoo CI 不受影響）
- `docs/decisions/patterns/phase-versioned-test-assertion.md` —（pattern doc，列此作 debug 對照）phase test 硬編當時 schema 快照值（manifest version `== "x"` / constraint 名 / 欄位 count）→ 後續 phase 合法演進同塊 schema 時撞 **test-vs-test red**（紅的 test 名與真因無關，debug 成本高）。改用 monotonic `>=` / 存在性斷言鎖 intent。**multi-phase schema test discipline**（agent-runtime-pod epic 內第二次撞牆抽：Cycle 1 `test_ac2` UNIQUE + Phase C M.9 manifest version；epic Phase D-F 版號 / schema 續演進會再撞）
- `docs/decisions/patterns/credential-claim-test-fixture-coupling.md` —（pattern doc，列此作 debug 對照）硬化共用 credential 路徑（token 加 required claim / secret·api_key optional→required fail-closed / 收緊 verify）→ 所有**自建該 credential 走此路徑的 test fixture** 都 forward-coupled，須 lock-step 補 claim/secret，否則紅（好：暴露 coupling）或**假綠**（壞：test 繞過被硬化的 gate → 回歸無守線）。**credential 硬化 test discipline**（agent-runtime-pod epic Phase B `_sign` / Phase C client-webhook fixture / Phase D `e94991d` 3 建立點；跨 repo 自建點易漏，Phase E/F 續硬化會再撞）
- `docs/tracking/pitfalls/cross-repo-artifact-landing-assumption.md` — plan 假設某部署 artifact（chart / secret mount / manifest）落 cross-repo X，實際落本 repo Y → impl 前**勘查實體落點** catch（按「修改誰的 deployment」定位 chart，非按 feature 名）。**cross-repo 落點教訓**（agent-runtime-pod epic Phase D：Odoo secret env mount plan 假設落 `woow-paas-charts`，實際本 repo `charts/odoo-dev-sandbox`；epic Phase E-G 續動 charts/CD 跨 repo 適用）
- `docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md` — agent-runtime 在 stg 啟用的 **5 層 fail-closed gap chain**：operator image 過舊 → `use_agent_runtime` flag 關 → project 無 agent_template → `AGENT_RUNTIME_NAMESPACE_SECRET` 未注入 → NetworkPolicy 沒開 agent pod 路徑。**單層修復不可觀測**（前層還擋著），須逐層驗到端到端；含「K3s NP 回 RST（refused）非 timeout，別用 refused 排除 NP」的辨錯 + 重現 checklist（PR #110 持久化 #4/#5 進 create.sh，deploy-stg 須帶 `AGENT_RUNTIME_*` env）。**stg 環境啟用教訓**（2026-05-31 debug session；operator 走 `paas-app-bundle-cd` Fleet 非 deploy-stg）。（gap #3「project 無 agent_template」對新環境已由 `agent_template_default` seed + `post_init` auto-point 緩解，見上方 Known patterns「Seeded 平台預設 agent_template 的 tenant-neutral invariant」條）
- `docs/tracking/pitfalls/default-agent-template-project-bind-silent-autoenable.md` — **平台預設 agent_template 被綁到 project → auto-enable 靜默全失效**。Settings toggle + operator 都設對了，但 `default_agent_template_id` 指向的那筆 template 被綁了 project → `_platform_default_agent_template` 的 `not project_ids` 跨租戶 guard 判它不合格 → `_agent_runtime_autoenable_ready` 第 3 條永遠 False → 每個新 project **無錯誤訊息地**不 auto-enable（chat-time `_resolve_agent_template` ④ 同步回空 → `no_template`）。是上面 gap-chain gap #3 緩解措施（seed default）被打破的失敗模式：**該綁 project 測試的是 `agent_template_demo`（draft），不是 `agent_template_default`（必須永遠 unbound）**，本 incident 把兩者搞反。含一鍵 readiness gate 診斷 + 解綁修法。程式守衛 PR #125 加 `agent_template._check_not_platform_default_when_bound`（綁定當下 fail loud，不對 superuser 放行）。**stg auto-enable 靜默失效教訓**（2026-06-02 debug session）。
- `docs/tracking/pitfalls/agent-runtime-coldstart-cpu-throttle-timeout.md` — **agent-runtime cold-start spawn ~36s 撞 client timeout，根因是 operator pod CPU limit `250m` throttle（不是同步等 pod-ready）**。`helm install` 無 `--wait`、`agent_runtimes.py:389`「readiness poll」只是單次非阻塞 `get_pods` → operator 不等 ready；cold-start path 連續 fork ~5 個 helm/kubectl（每個被 250m throttle 到 5–17s、累加 ~36s）> 修前 client `_TIMEOUT=30`。佐證 throttle 非節點競爭：operator pod 平時 ~11m、node CPU 0–1%。正式 SSE 因 controller commit session → 首訊息 spawn_failed、retry 即成、**無孤兒**；odoo-shell rollback 才留孤兒空 ns。修：① #123 client per-call spawn timeout 90s + retry + spawning 進度（治標）② PR `woow-paas/paas-app-bundle-cd#1` operator CPU `250m→1000m`（stg+prod，**必走 Fleet CD values**）→ **stg 實測 cold-start 36.1s→11.3s（<30s）**。**agent-runtime cold-start perf 教訓**（2026-06-02 verify session）。
- `docs/decisions/patterns/plan-gap-stop-and-report.md` —（pattern doc，列此作 debug 對照）agent 執行 leader hand-off / 連修 e2e gap chain 時，遇 **plan gap**（手上沒來源資料能填、是沒設計過的決策點）或 **test 假設錯**（mock 對象 / 期望值與真實 API 不符）→ **停下回報 + escalate，別硬塞假值掩蓋 design gap、別 bend impl 湊綠**（假性往下走會把缺口埋進更深下游、或把 prod crash 湊成 test-green）。**plan-gap-stop-and-report 守線**（agent-runtime-pod epic 反覆生效：pilot 3 plan-gap〔registry / LLM cred / probe〕+ Phase E0 T4.1a impl/test 層延伸；cycle/phase 多次）
- `docs/tracking/pitfalls/floor-env-relative-path-empty-ext-addons.md` — **maestri Floor 內建 dev sandbox：`.env` 的 `PAAS_CI_ADDONS_PATH` 相對路徑解析錯（Floor 比主 repo 深三層）→ 恰好掛到存在但空的目錄騙過 create.sh 檢查 → module 靜默裝不起來**（create.sh 只 warning 不中止）。Floor 的 `.env` 一律用絕對路徑；建完 sandbox 先驗 `ir_module_module.state='installed'` 再開工。同場另撞 F-GC-2（create.sh fresh `--set` 洗掉 agentRuntime secret keys）+ 本機 operator 過舊 — 本機 dev-sandbox 的 agent-runtime E2E **預期只驗到 spawn 邊界**，完整串流走 merge 後 deploy-stg（見 gap-chain pitfall 2026-06-05 本機變體節）。**Floor sandbox 環境教訓**（2026-06-05 portal-task-agent-chat E2E）
- `docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md` — **seed record 用 `noupdate="1"` → Odoo 把 flag 寫進 `ir.model.data` 變 sticky → 其他 data 檔（即使自己 `noupdate="0"`）在 `-u` 對同一 record 的更新被靜默 skip**（不報錯不 log）。SA `security_access_default` 偏離鄰居 `noupdate="0"` 慣例 → `subscription_template_links.xml` 的 M2M 寫入永遠跳過 → PlanSelector 顯示「no plans configured」。**fresh install 綠 ≠ 升級路徑綠**（install mode 套全部檔案、單元測試自建 record 都遮蔽，只在「先裝後升」DB 暴露＝stg/prod 真實路徑）。修：seed 改 `noupdate="0"` + `<function>` healer 翻正既有 sticky DB（commit `ebe6ec1`）。**sticky noupdate 升級路徑教訓**（2026-06-07 Security Access P1 UI 驗證）
- `docs/tracking/pitfalls/owl-template-crash-class-syntax.md` — **OWL template 語法錯誤（component 上 `t-att-`、Vue 按鍵 modifier `.enter`）過 XML well-formed + 全部 Python 測試，runtime render 直接摧毀整個 OWL root → `/woow` hash 導航全站白屏**（root 毀後所有 route 空白到 F5）。三層全綠是因為 parse 只驗 well-formed、Python 測試不跑 JS、bundle build 不 compile template。OWL≠Vue：component prop 已是 expression（直接 `name="expr"` 不加 `t-att-`）；按鍵判斷一律 handler 內讀 `ev.key`（無 `.enter`/`.esc`，`t-on-*.<mod>` 只支援 `.stop/.prevent/.self/.capture`）。唯一攔截網＝headed 瀏覽器實走＋console error 零容忍（本 repo 無 JS 測試框架）。修 commit `ebe6ec1`/`ada231f`。**OWL render 假綠教訓**（2026-06-07 Security Access P1 Phase 5）
- `docs/tracking/pitfalls/operator-cf-access-delete-202-mock-contract.md` — **`paas-operator` 包 Cloudflare Access 的 DELETE 把 HTTP 狀態白名單寫死 `200`/`(200,404)`，但真 CF 的 Access app/policy DELETE 回 `202 Accepted`（success:true）→ 誤判失敗 raise → teardown「先刪 app 再刪 policy」在刪 app 就中斷 → 孤兒 reusable policy**。所有 mock 單元/整合測試全綠（`fake_delete` 寫死 200），**只有 live 真 Cloudflare E2E（直接 `kubectl exec` 打 operator `PUT /api/access/sync` 走 create→resync→teardown）才暴露**。連帶：resync「刪舊 policy 前沒先脫鉤」→ CF 拒刪 in-use → 孤兒；best-effort cleanup 只 catch `CloudflareException`（暫態 httpx 錯逸出 → 已收斂 sync 失敗 → Odoo savepoint rollback → DB↔CF 分歧）；新 async 測試漏 `@pytest.mark.asyncio` 靜默 skip 假綠。守線：**operator 包外部 API 的 mock 狀態碼要對齊該 API 真實回值**（CF Access DELETE=202、CF DNS/tunnel DELETE=200〔P1 live 實證〕）；先脫鉤再刪舊；best-effort cleanup catch `Exception` 且回報 `orphaned_*` 訊號。修 PR #166（image `20260608.0541`），live 重驗 teardown 全綠、CF 零殘留。token 前置：Access 寫路徑需**帳戶級** Access:Apps&Policies(Edit)（非區域級）。**operator 外部 API delete 契約假綠教訓**（2026-06-08 Security Access P2 live stg E2E）

Follow-up 追蹤：`docs/tracking/followups/follow-ups.md`（隨 cycle 持續累積；最新見該檔開頭 State 段）

## Docs organisation

`docs/` 採 **6 類混合 + 1 工具自管理區**（詳見 `docs/README.md`）。新檔前先決定類別：

| 目錄                | 何時用                                                                                          |
| ------------------- | ----------------------------------------------------------------------------------------------- |
| `docs/guides/`      | 寫「如何做 X」的步驟（onboarding、deployment、testing）                                         |
| `docs/reference/`   | 寫「是什麼 / 有什麼」的事實規格（API、feature spec）                                            |
| `docs/decisions/`   | 寫「為什麼這樣設計」的論述（philosophy、refactor、pattern、PR review）                          |
| `docs/plans/`       | 啟動 epic / 大功能前寫 design+implementation 雙檔（按季度子目錄歸檔，例 `docs/plans/2026-Q2/`） |
| `docs/tracking/`    | 記錄 incident、pitfall、follow-up、test report                                                  |
| `docs/archived/`    | 不再使用但保留考古（不主動建檔）                                                                |
| `docs/superpowers/` | obra/superpowers plugin 自動寫入區（不算主題類）                                                |

**工具指令**：

- `/docs:new <category> <name>` — 建新 doc（產 frontmatter 模板）
- `/docs:promote ccpm:<feature-name>` 或 `/docs:promote superpowers:<file>` — 把 ccpm epic / superpowers 探索 promote 到主結構

**CI 策略**：

- Phase 1（目前）：lychee broken link check，PR 觸發，失敗只 warning
- Phase 2（穩定後）：升級為 block merge + orphan doc check
- 例外清單：`docs/archived/`、`docs/superpowers/`

## Code Style Guide

### Indentation

- JavaScript / OWL / XML / SCSS: 2 spaces
- Python: 4 spaces (PEP 8)

### Python

```python
# Model naming
class WoowSubscription(models.Model):
    _name = 'woow_paas_platform.subscription'
    _description = 'PaaS Subscription'

# Field definitions - use keyword arguments
name = fields.Char(
    string='Name',
    required=True,
    help='Brief description for users',
)
```

### JavaScript/OWL

```javascript
/** @odoo-module **/

import { Component } from "@odoo/owl";

export class SubscriptionCard extends Component {
  static template = "woow_paas_platform.SubscriptionCard";
  static props = {
    subscription: Object,
  };
}
```

### SCSS

```scss
// Use .o_woow_ prefix for isolation
.o_woow_subscription_card {
  &__header {
  }
  &__body {
  }
  &--active {
  }
}
```

### XML IDs

```xml
<!-- Pattern: {module}.{type}_{model}_{view_type} -->
<record id="view_subscription_form" model="ir.ui.view">
```

## Dependencies

### Odoo Module

- Odoo version: 18.0
- Odoo core modules: `base`, `web`, `project`, `mail`, `bus`
- Third-party addons: 權威清單見 `src/__manifest__.py` 的 `depends`（含 ai_base_gt、odoo_ai_assistant_chatgpt_connector、sh_subscription 系列、ecpay 系列等）
- External fonts: Google Fonts (Manrope, Outfit), Material Symbols

### PaaS Operator Service

- Python 3.11+
- FastAPI
- Helm 3.13+
- kubectl 1.28+
- Kubernetes cluster access

### Deployment

- Kubernetes 1.28+ (K3s recommended for self-hosted)
- PostgreSQL 14+ (for Odoo)
- Cloudflare DNS (or custom domain for Ingress)

## Framework integration

本 repo 同時用三套 Claude Code framework，各自 scope 與整合點不同。**詳細使用指南漸進載入** — 需要哪套再讀對應檔，不要一次全載：

| Framework                                                                                                                      | Scope / 安裝形式                                | 主要用途                                                                             | 詳細指南                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| **ccpm**（[automazeio/ccpm](https://github.com/automazeio/ccpm)）                                                              | Project skill — symlink `.claude/skills/ccpm`   | spec-driven PM：PRD → Epic → GitHub issues → 平行 agents                             | [`docs/guides/frameworks/ccpm.md`](docs/guides/frameworks/ccpm.md)               |
| **superpowers**（[obra/superpowers](https://github.com/obra/superpowers)）                                                     | User-scope plugin（SessionStart hook 自動注入） | 16+ skills：brainstorming / writing-plans / TDD / debugging / code-review / worktree | [`docs/guides/frameworks/superpowers.md`](docs/guides/frameworks/superpowers.md) |
| **SuperClaude_Framework**（[SuperClaude-Org/SuperClaude_Framework](https://github.com/SuperClaude-Org/SuperClaude_Framework)） | User-scope `~/.claude/commands/sc/*`            | 30+ slash commands；本 repo 只用 `/sc:brainstorm` + ad hoc 其他                      | [`docs/guides/frameworks/superclaude.md`](docs/guides/frameworks/superclaude.md) |

### 共用整合點：`/docs:promote`

ccpm 或 superpowers 的長期價值產出，透過 `/docs:promote` 升級到 `docs/` 主結構（6 類，見上方 Docs organisation）：

```
/docs:promote ccpm:<feature-name>           # 從 .claude/epics/ 或 .claude/prds/
/docs:promote superpowers:<filename>        # 從 docs/superpowers/{specs,plans}/
```

### 新 feature 探索入口：`/brainstorm-sc-to-ccpm`

`/sc:brainstorm`（SuperClaude）多 persona 探索 → 塑形 ccpm-compliant PRD → 進 ccpm tree：

```
/brainstorm-sc-to-ccpm <feature-slug>
```

### 不啟用清單

- **`/sc:pm`**（SuperClaude）— 預設寫 `docs/{temp,patterns,pdca,mistakes,checklists}/`，與本 repo 6 類結構衝突；PM 工作流由 ccpm 承擔
- **ccpm v1**（散置 `agents/` `commands/pm/` `rules/` `scripts/pm/`）— 已遷移到 v2 Agent Skill，舊 context dir archive 到 `docs/archived/ccpm-v1/context/`

### 三套之間的關係

- **ccpm** 是主 PM 工具（owns `.claude/prds/` + `.claude/epics/`）
- **superpowers** 是輔助工具集（owns `docs/superpowers/` 工具自管理區）
- **SuperClaude** 是 ad hoc command 庫（產出多在 `claudedocs/` git-ignored，唯 `/sc:brainstorm` 透過 `/brainstorm-sc-to-ccpm` 接 ccpm）
- **superpowers `brainstorming`** 與 **`/sc:brainstorm`** 功能重疊：本 repo 統一走 `/brainstorm-sc-to-ccpm`（SuperClaude 路徑，與 ccpm schema 對齊較好）
