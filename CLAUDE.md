# CLAUDE.md

本檔每個 session 自動載入（含 subagent／maestri leader）：只放 always-needed 的守線與指針，branch-specific 內容下沉到被指到的檔。新守線**不寫回本檔**（登錄點見下方 Known patterns；政策 `docs/decisions/harness-distillation.md`；`/retro` 以本檔不變長為硬約束）。

## Project Overview

`woow_paas_platform`：Odoo 18 addon（版號在 `src/__manifest__.py`），多租戶 PaaS 平台。租戶在 `/woow`（standalone OWL app，hash routing）的 marketplace 選 `cloud_app_template`，Odoo 只透過 HTTP 呼叫 `extra/paas-operator/`（FastAPI；Helm／kubectl 操作全在這裡，另負責 Cloudflare DNS／Tunnel）把 `cloud_service` 部署到 K8s——Odoo 端程式碼不直接呼叫 Helm／kubectl。

- 子系統 spec（model、state、UI 流程、架構圖）：`docs/reference/features/cloud-services.md`
- 名詞定義：`CONTEXT.md`；operator API 與架構：`extra/paas-operator/README.md`

## paas-platform 開發

- **儘量不使用 Odoo 原生網頁**：前台功能一律以 `/woow`（standalone OWL app，`src/controllers/paas.py` 的 `@route("/woow")`）為入口，不在 `/web`／`/my` 等原生路由上開新頁面或新曝光點。⚠ 原生路由並未關閉——`/my` portal 與 `/report` PDF 會繞過 `/woow` 的呈現層過濾（見 `docs/decisions/patterns/hidden-template-presentation-chain.md` §定性邊界）；`src/controllers/portal.py` 是唯一刻意擴充原生頁的例外（見下方 API Endpoints）。
- 已有 addon dependencies 不要重造輪子：新增 model／欄位／流程前先確認 `src/__manifest__.py` `depends` 裡的上游 addon（原始碼不在本 repo，本機路徑見 `.sandbox.yml` 的 `PAAS_CI_ADDONS_PATH` 註解）有沒有現成實作，有就用 `_inherit` 擴充（實例 `src/models/ai_assistant.py`、`src/models/sh_subscription_plan_cs_spec.py`）；只有修 bug 或補原始 addon 沒有的功能才自建。
- Odoo ERP 與 PaaS 共存（同一套 Odoo／company 也供官方網站與 ERP 使用）⇒ 不改 company 級或全域預設（`company.auto_invoice`、`company.account_sale_tax_id`、上游 addon 的 hook 行為等），PaaS 需要不同行為時在本 addon 層隔離（實例 `woow_skip_upstream_hook`、seed 明寫 `taxes_id`；依據 `docs/decisions/philosophy/billing-philosophy.md` §5「與上游 ERP 解耦」）。

## kubectl

兩個 context：

- `woow-k3s`：woow prod / stg cluster（需要 tailscale vpn 才可連線）
- `docker-desktop`：本機測試、開發用

**IMPORTANT**: kubectl 請明確指定 `--context`。

跑或改本 repo 的 skill script（`deploy-stg` / `e2e-seed*`）時，kubectl context 由共用守衛 [`.claude/skills/lib/kube-context.sh`](.claude/skills/lib/kube-context.sh)（#32）驗值域並隔離 kubeconfig（不改 `~/.kube/config`）；白名單宣告在 `.sandbox.yml` 的 `allowed_kube_contexts[_<task>]`（與 woow-dev plugin 同鍵），逃生門 `SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1`。機制與理由見該檔檔頭。

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
| `fleet-local`（**僅限 `paas-*` 前綴資源**） | 平台 Fleet 的 GitRepo／Bundle，以及 `valuesFrom` 注入用 secret（`paas-operator-fleet-secrets`、`paas-operator-stg-fleet-secrets`、`paas-odoo-fleet-secrets`…） |

⚠ **`fleet-local` 是本表唯一「按資源名前綴授權」而非整個 ns 授權的項目**——它同時住著叢集上其他部署的 Fleet 資源，`paas-*` 以外的 GitRepo／Bundle／secret 一律不碰；`kubectl-ns-guard` hook 只驗 namespace、不驗資源名，這一層沒有機器守線。為何必須列入與 2026-08-06 補列經緯（`paas-app-bundle-cd` 文件把 `-n fleet-local` 操作寫成標準程序）見 [`docs/guides/deployment/shared-cluster-paas-scope-guard.md`](docs/guides/deployment/shared-cluster-paas-scope-guard.md)（下稱 guard doc）。

