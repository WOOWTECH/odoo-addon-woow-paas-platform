---
title: harumi-odoo → PaaS odoo cloud_service 移植計畫（偵察與路線比較）
category: scout
status: draft
created: 2026-09-18
updated: 2026-09-19
related:
  - docs/scout/2026-09-18-odoo-bitnami-coupling-inventory.md
  - docs/plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-design.md
  - docs/plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md
  - docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md
---

# harumi-odoo → PaaS odoo cloud_service 移植計畫（審查修訂版）

> 狀態：draft（2026-09-15 調查並依三份審查修訂，2026-09-18 落檔），路線（Q1）已定案（2026-09-19，見 §0）；其餘待決問題仍未經人工覆核。依 `docs/scout/README.md`，後續要 promote 到 `plans/`（成案）或 `decisions/`（定案）。
> 依據：五份偵察報告（先例 [P]、平台 [PL]、image [I]、來源 IaC [S]、叢集 [C]）＋三份審查（db-filestore [R-db]、platform-lifecycle [R-pl]、ops-completeness [R-ops]）＋修訂時補做的兩項唯讀查證 [V]
> 範圍：沒有開任何 issue；沒有對叢集做任何寫入。`harumi-odoo` 不在本 repo 的 kubectl 白名單，對它只做過 get／list／logs；修訂時的查證只對白名單 ns 做 `kubectl get`，外加讀取 GitHub 原始碼。
> 敏感值：SMTP 帳號、Secret 值、MCP 私有 path 的隨機段（本文寫成 `private_<redacted>`）一律不寫入本文件。
> 標記：**[事實]** 表示偵察或查證有證據；**（推論）** 表示沒有直接驗證；**⚠矛盾** 表示來源之間互相衝突，後面附判斷

---

## 0. 後續討論更新（2026-09-17～18）

本節記錄計畫完成後的討論結果；與下方章節衝突時以本節為準。

1. **路線 C 建議升格**：叢集上 17 個 odoo-tenant 站（含 harumi）全部跑同一個官方 image digest（`odoo@sha256:4ea9b466…`，`18.0-20260810`），而平台租戶 odoo `cloud_service` 只有 stg 1 個、prod 0 個。因此建議不做「harumi 專屬隱藏 template」，改成**讓平台的 odoo template 本身換成官方 `odoo:18.0` lineage**，harumi 與其餘站共用同一套 template 與 runbook。**已定案（2026-09-19，即 Q1）**。改動範圍（約 61 處、27～47 人日）見[平台程式碼綁 Bitnami 的耦合盤點](2026-09-18-odoo-bitnami-coupling-inventory.md)。
2. **「只換 Bitnami 外殼內的 core」不採用**：技術上可行，但只解 A1（core 倒退），A2、A5、D3、D8 等卡點都還在。平台 image 停在 `bitnamilegacy/odoo:18.0.20250805` 的問題另外影響平台自身 `paas-odoo`，屬另案。
3. **已裁決（2026-09-18）：新 odoo image 走 JCR**。`paas-odoo-ci` 以 `FROM odoo:18.0@sha256:<digest>` 建置、加入平台 addon 與 harumi 需要的 pip 後推到 `jcr-prod.woowtech.io`；不直接從 Docker Hub 拉（harumi 現在是直接拉，k3s 沒有 registry mirror）。1.3 的做法因此確定。目標 core 必須 ≥ harumi 現用的 `18.0-20260810`。
4. **harumi image 來源（已查證）**：odoo、postgres（`pgvector/pgvector`）、nginx、busybox、cloudflared（`:latest` 浮動 tag）、pg-backup 的 image 都來自 Docker Hub；只有 `mcp-odoo` 來自 GHCR（`ghcr.io/woowtech/woow-odoo-mcp-server`，需要 pull secret）。
5. **SMTP 補充（已查證）**：Odoo 18.0 把 odoo.conf 的 `smtp_ssl` 當成 **STARTTLS**（`odoo/addons/base/models/ir_mail_server.py:441-442`）。harumi 的 conf 是 `smtp_port=465`＋`smtp_ssl=True`，推論這條 conf 層設定連不上（465 是 implicit TLS），實際寄信可能走 DB 的 `ir_mail_server`。D12 與 Q18 要以指令區塊 B 的 `ir_mail_server` 查詢結果為準；新站若要用 465，只能建 `ir.mail_server` 記錄（`smtp_encryption='ssl'`）。
6. **已開單（2026-09-19）**：chart → woow-paas/woow-paas-charts#81（藍本請求 woow-paas/woow-machines-operator#3）；cloud-odoo image → woow-paas/paas-odoo-ci#133；平台側 P0 → #1128（既存實例處置與 chart 換版守門）、#1129（operator odoo exec 路徑）、#1130（`cloud_app_odoo` template 切換）。P1、P2 尚未開單。

---

## 1. 摘要

harumi-odoo 是一個**全天都有真實流量的 POS／掃碼點餐站**。
- 證據 [C]：nginx log 顯示 `harumi-odoo.woowtech.io` 上有 `/pos/ui`、`/pos-self-order/`、`/websocket`，另有 ECPay 金流 `payment_ecpay`。
- 技術規格：官方 `odoo:18.0` image（core 日期 2026-08-10）、`pgvector/pgvector:pg17`、121 個模組。至少一個客製 addon（`pos_self_order_enhancement`）沒有 git 出處。
- 入口是 pod 內的 nginx sidecar（8080）。它同時負責 asset 快取、`/websocket` 升級、MCP 私有 path proxy，以及 100m 上傳上限 [R-ops]。
- 寄信走 odoo.conf 層的 Gmail SMTP [R-ops]。
- 資料量不大：pg_dump 7.9MB，filestore PVC 實際約 530MiB。

平台現行的 `cloud_app_odoo` template 走 Bitnami chart 加上凍結的 `bitnamilegacy/odoo:18.0.20250805` core。用它承接 harumi 有下列結構性衝突：
- core 要**倒退約 370 天**。
- 首次開機會跑 `--init=all`，還會覆寫 admin 密碼。即使設了 `odooSkipInstall=true`，平台自己的 `fix-admin-credentials.sh` 仍會覆寫 id=2 的密碼 [R-db]。
- 之後**每次重啟都會自動 `--update=all`**：template 沒有設 `ODOO_SKIP_MODULES_UPDATE` [V]。
- workers／limit_time 不能 per-instance 設定。
- 客製 addon 只能透過共用 image catalog 進來。
- 沒有 nginx 入口層，沒有自訂 hostname 路由，也沒有備份。

不管走哪條路，資料搬運都可以照 #1032 的 recipe（叢集內 pod-to-pod push）完成，而且規模比 #1032 小一個數量級。

**建議路線一句話**：走路線 C。新增一個隱藏的專屬 odoo template：
- image：官方 `odoo:18.0` lineage，pip 與 addon 在 build 時烘進去。
- chart：從 `odoo-tenant` chart 收編進 woow-paas-charts，含 nginx sidecar。
- 建立方式：由平台工程以 internal provisioning 建 instance，一次性搬資料，保留 `harumi-odoo.woowtech.io` 網域。

**主要 blocker**：
1. 客製 addon（`/mnt/extra-addons`）沒有任何 git 副本，唯一一份在一顆 Longhorn PVC 上。搬遷前必須先保全。
2. 還沒盤點的項目：addon／pip 完整清單、是否用到 pgvector、nginx 與 SMTP 設定。來源 ns 不在白名單，要由 woow-machines-operator 代跑。
3. ~~路線與目標形態還沒拍板~~：已定案（2026-09-19），平台 odoo template 改用官方 `odoo:18.0` lineage，見 §0。
4. 必須保留 hostname。桌面 QR code 與 ECPay callback 都綁在 hostname 上（推論），但平台沒有自訂 hostname 機制。
5. 租戶 cloud_service 沒有任何備份機制：程式碼與叢集都查無 [PL][R-pl]，也沒有專門追蹤的 issue（#512 是 odoo-tenant 站的同型旁證，範圍不同）。搬過來後會比現況（每日 dump）更差。
6. 今天 08:18／13:49 有人對 harumi 做了 helm install／upgrade，完全不在 git 裡。搬遷前必須和執行者同步，並凍結變更。
7. `mcp-odoo` 走叢集內部連 `harumi-odoo-svc`，來源 odoo 一停就斷 [R-ops]。它的去向（Q10）必須在 Phase 2 結束前拍板。

---

## 2. 現況對照表

