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
- **values key 命名（paas-charts 階段 1 審查抓到，Task 7 守線）**：chart 的 **helm values key
  用 camelCase**（`envPrefix`、`image.repository`、`service.port`…）；**config.json 內部 key
  用 snake_case**（`env_prefix`）。`helm_default_values` JSON 必須用 `envPrefix`（**非**
  `env_prefix`），否則 helm 忽略未知 key 退回預設——pilot hermes 因預設 `HERMES_` 僥倖正確，
  複製 odoo/n8n（`envPrefix` 空字串）會靜默配錯。平台 `cloud_app_mcp_hermes` 的
  `helm_default_values` 已用 `envPrefix` ✓；Task 7 複製務必沿用 camelCase。
- **MCP endpoint 精確路徑**：woow-mcp-server launcher 實作後確認為 `/private_{token}/sse`
  （+ `/messages/?session_id=...`），內部 FastMCP SSE 綁 `127.0.0.1:8000`。對外仍 :8080、不變。

### O-1 回報結果 + existingSecret 機制（階段 2，2026-06-30）

**O-1（woow-mcp-server 回報）：**
- image pull 對外前綴：`jcr-prod.woowtech.io/woow-paas-docker-local/hermes-mcp-admin`
  （CI push 走叢集內網 artifactory 繞 CF 上限，pull 用對外前綴；與既有 manifest 一致）。
- **image tag 待 PR #5 merge 進 main + CI 綠後，取該 commit sha pin**——pilot 階段先用 `latest`。
- :8080 **有 HTTP health**：`GET /healthz → 200 {"status":"ok"}`（`app.py:81-83`、AuthMiddleware 免 JWT、
  CI smoke 證實）→ chart probe 用 **httpGet /healthz**（非 TCP）。
- config.json 逐欄 schema + 敏感切分：woow-mcp-server 直接同步 paas-charts（填 chart fail-fast + 文件）。

**existingSecret 機制（paas-charts chart values）：**
- chart 採 `existingSecret`：**operator 為每個實例建 K8s Secret（key=`config.secret.json`，含敏感 config
  fragment）**，chart 引用此 Secret 名 → 敏感值**不落 helm values 明文**。
- `config.sensitive` 走 existingSecret（勿放 `helm_default_values`）、`config.nonSensitive` 進 values。
- chart initContainer 缺此 key 時印 WARNING（pre-O-1 不 fail-fast；O-1 釘死必填欄位後改 fail-fast）。
- ⚠️ 安全提醒：seed 後敏感值以明文存於 PVC `config.json`（GUI-owned 設計必然）→ longhorn 卷/備份
  需納入加密考量（GA 前 follow-up）。

**pilot 範圍裁定：**
- **pilot 端到端用 chart 預設 config 跑通**（hermes 啟動 + GUI + MCP + PVC + R1 回收）。
- **existingSecret 完整注入（operator 建 per-instance Secret）列 GA 前 follow-up**（不阻塞 pilot
  smoke；見 task #6）——這是 spec 原 paas-platform plan 未涵蓋的部署鏈新工作（operator 需 create-secret 能力）。

**pilot 端到端依賴鏈：**
paas-charts 接力（probe→httpGet、bump 版本、回報 OCI 座標）→ platform I-1 pin（OCI 座標 + image latest）
→ k8s-dev-sandbox 端到端驗證（含 R1 PVC 回收實證）→ 三 PR 對抗式審查 → 授權 merge → CI build image
→ re-pin image tag sha（GA 精確化）。

## 整合審查結論 + follow-up（階段 3，2026-06-30）

**層 2 chart e2e = 5/5 全綠**（merge 前實際部署證據，docker-desktop k8s）：pod Running（initContainer seed→main）/ `/healthz` 200 / GUI `/` 200 / **MCP `/private_<token>/sse` → 200 + `text/event-stream` + `event: endpoint`（真實 SSE handshake）、錯 token→403** / delete pod→Recreate 後 config.json 持久（seed-if-absent 不覆寫 GUI 編輯）。

**proxy Host-header bug（pilot 過程發現並修復）**：`proxy.py` 轉發裸 `localhost` 撞 FastMCP(mcp 1.28.1) DNS-rebinding 保護回 421 → 改送 `127.0.0.1:{mcp_port}`（PR #5 + 回歸測試 `test_proxy_forwards_loopback_host_with_port`）。經三層驗證：協定層直測（裸 localhost→421、127.0.0.1:8000→200）→ through-proxy e2e → chart 部署鏈路真實 SSE handshake。共用 proxy、一次修三 app。

