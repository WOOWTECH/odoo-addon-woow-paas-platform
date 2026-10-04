---
title: 平台 Odoo 換 chart：彩排 runbook
category: guides
status: draft
created: 2026-10-04T08:30:00Z
updated: 2026-10-04T08:30:00Z
related: [docs/guides/deployment/platform-odoo-cutover-runbook.md, docs/guides/deployment/platform-odoo-cutover-switchback-runbook.md, docs/guides/deployment/platform-odoo-cutover-teardown-runbook.md, docs/guides/deployment/prod-release-sop.md]
---

# 平台 Odoo 換 chart：彩排 runbook

> 四份同族 runbook：**彩排（本檔）**／[cutover](platform-odoo-cutover-runbook.md)／[回切](platform-odoo-cutover-switchback-runbook.md)／[teardown](platform-odoo-cutover-teardown-runbook.md)。票：odoo-addons/woow_paas_platform#1231。

## 狀態

`status: draft`——**先草擬、後定稿**（#1231 票面）。下表的佔位落地、且至少一次彩排的量測回填後，替換全部待定佔位並改 `status: active`，由發起人覆核。

| # | 佔位 | 出處票 |
|---|---|---|
| R1 | 沙盒工具以 web／cron 副本 0 部署 `woow-odoo` platform profile 的子命令與參數 | cc/woowprd#8 |
| R2 | 正式平台 image＋chart 0.2.5 的實機 smoke 結論（彩排前置） | woow-paas/woow-paas-charts#99 |
| R3 | 中和腳本名、斷言 fixture、中和段彩排 runbook 的檔名與介面 | #1206 |
| R4 | 穩定 Service 名與 raw bundle 目錄（暫名 `paas-odoo-active`／`paas-odoo-edge/`） | woow-paas/paas-app-bundle-cd#143 |
| R5 | 平台新 release 名與 bundle 目錄（暫名 `paas-odoo-platform`） | woow-paas/paas-app-bundle-cd#144 |
| R6 | 平台還原的人工段（helper pod 形狀、指令） | #1217 |
| R7 | 叢集容量第一版結論（沙盒容量是否放行） | woow-paas/woow-machines-operator#5 |
| R8 | cc/woowprd#6 一次性升級 Job 與本路徑的關係（新 chart 路徑不經它，是否仍是 #1206 前置） | #1206 留言 14673（「由發起人定」） |
| R9 | chart 版號：platform 記憶體預設改為 soft 4GiB／hard 5GiB 的那一版（進行中，暫記 0.2.6） | woow-paas/woow-paas-charts#99 |

## 操作者圖例

| 標記 | 意義 |
|---|---|
| 【人類】 | 發起人或叢集運維親手執行（依 #1142 留言 13439 ⑧、map #1134 2026-09-26 裁決）。agent 不代跑。 |
| 【agent】 | agent 可自行執行。僅限 `dev-sandbox-*`、唯讀 get、本 repo 檔案。 |
| 【agent 執行・人類逐步放行】 | agent 備妥指令，每一步由人類看過、放行後才執行（auto-mode classifier 會擋 prod 寫入，13439 ⑧）。 |
| 【agent 開 PR・人類 merge】 | agent 開 CD／repo PR，merge 是人工 gate（`prod-release-sop.md` §5.1.1 同精神）。 |

**步驟行慣例**：以「數字＋句點＋空格」開頭的行是步驟行，每一行帶、而且只帶一個上表標記；其餘說明用 `-` 子項，不帶標記。

## 術語

- 引用 [CONTEXT.md](../../../CONTEXT.md)，不重新定義：**平台 Odoo**（CONTEXT.md:77）、**備份**（:97；Longhorn snapshot 不叫備份，叫「還原錨點」，見 :122、:125）、**prod clone**（:130）、**中和**（:133）、**彩排**（:136）。
- **停機窗口**：平台切換的停機時段（13439 ⑧：非上班日凌晨 60 分，技術停機 15～20 分）。⚠ 不套 CONTEXT「維護窗口」（:118），那是租戶 odoo 的 22:00～01:00。
- **回退窗口**：cutover 起固定 14 天（13439 ③）。
- **升級**、**升級前備份**（CONTEXT.md:100、:121）是對 odoo `cloud_service` 定義的；本檔借用時明寫「平台」。
- **回切**＝反向搬遷回 Bitnami 側（不叫 rollback／回退；CONTEXT.md:125、:128 把它們保留給 helm revision 回退）。

