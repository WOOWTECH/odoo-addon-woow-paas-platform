---
title: 平台程式碼綁 Bitnami odoo chart／image 的耦合盤點
category: scout
status: draft
created: 2026-09-18
updated: 2026-09-18
related:
  - docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md
  - docs/reference/features/odoo-service-config.md
  - docs/reference/gotchas/bitnami-legacy-image-move.md
  - docs/decisions/cloud-odoo-addon-selection.md
---

# 盤點報告：平台程式碼綁 Bitnami odoo 的地方

> 盤點日期：2026-09-17（2026-09-18 落檔，補上 §0 與已查證項）
> 決策方向：租戶 odoo `cloud_service` 從「Bitnami chart 28.2.10 ＋ cloud-odoo image（`FROM bitnamilegacy/odoo`）」改成「官方 `odoo:18.0` image ＋ 自家 chart（以 odoo-tenant-1.0.0 為藍本）」，待 Eugene 拍板。
> 範圍：
> - woow_paas_platform：`src/`、`extra/paas-operator/`、`scripts/`、`.gitea/`、`.claude/`、`docs/`
> - 外部 repo：paas-odoo-ci、paas-app-bundle-cd、woow-paas-charts
> 產出方式：7 個切面平行掃描 → 分 4 區對抗式驗證 → 完整度批判 → 彙整；全程唯讀。行號以 2026-09-17 的 `origin/develop` 為準。

## 0. 背景與已裁決事項

**為什麼要盤點**：[harumi 移植計畫](2026-09-18-harumi-odoo-paas-migration-plan.md)發現，平台 odoo image 的 core 停在 2025-08，而叢集上要搬進平台的 odoo-tenant 站是 2026-08 的官方 image；Odoo 沒有降版路徑。已查證的事實（2026-09-17）：

- 平台 `cloud-odoo`（`paas-odoo-ci:Dockerfile.cloud:1`）與 `paas-odoo`（`paas-odoo-ci:Dockerfile:1`）都是 `FROM bitnamilegacy/odoo:18.0.20250805-debian-12-r9`。Docker Hub 上 `bitnamilegacy/odoo` 最新的 18 tag 就是它（2025-08-22 推送），`bitnami/odoo` 已沒有任何 tag ⇒ Bitnami lineage 拿不到更新的 core。
- 官方 `odoo:18.0` 最新為 `18.0-20260908`（2026-09-16 推送）。叢集上 17 個 odoo-tenant 站（含 harumi）全部跑同一個 digest `odoo@sha256:4ea9b466…`（`18.0-20260810`），直接從 Docker Hub 拉（k3s 沒有 `registries.yaml` mirror）。
- 平台租戶 odoo `cloud_service`：prod 0 個、stg 1 個（`paas-ws-stg-4bc02867/svc-42487178`）⇒ 換 lineage 對既有租戶的影響面幾乎為零。
- 「保留 Bitnami 外殼、只換 core」技術上可行：Bitnami image 的 core 在 `/opt/bitnami/odoo/lib/odoo`、套件在 `/opt/bitnami/odoo/venv`（py3.12），而 odoo 18.0 的 `requirements.txt` 在 20250805→20260908（odoo/odoo `2b3ad02c`→`db131550`，其間 4547 commits）的差異只影響 py<3.12 或 ≥3.13。但它只解 core 倒退，不解 bootstrap、admin 覆寫、每次重啟 `--update=all`、入口層、pgvector，所以本盤點以「換成官方 lineage」為前提。

**已裁決（2026-09-18）**：新的租戶 odoo image **走 JCR**——由 `paas-odoo-ci` 以 `FROM odoo:18.0@sha256:<digest>`（釘 digest）建置、加入平台 addon 後推到 `jcr-prod.woowtech.io`，不從 Docker Hub 直接拉。理由：避開 Docker Hub rate limit、不依賴上游 tag 的保留期，並與現行 `cloud-odoo` 的建置流程一致。本項回答 §6 第 8 點；「是否改用官方 lineage」本身仍待 Eugene 拍板。

**標記說明**

- **編號**：`C001`～`C112` 是驗證後清單的編號，順序和輸入清單相同。`S1`～`S4` 是撰寫本報告時補查到的，**沒有經過 verify pass**（S2 已於 2026-09-17 另行查證，見該列）。
- **impact**（影響程度，由重到輕）：
  - breaks：部署或功能直接失敗。
  - silently-wrong：看起來成功，但值是錯的或沒生效。
  - degraded：功能打折。
  - cosmetic：只影響文字或註解。
  - none：不受影響。
- **effort**（單點工作量，本報告的解讀）：S 約 0.5 人日以內，M 約 0.5～2 人日，L 超過 2 人日。
- **驗證狀態**：
  - confirmed：已確認。
  - adjusted：verify 修正過 scope、impact、effort 或行號。
  - **found-in-verify** 與 **found-in-critic**：分別在 verify 和 critic 階段新發現的條目。
  - **補查**：本報告撰寫時補查，未經 verify。

---

## 1. 結論摘要

### 1.1 耦合點數量

驗證清單共 112 條，另補查 4 條（S1～S4）。剔除 36 條，其中 duplicate 35 條、refuted 1 條，見附錄。

| scope ＼ impact | breaks | silently-wrong | degraded | cosmetic | none | 合計 |
|---|---|---|---|---|---|---|
| tenant-odoo（本案） | 24 | 4 | 9 | 0 | 0 | **37** |
| shared | 1 | 0 | 2 | 1 | 3 | 7 |
| test | 5 | 0 | 0 | 3 | 4 | 12 |
| paas-odoo-self（另案） | 19 | 7 | 2 | 2 | 2 | 32 |
| doc | 0 | 0 | 1 | 20 | 3 | 24 |
| **合計** | **49** | **11** | **14** | **26** | **12** | **112** |

- **tenant-odoo 的 effort 分布**：L 7 條、M 12 條、S 18 條。
- **驗證狀態分布**：confirmed 80、adjusted 8、found-in-verify 16、found-in-critic 8。
- **本案實際要動約 61 處**：
  - tenant-odoo 37 條
  - shared 中需處理的 3 條（C006、C010、C066）
  - test 中會 breaks 的 5 條（C019、C020、C021、C033、C054）
  - 租戶相關 docs 12 條
  - 補查 4 條
- 另有 10 條屬 paas-odoo-self 的 docs／腳本，以及 2 條已執行的歷史 migration，本案都不需要改。
- 改動分布在 3 個 repo：woow_paas_platform、paas-odoo-ci，以及要在 woow-paas-charts **新建**的 chart（sweep 確認該 repo 目前沒有任何 odoo chart）。

### 1.2 整體改動規模（人日粗估）

| 分期 | 內容 | 粗估 |
|---|---|---|
| P0 | 自家 chart（5～8）、cloud-odoo image（1～2）、seed／values／init／expose（3～5）、operator init 路徑（1～1.5）、同 PR 必改的測試（1～1.5）、dev sandbox 端到端驗證（2～3） | 13～20 |
| P1 | addon 注入重做（4～6）、operator module 路徑（1～2）、plan overlay（1～2）、SMTP（1～2）、credentials／reset（2～4）、image 升版時的模組升級（1～2） | 10～18 |
| P2 | slug 閘門改成 capability（1～3）、清理 Bitnami 殘留與測試（1～2）、docs（1～2）、chart 版號治理＋operator integration test（1～2） | 4～9 |
| **合計** | | **27～47 人日**（單人約 6～10 週，中位數約 35） |

估算假設：

1. 由一位熟悉本 repo 的工程師（含 agent 輔助）執行。人日不含 review、prod build `/approve` 等人工 gate 的等待時間。
2. 叢集上 odoo-tenant-1.0.0 chart 的原始碼拿得到，可以 fork。拿不到的話，chart 另加 3～5 人日。
3. stg 唯一的實例 svc-42487178 以銷毀重建處理，不做資料遷移。要遷移的話加 2～4 人日。
4. addon 選擇、plan overlay、SMTP 功能要做到對等。
5. 不含 paas-odoo-self。

### 1.3 最大的工程難點

1. **bootstrap 責任轉移（平台目前 100% 隱性依賴 Bitnami entrypoint）**
   - 平台 model 層完全沒有建 DB、`-i base`、設定 admin 帳密的程式碼（C007、C008、C013、C047）。
   - 非首次啟動自動跑 `--update=all` 的升版語意也是隱性依賴（S2，已查證）。
   - 換成官方 image 後，這些全部要從零設計，而且要冪等、可重入 `reset_values` upgrade。
2. **addon 選擇機制要重做，而且三條路徑要對稱**
   - 目前靠 Bitnami 的 initContainer ＋ `customPostInitScripts` ＋ `/bitnami/odoo` PVC 慣例（C001～C003、C011、C018）。
   - create、reconfigure、remove 三條路徑（C023、C024、C046）要同步改。
   - 還要和 operator 端 catalog 白名單、stale cleanup 保持一致（C053、C058）。