| 項目 | 來源 harumi-odoo（ns `harumi-odoo`） | PaaS `cloud_app_odoo` instance（現行 template，`src/data/cloud_app_templates.xml:200`、values 在 `:242`） | 差異判定 |
|---|---|---|---|
| Odoo image／core | `odoo:18.0`@`sha256:4ea9b466…`，`18.0-20260810`，ubuntu noble [I] | `jcr-prod…/cloud-odoo:20260814.0737`，FROM `bitnamilegacy/odoo:18.0.20250805-debian-12-r9`；Bitnami 已凍結，拿不到更新的 core [I] | **core 倒退約 370 天**（路線 A 的 blocker） |
| 開機時的自動動作 | 官方 entrypoint 只等 DB 就緒後 exec odoo，沒有 `--init` 或改 `res_users` 的邏輯 [R-db] | 首次：沒 skip 就 `--init=all`＋改 admin；skip 時會 `DELETE FROM ir_attachment WHERE url LIKE '/web/content/%'`，再 `--update=all`。之後每次重啟：`--update=all`（`libodoo.sh:156-209`）[V]。stg `svc-42487178-odoo` 的 env 只有 `ODOO_SKIP_BOOTSTRAP=no`，沒有 `ODOO_SKIP_MODULES_UPDATE`，image 預設是 `no` [V] | 路線 A 每次重啟都跑 `-u all` |
| python 依賴 | 每次啟動才 pip install：openai、google-genai、markdown、packaging、pycryptodome、pycryptodomex [背景]；`typing_extensions` 解除安裝會警告 [C] | build 時烘入：authlib、defusedxml、docstring_parser、markdownify、mistune、openai、packaging、pydantic、pyyaml [I] | 缺 google-genai、markdown、pycryptodome、pycryptodomex（`markdownify` 不能取代 `markdown`） |
| PostgreSQL | `pgvector/pgvector:pg17`（server 17.10）[S]，Deployment | `bitnamilegacy/postgresql:17.6.0-debian-12-r4`，StatefulSet，大機率不含 vector（推論）[I][PL] | 同為 major 17；vector 是否實際使用還沒查；vector extension 非 trusted，建立要 superuser [P][R-db] |
| DB 名／role | db `odoo`，user `harumi`（configmap 實測 [R-ops]、log [C]） | db `bitnami_odoo`，user `bn_odoo`（非 superuser）[C][P] | 需要 `--no-owner --no-privileges`；filestore 子目錄名要跟 DB 名一致 |
| filestore 路徑／uid | `data_dir=/var/lib/odoo` → `/var/lib/odoo/filestore/odoo`（公式見 Odoo core `odoo/tools/config.py:813` [R-db]）；官方 image 以 `USER odoo` 執行，uid 未查 | `/bitnami/odoo/data/filestore/<db>`。容器以 root 啟動（`runAsUser: 0`，stg 實測 [V]），但 `run.sh` 以 `exec_as_user odoo` 降權跑 odoo 進程 [V]。#1032 文件記錄 `chown 999:0` [P]，uid 值要實測 | 路徑與擁有者都要轉換（路線 A） |
| addons 載入 | addons_path：`/usr/lib/python3/dist-packages/odoo/addons`、`/var/lib/odoo/addons/18.0`、`/mnt/extra-addons`（PVC 5Gi，實際約 228MiB，無 git）[C][S] | initContainer `addon-selector` 從 image 的 `/all-addons` 複製租戶勾選的候選（目前只有 3 顆），只在 `template.slug == 'odoo'` 時觸發（`src/models/cloud_service.py:1639`）[PL][C] | 客製 addon 沒有進入路徑 |
| 已知使用中 addon | 121 模組；log 看到 `payment_ecpay`、`pos_self_order_enhancement`、`point_of_sale` [I][C] | 候選只有 `ai_base_gt`、`mcp_server`、`odoo_ai_assistant_chatgpt_connector`；`payment_ecpay` 只在 `paas-addons.list` [I] | 需要盤點完整清單 |
| workers／cron | `workers=4`、`max_cron_threads=2`（configmap 實測 [R-ops]） | `odoo-conf-system.sh` 強制 `workers = 0`；plan overlay 依 `cs_odoo_workers` 改成 2/3/4；腳本只在首次開機跑一次 [PL] | workers>0 時 websocket 由獨立的 gevent 進程服務 8072，chart 只曝露 8069 ⇒ **結構性必然斷線** [R-db]（原稿標為推論，現已由原始碼證實） |
| limit_time | cpu 1200／real 2400 [R-ops] | 固定 60／120，沒有 overlay [PL] | POS 關帳、大批同步有被砍的風險 |
| limit_memory | hard 2.5GiB／soft 2GiB [R-ops] | 腳本寫 0，plan overlay 改成計算值 [PL] | — |
| list_db／proxy_mode | `list_db=True`、`proxy_mode=True` [R-ops] | `list_db=False`（僅在沒設定時補上）、`proxy_mode=True` | 建議改 False（Q11） |
| SMTP／寄件 | odoo.conf 層：`smtp_server=smtp.gmail.com`、`smtp_port=465`、`smtp_ssl=True`；`smtp_user`、`email_from` 是一個**個人 Gmail 帳號**（值不寫入本文件）；密碼在 Secret `harumi-postgres-secret` 的 `SMTP_PASSWORD` key [R-ops] | Bitnami 以 `smtpHost`／`smtpPort`／`smtpUser`／`smtpPassword`／`smtpProtocol` helm values 寫進 conf（租戶白名單內）[PL][V] | 原稿漏列。conf 層 SMTP 不受 `ir_mail_server` 停用影響，演練時要另外處理（見 D12） |
| 入口層 | Deployment 雙容器 `odoo`＋`nginx`；Service 開 `8069/8072/8080`；nginx 聽 8080：`/web/assets`、`/web/static` 快取，`/websocket` → 8072，`/private_<redacted>/` → `mcp-odoo-proxy:8001`，`client_max_body_size 100m` [R-ops] | Service／Ingress 只曝露 8069，沒有 sidecar [PL][R-db] | 見 D8（原稿把它拆成 D3／D5／D8 三件事，實際上是同一個 sidecar） |
| 資源／排程 | nodeSelector control-plane；Recreate [背景]；resources 未盤點（Phase 0 補） | 2CPU/2Gi（stg svc-42487178 實測 spec）、無 nodeSelector、preferred anti-affinity [C] | 節點餘裕充足，不是瓶頸 [C] |
| PVC／SC | 5 顆 `longhorn`（**Retain**，實測 [C][R-ops]）；data 10Gi（實際約 530MiB）、db 10Gi（實際約 741MiB）、addons 5Gi、backup 5Gi、nginx-cache 1Gi [C] | `longhorn-delete`（**Delete**）＋ `resource-policy: keep` ＋ prod 90 天 grace（`paas-app-bundle-cd` `paas-operator/values-prod.yaml:38` [R-pl]）。DB PVC 走 STS 的 volumeClaimTemplates，固定 8Gi（chart 子 chart `charts/postgresql/values.yaml:817/1271` [PL]；stg `data-svc-42487178-postgresql-0` 實測 8Gi [C]） | 資料保護等級下降；DB 8Gi 對 harumi 實際量夠用 |
| 網域／tunnel | `harumi-odoo.woowtech.io`，自有 `harumi-cloudflared`（token、remote-managed）；CF 帳號有 `harumi`（59414daf…）、`harumi-cloud`（cef002b4…）兩條，未確認對應關係 [C][S] | `paas-{env}cs-{ws_hash}-{hash}.woowtech.io`，同一 zone `woowtech.io`，共用平台 tunnel；`custom_domain` 只是 bookmark（`src/models/cloud_service.py:627`，`_build_expose_config` 只讀 `subdomain`，在 `:1658`）[PL][R-pl] | 同 zone 讓手動路由可行，但平台沒有對應機制 |
| 備份 | CronJob `pg-backup` 03:30、`pg_dump -Fc` 到同叢集的 backup PVC，今日 7.9M、35 秒 [C]；叢集沒有 Longhorn backup target [C] | **無**：程式碼與 cron 定義都查無 [PL][R-pl]，也沒有專門追蹤的 issue | 退化 |
| MCP | 兩個 Deployment：`mcp-odoo`（`ghcr.io/woowtech/woow-odoo-mcp-server:latest`，XML-RPC 連同 ns ClusterIP `harumi-odoo-svc`）與 `mcp-odoo-proxy`（nginx）；configmap `mcp-admin-config`、`mcp-odoo-policy`、`mcp-odoo-proxy-config`；secret `mcp-odoo-secrets`、`mcp-admin-ghcr` [R-ops][I] | `cloud_app_mcp_odoo_manage` 可手填連線指向任意 Odoo（`src/data/cloud_app_templates.xml:439`）[PL] | 平台沒有 path-based routing；來源 odoo 停機的瞬間 MCP 就斷 |
| NetworkPolicy | `default-deny-external`：只擋外部 ingress，ns 內全開，egress 不限 [C] | 平台 netpol 機制（`paas-app-bundle-paas-netpol`）[C] | 不照搬；它會擋住跨 ns 連 `mcp-odoo-proxy`（見 D13） |
| 管理方式 | helm `odoo-tenant-1.0.0`（chart 原始碼不在任何已查 repo）＋ `mcp-odoo`（`odoo-mcp-1.0.0`）[S][C] | cloud_service 以 `reset_values=True` 重新組裝 values（`_assemble_deploy_values`，`src/models/cloud_service.py:1540`）[PL][R-pl] | — |

### 2.1 偵察矛盾與判斷

1. **hostname**：[S] 在 Gitea 查不到 hostname；[C] 在 nginx access log 看到 `host: harumi-odoo.woowtech.io`。**判斷：採 [C]**，活體 log 是直接證據。它在平台同一個 zone `woowtech.io` 下，所以手動加路由在技術上可行。
2. **DB PVC 容量**：[PL] 說「harumi DB PVC 10Gi 已超過平台 8Gi 上限」；[C] 實測 actualSize 約 741MiB、dump 7.9MB。**判斷：對 harumi 不是容量 blocker**，而是「沒有擴容路徑」的長期風險。741MiB 與 7.9MB 差將近百倍，推論是 WAL、膨脹或 Longhorn 沒回收的區塊；Phase 0 用 `pg_database_size` 確認。
3. **是否 helm 化**：[S] 引用 README（2026-07-26）說 18 家 tenant「無 Helm release」，chart 化「刻意延後」；[C] 今天 `helm list -A` 在約 17 個 ns 看到 `odoo-tenant-1.0.0`，harumi 的 rev1 是今天 08:18。**判斷**：這批站在 2026-08-25（wmo repo 最後一次 commit）之後、在 git 以外被 chart 化，harumi 是**今天**才 install/adopt，代表有人正在動這批站。**必須先確認執行者並凍結 harumi 變更**（Q3）。
4. **同類站台數**：[S] 以 #512（2026-08-01）的快照算出 22 家；[C] 的 helm list 出現 `evergreen-odoo`，不在那份清單。**判斷：22 已過期**，要以最新 `helm list -A` 為準。
5. **`resources/manifests/odoo-tenant-db-backup/harumi-odoo.yaml`**（wmo repo）：檔頭寫「suspend 維持 true」，實際 spec 是 `suspend: false`。**判斷：以 spec 為準**；今日 03:30 Job 成功 [C] 也佐證。「保留 7 份」沒有直接驗證。
6. **tunnel 架構**：[PL] 說平台「只有單一共用 tunnel」；`extra/paas-operator/src/services/cloudflare.py:1178`、`:1232` 另有 dedicated tunnel 的設定路徑 [R-pl 確認]。**判斷**：租戶服務預設走 shared tunnel 的說法成立，但「沒有 per-service tunnel」不精確；dedicated 路徑供 Security Access／HA 使用 [R-pl]。
7. **addons_path**：configmap 只列 2 條；啟動 log 顯示 3 條，多了 `/var/lib/odoo/addons/18.0`。**判斷**：這是 Odoo 自動加入的 data_dir addons，不算矛盾，但**那個目錄可能有從 Apps 下載的模組**，Phase 0 要一起列。
8. **`ODOO_SKIP_MODULES_UPDATE` 預設值**（修訂時新增）：[P] 寫「Bitnami chart 的 `ODOO_SKIP_MODULES_UPDATE` 預設 `yes`，換 image 開機不會自動 `-u`」；[R-db] 說非首次開機預設會 `--update=all`。**查證 [V]**：image `odoo-env.sh:97` 預設是 `no`；paas-system `paas-odoo` 的 env 明確設了 `yes`；租戶 stg `svc-42487178-odoo` 沒有設。**判斷**：[P] 的描述只適用於平台自身的 paas-odoo（Fleet values 明設），**不適用於 `cloud_app_odoo` 租戶 template**。後者每次非首次開機都跑 `--update=all`。

---

## 3. 缺口分析

標籤：🟥 blocker｜🟧 需平台改動｜🟩 可手動繞過｜括號內是影響的路線

### A. DB migration

| # | 缺口 | 標籤 | 說明／處置 |
|---|---|---|---|
| A1 | core 倒退約 370 天 | 🟥（A、B） | DB 由 2026-08 的 core 產生，要還原到 2025-08 的 core；Odoo 沒有降級 migration。#1032 的 aiot 也是 stock `odoo:18.0` 還原進 Bitnami，而且 `-u all` 成功（`docs/plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-design.md:48,99`），但文件沒記錄 aiot 的 core 日期，**不能當作倒退 370 天已驗證**。路線 C 沒有這個問題 |
| A2 | pgvector 使用狀況不明 | 🟥（待 Phase 0） | 若沒用到：比照 #1032，`pg_restore -l` 刪掉 extension 那行再 `-L` 餵回。若有用到：A、B 要換 postgres image（平台沒有先例），而且 `vector.control` 沒有 `trusted = true`，`CREATE EXTENSION vector` 需要 superuser [P][R-db]，`bn_odoo` 不是 superuser，所以光換 image 不夠，還要安排 superuser 建 extension。C 直接用 `pgvector/pgvector:pg17`，由 image 的 superuser 預先建 extension（見 2.3）。Phase 0 一併查角色屬性（指令區塊 B） |
| A3 | role 不同名 | 🟩 | `pg_restore --no-owner --no-privileges` [P] |
| A4 | DB 名 `odoo` 對 `bitnami_odoo` | 🟩（A）／不適用（C） | A：還原成 `bitnami_odoo`，filestore 子目錄改名；或改 `ODOO_DATABASE_NAME`（不在白名單，要 sudo）。C：chart 直接沿用 `odoo` |
| A5 | Bitnami 開機時的破壞性動作 | 🟥（A）／不適用（C） | 路線 A 有**三套互相獨立**的機制，逐一處理：<br>① `libodoo.sh odoo_initialize()`：全新 PVC 且 `odooSkipInstall=false` 時跑 `--init=all`，再 `UPDATE res_users … WHERE login='admin'`。要 sudo 設 `odooSkipInstall=true`。設了之後會改走另一支：`DELETE FROM ir_attachment WHERE url LIKE '/web/content/%'`，再 `--update=all`（除非 `ODOO_SKIP_MODULES_UPDATE=yes`）[V]<br>② 平台 `customPostInitScripts` 的 `fix-admin-credentials.sh`：`UPDATE res_users SET password=%s WHERE id=2`。它**只受 `/bitnami/odoo/.user_scripts_initialized` marker 控制，與 `odooSkipInstall` 無關**，全新 PVC 首次開機必定執行 [R-db]<br>③ 非首次開機：template 沒設 `ODOO_SKIP_MODULES_UPDATE`，**每次重啟都 `--update=all`** [V]<br>處置（A）：sudo 設 `odooSkipInstall=true`；以 `extraEnvVars` 設 `ODOO_SKIP_MODULES_UPDATE=yes`（chart 鍵名推論，Phase 1 驗）。marker 二選一：(a) 還原後、pod 首次啟動前，用一次性 Job 掛 PVC 在 `/bitnami/odoo/` 預先 touch `.user_scripts_initialized`。代價是 `odoo-conf-system.sh` 也不會跑，workers／limit／list_db 要另外寫進 conf。(b) 把 helm values 的 `odooPassword` 設成業主同意的新 admin 密碼，接受 id=2 密碼被改。無論選哪個，都要先查 id=2 是誰（指令區塊 B）。<br>C：官方 image entrypoint 已確認沒有 init 或改 `res_users` 的邏輯 [R-db] |
| A6 | 大檔傳輸 | 🟩（但執行者受限） | filestore 超過 20MB，禁用 `kubectl exec >`／`kubectl cp`（`docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md`），改走叢集內 push。**來源端 producer 的 exec 在 `harumi-odoo`，歸 woow-machines-operator** |
| A7 | 營業中的站，最終 dump 的一致性 | 🟩 | 凍結窗口先關閉所有 POS session，確認沒有 draft order，再把 odoo scale 0 後 dump（推論：POS 離線暫存的訂單若沒同步會遺失） |
| A8 | post-restore 設定 | 🟩 | `web.base.url` 維持 `https://harumi-odoo.woowtech.io`，smoke 前設 `web.base.url.freeze`；用 ORM 驗證，不用 SQL [P]；演練環境要 neutralize cron、mail（含 conf 層 SMTP，見 D12）、payment [P]；正式環境不 neutralize。**`database.uuid`／`database.secret`**：pg_dump/pg_restore 不會重產（只有 Odoo `exp_duplicate_database` 會 `init(force=True)`，`odoo/service/db.py:187-217`）[R-db]。建議：本案是「搬家」而非「複製」，**維持原值不重產**。重產 `database.secret` 可能讓既有簽章連結（mail 取消訂閱等）失效（推論）；舊站在 rollback 窗口也維持 scale 0，不會長期與新站並行。由 Q17 拍板 |
| A9 | 孤兒 attachment 或 asset bundle 500 | 🟩 | 驗收清單加入 `ir.attachment` 中 `/web/assets/%` 的實體檔檢查 [P] |
| A10 | pg_dump／pg_restore 工具版本 | 🟩 | 2.2 的 dump 在來源 `odoo` 容器內跑，用的是官方 odoo image 內附的 client，**不是** pgvector image 的 17.10。兩端都記錄 `pg_dump --version`／`pg_restore --version`。規則：restore 用的 client major 版號要大於或等於 dump 端；若出現 `unsupported version … in file header`，改用版號較新的 client 執行 restore。先例是 PGDG 18.4 dump → 17.6 restore，實測成功 [P]（見審查爭議 #2） |

