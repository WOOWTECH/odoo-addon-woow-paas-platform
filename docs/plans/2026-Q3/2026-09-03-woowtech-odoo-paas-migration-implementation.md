---
title: woowtech-odoo → paas 移植（基底翻轉）— implementation
category: plans
status: draft
created: 2026-09-03T04:11:22Z
updated: 2026-09-04T00:30:00Z
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
**產出**：`paas-odoo-ci` 新 image，內含 ERP 自製模組＋pip 依賴。（⚠ 09-03 Phase 2 量出 aiot 非官方已裝為 **29**，image 只有 28 ⇒ 1.8）

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
- [x] 1.7 PR（⛔ 勿代 merge）→ Eugene 合 → 依 `/woow:deploy` 發版流程上 prod（**image 先上、ERP addon 不裝**）
  → ✅ 2026-09-03：paas-odoo-ci#127 merge → tag `v18.0.1.0.177`（develop d1e1633c）→ paas-odoo-ci approval issue #128 `/approve` → phase 2 run 7095 三行錨點全對 → image `paas-odoo:20260903.0732`（digest `sha256:4f60f430…af8e`）→ CD PR-A paas-app-bundle-cd#126（三處 pin＋SKIP=no）／PR-B #127（切回 yes）皆 merge；prod pod 兩容器 digest 4f60f430… 五方一致、custom-addons 36 目錄、抽查 9 個 ERP addon 在 `ir_module_module` 皆 `uninstalled`、`/web/health` 200。⚠ 「DB 不動」不成立：同版帶 #993 migration `.177` 與相依 addon `payment_ecpay_ecpg` 3.0→3.5，故走 SOP 兩段式 `-u all`（判準與實證見 `docs/guides/deployment/prod-release-sop.md` §5.1）；ERP addon 本身零安裝。Phase 2 進入條件（跑穩 ≥1 天）自 08:33Z 起算。
- [ ] 1.8 **補 vendor `sale_combo_enhanced`**（Phase 2.4 量出：aiot installed 的 29 個非官方模組中唯一不在 image 者；來源 aiot PVC `/mnt/extra-addons/sale_combo_enhanced`，18.0.1.1.0、13 檔）
  → 09-04 開出：snapshot `mirror-internal/woowtech-erp-addons-snapshot` PR #1（034dcaddcf9f）→ paas-odoo-ci PR #129（`addons/`＋`paas-addons.list` +1，check-addon-lists 4/4）皆 ⛔ 待 Eugene 合
  → 合併後：`build-image.yml` 手動 dispatch **`addon_ref=v18.0.1.0.177`**（addon 無版號變化，不必發平台版；pitfall ci-addon-ref-empty-drifting-default-branch）→ `/approve` → CD 三處 pin 同進同退（`SKIP=yes` 即可，新模組在 prod 只是 uninstalled）→ **Phase 2 重跑 2.4–2.6**（`dev-sandbox-p2aiot` 換 image 重 restore）
  → `ai_livechat_gt`／`ai_mail_gt` 裁定**不 vendor**（見 2.5／4.4）

### Phase 2 — sandbox 排練（決定性驗證）

**進入條件**：Phase 1 image 已在 prod 跑穩 ≥1 天。→ ⚠ 實際於 .177 上線 **4.5 小時**後（09-03 13:09Z）由 Eugene 裁「開始 Phase 2 排練」提前開始。
**目的**：用真資料把 Phase 4 從頭到尾走一遍，量出所有未知。
**排練環境**：`dev-sandbox-p2aiot`@woow-k3s（`--mode stg`＋prod image `paas-odoo:20260903.0732`；`--values` 把 postgres 換成 `postgres:17`——無 vector、有 pg_trgm、pg17 `pg_restore`——filestore PVC 8Gi、odoo limits 對齊 prod 2cpu/4Gi；建立 6 分鐘）。所有量測、log、截圖在 state 檔指到的 sibling 目錄。