3. **「靜默失效」類的 chart 契約**
   - storageClass 路徑（C005）、image_ref schema（C004、C025）、addon 拔除（C018）、imagePullSecrets（C006）、孤兒 PVC label（C066）、expose 自動偵測 Service（S1）都屬於這類。
   - plan overlay 在新 seed 下其實會靜默跳過（C015，見 §2.1）。
   - 這些寫錯都不會報錯，helm 照樣綠燈。需要用 `values.schema.json` ＋ chart render 測試把它們轉成 fail-loud。
4. **憑證安全不變式要重建**
   - 管理員密碼目前走 `helm_values`，受 #559 欄位 ACL、#535 遮罩保護（C020、C021、C035）。
   - 改進 Secret 或 bootstrap 後，要重新確認租戶讀不到明文。
   - 還要裁決是否接入 #357 的一次性憑證與重設密碼流程（C007）。
5. **驗證回饋迴圈長**
   - PR CI 不跑 Odoo 測試；operator 的 odoo exec 路徑沒有 integration test，只有 C054 斷言了直譯器路徑。
   - cloud-odoo image build 要經過人工核可 gate。
   - 既存的 Bitnami release 不能 in-place helm upgrade 到新 chart。
   - 結果是大多只能靠 dev sandbox 做端到端驗證。

---

## 2. 耦合地圖

paas-odoo-self 的條目只列在 §5。本節列 tenant-odoo、shared、test、doc 四類，外加補查的 4 條。

### 2.1 values 組裝與 overlay

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C004 `src/models/cloud_service.py:1641` | `_assemble_deploy_values`（`:1540`）的 addon 分支（`:1639`）把 image 視為 `{registry, repository, tag}` 三段式，registry 預設 `docker.io` | silently-wrong：新 schema 下會組出 `docker.io/:latest` 這類壞掉的 ref，initContainer 卡 ImagePullBackOff | 依新 chart 的 image schema 改寫。和 C025 是同一邏輯的兩份實作，預設值還不一致，建議收斂成一個 model helper | S | tenant-odoo | confirmed |
| C025 `src/controllers/paas.py:3681` | reconfigure 路徑重複組 image_ref，registry 預設空字串 | silently-wrong：同 C004 | 同 C004，改呼叫 model helper | M | tenant-odoo | confirmed |
| C006 `src/models/cloud_service.py:1850` | `_inject_image_pull_secrets` 假設「所有 Bitnami chart 都吃 `global.imagePullSecrets`，而且會傳到 initContainers」；對所有 template 一律套用，沒有 path map | breaks：私有 JCR 上的 cloud-odoo image 拉不下來，平台端也沒有錯誤訊號 | 新 chart 同時消費 `global.imagePullSecrets` 與頂層 `imagePullSecrets`（operator `extra/paas-operator/src/api/releases.py:128` 會 setdefault 頂層鍵）；或比照 C005 做 path map | M | shared | confirmed |
| C062 `extra/paas-operator/src/api/releases.py:128` | docstring 說「bitnami odoo 是 public image，不消費頂層 imagePullSecrets」 | cosmetic | 更新註解。邏輯是 setdefault，本身與 chart 無關 | S | shared | confirmed |
| C015 `src/models/cloud_service.py:4748` | plan 的 `cs_odoo_workers`／`cs_ram_gb` 靠字串替換 `customPostInitScripts` 內的 `workers = 0`、`limit_memory_hard/soft = 0`，並斷言「合計恰 1 處」；`resources`、`persistence.size` 寫在頂層，假設 chart 只有 odoo 一個 workload | breaks（複查補充：`:4772` 是 `if plan.cs_odoo_workers and scripts:`，新 seed 沒有 `customPostInitScripts` 時會**靜默跳過**，實際是 silently-wrong；若 C003 的 fallback 仍寫入 script，則因 marker 為 0 處而 raise） | 整支改成寫 chart 原生 values（例如 `odoo.config.workers`、`odoo.config.limitMemoryHard`），由 ConfigMap render。建議新 chart 保留頂層 `resources`、`persistence` 作用於 odoo 容器，減少平台改動。附帶好處：修掉「post-init 只跑一次，plan 變更對既有 PVC 不生效」的潛在問題（推論，未實測） | L | tenant-odoo | confirmed |
| C009 `src/models/sh_subscription_plan_cs_spec.py:25` | `cs_odoo_workers` 的 help 用 Bitnami init script 的預設值（`workers=0, threaded`）定義語意 | degraded（文義錯誤） | 改寫 help，和 C015、C032 同步 | S | tenant-odoo | confirmed |

### 2.2 bootstrap 與 post-deploy init

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C013 `src/data/cloud_app_templates.xml:242` | `helm_default_values` 整段是 Bitnami 形狀，細節見表下說明 | breaks：新 chart 收到一堆不認得的鍵；DB、admin、odoo.conf 系統設定沒人處理 | 為新 chart 全段重寫。odoo.conf 改成 ConfigMap 模板；admin 密碼改由 bootstrap 用 ORM 寫入（§3.3） | L | tenant-odoo | confirmed |
| C008 `src/models/cloud_service.py:3592` | `_model_init_odoo` 只刷新 module list。前提是 `_is_container_ready`（純容器 Ready）成立時，entrypoint 已經建好 DB 和 admin。dispatch 在 `:3541`，seed 在 `src/data/cloud_app_templates.xml:275` | breaks：對空 DB 操作會失敗 | 建議把 bootstrap 移進 chart initContainer，讓「Ready」本身就代表 DB 已 init（§3.3），這支 hook 只保留增量工作。若改由 operator 負責 bootstrap，要另外新增「DB 已 init」判準 | M | tenant-odoo | confirmed |
| C047 `src/controllers/paas.py:4321` | controller 的 `_run_post_deploy_init`（`:4200`）在 `post_deploy_init_type=='odoo'` 分支同樣假設 DB 已 init；重試 5 次後轉 `error` | breaks：服務卡在 `initializing`，最後變 `error` | 和 C008 同步改。controller（`src/controllers/paas.py:4200`）與 model（`src/models/cloud_service.py:3531`）是兩份實作，建議先收斂 | L | tenant-odoo | **found-in-verify** |
| S2 `src/data/cloud_app_templates.xml:242`（隱性依賴：沒有設 `ODOO_SKIP_MODULES_UPDATE`） | Bitnami entrypoint 在非首次啟動時預設跑 `--update=all`，所以租戶 image 升版後模組 schema 會自動升級 | breaks：官方 image 升版後不會 `-u`，程式碼與 DB schema 版本脫節 | 新 chart 的 bootstrap 比對受管模組的 image 版號與 DB 版號，有落差才跑 `-u <list>`（參考 C084 的 module-version-gate） | M | tenant-odoo | **補查，已查證**：bitnami/containers `81704d52` 的 `bitnami/odoo/18/debian-12/rootfs/opt/bitnami/scripts/libodoo.sh:196-209`（非首次開機未 skip 就 `--update=all`）、`odoo-env.sh:96-97`（`ODOO_SKIP_MODULES_UPDATE` 預設 `no`）；stg `svc-42487178-odoo` 的 env 沒有設這個變數 |

C013 的 Bitnami 形狀細節：

- `odooEmail`、`odooSkipInstall=false`、`loadDemoData`：觸發 entrypoint 自動建庫、建 admin。
- `postgresql.auth.{username: bn_odoo, database: bitnami_odoo}` 與 `postgresql.image=bitnamilegacy/postgresql`：Bitnami 內建 postgresql 子 chart 的慣例。
- `global.security.allowInsecureImages`：Bitnami 專屬開關。
- `customPostInitScripts.fix-admin-credentials.sh`：用 `/opt/bitnami/odoo/venv/bin/python3` ＋ passlib 直接下 SQL `UPDATE res_users ... WHERE id=2`。
- `customPostInitScripts.odoo-conf-system.sh`：用 sed 改 `/opt/bitnami/odoo/conf/odoo.conf` 的 `proxy_mode`、`list_db`、`workers`、`limit_*`。

### 2.3 credentials／admin 密碼

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C007 `src/models/cloud_app_template.py:245` | `cloud_app_odoo` 沒有設 `has_admin_gui`（預設 False），所以 `_build_mcp_sensitive_values`（`src/models/cloud_service.py:2187`）等 #357 admin_password 供給、重設鏈對 Odoo 從未生效；完全靠 Bitnami 讀 `odooEmail`／`odooPassword` 建 admin | breaks：官方 image 下沒人建 admin，也沒有任何重設密碼的能力 | 從零設計 admin 建立流程（§3.3），並裁決是否接入 #357 一次性憑證與「重設管理員密碼」 | L | tenant-odoo | confirmed |
| C014a `src/data/cloud_app_templates.xml:243`（`odooEmail`、`odooPassword`、`loadDemoData`） | helm_value_specs 的鍵一對一對應 Bitnami values 與 entrypoint env（`ODOO_EMAIL`、`ODOO_PASSWORD`、`ODOO_LOAD_DEMO_DATA`） | breaks：UI 顯示成功，但沒人消費 | 改成新 chart 的 admin 鍵（值建議落 Secret），和 C022、C020、C021、C035 同步 | L（與 C014b 合計） | tenant-odoo | confirmed |
| C022 `src/controllers/paas.py:3381` | 建立服務時寫死 `.get('odooPassword', '')` 驗證長度至少 8 | breaks：鍵改名後永遠取到空字串，所有 Odoo 建立請求都被擋 | 改成 spec-driven（讀 `type=password` 的 spec），或同步改鍵名 | S | tenant-odoo | adjusted（impact：silently-wrong → breaks） |