### B. filestore migration

| # | 缺口 | 標籤 | 說明／處置 |
|---|---|---|---|
| B1 | 路徑與擁有者轉換 | 🟩（A）／大部分不適用（C） | A：`/var/lib/odoo/filestore/odoo` → `/bitnami/odoo/data/filestore/bitnami_odoo`。擁有者要改成 Bitnami 的 `odoo` daemon user：容器以 root 啟動，但 `run.sh` 以 `exec_as_user odoo` 降權 [V]，所以擁有者錯了會讀不到附件，先例有實例 [P]。uid 以 `id odoo` 實測後 `chown -R <uid>:0`。#1032 記錄的 999 是參考值，不直接沿用。C：chart 沿用 `/var/lib/odoo`，uid 在 Phase 0 以 `id` 確認 |
| B2 | 量與驗證 | 🟩 | 約 530MiB 以下（PVC actualSize 是上限）；參考 #1032 的 1.66GB 約 58 秒，預估 30 秒內。驗證：`gzip -t`、兩端 `find -type f \| wc -l`、`find -type f -printf %s` 加總；不用 `du -sb` [P] |
| B3 | 凍結窗口內的增量 | 🟩 | 量小，最終窗口整包重送即可，不需要 rsync 增量 |
| B4 | nginx-cache PVC | 不搬 | 快取可重建（推論）；新 chart 的 nginx 快取用 emptyDir |

### C. addons／python 依賴

| # | 缺口 | 標籤 | 說明／處置 |
|---|---|---|---|
| C1 | `/mnt/extra-addons` 沒有 git 副本 | 🟥 | PVC 損毀就等於原始碼永久遺失 [S]。**Phase 0 第一件事**：叢集內 push 出來，入 Gitea repo（Q4）。這件事和移植與否無關，本身就該做 |
| C2 | 完整模組清單未知 | 🟥（待 Phase 0） | 121 模組只從 log 看到 3 顆非核心的；AI 相關（需要 google-genai）的 addon 來源 `Woow_odoo_ai_assistant_package` 在 GitHub 回 404 [I] |
| C3 | `payment_ecpay` 不在 cloud catalog | 🟧（A） | 原始碼已在 `paas-odoo-ci/addons/`，但只列在 `paas-addons.list` [I]。A 要改共用的 `cloud-addons.list`，所有租戶都看得到；C 在專屬 image 內 vendor |
| C4 | 客製 addon 進共用 catalog | 🟧（A） | `pos_self_order_enhancement` 是 harumi 專屬；放進共用 `cloud-odoo` 候選清單等於暴露給所有租戶（推論：addon 層級沒有可見性控制，沒查） |
| C5 | pip 缺 4 個套件 | 🟧 | A：加進某顆候選 addon 的 `external_dependencies`，重建共用 image。C：寫在專屬 Dockerfile。兩者都要走 image build 的 `/approve` 人工 gate |
| C6 | 來源每次啟動才 pip install | 風險 | 版本不可重現；新 image 一律在 build 時鎖定版本（`pip freeze` 由 Phase 0 取得） |
| C7 | ESC/POS relay 回 404 | 業務確認 | 目前可能已經半失效 [C]（Q6） |
| C8 | `/var/lib/odoo/addons/18.0` 可能有 Apps 模組 | 待 Phase 0 | 見 2.1-7 |

### D. 平台能力缺口

| # | 缺口 | 標籤 | 說明／處置 |
|---|---|---|---|
| D1 | 沒有匯入既有 DB 的機制 | 🟩（一次性手動）／🟧（通用化） | 本案用手動 runbook；通用能力另開 issue（見 §8） |
| D2 | 自訂 addon 只能選 image catalog | 🟧 | 見 C3、C4 |
| D3 | conf 無法 per-instance 覆寫 | 🟧（A）／C 在 chart 內解決 | workers／limit_time 固定，且 marker 只跑一次，換 plan 也不會生效 [PL]。workers>0 時 websocket 由獨立 gevent 進程服務 8072，而 Bitnami chart 只曝露 8069。所以**任何 `cs_odoo_workers>0` 的 plan（含 T1 workers=2）都會讓 `/websocket` 斷線**，這已由 Odoo `odoo/service/server.py` PreforkServer 與 chart 模板證實 [R-db]。POS 高度依賴 bus／websocket。入口層的處置見 D8 |
| D4 | DB PVC 無法擴容；postgres image 不能換 | 🟧（A） | 固定 8Gi（出處見 §2），STS 的 volumeClaimTemplates immutable；harumi 目前夠用 |
| D5 | 自訂 hostname | 🟧／🟩（手動） | `custom_domain` 只是 bookmark [PL][R-pl]。單純 CNAME 到 `paas-cs-*` 時，Host header 對不上 tunnel ingress rule，推論會回 404。<br>手動在 shared tunnel 加 ingress rule 可行，service 指向新 instance 的 **nginx sidecar 8080**（見 D8）。`create_route` 是先 GET 整份 config、再整份 PUT，沒有 ETag／鎖（`extra/paas-operator/src/services/cloudflare.py:892`、`:864-890`）[R-pl]，推論不會洗掉手動規則，但併發寫入可能互蓋。Odoo 端也沒有對應紀錄，L4 reconciler 可能把它列為 `DNS_RECORD` 孤兒（首版 dry-run、不自動刪，`extra/paas-operator/src/api/reconcile.py`、`src/models/schemas.py:950-957`）。<br>**必須保留原 hostname**：桌面 QR（`/pos-self/<id>?access_token=…&table_identifier=…`）、ECPay 回呼、POS 裝置書籤都綁在 hostname 上（推論） |
| D6 | 沒有備份 | 🟧 | 平台 cloud_service 目前沒有任何備份機制（`src/`、`extra/paas-operator/src` 查無 [R-pl]；paas-ws-* 沒有 CronJob [PL]），也**沒有專門追蹤這個缺口的 issue**。#512（5 家 odoo-tenant 站無備份）與 #440 只是同型旁證，不是本缺口本身。過渡方案：在目標 `paas-ws-*` ns 自建 CronJob（屬白名單 ns，但是平台之外的資源，drift／cleanup 都不會管它；要 Eugene 授權，Q9）；路線 C 可以直接做成 chart 的一部分 |
| D7 | 生命週期風險 | 🟧／流程 | ① 訂閱到期會 `scale_release(0)`（`src/models/cloud_service.py:4056`），**營業中的 POS 會因帳務停擺**（Q8）。<br>② 刪除服務會 helm uninstall，PVC 在 `longhorn-delete` 下 90 天 grace 後連同 volume 一起刪除。<br>③ 任何 redeploy／reconfigure 都用 `reset_values=True` 重新組裝，手動 kubectl patch 會被蓋掉 ⇒ **所有客製只能寫進 `helm_values` 或 chart**。<br>④ drift cron 只讀，不會回滾（`src/models/cloud_service.py:2388-2506`）[R-pl]。<br>⑤〔A〕template 沒設 `ODOO_SKIP_MODULES_UPDATE`，每次重啟、redeploy、node 重新排程、resume 都會跑 `--update=all` [V]：啟動時間拉長（#1032 實測 `-u all` 約 4.5～5 分 [P]），而且每次都是一次模組升級。<br>⑥ `_rescale_halted_after_upgrade`（`src/models/cloud_service.py:2541-2568`）宣稱 chart 無關、會把停權中的服務在升級後壓回 0；新 template 是否也走到，要在 1.4 驗證 [R-pl] |
| D8 | 入口層：nginx sidecar 整包對齊 | 🟧（A）／C 在 chart 內解決 | 來源對外入口是 nginx sidecar 8080 [R-ops]，同時處理四件事：(a) `/web/assets`、`/web/static` 反代快取；(b) `/websocket` → 8072；(c) `/private_<redacted>/` → `mcp-odoo-proxy:8001`；(d) `client_max_body_size 100m`。平台 Bitnami template 四件都沒有。<br>C：chart 內建 nginx sidecar（依 0.2 取得的 `harumi-nginx-config` 重建），tunnel rule 指向 8080；(c) 依 Q10 決定保留或移除。<br>A：要 sudo 以 chart 的 sidecar 相關鍵加容器（鍵名推論，沒查）。<br>上傳上限：新舊都在同一個 zone `woowtech.io` 的 Cloudflare proxy 後面，CF 端上限不變（見審查爭議 #5），只需確保新 nginx 維持 100m |
| D9 | workspace／billing 歸屬 | 流程 | 需要一個 harumi 的 workspace 與 plan／subscription；ns 名是隨機 `paas-ws-{ws_hash}` [PL] |
| D10 | 跨 ns 傳輸的 NetworkPolicy | 🟩 | 在目標 ns（白名單內）加 ingress netpol，放行 `kubernetes.io/metadata.name=harumi-odoo` 到接收器的 port；來源 egress 不受限 [C]。注意：目標 ns 若有 Fleet 管理的 netpol，要確認 selector 不衝突 |
| D11 | 權限分界 | 流程鐵律 | `harumi-odoo` 寫入與 exec、`longhorn-system` snapshot → woow-machines-operator 或人工；`paas-ws-*`、`dev-sandbox-*` → 本 repo agent（prod 寫入每步人工放行）；PR merge、prod image `/approve` → 人工 |
| D12 | odoo.conf 層 SMTP | 🟧（C：chart values）／🟩（A：白名單 smtp* 欄位） | 來源用的是 conf 層 fallback（Gmail 465 SSL、個人帳號），`ir_mail_server` 資料表可能是空的，只查 SQL 會誤判「沒有寄信管道」[R-ops]。<br>處置：(1) 0.2 取得 conf 值、0.3 查 `ir_mail_server`，兩者一起判斷。(2) 1.5 讓 chart 支援 `smtp_server`／`smtp_port`／`smtp_ssl`／`smtp_user`／`email_from`，密碼從 Secret 引用，不寫字面值。(3) **演練與 T-3 預演的 instance 不帶 SMTP 值**：停用 `ir_mail_server` 擋不住 conf 層 fallback，所以要確認 pod 內 conf 沒有 `smtp_*`。(4) 3.7 才補上 SMTP 值並 redeploy。(5) 帳號是個人 Gmail，是否續用或改公司 relay、app password 由誰輪換（先例：只有帳號持有者能重產 [P]），由 Q18 決定 |
| D13 | mcp-odoo 對 `harumi-odoo-svc` 的相依 | 🟧／流程 | `mcp-odoo` 以 XML-RPC 連同 ns 的 ClusterIP `harumi-odoo-svc`，3.4 一 scale 0 就斷 [R-ops]。若選「保留原 release、改指向新站」：新站的 `/private_…/` path proxy 要跨 ns 連到 `harumi-odoo` 的 `mcp-odoo-proxy`，而 `default-deny-external` 會擋外部 ingress [C]，需要 WMO 改 `harumi-odoo` netpol；或改讓 mcp-odoo 走公開 hostname 連新站。若選 `cloud_app_mcp_odoo_manage`：新 hostname，消費端要改 URL／token。Q10 列為 **Phase 2 離開條件** |