**對抗式整合審查裁決：NO_BLOCKER**（14 agents 跨 3 維度 review + 對抗式 verify；11 findings：0 blocker / 9 deferred / 2 誤報）。三 PR（platform commits / woow-mcp-server #5 / paas-charts #31）可 merge。

**Follow-up（deferred，按時機）：**

⚠️ **複製 odoo/n8n 前必補**：
- **proxy Origin 半修**（`mcp-admin-core/proxy.py:57`）——Host 修了但 FastMCP `TransportSecurityMiddleware` 同時驗 **Origin**（loopback auto-enable `allowed_origins=["http://127.0.0.1:*",...]`）；proxy 未處理 Origin → 瀏覽器型 MCP client（送 `Origin: https://...`）會被子行程 403。pilot e2e 過是因非瀏覽器 SSE client（httpx/eventsource）不送 Origin。**shared across 3 apps**，複製前不補會帶入三者。修法：`origin` 加入 proxy skip set（或改寫 `http://127.0.0.1:{mcp_port}`）+ 鏡像 Host 回歸測試補 Origin 案例 + contract doc 註明 MCP endpoint 不得透傳外部 Origin。

**GA 硬化**：
- **CONFIG_PATH dead key**（chart `deployment.yaml:106`）——chart 注入 `CONFIG_PATH` 但 image 讀 `MCP_ADMIN_CONFIG`（dead/no-op）；pilot 靠 mountPath 預設 `/data` 與 image 三處硬編 `/data` 巧合對齊。改 `persistence.mountPath` 會 config 持久化靜默失效。修法：rename `CONFIG_PATH`→`MCP_ADMIN_CONFIG` + initContainer/seed mountPath 一致化，或刪 dead key + values 註明 config 路徑固定 `/data`（image 硬契約）。
- **runAsNonRoot / readOnlyRootFilesystem**：hermes image（python:3.12-slim 無 USER）跑 root；待 **O-1 釘 image sha** 後開啟（補償控制已備：drop ALL caps + allowPrivilegeEscalation=false + seccomp RuntimeDefault + automountToken=false）。屬硬性 GA gate，不可變永久 TODO。
- **image latest→git-sha**：pilot `latest`+IfNotPresent；GA 釘 sha（per-tenant 單 pod，pilot 風險低）。
- **networkPolicy egress/ingress 收斂**：spec 缺口4 已簽核 pilot 從寬（egress `- {}` 全開、ingressNamespaces 空→任何來源可達 :8080）；GA 收斂（egress `0.0.0.0/0 except 叢集 CIDR + block 169.254.169.254`；ingressNamespaces 設真實 tunnel/ingress ns）。agent-runtime §6.3 已有模板。
- **OCI chart repo 衛生（B→A 遷移）**：階段3 OCI publish 時 jcr-prod 無 `woow-paas-charts` 專用 chart repo（ops 未建/建錯型別；實際只有 image 型 repo）。pilot 採 **B 解**——chart 推進現有 image repo `woow-paas-docker-local`（最終座標 `oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server:0.1.1`，chart digest sha256:561d0420…，appVersion=image git-sha 9d109442）。隱藏優點：operator 重用 image repo 既有 OCI 認證、無須新增 chart repo 認證（降低層 3 operator 拉 chart 認證風險）。代價：chart/image 混同一 repo 衛生較差。**GA 遷 A**：請 ops 建專用 `woow-paas-charts-local`（rclass=LOCAL、packageType=Docker/OCI），遷移時同步對齊三處座標（platform `helm_chart_name` / paas-charts README + CI `OCI_CHART_REPO`）。

**operator / 層 3 實證**：
- **R1 PVC 孤兒回收**：chart 姿態正確（orphaned-at 由 operator teardown 貼標、chart 不擁有）；operator `releases.py` delete_release 已 chart-agnostic 對 ns 內 PVC 貼標 + pvc-cleanup-cronjob 消費 → 待層 3 deploy-stg 部署→teardown 實證 PVC 被回收。
- **existingSecret 完整注入**（task #6）：operator 建 per-instance Secret；文件警示（jwtSecret+existingSecret 同設靜默丟棄、config.sensitive 未走 existingSecret）已存在於 NOTES.txt/values 註解。

**層 3 deploy-stg e2e 完整結果（2026-06-30，step 1-4 全綠；逐一暴露+修 4 個 stg-only gap + R1 驗證成功）**

四個 gap 層 2 docker-desktop 全抓不到（無 ResourceQuota / 用 public chart / local image pullPolicy=Never / 不透過 operator）——這正是 pilot 走 stg 真實環境 e2e 的核心價值。**gap 1：operator helm 無 OCI registry 認證拉私有 chart**

