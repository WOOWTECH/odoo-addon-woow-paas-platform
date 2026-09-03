---
title: woowtech-odoo → paas 移植（基底翻轉）— implementation
category: plans
status: draft
created: 2026-09-03T04:11:22Z
updated: 2026-09-03T04:11:22Z
related:
  - ./2026-09-03-woowtech-odoo-paas-migration-design.md
  - https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/994
  - https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/996
---

# woowtech-odoo → paas 移植（基底翻轉）— implementation

## 目的

把 [design spec](./2026-09-03-woowtech-odoo-paas-migration-design.md) 的五個 phase 展開成可逐項打勾的執行清單：每步的指令、要量什麼、紅燈長什麼樣。給執行的人（含 agent）照做；裁決與量測依據見 spec，這裡不重複。

**守線**：每個 phase 都有「進入條件」與「離開條件」，離開條件沒量到就不進下一個。任何一步失敗優先 rollback 而不是 forward-fix。

## 內容

### Phase 0 — 前置（已完成 09-02～03）

- [x] 兩側現況全量測（spec §3）
- [x] gitea `mirror-internal` 9 個 mirror，sha 逐一等於 GitHub
- [x] gitea `mirror-internal/woowtech-erp-addons-snapshot` 21 個模組（`3a572a32a92e`），css 與 prod byte-identical
- [x] 28 個活模組 tar＋SHA256 存證（`erp-live-addons-snapshot/`）
- [x] spec 核准（PR #1003 merged）
- [x] Gitea 追蹤單 #1032

### Phase 1 — image（可先行，對現行 paas DB 零行為變化）

**進入條件**：Phase 0 全勾。
**產出**：`paas-odoo-ci` 新 image，內含 ERP 28 個自製模組＋pip 依賴。

- [x] 1.1 在 `paas-odoo-ci` 開分支，**照該 repo 的規矩 vendor**（不是 build 時 clone——`Dockerfile:5` 是 `COPY ./addons/` 全量，沒有任何 clone 步驟；權威見 paas-odoo-ci `CLAUDE.md` §Addon 管理架構）：
  → ✅ 09-03 paas-odoo-ci PR #127 commit 355a1834（26 模組，5 mirror＋21 snapshot@3a572a32）
  - 26 個模組目錄複製進 `addons/`（5 個從 gitea mirror 的本機 clone、21 個從 `woowtech-erp-addons-snapshot`），commit message 記每個來源 repo＋sha（該 repo 慣例 `chore: 同步 X 至上游（sha）`）
  - `config/paas-addons.list` 同步 +26（CI `check-addon-lists.sh` 強制與 `addons/` 目錄集合**嚴格相等**）
  - ⚠ `sh_document_management`／`Odoo_tool-device_borrow`／`Woow_odoo_acconting_enhance` 三個 mirror **不進 image**（ERP 沒裝）
  - ⚠ `cloud-addons.list` **不動**（ERP 模組不開放給 cloud-odoo 租戶）
- [x] 1.2 pip：**不能手改 `requirements-paas.txt`**（CI 每次 build 用 `generate-requirements.sh` 從各 addon manifest 的 `external_dependencies.python` 重產並覆寫）。唯一正路＝把相依寫進各 addon 自己的 manifest，且必須是 **pip 可安裝的名稱**（產生器把它當 pip 行；Odoo 18 `check_python_external_dependency` 用 `Requirement()` 解析，pip 名與 `==` spec 都合法）。依「逐模組 grep 實際 import」的 audit 決定：`PIL`→`Pillow`；`cloud_link` 補 fs／vuefinder-wsgi／munch／cachetools；`cloud_link_gdrive` 補 fs.googledrivefs==2.4.4（PyPI 有同版，取代 vendored wheel）＋google 家族；三個模組補 requests。install.sh 其餘套件無已裝模組 import，不搬。
  → ✅ commit d7f6ae25；pip dry-run 一次解開；build log pip 層實跑 42s
- [x] 1.3 **不做** `http.py` ProxyFix patch（Phase 2 驗）
- [x] 1.4 build stg image（`deploy-stg` skill 的 image 路徑），記 tag
  → ✅ head `paas-odoo-stg:20260903.0547`（run 7058）、base `paas-odoo-stg:20260903.0550`（main，run 7059；兩者 woow_paas_platform 同版 18.0.1.0.177／tests sha 41994ee697dc）