- [x] 2.1 取資料 → **plan 原指令（`kubectl exec … > file`）不可用**：dump 22,631,780 bytes 只收到 19,050,496／19,664,896（尾端截斷、exit 0、`pg_restore -l` 照樣列完整 TOC）；1.9 GB tar 跑 19 分被 `connection reset`。詳見 [pitfall](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md)。
  → ✅ 改**叢集內 push**：沙盒 ns 加 ingress NP 放行 `woowtech-odoo` → 沙盒 odoo pod 跑 HTTP PUT 接收器；aiot odoo pod `tar cf - odoo | gzip -1 | curl -T -`（**58 s、1,662,593,904 bytes**；gzip -t OK、7,193 檔、1,963,793,515 bytes 與源頭全等）；DB 由 aiot odoo pod 的 PGDG `pg_dump 18.4` 打 `woowtech-postgres-svc`（密碼由 postgres pod `printenv` 經 pipe 餵入、不落地）`| curl -T -`（**4.4 s、22,634,115 bytes**，archive 1.16-0）。
  - aiot 實況：postgres `pgvector/pgvector:pg17`（17.10）、DB 207 MB、extension plpgsql／vector 0.8.3（**零使用**）／pg_trgm 1.6（28 個索引）；filestore `/var/lib/odoo/filestore/odoo` 1.9 GB；ir_module_module installed 202／uninstalled 555／uninstallable 24
- [x] 2.2 restore → ✅ 沙盒 odoo pod 內 `pg_restore 17.6`（＝prod bitnami 版）`--no-owner --no-privileges -L <去 vector 的 list>`：**52 s**、exit 0、stderr 0；855 表、模組 202/555/24、trgm 索引 28、active users 35、attachments 12,084；filestore 解壓 29 s → `filestore/woow_prod`
  - ✅ 陽性對照：未去 vector 的 list ＋ `--exit-on-error` 在 postgres:17 停在 `CREATE EXTENSION IF NOT EXISTS vector`（0 表）⇒ 略過步驤真的被驗到
  - 🔴 前提修正：prod bitnami 17.6 **有** vector 0.8.0（`vector.so`／`pg_available_extensions`），但 `trusted=f`、`bn_odoo` rolsuper=f ⇒ 建不了 ⇒ **略過仍必要（權限），且 restore 必帶 `--no-owner --no-privileges`**（aiot 角色 woow-odoo → bn_odoo）
- [x] 2.3 neutralize → ✅ ir_cron active 40→0、ir_mail_server 1→0、fetchmail 0、payment_provider 19 全 disabled（原 enabled 2）、web.base.url → 沙盒 host＋freeze
- [x] 2.4 `-u all` → ✅ **4 分 42 秒**（202 modules / 100,999 queries），exit 0，Traceback 0；**ERROR 1**：`odoo.tests.common: Importing test framework…`（`barcode_scanner_account` 在業務碼 import odoo.tests，aiot 現況同）⇒ **判準改為「零 ERROR，本條 benign 例外」**；WARNING 451 全為既有 addon 品質警告
  - 🔴 **模組缺漏**：aiot 非官方已裝實為 **29**（design 寫 28），`sale_combo_enhanced`（installed、148 KB、inherit sale.order／sale.order.line／account.move.line）不在 image ⇒ 排練從 aiot PVC 複製進沙盒 `data_dir/addons/18.0/` 繼續；**正式修法＝Phase 1 補 1.8**
- [x] 2.5 `-i woow_paas_platform` → ✅ **48 s**，新裝 6：ai_base_gt odoo_ai_assistant_chatgpt_connector payment_ecpay_ecpg sh_subscription sh_subscription_wallet woow_paas_platform（ecpay_invoice_tw／payment_ecpay aiot 原有）→ installed 208。ERROR 5 逐條定性：
  - `CREATE EXTENSION IF NOT EXISTS vector` ← `ai_base_gt/models/ai_data_item.py:56`，失敗不中斷（prod bn_odoo 同樣建不了；平台現行即在無 vector 下跑）
  - `res_company.sh_journal_id` unable to set NOT NULL ×2 ← 暫態，結束後 company 1 值＝8（INV）；**Phase 4 驗收 assert 非空**
  - `inconsistent states: ['ai_livechat_gt','ai_mail_gt']` ← 兩者 auto_install、aiot 的 ir_module_module 有列（PVC 有目錄、未裝）、image 沒有 ⇒ 卡 `to install`。排練 SQL 改回 uninstalled；**Phase 4 runbook 要嘛 vendor 進 image、要嘛寫入這步 SQL**（aiot 共 92 列 image 沒有的模組、20 列 auto_install，本次只這兩個被觸發）