---

## 4. 路線選項

- **路線 A：平台原生 `cloud_app_odoo` ＋ 一次性手動匯入**。改共用 `cloud-addons.list`、pip 並重建 `cloud-odoo`。平台工程 sudo 改 `helm_values`：`odooSkipInstall=true`、`ODOO_SKIP_MODULES_UPDATE=yes`、marker 處理、DB 名、nginx sidecar、首次開機順序。還原後跑 `-u all`。
- **路線 B：先在 Bitnami template 上補齊通用的「匯入既有 Odoo」能力**。包括 internal-only 的 skip-bootstrap 與 restore 流程、per-instance conf 覆寫、websocket 8072／入口層、備份、自訂 hostname，再用這些能力搬 harumi。
- **路線 C（推薦）：專屬隱藏 template**。
  - image：以官方 `odoo:18.0` 釘選 digest（建議先等同 harumi 現用的 `4ea9b466…`），pip 與 harumi addon 在 build 時烘入。
  - chart：由 `odoo-tenant-1.0.0` 收編進 woow-paas-charts 並參數化（conf、nginx sidecar 入口含 8072 路由、`pgvector/pgvector:pg17`、SMTP、備份 CronJob）。
  - template：`is_hidden`（`src/models/cloud_app_template.py:343`），由平台工程 internal provision。
- **路線 D（對照組）：暫不移植**。原地保留，只做 C1 保全與備份補強。

| 維度 | A 原生＋手動 | B 先補通用能力 | **C 專屬 template** | D 暫緩 |
|---|---|---|---|---|
| core 版本 | ❌ 倒退約 370 天，且之後永遠卡在 Bitnami 凍結版 | ❌ 同 A | ✅ 同版或更新 | ✅ |
| pgvector | ❌ 需略過，或換 image＋superuser 建 extension（無先例） | ❌ 同 A | ✅ 同源 image | ✅ |
| 資料轉換量 | 高（DB 名、路徑、擁有者、首次開機） | 中（流程化後） | 低（路徑、DB 名、conf 同構） | 無 |
| 首次開機地雷（A5） | 三套機制都要繞，易錯 | 能力補上後解決 | ✅ 不適用（官方 entrypoint 已確認沒有 init 邏輯 [R-db]；Phase 2 實測） | — |
| 每次重啟自動 `-u all` | ❌ 預設會跑，需 sudo 關閉 | 能力補上後解決 | ✅ 不會 | — |
| workers／limit_time／入口層（8072、快取、100m） | 需改共用腳本或手動加 sidecar | 解決 | chart 內解決 | — |
| 影響其他租戶或全域 | ❌ 改共用 image／catalog | 中（改共用 template 行為） | ✅ 隔離 | 無 |
| 平台工作量 | 中（image＋SOP） | **最高**（Odoo＋operator＋image） | 中高（chart＋image＋template＋驗 injector） | 低 |
| 自訂 hostname、備份 | 仍需另做 | 含在範圍 | chart 含備份；hostname 仍要 D5 手動或補能力 | 已有 |
| 平台長期維運 | 單一 lineage | 單一 lineage | ❌ 多一條 Odoo lineage | 盲區持續 |
| 對其餘 odoo-tenant 站的可複製性 | 低（每家都會踩 A1、A5） | 高 | **高**（這批站多半是同一種 odoo-tenant 形態） | — |
| rollback 難度 | 中 | 中 | 低（同構，可逆向 dump） | — |
| 預估到切換 | 最快，但演練失敗風險高 | 最慢 | 中 | — |

**推薦 C，理由**：
1. 營業中的 POS 加上 ECPay 站，不應該承受沒有官方路徑的 core 倒退，也不應該永久停在沒有 security patch 的凍結 core，或每次重啟都自動升級模組。A、B 都繞不過這一點。
2. 來源與目標同構（官方 image、`/var/lib/odoo`、pg17＋vector、nginx 入口），資料轉換最少，演練與 rollback 最單純。
3. 客製 addon、pip、conf、入口層都封裝在專屬 image／chart 內，不動共用 `cloud-odoo` catalog 與 `odoo-conf-system.sh` 的全域預設，符合 CLAUDE.md「不改全域預設，在 addon 層隔離」的精神。
4. operator 層不需要改：`ReleaseCreateRequest`／`ReleaseUpgradeRequest` 的 `chart` 可以是任意 chart，`values` 沒有 schema 限制（`extra/paas-operator/src/models/schemas.py:54-94`）[R-pl]。
5. Odoo 端 injector 不會誤觸：Bitnami 專屬的 addon injector 有 `template.slug == 'odoo'` 閘門（`src/models/cloud_service.py:1639`；`src/controllers/paas.py:3381/3450/3620`）[R-pl]，新 slug 不會觸發。
6. plan overlay 部分共用：`resources.*`、`persistence.size` 是純鍵名比對，新 chart 沿用鍵名就能共用。**workers 相關 overlay 會靜默失效**：它要靠 `customPostInitScripts` 存在才觸發（`src/models/cloud_service.py:4748-4796`），新 chart 不帶這個鍵時不會報錯，只是不生效 [R-pl]。所以 conf 以 chart values 為唯一權威，見 1.4、1.6 的驗收。
7. 可複製：叢集上還有十餘家 `odoo-tenant` 站 [C][S]，C 可以直接複製成通用 runbook。

**條件與替代**（Q1 已定案採官方 lineage，本段僅留作紀錄）：如果 Eugene 裁決「平台只保留單一 Bitnami lineage」，改走 A，但 **Phase 2 必須以「倒退 core 的完整還原＋`-u all`＋A5 三套機制都處理＋POS 冒煙全數通過」作為 go/no-go gate**。B 不建議作為本案前置，但 C 做出的 chart 與 SOP 應回饋成 B 的需求。

---

## 5. 分階段執行計畫（以路線 C 為主線，路線 A 的差異標 〔A〕）

執行者代號：**Agent**＝本 repo agent（白名單 ns：`paas-ws-*`、`paas-ws-stg-*`、`dev-sandbox-*`）｜**WMO**＝woow-machines-operator｜**人工**＝Eugene、elmo、業主

### Phase 0：唯讀盤點與資料保全（D-14 前）

> 派工給 WMO 時，prompt 要附上本 repo 的 kubectl 白名單，並註明「harumi-odoo 只做唯讀 get、logs、exec 查詢，Secret 只列 key 不讀值」。
> exec 查詢的輸出都很小（低於 1MB），可以直接 exec；**原始碼與資料的導出一律走叢集內 push**。
> configmap 內含帳號類值（SMTP user、MCP 連線資訊，先例 #1068 有明文憑證）：輸出只給執行者與 elmo 看，**不貼進 issue、PR 或文件**。

| 步驟 | 執行者 | 指令形狀 | 驗收 | rollback |
|---|---|---|---|---|
| 0.1 確認今天 rev1／rev2 的執行者，凍結 harumi 變更 | 人工（elmo 問 WMO 或相關 session） | `helm --kube-context woow-k3s -n harumi-odoo history odoo-tenant`、`history mcp-odoo` | 有書面回覆，並約定搬遷前不再改 harumi | — |
| 0.2 release、conf、入口快照 | WMO | 見指令區塊 A | 取得 values（遮蔽敏感值）、manifest（排除 Secret）、odoo configmap（含 SMTP 段）、**nginx configmap 全文**、Deployment resources | 唯讀 |
| 0.3 DB 盤點 SQL | WMO | 指令區塊 B | 模組清單、extension、vector 欄位、角色屬性、DB 大小、id=2 身分、open POS session、`ir_mail_server`、`database.uuid` 是否存在 | 唯讀 |
| 0.4 filestore、addons、工具版本盤點 | WMO | 指令區塊 C | 檔數、bytes、uid；addon 清單、manifest 依賴、`.git` 有無；`pip freeze`；`pg_dump --version` | 唯讀 |
| 0.5 hostname、tunnel、MCP 盤點 | WMO＋人工（CF 權限持有者） | 指令區塊 D；CF 端 `GET accounts/<acc>/cfd_tunnel/{59414daf…,cef002b4…}/configurations` | 確認 harumi-cloudflared 用哪條 tunnel、**該 tunnel 上的全部 hostname**（#1032 的教訓：共用 tunnel 誤斷 65 分鐘）、MCP 兩個 Deployment 的設定與消費者 | 唯讀 |
| 0.6 **addon 原始碼保全**（寫入動作） | WMO 跑 producer，Agent 在 `dev-sandbox-*` 起接收器 | 接收端起 python PUT server，netpol 放行 `harumi-odoo`；來源 `tar cf - -C /mnt/extra-addons . \| gzip -1 \| curl -T - http://<recv>:8000/addons.tgz` | 兩端檔數與 bytes、sha256 相符；推入新 Gitea repo（Q4，PR 由人工 merge） | 刪除接收端副本 |
| 0.7 Longhorn snapshot（harumi 5 顆 PVC）＋離叢集 dump 副本 | 人工或 WMO（`longhorn-system` 不在白名單） | Longhorn snapshot CR；最新 `/backups/odoo_*.pg` 以叢集內 push 拉出 | `readyToUse=true`；離叢集 dump 以 `pg_restore -f /dev/null` 全讀通過 | — |
| 0.8 業務資訊 | 人工（業主） | 訪談 | 營業與公休時段、POS 裝置數、QR 是否印在實體物、AI 功能與 ESC/POS 是否仍在用、**寄信帳號歸屬（Q18）**、**MCP 消費者（Q10）**、是否有大檔上傳（對照 100m） | — |

**指令區塊 A：release、conf、入口（WMO）**
```bash
K="kubectl --context woow-k3s -n harumi-odoo"
$K get deploy,sts,cronjob,svc,pvc,netpol,configmap -o wide
$K get configmap harumi-odoo-config -o yaml          # 含 smtp_* / email_from，輸出不外流
$K get configmap harumi-nginx-config -o yaml         # 入口層：listen 8080、/websocket、/web/assets、/private_…/、client_max_body_size
$K get deploy harumi-odoo -o json | jq '.spec.template.spec.containers[] | {name, image, resources, ports}'
helm --kube-context woow-k3s -n harumi-odoo get manifest odoo-tenant | yq 'select(.kind != "Secret")'
helm --kube-context woow-k3s -n harumi-odoo get values odoo-tenant   # 輸出前遮蔽 password/token 類值
$K get secret -o json | jq '.items[] | {name: .metadata.name, keys: (.data|keys)}'   # 只列 key
```