其餘 namespace 屬叢集上其他部署（客戶實例、gitea、longhorn-system 等），**絕對不碰**。跨 ns 唯讀盤點（get/list）僅供歸屬分流——非上述 namespace 的資源只列不動，嚴禁任何寫入/刪除；叢集級設定（default StorageClass、Longhorn settings 等）影響全叢集，動之前須與叢集運維協調。起源實例（2026-07-08 Released PV 盤點，#319）與 cluster-wide 掃描的分流／溯源做法見 guard doc。派工給其他 agent／workflow 且會碰 kubectl 時，把上表白名單寫進派工 prompt（hook 只守本 repo 的 Bash 層）。

本鐵律另有 Bash 層機器守線：PreToolUse hook [`.claude/hooks/kubectl-ns-guard.py`](.claude/hooks/kubectl-ns-guard.py) 擋 `--context woow-k3s` 下寫入／exec 類指令的越界 namespace（唯讀盤點不擋；寫入類未帶 `--context` 也擋）。一次性繞過：指令前綴 `KUBECTL_NS_GUARD=off `。白名單三份副本（本表／guard doc／hook 常數）由 repo-lint `kubectl-ns-whitelist-sync` 對帳，改一份必須同步三份。

## git

PR / Issue / Comment 描述一律使用 zh-tw 繁體中文。

PR 由 agent 開（建 branch → push → `tea` 開 PR，標題前綴「⛔ 勿代 merge｜待 <覆核者> 覆核 — 」），**merge 是人工 gate、agent 不代 merge**——與 prod build `/approve` 同級（`.claude/commands/woow/deploy.md` Step 6）。

`src/__manifest__.py` 版號**一般 PR 不動**（必須等於 `origin/develop`）；只有**新增** `src/migrations/<X>/` 的 PR（X＝新版號）與 `release/*` 發版 PR 才 bump——`manifest-version-gate` 是 develop 唯一 required check，三條規則權威在 `scripts/ci/manifest-version-gate.py` docstring（#691）。

PR 的 CI 綠燈**不含 Odoo 測試**（`.gitea/workflows/odoo-tests.yml` 刻意不掛 `pull_request`，只在 merge 進 develop 後與每日 02:00 跑）⇒ 動 `src/` 的 PR 合併前自己在沙盒跑測試（見下方 Run tests for this module），並把結果寫進 PR 描述（#906：綠燈 merge 後 develop 紅）。

## Browser Automation

E2E 測試依場景選工具（皆 `--headed`，看得到瀏覽器）：

1. **UX 流程 / 操作類**（點擊、填表、導航）→ `playwright-cli`（`open --headed`）
2. **UI 畫面渲染 / 視覺驗證**（截圖、CSS）→ `agent-browser --headed`

本地 e2e / 整合測試一律在 K8s Dev Sandbox 內跑（`/woow-dev:k8s-dev-sandbox`，用法見下方 §K8s Dev Sandbox），不要用 docker compose。

E2E／smoke test 產物（截圖、log）先送交使用者或把路徑列進報告，之後才清理；清理只刪自己本次建立的資源。

## Development Commands

### Run tests for this module（一律在沙盒 odoo pod 內）

本 repo 沒有 `odoo-bin`／`odoo.conf`。建沙盒見 README §Quick Start；跑測試 `/woow-dev:k8s-dev-sandbox test <slug> --module woow_paas_platform`（等同 CI 的 `--test-tags /woow_paas_platform`）；只升級不測試、單一 test class 的 pod 內指令見 `docs/guides/testing/TESTING.md` §Run in K8s Dev Sandbox。

新增／改名測試 class 後手改 `scripts/audit/baselines/test-collection.txt`（CI `test-collection-audit` C2 雙向差集會紅；⚠ 用 `scripts/audit/test_collection_audit.py --update-baseline` 重產，**不是** `repo-lint.py --update-baseline`）。

### PaaS Operator Tests

unit tests（與 CI `.gitea/workflows/paas-operator-unit-tests.yml` 同式）：`cd extra/paas-operator && python -m pytest tests/ -m "not integration"`；`pytest.ini` addopts 預設已排除 integration。

#### PaaS Operator Integration Tests (real K8s)