### 2.4 SMTP

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C014b `src/data/cloud_app_templates.xml:243`（`smtpHost/Port/User/Password/Protocol`） | Bitnami chart 把 `smtp*` 轉成 `ODOO_SMTP_*` env，由 entrypoint 寫進 odoo.conf；密碼由 chart 轉成 K8s Secret | breaks（不生效）：官方 image 不吃這些鍵，租戶填了也沒用 | 二選一：(a) chart 用 Secret ＋ ConfigMap render odoo.conf 的 `smtp_server`、`smtp_port`、`smtp_user`、`smtp_password`、`smtp_ssl`，行為與 Bitnami 等價；(b) bootstrap 建 `ir.mail_server` record。建議 (a)，但 `smtpProtocol=ssl` 在 conf 層是否有等價設定待確認（§6） | 同 C014a | tenant-odoo | confirmed |

相關文件 C088、C089、C110（ADR D1）見 §2.13。

### 2.5 addon 注入與模組管理

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C001 `src/models/cloud_service.py:34` | `ODOO_SELECTED_ADDONS_PATH='/opt/bitnami/odoo/selected-addons'` | breaks | 改成新 chart 的路徑（例如 `/mnt/extra-addons/selected`），和 C019 同步 | S | tenant-odoo | confirmed |
| C002 `src/models/cloud_service.py:1710` | addon-selector initContainer 讀 PVC 上的 `/bitnami/odoo/conf/odoo.conf`，volume 掛在 `/bitnami/odoo`（`:1723`） | breaks | 重寫 script 與 volumeMount。addons_path 改由 ConfigMap 直接宣告，initContainer 只負責複製 | L | tenant-odoo | confirmed |
| C003 `src/models/cloud_service.py:1734` | fallback 走 `customPostInitScripts['00-addons-path.sh']`，用 sed 改 `/opt/bitnami/odoo/conf/odoo.conf` | breaks：官方 image 沒有這個 hook | 移除，改由 chart 原生 values 提供 addons_path | L | tenant-odoo | confirmed |
| C011 `src/models/cloud_service.py:1703` | initContainer 用和主容器相同的 image 讀 `/all-addons/$addon`，這是 cloud-odoo image 自訂的目錄 | degraded：複製全部失敗（`! $addon not found`） | 新的 `Dockerfile.cloud` 保留 `/all-addons/` 這一層 | M | tenant-odoo | adjusted（scope：shared → tenant-odoo） |
| C018 `src/models/cloud_service.py:1744` | `_remove_addon_helm_values` 用 Bitnami values 鍵 `initContainers`、`extraVolumes`、`extraVolumeMounts`、`customPostInitScripts` 做名稱比對來拔除 | silently-wrong：租戶取消勾選後，舊的 initContainer 與 volume 殘留 | 和新的 `_build_odoo_addon_helm_values` 對稱重寫。建議改成單一 values 子樹（例如 `addons.selected: [...]`）整鍵覆寫，就不需要拔除邏輯 | M | tenant-odoo | **found-in-verify** |
| C023 `src/controllers/paas.py:3450` | create 路徑用 `slug=='odoo'` 當閘門，放行 Bitnami 式 initContainer 注入 | breaks：下游 values 對不上新 schema | 和 model 層一起改；閘門改成 capability（§3.1） | M | tenant-odoo | confirmed |
| C024 `src/controllers/paas.py:3620` | reconfigure 路徑有同款 slug 閘門 | breaks | 同 C023 | M | tenant-odoo | confirmed |
| C046 `src/controllers/paas.py:3680` | reconfigure 時重跑 `_build_odoo_addon_helm_values`，或 `_remove_addon_helm_values`（`:3691`） | breaks | 跟著 C002、C018 換內容；rebuild／remove 的分支結構可以保留 | S | tenant-odoo | confirmed |
| C010 `src/models/cloud_odoo_addon.py:9` | `CATALOG_PATH='/all-addons/catalog.json'` 是平台自身 pod image（paas-odoo-ci `Dockerfile` lineage）烤進去的自訂目錄 | degraded：讀不到時靜默回 0 筆，marketplace 的 addon 選單消失 | 本案不動平台 image。但平台 image 與租戶 image（`Dockerfile.cloud`）的 catalog 必須一致，換 image 時列為驗收項 | M | shared | confirmed |

### 2.6 storage／PVC

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C005 `src/models/cloud_service.py:1785` | `_inject_storage_class.path_map['odoo']` 是 `persistence.storageClass` ＋ `postgresql.primary.persistence.storageClass`（Bitnami 子 chart 路徑） | silently-wrong：鍵錯了只會落到叢集預設 StorageClass（woow-k3s 有兩個 default，可能落到 node-local） | 依新 chart 改路徑；新 chart 的 `values.schema.json` 禁止未知鍵，讓錯鍵 fail-loud | S | tenant-odoo | confirmed |
| C066 `extra/paas-operator/src/api/releases.py:356` | 孤兒 PVC 延遲清理用 `app.kubernetes.io/instance={release}` 撈 PVC；Bitnami postgres StatefulSet 的 volumeClaimTemplate 會自動帶上這個 label | degraded：新 chart 的顯式 PVC 若沒手動加 label，清理機制靜默失效，儲存空間持續累積 | 列入新 chart 驗收清單：所有 PVC 都帶 instance label。operator 程式不用改 | S | shared | **found-in-verify** |

補充：Bitnami 的 filestore 在 `/bitnami/odoo/data/filestore/<db>`，官方 image 在 `/var/lib/odoo/filestore/<db>`；postgres 也從子 chart 的 StatefulSet（`bn_odoo`／`bitnami_odoo`）換成獨立 Deployment。只有在要保留 stg 實例資料時才需要遷移（§3.2）。

### 2.7 網路與 URL

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| S1 `extra/paas-operator/src/api/releases.py:1606` | expose 沒帶 service_name／port 時會自動偵測：在 instance label 下取「第一個非 headless Service」（K8s 依名稱排序），port 優先取 `[80, 8080, 3000, 8000, 443, 8443]`，否則取第一個。Bitnami 的 `<release>-odoo`（port 80）排在 `<release>-postgresql` 前面，所以剛好選對 | silently-wrong：新 chart 的 postgres Service 若排序在前（例如 `<release>-db`），或 odoo Service 埠不在偏好清單內、順序不對，Cloudflare route 會指到 DB 或錯的埠 | 平台 `_build_expose_config`（`src/models/cloud_service.py:1658`）依 profile 明確帶 `service_name`／`service_port`（operator 已經支援）；或新 chart 的主 Service 用裸 release fullname ＋ port 80 | S | tenant-odoo（程式是 shared） | **補查（未經 verify）** |
| S3 `src/data/cloud_app_templates.xml:266` | `default_port=8069`（Bitnami 容器埠）寫進 `internal_port`（`src/controllers/paas.py:3431`），只在 `src/static/src/paas/pages/service/tabs/OverviewTab.xml:70` 顯示 | cosmetic：nginx sidecar 架構下顯示的埠不準 | 改成新 chart 的對外埠 | S | tenant-odoo | **補查（未經 verify）** |

補充：

- `proxy_mode`、`list_db` 目前由 C013 的 `odoo-conf-system.sh` 注入。Cloudflare tunnel 前面必須保留 `proxy_mode=True`。
- `workers>0` 時 websocket 需要分流到 gevent 8072。Bitnami 租戶路徑目前只開了 8069（推論；paas-odoo-self 是靠 C105、C106 補上的），新 chart 的 nginx sidecar 應該處理 `/websocket`。

### 2.8 operator exec 腳本

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C049 `extra/paas-operator/src/services/odoo_init.py:80` | container 名寫死 `f"{release_name}-odoo"`（Bitnami `common.names.fullname`；stg 實測為 `svc-42487178-odoo`） | breaks：exec 進不存在的 container | chart 契約固定 container 名（例如 `odoo`），operator 收斂成單一 helper | M | tenant-odoo | adjusted（effort → M） |
| C056 `extra/paas-operator/src/services/odoo_init.py:126` | pod selector 用 `app.kubernetes.io/name=odoo` | degraded：有 instance-only fallback，但 `items[0]` 沒有排序保證，可能挑到 postgres pod（exec 會 fail-loud） | 新 chart 加 `app.kubernetes.io/component=odoo`，selector 改用它 | S | tenant-odoo | confirmed |
| C050 `extra/paas-operator/src/services/odoo_init.py:178`（另有 `:271`） | 直譯器寫死 `/opt/bitnami/odoo/venv/bin/python3` | breaks | 改成官方 image 的系統 `python3` | S | tenant-odoo | confirmed |
| C051 `extra/paas-operator/src/services/odoo_module.py:69` | 同 C049（重複實作） | breaks | 和 C049 同一次改 | M | tenant-odoo | adjusted（effort → M） |
| C057 `extra/paas-operator/src/services/odoo_module.py:62` | 同 C056 | degraded | 同 C056 | S | tenant-odoo | confirmed |
| C052 `extra/paas-operator/src/services/odoo_module.py:138`（另有 `:170`） | `parse_config(['-c', '/opt/bitnami/odoo/conf/odoo.conf'])` ＋ venv python | breaks | 改成新 chart 的 conf 掛載點（例如 `/etc/odoo/odoo.conf`）＋系統 python3 | S | tenant-odoo | confirmed |
| C053 `extra/paas-operator/src/services/odoo_scripts/update_module_list.py:38` | `ODOO_CONF_PATH` 是 Bitnami 路徑；#748 的 docstring 描述 Bitnami symlink 佈局 | breaks：`parse_config` 丟例外，變成 `OdooInitError` | 只改常數即可：域限制已改用 managed∩on_disk，與檔案佈局解耦。和 C052 同步 | S | tenant-odoo | adjusted（impact → breaks、effort → S） |
| C058 `extra/paas-operator/src/services/odoo_scripts/update_module_list.py:48` | `MANAGED_CATALOG_PATH='/all-addons/catalog.json'` 烤在 cloud-odoo image 裡 | degraded：讀不到就跳過 cleanup（fail-safe），stale record 持續累積 | 新 `Dockerfile.cloud` 保留這個 COPY | M | tenant-odoo | confirmed |