**指令區塊 B：DB 唯讀 SQL（WMO；`<pg-deploy>` 待 0.2 確認；連線方式參考 user memory `reference_prod_readonly_measure_recipes` 的唯讀 recipe）**
```bash
$K exec deploy/<pg-deploy> -- psql -U harumi -d odoo -At -f - <<'SQL'
SELECT version();
SELECT pg_size_pretty(pg_database_size('odoo'));
SELECT extname, extversion FROM pg_extension ORDER BY 1;
SELECT table_name, column_name FROM information_schema.columns WHERE udt_name = 'vector';
SELECT rolname, rolsuper, rolcreatedb FROM pg_roles WHERE rolname NOT LIKE 'pg\_%';
SELECT state, count(*) FROM ir_module_module GROUP BY state;
SELECT name, state, latest_version, author FROM ir_module_module WHERE state <> 'uninstalled' ORDER BY name;
SELECT id, login, active, share FROM res_users WHERE id IN (1, 2);
SELECT key, value FROM ir_config_parameter WHERE key IN ('web.base.url', 'web.base.url.freeze', 'report.url');
SELECT key FROM ir_config_parameter WHERE key IN ('database.uuid', 'database.secret', 'database.create_date');  -- 只確認存在，不輸出值
SELECT id, name, smtp_host, active FROM ir_mail_server;          -- 可能為空：conf 層 SMTP 另見指令區塊 A
SELECT id, code, state FROM payment_provider WHERE state <> 'disabled';
SELECT count(*), pg_size_pretty(sum(file_size)) FROM ir_attachment WHERE store_fname IS NOT NULL;
SELECT count(*) FROM ir_attachment WHERE url LIKE '/web/content/%';  -- 〔A〕Bitnami skip-bootstrap 會刪這批
SELECT id, config_id, state, start_at FROM pos_session WHERE state <> 'closed';
SELECT id, cron_name, active, interval_number, interval_type FROM ir_cron WHERE active ORDER BY id;  -- 供 3.1 白名單判斷
SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) FROM pg_class WHERE relkind = 'r'
  ORDER BY pg_total_relation_size(oid) DESC LIMIT 15;
SQL
```

**指令區塊 C：filestore、addons、pip、工具版本（WMO）**
```bash
$K exec deploy/harumi-odoo -c odoo -- sh -c '
  id; pg_dump --version; psql --version;
  cd /var/lib/odoo/filestore && ls;
  find odoo -type f | wc -l; find odoo -type f -printf "%s\n" | awk "{s+=\$1} END {print s}";
  ls -la /mnt/extra-addons /var/lib/odoo/addons/18.0 2>&1;
  find /mnt/extra-addons -maxdepth 2 \( -name .git -o -name __manifest__.py \);
  for f in /mnt/extra-addons/*/__manifest__.py; do python3 -c "import ast,sys; m=ast.literal_eval(open(sys.argv[1]).read()); print(sys.argv[1], m.get(\"version\"), m.get(\"depends\"), m.get(\"external_dependencies\"))" "$f"; done;
  python3 -m pip list --format=freeze'
```

**指令區塊 D：tunnel 與 MCP（WMO，logs 與 get 唯讀）**
```bash
$K logs deploy/harumi-cloudflared --since=720h | grep -iE "tunnelID|Updated to new configuration|hostname" | tail -50
$K logs deploy/harumi-odoo -c nginx --since=72h | grep private_<redacted> | awk '{print $1, $NF}' | sort | uniq -c
$K logs deploy/mcp-odoo --since=72h | tail -200
$K logs deploy/mcp-odoo-proxy --since=72h | tail -200
$K get configmap mcp-odoo-proxy-config -o yaml
$K get configmap mcp-admin-config mcp-odoo-policy -o json | jq '.items[] | {name: .metadata.name, keys: (.data|keys)}'   # 可能含明文憑證（#1068 同類），先只列 key
```

**Phase 0 離開條件**：
- C1 保全完成。
- A2、C2 有結論。
- hostname 與 tunnel 路由全表確認。
- nginx 與 SMTP 設定已取得。
- ~~Q1（路線）由 Eugene 拍板~~：已定案（2026-09-19）。

### Phase 1：平台與 image 準備（路線 C）

所有 PR 標題前綴「⛔ 勿代 merge｜待 Eugene 覆核 — 」；各 repo 的 PR 由人工 merge。

| 步驟 | 執行者 | 指令形狀 | 驗收 | rollback |
|---|---|---|---|---|
| 1.1 ADR：路線 C、第二條 Odoo lineage、hostname 保留、備份策略 | Agent 起草，人工拍板 | `docs/decisions/` 新檔 | Eugene 核准 | — |
| 1.2 harumi addon repo 化（承接 0.6） | Agent 開 PR，人工 merge | 新 Gitea repo（名稱待定）＋ README 記錄來源與 PVC 快照日期 | 內容與 0.6 快照 sha 一致 | — |
| 1.3 image：官方 lineage | Agent 在 `paas-odoo-ci` 開 PR（新 Dockerfile），人工 merge；prod build `/approve` **人工** | `FROM odoo:18.0@sha256:4ea9b466…`；`pip install` 鎖版本（依 0.4 的 freeze）；COPY harumi addons、`payment_ecpay` | image 在 dev-sandbox 能啟動並載入 121 模組；沒有 runtime pip | 不上 tag、不引用 |
| 1.4 確認平台 odoo 專屬路徑在新 slug 下的行為 | Agent（讀碼＋測試） | 檢查項目：<br>`_apply_plan_spec_overlay`（`src/models/cloud_service.py:4747`）：確認新 chart 沒有 `customPostInitScripts` 時 workers overlay 靜默不生效，而 `resources.*`／`persistence.size` 仍會被 plan 覆寫<br>`_assemble_deploy_values`（`:1540`）<br>`post_deploy_init_type`（`:3541`，`_model_init_odoo` 在 `:3592` 做 module list refresh）<br>`_rescale_halted_after_upgrade`（`:2541`）在 `action_redeploy`、controller `_update_service`、`_reset_admin_password` 三條路徑是否都會呼叫<br>reset-admin-password、SMTP reconfigure | 清單化結論；必要時在新 template 關閉對應欄位；選一個 `cs_vcpu`／`cs_ram_gb` 符合 0.2 實測 resources 的 plan（避免 overlay 把資源壓小） | — |
| 1.5 chart：收編 odoo-tenant | Agent 在 woow-paas-charts 開 PR，人工 merge | 以 0.2 的 manifest 重建並參數化：<br>conf：workers 4、limit_time 1200/2400、limit_memory、`list_db` 依 Q11；SMTP 段 `smtp_server`／`port`／`ssl`／`user`／`email_from` 可開關，密碼引用 Secret<br>**nginx sidecar**：依 `harumi-nginx-config` 重建，Service 曝露 8080；`/websocket` → 8072；`/web/assets`、`/web/static` 快取用 emptyDir；`client_max_body_size 100m`；`/private_…/` 依 Q10 設定<br>`pgvector/pgvector:pg17`：superuser 預先 `CREATE EXTENSION vector`，應用 user 與 superuser 分開<br>backup CronJob（Q9）<br>`resources.*`、`persistence.size` 沿用 overlay 鍵名<br>**不含** cloudflared、mcp | `helm template` 渲染通過；dev-sandbox 安裝空 DB 可開機；`curl` 經 8080 取 `/web/login` 200、`/websocket` 101；SMTP 關閉時 pod 內 conf 沒有 `smtp_*` | revert PR |
| 1.6 Odoo 端：隱藏 template 與 plan | Agent 開 PR（動 `src/` ⇒ 沙盒跑 `/woow-dev:k8s-dev-sandbox test <slug> --module woow_paas_platform`，結果寫進 PR），人工 merge | `src/data/cloud_app_templates.xml` 新增 `is_hidden=True` 的 record，`helm_value_specs` 最小化；新增測試時更新 `scripts/audit/baselines/test-collection.txt`；manifest 版號不動 | 測試綠燈；marketplace 不列出；補一條測試斷言：新 template 的 conf 值不受 plan `cs_odoo_workers` 影響（避免之後被誤以為 overlay 有效） | revert |
| 1.7 hostname 路由方案 | Agent 評估，人工（CF） | 短期：手動在平台 shared tunnel 加 `harumi-odoo.woowtech.io` 的 ingress rule，service 指向新 instance **nginx 8080**，並寫入 SOP，註明 `create_route` 先讀再寫、沒有鎖。長期：開 issue 做「cloud_service 附加 hostname」 | SOP 核准；stg 用測試 hostname 驗證 rule 不會被後續 create_route 洗掉 | 刪除 rule |
| 1.8 workspace／plan／訂閱 | 人工（業務）＋ Agent | 建 harumi workspace、選 plan（對齊 1.4 的 resources 結論）、確認到期 suspend 策略（Q8） | ns `paas-ws-<hash>` 確定 | — |
| 1.9 Odoo 端 internal provision SOP | Agent | 以 sudo／`_internal_provision`（`src/models/cloud_service.py:77-118`；先例 `src/controllers/ha_paas.py`）建 cloud_service，寫 `helm_values`（不走租戶白名單）；確認 provision 會自動建 `paas-cs-*` 公開 route，演練期間是否要加 Security Access（推論需要，待確認） | stg 可重現 | 刪除 stg instance |

**〔A〕差異**：
- 1.3：改成在 `paas-odoo-ci` 把 `payment_ecpay`、harumi addon、4 個 pip 加進 `cloud-addons.list`，重建共用 `cloud-odoo`（影響全租戶，需 Eugene 另外核准）。
- 1.5 不需要，改為在 `helm_values` sudo 設定：
  - `odooSkipInstall=true`
  - `extraEnvVars` 設 `ODOO_SKIP_MODULES_UPDATE=yes`（鍵名待驗）
  - nginx sidecar（鍵名待驗）
  - `postgresql.primary.persistence.size`（僅首次安裝有效）
  - SMTP 走白名單 `smtp*` 欄位
  - 首次開機前預先還原 DB 的順序
- 1.4 要依 A5 決定 marker 策略：預先 touch，則 conf 要另寫；或設 `odooPassword`。另外要評估 `DELETE FROM ir_attachment WHERE url LIKE '/web/content/%'` 對 harumi 的影響（指令區塊 B 的計數）。

### Phase 2：演練（dev-sandbox 完整還原 → stg 平台流程）

| 步驟 | 執行者 | 指令形狀 | 驗收 | rollback |
|---|---|---|---|---|
| 2.1 dev-sandbox 部署新 chart（空，**SMTP 關閉**） | Agent | `dev-sandbox-*` ns 內 `helm install` | pod Ready；pod 內 conf 沒有 `smtp_*` | `helm uninstall` |
| 2.2 叢集內 push：DB＋filestore | WMO 跑 producer，Agent 跑接收器與 netpol | DB（密碼不落地）：`$K exec <pg> -- printenv POSTGRES_PASSWORD \| $K exec -i deploy/harumi-odoo -c odoo -- sh -c 'read -r PW; pg_dump --version; PGPASSWORD=$PW pg_dump -Fc -h harumi-postgres-svc -U harumi odoo \| curl -sS -T - http://<recv>:8000/harumi.dump'`；filestore：`tar cf - -C /var/lib/odoo/filestore odoo \| gzip -1 \| curl -T - …` | 兩端 sha256 與 bytes 相符；`pg_restore -f /dev/null` 全讀；`gzip -t`；檔數與 bytes 對上 0.4；記下 pg_dump 版本 | 刪除接收端副本 |
| 2.3 restore | Agent | odoo scale 0 → 記錄 `pg_restore --version`（major 要大於或等於 dump 端，A10）→ C：以 superuser 確認 `vector` extension 已建 → `pg_restore --no-owner --no-privileges -d odoo harumi.dump` → 解壓 filestore、依 `id` 實測的 uid chown → scale 1。〔A〕：視 A2 用 `-L` 略過 extension；**首次開機前依 A5 處理 marker 或 `odooPassword`**；chown 成 Bitnami `odoo` user 的實測 uid | `/web/login` 200；log 沒有 ImportError／Traceback；`res_users` id=2 密碼雜湊與來源一致（C），或符合 A5 選定策略（A） | drop DB 重來 |
| 2.4 neutralize（僅演練） | Agent | ORM／SQL：`ir_cron.active=false`、`ir_mail_server` 停用、`payment_provider.state=disabled`、`web.base.url.freeze`；**另外確認 conf 層沒有 SMTP**（2.1 已關閉） | 不寄信、不動金流 | — |
| 2.5 〔A〕倒退 core 驗證 | Agent | 一次性 Job：`-u all --stop-after-init` | **go/no-go gate**：`-u all` 無錯誤，且 2.6 全數通過 | 判定 no-go 就回到 Q1 |
| 2.6 冒煙清單 | Agent（playwright-cli `--headed`、agent-browser） | 管理員登入；121 模組狀態；開 POS session、下單、關帳；self-order 頁與 QR 參數；**經 nginx 8080**：`/websocket` 101 且 bus 收得到通知、`/web/assets` 快取命中且無 500；上傳一個超過 1MB 的附件（驗證 body size 設定生效）；ECPay 設定頁可開（不交易）；報表 PDF；附件與 logo；MCP 依 Q10 方案的連線測試 | 全部通過，截圖路徑列入報告 | — |
| 2.7 量測 | Agent | 記錄各步耗時（對照 #1032 基線）；重啟 pod 兩次，記錄開機時間（C：確認沒有自動 `-u all`；〔A〕：量測每次重啟 `-u all` 的耗時） | 產出停機時間估算 | — |
| 2.8 stg 平台流程演練 | Agent | stg Odoo internal provision 建新 template instance（`paas-ws-stg-*`）→ 重跑 2.2～2.6 → 測 suspend/resume、reconfigure（驗證 `reset_values` 後 conf、nginx、SMTP 開關仍在）、升級後停權服務是否被 `_rescale_halted_after_upgrade` 壓回 0、手動 hostname rule 與後續 create_route 並存 | 生命週期操作不破壞 conf 與資料 | 刪除 stg instance（注意 90 天 PVC grace） |
| 2.9 清理 | Agent | 銷毀 sandbox，刪除含客戶真實資料的副本（Q14） | 列出已刪資源 | — |

