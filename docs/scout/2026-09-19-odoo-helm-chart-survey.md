---
title: 現成 Odoo Helm Chart 調查（Bitnami 以外）
category: scout
status: draft
created: 2026-09-19
updated: 2026-09-19
related:
  - https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1135
  - https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1134
  - https://git-prod.woowtech.io/woow-paas/woow-paas-charts/issues/81
  - https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/3
---

## 結論

除了 Bitnami，市面上**只有一個**仍在積極維護、直接跑官方 `odoo:18.0` image、且授權寬鬆可 fork 的候選：**[IMIO/helm-odoo](https://github.com/IMIO/helm-odoo)**（Apache-2.0，由比利時公部門 IT 共同體 IMIO 維護，Artifact Hub 上以 `imio/odoo` 發佈）。它不是能直接 `helm install` 套用到本平台的成品（DB bootstrap 是「一次性手動切旗標」而非冪等自動判斷、不會處理 admin 密碼輪替、`values.schema.json` 未鎖 `additionalProperties: false`），但架構幾乎是 woow-paas-charts#81 已經獨立訂出的設計藍圖的現成實作（nginx sidecar 轉 `/websocket` → gevent 8072、Helm hook Job 跑 `odoo -i/-u --stop-after-init`、單一 PVC 掛 `/var/lib/odoo`、`extraVolumes`／`extraInitContainers`／`extraContainers` 擴充點一應俱全）。建議 **#81 的自建 chart 以 IMIO/helm-odoo 當藍本／可能直接 fork 起手**，而非從零設計，可大幅降低設計風險與工時；但仍需補上 #81 已列出的三個落差（冪等 init 判斷、admin 密碼輪替、嚴格 schema）。

Bitnami chart 本身的維護現況：**自 2025-08-28（Broadcom 官方宣布的 catalog 變更生效日）起，`bitnami/charts` repo 的 `bitnami/odoo` 目錄再無功能性 commit**（最後一次 dependency 更新是 2025-08-07；之後到 2025-11-04 的 4 個 commit 全是 README wording／license annotation 調整）。更關鍵的是：**即使是 GitHub 上最新的 chart 原始碼（`main` 分支 28.2.11、也就是我們現在用的 28.2.10 的下一版），`values.yaml` 預設的 `image.repository` 仍寫死 `bitnami/odoo`，不是 `bitnamilegacy/odoo`**——換句話說 chart 自己的預設值現在對免費使用者是壞的（`bitnami/odoo` 這個 tag 已不可公開 pull）。平台目前的繞法是**自建一個 `FROM bitnamilegacy/odoo:18.0.20250805-debian-12-r9` 的衍生映像**（推到 JCR：`jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo`，Dockerfile 見 `paas-odoo-ci` repo），而不是直接把 chart 的 `image.repository` 改成 `bitnamilegacy/odoo`——同樣是繞過官方 catalog 斷裂，但路徑多一層（自建映像），不是 chart 支援的路徑。

⚠ 另有一個**叢集自有、不在本次市面搜尋範圍內**的內部候選 `odoo-tenant-1.0.0`／`odoo-mcp-1.0.0`：架構特徵（官方 `odoo` image、nginx sidecar 轉 `/websocket`、`data_dir=/var/lib/odoo`）與下文 IMIO 高度重疊，且已在平台自己的 17 個租戶 ns 生產運行數月，但原始碼下落不明，詳見下方「內部既有候選」一節與 [woow-machines-operator#3](https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/3)；#81 執行前應先嘗試取回比較，不應只以 IMIO 為唯一藍本。

## 證據與比較

### 調查方法

- Artifact Hub API：`GET /api/v1/packages/search?ts_query_web=odoo&kind=0&limit=60`（Helm chart，`kind=0`）
- GitHub API（`api.github.com`，未認證，走 rate limit）：repository search、commits、releases、issues、contents
- GitHub raw content（`raw.githubusercontent.com`）：逐檔讀 Chart.yaml／values.yaml／templates
- `tea api` 讀本 repo與跨 repo 的 Gitea issue（唯讀）

### 候選總覽（Artifact Hub 搜尋結果，共 6 筆）

| Chart | Repo/Publisher | 最新版本 | Artifact Hub 最後更新 | app_version | 可跑官方 image？ |
|---|---|---|---|---|---|
| `bitnami/odoo` | Bitnami（Broadcom） | 28.2.10 | 2025-08-08 | 18.0.20250805 | 否（見下方分析） |
| `imio/odoo` | IMIO | 3.1.0 | **2026-09-17**（研究當下前 2 天） | 18.0 | **是，`odoo:18.0` 官方 image** |
| `odoo`（OCI） | ptnglobalcorp | 0.1.0 | 2025-02-17 | 0.1.0 | 未知——`app_version` 不跟隨 Odoo 版號，無法判斷；星數/文件近乎零，視為不可行 |
| `bitnami-aks/odoo` | joaquinito2051（AKS Marketplace 鏡像） | 23.0.1 | 2022-11-17 | 16.0.20221015 | 是（Bitnami 鏡像），但停滯近 4 年、版號落後兩個大版，不可行 |
| `nicholaswilde/odoo` | nicholaswilde | 1.0.0 | 2021-06-29 | 14.0 | 未知，停滯逾 5 年、Odoo 14，不可行 |

另有 1 筆 `glasskube-operator`（發布方 glasskube，通用 Kubernetes operator，非 Odoo 專屬 chart，`description` 完全未提及 Odoo）予以排除，故上表僅列 5 筆。

GitHub repo search（`odoo helm chart` in:name,description，17 筆結果）交叉比對後，只有 `IMIO/helm-odoo` 有版號化 release、CI badge、星數與多名 contributor；其餘（`oerp-at/odoo-chart`、`phonehtut/odoo-deployment-k82`、`shivam74826/odoo-k8s-deployment`、`Linuxgurus/odoo_helm`、`bitnomee/odoo`、`pledra/helm-charts` 等）皆為 0～5 星的個人／課程／PoC 專案，無 release tag、無 Artifact Hub 上架；`oerp-at/odoo-chart` 雖 2026-09-15 有 push，但其描述明寫「Odoo helm charts for oerp based docker image」——不是官方 `odoo:18.0` image。全數排除。

**結論：在 Artifact Hub／GitHub 公開搜尋範圍內，本次調查唯一值得列入比較的候選是 IMIO/helm-odoo。**（叢集自有、非公開搜尋範圍的內部候選見下一節。）

### 內部既有候選：`odoo-tenant-1.0.0`／`odoo-mcp-1.0.0`（叢集自有，非市面搜尋範圍）

除了上述市面候選，叢集上**已經有 17 個租戶 ns** 用 `odoo-tenant-1.0.0`／`odoo-mcp-1.0.0` 這兩個 Helm chart 生產運行：`apollo-odoo`、`apun`、`bnidistrict-odoo`、`cindytech`、`evergreen-odoo`、`harumi-odoo`、`hsinpoli-odoo`、`inzense-odoo`、`komibright`、`markstudio-odoo`、`mujimed-odoo`、`puhsu-odoo`、`torchmedia`、`wassa-odoo`、`well-101`、`woowtech-odoo`、`wowcasa-odoo`（`kubectl --context woow-k3s get ns` 核實 17 個 ns 皆存在）。架構與本文比較表稱讚 IMIO 具備的關鍵特徵幾乎一致——跑官方 `odoo` image（非 Bitnami）、nginx sidecar 轉 `/websocket`、`data_dir` 走官方 image 原生路徑。

以 `harumi-odoo` ns 核實（`kubectl --context woow-k3s get pods/cm/secret -n harumi-odoo`，唯讀 get/list，未讀 Secret 值）：
- pod `harumi-odoo-*` 的 `odoo` 容器 image 為 `odoo@sha256:4ea9b4667921130add13c1b859aa170a4572b5c3c3d747bfb0ef152fdb0b48a7`（官方 image，17 個 ns 同一 digest，`18.0-20260810`）＋ `nginx:1.27-alpine` sidecar。
- `sh.helm.release.v1.odoo-tenant.v1`／`.v2` 與 `sh.helm.release.v1.mcp-odoo.v1`～`.v3` release secret 存在（只看 metadata，未讀值）。
- configmap `harumi-nginx-config` 的 `nginx.conf` 有 `upstream odoo_longpolling { server 127.0.0.1:8072; }` 與 `location /websocket { proxy_pass http://odoo_longpolling; proxy_set_header Upgrade $http_upgrade; ... }`；`harumi-odoo-config` 的 `odoo.conf` 有 `data_dir = /var/lib/odoo`。

**但這個 chart 的原始碼下落不明**：`woow-machines-operator`、`woow-paas-charts`、`paas-odoo-ci`、`paas-app-bundle-cd` 四個 repo 都找不到，已在 [woow-machines-operator#3](https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/3)（open）求助中。這代表它是一個**已經在平台自己環境驗證過官方 image 架構可行、但原始碼目前拿不到**的內部候選——與「市面上只有 IMIO 一個候選」的結論範圍不同（IMIO 是公開市面搜尋的結果；這個是叢集自有、非市面搜尋範圍的候選），不應被誤解為調查已經窮盡平台可參考的所有現成候選。**#81 執行前應優先嘗試從 woow-machines-operator#3 取回這個 chart 的原始碼，與 IMIO 做並列比較**，而非只拿 IMIO 當唯一藍本。

### IMIO/helm-odoo 詳細比較

來源：[IMIO/helm-odoo](https://github.com/IMIO/helm-odoo)（chart 原始碼）、[Artifact Hub `imio/odoo`](https://artifacthub.io/packages/helm/imio/odoo)（發佈頁）。

| 面向 | Bitnami `odoo` 28.2.10 | IMIO `helm-odoo` 3.1.0 |
|---|---|---|
| **維護狀態** | 2025-08-07 後無功能性 commit（見下節）；`open_issues`／release 節奏已停滯 | 2024-12-17 建 repo，29 個版號化 release，最新 3.1.0 於 2026-09-17（研究前 2 天）；`open_issues_count: 0`；4 個 contributor（主要 1 人 52 commits）；CI badge（test/release workflow）齊全 |
| **授權** | Apache-2.0 | Apache-2.0 |
| **維護方** | Broadcom（商業公司，2025-08 起把免費 image 移到唯讀凍結的 `bitnamilegacy`） | IMIO（比利時公部門 IT 共同體，381 個公開 repo，README 明寫「為 IMIO 自身需求設計，非通用方案，歡迎貢獻」） |
| **是否支援官方 image** | 否，`templates/deployment.yaml` 寫死 `/bitnami/odoo`、`/opt/bitnami/odoo/secrets`、`ODOO_*` env、`/docker-entrypoint-init.d`（僅 Bitnami entrypoint 認得，見 woow-paas-charts#81 背景段的行號引用） | **是**，`values.yaml` 預設 `image.repository: odoo`、`image.tag: "18.0"`，容器直接跑官方 entrypoint；README 明寫「Why do we not use the bitnami/odoo chart?」→「we want to use the official Odoo Docker Image or our custom Odoo Docker Image」 |
| **websocket（gevent 8072）** | Service 只暴露單一 `http`（8069）port（`templates/svc.yaml`），未見獨立 websocket port/route | nginx sidecar `configmap.yaml`：`upstream odoochat { server 0.0.0.0:8072; }`、`location /websocket { proxy_pass http://odoochat; proxy_set_header Upgrade ...; }`，容器同時開 `odoo-http`(8069) 與 `odoo-lp-http`(8072) 兩個 containerPort |
| **多 worker** | values.yaml 未見 `workers` 專屬鍵（走 `extraEnvVars`/自訂 configmap 才能設） | `odoo.workers: 2`（一級 values 鍵，直接寫進產生的 `odoo.conf`），另有獨立 `cron.enabled` 部署＋自己的 `workers`/`max_cron_threads` |
| **DB 初始化** | Bitnami entrypoint script（container 啟動時跑，邏輯不在 chart templates 內，在 bitnami/containers image 裡） | **Helm pre-install/pre-upgrade hook Job**（`templates/hooks/job-init.yaml`）：`prepare` initContainer 先把 Odoo/cron scale to 0 → 等 DB → `odoo -i <modules> -d <db> --stop-after-init`，之後 Deployment 才套用，避免 Odoo 對著未初始化的 DB 啟動 |
| **admin 建立方式** | Bitnami entrypoint 設 `ODOO_EMAIL`/`ODOO_PASSWORD` env 對應的 admin 帳密（entrypoint 邏輯，未在 chart 內） | **未做**——`job-init.yaml` 只跑 `odoo -i base,web --stop-after-init`，沒有後續 ORM 步驟改 admin 密碼。`.Values.odoo.admin_passwd` 只寫入 odoo.conf 的 `admin_passwd`（DB manager 主密碼），**不是** `res.users` 的 admin 登入密碼。經查 Odoo 18.0 原始碼 `odoo/addons/base/data/res_users_data.xml`：`base.user_admin` 的預設 `login`/`password` 硬編碼為 `admin`/`admin`——只跑 `odoo -i base` 不會覆蓋它。這是 Odoo 本身的預設行為，不是 IMIO chart 特有的漏洞，但代表**兩個 chart 目前都沒有「用 Secret 產生隨機 admin 密碼」這一步**，woow-paas-charts#81 規劃的「首次 init 後用 ORM 設 admin 密碼」是必要的、IMIO 沒有現成 |
| **PVC 佈局** | `persistence.*`（單一 PVC，Bitnami 慣例掛在 `/bitnami/odoo`） | 單一 PVC 掛 `/var/lib/odoo`（官方 image 原生可寫路徑），`persistence.enabled: false` 時退回 `emptyDir` |
| **擴充點** | 有（Bitnami 系列 chart 慣例支援 `extraVolumes`/`extraInitContainers`/sidecar，未逐行覆核） | `extraVolumes`／`extraVolumeMounts`／`extraInitContainers`／`extraContainers` 在主 Odoo pod **與** 獨立 `cron` pod 都各自支援；`extraEnv`／`extraEnvFrom` 同樣兩邊都有，且**init/update hook Job 也會繼承**這些設定（values.yaml 逐項註記） |
| **發佈形式** | OCI（`oci://registry-1.docker.io/bitnamicharts/odoo`） | 傳統 Helm repo（`helm repo add imio https://imio.github.io/helm-charts`），非 OCI；自 3.0.1 起 chart 有簽章可驗證 |
| **DB 依賴** | Bitnami PostgreSQL 子 chart（`bitnamilegacy/postgresql`，同樣已凍結） | 預設啟用 CloudPirates `postgres` 子 chart（官方 `docker.io/postgres` image，**明確標註 DEV/TEST ONLY**），或 `postgresql.enabled: false` + `externalDatabase.*` 接外部 DB（README 建議生產環境用 CloudNativePG） |
| **Security context** | 未逐行覆核 | Odoo 容器 `runAsUser: 100`（官方 image 的 `odoo` 系統使用者 uid），`allowPrivilegeEscalation: false`、`capabilities.drop: [ALL]`；nginx sidecar 因需 bind :80 而保留 root + 精確 capability 白名單（`CHOWN`/`SETGID`/`SETUID`/`NET_BIND_SERVICE`），values.yaml 註解說明可切換 `nginxinc/nginx-unprivileged` 完全去 root |

### Bitnami chart「已凍結」的第一手佐證

1. **`bitnami/charts` repo，`bitnami/odoo` 目錄 commit 歷史**（`GET /repos/bitnami/charts/commits?path=bitnami/odoo`）：最後一次觸及邏輯/依賴的 commit 是 2025-08-07（`Update dependency references` #35663）；之後 2025-09-01 到 2025-11-04 的 4 個 commit 全部是「移除 annotations.category」「改 README 用詞」「改 BSI 警語」等文字性變動，**逾一年沒有任何功能性 commit**（研究執行日 2026-09-19）。
2. **官方公告**：[bitnami/charts#35164](https://github.com/bitnami/charts/issues/35164)「Upcoming changes to the Bitnami catalog (effective August 28th, 2025)」——Bitnami 官方確認：自 2025-08-28 起**停止對 `docker.io/bitnami` 發佈新的 Debian-based image／Helm chart**，既有 image 逐步搬到唯讀的 [Bitnami Legacy](https://hub.docker.com/u/bitnamilegacy) repo；免費新 image 改成只維護 `latest` tag 的精簡集合。公告內另註記：原定刪除 `docker.io/bitnami` catalog 的時間已延後到 2025-09-29，中間穿插多輪 24 小時「brownout」（image 暫時不可拉取）。
3. **`bitnami/containers` repo README**：明文寫「If you are looking for our previous generation of images based on Debian Linux, please see the Bitnami Legacy registry」→連到 `hub.docker.com/u/bitnamilegacy`，確認 `bitnamilegacy` 是官方承認的「上一代」路徑，非社群自行整理。
4. **Chart 原始碼本身仍指向已死的路徑**：無論是 GitHub `main` 分支（chart 版本 28.2.11）或平台現用的 tag `odoo/28.2.10`，`values.yaml` 的 `image.repository` 都還是 `bitnami/odoo`、`tag: 18.0.20250805-debian-12-r8`——**chart 沒有被更新去指向 `bitnamilegacy`**，代表 `helm install bitnami/odoo`（不覆寫 image 值）在免費層級下已經是壞的、需要使用者自己覆寫成 `bitnamilegacy/odoo`（平台現況正是如此）。這點與 [bitnami/charts#35314](https://github.com/bitnami/charts/issues/35314) 社群提出的疑問（chart 沒有更新預設 image、免費使用者不確定該怎麼辦）一致（該 issue 標題與內文其實以 `external-dns` chart 為例，但問題屬於 Bitnami catalog 政策的通案，同樣適用於 `odoo` chart）。

### 已查證 vs 推論

**已查證**（有直接來源，如上列引用）：
- Artifact Hub 上除 Bitnami 外只有 IMIO 的 chart 符合「近期維護＋官方 image」；其餘候選版本落後或未上架。
- IMIO chart 的 websocket 路由、workers 設定、PVC 掛載路徑、DB init hook Job 邏輯、擴充點清單、發佈形式（傳統 repo + 簽章）——皆直接讀 chart 原始碼確認。
- Bitnami `bitnami/odoo` chart 目錄自 2025-08-07 起無功能性 commit；Bitnami 官方公告 2025-08-28 起免費 catalog 政策變更；GitHub 上的 chart 原始碼（含最新 main 分支）預設 image 仍指向已停止發佈的 `bitnami/odoo`。
- Odoo 18.0 官方原始碼中 `base.user_admin` 的預設帳密硬編碼為 `admin`/`admin`；IMIO 的 init Job 沒有覆蓋它的步驟。

**推論**（無法在本次調查時間內逐行覆核、但依現有證據合理推斷）：
- Bitnami chart 本身的 sidecar／extraVolumes 擴充點「應該有」（Bitnami 系列 chart 一貫支援這類 values 鍵），但未逐行比對 28.2.10 的 templates 確認完整度，故比較表該欄位標「未逐行覆核」而非直接打勾。
- Bitnami chart 的 admin 建立方式（`ODOO_EMAIL`/`ODOO_PASSWORD` env）是依 Bitnami 慣例＋平台 CLAUDE.md 背景段落的既有認知寫的，未在本次調查中重新反組譯 `bitnamilegacy/odoo` image 的 entrypoint script 逐行驗證。
- IMIO chart「作為藍本可省下多少工時」是主觀判斷，非可量化事實；依據是它與 #81 規格在架構層面的高度重疊（nginx 轉 websocket、Helm hook 做 init/update、單一 PVC、擴充點鍵名），但兩者不完全等價（見下）。

## 對 map 的影響

這張票（#1135）是 wayfinder map #1134 底下的 research 票，目的是讓「要不要自建 odoo chart」（woow-paas-charts#81）這張決策票問得更準。具體影響：

1. **「有沒有現成可用的候選」這個開放問題現在有階段性答案，但尚未窮盡**：市面（Artifact Hub／GitHub 公開搜尋）範圍內沒有能直接套用的候選，但有一個**架構高度吻合、可當 fork 起點的參考實作**（IMIO/helm-odoo）。叢集上另有一個**架構同樣吻合、且已在平台自己生產環境驗證過**的內部候選（`odoo-tenant-1.0.0`／`odoo-mcp-1.0.0`，見「內部既有候選」一節），但原始碼下落不明（woow-machines-operator#3 求助中），取回前無法真正並列比較兩者優劣。這把 #81 從「純設計」降級為「對照真實運作中的實作做差異化設計」，可以縮小 #81 執行時的方案空間、降低文件裡「這樣設計對不對」的不確定性；**但 #81 執行前應先透過 woow-machines-operator#3 取回內部 chart 原始碼，與 IMIO 並列比較後再決定藍本**，而非直接假設 IMIO 是唯一或最佳起點。
2. **#81 已列的三個驗收要求，IMIO 目前都沒做到，necessitating 平台仍需自己補**：
   - 冪等的 DB init 判斷（查 `ir_module_module` 是否已存在再決定要不要 `-i`）——IMIO 是「手動切 `init.enabled` 旗標、裝完記得關掉」的一次性模型，不適合平台 `reset_values` 驅動的自動化流程。
   - admin 密碼從 Secret 用 ORM 覆蓋——IMIO 完全沒做，兩者都會落在 Odoo 硬編碼的 `admin`/`admin`，**這是必須修的安全落差**，不是「可選加強」。
   - `values.schema.json` 嚴格模式（`additionalProperties: false` 擋殘留 Bitnami 鍵）——IMIO 的 schema 沒有鎖頂層 additionalProperties。
3. **Bitnami chart「已凍結」的證據**：可以直接引用進 #81 的背景段（或取代目前僅止於「Bitnami lineage 已凍結」一句話的鬆散陳述），佐以確切的 commit 時間線與官方公告連結，讓「為什麼要換掉 Bitnami」這個前提有可覆核的第一手依據，而不只是觀察到的症狀（`bitnamilegacy` tag 停在某天）。
4. **對 #1134 map 本身**：本票完成後，「調查 Bitnami 以外候選」這條分支可以標記完成並收斂——後續動作是 #81 的執行細節（要不要真的 `git clone` IMIO 起手，或只挑幾個 template 檔案抄），這是實作階段的決定，不在本票範圍內，留給 #81 或後續 plan 票處理。

## 驗證紀錄

驗證者回報 6 項問題，逐項附有可核實的第一手證據；重新查證後全數成立，故全數採納（無不採納項目）：

1. **採納**（major）：結論段「10 個 commit」誤植。重查 `GET /repos/bitnami/charts/commits?path=bitnami/odoo&since=2025-08-08&until=2025-11-05` 逐筆核對確為 4 筆，且與第一手佐證段落原本就寫的「4 個」互相印證，已將結論段改為「4 個」。
2. **採納**（major）：內部既有候選 `odoo-tenant-1.0.0`／`odoo-mcp-1.0.0` 原文完全未展開討論。重新以 `kubectl --context woow-k3s get ns/pods/cm/secret -n harumi-odoo`（唯讀 get/list，未讀 Secret 值）核實 17 個租戶 ns 皆存在、harumi-odoo 確實跑官方 `odoo` image＋nginx sidecar 轉 `/websocket`（`proxy_pass http://odoo_longpolling`，8072）＋`data_dir=/var/lib/odoo`，且 [woow-machines-operator#3](https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/3)（open）確認原始碼下落不明；已新增「內部既有候選」一節，並在結論、比較表結論句與「對 map 的影響」點 1 補上排除範圍說明與指向該節的連結。
3. **採納**（minor）：IMIO/helm-odoo release 數量點錯。重查 `GET /repos/IMIO/helm-odoo/releases?per_page=100` 與 `.../tags?per_page=100` 皆回傳 29 筆（非 30），已修正。
4. **採納**（minor）：Artifact Hub 搜尋第 6 筆未點名。重查 `GET /api/v1/packages/search?ts_query_web=odoo&kind=0&limit=60` 確認第 6 筆為 `glasskube-operator`（發布方 glasskube，通用 K8s operator，非 Odoo 專屬），已在候選總覽表後補充排除說明。
5. **採納**（minor）：bitnami/charts#35314 引用未說明其主題其實是 external-dns chart。重查該 issue 確認標題為「Questions on availability of legacy images, default images and ECR」、內文開頭即言明是 external-dns chart 使用者，已補充括號說明避免讀者誤以為是 Odoo 專屬佐證。
6. **採納**（minor）：「平台手動覆寫成 `bitnamilegacy/odoo`」的機制描述不夠精確。重查 `paas-system` ns 的 `paas-odoo` pod 實際 image 為自建映像 `jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo`（非 `bitnami/odoo` 也非 `bitnamilegacy/odoo`），並以 `tea api` 讀出 `paas-odoo-ci` repo 的 `Dockerfile` 首行為 `FROM bitnamilegacy/odoo:18.0.20250805-debian-12-r9`，證實平台是自建衍生映像而非直接覆寫 chart 的 `image.repository`，已改寫該段落。