**integration tests 沒有 CI runner（unit 有）** — 改動 `extra/paas-operator/` 合併前必須本機跑 integration tests（真 K8s；unit-test mocks 蓋不住的 contract drift）。完整 recipe（⚠ helm 必須用 3.13.3 不可用系統 helm、`REQUIRE_KUBEVIRT`、殘留 namespace 清理）見 **`paas-operator-dev` skill**（`.claude/skills/paas-operator-dev/SKILL.md`）與 `extra/paas-operator/tests/integration/README.md`。無 Ingress 時本機連 operator（port-forward、API key、Docker 容器改用 `host.docker.internal`）也在同一個 skill。

### K8s Dev Sandbox

本機開發與測試一律走沙盒：用 `woow-dev` plugin `/woow-dev:k8s-dev-sandbox <subcommand>`（舊名 `woowprd`；`create`/`destroy` 必帶 `--context`，需要被測 repo 的子命令帶 `--repo "$(git rev-parse --show-toplevel)"`）。子命令、三種 mode、`.sandbox/` hook 以 plugin 的 SKILL.md 為準；本 repo 的宣告在 `.sandbox.yml`；上手指令見 README §Quick Start。in-repo `woow-e2e-fixtures` skill 只剩 woow 專屬 seed helper（e2e seed／stg clone）；test／extend 已由 plugin 取代（2026-09-04），測試後 e2e seed 走 `.sandbox/post-test.sh` hook（`WOOW_E2E_SEED=1`）。

### Release & Build paas-odoo Image

發版一律走 `/woow:deploy`（[`.claude/commands/woow/deploy.md`](.claude/commands/woow/deploy.md)）：#691 起版號只由 `release/<版號>` 發版 PR bump，merge 後才打 `v{version}` tag → `.gitea/workflows/trigger-odoo-build.yml` dispatch `paas-odoo-ci`（image tag 為時間戳、不綁單一 addon 版本；前置 `CI_GITEA_TOKEN` 雙 org Write）。⚠ **prod build 卡人工核可 gate**：push tag 只跑到「開 approval issue」就停，要授權者留言 `/approve` 才真的 build ⇒ **agent 不代簽——即使人類當場明確授權、即使手上憑證就是授權者本人（裁決無例外）；且 phase 1 綠燈 ≠ 發版完成**。

### Deploy Staging (deploy-stg)

stg 平台換版走 paas-app-bundle-cd 的 `paas-odoo-platform-stg/values-stg.yaml` image pin PR（cd#289）；其他 dev sandbox 才用 `deploy-stg` skill（[`.claude/skills/deploy-stg/SKILL.md`](.claude/skills/deploy-stg/SKILL.md)，`/woow:deploy-stg --slug <name>`）。前置條件、參數、image 命名與 agent 兩段式跑法見該 skill；沙盒日誌／銷毀走 `/woow-dev:k8s-dev-sandbox logs|destroy`。

## Architecture

### Directory Structure

頂層佈局：`src/`（Odoo addon 源碼＋`static/src/paas/` standalone OWL app）、`extra/paas-operator/`（FastAPI 包 Helm CLI；helm chart 已移至 woow-paas-charts repo）、`extra/cloudflare/`、`docs/`、`.claude/skills/`。細節用 `ls` 看即可；詳細結構與檔案列表已交由其他文件維護，避免雙寫漂移：

- 子系統 feature spec：`docs/reference/features/`
- 端點與前端路由：見下方「API Endpoints」（`docs/reference/api/` 只放跨系統協定 contract，不是端點清單）

### Standalone OWL Application (`/woow`)

入口 `src/controllers/paas.py` 的 `@route("/woow")` render `src/views/paas_app.xml`（template id `paas_app`，`t-call-assets` 載入 `woow_paas_platform.assets_paas` bundle）；前端 hash 路由由 `src/static/src/paas/core/router.js` 接手。新元件放 `src/static/src/paas/components/<name>/`（每元件一資料夾，scss 同放）；`assets_paas` 已用 glob 收 `static/src/paas/**`（`src/__manifest__.py`），新元件**不必**登記 manifest。

### API Endpoints

端點權威＝`grep -rn '@route' src/controllers/`（各檔範圍看檔名即明）＋ OWL hash 路由 `src/static/src/paas/core/router.js`；`docs/reference/api/` 只放跨系統協定 contract（agent-runtime WebSocket、HA／Security Access），**不是**端點清單——整站 API 設計稿已 archive（見 `docs/reference/README.md` §子目錄）。唯一例外 `src/controllers/portal.py` **沒有自有 route**——它覆寫原生 `ProjectCustomerPortal` 的 view-value hook 注入 agent 卡片，grep `@route` 找不到它。

