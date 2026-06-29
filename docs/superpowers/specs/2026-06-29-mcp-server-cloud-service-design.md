# Design: MCP Server Cloud Service（odoo / n8n / hermes 獨立服務化）

> 狀態：design approved（brainstorming 2026-06-29），待 writing-plans。
> 協作：三 repo 多 agent（maestro = paas-platform）。
> - `woow-mcp-server`：產 `<app>-mcp-admin` image + config seed 契約
> - `paas-charts`（canvas agent 名 "Terminal"）：`charts/mcp-server/` + OCI 發布
> - `woow_paas_platform`（本 repo / maestro）：cloud service type + 訂閱 + 部署鏈整合
> pilot：**hermes 先行打通端到端**，驗證後平行複製 odoo / n8n。

## 背景

`woow-mcp-server` 用共用框架 `mcp-admin-core` 包出三種 `<app>-mcp-admin` 服務
（hermes / odoo / n8n），每個都是「admin GUI + MCP client endpoint + `/data` config」
的單體服務。目前這些服務尚未進 PaaS 平台販售。

平台現有的「MCP」是**另一回事**：n8n cloud app template 旁掛一個 `n8n-mcp` **sidecar**
（`cloud_app_template.py` 的 `mcp_enabled` / `mcp_sidecar_*` 欄位）。本設計與該 sidecar
機制**完全正交、互不干涉**——本設計做的是把 MCP server **本體**當成獨立 cloud service。

## 目標

新增三種 cloud service type（`mcp-hermes` / `mcp-odoo` / `mcp-n8n`），讓租戶能在
`/woow` marketplace 像買 odoo/n8n cloud service 一樣，**訂閱 → operator 動態部署一個
per-tenant 實例 → 經 subdomain+tunnel 對外存取 admin GUI 與 MCP endpoint**，config
持久化於 PVC。

## 範圍

**In scope**
- paas-platform：`category='mcp'`、3 筆 `CloudAppTemplate` seed、3 組訂閱 plan/product、
  plan↔template link、`_inject_storage_class` path_map、TDD 測試。
- paas-charts：`charts/mcp-server/`（單一 chart，values 切 app）、PVC、Secret 注入、
  安全層、chart tests、OCI publish pipeline。
- woow-mcp-server：三份 `k8s-deploy.yaml` port 修正為 8080、config seed 從 Secret +
  initContainer、（必要時）Dockerfile 啟動腳本支援。
- 端到端：k8s-dev-sandbox 部署 `mcp-hermes` 驗證全鏈。

**Out of scope**
- 現有 n8n + n8n-mcp **sidecar** 機制（不動）。
- MCP client 端的 token 自動發現/呈現自動化（pilot 先讓使用者進 admin GUI 自取，
  見 Open Items O-5）。
- odoo / n8n 的完整複製（pilot hermes 驗證後再做，沿用同契約）。

## 決策摘要（brainstorming 2026-06-29 拍板）

| # | 決策 | 選擇 |
|---|------|------|
| D1 | 動工順序 | 先 pilot **hermes** 打通端到端，再複製 odoo/n8n |
| D2 | slug / 分類 | `mcp-hermes` / `mcp-odoo` / `mcp-n8n`；`category` Selection 新增 `mcp` |
| D3 | 定價 | 三種統一 **月 499 / 年 4990**（odoo 同級），之後可在 Odoo 後台調 |
| D4 | chart 配送 | **OCI registry**（`oci://...`，與現有 platform template 一致） |
| D5 | chart 結構 | **單一 `mcp-server` chart + values 切三種 app**；授權 paas-charts 動工時若 image 差異過大改拆 subchart |
| D6 | 部署形態 | **per-instance**，沿用現有 cloud service（operator 動態 helm install、`reference_id`/`subdomain`/`paas-ws-*` ns）；**不做共享單例** |
| D7 | 對外 port | **統一 8080**（woow-mcp-server 修正三份 manifest；chart `targetPort: 8080` 可 pin） |

## 契約介面（三方共享 source of truth）

以 pilot hermes 為準；三種 app 對外結構**相同**，差異僅在容器內（internal port、
transport path、`env_prefix`）。