層 3 step 1-2 全綠：deploy-stg 建 stg-* tag → CI clone **develop**（build-image.yml 對 stg-* tag 預設 develop）→ paas-odoo-stg:20260630.0915 → helm upgrade dev-sandbox-stg（DB 保留、mcp seed reload）→ `/woow` marketplace **顯示 Hermes MCP Server 卡片 + MCP Server category tab（active filter）+ hub icon + plans 499/4990 + configure 頁 Launch**，OWL render 零 console error（只 favicon 404）。**step 3（operator 部署 mcp-hermes 實例）失敗**：operator `helm install oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server --version 0.1.1` 回 `401 Unauthorized（failed to fetch anonymous token，scope=repository:woow-paas-docker-local/mcp-server:pull）`。

根因：**helm OCI chart pull 認證（`helm registry login`）≠ pod imagePullSecret（kubelet）**——operator 為 pod 注入 imagePullSecret（paas-registry）拉 image，但 operator 自己的 helm CLI 無 OCI registry 認證（確認 operator pod `~/.config/helm/registry/config.json` 不存在），對私有 repo anonymous pull → 401。這是 **mcp-server（首個私有 OCI chart cloud service）暴露的 operator gap**——既有 cloud service 用 bitnami public chart（registry-1.docker.io）不需認證，整合審查未抓到（層 2 是 paas-charts 直接 helm install、不透過 operator）。**[[two-layer-acl-lockstep]] 類盲區的部署版：B 解「chart 推 image repo 重用認證」的假設因此不成立——image pull 與 helm pull 是兩套認證。**

修法（已修）：operator `_ensure_oci_login`——helm install/upgrade OCI chart 前 `helm registry login`（重用 tenant imagePullSecret 同憑證 `settings.registry_*`，`--password-stdin`；stg `REGISTRY_SERVER=jcr-prod.woowtech.io` 與 chart host 一致）。commit 315fa3a、test_helm.py +2。

**gap 2：operator `_run_command` input encode dormant bug。** retry 時暴露——`_run_command` 對 input_data 做 `.encode()` → bytes，但 `subprocess.run(text=True)` 預期 str（自己 encode），對 bytes 再 encode → AttributeError。既有 caller 都 `input_data=None` 沒觸發；`_ensure_oci_login --password-stdin` 是**首個傳 input_data 的 caller**。mock `subprocess.run` 抓不到（不真 encode）— mock-vs-真實。修：移除 `.encode()`（commit 517773e）。

**gap 3：chart seed-config initContainer 缺 resources。** stg `paas-ws-stg-*` ns 有 operator ResourceQuota（要求所有 container 含 initContainer 設 limits/requests），seed-config 缺 → pod `FailedCreate`。修：paas-charts chart **0.1.2**（seed-config + main 都有 resources，可配置 `seedResources`）。

**gap 4：operator releases.py 漏注入 imagePullSecrets。** pod 拉私有 hermes-mcp-admin image → `Init:ImagePullBackOff 401`。operator `ensure_registry_secret` 建了 `paas-registry` dockerconfigjson secret，但 `releases.py`（cloud service install）漏注入 imagePullSecrets——`agent_runtimes.py`（F-PILOT-4）有、releases 漏（**同 family 半套**，呼應 [[orm-vs-endpoint-acl-test-coverage]]）。既有 n8n/odoo 用 bitnami public image 沒踩到。修：`releases.py` install `setdefault("imagePullSecrets", [{"name": registry_secret_name}])`（commit a0e7c25）；paas-charts 確認 chart 消費 top-level `.Values.imagePullSecrets`（key+型別對上）。

operator 三修復（OCI login + input encode + imagePullSecrets）依序 build operator-v20260630.1016/1057/1123、Fleet deploy `paas-app-bundle-cd` paas-operator-stg；chart 0.1.2 publish（platform helm_chart_version + DB re-pin）。

**層 3 完整成功（step 1-4 全綠）：** step 1 deploy-stg build develop → dev-sandbox-stg（mcp seed reload）；step 2 marketplace 顯示 Hermes 卡片 + MCP Server tab + hub icon + plans + configure，**OWL render 零 console error**；step 3 operator 端到端——pod 1/1 Running + service Running + **CF tunnel public URL `https://paas-cs-*.woowtech.io/healthz` → 200 `{"status":"ok"}` + GUI / → 200**；step 4 R1——delete service → operator teardown 對 ns 殘留 PVC 貼 `paas.woowtech.io/orphaned-at` + `grace-period-days=7` → pvc-cleanup cronjob 7 天回收。