### Odoo Module Patterns

新增 Odoo model／OWL component 的逐步 checklist（含程式碼樣板、ACL、manifest、更新指令）→ `docs/guides/getting-started.md` §Adding New Features（該節為權威）。ACL 那步另有 pattern-guard hook 在編輯 `src/security/` 時自動推播必讀 docs。

**使用 `config_parameter` 時的卸載清理：**

Odoo 卸載 addon 時**不會**自動清除 `ir.config_parameter` 記錄；`uninstall_hook` 只刪 `src/hooks.py` `_CONFIG_PARAMETERS` 清單內的 key。新增任何 `woow_paas_platform.*` key 都要登記進該清單，兩種寫法的守線不同：

- `res.config.settings` 欄位的 `config_parameter=`：repo-lint `config-parameter-uninstall` 機械對帳（只認字面量、只查「宣告了沒登記」單向）。
- 「模組常數＋`get_param()`」寫法：lint 掃不到，登記之外要自己補一支 registered-for-uninstall 測試（範例 `src/tests/test_public_url_dns_gate_648.py::TestNewConfigParametersAreRegisteredForUninstall`）。

## Known patterns

改任何守護路徑（權威清單＝下列 map 各 class 的 `pattern`；涵蓋 model／ACL／seed／OWL template／controller／tests／operator 等，本行不窮舉）前的守線知識採兩層供給，不雙寫在本檔：

1. **pattern-guard hook（機器強制）**：編輯守護路徑時 hook 自動列出必讀 docs，讀完重試即放行；對照表 `.claude/hooks/pattern-guard-map.json`（新增守線在此 append）。
2. **完整索引（人工查閱）**：[`docs/decisions/patterns/README.md`](docs/decisions/patterns/README.md)「何時必看」表。

新守線的登錄點（三分流，勿寫回本檔）：綁路徑 → `.claude/hooks/pattern-guard-map.json` append；可機械判定 → `scripts/audit/repo-lint.py` 加 check（gate 比散文強）；判斷類 → pattern／pitfall doc ＋ 各自 README 索引。

唯二留本檔的高頻 invariant：

- **CloudService / Project decoupling**：Invariants 見 `docs/decisions/refactor/cloud-service-project-decouple.md`
  的 §Invariants（該段即權威清單；此處刻意不寫條數——寫下就會漂，見 #615）。
- **Seeded 平台預設 agent_template 的 tenant-neutral invariant**：`agent_template_default` 必須 tenant-neutral（unbound + 無 user-scope 綁定；經 sudo 廣播為全租戶 fallback）。綁定當下有 code guard fail-loud（`src/models/agent_template.py` `_check_not_platform_default_when_bound`）；設計依據 `docs/decisions/patterns/seed-agent-template-design.md`；與 `agent_template_demo`（draft、可綁 project 測試）分工勿互換。

Pitfall 持久化紀錄（debug 撞牆時對照）：完整索引見 [`docs/tracking/pitfalls/README.md`](docs/tracking/pitfalls/README.md)（一行症狀 + 機械化狀態）。其中已機械化者撞到 gate / guard 時訊息內附 doc 指針，不需預先記憶。

Follow-up 追蹤：`docs/tracking/followups/follow-ups.md`（隨 cycle 持續累積；最新見該檔開頭 State 段）

## Docs organisation

**找文件** → [`docs/index.md`](docs/index.md)（依「我現在想做什麼」routing，篇數由
`scripts/audit/gen-docs-index.py` 即時算出）。**寫新文件該放哪、建檔／promote 指令** → [`docs/README.md`](docs/README.md)
（分類定義、決策樹與「建檔工具」表，權威來源）。分類清單不在本檔複述——複述過的版本已漂移成錯的。

**動過 `docs/` 後**（新增／刪除／搬移任何 tracked `.md`，或改了 `scripts/audit/gen-docs-index.py`）：跑 `python3 scripts/audit/gen-docs-index.py` 重產 `docs/index.md`；`--check` 只比對不寫檔（exit 0＝同步）。CI `.gitea/workflows/docs-link-check.yml` 的 `docs-index-sync` job 跑同一個 `--check` 且**會紅**——同 workflow 的 lychee link check 才是 warning-only（排除清單在 `.lychee.toml`），所以連結本機先驗：`lychee --offline --config .lychee.toml <改動的 .md>`（`--offline` 只驗相對路徑、不打網路；要驗外部 URL 拿掉它）。