| 介面 | 約定 | 負責 repo |
|------|------|-----------|
| **Image** | `<registry>/<app>-mcp-admin:<pinned-tag>`，`EXPOSE 8080`、`VOLUME /data`；registry/tag 真實來源 `.gitea/workflows/ci.yml` | woow-mcp-server |
| **對外 port** | 統一 `8080`（修正三份 `apps/<app>/k8s-deploy.yaml` 的 9001/9002/9003 → 8080） | woow-mcp-server |
| **對外路由** | subdomain（platform per-instance 動態生成，格式 `ws-{workspace}-{name}-{hash}`）+ Cloudflare tunnel **單一路由 → :8080** | paas-platform |
| **Path 規則** | admin GUI 在 root `/`（hermes 另有 `/api` dashboard 反代，同 root）；MCP client 在 `/private_{token}/...`（同 port 8080，`{token}` 服務內部生成）。ingress/tunnel **不可改寫/strip path**；服務掛 root，不跑 subpath | paas-charts + platform |
| **MCP 能力** | 由服務本體內建（**非** sidecar）。新 template 的 `mcp_enabled=False`（不掛 sidecar）；MCP endpoint 經 path `/private_{token}/` 對外 | paas-platform |
| **Chart** | `oci://<registry>/woow-paas-charts/mcp-server:<ver>`，values 區分 app（image / `env_prefix` / config） | paas-charts |
| **安全（config seed）** | config.json 敏感欄位（`admin_password`、各服務 API key/密碼）→ **k8s Secret + initContainer seed** 到 `/data/config.json`，不落明文（呼應 CLAUDE.md 安全 pattern）。`env_prefix`/command 須在子行程啟動前就位 | paas-charts（chart/Secret/init）+ woow-mcp-server（image 支援） |
| **PVC** | 獨立 RWO PVC 掛 `/data`，Deployment `strategy: Recreate`（避免 Multi-Attach）；storageClass 用 platform 的 `ir.config_parameter['woow_paas_platform.storage_class_name']`（預設 `longhorn-delete`），chart 留空可覆寫 | paas-charts |
| **PVC 回收** | 部署在 `paas-ws-*` ns + operator teardown 為 PVC 貼 `paas.woowtech.io/orphaned-at`(+grace-period) 標籤，否則 `pvc-cleanup-cronjob` 不回收 → 永久孤兒（見風險 R1） | platform/operator + 驗證 |

## 部署形態（D6 展開）

沿用現有 cloud service 全鏈，**不需改 operator 主流程**（除非 R1 驗證發現 PVC 標籤缺口）：

```
/woow marketplace 選 mcp-hermes → 選方案 → controllers/paas.py 建 CloudService(state=pending)
  → 建立並確認 subscription
  → ir_cron_deploy_cloud_services 每分鐘 _cron_process_pending_deploys()
    → cloud_service.py:_execute_deploy()
        ├ _inject_storage_class()         # path_map 加 mcp-* slug
        ├ _inject_image_pull_secrets()
        └ paas_operator.install_release() # helm install mcp-server chart (values: hermes)
  → ir_cron_check_deploying_services 輪詢 pod 狀態 → running
```

每租戶一個獨立實例：唯一 `reference_id`(UUID) / `subdomain` / `helm_release_name`
(`svc-{reference_id[:8]}`) / `helm_namespace`(`paas-ws-*`)。

## paas-platform 端實作

| 檔案 | 變更 |
|------|------|
| `src/models/cloud_app_template.py`（`category` Selection 約 L5-230） | 新增 `('mcp', 'MCP Server')` 選項 |
| `src/data/cloud_app_templates.xml` | 新增 `cloud_app_mcp_hermes`（pilot）；複製階段加 `cloud_app_mcp_odoo` / `cloud_app_mcp_n8n`。`helm_chart_name=oci://.../mcp-server`、`helm_default_values` 區分 app image/env_prefix、`default_port=8080`、`ingress_enabled=True`、`mcp_enabled=False`、`post_deploy_init_type='none'`（暫定，見 O-4） |
| `src/data/subscription_products.xml` | 新增 `plan_mcp_hermes_{monthly,yearly}`（499/4990）+ 對應 product；複製階段加 odoo/n8n |
| `src/data/subscription_template_links.xml` | 補 `mcp-*` template ↔ plan 的 M2M link（注意 `noupdate="0"`，見 R3） |
| `src/models/cloud_service.py:609` `_inject_storage_class()` | `path_map` 加 `'mcp-hermes'`/`'mcp-odoo'`/`'mcp-n8n'` → `[('persistence','storageClass')]` |
| `src/hooks.py` | 若新增任何 `config_parameter` 才需動 `_CONFIG_PARAMETERS`（本設計目前不新增） |

## pilot hermes 端到端流程

1. 使用者在 marketplace 選 `mcp-hermes` → 選方案 → 建 `CloudService`。
2. operator `helm install` mcp-server chart（values: hermes image + `env_prefix=HERMES_`）。
3. chart 建 Secret（admin_password / API keys）+ PVC（/data）；initContainer 把 Secret
   seed 成 `/data/config.json`。
