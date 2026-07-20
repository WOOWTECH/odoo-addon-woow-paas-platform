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

### ⛔ 鐵律：woow-k3s 是共用叢集，本 repo（paas 平台開發/維運）的 kubectl 只能操作以下 namespace

> 適用範圍＝**paas 平台相關的開發與維運**（本 repo 及其派出的 agent/workflow）。叢集**全域**維運屬 `woow-machines-operator` agent 職責（可完整操作所有 ns）——需要動非下表 namespace 時，透過該 agent / 叢集運維處理，不要在本 repo 事務中自己動手。

| Namespace | 用途 |
| --- | --- |
| `paas-ws-*` | prod 環境中的平台租戶 |
| `paas-ws-stg-*` | stg 環境中的平台租戶 |
| `paas-system` | prod 環境中的 paas 平台系統 |
| `paas-stg-system` | stg 環境中的 paas 平台系統 |
| `paas-cloudflare-system` | prod 環境中的 cloudflare tunnel（主要用在 ingress） |
| `paas-stg-cloudflare-system` | stg 環境中的 cloudflare tunnel（主要用在 ingress） |
| `dev-sandbox-*` | 開發時的 dev sandbox |

其餘 namespace 屬叢集上其他部署（客戶實例、gitea、longhorn-system 等），**絕對不碰**。跨 ns 唯讀盤點（get/list）僅供歸屬分流——非上述 namespace 的資源只列不動，嚴禁任何寫入/刪除；叢集級設定（default StorageClass、Longhorn settings 等）影響全叢集，動之前須與叢集運維協調。（實例教訓：2026-07-08 盤點 48 顆 Released PV，其中 ~39 顆屬其他部署，差點被當平台殘留清理——見 issue #319。）

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

# Run (21 tests)
HELM_BINARY=$(which helm) pytest -m integration tests/integration/ -v
```

| 變數                           | 為什麼                                                                                                                |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| `HELM_BINARY`                  | macOS Homebrew 把 helm 放在 `/opt/homebrew/bin/`，operator 預設找 `/usr/local/bin/helm`，沒給就 lifespan startup 失敗 |
| `API_KEY` / `NAMESPACE_PREFIX` | 不用設，`app_client` fixture 自動 set（`"integration-test-key"` + `"paas-ws-test-op-"`）                                                  |

Cluster 殘留風險：測試用唯一 namespace `paas-ws-test-op-{uuid8}`，正常 teardown 會刪；中斷或 cleanup 失敗會留下，需要時手動 `kubectl delete ns paas-ws-test-op-<x>`。詳細請見 `extra/paas-operator/tests/integration/README.md`。

### K8s Dev Sandbox (Recommended)

本機開發與測試首選（namespace 隔離、TTL 自動清理、可跑測試）。**已移植成 `woow-dev` plugin 通用工具（舊名 `woowprd`），優先用 `/woow-dev:k8s-dev-sandbox <command>`**（onboard / create / up / list / status / logs / test / extend / destroy / build；被測 repo 的子命令帶 `--repo "$(git rev-parse --show-toplevel)"`，`create`/`destroy` 必帶 `--context`）。架構、三種模式（local hostPath / ci-local / stg 的 addons 路徑差異）、平台 seed 的 `.sandbox/{pre-install,post-create}.sh` opt-in hook 詳見 plugin 的 SKILL.md；移植設計見 `docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-{design,implementation}.md`。

in-repo `.claude/skills/k8s-dev-sandbox/` 僅剩未進通用 plugin 的 woow 專屬工具：`test.sh`（沙盒內跑模組測試，`--keep-on-fail`）、`extend.sh`（TTL）、`e2e-seed.sh` / `seed-from-stg.sh`（e2e fixture / stg clone helper）。

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

發版用 `/woow:deploy`：讀 `src/__manifest__.py` 版號 → pre-flight → 打 `v{version}` tag → push 觸發 cross-repo CI（`trigger-odoo-build.yml` → `paas-odoo-ci` build & push image，image tag 為時間戳、不綁單一 addon 版本）。tag 規則、pre-flight 檢查與前置條件（`CI_GITEA_TOKEN` 雙 org Write）見 [`.claude/commands/woow/deploy.md`](.claude/commands/woow/deploy.md)。

### Deploy Staging (deploy-stg)

部署 staging 到 K8s dev sandbox：`/woow:deploy-stg [ref] [--slug x] [--image ...]`（tea 建 `stg-*` tag 觸發 CI → 輪詢完成 → 委派 plugin create.sh 部署到 `dev-sandbox-{slug}`）。前置：`tea login add --name gitea --url https://git-prod.woowtech.io --token <token>`（repo + write:packages scope）。完整流程、skip-build 用法、image 命名規則見 [`.claude/skills/deploy-stg/SKILL.md`](.claude/skills/deploy-stg/SKILL.md)；日誌 / 銷毀走 `/woow-dev:k8s-dev-sandbox logs|destroy`。