出處縮寫（未標 repo 者為 odoo-addons/woow_paas_platform）：

| 縮寫 | 位置 |
|---|---|
| 13439、14050、14356、14701 | [#1142 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1142#issuecomment-13439)（resolution 與追加） |
| 13786、14690 | [#1145 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1145#issuecomment-13786) |
| 14057、14676 | [#1162 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1162#issuecomment-14057)（D1～D26） |
| 14201、14686 | [#1174 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1174#issuecomment-14201) |
| 14363、14698 | [#1194 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1194#issuecomment-14363) |
| 14632（R2-Qn） | [woow-paas/woow-paas-charts#81 Round 2 resolution](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/issues/81#issuecomment-14632) |
| 14662、16804 | [#1231 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1231#issuecomment-14662) |
| chart README | woow-paas/woow-paas-charts `main` 的 `charts/woow-odoo/README.md`（0.2.5） |
| #1032 plan | [`2026-09-03-woowtech-odoo-paas-migration-implementation.md`](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md) |

## A0 定位與邊界

- 只處理平台 `woow_prod`（14057 D1）。租戶 odoo DB clone 不在範圍。
- ⛔ 禁止重用或覆蓋 `dev-sandbox-stg`：它是 `seed-from-stg.sh` 的來源，也是 `paas-operator-stg` 的 `odooBaseUrl` 目標，誤用＝stg 全租戶事故（13439 ⑥）。
- 與 #1206 交付 3 分工：中和段、斷言表、外連證據、PII 與銷毀規則一律**引用** #1206〔待定：#1206（R3）〕；本檔只寫 chart 切換段。
- 彩排不是正式切換的替身；與正式切換的差異列在 A3 差異表（CONTEXT.md:136）。
- 彩排使用的現成值（已落地，不必佔位）：
  - chart：`woow-odoo` **0.2.5**（woow-paas/woow-paas-charts#120 merged；platform `addons_path` 含 `/opt/woow/addons`）。彩排改用含 platform 記憶體新預設的下一版〔待定：woow-paas/woow-paas-charts#99（R9）〕。
  - 平台 image：`jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-official:20261003.1514@sha256:0be8cc620dc457ee81ef804f0b8c974c22ccec37d137576c27d07ecdc63fce87`（官方 `odoo:18.0-20250807` base、Odoo deb `18.0.20250807`、addon `18.0.1.0.203`；16804）。這顆綁 addon .203，只適合彩排與「promote 走通」演練；cutover 時以當時 prod 版號重 build（16804）。

## A1 授權與稽核

1. 【人類】Eugene 在本次彩排的 issue 明示核准這一次 prod clone（14057 D14）。
   - 稽核 comment 欄位：核准人、時間、來源＋sha256、目標 ns、預定銷毀時間（14057 D14）。
   - 每次 clone 都要重新核准；不得沿用前一次的核准。

## A2 前置與輸入

1. 【agent】確認前置票都已落地，任一未落地就停：#1201、#1205、#1206、cc/woowprd#8、woow-paas/woow-paas-charts#99（14662 A 段「依賴」補 woowprd#8；#1206 前置見其票面）。
   - charts#99 smoke 結論〔待定：woow-paas/woow-paas-charts#99（R2）〕。
   - #1206 前置與 cc/woowprd#6 的關係〔待定：#1206 留言 14673（R8）〕。
2. 【agent】確認沙盒容量已由 woow-paas/woow-machines-operator#5 放行〔待定：woow-paas/woow-machines-operator#5（R7）〕。
   - 2026-09-30 唯讀量測：Longhorn 排程額度是瓶頸（z0v3w 已用 99.51%，單顆 2-replica 新 volume 最大 70.52 GiB；woow-machines-operator#5 留言 15571）。
3. 【人類】取資料段：產出 #1145 備份組形狀的輸入（dump＋filestore 快照＋manifest；14057 D1、#1145 留言 14055），以叢集內 HTTP PUT push 進沙盒。
   - classifier 擋 agent 做這一步（14057 D8）。
   - ⛔ 不用 `kubectl exec … > file`：超過約 20 MB 會尾端截斷、exit 0，`pg_restore -l` 驗不出來（[pitfall](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md)；#1032 plan:70-71）。
   - 來源可用 live `pg_dump -Fc` 或 `paas-odoo-backup-pvc` 上的每日 dump 包成同形（#1206 交付 1）。
4. 【agent】建沙盒：`/woow-dev:k8s-dev-sandbox create --context woow-k3s`，platform mode、web 與 cron 副本 0、走 #1205 lockdown gate〔待定：cc/woowprd#8（R1）〕。
   - cc/woowprd#8 開工留言（17217）預定介面：`create --mode platform --chart-version <版> --platform-image <ref>@sha256:…`，web／cron 一律 0 起手；新子命令 `scale <name> --component server|cron --replicas N`。以 PR 落地為準。
   - ns 名 `dev-sandbox-<slug>`，`<slug>` 必填、不從 branch 推導（#1206 交付 1 第 0 條防呆）。
5. 【agent】準備 platform profile values（鍵名以 chart 0.2.5 `values.schema.json` 為準）：
   - `profile: platform`、`postgres.database: woow_prod`（R2-Q4）。
   - `conf.emailFrom`：**中和值**，⛔ 不得帶 prod `odooEmail` 現值（R2-Q5、14676、14673）。
   - `postgres.extensions.vector: false`（R2-Q6）。
   - `conf.limitTimeRealCron ≥ 1800`、`cron.maxCronThreads ≥ 3`（woow-paas/woow-paas-charts#100 cutover gate；chart README §values 重點）。
   - 記憶體上限不明設，用 chart 預設 soft 4GiB／hard 5GiB〔待定：woow-paas/woow-paas-charts#99（R9）〕（charts#99 裁決；彩排應用含此預設的 chart 版號）。
   - `replicas: 0`、`cron.replicas: 0`（chart README；woowprd#8 留言 17217）。
   - `image.*`＝A0 的 Step 1 image；`credentials.*` 由沙盒工具隨機產生（woowprd#8 留言 17217）。
   - `backup.enabled: true`（沙盒用自己的 PVC，無 #1145 D7 共寫問題；13786 D7）。

## A3 restore＋中和

> 順序依 R2「彩排順序改寫」（14662 A3、14673）。#1162 不變式：**任何會啟動 Odoo 程序的步驟之後都要再過一次 gate**（14057 答案表「順序」）。一次性 `--stop-after-init` 升級 Job 不在新 chart 路徑上（14662）。

1. 【agent】restore：`seed-from-prod.sh <slug> --from <備份組>`〔待定：#1206（R3）〕。
   - 建庫 locale 取來源 `woow_prod` 實測值 `UTF8 | en_US.UTF-8 | en_US.UTF-8`（PG 17.6，2026-09-27 唯讀；R2-Q4、14676）；不套 chart canonical。
   - 資料段以 `odoo` 帳號 `pg_restore -1 -L <濾 COMMENT - EXTENSION>`；⛔ 不用 `-C`、⛔ 不用 `--no-comments`（14201 D2）。
   - ⚠ platform profile web 副本 0 時 bootstrap 從未跑過 ⇒ 新 postgres 上還沒有 `odoo` 角色（bootstrap ② 才建，chart `files/bootstrap.sh` 檔頭 ②）。restore 前要由 superuser 先建 `odoo` 角色（屬性同 bootstrap：`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`），密碼之後由 bootstrap ② 每次同步。〔待驗：本次彩排實測，回填 #1206 腳本〕
2. 【agent】Stage A 中和＋gate：`neutralize-clone.sh <slug> --db woow_prod`，斷言表全綠才往下〔待定：#1206（R3）〕。
   - 同一段**不**預寫 `upgrade_done`——A4 第 1 步要先刻意驗一次「無 marker」的 fail-loud（14662 A4）。
3. 【agent】記錄差異表（彩排 vs 正式切換；固定段落引 14057 D10 (5)，已依 14676、14662 修訂）：

   | 項目 | 彩排 | 正式切換 | 出處 |
   |---|---|---|---|
   | cron | 全停，只留 `base.autovacuum_job` 當探針 | 照常 | 14057 D3-A |
   | operator | 黑洞 `http://127.0.0.1:9`＋`operator_api_key='neutralized'` | 真 operator | 14057 D4 |
   | `database.secret`／`database.uuid` | 已輪替 | **維持原值** | 14050 ②、14057 D3-C |
   | 使用者密碼 | 叢集內隨機重設 | 不動 | 14057 D3-D |
   | `deploy_env` | `stg` | `prod` | 14057 D3-C |
   | conf `email_from` | 中和值 | CD `paas-odoo/values-prod.yaml` 的 `odooEmail` | R2-Q5、14676 |
   | admin（`res_users` id 2） | 首次開機後 login／password 指紋不變（斷言） | 同左 | R2-Q2、14676 |
   | `upgrade_done` | A4 先驗缺席，再預寫 | 切換前預寫 | R2-Q1 |
   | `vector` extension | 不建（`postgres.extensions.vector: false`） | 同左 | R2-Q6 |

## A4 Step 1 開機行為

> 每一次開機都在 A3 restore 之後；platform bootstrap 只跑 ① 等 DB＋② 冪等 SQL，不建庫、不 `-i base`、不設管理員（R2-Q2；chart README §Pod 啟動順序）。

1. 【agent】**無 marker 開機一次**：web 副本 1、cron 副本 0。
   - 預期：升級閘 initContainer `upgrade-gate` 以 **exit 3** fail-loud，訊息 `adopted DB without upgrade marker: pre-write woow_paas_platform.upgrade_done=…`；不跑 `-u all`、不靜默收養（R2-Q1；chart `files/upgrade-gate.sh` exit 表）。
   - 讀法：`kubectl --context woow-k3s -n dev-sandbox-<slug> logs deploy/<release> -c upgrade-gate`（chart README）。
   - 記錄：exit code、訊息全文、pod 是否停在 `Init:Error`。
2. 【agent】web 副本歸 0，重跑 Stage B 中和＋gate（上一步啟動過 Odoo 程序的 initContainer；#1162 不變式，14662 推導）。
3. 【agent】預寫 marker（屬 Stage A 同段，在 web 起來前完成；14673）：

   ```sql
   -- 在沙盒 postgres 的 woow_prod 執行；digest 取 values image.digest，core_date 取 image 內 dpkg-query
   INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
   VALUES ('woow_paas_platform.upgrade_done',
           '{"digest":"sha256:<64 hex>","core_date":"18.0.20250807"}',
           1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;
   ```

   - JSON 形狀與 upsert 形狀照 chart `files/lib.sh` 的 `marker_json`／`marker_set`（`{"digest","core_date"}`、無空白）。
   - `core_date`：`dpkg-query -W -f='${Version}' odoo` 去 epoch／revision 後取前三段（chart `lib.sh` `core_date`；16804 實值 `18.0.20250807`）。
   - ⛔ 不預寫 `upgrade_attempt`；attempt 缺席視為等於 done，備份不 skip（R2-Q1、14690）。
4. 【agent】**預寫後開機**：web 副本 1、cron 副本 0。
   - 預期：`upgrade-gate` log 出現 `skip: woow_paas_platform.upgrade_done.digest == current image digest`、web Ready（chart `upgrade-gate.sh`）。
   - 驗 admin 指紋不變：`res_users` id 2 的 `login`／`password` sha256 前 12 碼，開機前後相同（R2-Q2）。
   - 驗 `ir_module_module` name→`latest_version` 全表與 restore 當下逐筆相同（＝沒有被 `-u`；woow-paas/paas-app-bundle-cd#144 PR-C 驗收同法）。
5. 【agent】Stage B 再中和＋gate，全綠後 cron 副本 1；cron pod 的 `cron-wait` initContainer 應立刻放行（chart README；14673）。
6. 【agent】記錄方向檢查訊號：把 `upgrade_done.core_date` 暫改成比 image 新的值（例 `18.0.29991231`）後重啟 web，預期 `upgrade-gate` **exit 78**，訊息 `DB core newer than image`；驗完改回原值、再過一次 Stage B gate（chart `upgrade-gate.sh` 步驟 d）。
7. 【agent】把 A4 第 1、4、6 步的實測結果回填 [cutover runbook](platform-odoo-cutover-runbook.md) B3／B6a 與 [回切 runbook](platform-odoo-cutover-switchback-runbook.md) C0。

## A5 全模組測試＋check26

1. 【agent】全模組測試，用 #1032 plan 1.5 的 base/head 法（#1032 plan:48-52）：head＝A0 的 `paas-odoo-official` image，base＝同 addon 版號的 Bitnami `paas-odoo`（16804）。
   - 離開條件：兩次都跑、逐 class 比對名字不比條數，差集為空（#1032 plan:52）。
   - ERROR 判準：零 ERROR；唯一 benign 例外是 `barcode_scanner_account` 在業務碼 import `odoo.tests` 引發的 `Importing test framework` ERROR（#1032 plan:77）。
2. 【agent】check26：項目以 [design doc](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-design.md):101 的檢核點清單、與 #1032 plan 2.6（:83-88）的實跑項目為準。
3. 【agent】image 驗收外的三項（16804「runbook 需要寫進去的點」）：
   - G25：`odoo_core_patch_browser_enhancement` 的 Safari overlay／position 與 OAuth email fallback 人工重驗——截圖交人類判讀。
   - 中文 PDF：抽一份含中文的報表 PDF 檢視字型（舊 release 的 `ensure-cjk-fonts` initContainer 不在任何 repo；woow-paas/paas-app-bundle-cd#144 背景「GitOps 缺口」）。
   - digest 核對：以 Pod `imageID` 對 values digest 做 64 hex 逐字比對（JCR 不給匿名讀，16804）。
4. 【人類】判讀 G25 截圖與中文 PDF，結論寫進彩排報告。

## A6 正向 cutover 演練

1. 【agent】在沙盒內建穩定 Service（名稱比照正式〔待定：woow-paas/paas-app-bundle-cd#143（R4）〕），先用舊形狀指向 Bitnami 形狀的對照 pod，再切新形狀：
   - 新形狀 selector：`app.kubernetes.io/instance: <release>`＋`app.kubernetes.io/component: server`（排除同樣開 8069 的 cron pod，以及 `db-backup`／`db-restore` Job pod；13439 ②、cd#143 陷阱 2）。
   - port 80 與 8072 都以**數字** targetPort `8080` 指向 nginx sidecar（chart README 固定契約）。⚠ 不可用具名 targetPort，否則 0 endpoints 靜默斷線（13439 ②）。
   - 驗：endpoints 非 0、只含 web pod IP；cron pod 與 `db-backup` pod IP 不在其中。
2. 【agent】raw Ingress 路由（`/`、`/websocket`、`/longpolling`）：`/web/health` 200、`/websocket` 101（探針帶 `--http1.1`＋`Origin`，#1032 plan:126）。
   - 預設禁 `--ingress`（14057 D3-C）；本步在沙盒內以 port-forward 或叢集內 curl 驗，不開公網 host。
3. 【agent】三條 NP（traefik／paas-operator／mcp-odoo 跨 ns）對新 pod label 生效（13439 ②）。
   - chart 預設來源：traefik `kube-system`＋`app.kubernetes.io/name=traefik`、operator `app.kubernetes.io/name=paas-operator`、mcp-odoo `woowtech-odoo`＋`mcp-odoo`／`mcp-odoo-admin`（chart `values.yaml` `networkPolicy`）。
   - 沙盒 lockdown 會清空跨 ns 放行（14057 D7）⇒ 跨 ns 兩條只驗「NP 物件 render 正確」，實際生效列入 A9「蓋不到」。
4. 【agent】nginx sidecar 原樣轉傳 `X-Forwarded-Host`／`X-Forwarded-Proto`：以 system-user 登入寫 `web.base.url` 當探針（#1032 plan:88），並驗 OAuth `redirect_uri` 用的是請求 host（#1137 G15）。

## A7 備份與還原

> 對新 release 的任何歸零都**同時** scale `component=server` 與 `component=cron`（R2-Q3；14686）。

1. 【agent】跑一次 nightly 備份：`kubectl --context woow-k3s -n dev-sandbox-<slug> create job --from=cronjob/<release>-backup <name>`（13786 D9）。
   - 驗：產出 `daily/<UTC ISO8601>/` 下 `db.dump`、`filestore/`、`attachments.txt`、`manifest.json`；沒有 `skipped_reason`（chart README §備份格式與驗證）。
2. 【agent】跑一次 D11 升級前備份路徑：平台 release 由 Fleet 管、不經 operator（13439「給其他票」#1128 條）⇒ 以 backup CronJob 的 jobTemplate 自組 Job，注入 `MODE=backup`、`BACKUP_KIND=pre-upgrade`（chart README §operator 參數契約；#1145 留言 14180）。
   - 驗：產出在 `pre-upgrade/`，retention 2 份（chart README）。
3. 【agent】從備份還原一輪，走 #1217 platform 人工段〔待定：#1217（R6）〕；platform `restore.enabled` 預設 false（14201 D11；chart README）。
   - 還原前確認該 release 沒有 Running 的 `component=db-backup` Job（13786 D2）。
   - 歸零時 server 與 cron 一起 scale 0。

## A8 反向搬遷與雙向 selector

1. 【agent 執行・人類逐步放行】在沙盒走一遍 13439 ⑤ 的反向 recipe（細節同 [回切 runbook](platform-odoo-cutover-switchback-runbook.md) C2～C4，以沙盒內的 Bitnami 對照 release 為目標）：
   - 凍結：新 release server 與 cron 同時 scale 0（R2-Q3）。
   - 新 pg `pg_dump -Fc` → Bitnami 側 `pg_restore --no-owner --no-privileges -L <濾 COMMENT - EXTENSION 與 vector>`；⛔ 不用 `--no-comments`（13439 ⑤、14201 D2）。
   - 驗反向 restore **零錯誤**。
2. 【agent】量 Bitnami filestore 目錄的 owner，回填 C3 的 chown 值〔待驗：13439 ⑤ 寫 `999:999`，#1032 plan:112 實際用 `999:0`〕。
3. 【agent】雙向 selector 切換各一次（舊→新、新→舊），每次驗 endpoints 非 0、`/web/health` 200、`/websocket` 101；並演練一次「緊急回切」JSON patch（selector 與兩個 targetPort 一起改；cd#143 PR-1 檔頭程序）。
4. 【agent】量測秒數：dump、restore、filestore push、selector 切換、web Ready，回填 cutover B6／B7 與回切 C3／C4 的停機估算。
   - 現行估算：技術停機 15～20 分（13439 ⑧）；Step 1 切回約 5～8 分、反向搬遷再加約 5 分（#1137 [scout §7 情境一](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/src/branch/research/platform-odoo-chart-gap/docs/scout/2026-09-19-platform-odoo-chart-gap.md)，推論，待本步量測）。

## A9 蓋不到、只能在 prod 切換當下第一次驗

1. 【agent】在彩排報告固定列出以下項目（13439 ⑥），逐項註明「已由他票提前驗掉」或「cutover 當下第一次驗」：
   - traefik 多 host＋cert-manager（woow-paas/paas-app-bundle-cd#143 若先在 prod 用舊 pod 落地，可提前驗掉；cd#143「依賴與順序」推論）。
   - CF tunnel→traefik（同上）。
   - Fleet `valuesFrom` 解析與 Bundle rollout（cd#144 PR-A 驗收）。
   - operator webhook 對新容器（沙盒 operator 是黑洞，14057 D4）。
   - mcp-odoo 不中斷（woow-paas/woow-machines-operator#4）。
   - 跨 ns NP 實際生效（A6 第 3 步）。
   - image 線 G24：彩排用 `Dockerfile.official` 的 `paas-odoo-official`；G26 不阻塞 cutover（13439 ⑥）。

## A10 報告與銷毀

1. 【agent】寫彩排報告，固定段落引 14057 D10 (5)：差異表（A3）、既知限制（clone 當下卡 deploying／upgrading 的服務整場不動，14057 D4）、產物 PII 規則（只輸出 count／聚合、截圖不拍 PII 頁、不附 Gitea、報告後刪本機目錄）、外連證據（引 #1206）〔待定：#1206（R3）〕。
2. 【人類】覆核者親跑斷言 SQL 與兩支 operator log 指令（14057 D10 (4)）。
3. 【agent】銷毀沙盒：`/woow-dev:k8s-dev-sandbox destroy dev-sandbox-<slug> --context woow-k3s`。
   - 證據＝ns、PVC、PV 都已不存在；TTL 只當後備（cc/woowprd#7 實測過靜默失效；14057 D10 (5)）。
4. 【人類】覆核銷毀證據，在 A1 的稽核 comment 回填實際銷毀時間。

## 相關

- [cutover runbook](platform-odoo-cutover-runbook.md)、[回切 runbook](platform-odoo-cutover-switchback-runbook.md)、[teardown runbook](platform-odoo-cutover-teardown-runbook.md)
- [`prod-release-sop.md`](prod-release-sop.md)、[`shared-cluster-paas-scope-guard.md`](shared-cluster-paas-scope-guard.md)
- #1142、#1162、#1206、#1217、woow-paas/woow-paas-charts#81／#99、woow-paas/paas-app-bundle-cd#143／#144、cc/woowprd#8