4. `hermes-mcp-admin` 啟動（:8080），admin GUI 在 `/`、MCP endpoint 在 `/private_{token}/`。
5. platform 生成 subdomain + Cloudflare tunnel 單一路由 → :8080，path 不改寫。
6. 重啟 pod 驗 PVC 持久化 config（Recreate strategy）。
7. 訂閱生命週期：suspend / resume / cleanup 沿用 `subscription_lifecycle_mixin`。

## 測試 / 驗證策略（TDD）

| repo | 測試 |
|------|------|
| paas-platform | Odoo `TestCase`：category 新值、3 筆 template seed 正確性、plan↔template link、`_inject_storage_class` 對 mcp-* slug 注入、ACL（沿用 cloud service 既有 ir.rule，補 cross-tenant negative）；升級路徑（先裝後升）驗 link 不被 sticky noupdate skip（R3） |
| paas-charts | `helm lint` / `helm template`：PVC 掛載 /data、Secret 注入路徑、`strategy: Recreate`、targetPort 8080、values 切 app；chart tests |
| woow-mcp-server | image build；config seed（Secret → /data/config.json）；port 8080 監聽 |
| 端到端（sandbox） | 部署 mcp-hermes → GUI 可達 → MCP endpoint 可連 → 重啟驗 PVC 持久 → 訂閱 suspend/resume/cleanup → **teardown 後驗 PVC 被回收（無孤兒）** |

## 對抗式審查重點

獨立 reviewer agent 針對：
- **PVC 孤兒**（R1）：teardown 是否真的貼 orphaned-at 標籤、是否在 paas-ws-* ns；
  繞過 Odoo unlink 的刪除路徑（sandbox destroy / 手動 kubectl）是否留孤兒。
- **Secret 明文**：config.json 敏感欄位是否確實從 Secret 注入、不落 helm values 明文、
  不寫進 git。
- **ACL cross-tenant**：mcp-* CloudService 沿用 cloud service ir.rule，補 HttpCase
  cross-user negative（呼應 `orm-vs-endpoint-acl-test-coverage` pattern）。
- **契約漂移**：chart mock 測試的 port/path/secret key 是否對齊真實 image 與 manifest
  （呼應 `operator-cf-access-delete-202-mock-contract` pitfall）。
- **path 改寫**：tunnel/ingress 是否真的保留 `/private_{token}/` 不被 strip。

## 風險與緩解

| # | 風險 | 緩解 |
|---|------|------|
| R1 | operator 未對 mcp-server chart 的 PVC 貼 `orphaned-at` 標籤 → PVC 永久孤兒（CLAUDE.md `cf-ns-orphan-no-account-gc` / `reaped-since-single-path-stamp-gc-immunity` 同家族） | 動工前**驗證** operator teardown 對此 chart PVC 的標籤行為；若缺口，operator 端補（可能跨 `paas-app-bundle-cd`） |
| R2 | port 不一致（image 8080 vs manifest 9001-9003）導致 chart targetPort pin 錯 | D7：woow-mcp-server 先統一 8080 修正三份 manifest（唯一 blocker，優先處理） |
| R3 | `subscription_template_links.xml` 若用 `noupdate="1"` → 升級路徑 M2M link 被靜默 skip（CLAUDE.md `seed-noupdate-sticky-template-links` pitfall） | 沿用 `noupdate="0"`；升級路徑測試守線 |
| R4 | n8n MCP transport path（/mcp vs /sse）不確定 | 不阻塞 pilot；複製 n8n 階段由 woow-mcp-server 起容器實測再定 client URL |
| R5 | 單一 chart 切三種 app 若差異過大 → values 過度複雜 | D5：授權 paas-charts 動工時改拆 subchart |

## 動工順序與 worktree / PR 流程

1. **三方對齊**（本 spec）→ 使用者審核通過。
2. **woow-mcp-server**（前置 blocker R2）：worktree 修正三份 manifest port=8080 +
   config seed Secret/init 支援 → PR。
3. **paas-charts**：worktree 建 `charts/mcp-server/`（pilot hermes values）+ OCI publish → PR。
4. **paas-platform**：worktree 加 category/template/plan/link/storageClass + TDD → PR。
   （依賴 chart OCI 座標與 image tag；可先用議定契約並行開發，整合時 pin 實際座標。）
5. **端到端整合驗證**（k8s-dev-sandbox 部署 mcp-hermes）+ 對抗式審查 → 三 PR 整合。
6. **複製 odoo / n8n**：沿用同契約，重複 2-5（n8n 補 R4 實測）。

各 repo agent **動工前先開 worktree**，完成後開 PR；maestro（paas-platform）協調契約與整合驗證。

## Open Items（動工時各 repo 自行鑽研，回報 maestro 校正本 spec）

- **O-1**（woow-mcp-server）：hermes `config.json` 完整 schema（`config/store.py:32-54`）+
  明確標出哪些欄位敏感須走 Secret。