- [x] 2.6 檢核點 → ✅（RPC log `check26-woow_prod.log`＋截圖 `screenshots/01–08`）
  - ERP：登入 uid 2（system）；Journal Items 6,618 posted lines／Invoices 352／Partner Ledger／Payslips(1216) 1／Salary Structures 1／SO 688／Employees 7 — action 載入＋ACL＋count 全通；截圖 Journal Items 1,991 筆分組、Payslips 列。⚠ `action_hr_payslip`(1217) 直接開會 `EvalError active_id`——smart button 專用 action，aiot 同，非回歸
  - LINE：`POST /line/webhook` 403（簽章）、`/api/line/me` 405、`/liff/redirect` 200、`/liff/news` 200（皆非 404）
  - 平台：`/woow` 200＋Dashboard 渲染；cloud_app_template **16** 筆；`action_cloud_service` 在 woow_prod 是 **id 1542**（prod 541 ⇒ 用 xmlid 驗），admin 載入 OK（#994）
  - 模組：installed 208、無 to upgrade/to install/to remove（修正 ai_* 後）
  - ProxyFix：system-user 登入寫 `web.base.url` 為探針——帶 `X-Forwarded-Host`＋`X-Forwarded-Proto: https` → `https://aiot.woowtech.io` ✅；**只送 Proto 不送 Host → 被忽略**（Odoo 18 http.py 只在 X-Forwarded-Host 存在時套 ProxyFix）⇒ image 不補 `http.py` patch，改列 **4.0 前置檢查**。⚠ 用 SQL 改 ir_config_parameter 探針會被 ormcache 假陰性，要走 ORM
- [x] 2.7 耗時（沙盒 2cpu/4Gi；prod paas-odoo 同 limits）：搬運 ~1 分（叢集內）｜restore 52 s｜filestore 解壓 29 s｜`-u all` 4m42s｜`-i` 48 s｜DB 切換（configmap＋restart）到 `/web/login` 200：切到 woow_prod 306 s（含 stg chart 的 `-u web` 282 s，prod `SKIP=yes` 沒這步 ⇒ 估 ~70–90 s）。**Phase 4 純技術停機估 ≈ 10 分鐘**（不含 replay 設定與驗證）
- [x] 2.8 rollback 演練 → ✅ 沙盒版「`ODOO_DATABASE_NAME` 切換」＝改 odoo configmap `db_name`＋rollout restart（`switch-db.sh`）：woow_prod → woow_p2aiot **116 s** 到 `/web/login` 200，`database/list=['woow_p2aiot']`、admin 登入 uid 2、83 模組、`/woow` 正常；再切回 woow_prod **307 s（同樣含 `-u web`；prod 無此步）**。三段 rollback 中「指回平台 DB」這段證明可退
  - 離叢集副本（Phase 3 方法驗證）：dump 經 HTTP over port-forward 18 s、sha 相符；filestore 1.66 GB 走 100 MB 分塊逐塊 sha256（port-forward 會死、python http.server 無 Range）→ 首輪 4/17 塊（~1.3 MB/s）後被 2.8 演練的 pod 重啟打斷（接收 pod 換了），重跑結果補記 state 檔；方法本身成立、但 Phase 3 要預留 30 分以上

**離開條件**：2.6 全綠、2.8 通過、耗時已記錄。→ **條件式通過**：流程與數字全到位，但 image 缺 `sale_combo_enhanced`（Phase 1.8）⇒ 新 image 出來後**重跑 2.4–2.6**（同沙盒可重用：drop `woow_prod` 重 restore 約 2 分）才算離開。✅ Eugene 2026-09-04 裁決：(a) `ai_livechat_gt`／`ai_mail_gt` **SQL 改回 uninstalled**（不 vendor，維持 aiot 現狀，寫進 4.4）；(b) `sale_combo_enhanced` **vendor**（1.8 → paas-odoo-ci PR #129＋snapshot mirror-internal PR #1）；(c) `seed-from-stg.sh` **最小 fail-loud**（woow_paas_platform PR #1039）。

### Phase 3 — 保護（切換前 24h 內）

> 📣 排定 Phase 3／4 日期後**先通知 issue-leader**：#955 prod 半邊（operator image／agentRuntime 0.5.0／skillBundleSecret）以「Phase 3 開始前 ≥24h 全部落地」為硬條件，它要對這條線（2026-09-04 協調）。

- [ ] 3.1 當日 `pg_dump -Fc`＋既有每日 dump 各一份 → 離叢集（本機＋NAS）。⚠ **不可用 `kubectl exec`／`cp` 從 pod 拉**（>~20 MB 尾端截斷，見 2.1）：先叢集內 push 到 paas 側 PVC（同 2.1 方法），再以 HTTP over port-forward 分塊＋逐塊 sha256 拉到本機；完整性判準 `pg_restore -f /dev/null`（`-l` 驗不到）
- [ ] 3.2 filestore tar → 離叢集；`sha256sum` 存證（同上方法；1.66 GB 分塊拉約 20–30 分，`gzip -t`＋檔數 7,193／bytes 對源頭 `find`／`du -sb`）
- [ ] 3.3 `kubectl get -n woowtech-odoo all,cm,secret,pvc,cronjob -o yaml` → 加密存放（含 `woowtech-github-token`、`woowtech-secrets`、`woowtech-postgres-secret`）
- [ ] 3.4 **還原驗證**：3.1＋3.2 在 sandbox 還原一次能起（＝Phase 2 的縮小版）
- [ ] 3.5 paas 側：`bitnami_odoo` 也 dump 一份（rollback 的另一半）