**Phase 2 離開條件**：
- 2.6 全數通過兩次（sandbox 與 stg 各一次）。
- 停機估算與 rollback 演練完成。
- 業主確認凍結窗口。
- **Q10（MCP 去向）、Q17（database.uuid）、Q18（SMTP 帳號）已拍板**。

### Phase 3：prod 建 instance 與正式搬遷

> prod 寫入：Agent 需切到 default permission mode，每步人工放行（先例 #1032）；harumi-odoo 端動作全部由 WMO 執行。

| 步驟 | 執行者 | 指令形狀 | 驗收 | rollback |
|---|---|---|---|---|
| 3.1 T-3 天：prod 建 instance（空，**SMTP 關閉**）＋預演還原 | Agent（人工放行） | internal provision → 用 Phase 0 的 dump 走一次 2.2～2.6。**比照 2.4 完整 neutralize**：`ir_cron.active=false` 全部停用、`ir_mail_server` 停用、`payment_provider` disabled、conf 沒有 SMTP。若需要驗證特定 cron，改用白名單，只開逐條核過、確認沒有外部副作用的 cron id，清單寫進 runbook（依指令區塊 B 的 cron 清單）。不公告 `paas-cs-*` hostname | 冒煙通過；3 天內沒有任何外部副作用（寄信、金流、webhook） | 刪 DB 重建 |
| 3.2 T-1 天：Longhorn snapshot（harumi 與新 instance PVC） | 人工或 WMO | 同 0.7 | `readyToUse=true` | — |
| 3.3 T0：公告並凍結 | 人工（業主） | 所有 POS 關帳；確認沒有 open session 與 draft order（0.3 的 SQL）；**通知 MCP 消費者停機窗口** | SQL 回 0 列 | 取消窗口 |
| 3.4 停來源 odoo（保留 postgres） | WMO | `$K scale deploy/harumi-odoo --replicas=0`（此刻 `mcp-odoo` 隨之斷線，屬預期） | pod 消失；外部顯示 502／維護頁 | `scale --replicas=1` |
| 3.5 最終 dump＋filestore push | WMO 跑 producer，Agent 跑接收器 | 同 2.2 | 同 2.2 的驗收 | 同 3.4 |
| 3.6 新 instance restore | Agent（人工放行） | 新 instance scale 0 → drop 3.1 的預演 DB → restore（A10 版本規則）→ filestore → scale 1。〔A〕：依 A5 的 marker 或 `odooPassword` 策略 | `/web/login` 200 | 同 3.4（來源未動） |
| 3.7 post-restore 修正 | Agent（人工放行） | `web.base.url=https://harumi-odoo.woowtech.io` 並 freeze。最終 dump 來自來源，cron、mail、payment 維持來源狀態，**不 neutralize**。`helm_values` 補上 SMTP 值（依 Q18）並 redeploy，確認 pod 內 conf 的 `smtp_*` 正確。`database.uuid`／`database.secret` 依 Q17（建議維持原值）。清孤兒 asset。確認 mcp 使用者與 API key 存在 | ORM 驗證值正確；conf 內 SMTP 正確 | 同 3.4 |
| 3.8 內部冒煙（走 `paas-cs-*` hostname） | Agent | 2.6 清單（不含 ECPay 交易；寄一封測試信給內部信箱） | 通過 | 同 3.4 |

### Phase 4：切換 DNS／tunnel 並驗證

| 步驟 | 執行者 | 指令形狀 | 驗收 | rollback |
|---|---|---|---|---|
| 4.1 平台 tunnel 加 ingress rule | 人工（CF）或 Agent 經 operator，依 1.7 SOP | `harumi-odoo.woowtech.io` → 新 instance nginx Service **8080**（nginx 內部再分流 8069／8072） | CF config 含該 rule，既有 rule 數不變 | 移除該 rule |
| 4.2 DNS 改指向 | 人工（CF） | `harumi-odoo.woowtech.io` 的 CNAME 從 harumi tunnel 改成平台 tunnel | `dig` 與 CF 面板一致 | CNAME 改回 |
| 4.3 從 harumi tunnel 移除該 hostname（不刪 tunnel） | 人工（CF） | 依 0.5 全表只移除這一條；**其他 hostname（如有）保持不動** | 其他 hostname 仍 200 | 加回 |
| 4.4 對外驗證 | Agent＋業主（現場） | `/web/login` 200；`/websocket` 101；現場 POS 裝置重新連線；掃桌面 QR 下單；ECPay 小額或測試回呼（業主決定） | 全部通過 | **rollback 截止點**：新站出現第一筆真實交易前，可以 4.2 改回＋WMO scale harumi-odoo 1。截止點之後只 forward-fix，或人工補登（Q12） |
| 4.5 MCP 切換（依 Q10） | 選項 (a) WMO；選項 (b) Agent（人工放行） | (a) 保留原 release：`mcp-odoo` 改連新站（公開 hostname 或新 instance），新 nginx 的 `/private_…/` 對應調整；若要跨 ns 連 `mcp-odoo-proxy`，WMO 同步調整 `harumi-odoo` netpol。(b) 在 harumi workspace 建 `cloud_app_mcp_odoo_manage`，Connection 填新站，通知消費者新 URL／token | MCP 健康檢查（XML-RPC authenticate 成功、消費端一次實際呼叫） | (a) 設定改回；(b) 刪除服務，暫時停用 MCP |
| 4.6 解凍並公告 | 人工 | — | 營業恢復 | — |

**停機估算（推論）**：
- 純技術約 30 分鐘：dump 加 filestore push 約 1 分鐘、restore 約 1 分鐘、開機 1～2 分鐘、冒煙 10 分鐘、DNS 與 tunnel 5～10 分鐘。
- 〔A〕另加 `-u all` 約 5 分鐘。
- MCP 切換在 POS 恢復營業後可以並行，不計入 POS 停機。
- #1032 預估 30 分鐘、實際 65 分鐘，**建議申請 90 分鐘窗口**。

### Phase 5：觀察期與舊站下線

| 步驟 | 執行者 | 時點 | 指令形狀 | 驗收 | rollback |
|---|---|---|---|---|---|
| 5.1 每日巡檢 | Agent | D+1～D+14 | 新 instance log 的 ERROR 與 Traceback、cron 失敗數、備份 Job 成功、POS 關帳是否正常、寄信佇列（`mail.mail` 失敗數）、MCP 呼叫是否正常 | 連續 14 天無新 class 錯誤 | 視問題 forward-fix |
| 5.2 備份還原驗證 | Agent | D+3 | 取新站當日 dump，還原到 dev-sandbox 並全讀 | 還原成功 | — |
| 5.3 舊站維持停機並保留資料 | WMO | D+0～D+30 | harumi-odoo odoo 維持 0；postgres 可 scale 0；PVC 保留（`longhorn`，Retain） | — | 可隨時 scale 回 |
| 5.4 下線核准 | 人工（Eugene） | D+30 | 確認 5.1、5.2 通過，且離叢集最終 dump 已歸檔 | 書面核准 | — |
| 5.5 舊站下線 | WMO＋人工（CF） | D+30 之後 | `helm uninstall odoo-tenant`；`mcp-odoo` 依 Q10：選 (a) 則留在 `harumi-odoo` ns，ns 不能清空，要另外記錄歸屬；選 (b) 則一併 uninstall。harumi-cloudflared uninstall；刪除 CF tunnel（人工）；Released PV 與 Longhorn volume 在確認歸檔後刪除；wmo repo 的 `odoo-tenant-db-backup/harumi-odoo.yaml` 標記下線 | ns 清空（或只剩 Q10 (a) 的 mcp 資源）；歸檔可還原 | uninstall 前以 snapshot 保底 |
| 5.6 runbook 沉澱 | Agent | D+30 | 回寫 pitfalls 與通用 runbook（§8 #13） | PR 合併 | — |

---

## 6. 風險與 rollback 總表

| # | 風險 | 機率／衝擊 | 緩解 | rollback |
|---|---|---|---|---|
| R1 | 客製 addon 原始碼遺失（PVC 損毀、誤刪） | 低／**極高** | 0.6 保全＋1.2 repo 化＋0.7 snapshot | 從快照或 repo 還原 |
| R2 | 〔A〕core 倒退造成啟動或執行錯誤 | 中／高 | 改走 C；A 以 2.5 作為 go/no-go gate | 回到 Q1 |
| R3 | pgvector 欄位還原失敗（含非 trusted extension 需要 superuser） | 未知／高 | Phase 0 查證；C 用同源 image 並由 superuser 預建 extension | — |
| R4 | 首次開機覆寫 admin 或 `--init=all` | 〔A〕高／高；C 低 | 〔A〕`odooSkipInstall=true` **加上** marker／`odooPassword` 策略（A5，只設 skip 擋不住 `fix-admin-credentials.sh`）；C 在 Phase 2 驗證 id=2 雜湊不變 | drop DB 重還原 |
| R5 | `kubectl exec` 串流截斷導致資料靜默損壞 | 高（若誤用）／高 | 一律叢集內 push＋源頭側 sha 與 bytes 對照 | 重傳 |
| R6 | websocket、bus、asset 或上傳失效（入口層沒有對齊） | 中／高 | chart 內建 nginx sidecar（D8）；冒煙必驗 8080 上的 101、asset、上傳 | 4.2 rollback |
| R7 | limit_time 過短造成關帳或同步被砍 | 〔A〕高／中 | C 沿用 1200/2400 | 調整 chart values 後 redeploy |
| R8 | 更改 hostname 讓 QR 與 ECPay 回呼失效 | 若不保留：高／高 | 保留原 hostname（D5） | DNS 改回 |
| R9 | 誤斷同一 tunnel 上的其他 hostname | 中／中 | 0.5 先列全表，4.3 只移除單一 hostname | 加回 |
| R10 | 手動 tunnel rule 被併發寫入覆蓋，或被當成孤兒 | 低／高 | SOP 記錄；Odoo 端補紀錄的 issue；避開 prod 高頻部署時段切換 | 重加 rule |
| R11 | 搬遷後沒有備份 | 高（若不處理）／高 | chart 內建 CronJob（Q9）；5.2 還原驗證；平台層缺口另開 issue（§8 #8） | — |
| R12 | 訂閱或帳務到期，營業站被 suspend | 中／高 | Q8：專案型 plan 或豁免 | resume |
| R13 | 誤刪服務：`longhorn-delete` 下 90 天後資料永久消失 | 低／極高 | 刪除權限管控；備份離叢集 | 90 天內救回 PVC |
| R14 | 手動 patch 被 `reset_values` 蓋掉 | 中／中 | 所有客製寫進 `helm_values` 或 chart，禁止 kubectl patch | redeploy |
| R15 | 搬遷期間有人又 upgrade harumi | 中（今天剛發生）／高 | 0.1 凍結約定 | 以最終 dump 為準重跑 3.5 |
| R16 | 切換後出現資料分歧（新站已有交易才要 rollback） | 低／高 | 明訂 rollback 截止點（Q12） | 人工補登 |
| R17 | 演練環境用真實客戶資料造成外洩或誤發信 | 中／中 | 2.4 neutralize（含 conf 層 SMTP）；2.9 銷毀；Q14 | — |
| R18 | 平台多一條 lineage 的長期維運負擔 | 確定／中 | ADR 明訂 image 更新責任；若多家採用則通用化 | — |
| R19 | auto-mode classifier 擋下 prod 寫入，窗口延長 | 高／低 | 事先切 default mode；每步由人工在場放行 | — |
| R20 | 3.4 停來源 odoo 後 mcp-odoo 斷線，沒有配套 | 確定（若不處理）／中 | Q10 在 Phase 2 前拍板；3.3 通知消費者；4.5 切換並驗證 | 4.5 各選項的 rollback |
| R21 | 〔A〕每次重啟、redeploy、resume 都自動 `--update=all` | 確定（若不處理）／中 | `ODOO_SKIP_MODULES_UPDATE=yes`；§8 #15 評估全 template 影響 | — |
| R22 | 演練或預演因 conf 層 SMTP 仍在而實際寄信（只停 `ir_mail_server` 擋不住） | 中（若沿用來源 conf）／中 | 演練與預演 instance 不帶 SMTP 值，2.1、3.1 驗證 conf；3.7 才開啟 | 停用 SMTP、通知收件者 |
| R23 | T-3 預演期間 cron 與營業中的來源站雙跑，造成重複外部副作用或序號分歧 | 中（若只停 payment／mail）／中 | 3.1 全部 cron 停用或只開白名單 | 停 cron；外部副作用人工沖銷 |