- **O-2**（paas-charts）：確認 operator/CD 端 OCI registry 座標與既有 publish pipeline
  的整合點（`publish-charts.yaml` 加 `helm push oci://`）。
- **O-3**（platform/operator）：R1 的 PVC 標籤驗證結論。
- **O-4**（platform）：`post_deploy_init_type` 是否需要 `mcp`/`hermes` 專屬 init（傾向
  `none`，因 config seed 已由 initContainer 處理；待 O-1 確認無部署後動作需求）。
- **O-5**（platform）：MCP client 連線 URL + token 如何呈現給使用者（pilot 先讓使用者
  進 admin GUI 自取；後續可自動化）。
- **O-6**（woow-mcp-server）：n8n `--transport http` 真實端點路徑（/mcp vs /sse），複製
  n8n 階段實測。

## 契約補遺（階段 1 三方對齊，2026-06-29）

兩個 repo agent 開 worktree（charts=`feat+mcp-server-chart`、mcp-server=`paas-cloud-service-hermes-pilot`）後，獨立指出一個 spec 未釘死的 design gap 並提出缺口清單。以下為拍板結果：

### config.json 生命週期：**GUI-owned / seed-if-absent**（拍板）

config.json 同時是「Secret seed 的目標」又是「admin GUI 的 runtime 可寫狀態庫」
（GUI 會改 connection、輪替 token、開關工具）。決策：

- initContainer **只在 `/data/config.json` 不存在時** seed 初始值（admin_password 等 bootstrap）。
- 之後 GUI 的 runtime 變更持久化於 PVC，pod 重啟保留（與 PVC 持久化目的一致）。
- initContainer 邏輯 = **seed-once / 補缺，非全量覆寫**。
- 代價：Secret 輪替不自動生效（pilot 接受；後續可加 reconcile）。
- 採 woow-mcp-server 的 Option A：**Dockerfile / 啟動腳本不改**，由 chart 的
  Secret + ConfigMap（含 `env_prefix="HERMES_"` 這個 config key）+ initContainer 組出 config.json。

### 缺口拍板（回覆兩 agent 放行用）

| # | 缺口 | 拍板 |
|---|------|------|
| 1 | OCI registry | 議定 `oci://jcr-prod.woowtech.io/woow-paas-charts/mcp-server`；paas-charts 用真實 publish registry 確認/修正並回報（I-1 pin） |
| 2 | config schema + 敏感切分 | woow-mcp-server **O-1** 回報（hermes config.json 完整 schema + 逐欄敏感/非敏感）；chart 先把 Secret/ConfigMap/initContainer 機制做好、schema 標 TODO |
| 3 | config.json 生命週期 | GUI-owned / seed-if-absent（見上） |
| 4 | NetworkPolicy | 仿 agent-runtime；pilot allow-list：ingress 允許 tunnel/ingress-controller ns → :8080；egress 允許 DNS + 對外（hermes 連使用者後端/外部 API），pilot 從寬 |
| 5 | 部署形態 / ns | per-instance、operator 託管、`paas-ws-*` ns（= D6）；PVC 落 paas-ws-* + operator teardown 貼 orphaned-at 回收（R1） |
| 6 | 對外性 | **ClusterIP-only，chart 不建 K8s Ingress**；對外由 platform subdomain + Cloudflare tunnel 處理（沿用現有 cloud service） |
| 7 | health probe | woow-mcp-server O-1 確認 :8080 有無 HTTP health endpoint；無則 pilot 用 TCP probe，有則 httpGet |
| 8 | image 座標 | woow-mcp-server O-1（hermes-mcp-admin registry 前綴 + tag 來源 from `ci.yml`） |
| 9 | values 切 app 形式 | 單一 values + `app:` 選擇器（pilot `app: hermes`）；差異過大授權 paas-charts 改 per-app values / subchart（= D5） |

### 其他

- **hermes SSE launcher**（woow-mcp-server Blocker 2）：授權 woow-mcp-server 以其 SSE 方案
  （內部 `/sse`+`/messages/`、bind `127.0.0.1:8000`）實作 hermes launcher，**維持對外契約**
  （:8080、GUI 在 `/`、MCP client 在 `/private_{token}/`）。
- **manifest 去留**：chart 是正式部署來源（platform 用 chart 部署）；三份
  `apps/<app>/k8s-deploy.yaml` 降為 woow-mcp-server 的 **dev 參考**，port 8080 契約已定、chart
  直接用、不依賴 manifest（manifest 若保留請同步 8080）。R2 因此從「blocker」降為「dev 一致性」。
- **note 連接限制**：maestri note 只連建立者終端，repo agent 讀不到共享 note；對齊改用
  「回覆 prompt 直接附 spec 絕對路徑 + 貼契約答案」。
