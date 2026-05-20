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
./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init

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

| 變數 | 為什麼 |
|------|-------|
| `HELM_BINARY` | macOS Homebrew 把 helm 放在 `/opt/homebrew/bin/`，operator 預設找 `/usr/local/bin/helm`，沒給就 lifespan startup 失敗 |
| `API_KEY` / `NAMESPACE_PREFIX` | 不用設，`app_client` fixture 自動 set（`""` + `"paas-ws-test-op-"`） |

Cluster 殘留風險：測試用唯一 namespace `paas-ws-test-op-{uuid8}`，正常 teardown 會刪；中斷或 cleanup 失敗會留下，需要時手動 `kubectl delete ns paas-ws-test-op-<x>`。詳細請見 `extra/paas-operator/tests/integration/README.md`。

### Docker Development (Recommended)

使用 Worktree Development 腳本來管理開發環境：

```bash
# 啟動開發環境（自動設定 .env 並啟動 Docker）
./scripts/start-dev.sh

# 執行測試
./scripts/test-addon.sh

# 清理環境
./scripts/cleanup-worktree.sh
```

詳細說明請參考下方 [Worktree Development](#worktree-development) 章節。

**Test URL**: http://localhost (NOT :8069, to enable websocket)

### K8s Dev Sandbox

部署獨立的開發沙盒環境到 Kubernetes：

```bash
# 為當前 branch 建立沙盒 (--context required, see fix/sandbox-scripts-context-safety)
./.claude/skills/k8s-dev-sandbox/scripts/create.sh --context woow-k3s

# 列出所有活躍的沙盒
./.claude/skills/k8s-dev-sandbox/scripts/list.sh

# 檢查沙盒狀態
./.claude/skills/k8s-dev-sandbox/scripts/status.sh <sandbox-name>

# 查看日誌
./.claude/skills/k8s-dev-sandbox/scripts/logs.sh <sandbox-name> [--service odoo|postgres|nginx] [--follow]

# 在沙盒中執行測試
./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> [--module <module>]

# 延長沙盒 TTL
./.claude/skills/k8s-dev-sandbox/scripts/extend.sh <sandbox-name> [--ttl 336h]

# 銷毀沙盒
./.claude/skills/k8s-dev-sandbox/scripts/destroy.sh <sandbox-name>

# 建構 CI 映像
./.claude/skills/k8s-dev-sandbox/scripts/build.sh [--registry <registry>] [--push]
```

或使用 Claude Code skill：`/k8s-dev-sandbox <command>`

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

| 項目                              | 格式                                     | 範例             |
| --------------------------------- | ---------------------------------------- | ---------------- |
| Addon version (`__manifest__.py`) | `{odoo_version}.{major}.{minor}.{patch}` | `18.0.1.0.24`    |
| Git tag (woow_paas_platform)      | `v{完整 version}`                         | `v18.0.1.0.24`   |
| paas-odoo image tag               | `YYYYMMDD.HHmm`（自動產生）               | `20260328.1430`  |

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

# 查看日誌
./.claude/skills/k8s-dev-sandbox/scripts/logs.sh stg --service odoo --follow

# 銷毀 staging
/k8s-dev-sandbox destroy stg
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

| 項目 | 格式 | 範例 |
|------|------|------|
| STG image repo | `woow-paas-docker-local/paas-odoo-stg` | - |
| Git tag（觸發 CI） | `stg-YYYYMMDD.HHMM` | `stg-20260329.1500` |
| Docker image tag | `YYYYMMDD.HHMM`（CI strip `stg-`） | `20260329.1500` |
| Sandbox namespace | `dev-sandbox-{slug}` | `dev-sandbox-stg` |

## Architecture

### Directory Structure

```
woow_paas_platform/
├── src/                          # Odoo addon 源碼（35+ models, 8 controllers, OWL app at /woow）
│   ├── __manifest__.py
│   ├── controllers/              # HTTP / JSON API（paas、billing、dashboard、ai_assistant、smart_home、profile、ha_api、oauth2）
│   ├── models/                   # Workspace、CloudService、Wallet/Billing、AI、MCP、OAuth2、SmartHome、Subscription、Project/Account...
│   ├── views/                    # backend XML views + menu
│   ├── data/                     # demo data、cron、subscription products、templates
│   ├── security/                 # ir.model.access.csv、ir_rules.xml
│   └── static/src/paas/          # Standalone OWL App（layout / pages / components / services / core/router.js / styles）
├── extra/
│   ├── paas-operator/            # FastAPI service 包 Helm CLI（含 helm chart）
│   └── cloudflare/               # Cloudflare 整合
├── charts/odoo-dev-sandbox/      # K8s dev sandbox Helm chart
├── scripts/                      # Worktree 開發腳本（start-dev、test-addon、setup-worktree-env、cleanup）
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

| Controller | 範圍 |
|------|------|
| `paas.py` | 主入口 `/woow`、cloud-services 端點（templates、services、plans、trial、addon-removal） |
| `billing.py` | wallet、payment-methods、subscriptions、invoices、topup |
| `dashboard.py` | 首頁 summary |
| `ai_assistant.py` | AI providers / agents、chat、support |
| `smart_home.py` | smart home 模板與裝置 |
| `profile.py` | `/api/me` partner self-update |
| `ha_api.py` | Home Assistant API proxy |
| `oauth2.py` | OAuth2 server endpoints |

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

改動以下 model 與 ACL 前必看 invariants 與 pattern docs，避免 B1-class data leak 重炸：

- **CloudService / Project decoupling**：見 `docs/decisions/refactor/cloud-service-project-decouple.md`（含 7 條 Invariants）
- **Odoo addon override std ir.rule**（任何在 `ir.model.access.csv` 給 `base.group_user` 加 project / task / Odoo std model 的 RWCU 時）：見 `docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md`
- **PaaS workspace member ACL 收緊**：見 `docs/decisions/patterns/paas-workspace-member-acl-tightening.md`

Pitfall 持久化紀錄（debug 撞牆時對照）：

- `docs/tracking/pitfalls/cross-workspace-task-data-leak.md` — CAQA B1 incident
- `docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md` — plan 引用不存在欄位的教訓

Follow-up 追蹤：`docs/tracking/followups/follow-ups.md`（11 條未竟事項）

## Docs organisation

`docs/` 採 **6 類混合 + 1 工具自管理區**（詳見 `docs/README.md`）。新檔前先決定類別：

| 目錄 | 何時用 |
|------|-------|
| `docs/guides/` | 寫「如何做 X」的步驟（onboarding、deployment、testing） |
| `docs/reference/` | 寫「是什麼 / 有什麼」的事實規格（API、feature spec） |
| `docs/decisions/` | 寫「為什麼這樣設計」的論述（philosophy、refactor、pattern、PR review） |
| `docs/plans/` | 啟動 epic / 大功能前寫 design+implementation 雙檔（按季度子目錄歸檔，例 `docs/plans/2026-Q2/`） |
| `docs/tracking/` | 記錄 incident、pitfall、follow-up、test report |
| `docs/archived/` | 不再使用但保留考古（不主動建檔） |
| `docs/superpowers/` | obra/superpowers plugin 自動寫入區（不算主題類） |

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
- Third-party addons: `ai_base_gt`, `odoo_ai_assistant_chatgpt_connector`, `sh_subscription`, `sh_subscription_wallet`, `payment_ecpay_ecpg`, `ecpay_invoice_tw`
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

| Framework | Scope / 安裝形式 | 主要用途 | 詳細指南 |
|-----------|----------------|---------|---------|
| **ccpm**（[automazeio/ccpm](https://github.com/automazeio/ccpm)） | Project skill — symlink `.claude/skills/ccpm` | spec-driven PM：PRD → Epic → GitHub issues → 平行 agents | [`docs/guides/frameworks/ccpm.md`](docs/guides/frameworks/ccpm.md) |
| **superpowers**（[obra/superpowers](https://github.com/obra/superpowers)） | User-scope plugin（SessionStart hook 自動注入） | 16+ skills：brainstorming / writing-plans / TDD / debugging / code-review / worktree | [`docs/guides/frameworks/superpowers.md`](docs/guides/frameworks/superpowers.md) |
| **SuperClaude_Framework**（[SuperClaude-Org/SuperClaude_Framework](https://github.com/SuperClaude-Org/SuperClaude_Framework)） | User-scope `~/.claude/commands/sc/*` | 30+ slash commands；本 repo 只用 `/sc:brainstorm` + ad hoc 其他 | [`docs/guides/frameworks/superclaude.md`](docs/guides/frameworks/superclaude.md) |

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

## Worktree Development

此專案支援 git worktree 並行測試，每個 worktree 可以獨立運行自己的 Docker 環境。

### 快速開始

```bash
# 1. 建立 worktree
git worktree add ../woow_paas_platform.worktrees/epic-feature -b epic/feature

# 2. 切換到 worktree
cd ../woow_paas_platform.worktrees/epic-feature

# 3. 自動設定環境並啟動
./scripts/start-dev.sh
```

### 環境配置

每個 worktree 會自動配置：

- **唯一的 port**（8069 + 目錄 hash）- 避免衝突
- **獨立的資料庫名稱**（`woow_<branch>`）- 資料隔離
- **獨立的 Docker 容器**（基於 `COMPOSE_PROJECT_NAME`）

所有配置透過 `.env` 檔案管理（由 `scripts/setup-worktree-env.sh` 自動生成）。

### 可用腳本

```bash
# 設定環境變數（.env）
./scripts/setup-worktree-env.sh

# 啟動開發環境
./scripts/start-dev.sh

# 執行測試
./scripts/test-addon.sh

# 清理環境
./scripts/cleanup-worktree.sh
```

### 並行測試範例

同時運行多個 worktree：

```bash
# Terminal 1 - 主專案
cd /path/to/woow_paas_platform
./scripts/start-dev.sh
# → http://localhost:8069 (woow_main)

# Terminal 2 - Feature A
cd ../woow_paas_platform.worktrees/epic-feature-a
./scripts/start-dev.sh
# → http://localhost:8234 (woow_epic_feature_a)

# Terminal 3 - Feature B
cd ../woow_paas_platform.worktrees/fix-bug-123
./scripts/start-dev.sh
# → http://localhost:8501 (woow_fix_bug_123)
```

每個實例使用各自的資料庫，互不干擾。

### 共享 PostgreSQL（可選）

如果希望節省資源，可以讓所有 worktree 共享一個 PostgreSQL 容器：

```bash
# 1. 首次啟動共享資料庫（在主專案目錄執行一次）
docker compose -f docker-compose.shared-db.yml up -d

# 2. 在每個 worktree 設定環境（使用 --shared-db 參數）
./scripts/setup-worktree-env.sh --shared-db

# 3. 啟動（不會建立獨立的 PostgreSQL）
./scripts/start-dev.sh
```

### VS Code 本機開發

使用本機 VS Code（不使用 Dev Container）：

- 每個 worktree 可以開啟獨立的 VS Code 視窗
- Odoo 服務運行在 Docker 容器中
- 支援遠端調試（Python debugger attach to container）

推薦擴充套件已配置在 `.vscode/extensions.json`。

### 重要提醒

- **網址測試**：使用 http://localhost（不是 :8069）以啟用 websocket
- **自動配置**：環境變數由腳本自動生成，請勿手動修改 `.env`
- **資料庫名稱**：基於 branch 名稱，將 `/` 和 `-` 轉為 `_`
- **Port 範圍**：8069-9068（基於目錄路徑 hash 計算）