缺口：這三支腳本的容器名、conf 路徑都沒有測試斷言，也沒有 integration test（sweep 確認）。只有 C054 斷言了直譯器路徑。

### 2.9 UI

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C026 `src/static/src/paas/pages/configure/AppConfigurationPage.js:197` | `isOdooTemplate` 用 slug 判斷，隱含「有 Bitnami 式 addon 注入能力」 | degraded（設計債） | slug 維持 `odoo` 的話短期不用改。P2 改成鏡射後端 capability 欄位（例如 `supports_addon_selection`），比照 has_admin_gui 的模式 | S | tenant-odoo | confirmed |
| C027 `src/static/src/paas/pages/configure/AppConfigurationPage.js:356` | 只有 slug 閘門成立時才送 `odoo_addon_ids` | degraded | 跟著 C026 | S | tenant-odoo | confirmed |
| C028 `src/static/src/paas/pages/service/ServiceDetailPage.js:175` | `isOdooService` 是檔案內唯一殘留的 slug 判斷 | degraded | 同 C026 | S | tenant-odoo | confirmed |
| C029 `src/static/src/paas/pages/service/ServiceDetailPage.xml:178` | Addons 分頁按鈕與 `:217-221` 的 AddonsTab 掛載都以 `isOdooService` 控制 | degraded | 跟著 getter，不用單獨改 | S | tenant-odoo | confirmed |

補充：HelmValueForm 與 ConfigurationTab 的密碼遮罩是 spec-driven，不受換 chart 影響（C031 只是註解範例）。

### 2.10 seed／migrations

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C012 `src/data/cloud_app_templates.xml:235` | `helm_chart_name=oci://registry-1.docker.io/bitnamicharts/odoo`，版本 28.2.10（`:236`） | breaks（整個切換的閘門） | 改成自家 chart 座標與版號，和 C013、C014 同一個 PR | S | tenant-odoo | confirmed |
| S4 `src/data/cloud_app_templates.xml:221` | 註解寫「租戶側無既存服務」（2026-08-03 實測） | cosmetic，但會誤導遷移評估：stg 其實有 svc-42487178 | 更正註解；換 chart 的 PR 要處理既存實例（§3.2） | S | doc | **found-in-critic（本報告採納）** |
| C016 `src/migrations/18.0.1.0.2/post-migrate.py:38` | 改寫 `postgresql.image.*`（Bitnami 子 chart 路徑） | none：已執行的歷史 migration；證明 Bitnami 搬遷已經咬過平台一次 | 不回改 | S | doc | confirmed |
| C017 `src/migrations/18.0.1.0.1/post-migrate.py:36` | `SPECS_BY_SLUG['odoo']` 寫入 odooEmail、odooPassword、loadDemoData | none | 不回改 | S | doc | confirmed |

補充：seed 是 `noupdate="0"`（`src/data/cloud_app_templates.xml:4`），改了隨 `-u` 就生效，template record 不需要另寫 migration。但若要對既存的 odoo cloud_service 做守門或標記，就要新增 `src/migrations/<X>/` 並依 #691 bump manifest。

### 2.11 tests

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C019 `src/tests/test_helm_values_addon_builder_1052.py:26` | `_PINNED_PATH` 是 Bitnami 路徑，並斷言 initContainers、extraVolumes、customPostInitScripts 的形狀 | breaks | 跟著 C001～C003、C018 改寫 | M | test | confirmed |
| C020 `src/tests/test_credential_mask_spec_driven_535.py:34` | anchor 是 `{'odooPassword', 'smtpPassword'}`；另有 `:327-360` 的 class | breaks | 跟著 C014 | S | test | confirmed |
| C021 `src/tests/test_helm_values_field_acl_559.py:69` | fixture 用明文 odooEmail／odooPassword，`:166` 斷言 `***` | breaks，或變成假綠 | 跟著 C014 | S | test | confirmed |
| C035 `src/tests/test_helm_values_field_acl_559.py:68` | 整份 #559 欄位 ACL 以「憑證放在 helm_values」為前提 | breaks | 密碼改進 Secret、不再落 helm_values 的話，要重新確認這個不變式由誰把關 | M | tenant-odoo | confirmed |
| C033 `src/tests/test_seed_chart_version_pin_552.py:47` | `EXPECTED_PINS['cloud_app_odoo']='28.2.10'` | breaks（設計上就該紅） | 在換 chart 的同一個 commit 改釘值 | S | test | confirmed |
| C034 `src/tests/test_seed_chart_version_pin_552.py:109` | 以「第三方 registry chart」為前提，含 `assertNotIn('jcr-prod.woowtech.io')` 自我失效斷言 | breaks（設計上就該紅） | 從涵蓋集合移除 cloud_app_odoo，另立自家 chart 版號治理守線 | S | tenant-odoo | confirmed |
| C036 `src/tests/test_cs_spec_332.py:146` | workers overlay 斷言 customPostInitScripts 內容 | breaks | 改斷言新 values 鍵，並核對 `persistence.size`、`resources` 的鍵路徑 | M | tenant-odoo | confirmed |
| C054 `extra/paas-operator/tests/test_odoo_init_cleanup.py:543` | 斷言 command 前綴是 venv python（唯一的 canary） | breaks | 跟著 C050 | M | test | confirmed |
| C059 `extra/paas-operator/tests/test_odoo_init_cleanup.py:287` | fixture 的 addons_path 含 Bitnami symlink（已經是死資料） | cosmetic | 簡化 | S | test | confirmed |
| C060 `extra/paas-operator/tests/test_odoo_init_cleanup.py:213` | `db_name="bitnami_odoo"` | cosmetic | 改成中性名稱 | S | test | confirmed |
| C044 `scripts/audit/test_collection_audit.py:923` | 裸 traceback fixture 含 `/opt/bitnami/...` 路徑 | cosmetic | 選擇性更新 | S | test | confirmed |
| C037 `src/tests/test_credential_mask_spec_driven_535.py:180` | `bn_odoo` 字面量其實掛在 opendesign template 上 | none | 不改 | S | test | confirmed |
| C038 `src/tests/test_cloud_app_template.py:35` | 只拿 bitnami repo URL 當範例 | none | 不改 | S | test | confirmed |
| C039 `src/tests/test_mcp_integration.py:248` | 只拿 bitnami repo URL 當範例 | none | 不改 | S | test | confirmed |
| C040 `src/tests/test_suspended_redeploy_state_482.py:32` | docstring 以 `primary.replicaCount` 論證 scale 要與 chart 無關 | none：其他第三方 chart 仍適用 | 不改 | S | shared | confirmed |
| C055 `extra/paas-operator/tests/test_reset_requires_pinned_version.py:56` | bitnami OCI URL 當 remote 樣本 | none | 不改 | S | test | confirmed |
| C063 `extra/paas-operator/tests/test_scale.py:3` | 「Bitnami 系 release 以 StatefulSet 部署」（BUG-7） | none：odoo 退出 StatefulSet 名單，其他 template 仍需要 | 不改 | S | shared | confirmed |
| C064 `extra/paas-operator/tests/test_helm.py:323` | bitnami postgresql OCI 當範例 | none | 不改 | S | shared | adjusted（行號） |

缺口：`.gitea/workflows/odoo-tests.yml` 不掛 `pull_request`，所以動 `src/` 的 PR 要自己在沙盒跑 `/woow-dev:k8s-dev-sandbox test <slug> --module woow_paas_platform`。

### 2.12 CI／image build