**R1 over-label 觀察（follow-up）：** operator teardown 是 chart-agnostic 對 ns **所有殘留** PVC 貼 orphaned-at（reaped-since 機制：`releases.py` delete_release → `label_pvcs(ns, remaining)`）。pilot 多次 retry 累積多 service 同 ns `ad023482`（reference_id 對應同 workspace test2）→ teardown 一個連帶把其他**活著** service 的 PVC 也貼 orphaned-at（grace 7 天後 cronjob 會誤刪）。正常 per-service 獨立 ns 不會 over-label；但若 reference_id 真允許多 service 同 ns，operator teardown 應改 per-release PVC selector 貼標（非 ns-wide）以免誤刪。

**OCI repo follow-up 更新（gap 1 連帶）：** gap 1 修法用 `helm registry login` 證實 B 解（chart 推 image repo `woow-paas-docker-local`）可行——operator 用 `settings.registry_*`（同 jcr-prod 憑證）拉 chart 成功。GA 遷 A（專用 `woow-paas-charts-local`）時 operator helm 認證不變（同 jcr-prod registry），只改座標。

**誤報（無需 action）**：`updateStrategy`（platform helm_default_values 有但 chart 硬編 Recreate，fail-safe）、`ENV_PREFIX`（chart 注入但 env_prefix 走 config.json，dead 但無害）。

## 階段 4：odoo/n8n 複製接續指引（下次 session 起點；hermes pilot 已完成）

**啟動方式**：新 session 一句話喚起——「繼續 MCP server cloud service：複製 odoo/n8n service type（Task 7）。先讀 `docs/superpowers/specs/2026-06-29-mcp-server-cloud-service-design.md` 階段 3-4 恢復 context。」開場跑 `maestri list` 確認連到 woow-mcp-server + paas-charts；各 repo agent 動工前先開/確認 worktree。

**hermes pilot 已落地（複製直接參照，基礎設施不需重做）：**
- 三 repo merge：platform develop（至 #260）/ woow-mcp-server main（#5）/ paas-charts main（#31/#32）
- **operator 20260630.1123**：私有 image 部署鏈三修復已打通（OCI login + input encode + imagePullSecrets）→ **odoo/n8n 複製不需再改 operator**
- chart `mcp-server` 0.1.2 已通用（values 切 app）+ OCI published `oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server`
- 層 1-3 全驗證（層 3 stg 端到端 + R1）

**複製前必補（先做，否則複製會把 bug 帶進 3 app）：**
1. ⚠️ **proxy Origin**（woow-mcp-server `mcp-admin-core/proxy.py`）：Host 修了但 Origin 沒——FastMCP DNS-rebinding 同時驗 Origin，瀏覽器型 MCP client 會 403。**共用 proxy、shared 3 apps**。修：`origin` 加 skip set（或改寫 `127.0.0.1:{port}`）+ 鏡像 Host 的 Origin 回歸測試 + contract doc 註明。詳見階段 3 follow-up。
2. **O-6 n8n transport 實測**：n8n MCP 子行程是 `/mcp`（streamable-http）還是 `/sse`？實測決定 chart/proxy 路由。

**複製步驟（odoo / n8n 各走一次層 1-3，照 hermes pattern）：**
- platform：`cloud_app_templates.xml` 加 `cloud_app_mcp_odoo` / `cloud_app_mcp_n8n` seed（category=mcp、helm_chart_name 同 OCI 座標、image 釘 git-sha、helm_chart_version 0.1.2、`_inject_storage_class` mcp-odoo/mcp-n8n path_map 已在）+ 訂閱方案 + link + tests（仿 `test_mcp_server_template_seed.py`）
- 各 app helm_default_values 差異（woow-mcp-server 已給；O-6 待確認 n8n）：
  - **odoo**：image repo `odoo-mcp-admin`、`app: odoo`、port=8000、transport `/sse`、env_prefix `""`（envPrefix camelCase 也空）
  - **n8n**：image repo `n8n-mcp-admin`、`app: n8n`、port=3000、transport `/mcp`（待 O-6 實測確認）、env_prefix `""`
  - 注意：**這是 mcp-server-as-a-service（獨立 admin GUI + MCP endpoint），不是既有 n8n cloud service 的 n8n-mcp sidecar**（兩者並存、勿混）
- 層 1-3 同 hermes：platform unit test → 層 2 chart e2e（local image）→ 層 3 deploy-stg 端到端（operator 部署鏈已打通，預期順無新 gap）

**已知 GA 硬化（非複製阻塞，GA 前處理，詳見階段 3）：** CONFIG_PATH dead key（chart）/ runAsNonRoot + image sha pin / networkPolicy egress·ingress 收斂 / R1 over-label 改 per-release PVC selector / existingSecret（task #6 operator 建 per-instance Secret）/ OCI repo B→A 遷專用 `woow-paas-charts-local`。