**離開條件**：3.4 通過、所有副本的 sha256 已記在本檔。

### Phase 4 — 切換（離峰窗，日期待 Eugene 排）

- [ ] 4.0 **前置檢查**（切換前任何時候可做）：(a) CF dashboard 確認平台 tunnel（remote-managed，叢集內看不到規則）對 `paas.woowtech.io` 與將加入的 `aiot.woowtech.io` 的 service 打 traefik **websecure（https）**——traefik 一律送 `X-Forwarded-Host`，Proto 取決於此；Odoo 18 需兩者同時存在才套 ProxyFix（2.6 實測）；(b) `paas-system` 加 ingress NP 放行 `woowtech-odoo`（叢集內 push 用）；(c) 準備接收器（`recv.py`）與 `-L` 清單流程
- [ ] 4.1 公告；cloudflared `scale --replicas=0`（aiot 外部斷）；Odoo 本體保持 up
- [ ] 4.2 最終 dump＋filestore tar：**叢集內 push** 到 paas-odoo pod（aiot odoo pod `pg_dump 18.4 | curl -T -`、`tar | gzip -1 | curl -T -`，實測合計 ~1 分）→ 校驗（`pg_restore -f /dev/null`、`gzip -t`、檔數／bytes）→ 離叢集副本可事後補拉
- [ ] 4.3 restore 進 `paas-odoo-postgresql` 成 **`woow_prod`**（同 2.2：`-L` 去 vector——bn_odoo 非 superuser 建不了、**`--no-owner --no-privileges`**；pg_restore 17.6 在 paas-odoo pod 內；實測 52 s）；filestore 進 paas-odoo PVC `filestore/woow_prod`（20Gi 夠；解壓 29 s）
- [ ] 4.4 對 `woow_prod`：neutralize 不做（正式切換要 cron／mail／payment 活著，但 **web.base.url 改 https://aiot.woowtech.io 前先 freeze**）→ `-u all`（4m42s；ERROR 只允許 `Importing test framework` 那條）→ `-i woow_paas_platform`（48 s；預期 ERROR：ai_base_gt `CREATE EXTENSION vector`、`sh_journal_id` NOT NULL 暫態）→ **SQL 修 `to install` 且不在 addons_path 的列**（`ai_livechat_gt`／`ai_mail_gt`；09-04 裁決：不 vendor、就用這步 SQL）→ assert `res_company.sh_journal_id` 非空、無 to upgrade/to install/to remove
- [ ] 4.5 replay 設定（spec §8 步驟 5 清單；**`default_*_id` 三個要重選**；ecpay 不動；`web.base.url=https://aiot.woowtech.io`）
- [ ] 4.6 `paas-app-bundle-cd` 改 Fleet values：`ODOO_DATABASE_NAME=woow_prod`、`dbfilter=^woow_prod$`、odoo.conf `workers=6`／`limit_time_cpu=600`／`limit_time_real=1200`／memory 對齊 → PR → 合 → Fleet 套用 → pod 重啟
- [ ] 4.7 `paas-system/pg-backup` cronjob 目標 DB → `woow_prod`
- [ ] 4.8 驗證（同 2.6，加金流 sandbox 交易一筆；`action_cloud_service` 用 xmlid 不用 id；**aiot 登入頁 OAuth `redirect_uri` 必須是 `https://aiot.woowtech.io/auth_oauth/signin`**——ProxyFix／header 鏈的終驗）
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
- [x] Phase 2：2.6 全項有證據、2.8 rollback 演練通過、耗時已記（09-03；**條件式**——待 1.8 新 image 重跑 2.4–2.6）
- [ ] Phase 3：3.4 還原驗證通過、sha256 表已填
- [ ] Phase 4：4.8 全綠、rollback 三段各自能退（至少 4.6 那段在 Phase 2 演練過）
- [ ] 每個 phase 的結果回寫本檔（打勾＋量測值），不另開報告

## 相關

- [design spec](./2026-09-03-woowtech-odoo-paas-migration-design.md)（方案、裁決、現況量測、陷阱）
- 裁決與量測原始檔：`~/Documents/woow/AREA-odoo/woow-addons/woowtech-odoo-migration-state.md`
- #994、#996
