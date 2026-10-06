---
title: 平台 Odoo 換 chart：cutover runbook（Step 1）
category: guides
status: active
created: 2026-10-04T08:30:00Z
updated: 2026-10-06T05:30:00Z
related: [docs/guides/deployment/platform-odoo-cutover-rehearsal-runbook.md, docs/guides/deployment/platform-odoo-cutover-switchback-runbook.md, docs/guides/deployment/platform-odoo-cutover-teardown-runbook.md, docs/guides/deployment/prod-release-sop.md]
---

# 平台 Odoo 換 chart：cutover runbook（Step 1）

> 四份同族 runbook：[彩排](platform-odoo-cutover-rehearsal-runbook.md)／**cutover（本檔）**／[回切](platform-odoo-cutover-switchback-runbook.md)／[teardown](platform-odoo-cutover-teardown-runbook.md)。票：odoo-addons/woow_paas_platform#1231；CD 執行面：woow-paas/paas-app-bundle-cd#143（edge）、#144（cutover PR-A～PR-D）。

## 狀態

`status: active`——2026-10-06 定稿：佔位全數落地（#1495），且第一次彩排（2026-10-05，`dev-sandbox-rehearsal-1`，已銷毀）的量測已回填；發起人以合併回填 PR 覆核（#1231 票面定稿條件）。

2026-10-05 定稿輪：已落地的佔位直接改寫進本文（下表「狀態」欄註明出處）；需要發起人裁決的 G1～G7 已於同日裁決（G1～G6 選建議案 A；G7 由 woow-paas/paas-app-bundle-cd#209 取代），題目全文見 #1495 描述。

2026-10-06 **正式 cutover 已執行**（04:17Z T0 → 04:39:08Z PR-D 生效；回退窗口至 2026-10-20T04:39Z）。實跑結果與修正點見文末「cutover 實跑紀錄（2026-10-06）」節，各步驟的對應修正已就地回填（B9 收尾紀錄：#1231 留言 18456、woow-paas/paas-app-bundle-cd#144 留言 18466）。

| # | 佔位 | 狀態 |
|---|---|---|
| C1 | 穩定 Service 名與 raw bundle 目錄 | ✅ `paas-system/paas-odoo-active`（80→8069、8072→8072）＋`paas-odoo-edge/`；cd#143 PR-1～3（#199～#201）已上線，B1～B7 驗收完成（cd#143 留言 18070、18105、18135、18165；WMO#4 留言 18151） |
| C2 | 新 release 名、bundle 目錄、fleet-local secret 名 | ✅ `paas-odoo-platform`（目錄、releaseName 同名）、`paas-odoo-platform-fleet-secrets`（2026-10-05 發起人定案；cd#145 留言 18089） |
| C3 | 正式平台 image＋chart 的實機 smoke 結論 | ✅ PASS：chart 0.2.7 搭 `.203` 與 `.209` 官方 image 各一輪（含 `.209` 升級閘實跑 `-u all` 88 s）；woow-paas/woow-paas-charts#99 留言 18097 |
| C4 | 搬運 helper pod 形狀與指令 | ✅ 附錄 T：沿用 #1206 形狀，不等 #1217（G3） |
| C5 | 窗口容量、snapshot 命名與保留 | ✅ 容量與保留政策：WMO#5 裁決 Q1～Q6（woow-machines-operator#5 留言 18108；Q2 清理已執行，留言 18189）；snapshot 名見 B4（G4） |
| C6 | 關舊 Ingress 是否排進停機窗口 | ✅ 不排停機窗口、避開上班尖峰（2026-10-05 發起人裁決，cd#143 留言 18090）；#200 已上線 |
| C7 | cron 副本 0→1 走哪張 CD PR | ✅ 獨立小 PR「PR-C2」，B6a 第 3 步驗收後、PR-D 前 merge（G1） |
| C8 | 平台發版改 build `paas-odoo-official` 的切換時點與載體 | ✅ cutover 當下（PR-D 生效後立即）；載體＝本 repo 一張 PR 同改 `trigger-odoo-build.yml`、`/woow:deploy`、`prod-release-sop.md`（G2） |
| C9 | chart 版號 | ✅ 定稿時 `woow-odoo` **0.2.7**（0.2.6 起 platform 記憶體預設 soft 4GiB／hard 5GiB；0.2.7 備份 Job 起跑等 PG；charts#99 留言 18097）；正式 cutover 實際用 **0.2.8**（只多 `conf.dbMaxconnGevent`；woow-paas/woow-paas-charts#140，cd#144 留言 18359／18388） |
| C10 | 停機前要停的三支 cron 的 xml id | ✅ `woow_paas_platform.cron_topup_cleanup`、`woow_paas_platform.ir_cron_issue_pending_ecpay_invoices`、`payment.cron_post_process_payment_tx`（#1163 scout §5.1；`src/data/cron_topup.xml:4`、`src/data/ecpay_invoice_cron.xml:4`、Odoo 18 `addons/payment/data/payment_cron.xml:4`） |

## 操作者圖例

| 標記 | 意義 |
|---|---|
| 【人類】 | 發起人或叢集運維親手執行（13439 ⑧、map #1134 2026-09-26 裁決）。⚠ 寫入 `paas-system`、建 Longhorn snapshot、動 fleet-local secret、CD PR merge、`/approve`、離叢集上傳、動 `woowtech-odoo` ns 一律【人類】。 |
| 【agent】 | agent 可自行執行：唯讀 get（含 `longhorn-system`／非白名單 ns 的唯讀，CLAUDE.md §kubectl）、本 repo 與 CD repo 的草稿。 |
| 【agent 執行・人類逐步放行】 | agent 備妥指令，每一步由人類放行（auto-mode classifier 會擋 prod 寫入，13439 ⑧）。 |
| 【agent 開 PR・人類 merge】 | agent 開 PR，merge 是人工 gate；agent 不代 merge。 |

**步驟行慣例**：以「數字＋句點＋空格」開頭的行是步驟行，每一行帶、而且只帶一個上表標記；其餘說明用 `-` 子項，不帶標記。

## 術語

- 引用 [CONTEXT.md](../../../CONTEXT.md)：**平台 Odoo**（:77）、**備份**（:97；Longhorn snapshot 是「還原錨點」，不叫備份，:122、:125）、**prod clone**（:130）、**中和**（:133）、**彩排**（:136）。
- **停機窗口**：本次切換的停機時段，非上班日凌晨 60 分、技術停機 15～20 分（13439 ⑧）。⚠ 不套 CONTEXT「維護窗口」（:118，租戶 22:00～01:00）。
- **回退窗口**：cutover 起固定 14 天（13439 ③）。
- **升級**、**升級前備份**（CONTEXT.md:100、:121）是對 odoo `cloud_service` 定義的；本檔指平台時明寫「平台」。
- **回切**＝反向搬遷回 Bitnami 側；「rollback／回退」保留給 helm revision 回退（CONTEXT.md:125、:128）。

出處縮寫（未標 repo 者為 odoo-addons/woow_paas_platform）：