| 位置 | Bitnami 假設 | 不改的後果（impact） | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C067 `paas-odoo-ci:Dockerfile.cloud:1` | `FROM bitnamilegacy/odoo:18.0.20250805-debian-12-r9`，繼承 Bitnami 的佈局、entrypoint 與 venv | breaks | 改成 `FROM odoo:18.0`，建議釘 dated tag 或 digest 並 mirror 到 jcr-prod；確認 `USER odoo` 下的 COPY 與目錄權限 | M | tenant-odoo | confirmed |
| C068 `paas-odoo-ci:Dockerfile.cloud:18` | `/opt/bitnami/odoo/venv/bin/pip install` | breaks | 改用系統 `python3 -m pip`（受 PEP 668 限制，要加 `--break-system-packages`，或自建 venv 並讓 odoo 用它） | S | tenant-odoo | confirmed |
| `paas-odoo-ci:.gitea/workflows/build-cloud-image.yml`、`paas-odoo-ci:config/cloud-addons.list`、`paas-odoo-ci:scripts/copy-addons.sh` | 查無 Bitnami 耦合。租戶的 catalog 由 `generate-catalog.sh` 在 CI host 產生，見 C073 | none | 不用改；但 build 要過人工核可 gate，排程要預留時間 | — | tenant-odoo | sweep 確認 |

### 2.13 docs

租戶相關（本案要改）：

| 位置 | Bitnami 假設 | impact | 需要的改動 | effort | scope | 驗證 |
|---|---|---|---|---|---|---|
| C088 `docs/reference/features/odoo-service-config.md:45` | SMTP 傳進 Bitnami chart 的 Helm values | cosmetic | 改寫成新機制 | S | doc | confirmed |
| C089 `docs/reference/features/odoo-service-config.md:92` | SMTP 密碼由 Bitnami 轉成 Secret | cosmetic | 改寫 | S | doc | confirmed |
| C092 `docs/reference/features/odoo-service-config.md:74` | 系統層設定靠 initContainer 注入 | cosmetic | 改寫成 ConfigMap | S | doc | confirmed |
| C090 `docs/reference/features/odoo-service-config.md:105` | Dependencies 依賴 Bitnami entrypoint | cosmetic | 整段重寫 | S | doc | adjusted（行號 104 → 105） |
| C091 `docs/reference/features/odoo-service-config.md:110` | Constraints 裡兩條 Bitnami 限制 | cosmetic | 重寫 | M | doc | confirmed |
| C110 `docs/decisions/refactor/odoo-service-config-enhancement.md:20` | ADR D1、D2 的前提是 Bitnami 原生封裝 | cosmetic（會誤導） | 標記 superseded，或寫新 ADR | S | doc | **found-in-critic** |
| C111 `docs/decisions/cloud-odoo-addon-selection.md:28` | 「paas-charts 不需改動（Bitnami 原生支援 initContainers）」 | cosmetic | 更新結論 | S | doc | **found-in-critic** |
| C112 `docs/reference/gotchas/bitnami-legacy-image-move.md:8` | Odoo base image 座標 | cosmetic | 標為歷史紀錄，或更新 | S | doc | **found-in-critic** |
| C093 `paas-odoo-ci:docs/sop-ci.md:22` | paas-odoo 與 cloud-odoo 兩條 image 線都寫成「基底 bitnamilegacy/odoo」 | cosmetic | 本案改 cloud-odoo 段，paas-odoo 段另案改 | S | doc | **found-in-verify** |
| C065 `extra/paas-operator/src/services/helm.py:1905` | docstring 說 odoo 主體是 StatefulSet（實際是 Deployment） | cosmetic | 修正 | S | doc | confirmed |
| C030 `src/controllers/paas.py:3156` | docstring 以 `odooPassword` 當範例（機制本身 spec-driven） | cosmetic | 選擇性更新 | S | doc | confirmed |
| C031 `src/static/src/paas/pages/service/tabs/ConfigurationTab.js:118` | 註解範例 | cosmetic | 不用改 | S | doc | confirmed |
| C032 `src/i18n/zh_TW.po:8789` | `cs_odoo_workers` help 的翻譯 | cosmetic | 跟著 C009 | S | doc | confirmed |

self lineage 相關（本案不改，另案才改）：

| 位置 | impact | 驗證 |
|---|---|---|
| C043 `.claude/skills/deploy-stg/SKILL.md:320` | none | confirmed |
| C048 `.claude/skills/deploy-stg/scripts/verify-addon-version.sh:104` | cosmetic | **found-in-verify** |
| C045 `scripts/audit/race_global_instance_cap.py:28` | cosmetic | confirmed |
| C076 `paas-odoo-ci:README.md:23` | cosmetic（檔頭已自陳過時） | confirmed |
| C094 `paas-odoo-ci:CLAUDE.md:10` | cosmetic | **found-in-verify** |
| C095 `paas-odoo-ci:.claude/commands/woow/build-paas.md:112` | cosmetic | **found-in-verify** |
| C096 `paas-odoo-ci:.gitea/workflows/check-addon-lists.yml:5` | cosmetic | **found-in-verify** |
| C102 `paas-app-bundle-cd:docs/promote-acceptance-checklist.md:101` | degraded（M；runbook 指令路徑全錯） | **found-in-verify** |
| C103 `paas-app-bundle-cd:docs/sop-cd.md:38` | cosmetic | **found-in-verify** |

---

## 3. 設計建議

### 3.1 不再把 chart 細節寫死在 `slug=='odoo'` 分支：template 層宣告 chart profile

**問題**：目前有四類 chart 知識散在程式碼裡，而且大多沒有機器守線。

| 類別 | 目前寫死的地方 |
|---|---|
| values 鍵路徑 | C004、C005、C006、C015、C022 |
| 容器內路徑 | C001、C002、C050、C052、C053 |
| workload 身分（容器名、selector、Service、port） | C049、C056、S1 |
| 生命週期行為（bootstrap、post-deploy init、addon 注入） | C008、C047、C023、C024 |

**建議**：

1. **資料端**：template 新增 `chart_profile` selection 欄位，例如 `generic` 或 `woow_odoo_tenant`。
   - 已有先例：`post_deploy_init_type` 本身就是 selection，`_inject_storage_class.path_map` 則是寫在程式碼裡、依 slug 分的 dict。
   - 不建議在 seed XML 塞一大段 JSON DSL，容易漂移，又無法做型別檢查。
2. **程式端**：用一個 typed registry（dataclass）描述每個 profile，下面是示意：

   ```python
   CHART_PROFILES = {
       'woow_odoo_tenant': ChartProfile(
           storage_class_paths=[('persistence', 'storageClass'), ('postgres', 'persistence', 'storageClass')],
           image_pull_secret_paths=[('global', 'imagePullSecrets'), ('imagePullSecrets',)],
           expose=Expose(service='{release}', port=80),          # 明帶給 operator，免自動偵測（S1）
           admin_credential_keys=('odoo.adminEmail', 'odoo.adminPassword'),
           plan_overlay={'cs_odoo_workers': ('odoo', 'config', 'workers')},
           capabilities={'addon_selection', 'module_state'},
           init_strategy='chart',                                 # bootstrap 由 chart 負責（§3.3）
       ),
   }
   ```

   以下這些消費端改成讀 profile：
   - `_inject_storage_class`、`_inject_image_pull_secrets`
   - `_apply_plan_spec_overlay`、`_build_expose_config`
   - addon builder、`_create_service` 的密碼驗證
   - post-deploy init 的 dispatch
3. **前端**：後端鏡射 capability booleans（例如 `supports_addon_selection`），取代 C026～C029 的 slug 判斷，和 has_admin_gui、has_gateway 的既有模式一致。
4. **operator 端**：
   - 容器名、selector、conf 路徑、python 由 chart 契約固定：container `odoo`、label `app.kubernetes.io/component=odoo`、`/etc/odoo/odoo.conf`。
   - operator 收斂成一個常數與 helper 模組，消掉 `odoo_init.py`、`odoo_module.py` 的重複實作。
   - 採替換策略（§3.2）的話，不需要同時支援兩種佈局。
5. **把靜默失效轉成 fail-loud**（針對 silently-wrong 這類問題最有效）：
   - 新 chart 附 `values.schema.json`，頂層 `additionalProperties: false`，並宣告 `global`。殘留的 Bitnami 鍵（`customPostInitScripts`、`postgresql.*`、`odooEmail`）或拼錯的 storageClass 路徑，會在 `helm install` 當下失敗，而不是被忽略。
   - woow-paas-charts 端加 chart render 測試（`helm template` ＋ 斷言），驗證 Service 名與埠、container 名、所有 PVC 帶 instance label（C066）、ConfigMap 內容。
   - 平台端 profile 的路徑，對一份 vendored 的 `values.schema.json` 快照做契約測試，偵測漂移。
6. **避免過度設計**：替換完成後，odoo 只會剩一個 profile。registry 維持輕量，不要做成通用 DSL。

### 3.2 舊 Bitnami template 與新 template：並存還是直接替換

**現況**：

- prod：0 個實例。
- stg：只有 `paas-ws-stg-4bc02867/svc-42487178`，建立於 2026-08-17，Deployment 為 0/0，是 suspended 而非已刪除；Service、PVC、Secret 都還在。
- 同 namespace 另有兩個 MCP 伴生服務：svc-7dd7970c（odoo-manage-mcp）、svc-fbc97612（odoo-mcp）。

**建議：直接替換**，保留 xmlid `cloud_app_odoo` 和 slug `odoo`。理由：