- [x] 1.5 sandbox（docker-desktop）用新 image 對**現行 `bitnami_odoo` 快照**跑全模組測試：
  → ✅ woow-k3s `dev-sandbox-erp-{base,head}`（本機 docker 對 jcr-prod 憑證失效，改在叢集跑）：兩邊各 **3844 支、14 failed＋11 error、25 支紅逐支同名、差集空**（log `phase1-test-erp-{base,head}.log`、清單 `phase1-red-*.txt`、摘要 `phase1-diff-summary.txt`）
  - base：現行 image → `N failed` 清單
  - head：新 image → 清單
  - **離開條件**：差集為空（照 #994 的 base/head 對照法，兩次都跑、逐 class 比對名字不比條數）
- [x] 1.6 `odoo -u woow_paas_platform` 對現行 DB 起得來、`/woow` 首頁 200、模組列表多出 28 個 `uninstalled`
  → ✅ head 沙盒 custom-addons 36 目錄、28/28 在 ir_module_module（26 uninstalled＋2 installed＝ecpay）、/web/health 200、/woow → login 200
- [ ] 1.7 PR（⛔ 勿代 merge）→ Eugene 合 → 依 `/woow:deploy` 發版流程上 prod（**image 先上、DB 不動**）

### Phase 2 — sandbox 排練（決定性驗證）

**進入條件**：Phase 1 image 已在 prod 跑穩 ≥1 天。
**目的**：用真資料把 Phase 4 從頭到尾走一遍，量出所有未知。

- [ ] 2.1 取資料（`KUBECTL_NS_GUARD=off`，唯讀）：
  ```
  kubectl --context woow-k3s exec -n woowtech-odoo deploy/woowtech-postgres -- sh -c 'PGPASSWORD=$POSTGRES_PASSWORD pg_dump -U woow-odoo -d odoo -Fc' > aiot-YYYYMMDD.dump
  kubectl --context woow-k3s exec -n woowtech-odoo deploy/woowtech-odoo -c odoo -- tar czf - -C /var/lib/odoo/filestore odoo > aiot-filestore-YYYYMMDD.tgz
  ```
  校驗：`pg_restore -l` 能列、tar 能 `-t`；記大小（預期 dump ≈ 數十 MB、filestore 1.9 GB）
- [ ] 2.2 sandbox（Phase 1 image）建新 DB `woow_prod`；`pg_restore -l` 產清單、**刪 `EXTENSION vector` 那行**、`-L` 餵回；filestore 解到 `data_dir/filestore/woow_prod`
  - ⚠ **沙盒的 postgres 是 `pgvector/pgvector:pg15`、prod 是 bitnami 17.6（無 vector）**（09-03 實測）⇒ 在沙盒「忘了略過 vector」也會成功，這步**不會自然被驗到**。要嘛臨時起一顆 bitnami 17.6 容器做 restore 對照，要嘛至少 assert `-L` 清單裡沒有 `EXTENSION - vector` 那行。另外 pg15 的 `pg_restore` 讀 pg17 產的 `-Fc` dump 可能拒絕新格式，先比 `pg_restore --version`。
- [ ] 2.3 **neutralize**（restore 完、起 Odoo 前，直接 SQL）：`UPDATE ir_cron SET active=false`；`UPDATE ir_mail_server SET active=false`；`UPDATE fetchmail_server SET active=false`；`UPDATE payment_provider SET state='disabled'`；`web.base.url` 改 sandbox host
- [ ] 2.4 `odoo -d woow_prod -u all --stop-after-init`（對齊 stock vs bitnami build）→ log 零 ERROR
- [ ] 2.5 `odoo -d woow_prod -i woow_paas_platform --stop-after-init` → 記錄拉進來的 depends 清單、零 ERROR
- [ ] 2.6 **檢核點**（每項要有截圖或 log）：
  - ERP：登入（既有帳號）、會計報表開得出、payroll 頁、LINE webhook route `/line/*` 回 200/405 而非 404
  - 平台：`/woow` 起得來、marketplace 列 template、`/odoo/action-541` 看得到（#994 已合）
  - 模組衝突：`ir_module_module` 裡 ERP 28 個全 installed、平台 depends 全 installed、無 `to upgrade`／`to remove` 殘留
  - `http.py` ProxyFix：登入後 redirect 與 asset URL 是 https；壞了才在 Phase 1 補 patch（回 1.3）
- [ ] 2.7 記 restore 總耗時（spec 預估 <30 min），寫回本檔
- [ ] 2.8 **rollback 演練**：把 sandbox 的 `ODOO_DATABASE_NAME` 指回 `bitnami_odoo` 快照，確認平台照常 → 證明 Phase 4 的退路真的能走