---

## 7. 待決問題清單

| # | 問題 | 拍板者 |
|---|---|---|
| Q1 | 路線 C 或 A；平台是否接受第二條 Odoo image lineage（官方 `odoo:18.0`）與非 Bitnami 的 postgres（pgvector）。**已定案（2026-09-19）**：平台 odoo template 改用官方 `odoo:18.0` lineage，image 走 JCR；postgres 是否用 pgvector 待 Phase 0 盤點 | 已定案 |
| Q2 | 是否保留 `harumi-odoo.woowtech.io`（建議保留）；長期是否補「cloud_service 附加 hostname」能力 | Eugene＋業主 |
| Q3 | 今天 08:18／13:49 對 harumi 做 helm install／upgrade 的是誰、目的是什麼；搬遷前是否凍結變更 | elmo（向 WMO 或相關 session 確認） |
| Q4 | harumi 客製 addon 的原始碼歸屬（業主或 woow）、要放哪個 Gitea repo | Eugene＋業主 |
| Q5 | AI 功能（google-genai 等）是否仍在使用；不用的話可以移除依賴 | 業主 |
| Q6 | ESC/POS relay（目前 404）是否保留，relay 在哪裡 | 業主 |
| Q7 | 營業與公休時段、凍結窗口 | 業主 |
| Q8 | harumi 的 workspace 與訂閱歸屬、plan；營業站遇到帳務到期是否豁免 suspend | Eugene／業務 |
| Q9 | 備份：chart 內建 CronJob 的保留份數與存放位置（同叢集 PVC 或離叢集）；平台層備份缺口是否另開 issue 追蹤 | Eugene |
| Q10 | **（Phase 2 離開條件）** mcp-odoo 的消費者是誰；搬遷後 (a) 保留原 release 改指向新站（涉及 `harumi-odoo` netpol 與 path proxy），還是 (b) 改用 `cloud_app_mcp_odoo_manage`（需要獨立 hostname，消費端改 URL） | elmo／Eugene |
| Q11 | `list_db` 由 True 改成 False | Eugene |
| Q12 | rollback 截止點與可接受的資料補登範圍 | Eugene＋業主 |
| Q13 | 是否把本案做成其餘 odoo-tenant 站（十餘家，要以最新 `helm list -A` 為準）的通用 runbook 範本 | Eugene |
| Q14 | dev-sandbox 或 stg 使用真實客戶資料的處理規範 | Eugene |
| Q15 | 舊站保留天數（草案 30 天）與最終歸檔位置 | Eugene |
| Q16 | Longhorn snapshot 與 CF tunnel、DNS 操作由誰親跑（人工或 WMO） | Eugene／elmo |
| Q17 | **（Phase 2 離開條件）** 還原後 `database.uuid`／`database.secret` 維持原值（建議）或重產 | Eugene＋業主 |
| Q18 | **（Phase 2 離開條件）** 寄信帳號：續用目前的個人 Gmail（app password 由帳號持有者提供或輪換），或改用公司 SMTP relay | 業主＋Eugene |

---

## 8. 建議開的 Gitea issues（未開）

| # | 標題（草案） | Repo | 範圍 |
|---|---|---|---|
| 1 | harumi-odoo 移植 Phase 0：唯讀盤點（派 woow-machines-operator） | woow_paas_platform | §5 Phase 0 指令區塊 A～D；產出盤點報告；附白名單派工 prompt |
| 2 | harumi 客製 addon 原始碼保全與 repo 化＋Longhorn snapshot＋離叢集 dump | woow_paas_platform（追蹤）＋新 addon repo | 0.6、0.7、1.2 |
| 3 | ADR：harumi 移植路線（C 或 A）與第二條 Odoo lineage 政策 | woow_paas_platform `docs/decisions/` | §4 比較、Q1、Q2、Q9、Q11 |
| 4 | 收編 odoo-tenant chart 為平台 chart（官方 odoo lineage、nginx sidecar 入口、pg17＋vector、SMTP、備份 CronJob） | woow-paas-charts | 1.5 |
| 5 | 新增官方 lineage Odoo image（harumi addons、鎖版 pip）與 build pipeline | paas-odoo-ci | 1.3，含 `/approve` gate |
| 6 | 新增隱藏 odoo template＋確認 odoo 專屬 injector、plan overlay、post-deploy、reset-password、rescale-halted 在新 slug 下的行為＋internal provision SOP | woow_paas_platform | 1.4、1.6、1.9 |
| 7 | cloud_service 附加自訂 hostname（shared tunnel ingress＋DNS＋Odoo 端紀錄，避免成為 L4 孤兒） | woow_paas_platform＋operator | D5 長期方案；短期手動 SOP 可先寫在本 issue |
| 8 | 平台 cloud_service（Odoo）備份機制缺口（程式碼與叢集皆無；Refs #443 異地備份規劃；#512／#440 為 odoo-tenant 站同型旁證，不寫 `Refs #512`） | woow_paas_platform | D6 通用化 |
| 9 | Bitnami odoo template：workers>0 時的 websocket 8072 曝露與 per-instance conf 覆寫 | woow_paas_platform | D3；原始碼已證實任何 `cs_odoo_workers>0` 的 plan 都會斷 `/websocket`，需決定修補或暫時下架該 tier |
| 10 | harumi 移植 Phase 2：dev-sandbox 與 stg 完整還原演練與量測 | woow_paas_platform | §5 Phase 2；含真實資料處理規範（Q14） |
| 11 | harumi 移植 Phase 3～4：prod 搬遷、DNS／tunnel 與 MCP 切換 runbook | woow_paas_platform | §5 Phase 3～4；rollback 截止點（Q12）；MCP（Q10） |
| 12 | harumi 移植 Phase 5：觀察期與舊站下線（WMO 執行） | woow_paas_platform（追蹤）＋woow-machines-operator | 5.1～5.5 |
| 13 | odoo-tenant 客戶站移植通用 runbook（以 #1032＋harumi 為基礎，先取得最新 `helm list -A` 盤點） | woow_paas_platform | Q13；兩次移植的差異沉澱 |
| 14 | 〔僅在 Q1 裁決為 A 時；Q1 已定案為官方 lineage，本項不開〕cloud-odoo 候選加入 `payment_ecpay`、harumi addon 與 pip，並評估 addon 層級可見性 | paas-odoo-ci＋woow_paas_platform | C3、C4、C5 |
| 15 | `cloud_app_odoo` template 沒設 `ODOO_SKIP_MODULES_UPDATE`：每次非首次開機都跑 `--update=all` 的影響評估 | woow_paas_platform | D7⑤；stg 實測重啟耗時；決定是否在 template 設 `yes`（與 paas-odoo 一致） |

**已開單（2026-09-19）**：chart → woow-paas/woow-paas-charts#81（藍本請求 woow-paas/woow-machines-operator#3）；cloud-odoo image → woow-paas/paas-odoo-ci#133；平台側 P0 → #1128（既存實例處置與 chart 換版守門）、#1129（operator odoo exec 路徑）、#1130（`cloud_app_odoo` template 切換）。P1、P2 尚未開單。 上表 #4 對應 charts#81、#5 對應 paas-odoo-ci#133、#6 由平台側三張取代。

---

## 附錄 A：審查爭議

以下幾條是審查主張缺乏證據、或與偵察／查證證據矛盾的部分，逐條記錄判斷理由。