1. plan、product 以及 `cloud_service.template_id` 的 FK 都不會斷；slug 是商品識別碼，不是 chart 識別碼。
2. 並存的話，slug 閘門、path_map、operator 路徑、測試矩陣全部要維持兩套，而且得先做完 §3.1 的 capability 重構才能並存。
3. 沒有 prod 使用者，rollback 用 git revert seed PR 比維護雙 template 便宜。
4. Bitnami legacy image 已經凍結（C112），繼續留著本身就是風險。

**替換時必須補的守門**：

- Bitnami release 不能 in-place `helm upgrade` 成新 chart：資源名不同，postgres 從 StatefulSet 變 Deployment，PVC 佈局也不同。
- 但平台的 upgrade 路徑（`_assemble_deploy_values` ＋ `reset_values`，會被 redeploy 或 plan 變更觸發）會嘗試這麼做。
- 所以要做兩件事：
  - (a) seed 切換前先把 stg 的 svc-42487178 銷毀重建；要保留資料的話，先 export DB 與 filestore，搬遷對照見 §2.6 補充。
  - (b) 加一個通用守門：既存 release 的 chart 名稱和 template 不一致時，拒絕 in-place upgrade，fail-loud 並要求 fresh install。這個守門對所有 template 都有價值。
- 重建 odoo 實例後，MCP 伴生服務的目標 URL 與 API Key 可能要重新設定。這是推論：mcp-odoo-manage 是用 API Key 經 XML-RPC 連目標 Odoo。
- 開發期間在 dev sandbox 用 branch seed 驗證即可，stg 不需要掛並行的 template。

**什麼情況下考慮並存**：Eugene 如果要求 prod 上線後保留一個 release 週期的回退窗口，可以把 legacy record 設成 `is_active=False` 留一版。代價是 §3.1 的 capability 重構得從 P2 提前到 P0。

### 3.3 官方 image 沒有 bootstrap 之後，誰負責

| 職責 | 現況（Bitnami） | 建議負責方 | 理由 |
|---|---|---|---|
| PostgreSQL user／db 建立 | postgresql 子 chart 的 `auth`（`bn_odoo`／`bitnami_odoo`） | chart：官方 postgres image 的 `POSTGRES_USER`、`POSTGRES_DB`、`POSTGRES_PASSWORD`（取自 Secret） | 原生機制 |
| odoo DB 初始化（`-i base`） | entrypoint `--init=all`（`odooSkipInstall=false`） | chart：odoo Deployment 的 `bootstrap` initContainer，流程見表下 | 冪等檢查成本低；跑在主容器之前，所以 Ready 就代表 DB 已 init，平台 `_is_container_ready` 的語意自動成立，不必新增判準（解 C008、C047） |
| admin login 與密碼 | entrypoint ＋ `fix-admin-credentials.sh`（直接 SQL 寫 pbkdf2，寫死 `id=2`） | 同一個 initContainer，首次 init 後用 odoo shell／ORM 寫 `base.user_admin`（密碼取自 Secret） | ORM 會自動雜湊，不需要 passlib 和寫死 id 的 SQL；維持「只在首次 init 設定」的語意，重設另走顯式動作 |
| odoo.conf 系統設定 | `odoo-conf-system.sh` 用 sed | chart ConfigMap：`proxy_mode`、`list_db=False`、隨機 `admin_passwd`、`dbfilter`、`workers`、`limit_*`、`smtp_*`、`addons_path` | 每次 helm upgrade 重新 render，plan 變更能生效（解 C015） |
| 第三方 addon 選擇 | 平台注入 initContainer ＋ customPostInitScripts | chart 原生 values `addons.selected`：initContainer 從 `/all-addons` 複製到子目錄，平台只填清單 | 平台不再需要知道容器內路徑（解 C001～C003、C018） |
| image 升版時的模組升級 | entrypoint 非首次啟動跑 `--update=all`（S2，已查證） | chart initContainer 比對受管模組版號後，只 `-u <list>` | 避免每次重啟都 `-u all` 拉長啟動時間 |
| module list 刷新＋stale cleanup | operator `/init/odoo` exec | 維持 operator post-deploy（修 C049～C053 的路徑） | 這是平台專屬邏輯，和 catalog 綁定 |

`bootstrap` initContainer 的流程：

1. 等待 DB 可連線。
2. 檢查 `ir_module_module` 是否存在。
3. 不存在才執行 `odoo -c ... -d $DB -i base --stop-after-init --no-http`。

**為什麼不用 Helm hook Job**：

- Job 和 Deployment 會並行，odoo 主容器在空 DB 上 crashloop。
- hook 在 `reset_values` upgrade 時會重跑，要另外處理冪等。
- Job 失敗時，平台端只看得到 helm timeout。

**為什麼不用 operator post-deploy exec**：

- 必須先讓 pod 以空 DB 跑起來。
- 會多一條 exec 路徑耦合。
- controller 狀態機重試 5 次就轉 `error`（C047）。

**風險與待量測**：

- 首次 init 需要數分鐘：startupProbe、operator `helm --timeout`、平台的部署等待都要跟著調。
- 官方 image 以 `USER odoo` 執行：PVC 需要 `securityContext.fsGroup`。
- master password：Odoo 沒設 `admin_passwd` 時預設是 `admin`，就算 `list_db=False` 也應該設一個隨機值做縱深防禦。

---

## 4. 分期改動清單

### P0：新 template 能部署並登入的最小集（13～20 人日）

| 建議 issue 標題 | repo | 涉及檔案／條目 |
|---|---|---|
| 新增自家 odoo-tenant chart：官方 odoo:18.0＋nginx sidecar＋獨立 postgres＋bootstrap initContainer | woow-paas-charts | `charts/odoo-tenant/`（新增）。含：Service 以裸 fullname＋80 為主；container `odoo`；label `app.kubernetes.io/component=odoo`；所有 PVC 帶 instance label（C066）；`global` 與頂層 imagePullSecrets（C006）；`values.schema.json`；ConfigMap odoo.conf；bootstrap initContainer（§3.3）；頂層 `resources`／`persistence` 作用於 odoo |
| cloud-odoo image 改以官方 odoo:18.0 為基底 | paas-odoo-ci | `Dockerfile.cloud:1`（C067）、`Dockerfile.cloud:18`（C068）；保留 `/all-addons/` 與 catalog（C011、C058） |
| cloud_app_odoo template 切換至 odoo-tenant chart（seed、values、憑證鍵、storageClass、expose） | woow_paas_platform | `src/data/cloud_app_templates.xml:221,235,242,243,266,275`（S4、C012、C013、C014a、S3）；`src/models/cloud_service.py:1641`（C004）、`:1785`（C005）、`:1850`（C006 驗證）、`:1658`（S1 明帶 service／port）；`src/controllers/paas.py:3381`（C022）；`src/models/cloud_service.py:3592`、`src/controllers/paas.py:4321`（C008、C047，確認 init 能通過）；測試 C020、C021、C033、C034、C035 |
| paas-operator odoo_init 對齊 odoo-tenant chart（容器名、selector、conf、python） | woow_paas_platform | `extra/paas-operator/src/services/odoo_init.py:80,126,178,271`（C049、C056、C050）；`extra/paas-operator/src/services/odoo_scripts/update_module_list.py:38`（C053）；`extra/paas-operator/tests/test_odoo_init_cleanup.py:543`（C054）。`post_deploy_init_type=odoo` 會呼叫這條路徑，不修服務會轉 error |
| 既存 Bitnami odoo 租戶實例處置與「chart 換版拒絕 in-place upgrade」守門 | woow_paas_platform（＋ operator） | stg svc-42487178 與伴生 MCP；守門邏輯（§3.2）；需要 migration 的話新增 `src/migrations/<X>/` 並 bump manifest |

### P1：功能對等（10～18 人日）

| 建議 issue 標題 | 涉及檔案／條目 |
|---|---|
| Odoo addon 選擇機制改走 odoo-tenant chart 原生 values（取代 initContainer＋customPostInitScripts） | `src/models/cloud_service.py:34,1671-1742,1744`（C001、C002、C003、C011、C018）；`src/controllers/paas.py:3450,3620,3680,3681`（C023、C024、C046、C025）；`src/tests/test_helm_values_addon_builder_1052.py:26`（C019）；chart 的 `addons.*` |
| paas-operator odoo_module 路徑對齊並收斂 pod 定位 helper | `extra/paas-operator/src/services/odoo_module.py:62,69,138,170`（C057、C051、C052）；`update_module_list.py:48`（C058）；`tests/test_odoo_init_cleanup.py:213,287`（C060、C059） |
| plan 規格 overlay（workers／limit_memory）改寫為 chart values | `src/models/cloud_service.py:4748`（C015）；`src/models/sh_subscription_plan_cs_spec.py:25`（C009）；`src/i18n/zh_TW.po:8789`（C032）；`src/tests/test_cs_spec_332.py:146`（C036） |
| Odoo 租戶 SMTP 設定改由 odoo-tenant chart render odoo.conf | `src/data/cloud_app_templates.xml:243`（C014b）；chart ConfigMap／Secret |
| Odoo 租戶管理員憑證供給與重設（是否接入 #357 一次性憑證） | `src/models/cloud_app_template.py:245`（C007）；`src/models/cloud_service.py:2187`；#559／#535 不變式重驗（C035、C020）；reset-config 在新 chart 下的冪等驗證 |
| Odoo 租戶 image 升版時的模組升級機制（取代 Bitnami `--update=all`） | S2；chart bootstrap initContainer |