## Architecture

### Directory Structure

```
woow_paas_platform/
├── src/                          # Odoo addon 源碼（models / controllers / OWL app at /woow，數量見各目錄）
│   ├── __manifest__.py
│   ├── controllers/              # HTTP / JSON API（paas、billing、dashboard、ai_assistant、smart_home、profile、ha_api、ha_paas、oauth2、portal、reconcile_report、security_access、agent_runtime_skill、agent_runtime_webhook）
│   ├── models/                   # Workspace、CloudService、Wallet/Billing、AI、MCP、OAuth2、SmartHome、Subscription、Project/Account...
│   ├── views/                    # backend XML views + menu
│   ├── data/                     # demo data、cron、subscription products、templates
│   ├── security/                 # ir.model.access.csv、ir_rules.xml
│   └── static/src/paas/          # Standalone OWL App（layout / pages / components / services / core/router.js / styles）
├── extra/
│   ├── paas-operator/            # FastAPI service 包 Helm CLI（helm chart 已移至 woow-paas-charts repo）
│   └── cloudflare/               # Cloudflare 整合
├── docs/                         # 6 類 docs（guides / reference / decisions / plans / tracking / archived + superpowers）
└── .claude/skills/               # Project skills（ccpm、k8s-dev-sandbox、deploy-stg、deploy-local、build-operator、ecpay、playwright-cli、retro）
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
| `ha_paas.py`               | HA PaaS 0 元開通（`/api/ha-paas/provision`、`/api/ha-paas/status`）                     |
| `oauth2.py`                | OAuth2 server endpoints                                                                 |
| `portal.py`                | 原生 portal 頁擴充（project/task view values 注入 agent 卡片；無自有 route）            |
| `reconcile_report.py`      | 對帳報表（`/api/reconcile-report/list`、`/api/reconcile-report/reverify`）              |
| `security_access.py`       | workspace Security Access CRUD 與 routes（`/api/workspaces/<id>/security-accesses`…）  |
| `agent_runtime_skill.py`   | agent-runtime skill bundle（`/api/agent-runtimes/skill-bundle`）                        |
| `agent_runtime_webhook.py` | agent-runtime 狀態回呼（`/api/agent-runtimes/webhook/state-update`）                    |

完整端點與 OWL hash-based 路由詳見 `docs/reference/api/`，或直接 grep `@route` 與 `src/static/src/paas/core/router.js`。

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

改 model / ACL / lifecycle / seed / OWL template 前的守線知識採兩層供給，不雙寫在本檔：

1. **pattern-guard hook（機器強制）**：編輯守護路徑時 hook 自動列出必讀 docs，讀完重試即放行；對照表 `.claude/hooks/pattern-guard-map.json`（新增守線在此 append）。
2. **完整索引（人工查閱）**：[`docs/decisions/patterns/README.md`](docs/decisions/patterns/README.md)「何時必看」表。

唯二留本檔的高頻 invariant：

- **CloudService / Project decoupling**：7 條 Invariants 見 `docs/decisions/refactor/cloud-service-project-decouple.md`。
- **Seeded `agent_template_default` 必須 tenant-neutral**（unbound + 無 user-scope 綁定；經 sudo 廣播為全租戶 fallback）。綁定當下有 code guard fail-loud（`src/models/agent_template.py:125`）；設計依據 `docs/decisions/patterns/seed-agent-template-design.md`；與 `agent_template_demo`（draft、可綁 project 測試）分工勿互換。

可機械判定的守線改進 `scripts/audit/repo-lint.py`（gate 比散文強）。

Pitfall 持久化紀錄（debug 撞牆時對照）：完整索引見 [`docs/tracking/pitfalls/README.md`](docs/tracking/pitfalls/README.md)（一行症狀 + 機械化狀態）。其中已機械化者撞到 gate / guard 時訊息內附 doc 指針，不需預先記憶。

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