**離開條件**：2.6 全綠、2.8 通過、耗時已記錄。任一項紅 → 修 Phase 1 或 spec，重跑 Phase 2。

### Phase 3 — 保護（切換前 24h 內）

- [ ] 3.1 當日 `pg_dump -Fc`（同 2.1 指令）＋既有每日 dump 各一份 → 離叢集（本機＋NAS）
- [ ] 3.2 filestore tar → 離叢集；`sha256sum` 存證
- [ ] 3.3 `kubectl get -n woowtech-odoo all,cm,secret,pvc,cronjob -o yaml` → 加密存放（含 `woowtech-github-token`、`woowtech-secrets`、`woowtech-postgres-secret`）
- [ ] 3.4 **還原驗證**：3.1＋3.2 在 sandbox 還原一次能起（＝Phase 2 的縮小版）
- [ ] 3.5 paas 側：`bitnami_odoo` 也 dump 一份（rollback 的另一半）

**離開條件**：3.4 通過、所有副本的 sha256 已記在本檔。

### Phase 4 — 切換（離峰窗，日期待 Eugene 排）

- [ ] 4.1 公告；cloudflared `scale --replicas=0`（aiot 外部斷）；Odoo 本體保持 up
- [ ] 4.2 最終 dump＋filestore tar → 離叢集 → 校驗（同 3.1–3.2）
- [ ] 4.3 restore 進 `paas-odoo-postgresql` 成 **`woow_prod`**（同 2.2，略過 vector）；filestore 進 paas-odoo PVC `filestore/woow_prod`（20Gi 夠）
- [ ] 4.4 對 `woow_prod`：`-u all` → `-i woow_paas_platform`（同 2.4–2.5）
- [ ] 4.5 replay 設定（spec §8 步驟 5 清單；**`default_*_id` 三個要重選**；ecpay 不動；`web.base.url=https://aiot.woowtech.io`）
- [ ] 4.6 `paas-app-bundle-cd` 改 Fleet values：`ODOO_DATABASE_NAME=woow_prod`、`dbfilter=^woow_prod$`、odoo.conf `workers=6`／`limit_time_cpu=600`／`limit_time_real=1200`／memory 對齊 → PR → 合 → Fleet 套用 → pod 重啟
- [ ] 4.7 `paas-system/pg-backup` cronjob 目標 DB → `woow_prod`
- [ ] 4.8 驗證（同 2.6，加金流 sandbox 交易一筆）
- [ ] 4.9 DNS：`aiot.woowtech.io` CNAME 從舊 tunnel 改指平台入口；traefik ingress 加 host；`paas.woowtech.io` 保留
- [ ] 4.10 舊 `woowtech-odoo` ns：**只 `scale --replicas=0`，不刪**；記下切換時間

**Rollback**（任一步紅）：4.6 反向（指回 `bitnami_odoo`）＋ 4.9 反向（DNS 切回）＋ 4.10 反向（scale 回 1）。三段獨立，只退需要的那段。

### Phase 5 — 收尾（切換後 ≥2 週）

- [ ] 5.1 mcp-odoo×3／n8n 改指 `paas-odoo.paas-system.svc`；先驗 `paas-system` NetworkPolicy 是否放行來源 ns
- [ ] 5.2 密碼全輪換（admin、SMTP、postgres、GitHub token、CF tunnel token）
- [ ] 5.3 alan 的 HA：`helm uninstall` ＋ 刪 `paas-ws-8594c519`（PVC 隨 `longhorn-delete` 消失，已裁決）
- [ ] 5.4 `billing-philosophy.md` §5 補「本次為反向操作」對照段
- [ ] 5.5 觀察期滿 → 除役舊 ns（另開單，不在本計畫內自動做）

## 驗證

- [ ] Phase 1：base/head 差集為空（log 留 sibling 目錄）
- [ ] Phase 2：2.6 全項有證據、2.8 rollback 演練通過、耗時已記
- [ ] Phase 3：3.4 還原驗證通過、sha256 表已填
- [ ] Phase 4：4.8 全綠、rollback 三段各自能退（至少 4.6 那段在 Phase 2 演練過）
- [ ] 每個 phase 的結果回寫本檔（打勾＋量測值），不另開報告

## 相關

- [design spec](./2026-09-03-woowtech-odoo-paas-migration-design.md)（方案、裁決、現況量測、陷阱）
- 裁決與量測原始檔：`~/Documents/woow/AREA-odoo/woow-addons/woowtech-odoo-migration-state.md`
- #994、#996