1. **[R-db minor #2] 「Bitnami pod 以 root 執行，chown 成 999 或 0 都不影響功能」**
   - 審查者引用的 chart values `runAsUser: 0` 正確，stg 實測也一致 [V]。
   - 但 Bitnami `run.sh:24-25` 在 root 身分下以 `exec_as_user "$ODOO_DAEMON_USER"` 降權（`odoo-env.sh:90` `ODOO_DAEMON_USER="odoo"`）[V]。odoo 進程不是 root，檔案擁有者錯誤仍會讀不到附件，先例也有實際事件 [P]。
   - **判斷**：「無功能影響」不採納，保留 chown 步驟。採納「999 不直接沿用、要實測 uid」這一半，並把表格改成「容器 root 啟動、進程降權」。

2. **[R-db minor #4] 「來源 pg_dump 隨 pgvector image 是 17.10，restore 應在來源容器內跑、管線直連目標 DB」**
   - 前提與本計畫不符：2.2 的 pg_dump 在 `harumi-odoo` 的 `odoo` 容器內執行，用的是官方 odoo image 內附的 client，不是 pgvector image。
   - 審查者自己也標為推論，引用的官方文件談的是 major 版本規則。
   - 先例 PGDG 18.4 dump → 17.6 restore 實測成功 [P]。
   - 建議做法本身也有問題：從 `harumi-odoo` 容器直連 `paas-ws-*` 的 DB，等於讓非白名單 ns 對白名單 ns 做寫入，而且要另外開跨 ns 的 DB 連線，偏離叢集內 push 的 recipe。
   - **判斷**：保留 push＋目標端 restore 的流程。只採納「兩端記錄版本，restore 用的 client major 要大於或等於 dump 端，失敗時換新版 client」（A10）。

3. **[R-pl missing #1] 「DB PVC 8Gi 在本 repo 找不到出處、未驗證」**
   - 與偵察證據矛盾：[PL] 以 `helm pull` 解包 chart 28.2.10，確認 `charts/postgresql/values.yaml:817/1271` 為 `size: 8Gi`、`statefulset.yaml:8` 為 StatefulSet；[C] 實測 stg `data-svc-42487178-postgresql-0` 就是 8Gi。
   - **判斷**：數字保留，只在 §2 補出處。

4. **[R-db minor #3] database.uuid 與保留 30 天的舊站「會被視為同一 site」**
   - Odoo 原始碼證據成立：pg_dump／restore 不會重產 uuid。
   - 但本案是搬家，舊站在 3.4 之後維持 scale 0，只在 rollback 時短暫回來，不會與新站長期並行回報。
   - **判斷**：採納為待決項（A8、Q17），但建議是**維持原值**，與審查者傾向重產的方向不同。

5. **[R-ops major #2 附帶] 「需查證 Cloudflare 方案上傳上限是否 ≥100MB」**
   - `harumi-odoo.woowtech.io` 搬遷前後都在同一個 zone `woowtech.io` 的 Cloudflare proxy 後面 [C][PL]，CF 端上傳上限不會因搬遷改變，不構成新風險。
   - **判斷**：不另列查證步驟，只要求新 nginx 維持 100m（D8），並在 2.6 做一次上傳驗證。

6. **[P] 「Bitnami chart 的 `ODOO_SKIP_MODULES_UPDATE` 預設 yes」與 [R-db missing #1] 衝突**
   - **查證 [V]**：image 預設為 `no`；paas-odoo 明設 `yes`；租戶 stg deployment 沒有設。
   - **判斷**：採 [R-db]，先例的描述限定在 paas-odoo（見 §2.1-8）。

## 附錄 B：審查修訂紀錄

| # | 來源 | 主張摘要 | 處置 | 理由／落點 |
|---|---|---|---|---|
| 1 | R-db issue major | `odooSkipInstall=true` 擋不住 `fix-admin-credentials.sh` 覆寫 id=2 | **採納** | 證據完整（bitnami `post-init.sh` 的 marker 與 `ODOO_SKIP_BOOTSTRAP` 無關），[PL] 也獨立確認 marker 機制。改寫 A5 為三套機制，並修改 R4、Phase 1〔A〕、2.3、3.6 |
| 2 | R-db issue minor | 999:0 是從 #1032 照搬，pod 以 root 執行 | **部分採納** | 採納「uid 要實測」；不採納「root 所以沒影響」，理由見附錄 A-1。落點 B1、§2、2.3 |
| 3 | R-db issue minor | database.uuid／secret 沒處理 | **採納（建議方向不同）** | Odoo 原始碼證據成立。加進 A8、指令區塊 B、3.7、Q17；建議維持原值，見附錄 A-4 |
| 4 | R-db issue minor | pg_dump／pg_restore 小版本方向 | **部分採納** | 新增 A10 版本記錄與規則；不採納改由來源容器 restore，見附錄 A-2 |
| 5 | R-db missing | Bitnami 非首次開機預設每次 `--update=all` | **採納** | 修訂時查證 [V] 確認租戶 template 沒有設該 env。加進 §2 新列、§2.1-8、D7⑤、§4 表格、R21、2.7、§8 #15 |
| 6 | R-db missing | vector 非 trusted，需 superuser；Phase 0 未查角色屬性 | **採納** | 與先例 [P] 一致。落點 A2、指令區塊 B（`pg_roles`）、1.5、2.3、R3 |
| 7 | R-db confirmed | 官方 image entrypoint 沒有 init 邏輯；workers>0 必定需要 8072 | **採納以升級措辭** | 原稿兩處「推論」改為事實：§4 表格首次開機列、D3、§2 workers 列 |
| 8 | R-pl issue minor | #512 引用錯誤（它是 odoo-tenant 站，不是平台 cloud_service） | **採納** | Gitea issue 內容與 [S] 偵察一致。修改 §1 blocker 5、§2 備份列、D6、R11、§8 #8（不寫 `Refs #512`，改參照 #443 並註明旁證） |
| 9 | R-pl missing | DB PVC 8Gi 未驗證 | **不採納（補出處）** | 與 [PL][C] 證據矛盾，見附錄 A-3。§2 補出處 |
| 10 | R-pl missing | 新 chart 不帶 `customPostInitScripts` 時 workers overlay 靜默不生效 | **採納** | 程式碼證據（`src/models/cloud_service.py:4748-4796`）。落點 §4 理由 6、1.4、1.6 驗收、1.8 plan 選擇 |
| 11 | R-pl missing | `_rescale_halted_after_upgrade` 對新 template 是否生效沒驗 | **採納** | 列為驗證項，不是負面發現。落點 D7⑥、1.4、2.8 |
| 12 | R-pl confirmed | operator 接受任意 chart／values；prod grace 90 天出處；`custom_domain`、`create_route` 行號確認 | **採納以補證據** | §4 理由 4、§2 PVC 列與網域列、D5 |
| 13 | R-ops issue major | conf 層 Gmail SMTP 沒有列為搬遷項目 | **採納** | kubectl 唯讀證據。新增 §2 SMTP 列、D12、指令區塊 A／B、1.5、2.1、2.4、3.1、3.7、R22、Q18；個人帳號值不寫入文件 |
| 14 | R-ops issue major | D3／D5／D8 其實是同一個 nginx sidecar（8080），1.5 不該排除 nginx | **採納** | kubectl 證據（Service 三個 port、雙容器、nginx.conf 全文）。§2 新增入口層列，D8 改寫為入口層整包，D3 只留 conf，D5 指向 8080；1.5 納入 sidecar；指令區塊 A 補明確指令；2.6、4.1、R6 同步。CF 上限不另查，見附錄 A-5 |
| 15 | R-ops issue major | 3.1 T-3 預演只停 payment／mail、cron 照跑，與 2.4 和先例矛盾 | **採納** | 計畫內部矛盾＋先例文件證據。3.1 改為完整 neutralize 或 cron 白名單；指令區塊 B 加 cron 清單；新增 R23 |
| 16 | R-ops issue major | mcp-odoo 沒有排進 Phase 3／4，停來源即斷 | **採納** | kubectl 證據（同 ns ClusterIP 相依）。新增 D13、3.3 通知、3.4 註記、4.5 MCP 切換、R20；Q10 升級為 Phase 2 離開條件；5.5 依 Q10 分支 |
| 17 | R-ops issue minor | 指令區塊 D 的 MCP 是兩個 Deployment 與多個 configmap／secret | **採納** | kubectl 證據。指令區塊 D 拆成 `mcp-odoo`／`mcp-odoo-proxy`，加 `mcp-odoo-proxy-config`；其他 configmap 先只列 key |
| 18 | R-ops missing | 附件上傳大小與 CF 上限 | **部分採納** | 維持 100m 並驗證（D8、2.6）；CF 上限不另查（附錄 A-5） |
| 19 | R-ops missing | 通知、凍結窗口、資料補登、PVC 保留、帳務歸屬已有覆蓋；Longhorn 容量無虞 | **不需修改** | 審查者已確認沒有矛盾 |
| 20 | R-ops confirmed | 鐵律分工、merge／`/approve` gate、manifest 版號、odoo.conf 各值、PVC 實測、netpol | **採納以補證據** | §2 表格各列補 [R-ops] 標記 |
| 21 | 修訂時自行查證 | Bitnami skip-bootstrap 分支會 `DELETE FROM ir_attachment WHERE url LIKE '/web/content/%'` | **新增** | `libodoo.sh` 原始碼 [V]。落點 §2、A5、指令區塊 B 計數、Phase 1〔A〕 |

## 附錄 C：證據索引

**偵察報告**
- [P] 先例研究（#1032）：`docs/plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-design.md`（:48、:97-110、:125-127）、`docs/plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md`（:71-126）、`docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md`、`docs/guides/deployment/prod-release-sop.md`（:122、:237-239、:328、:371-386）、`.claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh`
- [PL] 平台調查：`src/models/cloud_service.py`（:26-34、:77-118、:627-630、:741-745、:1540-1656、:1639、:2388-2506、:4056-4103、:4204-4260、:4747-4796）、`src/controllers/paas.py:3339-3420`、`src/models/sh_subscription_plan_cs_spec.py:14-51`、`src/models/cloud_app_template.py:343`、`src/models/cloud_odoo_addon.py`、`src/services/naming.py`、`extra/paas-operator/src/pvc_keep_post_renderer.py`、`extra/paas-operator/src/api/releases.py:310-460`、`extra/paas-operator/src/config.py:29`；Bitnami chart `odoo:28.2.10`（`helm pull`：`templates/deployment.yaml`、`charts/postgresql/templates/primary/statefulset.yaml:8`、`charts/postgresql/values.yaml:817/1271`）
- [I] image 調查：`woow-paas/paas-odoo-ci` tag `cloud-v20260814.0737`（commit `da91c5ec19`）的 `Dockerfile.cloud`、`requirements-cloud.txt`、`config/cloud-addons.list`、`config/paas-addons.list`；Docker Registry manifest `odoo@sha256:4ea9b466…`（`18.0-20260810`）；`bitnamilegacy/odoo` tag list；`src/data/cloud_app_templates.xml:242`（commit `a7f3d8cb`）
- [S] 來源 IaC：`woow-paas/woow-machines-operator` 的 `resources/manifests/odoo-tenant-db-backup/{README.md,harumi-odoo.yaml}`（最新 commit `87e6f4c0`，2026-08-25）；`woow-paas/woow-paas-charts` tree；`odoo-addons/woow_paas_platform` #512、#440；本機 `.scratch/385-candidate-fulllist-prod.md:85-86`（未入版控）
- [C] 叢集盤點：`volumes.longhorn.io` actualSize；`pg-backup-29824050` log（7.9M）；`backuptargets.longhorn.io`；`storageclass`；harumi-odoo pod log（`--since=9h`）與 nginx access log；netpol `default-deny-external`；`helm list -A`；`paas-ws-stg-4bc02867/svc-42487178` spec；`kubectl describe nodes`

**審查**
- [R-db] db-filestore：bitnami/containers commit `81704d525db8f9284be426549958d82b48f38ec2` 的 `post-init.sh`、`entrypoint.sh`、`libodoo.sh:113-209`、`odoo-env.sh:124-125`；bitnami/charts `odoo/28.2.10` 的 `values.yaml:234-259`、`templates/deployment.yaml:282,313`；Odoo 18.0 `odoo/service/db.py:187-217`、`odoo/tools/config.py:813-814`、`odoo/service/server.py:958-963`；odoo/docker `18.0/entrypoint.sh`、`odoo.conf`；PostgreSQL `REL_17_STABLE` `doc/src/sgml/ref/pg_dump.sgml:1632-1647`；pgvector `vector.control`
- [R-pl] platform-lifecycle：Gitea issue #512（`tea api repos/odoo-addons/woow_paas_platform/issues/512`）；`extra/paas-operator/src/models/schemas.py:54-94`、`:950-957`；`extra/paas-operator/src/services/cloudflare.py:844-890`、`:892`、`:947`、`:1178`、`:1232`；`extra/paas-operator/src/api/reconcile.py`；`src/models/cloud_service.py:1658-1669`、`:2541-2568`、`:4748-4796`；`src/controllers/paas.py:3381/3450/3620`；`src/data/cloud_service_cron.xml`；`woow-paas/paas-app-bundle-cd` 的 `paas-operator/values-prod.yaml:38`、`paas-operator-stg/values-stg.yaml:23`
- [R-ops] ops-completeness：`kubectl --context woow-k3s -n harumi-odoo get configmap harumi-odoo-config`、`harumi-nginx-config`（`jsonpath='{.data}'`）；`get secret harumi-postgres-secret`（只列 key）；`get svc harumi-odoo-svc -o wide`（8069/8072/8080）；`get deploy harumi-odoo`（容器 `odoo nginx`）；`get deploy,configmap,secret`（`mcp-odoo`、`mcp-odoo-proxy` 等）；`get pv … persistentVolumeReclaimPolicy`

**修訂時補做的查證（唯讀）**
- [V1] `kubectl --context woow-k3s -n paas-ws-stg-4bc02867 get deploy svc-42487178-odoo -o json`：`runAsUser: 0`、`fsGroup: 0`；env 只有 `ODOO_SKIP_BOOTSTRAP=no`、`ODOO_DATABASE_NAME=bitnami_odoo`，沒有 `ODOO_SKIP_MODULES_UPDATE`
- [V2] `kubectl --context woow-k3s -n paas-system get deploy paas-odoo -o json`：`runAsUser: 0`；env `ODOO_SKIP_BOOTSTRAP=no`、`ODOO_SKIP_MODULES_UPDATE=yes`
- [V3] bitnami/containers commit `81704d525db8f9284be426549958d82b48f38ec2`（`bitnami/odoo/18/debian-12/rootfs/opt/bitnami/scripts/`）：
  - `odoo/run.sh:24-25`：`am_i_root` 時以 `exec_as_user "$ODOO_DAEMON_USER"` 執行
  - `odoo-env.sh:90`：`ODOO_DAEMON_USER="odoo"`
  - `odoo-env.sh:96-97`：`ODOO_SKIP_BOOTSTRAP`、`ODOO_SKIP_MODULES_UPDATE` 預設 `no`
  - `libodoo.sh:156-181`：首次開機分支，skip 時會 `DELETE FROM ir_attachment WHERE url LIKE '/web/content/%'`，再 `--update=all`
  - `libodoo.sh:196-209`：非首次開機分支，未 skip 時跑 `--update=all`