### P2：清理舊 Bitnami 路徑、tests、docs（4～9 人日）

| 建議 issue 標題 | 涉及檔案／條目 |
|---|---|
| 以 template capability 取代 `slug=='odoo'` 閘門 | `src/models/cloud_app_template.py`（新欄位）、`src/controllers/paas.py:3450,3620`、`src/static/src/paas/pages/configure/AppConfigurationPage.js:197,356`、`src/static/src/paas/pages/service/ServiceDetailPage.js:175`、`ServiceDetailPage.xml:178`（C023、C024、C026～C029） |
| 清理 Bitnami odoo 殘留常數、註解與測試字面量 | C062、C063（docstring）、C065、C030、C031、C044 |
| 文件：Odoo 服務設定與 addon 選擇 ADR 改寫（Bitnami → odoo-tenant chart） | `docs/reference/features/odoo-service-config.md:45,74,92,105,110`（C088～C092）；`docs/decisions/refactor/odoo-service-config-enhancement.md:20`（C110）；`docs/decisions/cloud-odoo-addon-selection.md:28`（C111）；`docs/reference/gotchas/bitnami-legacy-image-move.md:8`（C112）；`paas-odoo-ci:docs/sop-ci.md:22` 的 cloud-odoo 段（C093）；跑 `python3 scripts/audit/gen-docs-index.py` |
| 自家 chart 版號治理守線（取代 #552 的第三方釘版） | `src/tests/test_seed_chart_version_pin_552.py`（C034 延伸） |
| paas-operator odoo exec 路徑 integration test（真的 odoo-tenant release） | `extra/paas-operator/tests/integration/` |

---

## 5. scope=paas-odoo-self 附帶清單（平台自身 paas-odoo，另案）

另案粗估 20～35 人日。不含 prod 停機窗口，含用 stg clone 演練資料遷移。最大難點依序是：

1. prod `woow_prod` 的 DB 與 filestore 要從 Bitnami 子 chart StatefulSet 和 `/bitnami/odoo` 遷出。
2. `ODOO_SKIP_MODULES_UPDATE` 兩段式 promote SOP 要重新設計（C097）。
3. NetworkPolicy、備份、operator webhook 依賴 Service 名與 label，改錯會靜默斷線。

| 位置 | Bitnami 假設 | impact | 需要的改動 | effort | 驗證 |
|---|---|---|---|---|---|
| C041 `.gitea/workflows/odoo-tests.yml:37` | CI 測試 image 的 `ODOO_BIN`、`ADDONS_PATH` 在 `/opt/bitnami` | none（本案） | 另案改成 `/usr/bin/odoo` 等 | S | confirmed |
| C042 `.claude/skills/deploy-stg/scripts/verify-addon-version.sh:382` | ADDONS_PATH 預設 `/opt/bitnami/odoo/custom-addons` | none（可用參數覆寫） | 另案更新預設值 | S | confirmed |
| C061 `extra/paas-operator/src/config.py:83` | 註解範例用 `<release>-odoo` 命名 | cosmetic | 部署注入的 env 值跟著改 | S | confirmed |
| C069 `paas-odoo-ci:Dockerfile:1` | `FROM bitnamilegacy/odoo` | breaks | 改官方 image | M | confirmed |
| C070 `paas-odoo-ci:Dockerfile:5` | COPY 到 `/opt/bitnami/odoo/custom-addons/`，避開 PVC 覆蓋 | breaks | 改 `/mnt/extra-addons`，同步 addons_path | S | confirmed |
| C071 `paas-odoo-ci:Dockerfile:12` | venv pip | breaks | 改系統 pip | S | confirmed |
| C072 `paas-odoo-ci:Dockerfile:22` | `mv /tmp/addon-catalog.json` 依賴 generate-catalog 的環境偵測 | breaks | 和 C073 同步 | S | confirmed |
| C073 `paas-odoo-ci:scripts/generate-catalog.sh:11` | 以 venv python 是否存在判斷是否在容器內 | breaks（self）；tenant 為 none | 改偵測條件 | S | adjusted |
| C074 `paas-odoo-ci:.gitea/workflows/build-image.yml:401` | IMAGE_MANIFEST 路徑 | breaks（FATAL 擋 build） | 改路徑 | S | confirmed |
| C075 `paas-odoo-ci:scripts/check-addon-lists.sh:6`（另有 `:148`） | 註解引用 Bitnami 路徑 | cosmetic | 同步文字 | S | confirmed |
| C077 `paas-app-bundle-cd:paas-odoo/fleet.yaml:5` | chart 座標是 bitnamicharts/odoo 28.2.10 | breaks | 換自家 chart | L | confirmed |
| C078 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:75` | customPostInitScripts 一次性 hook | breaks | 改 ConfigMap／initContainer | L | confirmed |
| C079 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:109` | odooEmail、odooSkipInstall、odooDatabaseFilter、loadDemoData | breaks | 重新設計 values | L | confirmed |
| C080 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:1104` | postgresql.auth 為 bn_odoo／bitnami_odoo | breaks | 獨立 postgres 重新設計 | L | confirmed |
| C081 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:1135` | postgres netpol podSelector 用 `name=odoo`＋`instance=paas-odoo` | silently-wrong | 新 selector | M | confirmed |
| C082 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:389` | patch-odoo-conf 依賴 `is_app_initialized` | breaks | 重寫 | L | confirmed |
| C083 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:566` | filestore-hashdir-guard 處理 Bitnami 佈局 | breaks | 原則上移除，但要重新驗證權限問題 | M | confirmed |
| C084 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:845` | module-version-gate 的 CONF、PY、MANIFEST 與首次部署判斷 | breaks | 重新設計（可作為租戶 S2 的參考） | L | confirmed |
| C097 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:121` | `ODOO_SKIP_MODULES_UPDATE` 的兩段式 promote SOP | breaks | 重新設計升級觸發機制 | L | **found-in-verify** |
| C098 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:428`（另有 `:802`、`:938`） | 三支 initContainer 都掛在 `/bitnami/odoo` | breaks | 改掛載點 | S | **found-in-verify** |
| C099 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:921` | gate 直接呼叫 Bitnami 的 odoo-bin | breaks | 改執行路徑 | S | **found-in-verify** |
| C100 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:973` | 頂層 `networkPolicy:` 的 values 路徑 | breaks | 自家 chart 設計對等入口，並實拉 schema 驗證 | M | **found-in-verify** |
| C105 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:234` | 8072 埠靠 `extraContainerPorts` 宣告 | silently-wrong | chart template 直接宣告 | S | **found-in-critic** |
| C106 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:243` | Service 第二個埠靠 `service.extraPorts` | silently-wrong | 支援多埠 Service | S | **found-in-critic** |
| C107 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:294` | ingress `extraPaths`／`extraRules` 寫死 backend `paas-odoo` | degraded | 依新 fullname 簡化 | M | **found-in-critic** |
| C108 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:943` | startup／liveness probe 的扁平 values 鍵 | degraded | 重建 probe 設定 | M | **found-in-critic** |
| C109 `paas-app-bundle-cd:paas-odoo/values-prod.yaml:996` | netpol 埠寫 8069／8072（容器埠佈局） | breaks | nginx sidecar 架構下重新量測後再填 | M | **found-in-critic** |
| C085 `paas-app-bundle-cd:paas-netpol/operator-ingress-netpol.yaml:138` | 白名單用 `app.kubernetes.io/name=odoo` | silently-wrong（Odoo 呼叫 operator 會 timeout） | 換 selector | S | confirmed |
| C104 `paas-app-bundle-cd:paas-netpol-stg/operator-ingress-netpol.yaml:111` | 同 C085（stg 的獨立檔案） | silently-wrong | 單獨修改 | S | **found-in-verify** |
| C086 `paas-app-bundle-cd:paas-odoo-backup/pg-backup-cronjob.yaml:66` | `paas-odoo-postgresql-client` label | silently-wrong（每日備份靜默不產出） | 重新設計連線白名單 | S | confirmed |
| C087 `paas-app-bundle-cd:paas-odoo-backup/pg-backup-cronjob.yaml:90` | 連 `paas-odoo-postgresql`，帳號 `bn_odoo` | breaks | 改 Service 名、帳號、secret 鍵 | S | confirmed |
| C101 `paas-app-bundle-cd:paas-operator/values-prod.yaml:55` | `config.agentRuntime.odooBaseUrl` 寫死 `paas-odoo` 的 port 80（路徑依 critic 更正） | silently-wrong（webhook timeout） | 跟新 Service 名與埠同步 | S | **found-in-verify** |

與本案的交界：

- 平台 pod 的 `CATALOG_PATH`（C010）來自 `Dockerfile`（C072、C073），租戶的 catalog 來自 `Dockerfile.cloud`。本案只要求兩者內容一致，不改 self lineage。
- `paas-odoo-ci:Dockerfile` 同時產出 CI 測試 image，另案換基底時 C041 要同步改。

---

## 6. 待確認問題

1. **odoo-tenant-1.0.0 chart 的來源**：叢集上是哪個 namespace、哪個 repo？能不能 fork 到 woow-paas-charts？（woow-paas-charts 目前沒有任何 odoo chart。）它的 values schema、Service 與容器命名、label、有沒有現成的 bootstrap？
2. **stg 既存實例**：svc-42487178（suspended 0/0）和伴生的 svc-7dd7970c、svc-fbc97612 可以銷毀重建嗎？資料有沒有保留價值？
3. **替換或並存**（§3.2）：需要 prod 回退窗口嗎？
4. **bootstrap 責任歸屬**（§3.3）：chart initContainer，還是 operator post-deploy？
5. **admin 帳號語意**：login 是否維持用租戶填的 email（Bitnami 的行為）？要不要接入 has_admin_gui／#357 的一次性憑證與「重設管理員密碼」？
6. **SMTP 落點**：用 odoo.conf 的 `smtp_*`（全域 fallback），還是 bootstrap 建 `ir.mail_server`？
   - 已查證（2026-09-17）：Odoo 18.0 把 odoo.conf 的 `smtp_ssl` 當成 **STARTTLS**（`odoo/addons/base/models/ir_mail_server.py:441-442`：`smtp_encryption = 'starttls' # smtp_ssl => STARTTLS as of v7`），conf 層沒有 implicit TLS（465）的設定。
   - ⇒ 租戶選 `smtpProtocol=ssl` 時只能建 `ir.mail_server` 記錄（`smtp_encryption='ssl'`），C014b 的建議 (a) 只涵蓋 STARTTLS 與無加密。