`/docs:promote` 只剩清 `docs/superpowers/` 舊產出用（superpowers 已退役，該目錄目前 0 份待 promote）；新文件一律 `/docs:new`。

## Framework integration

現行只用 **mattpocock/skills**（Claude Code 官方 marketplace plugin `mattpocock-skills`；plugin 與 skills.sh 副本二選一、勿同裝，skill 清單以 plugin 為準）；它對 issue tracker／triage labels／domain docs 的消費規則由 `setup-matt-pocock-skills` 產在 `docs/agents/`（見下方 Agent skills），PM 一律走 Gitea issues。`research` 產出照 `docs/README.md` 的分類落檔（決策前偵察 → `docs/scout/`）。

superpowers（obra）、SuperClaude（`/sc:*`）與 ccpm 均已退役（ccpm 2026-08-07，#631）；superpowers plugin 須在 `~/.claude/settings.json` 與 `.claude/settings.local.json` 停用（`"superpowers@claude-plugins-official": false`），否則 SessionStart 仍會注入它的 skill 規則。歷史產出 → [`docs/archived/ccpm-v2/README.md`](docs/archived/ccpm-v2/README.md) 與 `docs/superpowers/`；舊指南在 `docs/archived/guides/frameworks/`。

## Code Style Guide

### Indentation

規則的單一來源是 `.editorconfig`（repo-lint 第 36 條 `indentation` 只驗 PR **新增**的檔）：

- Python: 4 spaces (PEP 8)
- `src/static/src/**`（OWL JS／template XML／SCSS）: 2 spaces
- 其餘 Odoo XML（`src/views/`、`src/data/`、`src/security/`）: 4 spaces（與 Odoo core 一致）
- 編輯既有檔一律跟該檔現況（2026-03 前的前端檔多為 4 spaces），不要順手重排縮排

### Python

```python
# Model naming：_name 一律 `woow_paas_platform.<snake_case>`（module 名＋底線，
# 不用 Odoo 慣用的點分式）；class 名不加 Woow 前綴。實檔：src/models/skill.py
class Skill(models.Model):
    _name = 'woow_paas_platform.skill'
    _description = 'Project-scoped Agent Skill Bundle'

# Field definitions - use keyword arguments（Many2one 的 comodel 可放第一個位置參數）
name = fields.Char(
    string='Skill Name',
    required=True,
    index=True,
    help='Slug from SKILL.md frontmatter; unique per (project, version).',
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

- Odoo core modules 與 third-party addons: 權威清單見 `src/__manifest__.py` 的 `depends`（含 ai_base_gt、odoo_ai_assistant_chatgpt_connector、sh_subscription 系列、ecpay 系列等）
- External fonts: Google Fonts (Manrope, Outfit) 與 Material Symbols 已在 `src/views/paas_app.xml` 的 `<link>` 全域載入（OAuth 頁另載於 `src/views/oauth2_templates.xml`）；SCSS 一律用 `src/static/src/paas/styles/00_variables.scss` 的 `$woow-font-display`／`$woow-font-body` 變數。

### Deployment

- 從零架叢集／DNS／DB 的前置條件見 `docs/guides/deployment/k8s-setup.md` §Prerequisites；平台實際入口走 Cloudflare Tunnel（上表 `paas-cloudflare-system`），不建 Ingress／DNS A record。

### PaaS Operator Service

- 改 `extra/paas-operator/` 前：工具鏈版本以 `extra/paas-operator/Dockerfile` 釘的 `python` image／`HELM_VERSION`／`KUBECTL_VERSION` 為準，Python 套件見同目錄 `requirements.txt`；本機 helm 大版號陷阱見上方 §PaaS Operator Integration Tests。

## Agent skills

### Issue tracker

Issues 在 Gitea（git-prod.woowtech.io / odoo-addons/woow_paas_platform），一律用 `tea` CLI 操作；`gh-origin` 僅為 mirror。見 `docs/agents/issue-tracker.md`。

### Triage labels

Work-state label 六選一（open issue 同時只掛一個；mattpocock 五個 canonical role → 本 repo label 的對映表在文件內）；agent 工作以 `agent-context:*`、`agent-status:in-progress` 與 `agent-assignee:leader1`～`leader5` 標記。見 `docs/agents/triage-labels.md`。

### Domain docs

單一 context：根目錄 `CONTEXT.md`＝純 glossary；ADR 級決策寫 `docs/decisions/`（不用 `docs/adr/`）。見 `docs/agents/domain.md`。