| 縮寫 | 位置 |
|---|---|
| 13439、14050、14356、14701 | [#1142 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1142#issuecomment-13439) |
| 13476 | [#1163 research 結果](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1163#issuecomment-13476)；scout [`2026-09-22-ecpay-callback-retry-policy.md`](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/src/branch/research/1163-ecpay-callback-retry/docs/scout/2026-09-22-ecpay-callback-retry-policy.md)（未合併分支） |
| 13786、14690 | [#1145 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1145#issuecomment-13786) |
| 14201、14686 | [#1174 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1174#issuecomment-14201) |
| 14363、14698 | [#1194 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1194#issuecomment-14363) |
| 14632（R2-Qn） | [woow-paas/woow-paas-charts#81 Round 2 resolution](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/issues/81#issuecomment-14632) |
| 14662、16804 | [#1231 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1231#issuecomment-14662) |
| 14668、16802 | [woow-paas/paas-app-bundle-cd#144 留言](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/144#issuecomment-14668) |
| WMO#5 裁決 | [woow-paas/woow-machines-operator#5 留言 18108](https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/5#issuecomment-18108)（Q1～Q6）；#1231 pointer 留言 18116 |
| #1137 scout | [`2026-09-19-platform-odoo-chart-gap.md`](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/src/branch/research/platform-odoo-chart-gap/docs/scout/2026-09-19-platform-odoo-chart-gap.md)（未合併分支） |
| chart README | woow-paas/woow-paas-charts `main` 的 `charts/woow-odoo/README.md`（0.2.7） |
| #1032 plan | [`2026-09-03-woowtech-odoo-paas-migration-implementation.md`](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md) |

## 已落地、直接用的實值

| 項目 | 值 | 出處 |
|---|---|---|
| chart | `woow-odoo` **0.2.7**；merge `main` 即同時推 Gitea helm registry（Fleet 用）與 JCR OCI | woow-paas/woow-paas-charts#99 留言 18097；chart README §發佈 |
| platform cutover gate | `conf.limitTimeRealCron ≥ 1800`、`cron.maxCronThreads ≥ 3`（schema fail-loud）；連線池 `conf.dbMaxconn ≥ 2×maxCronThreads＋餘裕`（＝至少 8，見 B5 第 4 步） | woow-paas/woow-paas-charts#100；chart README；2026-10-06 cutover 實測 |
| `addons_path`（platform） | core、`/opt/woow/addons`、`/mnt/extra-addons` | woow-paas/woow-paas-charts#120；chart README 固定契約 |
| Service／port | port 80 與 8072 都以數字 targetPort **8080** 指向 nginx sidecar | chart README 固定契約 |
| pod label | `app.kubernetes.io/name=odoo`、`instance=<release>`、`component=server｜cron｜postgresql｜db-backup｜db-restore` | chart README 固定契約 |
| uid | odoo 100／gid 101（PVC `fsGroup 101`）；postgres 999 | chart README 固定契約；16804 |
| 副本鍵 | web `replicas`、cron `cron.replicas` | chart README §values 重點；cc/woowprd#8 留言 17217 |
| marker | `woow_paas_platform.upgrade_done`＝`{"digest":"sha256:…","core_date":"18.0.YYYYMMDD"}` | chart README 固定契約；chart `files/lib.sh` |
| 平台 image 線 | `paas-odoo-official`（`Dockerfile.official`，官方 `odoo:18.0-20250807` base，Odoo deb `18.0.20250807`）；最新一顆 `20261005.1128@sha256:76debd4b2176fcfc039628b57569fa7d3ee99e9eb19208910ae49cd218cb6c73`（addon **18.0.1.0.209**）；cutover 當天 prod 若已升版，照 B5 第 1～3 步以當時版號重 build | woow-paas/paas-odoo-ci#145 留言 18075；16804 |
| 回退窗口 build 規則 | **不雙 build**；舊 Bitnami release 只動 `replicas`、image 凍結在 cutover 前那顆；回切時維持該 image（2026-10-04 發起人裁決，維持 13439 ③；#1392 已改題為「平台換 chart 後發版改 build `paas-odoo-official`」） | 13439 ③；#1392 |
| platform 記憶體預設 | `limit_memory_soft` 4GiB／`limit_memory_hard` 5GiB，由 chart 預設提供（0.2.6 起）、values 不明設 | woow-paas/woow-paas-charts#99 |
| 新 release PVC | data 10Gi、pg 10Gi，`storageClass: longhorn`（Retain）；必須覆寫 chart 預設的 5/8Gi `longhorn-delete`；`paas-odoo-backup-pvc` cutover 前擴到 10Gi（併入 PR-A） | WMO#5 裁決 Q1、Q6；cd#144 留言 18127 |
| 升級閘與備份 | 升級閘跑 `-u all` 前**不會**自動做 pre-upgrade 備份 ⇒ 每次會換新 release image digest 的發版，都要先由人做 snapshot＋on-demand 備份（B8 第 7 步） | charts#99 留言 18097；#1231 留言 18116 |
| Longhorn 排程（2026-10-05 16:00Z 重量） | 31zix 71%、59mqj 97%（剩 35 GiB）、pggrg 86%、z0v3w 76%；新 release 40 GiB 額度放 31zix／z0v3w／pggrg 帳面寬裕 | woow-machines-operator#5 留言 18189 |
| build 快取 | 依賴層先於 addon；只改 addon 時 pip 層 CACHED | woow-paas/paas-odoo-ci#228 |
| `woow_prod` locale | `UTF8 | en_US.UTF-8 | en_US.UTF-8`、PG 17.6（2026-09-27 唯讀） | R2-Q4；#1162 留言 14676 |

## 時序總覽

| 時點 | 章節 | 內容 |
|---|---|---|
| 窗口前數週 | B0、B2 | 進入條件；edge bundle 與消費者改指（2026-10-05 已完成） |
| 窗口前數日 | B5 | 新 release 並存（web／cron 0）、promote 走通 |
| 窗口前 ≥3 日 | B1 | 內部公告 |
| 停機窗口 | B6～B7 | 機器時間約 5～6 分：凍結舊側約 40～90 s（odoo 優雅終止）＋取資料約 80 s＋restore 約 60～90 s＋web Ready 約 30～60 s＋selector < 1 s（[彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)「彩排紀錄」）。⚠ **服務中斷要另加**每張窗口內 PR（#214／#215／#216／#217 共 4 張）merge 後等 Fleet 同步約 1 分，以及人工逐步放行＋唯讀驗收：2026-10-06 實測 **約 21 分**（T0 → PR-D 生效）。排窗口與公告以 25 分估 |
| 停機窗口 T0 | B6 第 1～3 步 | ECPay 前置、凍結舊側 |
| 凍結後 | B4 | Longhorn snapshot 兩顆 |
| 凍結後 | B6 第 4 步起、B6a | 正向搬遷、預寫 marker、起新 odoo |
| 切流量 | B7 | selector 切新 pod、驗收、ECPay 收尾 |
| T0＋14 天 | B8 | 回退窗口 |

## B0 進入條件 gate（全部滿足才排停機窗口）

1. 【agent】確認 13439 ④ 兩條觸發條件成立，結果寫進本次 cutover issue：
   - 操作覆蓋：任一 prod odoo `cloud_service` 在新 chart（tenant profile）上，用平台 UI／API 主動走過 #1154 十步生命週期；#1152、#1156～#1159 已修或確認不影響上線版本（逐張列狀態；woow-paas/paas-app-bundle-cd#144「blocked_by 的讀法」）。
   - 時間覆蓋：之後連續正常 ≥ 7 天。
2. 【人類】確認彩排報告已由覆核者親跑斷言 SQL 與 operator log 指令（[彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md) A10；#1162 留言 14057 D10 (4)）。
3. 【agent】確認 chart **0.2.7** 在 Gitea helm registry 可取得（13439 ②；woow-paas/paas-app-bundle-cd#144 PR-A 行內註解記實測 HTTP 200）。
   - smoke 已通過：0.2.7＋`.203`／`.209` 各一輪（woow-paas/woow-paas-charts#99 留言 18097）。cutover 若改用更新的 chart，要先補一輪同樣的 smoke。
4. 【agent】叢集內產出 `bitnami_odoo`（88 MB，13439 ⑦）最終 dump，附 sha256 與 `pg_restore -f /dev/null` 結果。
   - 只能叢集內 HTTP PUT push，⛔ 不用 `kubectl exec … > file`（[pitfall](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md)）。
   - `bitnami_odoo` 不搬進新 postgres（13439 ⑦）。
5. 【人類】把上一步的 dump `age` 加密，上傳公司 Google Drive 專用資料夾，回填 sha256；保留到回退窗口結束後再 90 天（map #1134 2026-09-26 裁決；13439 ⑦）。
6. 【agent】確認「promote 走通」證明已完成（B5 第 4 步；13439 ③）。
7. 【人類】請 woow-machines-operator 在 cutover 前**重量一次** `nodes.longhorn.io`，在 woow-machines-operator#5 留第二版容量結論「夠」（WMO#5 裁決 Q2；longhorn-system 不在本 repo 白名單）。
   - 基準：2026-10-05 16:00Z 59mqj 剩 35 GiB、其餘三台合計剩約 875 GiB；新 release 額度 40 GiB（Q1）＋backup PVC 擴容 10 GiB（Q6）（woow-machines-operator#5 留言 18189）。
8. 【agent】確認 woow-paas/woow-machines-operator#4 回報 mcp-odoo ConfigMap（`mcp-admin-config`、`mcp-admin-init-config`）已改指穩定 Service（13439 ⑧；cd#143「交接與盤點」）。
9. 【agent】確認 woow-paas/paas-app-bundle-cd#145（SOP／`promote-acceptance-checklist.md` 參數化）已落地——窗口內仍可能發 hotfix（13439 ③）。
10. 【agent】確認平台發版改 build `paas-odoo-official` 的切換已備妥（2026-10-05 發起人裁決 G2，#1495）：`.gitea/workflows/trigger-odoo-build.yml`、`/woow:deploy` 與 `prod-release-sop.md` 的修改 PR 已開好、等 B8 第 2 步 merge（#1392 留言 17382）。
11. 【agent】確認 Step 1 平台 image 是以「停機窗口當下的 prod 版號」build 的：image 內 addon 版號＝prod 現行 `src/__manifest__.py` 版號（16804「cutover」段；ci#145 留言 18075 §1.3）；不是就照 B5 第 1～3 步重 build、重 pin。
12. 【agent】唯讀確認舊 Bitnami 兩顆 PVC 的保護仍在（WMO#16）：
   - `paas-odoo`（filestore，helm release 管）：帶 `helm.sh/resource-policy: keep`——由 values `persistence.resourcePolicy: keep` 寫進 release manifest（woow-paas/paas-app-bundle-cd#209，2026-10-05 merge 並驗收）。helm 的 keep 只讀 release 內存的 manifest，`kubectl annotate` 擋不住 uninstall，所以必須走 values（helm v3.13.3 `pkg/action/uninstall.go:204-214`）。
   - `data-paas-odoo-postgresql-0`：STS 的 volumeClaimTemplate PVC，不在 release manifest，helm uninstall 不會刪；STS retention `whenDeleted: Retain`。不另加 annotation（2026-10-05 發起人決定）。
   - 兩顆 PV 都是 Retain／Bound。⚠ keep 擋不住 `kubectl delete pvc`、刪 ns，也擋不住 WMO#16 追查中的不明批次刪除；那些屬 WMO#16 範疇。
13. 【agent】確認舊 `paas-odoo` 兩顆 PVC 在本次 cutover 期間沒有任何 Fleet／helm 變更計畫會動到它們（cd#145 守門腳本 R1～R5：同目錄一律不准改 releaseName；woow-paas/paas-app-bundle-cd#202）。

## B1 窗口與公告

1. 【人類】排停機窗口：非上班日凌晨 60 分（服務中斷以 25 分估：2026-10-06 實測約 21 分，見時序總覽），內部公告 ERP 使用者停機時段與「切換後須重新登入」；官網不另公告；sessions 不搬（13439 ⑧）。
   - 2026-10-06 實測：Odoo 用 filesystem session store（`data_dir/sessions`），附錄 T 只搬 `filestore/<db>` ⇒ 切換後**全員**（內部使用者、租戶、`/woow` 使用者）既有 session 失效、須重新登入。公告要寫明；B7 第 3 步抽查前先重新登入。
2. 【人類】依 #1163 以「停機 ≥ 15 分即視為會丟 ECPay 回呼」規劃金流段，指定 B6／B7 的 ECPay 前置與收尾負責人（13476 第 2、9 點）。

## B2 一次性前置變更（✅ 2026-10-05 已全部完成）

> 本節保留為紀錄與回切時的對照；cutover 時只需在 B0 確認狀態沒有漂移。

1. 【agent 開 PR・人類 merge】`paas-odoo-edge/` raw bundle：穩定 Service `paas-system/paas-odoo-active` 先指舊 pod（woow-paas/paas-app-bundle-cd#199，cd#143 PR-1；B1～B3 驗收留言 18070）。
   - 舊形狀：selector 逐字照抄 live `svc/paas-odoo`：`app.kubernetes.io/instance: paas-odoo`＋`app.kubernetes.io/name: odoo`，⛔ 不加 `component`；ports 80→8069、8072→8072，targetPort 寫數字（cd#143 設計確認，2026-10-05 live 實查）。
   - raw Ingress：4 host（`paas.woowtech.io`、`aiot.woowtech.io`、`www.designsmart.com.tw`、`woowtech-ds.woowtech.io`）、4 個 TLS secretName 與 live 逐字相同、`cert-manager.io/cluster-issuer: letsencrypt-prod`。緊急回切 JSON patch 在 `paas-odoo-edge/fleet.yaml` 檔頭。
   - ⚠ Fleet 對 bundle 內容跑 Go template，連 YAML 註解裡的連續大括號都吃（cd#143 現況段）。
2. 【agent 開 PR・人類 merge】舊 release 的 `ingress.enabled` 改 `false`（woow-paas/paas-app-bundle-cd#200，cd#143 PR-2；B3／B4／B5 驗收留言 18105）。
   - 不排停機窗口、避開上班尖峰（2026-10-05 發起人裁決，cd#143 留言 18090）。
   - 舊 Ingress 被刪後，4 個 Certificate 隨 ownerReference 被 GC、ingress-shim 未重建 ⇒ 由 woow-paas/paas-app-bundle-cd#206 加 annotation 觸發重建；Certificate 改歸 `Ingress/paas-odoo-edge`、未重簽、續期已恢復排程（留言 18135）。
   - 既有缺口：`www.designsmart.com.tw` 的 Certificate 自 2026-09-04 起 `Ready=False`（ClusterIssuer solver 不涵蓋該網域），站台經 CF 正常；屬叢集級，另案處理。
3. 【agent 開 PR・人類 merge】`paas-operator/values-prod.yaml` 的 `odooBaseUrl` 改指 `http://paas-odoo-active.paas-system.svc.cluster.local`（woow-paas/paas-app-bundle-cd#201，cd#143 PR-3；B6 驗收留言 18165）。
   - webhook 實流量待首次 prod agent-runtime spawn 補驗。
4. 【agent】prod 存活 agent-runtime pod 的 `ODOO_BASE_URL` 盤點：22 個 `paas-ws-*` ns，0 顆帶該 env（cd#143 設計確認）。B0 前再盤點一次。
5. 【agent】從 odoo access log 列出叢集內實際來源（`NetworkPolicy/paas-odoo` 規則 ① 沒有 `from`；13439 checklist 補充項）。
   - 2026-10-05 只有約 7.5 小時 log，視窗內只見 kubelet 與 CF connector→traefik，**unverifiable**（cd#143 設計確認）⇒ cutover 前要用更長視窗重跑。
6. 【人類】woow-machines-operator 改指 mcp-odoo（woow-machines-operator#4，2026-10-05 15:33:59Z 完成；留言 18151、cd#143 B7 留言 18154）。
   - 改了 cm `mcp-admin-config`（2 處）、`mcp-admin-init-config`（1 處）與 Deployment `mcp-odoo` 的 env `ODOO_URL`；helm values `.odoo.url` 已漂移，日後 `helm upgrade` 要帶 `--set odoo.url=…paas-odoo-active…`（WMO#4 留言 18151）。
   - cloudflared 舊 tunnel origin、n8n 是否直連舊 Service，待人工確認、不擋 cutover（woow-machines-operator#15）。

## B3 人工前置資料

1. 【人類】建新 bundle 的 fleet-local secret `fleet-local/paas-odoo-platform-fleet-secrets`（cd#145 留言 18089），名稱必須有 `paas-*` 前綴（CLAUDE.md §kubectl `fleet-local` 只按資源名前綴授權）。
   - 內容：`values.yaml` 鍵下的 `credentials.postgresPassword`、`credentials.odooPassword`、`credentials.adminPasswd`（platform 三個必填，chart README §values 重點）。
   - agent 只用 key 名與 sha256 指紋驗，不印值（woow-paas/paas-app-bundle-cd#144 背景「secret 紀律」）。
2. 【agent】`conf.emailFrom` 取現行 CD `paas-odoo/values-prod.yaml` 的 `odooEmail` 值帶入新 values；不加 `from_filter`；不設 `smtp.*`（R2-Q5；14662 B3；平台 SMTP 走 DB `ir.mail_server`）。
3. 【agent】記錄切換前指紋（只印 sha256 前 12 碼，先排除哨兵值 `e3b0c44298fc`／`74234e98afe7`；cd#144 背景「secret 紀律」）：`ir_config_parameter` 的 `database.secret`、`database.uuid`，`res_users` id 2 的 `login`、`password`，以及 `ir_module_module` name→`latest_version` 全表的 sha256。
   - `database.secret`／`database.uuid` 正式切換**必須維持原值**，否則 prod 簽出的 portal／取消訂閱／CSRF 連結全部失效（14050 ②）。
   - bootstrap 完成標記不需處置：platform 沒有 bootstrap ⑤（R2-Q2；14701）。
   - 升級閘 marker 在 B6 第 8 步預寫（R2-Q1）。

## B4 還原錨點

> 時點：B6 第 2 步凍結舊側之後、第 4 步 dump 之前（舊 odoo 已 0，DB 靜止；推論）。命名沿 `prod-release-sop.md` §5.1.1。

1. 【人類】發起人（eugene）親跑、叢集運維備援（WMO#5 裁決 Q3）：建 Longhorn snapshot 兩顆，舊 pg PVC `data-paas-odoo-postgresql-0`、舊 filestore PVC `paas-odoo`。
   - 名稱 `snap-paas-odoo-{pg,fs}-precutover-{YYYYMMDD}`（2026-10-05 發起人裁決 G4，#1495）；`prod-release-sop.md` §5.1.1 的 `pre{版號}` 在本次版號不變時無法區分。之後新 release 的發版前 snapshot 用 `snap-paas-odoo-platform-{pg,fs}-pre{版號}-{YYYYMMDD}`（B8 第 7 步）。
   - 保留到回退窗口結束、且退役 dump 已在叢集外驗過 sha256（WMO#5 裁決 Q4）；舊 volume 上既有的歷史 snapshot 不另清，隨 teardown 消失。
2. 【agent】唯讀驗兩顆都 `readyToUse=true` 且 `markRemoved=false`；任一不符＝沒有還原錨點 ⇒ STOP（`prod-release-sop.md` §5.1.1）。

## B5 新 release 並存部署（窗口前數日，不停機）

1. 【agent 執行・人類逐步放行】若 B0 第 11 步不符：dispatch paas-odoo-ci `build-image.yml`，帶 `image_name=paas-odoo-official`、`addon_ref=<prod 現行 tag>`（16804「cutover」段）。
   - ⚠ `addon_ref` 必填，漏填會從移動中的 `develop` HEAD 烤 image（`prod-release-sop.md` §9 坑 12）。
   - 只改 addon 時 pip 層走快取（woow-paas/paas-odoo-ci#228）。
2. 【人類】授權者本人在 approval issue 留 `/approve`；agent 不代簽（CLAUDE.md §Release；16804）。
3. 【agent】記下新 digest，部署後以 Pod `imageID` 做 64 hex 逐字比對（JCR 不給匿名讀，16804）。
4. 【agent 開 PR・人類 merge】PR-A：新 bundle 目錄 `paas-odoo-platform/`、releaseName `paas-odoo-platform`（cd#145 留言 18089），release 名 ≠ `paas-odoo`（原地換 chart 會刪 Helm 管的 PVC 與 Secret，#1137 G30）。PR 描述必須貼 `scripts/ci/check-fleet-chart-coordinates.py` 手跑輸出（woow-paas/paas-app-bundle-cd#202；sop-cd §2、§5；CI 化另案 cd#205）。values 要點：
   - `profile: platform`、`postgres.database: woow_prod`、`conf.emailFrom`（B3 第 2 步）、`postgres.extensions.vector: false`（R2-Q6）、`conf.limitTimeRealCron ≥ 1800`、`cron.maxCronThreads ≥ 3`。
   - ⚠ 連線池：platform 的 cron 是**單一 threaded 程序**，`ir_cron` 每條執行緒同時要「取 job」與「跑 job」兩條連線，另有 dispatcher 監聽 ⇒ `conf.dbMaxconn ≥ 2×maxCronThreads＋餘裕`。chart schema 對 platform 的 `maxCronThreads` 下限是 3，無法改小 ⇒ `dbMaxconn` 至少 **8**。2026-10-06 實測：`dbMaxconn: 6` 時 cron 04:36～04:38Z 出現 3 起 `PoolError: The Connection Pool Is Full`，woow-paas/paas-app-bundle-cd#219 改 8 後歸 0（代價：web 一起重啟約 60 s）。`dbMaxconn` 同時是 web 每個 worker 的池上限，改大要重算最壞總連線數（web workers×D＋gevent G＋cron D，< PG `max_connections` 100；cd#144 留言 18359）。建議 chart 另加 cron 專用 `db_maxconn`（另案）。
   - `replicas: 0`、`cron.replicas: 0`、postgres 1：restore 前起 odoo 會停在 `Init:Error`（bootstrap exit 3 `platform DB must be restored before first boot`；R2-Q2、14668）。
   - `image.*`＝Step 1 digest；`postgres.image.*`＝chart 預設 paas-postgres（PG 17.11、pgvector 0.8.6、rsync；chart README）；`imagePullSecrets: [jfrog-registry]`（cd#144 PR-A）。
   - `ingress.enabled: false`（Ingress 由 raw bundle 管，13439 ②）。
   - chart `woow-odoo` 0.2.7；記憶體上限（odoo.conf `limit_memory_soft`／`limit_memory_hard`）**不明設**，用 chart 預設 soft 4GiB／hard 5GiB（woow-paas/woow-paas-charts#99）。
   - `backup.enabled: false`（本階段）、`backup.persistence.existingClaim: paas-odoo-backup-pvc`、`restore.enabled: false`（13786 D7；14201 D11）。
   - PVC：data 10Gi、pg 10Gi，`storageClass: longhorn`（Retain），覆寫 chart 預設 5/8Gi `longhorn-delete`；一律明寫 `storageClass`（叢集有兩個 default SC；chart README；WMO#5 裁決 Q1）。
   - 同一張 PR 把 `paas-odoo-backup/backup-pvc.yaml` 的 `paas-odoo-backup-pvc` 從 5Gi 擴到 10Gi（只能擴不能縮；WMO#5 裁決 Q6；cd#144 留言 18127）。
5. 【agent】驗 PR-A：新 Bundle `Ready=True`；新 postgres Running；web／cron pod 0；舊 `paas-odoo` 沒有新 helm revision、pod UID 與 restartCount 不變；新舊物件名不相交；`valuesFrom` 已解析（Helm 層 Secret 指紋＝fleet-local 欄位指紋）（cd#144 PR-A 驗收）。
6. 【agent 開 PR・人類 merge】「promote 走通」證明：對新 bundle 開一張 image pin 變更 PR，最後停在 Step 1 目標 digest，照 woow-paas/paas-app-bundle-cd#145 參數化後的程序驗收（13439 ③；cd#144 PR-A）。
7. 【agent】驗三條 NP 物件（traefik／paas-operator／mcp-odoo）與放行 `app.kubernetes.io/instance=<release>` 的 pg NP 都已 render；生效驗證在 B7（13439 ②；cd#144 PR-A 驗收）。

## B6 凍結＋正向搬遷（停機窗口內）

1. 【agent 執行・人類逐步放行】ECPay 前置（#1163 scout §5.1 T-1）：停用三支 cron `woow_paas_platform.cron_topup_cleanup`、`woow_paas_platform.ir_cron_issue_pending_ecpay_invoices`、`payment.cron_post_process_payment_tx`；等發票 cron 當下那一趟跑完（沒有 `issuing` 殘留）；記下 pending tx／topup／發票三張清單與 T0（13476 第 9 點）。
   - ⛔ 不 disable payment provider，會 archive token（13476 第 9 點）。
   - 先記下三支 cron 的**原始** `active`，收尾時照原值恢復、不一律開啟（2026-10-06 prod 實查：三支原值都是 `false`，`write_date` 停在 2026-09-04；cd#144 留言 18414）。
   - 清單 SQL 若第一欄放常數標籤，`GROUP BY`／`ORDER BY` 的位置號要指實際欄位（2026-10-06 實撞：`GROUP BY 1` 指到常數欄而報錯，改 `GROUP BY 2` 補跑）：

     ```sql
     SELECT 'invoice-state', woow_invoice_issue_state, count(*) FROM account_move
      WHERE woow_invoice_issue_state IN ('pending','issuing','failed') GROUP BY 2 ORDER BY 2;
     ```

   - `payment_transaction.provider_code` 是 related 欄位（不一定 stored），篩 ECPay 一律 `JOIN payment_provider p ON p.id = t.provider_id WHERE p.code = 'ECPay'`。
2. 【agent 開 PR・人類 merge】PR-B：`paas-odoo/values-prod.yaml` **只加** `replicaCount: 0`（merge 前用 `helm show values` 實拉確認 Bitnami 28.2.10 的鍵路徑）。這是窗口內唯一一次對舊 release 的 upgrade（13439 ③；cd#144 PR-B）。
3. 【agent】驗：`deploy/paas-odoo` replicas 0、pod 已消失（彩排實測 odoo pod 優雅終止約 90 s）；`paas-odoo-postgresql-0` pod UID 與 restartCount 不變；兩顆舊 PVC 仍 Bound（cd#144 PR-B 驗收）。接著做 B4。
4. 【agent 執行・人類逐步放行】建附錄 T 的 T1～T3，跑 T4 來源 Job：舊 pg（`paas-odoo-postgresql`，`bn_odoo`）的 `woow_prod` `pg_dump -Fc`＋manifest＋attachments、舊 PVC `paas-odoo` 的 `/bitnami/odoo/data/filestore/woow_prod` 打 tar，HTTP PUT 到 receiver；每個 PUT 回應的 sha256 與來源端相符（2026-10-05 發起人裁決 G3，#1495）。
   - ⛔ 不用 `kubectl exec … > file`，也不用 exec 串 exec 的 stdout 管線（同一個截斷面；pitfall doc）。
   - 舊 pg 的 NP 放行 `name=odoo`＋`instance=paas-odoo`，以及 label `paas-odoo-postgresql-client: "true"`（`pg-backup` 同款；2026-10-05 彩排實查）⇒ T4 帶後者即可連舊 pg，不必新增 NP 規則；⛔ 來源 Job 不可帶 `instance=paas-odoo`＋`name=odoo`（會被 `paas-odoo-active` 舊形狀 selector 選進去）。
   - 舊 odoo 已 0 副本，舊 PVC `paas-odoo`（RWO）可由 T4 唯讀掛載。
5. 【agent 執行・人類逐步放行】跑附錄 T 的 T5 restore Job，涵蓋本步與第 6、9 步。它以 superuser 建角色、建庫、預建 extension（14201 D2；R2-Q4、R2-Q6），等價 SQL：

   ```sql
   -- 角色：屬性同 chart bootstrap ②；密碼由 helper pod 以 secretKeyRef 讀 <release>-credentials 的 odoo-password，經 psql \getenv 代入（不進 argv）
   CREATE ROLE odoo LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;   -- 已存在則略過
   ALTER ROLE odoo WITH LOGIN PASSWORD :'pw';
   -- 建庫：locale 取來源 woow_prod 的 pg_database.datcollate／datctype（停機當下再量一次；2026-09-27 實測 en_US.UTF-8）
   CREATE DATABASE woow_prod ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' TEMPLATE template0 OWNER odoo;
   \c woow_prod
   CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- Step 1 不建 vector
   ```

   - platform web 副本 0 時 bootstrap 從未跑過，新 pg 上沒有 `odoo` 角色（chart `files/bootstrap.sh` 檔頭 ②）；之後 bootstrap ② 每次開機會同步密碼。
   - ⛔ 不套 chart canonical（`LC_COLLATE 'C'`）：既有 DB 搬遷一律取來源值（R2-Q4）。
6. 【agent】讀 T5 的 log 確認資料段：以 `odoo` 帳號 `pg_restore -1 -d woow_prod -L <list>`，`<list>` 由 `pg_restore -l` 濾掉 `COMMENT - EXTENSION`、`EXTENSION`，以及 `ALTER SCHEMA public OWNER` 條目（`woow_prod` 的 `public` owner 若不是 `pg_database_owner`，dump 會多出這條；chart invariant 永不 `ALTER SCHEMA public OWNER`）（14201 D2 與「給其他票」）。
   - ⛔ 不用 `-C`；⛔ 不用 `--no-comments`（Odoo 18 `_sql_constraints` 存在 `COMMENT ON CONSTRAINT`；14201 D2）。
7. 【agent】唯讀驗：`database.secret`／`database.uuid`、admin login／password、`ir_module_module` 全表的指紋都與 B3 第 3 步相同；`woow_prod` 內沒有 `vector` extension、沒有 vector 型別欄位（14050；R2-Q6；14668）。
8. 【agent 執行・人類逐步放行】預寫升級閘 marker（R2-Q1；14662 B3）：

   ```sql
   INSERT INTO ir_config_parameter (key, value, create_uid, create_date, write_uid, write_date)
   VALUES ('woow_paas_platform.upgrade_done',
           '{"digest":"sha256:<Step 1 image digest，64 hex>","core_date":"18.0.20250807"}',
           1, (now() AT TIME ZONE 'UTC'), 1, (now() AT TIME ZONE 'UTC'))
   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, write_uid = EXCLUDED.write_uid, write_date = EXCLUDED.write_date;
   ```

   - digest＝PR-A／promote 證明後最終 pin 的 `image.digest`；`core_date`＝該 image 內 `dpkg-query -W -f='${Version}' odoo` 去 epoch／revision 後前三段（chart `files/lib.sh` `core_date`；16804 實值 `18.0.20250807`）。
   - ⛔ 不預寫 `upgrade_attempt`；attempt 缺席視為等於 done，PR-C 的 on-demand 備份不會 `skipped_reason=partial_upgrade`（R2-Q1、14690）。
   - 不預寫 ⇒ 新 odoo 第一次開機升級閘 exit 3 fail-loud（chart `upgrade-gate.sh`）；預寫錯 digest ⇒ 自動 `-u all`，打掉 Step 1 的「不 -u」（13439 ①、14363 D2(3)）。
9. 【agent】讀 T5 的 log 確認 filestore 段：全量 push（不用增量 rsync），舊 PVC `paas-odoo` 上的 `/bitnami/odoo/data/filestore/woow_prod`（Bitnami data_dir；#1032 plan:112 的 2026-09-04 prod 實作與 `docs/scout/2026-09-18-odoo-bitnami-coupling-inventory.md:194`）→ 新 PVC `paas-odoo-platform-data` 的 `/var/lib/odoo/filestore/woow_prod`（chart README 固定契約）。
   - 校驗：檔數＋`find -type f -printf '%s\n'` 位元組總和，兩端相等（不用 `du -sb`；#1137 §4.2）。參考量：7,194 檔、1,963,901,297 bytes（2026-09-04；#1032 plan:111，引自 cd#144 PR-C）。
   - 校驗通過後 `chown -R 100:101`（chart README uid／fsGroup）。

## B6a 起新 odoo（位置：B6 filestore 校驗後、B7 selector 切換前）

> 順序依 R2-Q3 與「彩排順序改寫」（14662 B 段新增步）：web 1、cron 0 → 升級閘 marker 相等跳過、web Ready → 才 cron 1。cron pod 的 `cron-wait` initContainer 是第二道保險，不取代順序。

1. 【agent】merge 前 gate：唯讀確認新 pg 上 `upgrade_done` 的 digest 等於 values `image.digest`，且該 release 沒有 Running 的 `component=db-backup` Job（cd#144 PR-C merge 前 gate；13786 D2）。
2. 【agent 開 PR・人類 merge】PR-C（13786 D7 同一張 PR）：
   - `replicas: 1`、`cron.replicas: 0`、`backup.enabled: true`（retention／timeZone／schedule 用 chart 預設：7 daily＋4 weekly、Asia/Taipei、02:00＋adler32 偏移；13786 D4）。
   - 刪除 `paas-odoo-backup/pg-backup-cronjob.yaml`；⛔ 保留 `backup-pvc.yaml` 與 `fleet.yaml`（刪整個目錄會連 `paas-odoo-backup-pvc` 一起下架；cd#144 PR-C）。PR 描述記下被刪檔的最後 commit sha，回切要重新 apply（13786 D7）。
3. 【agent】驗：`upgrade-gate` log 出現 `skip: woow_paas_platform.upgrade_done.digest == current image digest`、web Ready；`ir_module_module` 全表 sha256 與凍結當下相同（＝沒有 `-u all`）；admin、`database.secret`／`uuid` 指紋不變；中文 PDF 抽查交人類（cd#144 PR-C 驗收）。
   - 2026-10-06 觀察（字形）：舊 Bitnami pod 另掛 live `configmap/paas-odoo-fontconfig` 的 `98-woow-cjk-tc.conf`（不在任何 repo），把 `sans-serif`／`serif`／`monospace`／`Open Sans` accept 到 `Noto … CJK TC`。官方 image 只有發行版 `70-fonts-noto-cjk.conf`，`fc-match "Open Sans"` → DejaVu Sans；wkhtmltopdf 不看 `lang` 選 face，CJK fallback 落到 Noto Sans CJK OTC 的 index 0（JP）⇒ PDF 嵌入 `NotoSansCJKjp`，骨／直／令／次等字印成日文字形。修正走 chart 或 image 補同內容的 fontconfig（另案）；抽查時看 PDF 內嵌字型名，是 `NotoSansCJKtc` 才算過。
4. 【agent 開 PR・人類 merge】PR-C2：只改 `cron.replicas: 1`，第 3 步驗收後、PR-D 前 merge（2026-10-05 發起人裁決 G1，#1495）；cron pod 的 `cron-wait` 應放行（chart README）。PR-D 因此維持「只改 selector」，回切對照單純。
   - ⚠ 新 pod 開頭約 1～2 秒 NetworkPolicy 尚未生效，`cron-wait` 第一次輪詢常連不到 DB、約 10 秒後放行，屬預期（woowprd#13 實測；#1206 證據）。
5. 【agent】cron pod 起來後照 #1162 不變式再過一次檢查：只有預期的 cron 是 active、`database.is_neutralized` 不存在（prod 不得出現中和標記）、`ir_module_module` 全表 sha256 不變。
6. 【agent 執行・人類逐步放行】on-demand 備份一次：`kubectl --context woow-k3s -n paas-system create job --from=cronjob/<release>-backup <name>`；Job Complete、產出 `db.dump`＋`filestore/`＋`manifest.json`、沒有 `skipped_reason`；PVC 根目錄舊 `*.pg` 仍在；任何時點都沒有兩支指向 `paas-odoo-backup-pvc` 的 CronJob（cd#144 PR-C 驗收）。

## B7 切流量

1. 【agent 開 PR・人類 merge】PR-D：穩定 Service `paas-odoo-edge/service.yaml`（`paas-system/paas-odoo-active`）改新形狀（cd#143 設計確認：只改這一檔）。
   - selector：`app.kubernetes.io/instance: paas-odoo-platform`＋`app.kubernetes.io/component: server`（改鍵集合，不只換值；排除 cron 與 `db-backup`／`db-restore` pod——該 chart 所有 pod 都帶 `name=odoo`，沙盒實測不帶 component 會選到 3 顆；13439 ②、cd#143 設計確認）。
   - port 80 與 8072 的 targetPort 都改成數字 `8080`（chart README）。⚠ 不可用具名 targetPort（13439 ②）。
   - PR 描述附回切用舊形狀全文（selector；80→8069、8072→8072），供緊急 `kubectl patch` 與回切 PR 直接用（cd#144 PR-D）。
2. 【agent】驗 endpoints 非 0、只含新 web pod IP、80 與 8072 都在，接著跑驗收清單（#1137 scout §7 情境一）：
   - check26（[design doc](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-design.md):101；#1032 plan:83-88）。
   - 4 個 host 的 `/web/health` 200 與登入頁。
   - `/websocket` 回 101，探針帶 `--http1.1`＋`Origin`（不帶 `Origin` 會回 400；#1032 plan:126、charts#99 留言 18097）。新形狀下 80 與 8072 都經 nginx 8080 回 101（彩排 A6）。
   - OAuth `redirect_uri`、`/woow` 起得來。
     - 2026-10-06 真 host 實測：ProxyFix ✅——4 host 未登入打 `/odoo`，303 `Location` 都是 `https://<該 host>/web/login?...`（彩排 A6 的動態探針 inconclusive，以此取代）。OAuth `redirect_uri` 在 4 host 都是 `https://aiot.woowtech.io/auth_oauth/signin`：屬設計，`auth_oauth_proxy_fix` 覆寫 `list_providers` 改用 `web.base.url`（core 18.0 用 `url_root`），每次登入頁會記一行 `ProxyFix: rewrote redirect_uri` WARNING。
     - operator→odoo 若切換後沒有流量，標「待首次 spawn」，下一次租戶 spawn／agent session 時補驗回呼 2xx；反方向看 operator access log 來源 IP 是否為新 web pod（2026-10-06：19 筆 `GET /api/tunnels/<id>` 200）。
   - operator webhook：paas-operator 對 Odoo 的呼叫回 2xx；反方向 Odoo → operator 仍通（cd#144 PR-D 驗收）。
3. 【人類】以內部帳號登入 4 個 host 抽查 ERP 與 `/woow`；請 woow-machines-operator 從 `woowtech-odoo` 確認 mcp-odoo 呼叫正常（cd#144 PR-D 驗收）。
4. 【agent 執行・人類逐步放行】ECPay 收尾（13476 第 9 點；#1163 scout §5.1 T+0）：恢復後等 15 分讓重送進來 → 對 pending tx 用 SDK `query_credit_trade()` 逐筆查（付款後 ≥10 分才查，過快 403 鎖 30 分；13476 第 4 點）→ 有扣款走 `_set_done()`（已 cancel 者走 #864 repair 路徑）→ 查 `woow_invoice_issue_state='issuing'` 殘留 → 依序重新啟用 `ir_cron_issue_pending_ecpay_invoices` → `cron_post_process_payment_tx` → `cron_topup_cleanup`（最後）。
   - 觀測：`[TOPUP] cron cleanup hard-cancel stuck 3DS` 在恢復後 15 分內不應出現（#1163 scout §5.1）。

## B8 回退窗口啟動

1. 【agent】在 #1142 與 woow-paas/paas-app-bundle-cd#144 留言記錄窗口起訖：起點＝PR-D 生效時間（新側開始接寫入），訖點＝起點＋14 天（13439 ③；cd#144 PR-D）。
2. 【人類】PR-D 生效後立即 merge 平台發版改 build `paas-odoo-official` 的 PR（2026-10-05 發起人裁決 G2，#1495）；窗口內**不雙 build**（2026-10-04 發起人裁決；#1392 留言 17382）。
   - 理由：窗口內新 release 只 pin `paas-odoo-official`（第 4 步）、舊 release image 凍結，若仍 build Bitnami `paas-odoo`，窗口內的發版就沒有可部署的 image。
3. 【agent】窗口內每次平台發版前檢查 `git diff v<cutover 當下版>..v<新版> -- src/migrations/` 為空（以整個 release 連坐，跟版 seed PR 也會被同 release 的 migration 連坐；13439 ③、14356）。
   - 非空 ⇒ 在 #1142 記錄「窗口提前關閉（單向門）」並通知發起人；反向搬遷不再保證（13439 ③）。
4. 【agent】窗口內 pin 規則：新 release 只 pin `paas-odoo-official` 的 digest；舊 release 只允許動 `replicas`，image 凍結在 cutover 前那顆（env、persistence、`postgresql.*` 同樣凍結；13439 ③；2026-10-04 發起人裁決）。
   - 回切時舊 release 維持 cutover 前 image：窗口內禁含 migration 的發版，所以回切後只有 addon 程式碼較舊，DB 結構不受影響（2026-10-04 發起人裁決）。
5. 【agent】窗口內 `postgres.extensions.vector` 維持 `false`；回退窗口結束後隨 Step 2 的 CD PR 改回 `true`（R2-Q6）。不做 Step 2（13439 ③）。
6. 【agent】緊急回切入口：[回切 runbook](platform-odoo-cutover-switchback-runbook.md) C1。
7. 【人類】窗口內（以及之後）每一次會改變新 release image digest 的平台發版：升級閘會跑 `-u all`，且**不會**先自動備份（charts#99 留言 18097）⇒ 發版前由發起人親跑新 release 兩顆 PVC 的 Longhorn snapshot（`data-paas-odoo-platform-postgresql-0`、`paas-odoo-platform-data`），驗 `readyToUse=true`／`markRemoved=false`（WMO#5 裁決 Q3）；每顆 volume 保留最近 3 次發版前的 snapshot（Q4）。
   - 同時由 agent 備妥、人類放行一次 on-demand 升級前備份：以 backup CronJob 的 jobTemplate 自組 Job，注入 `MODE=backup`、`BACKUP_KIND=pre-upgrade`（chart README §operator 參數契約；平台 release 不經 operator，13439「給其他票」#1128 條）；產出在 `pre-upgrade/`、沒有 `skipped_reason` 才發版。

## B9 收尾紀錄

1. 【agent】在 cutover issue 寫收尾紀錄：新舊 release 的 helm revision、PR-A～PR-D 與 promote 證明 PR 號、各步時間戳、實測停機時間 vs 彩排量測（[彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md) A8 第 4 步）、Longhorn snapshot 名稱與 `readyToUse` 結果、退役 dump 的 sha256。

## 附錄 T：prod 搬運 helper（G3 裁決：沿用 #1206 形狀，不等 #1217）

> 2026-10-05 發起人裁決 G3（#1495）：不等 #1217 的 platform 人工段，四份 runbook 直接沿用 #1206 在沙盒實測過的形狀；#1217 之後引用本附錄。所有物件都在 `paas-system`、帶 label `app.kubernetes.io/part-of: platform-cutover-transfer`，用完由人類刪除。建立／刪除都是 prod 寫入 ⇒ 【agent 執行・人類逐步放行】。2026-10-05 彩排已實跑：T4＝A2 取資料 Job（下方 manifest，原樣在 prod `paas-system` 由人類 apply 過）、T5＝A3 restore Job（`restore-in-pod.sh`）、反向＝A8（回切 runbook C3）。

| 物件 | 形狀 | 來源 |
|---|---|---|
| T1 搬運 PVC | `platform-cutover-transfer`，5Gi、`longhorn-delete`（dump 約 26 MB＋filestore 約 2 GiB；用完即刪） | WMO#5 c18108 容量估算 |
| T2 receiver | 平台 image（有 python3）跑 `.claude/skills/woow-e2e-fixtures/scripts/prod-clone/receiver.py`（ConfigMap 掛入）；掛 T1；token 放一次性 Secret；Service `platform-cutover-receiver:8080` | #1206 `receiver.py` |
| T3 NP | 只放行 `component=transfer-source` → receiver 8080（`paas-system` 有 ns 級 ingress default-deny）。來源若是舊 Bitnami pg，**不必**新增規則：舊 pg NP 已放行 label `paas-odoo-postgresql-client: "true"`（`pg-backup` 同款），T4 帶這個 label 即可；`paas-system` 沒有 egress 限制。不動既有 NP | #1206 搬運洞 NP 同形；2026-10-05 彩排 A2 實查 |
| T4 來源 Job（`component=transfer-source`） | initContainer＝`paas-postgres` image：`pg_dump -Fc` 來源 DB，另以 psql 產 `manifest.json`（欄位同 prod clone 中和 runbook §B）與 `attachments.txt`；主容器＝平台 image：`tar -C <filestore 目錄> -cf filestore.tar .`、`sha256sum` 產 `SHA256SUMS`，以 python `http.client` PUT 五個檔到 receiver | #1206 端到端實測的合成備份組產生 Job（PR #1493 描述「驗證」段） |
| T5 restore Job（正向） | `paas-postgres` image 跑 `prod-clone/restore-in-pod.sh`：`sha256sum --strict`、`pg_restore -f /dev/null` 全讀、superuser 建 `odoo` 角色、locale 取 manifest 建庫、只建 manifest 列的 extension（`vector` 拒絕）、TOC 只濾 EXTENSION、以 `odoo` 角色 `pg_restore -1 --exit-on-error --no-owner --no-privileges`、filestore 解到 `/var/lib/odoo/filestore/woow_prod` 並 `chown -R 100:101`、驗 `attachments.txt` 引用都在；label `app.kubernetes.io/instance=paas-odoo-platform`（新 pg NP 只放行這個 label） | #1206 `restore-in-pod.sh`（沙盒實測 64/64 gate） |

### T4 來源 Job（彩排 A2 實跑版）

- 彩排實測：`pg_dump` 6 s（27.6 MB）、filestore tar 52 s（7,361 檔、2.02 GB）、PUT 五檔 21 s；Job 以 nodeSelector 釘在舊 odoo pod 所在 node，唯讀共掛 RWO 的 `paas-odoo` PVC。cutover 時舊 odoo 已 0 副本，可拿掉 nodeSelector。
- token：人類從 T2 的 Secret 複製到 `platform-cutover-upload-token`（值經 stdin，不上 argv）。用完刪 Job 與 Secret。

```yaml
# 附錄 T4 來源 Job（2026-10-05 彩排 A2 實跑版，改為 cutover 目的地）。【人類】apply；image digest 換成 cutover 當下的值
# 只讀：pg_dump（bn_odoo）＋唯讀掛載舊 filestore PVC；產物送到同 ns 的 T2 receiver（cutover）。
# 不改任何既有 paas-system 物件：舊 pg NP 已放行 label paas-odoo-postgresql-client=true（pg-backup CronJob 同款）；
# paas-system 沒有 egress 限制。用完由人類刪除本 Job 與 Secret platform-cutover-upload-token。
apiVersion: batch/v1
kind: Job
metadata:
  name: platform-cutover-source-<YYYYMMDD>
  namespace: paas-system
  labels:
    app.kubernetes.io/part-of: platform-cutover-transfer
    app.kubernetes.io/component: transfer-source
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 3600
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: platform-cutover-transfer
        app.kubernetes.io/component: transfer-source
        paas-odoo-postgresql-client: "true"
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      # 舊 filestore PVC（RWO）正被 ubuntuserver-59mqj 上的 paas-odoo pod 掛著 ⇒ 同 node 才能再唯讀掛載
      nodeSelector: {kubernetes.io/hostname: ubuntuserver-59mqj}
      imagePullSecrets: [{name: jfrog-registry}]
      initContainers:
        - name: dump
          image: jcr-prod.woowtech.io/woow-paas-docker-local/paas-postgres:17.11-20260928.1549@sha256:dbd1fed8882043f45bf759e8f7c73092505ef0afef7a23a93b177e6f7c2c840f
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -euo pipefail
              export PGHOST=paas-odoo-postgresql PGUSER=bn_odoo PGDATABASE=woow_prod
              for i in $(seq 1 60); do pg_isready -q -t 2 && break; sleep 1; done
              t0=$(date +%s)
              pg_dump -Fc -f /out/db.dump
              echo "TIMING pg_dump $(( $(date +%s) - t0 ))s $(stat -c %s /out/db.dump) bytes"
              psql -X -tA -c "SELECT json_build_object('schema_version',1,'db_name',current_database(),'encoding',pg_encoding_to_char(encoding),'datcollate',datcollate,'datctype',datctype,'datlocprovider',datlocprovider,'extensions',(SELECT json_object_agg(extname,extversion) FROM pg_extension),'pg_version',current_setting('server_version'),'db_size_bytes',pg_database_size(current_database()),'source','paas-system/paas-odoo-postgresql live pg_dump') FROM pg_database WHERE datname=current_database()" > /out/manifest.json
              psql -X -tA -c "SELECT DISTINCT store_fname FROM ir_attachment WHERE store_fname IS NOT NULL ORDER BY 1" > /out/attachments.txt
              pg_restore -f /dev/null /out/db.dump && echo "VERIFY pg_restore full-read OK"
          env:
            - name: PGPASSWORD
              valueFrom: {secretKeyRef: {name: paas-odoo-postgresql, key: password}}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 512Mi}
          volumeMounts: [{name: out, mountPath: /out}]
      containers:
        - name: push
          image: jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-official:20261005.1128@sha256:76debd4b2176fcfc039628b57569fa7d3ee99e9eb19208910ae49cd218cb6c73
          securityContext: {runAsUser: 0}   # 只為讀 Bitnami 999:0 擁有的 filestore；PVC 唯讀掛載
          command: ["/bin/bash", "-c"]
          args:
            - |
              set -euo pipefail
              cd /out
              src=/src/data/filestore/woow_prod
              echo "SOURCE files=$(find "$src" -type f | wc -l) bytes=$(find "$src" -type f -printf '%s\n' | awk '{s+=$1} END {print s}') owner=$(stat -c %u:%g "$src")"
              t0=$(date +%s); tar -C "$src" -cf filestore.tar .; echo "TIMING tar $(( $(date +%s) - t0 ))s $(stat -c %s filestore.tar) bytes"
              sha256sum db.dump filestore.tar manifest.json attachments.txt > SHA256SUMS
              cat SHA256SUMS
              t0=$(date +%s)
              python3 - <<'PY'
              import http.client, os
              tok = os.environ["UPLOAD_TOKEN"]
              host = "platform-cutover-receiver.paas-system.svc.cluster.local"
              for f in ["db.dump", "filestore.tar", "manifest.json", "attachments.txt", "SHA256SUMS"]:
                  c = http.client.HTTPConnection(host, 8080, timeout=900)
                  with open(f, "rb") as fh:
                      c.request("PUT", "/cutover/" + f, body=fh,
                                headers={"X-Upload-Token": tok, "Content-Length": str(os.path.getsize(f))})
                  r = c.getresponse(); print("PUT", f, r.status, r.read().decode().strip(), flush=True)
                  assert r.status == 201, f
              PY
              echo "TIMING push $(( $(date +%s) - t0 ))s"
          env:
            - name: UPLOAD_TOKEN
              valueFrom: {secretKeyRef: {name: platform-cutover-upload-token, key: token}}
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: "1", memory: 1Gi}
          volumeMounts:
            - {name: out, mountPath: /out}
            - {name: src, mountPath: /src, readOnly: true}
      volumes:
        - {name: out, emptyDir: {sizeLimit: 8Gi}}
        - name: src
          persistentVolumeClaim: {claimName: paas-odoo, readOnly: true}
```

- `--no-owner` 會跳過 `ALTER … OWNER`（含 `ALTER SCHEMA public OWNER`），所以 B6 第 6 步要濾的那條不必另處理（14201「給其他票」）。
- 反向（回切 C3）與 teardown（D1）用同一組 T1～T4，只換來源與目的；反向的寫入端是 Bitnami `bn_odoo`（非 superuser），不用 T5，改用回切 runbook C3 寫明的指令。
- 新 pod 開頭約 1～2 秒 NP 尚未生效：T4／T5 都先等 DB 可連再動作（#1206 證據；woowprd#13）。
- 刻意**不搬** `data_dir/sessions`（13439 ⑧）：切換後全員重新登入（B1 第 1 步公告）。
- 本機背景 port-forward 收尾：⛔ 不用 `pkill -f '<pattern>'` 直接打字面 pattern——以 `!`／`bash -c` 執行時，呼叫端 shell 自己的 command line 也含該 pattern，`pkill` 會連自己一起殺掉（同一行後面的步驟不會執行，exit 143／144）。改用 `kill "$(cat <pidfile>)"`，或把 pattern 第一個字元包成字元類別（`pkill -f '[p]ort-forward .*18000:8000'`）。2026-10-06 B0 第 4～5 步人類指令第 8 步實撞。

## cutover 實跑紀錄（2026-10-06）

完整時間線、helm revision、snapshot 與退役 dump 見 B9 收尾紀錄（#1231 留言 18456）。

| 量測 | 實測 | 彩排／估計 |
|---|---|---|
| 舊 odoo 優雅終止 | 約 37 s（04:19:16 `Killing` → 04:19:53 前消失；#214 merge 到 helm v137 另有約 1 分 Fleet 同步） | 90 s |
| T4 來源 Job | dump 5 s、filestore tar 55 s、PUT 五檔 20 s，sha256 全符 | 6／52／21 s |
| T5 restore | `RESTORE-DONE` | 60～90 s |
| 指紋 | W2 舊側 vs restore 後／web 開機後／cron 開機後 **三次 FP-IDENTICAL** | — |
| 升級閘 | `skip`（沒有 `-u all`），web 約 60 s Ready | 約 30 s |
| 服務中斷 | **約 21 分**（T0 04:17:23 → PR-D 04:39:08） | 5～6 分（機器時間） |
| 切換後修正 | woow-paas/paas-app-bundle-cd#219 `dbMaxconn 6→8`：web 重啟約 60 s | — |

差距主因與已回填的修正：

| 項 | 現象 | 回填處 |
|---|---|---|
| 停機估計 | 每張窗口內 PR 等 Fleet 約 1 分 ×4、人工逐步放行與唯讀驗收 | 時序總覽、B1 第 1 步 |
| session | filesystem session store 未搬，全員重新登入 | B1 第 1 步、附錄 T |
| ECPay SQL | `GROUP BY 1` 指到常數欄；三支 cron 原值即 `false` | B6 第 1 步 |
| cron 連線池 | `dbMaxconn 6` → cron PoolError；schema 擋 `maxCronThreads < 3` | 術語表、B5 第 4 步 |
| port-forward 收尾 | `pkill -f` 殺到自己 | 附錄 T |
| ProxyFix／OAuth | 真 host 驗過；`redirect_uri` 固定 aiot 屬設計 | B7 第 2 步 |
| 中文 PDF 字形 | `NotoSansCJKjp` 嵌入（fontconfig 未隨 image） | B6a 第 3 步 |

## 相關

- [彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)、[回切 runbook](platform-odoo-cutover-switchback-runbook.md)、[teardown runbook](platform-odoo-cutover-teardown-runbook.md)
- [`prod-release-sop.md`](prod-release-sop.md) §5.1.1、[`shared-cluster-paas-scope-guard.md`](shared-cluster-paas-scope-guard.md)
- #1142、#1145、#1163、#1174、#1194、#1392、woow-paas/woow-paas-charts#81／#99／#100／#120、woow-paas/paas-app-bundle-cd#143／#144／#145、woow-paas/paas-odoo-ci#145、woow-paas/woow-machines-operator#4／#5