7. ~~**現行租戶的升版語意**~~：已查證，Bitnami entrypoint 非首次啟動確實跑 `--update=all`（見 S2 列）。
8. ~~**官方 image 釘版**~~：已裁決（2026-09-18），走 JCR 並釘 digest，見 §0。仍要確認的只剩 digest 的更新節奏與由誰負責。
9. **首次 init 耗時**：要和 operator `helm --timeout`、平台部署等待、startupProbe 門檻對照量測。
10. **websocket**：`workers>0` 時的 8072 分流，Bitnami 租戶路徑現在是否根本沒有 8072（推論）？新 chart 的 nginx sidecar 設定要驗證。
11. **catalog 一致性**：平台 image（`Dockerfile`）和租戶 image（`Dockerfile.cloud`）的 `/all-addons/catalog.json` 是否保證一致？這是既有風險，換 image 時會放大。
12. **demo data**：Odoo 18.0 的 demo 預設旗標（`--without-demo`／`--with-demo`）與 `loadDemoData` 的對應需要核實。
13. **PVC 權限**：官方 image 的 odoo uid 和 fsGroup 設定，在 Longhorn／local-path 上的行為。
14. **重複實作收斂**：controller `src/controllers/paas.py:4200` 與 model `src/models/cloud_service.py:3531` 兩份 post-deploy init，要在 P0 之前先收斂嗎？
15. **imagePullSecrets 鍵**：新 chart 要消費 `global.imagePullSecrets`、頂層 `imagePullSecrets`，還是兩者都吃（C006、C062）？

---

## 7. 附錄

### 7.1 被剔除的 findings（36 條：duplicate 35、refuted 1）

| 保留條目 | 被併入的剔除條目 | 理由摘要 |
|---|---|---|
| C015 `src/models/cloud_service.py:4748` | `:4773` | 同一耦合；剔除條目的行號 off-by-one（實為 `:4772`） |
| C012 `src/data/cloud_app_templates.xml:235` | 同檔 `:235`（helm_chart_name／version） | 不同 sweep 重複列出 |
| C013 `src/data/cloud_app_templates.xml:242` | 同檔 `:200`（cloud_app_odoo record） | 同一 record 的另一種切法。剔除理由說 record 起於 `:199`，本報告以 `grep -n` 複查實為 `:200`，但重複的判定不變 |
| C002 `src/models/cloud_service.py:1710` | `:1723`（volumeMounts） | 同一段程式碼 |
| C003 `src/models/cloud_service.py:1734` | `:1734`（fallback sed）、`:1671`（函式定義） | 內容重複，或是 C002＋C003 的合併敘述 |
| C005 `src/models/cloud_service.py:1785` | `:1787` | 同一個 dict，只是錨點不同 |
| C009 `src/models/sh_subscription_plan_cs_spec.py:25` | 同檔 `:25`（scope 標 shared） | help 寫明 Odoo only，保留 tenant-odoo 的版本 |
| C006 `src/models/cloud_service.py:1850` | `:1847` | 同一函式 |
| C016 `src/migrations/18.0.1.0.2/post-migrate.py:38` | 同檔 `:1` | 同一檔案 |
| C022 `src/controllers/paas.py:3381` | `:3382`、`:3381`（runtime-lifecycle sweep） | 同一段驗證程式碼 |
| C036 `src/tests/test_cs_spec_332.py:146` | 同檔 `:139` | `:139` 實際屬於另一個 method |
| C037 `src/tests/test_credential_mask_spec_driven_535.py:180` | 同檔 `:180` ×2 | 1 條 **refuted**（測試對象是 opendesign 而非 odoo）＋1 條重複 |
| C021 `src/tests/test_helm_values_field_acl_559.py:69` | 同檔 `:69` ×2 | 同一個 fixture |
| C019 `src/tests/test_helm_values_addon_builder_1052.py:26` | 同檔 `:26` | 只有 kind／scope 標籤不同 |
| C026 `AppConfigurationPage.js:197` | 同行 | 重複 |
| C028 `ServiceDetailPage.js:175` | 同行 | 重複 |
| C049 `extra/paas-operator/src/services/odoo_init.py:80` | 同行 ×2 | 其中一條帶有叢集實證（`svc-42487178-odoo`），已併入 C049 的說明 |
| C051 `extra/paas-operator/src/services/odoo_module.py:69` | `:69`、`:43` | 同一斷言 |
| C050 `extra/paas-operator/src/services/odoo_init.py:178` | `:178` ×2、`:271` | 同一個 venv 路徑 |
| C052 `extra/paas-operator/src/services/odoo_module.py:138` | `:138` ×2、`:170` | 同一組 conf 與 python 路徑 |
| C054 `extra/paas-operator/tests/test_odoo_init_cleanup.py:543` | 同行 | 重複 |
| C059／C060 | `extra/paas-operator/tests/test_odoo_init_cleanup.py:8`、`:213` | `:8` 的 silently-wrong／L 嚴重度被推翻（域限制已和佈局解耦） |
| C058（含 C053） | `extra/paas-operator/src/services/odoo_scripts/update_module_list.py:25` | 錨點與 C058 相同，嚴重度同樣被推翻 |
| C067 `paas-odoo-ci:Dockerfile.cloud:1` | 同行 | 重複 |
| C068 `paas-odoo-ci:Dockerfile.cloud:18` | `Dockerfile.cloud:1`（pip） | 內容重複，而且行號錯誤 |

### 7.2 critic 質疑項與判斷

| 質疑 | 判斷 |
|---|---|
| `src/data/cloud_app_templates.xml:221` 的註解說「租戶側無既存服務」，但 stg `paas-ws-stg-4bc02867` 有 svc-42487178（suspended）＋2 個 MCP 伴生服務 | **採納**。和任務背景「stg 僅 svc-42487178 一個實例」一致。已列為 S4，並寫進 §3.2 的守門與 §6 的第 2 點。影響只在 stg，但「chart 換版不可 in-place upgrade」的守門是必要的 |
| `paas-app-bundle-cd:paas-operator/values-prod.yaml:55` 的 symbol 寫成「chartValues.hermesAgent（或同層設定）」，實際是 `config.agentRuntime.odooBaseUrl` | **採納**。論點本身成立，只是路徑指錯，已在 §5 的 C101 更正 |

### 7.3 本報告對驗證清單的修正與補充

- **補查 4 條**：S1（expose 自動偵測 Service 與埠）、S2（隱性的 `--update=all`，2026-09-17 已查證）、S3（`default_port=8069`）、S4（seed 註解過時）。S1～S3 未經 verify pass；S4 來自 critic。
- **C015**：複查 `src/models/cloud_service.py:4772` 後發現，新 seed 沒有 `customPostInitScripts` 時會靜默跳過，不會 raise。只有 C003 的 fallback 仍寫入 script 時才會 raise。實際 impact 應視為 silently-wrong。
- **C010**：保留 shared。但就本案而言，它讀的是平台 pod（`Dockerfile` lineage）的 catalog，本案只需要保證和租戶 image 的 catalog 一致。
- **C013**：補上清單原本沒點名的 Bitnami 專屬鍵 `global.security.allowInsecureImages`。
- **sweep 結論被 verify 覆蓋的地方**：
  - data-migrations sweep 認為 `update_module_list.py` 的 #748 域限制需要重新驗證，已被 C053 的 verify 推翻，只要改常數。
  - frontend sweep 把 `src/controllers/paas.py:3382` 評為 silently-wrong，verify 更正為 `:3381`、breaks。