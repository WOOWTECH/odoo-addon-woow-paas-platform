---
title: 平台 Odoo 換 chart：cutover runbook（Step 1）
category: guides
status: draft
created: 2026-10-04T08:30:00Z
updated: 2026-10-04T08:30:00Z
related: [docs/guides/deployment/platform-odoo-cutover-rehearsal-runbook.md, docs/guides/deployment/platform-odoo-cutover-switchback-runbook.md, docs/guides/deployment/platform-odoo-cutover-teardown-runbook.md, docs/guides/deployment/prod-release-sop.md]
---

# 平台 Odoo 換 chart：cutover runbook（Step 1）

> 四份同族 runbook：[彩排](platform-odoo-cutover-rehearsal-runbook.md)／**cutover（本檔）**／[回切](platform-odoo-cutover-switchback-runbook.md)／[teardown](platform-odoo-cutover-teardown-runbook.md)。票：odoo-addons/woow_paas_platform#1231；CD 執行面：woow-paas/paas-app-bundle-cd#143（edge）、#144（cutover PR-A～PR-D）。

## 狀態

`status: draft`——**先草擬、後定稿**（#1231 票面）。須在 13439 ④ 觸發條件成立前定稿；佔位落地＋彩排量測回填後改 `status: active`，由發起人覆核。

| # | 佔位 | 出處票 |
|---|---|---|
| C1 | 穩定 Service 名與 raw bundle 目錄（暫名 `paas-odoo-active`／`paas-odoo-edge/`） | woow-paas/paas-app-bundle-cd#143 |
| C2 | 新 release 名、bundle 目錄、fleet-local secret 名（暫名 `paas-odoo-platform`／`paas-odoo-platform-fleet-secrets`） | woow-paas/paas-app-bundle-cd#144 |
| C3 | 正式平台 image＋chart 的實機 smoke 結論 | woow-paas/woow-paas-charts#99 |
| C4 | 搬運 helper pod 形狀與指令（平台還原人工段） | #1217 |
| C5 | 窗口期間叢集容量、Longhorn snapshot 命名（版號不變時的 `pre{版號}`）與保留政策 | woow-paas/woow-machines-operator#5 |
| C6 | 「關舊 Ingress」與「開 raw Ingress」的順序是否排進停機窗口（13439 ② 指出同 host 雙 Ingress 的 traefik 仲裁未驗） | woow-paas/paas-app-bundle-cd#143（發起人裁定） |
| C7 | cron 副本 0→1 走哪張 CD PR（PR-C 二次 merge 或併入 PR-D） | woow-paas/paas-app-bundle-cd#144 留言 14668（「由發起人定」） |
| C8 | 雙 build 模式的切換載體（repo variable 或 workflow 常數） | #1392 |
| C9 | 回切時舊 release 是否改 pin 窗口內最新的 Bitnami build（#1392「addon 一致」與 13439 ③「舊 release 只允許動 replicas」的關係） | #1392／#1142（發起人裁定） |
| C10 | 停機前要停的三支 cron 的 xml id | #1163 scout §5 |

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
| #1137 scout | [`2026-09-19-platform-odoo-chart-gap.md`](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/src/branch/research/platform-odoo-chart-gap/docs/scout/2026-09-19-platform-odoo-chart-gap.md)（未合併分支） |
| chart README | woow-paas/woow-paas-charts `main` 的 `charts/woow-odoo/README.md`（0.2.5） |
| #1032 plan | [`2026-09-03-woowtech-odoo-paas-migration-implementation.md`](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md) |

## 已落地、直接用的實值

| 項目 | 值 | 出處 |
|---|---|---|
| chart | `woow-odoo` 0.2.5；merge `main` 即同時推 Gitea helm registry（Fleet 用）與 JCR OCI | woow-paas/woow-paas-charts#120；chart README §發佈 |
| platform cutover gate | `conf.limitTimeRealCron ≥ 1800`、`cron.maxCronThreads ≥ 3`（schema fail-loud） | woow-paas/woow-paas-charts#100；chart README |
| `addons_path`（platform） | core、`/opt/woow/addons`、`/mnt/extra-addons` | woow-paas/woow-paas-charts#120；chart README 固定契約 |
| Service／port | port 80 與 8072 都以數字 targetPort **8080** 指向 nginx sidecar | chart README 固定契約 |
| pod label | `app.kubernetes.io/name=odoo`、`instance=<release>`、`component=server｜cron｜postgresql｜db-backup｜db-restore` | chart README 固定契約 |
| uid | odoo 100／gid 101（PVC `fsGroup 101`）；postgres 999 | chart README 固定契約；16804 |
| 副本鍵 | web `replicas`、cron `cron.replicas` | chart README §values 重點；cc/woowprd#8 留言 17217 |
| marker | `woow_paas_platform.upgrade_done`＝`{"digest":"sha256:…","core_date":"18.0.YYYYMMDD"}` | chart README 固定契約；chart `files/lib.sh` |
| 平台 image 線 | `paas-odoo-official`（`Dockerfile.official`，官方 `odoo:18.0-20250807` base，Odoo deb `18.0.20250807`）；Phase B `20261003.1514@sha256:0be8cc62…`（綁 addon .203，cutover 時以當時 prod 版號重 build） | woow-paas/paas-odoo-ci#145；16804 |
| 雙 build 規則 | 回退窗口內 `paas-odoo`＋`paas-odoo-official` 兩顆都 build、同一 `addon_ref`；窗口後只 build `paas-odoo-official` | #1392 |
| build 快取 | 依賴層先於 addon；只改 addon 時 pip 層 CACHED | woow-paas/paas-odoo-ci#228 |
| `woow_prod` locale | `UTF8 | en_US.UTF-8 | en_US.UTF-8`、PG 17.6（2026-09-27 唯讀） | R2-Q4；#1162 留言 14676 |

## 時序總覽

| 時點 | 章節 | 內容 |
|---|---|---|
| 窗口前數週 | B0、B2 | 進入條件、edge bundle、消費者改指 |
| 窗口前數日 | B5 | 新 release 並存（web／cron 0）、promote 走通 |
| 窗口前 ≥3 日 | B1 | 內部公告 |
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
3. 【agent】確認 woow-paas/woow-paas-charts#99 smoke 通過〔待定：woow-paas/woow-paas-charts#99（C3）〕，且 chart 0.2.5 在 Gitea helm registry 可取得（13439 ②；woow-paas/paas-app-bundle-cd#144 PR-A 行內註解記實測 HTTP 200）。
4. 【agent】叢集內產出 `bitnami_odoo`（88 MB，13439 ⑦）最終 dump，附 sha256 與 `pg_restore -f /dev/null` 結果。
   - 只能叢集內 HTTP PUT push，⛔ 不用 `kubectl exec … > file`（[pitfall](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md)）。
   - `bitnami_odoo` 不搬進新 postgres（13439 ⑦）。
5. 【人類】把上一步的 dump `age` 加密，上傳公司 Google Drive 專用資料夾，回填 sha256；保留到回退窗口結束後再 90 天（map #1134 2026-09-26 裁決；13439 ⑦）。
6. 【agent】確認「promote 走通」證明已完成（B5 第 4 步；13439 ③）。
7. 【agent】確認 woow-paas/woow-machines-operator#5 容量結論為「夠」〔待定：woow-paas/woow-machines-operator#5（C5）〕。
8. 【agent】確認 woow-paas/woow-machines-operator#4 回報 mcp-odoo ConfigMap（`mcp-admin-config`、`mcp-admin-init-config`）已改指穩定 Service（13439 ⑧；cd#143「交接與盤點」）。
9. 【agent】確認 woow-paas/paas-app-bundle-cd#145（SOP／`promote-acceptance-checklist.md` 參數化）已落地——窗口內仍可能發 hotfix（13439 ③）。
10. 【agent】確認 #1392 已落地（`trigger-odoo-build.yml` 可依模式 dispatch 一或兩顆，`/woow:deploy` 與 `prod-release-sop.md` 已同步；#1392「要做」1～2）。
11. 【agent】確認 Step 1 平台 image 是以「停機窗口當下的 prod 版號」build 的：image 內 addon 版號＝prod 現行 `src/__manifest__.py` 版號（16804「cutover」段）；不是就照 B5 第 1～3 步重 build、重 pin。

## B1 窗口與公告

1. 【人類】排停機窗口：非上班日凌晨 60 分（技術停機 15～20 分），內部公告 ERP 使用者停機時段與「切換後須重新登入」；官網不另公告；sessions 不搬（13439 ⑧）。
2. 【人類】依 #1163 以「停機 ≥ 15 分即視為會丟 ECPay 回呼」規劃金流段，指定 B6／B7 的 ECPay 前置與收尾負責人（13476 第 2、9 點）。

## B2 一次性前置變更（可提前數週，各自一張 PR）

1. 【agent 開 PR・人類 merge】`paas-odoo-edge/` raw bundle（cd#143 PR-1）：穩定 Service 先指舊 pod〔待定：woow-paas/paas-app-bundle-cd#143（C1）〕。
   - 舊形狀：selector 逐字照抄 live `svc/paas-odoo` 的 `.spec.selector`（預期 `app.kubernetes.io/instance: paas-odoo`＋`app.kubernetes.io/name: odoo`，待驗），⛔ 不加 `component`（舊 pod 沒有這個鍵，加了就是 0 endpoints）；ports 80→8069、8072→8072，targetPort 一律寫數字（cd#143 PR-1）。
   - raw Ingress：4 host（`paas.woowtech.io`、`aiot.woowtech.io`、`www.designsmart.com.tw`、`woowtech-ds.woowtech.io`）、4 個 TLS secretName 與 live 逐字相同、`cert-manager.io/cluster-issuer: letsencrypt-prod`（cd#143 現況段）。
   - ⚠ Fleet 對 bundle 內容跑 Go template，連 YAML 註解裡的連續大括號都吃（cd#143 現況段）。
2. 【agent 開 PR・人類 merge】舊 release 的 `ingress.enabled` 改 `false`（cd#143 PR-2），附同一 chart／values／dummy 密碼的 `helm template` 前後 diff：只允許 `Ingress/paas-odoo` 消失。
   - 順序：先 raw Ingress、再關 chart Ingress；反過來 4 個 host 會有無路由空窗（cd#143「依賴與順序」）。
   - 是否排進停機窗口〔待定：woow-paas/paas-app-bundle-cd#143（C6）〕。
   - TLS Secret 沒有 ownerReference，重建 Ingress 不會重簽（13439 ②）。
3. 【agent 開 PR・人類 merge】`paas-operator/values-prod.yaml:55` 的 `odooBaseUrl` 改指穩定 Service（cd#143 PR-3）。
4. 【agent】盤點 prod（`paas-ws-*`，不含 `paas-ws-stg-*`）存活 agent-runtime pod 中 `ODOO_BASE_URL` 仍指 `paas-odoo.paas-system` 者；只用 jsonpath 取該 env，不 dump pod 全文（13439 checklist 補充項；cd#143 PR-3）。
5. 【agent】從 odoo access log 列出叢集內實際來源，扣掉 traefik／paas-operator／mcp-odoo，剩下的逐一給處置（`NetworkPolicy/paas-odoo` 規則 ① 沒有 `from`；13439 checklist 補充項）。
6. 【人類】請 woow-machines-operator 改指 mcp-odoo（`woowtech-odoo` ns 不在 paas 白名單；woow-paas/woow-machines-operator#4）。

## B3 人工前置資料

1. 【人類】建新 bundle 的 fleet-local secret〔待定：woow-paas/paas-app-bundle-cd#144（C2）〕，名稱必須有 `paas-*` 前綴（CLAUDE.md §kubectl `fleet-local` 只按資源名前綴授權）。
   - 內容：`values.yaml` 鍵下的 `credentials.postgresPassword`、`credentials.odooPassword`、`credentials.adminPasswd`（platform 三個必填，chart README §values 重點）。
   - agent 只用 key 名與 sha256 指紋驗，不印值（woow-paas/paas-app-bundle-cd#144 背景「secret 紀律」）。
2. 【agent】`conf.emailFrom` 取現行 CD `paas-odoo/values-prod.yaml` 的 `odooEmail` 值帶入新 values；不加 `from_filter`；不設 `smtp.*`（R2-Q5；14662 B3；平台 SMTP 走 DB `ir.mail_server`）。
3. 【agent】記錄切換前指紋（只印 sha256 前 12 碼，先排除哨兵值 `e3b0c44298fc`／`74234e98afe7`；cd#144 背景「secret 紀律」）：`ir_config_parameter` 的 `database.secret`、`database.uuid`，`res_users` id 2 的 `login`、`password`，以及 `ir_module_module` name→`latest_version` 全表的 sha256。
   - `database.secret`／`database.uuid` 正式切換**必須維持原值**，否則 prod 簽出的 portal／取消訂閱／CSRF 連結全部失效（14050 ②）。
   - bootstrap 完成標記不需處置：platform 沒有 bootstrap ⑤（R2-Q2；14701）。
   - 升級閘 marker 在 B6 第 8 步預寫（R2-Q1）。

## B4 還原錨點

> 時點：B6 第 2 步凍結舊側之後、第 4 步 dump 之前（舊 odoo 已 0，DB 靜止；推論）。命名沿 `prod-release-sop.md` §5.1.1。

1. 【人類】建 Longhorn snapshot 兩顆：舊 pg PVC `data-paas-odoo-postgresql-0`、舊 filestore PVC `paas-odoo`；名稱 `snap-paas-odoo-{pg,fs}-pre{版號}-{YYYYMMDD}`（`prod-release-sop.md` §5.1.1）。
   - 版號不變時 `pre{版號}` 的填法〔待定：woow-paas/woow-machines-operator#5（C5）〕。
2. 【agent】唯讀驗兩顆都 `readyToUse=true` 且 `markRemoved=false`；任一不符＝沒有還原錨點 ⇒ STOP（`prod-release-sop.md` §5.1.1）。

## B5 新 release 並存部署（窗口前數日，不停機）

1. 【agent 執行・人類逐步放行】若 B0 第 11 步不符：dispatch paas-odoo-ci `build-image.yml`，帶 `image_name=paas-odoo-official`、`addon_ref=<prod 現行 tag>`（16804「cutover」段）。
   - ⚠ `addon_ref` 必填，漏填會從移動中的 `develop` HEAD 烤 image（`prod-release-sop.md` §9 坑 12）。
   - 只改 addon 時 pip 層走快取（woow-paas/paas-odoo-ci#228）。
2. 【人類】授權者本人在 approval issue 留 `/approve`；agent 不代簽（CLAUDE.md §Release；16804）。
3. 【agent】記下新 digest，部署後以 Pod `imageID` 做 64 hex 逐字比對（JCR 不給匿名讀，16804）。
4. 【agent 開 PR・人類 merge】PR-A：新 bundle 目錄〔待定：woow-paas/paas-app-bundle-cd#144（C2）〕，release 名 ≠ `paas-odoo`（原地換 chart 會刪 Helm 管的 PVC 與 Secret，#1137 G30）。values 要點：
   - `profile: platform`、`postgres.database: woow_prod`、`conf.emailFrom`（B3 第 2 步）、`postgres.extensions.vector: false`（R2-Q6）、`conf.limitTimeRealCron ≥ 1800`、`cron.maxCronThreads ≥ 3`。
   - `replicas: 0`、`cron.replicas: 0`、postgres 1：restore 前起 odoo 會停在 `Init:Error`（bootstrap exit 3 `platform DB must be restored before first boot`；R2-Q2、14668）。
   - `image.*`＝Step 1 digest；`postgres.image.*`＝chart 預設 paas-postgres（PG 17.11、pgvector 0.8.6、rsync；chart README）；`imagePullSecrets: [jfrog-registry]`（cd#144 PR-A）。
   - `ingress.enabled: false`（Ingress 由 raw bundle 管，13439 ②）。
   - `backup.enabled: false`（本階段）、`backup.persistence.existingClaim: paas-odoo-backup-pvc`、`restore.enabled: false`（13786 D7；14201 D11）。
   - PVC 一律明寫 `storageClass`（叢集有兩個 default SC；chart README）。
5. 【agent】驗 PR-A：新 Bundle `Ready=True`；新 postgres Running；web／cron pod 0；舊 `paas-odoo` 沒有新 helm revision、pod UID 與 restartCount 不變；新舊物件名不相交；`valuesFrom` 已解析（Helm 層 Secret 指紋＝fleet-local 欄位指紋）（cd#144 PR-A 驗收）。
6. 【agent 開 PR・人類 merge】「promote 走通」證明：對新 bundle 開一張 image pin 變更 PR，最後停在 Step 1 目標 digest，照 woow-paas/paas-app-bundle-cd#145 參數化後的程序驗收（13439 ③；cd#144 PR-A）。
7. 【agent】驗三條 NP 物件（traefik／paas-operator／mcp-odoo）與放行 `app.kubernetes.io/instance=<release>` 的 pg NP 都已 render；生效驗證在 B7（13439 ②；cd#144 PR-A 驗收）。

## B6 凍結＋正向搬遷（停機窗口內）

1. 【agent 執行・人類逐步放行】ECPay 前置：停 cleanup／發票／core post-process 三支 cron〔待定：#1163 scout §5（C10）〕，記下 pending tx 清單與 T0（13476 第 9 點）。
   - ⛔ 不 disable payment provider，會 archive token（13476 第 9 點）。
2. 【agent 開 PR・人類 merge】PR-B：`paas-odoo/values-prod.yaml` **只加** `replicaCount: 0`（merge 前用 `helm show values` 實拉確認 Bitnami 28.2.10 的鍵路徑）。這是窗口內唯一一次對舊 release 的 upgrade（13439 ③；cd#144 PR-B）。
3. 【agent】驗：`deploy/paas-odoo` replicas 0、pod 已消失；`paas-odoo-postgresql-0` pod UID 與 restartCount 不變；兩顆舊 PVC 仍 Bound（cd#144 PR-B 驗收）。接著做 B4。
4. 【agent 執行・人類逐步放行】舊 pg 對 `woow_prod` 跑 `pg_dump -Fc`，叢集內 HTTP PUT push 到搬運 helper pod〔待定：#1217（C4）〕；附 sha256 與 `pg_restore -f /dev/null` 結果。
   - ⛔ 不用 `kubectl exec … > file`，也不用 exec 串 exec 的 stdout 管線（同一個截斷面；pitfall doc）。
   - 舊 pg 的 NP 只放行 `name=odoo`＋`instance=paas-odoo`（cd#144 背景）；push 端 pod 的 label 由彩排 A8 定案〔待驗〕。
5. 【agent 執行・人類逐步放行】新 pg 以 superuser 建角色、建庫、預建 extension（14201 D2；R2-Q4、R2-Q6）：

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
6. 【agent 執行・人類逐步放行】以 `odoo` 帳號 `pg_restore -1 -d woow_prod -L <list>`，`<list>` 由 `pg_restore -l` 濾掉 `COMMENT - EXTENSION`、`EXTENSION`，以及 `ALTER SCHEMA public OWNER` 條目（`woow_prod` 的 `public` owner 若不是 `pg_database_owner`，dump 會多出這條；chart invariant 永不 `ALTER SCHEMA public OWNER`）（14201 D2 與「給其他票」）。
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
9. 【agent 執行・人類逐步放行】filestore 全量 push（不用增量 rsync），舊 PVC `paas-odoo` 上的 `filestore/woow_prod`〔待驗：Bitnami 路徑〕→ 新 PVC `<release>-data` 的 `/var/lib/odoo/filestore/woow_prod`（chart README 固定契約）。
   - 校驗：檔數＋`find -type f -printf '%s\n'` 位元組總和，兩端相等（不用 `du -sb`；#1137 §4.2）。參考量：7,194 檔、1,963,901,297 bytes（2026-09-04；#1032 plan:111，引自 cd#144 PR-C）。
   - 校驗通過後 `chown -R 100:101`（chart README uid／fsGroup）。

## B6a 起新 odoo（位置：B6 filestore 校驗後、B7 selector 切換前）

> 順序依 R2-Q3 與「彩排順序改寫」（14662 B 段新增步）：web 1、cron 0 → 升級閘 marker 相等跳過、web Ready → 才 cron 1。cron pod 的 `cron-wait` initContainer 是第二道保險，不取代順序。

1. 【agent】merge 前 gate：唯讀確認新 pg 上 `upgrade_done` 的 digest 等於 values `image.digest`，且該 release 沒有 Running 的 `component=db-backup` Job（cd#144 PR-C merge 前 gate；13786 D2）。
2. 【agent 開 PR・人類 merge】PR-C（13786 D7 同一張 PR）：
   - `replicas: 1`、`cron.replicas: 0`、`backup.enabled: true`（retention／timeZone／schedule 用 chart 預設：7 daily＋4 weekly、Asia/Taipei、02:00＋adler32 偏移；13786 D4）。
   - 刪除 `paas-odoo-backup/pg-backup-cronjob.yaml`；⛔ 保留 `backup-pvc.yaml` 與 `fleet.yaml`（刪整個目錄會連 `paas-odoo-backup-pvc` 一起下架；cd#144 PR-C）。PR 描述記下被刪檔的最後 commit sha，回切要重新 apply（13786 D7）。
3. 【agent】驗：`upgrade-gate` log 出現 `skip: woow_paas_platform.upgrade_done.digest == current image digest`、web Ready；`ir_module_module` 全表 sha256 與凍結當下相同（＝沒有 `-u all`）；admin、`database.secret`／`uuid` 指紋不變；中文 PDF 抽查交人類（cd#144 PR-C 驗收）。
4. 【agent 開 PR・人類 merge】`cron.replicas: 1`〔待定：woow-paas/paas-app-bundle-cd#144 留言 14668（C7）〕；cron pod 的 `cron-wait` 應立刻放行（chart README）。
5. 【agent 執行・人類逐步放行】on-demand 備份一次：`kubectl --context woow-k3s -n paas-system create job --from=cronjob/<release>-backup <name>`；Job Complete、產出 `db.dump`＋`filestore/`＋`manifest.json`、沒有 `skipped_reason`；PVC 根目錄舊 `*.pg` 仍在；任何時點都沒有兩支指向 `paas-odoo-backup-pvc` 的 CronJob（cd#144 PR-C 驗收）。

## B7 切流量

1. 【agent 開 PR・人類 merge】PR-D：穩定 Service 改新形狀〔待定：woow-paas/paas-app-bundle-cd#143（C1）〕。
   - selector：`app.kubernetes.io/instance: <release>`＋`app.kubernetes.io/component: server`（改鍵集合，不只換值；排除 cron 與 `db-backup`／`db-restore` pod；13439 ②）。
   - port 80 與 8072 的 targetPort 都改成數字 `8080`（chart README）。⚠ 不可用具名 targetPort（13439 ②）。
   - PR 描述附回切用舊形狀全文（selector；80→8069、8072→8072），供緊急 `kubectl patch` 與回切 PR 直接用（cd#144 PR-D）。
2. 【agent】驗 endpoints 非 0、只含新 web pod IP、80 與 8072 都在，接著跑驗收清單（#1137 scout §7 情境一）：
   - check26（[design doc](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-design.md):101；#1032 plan:83-88）。
   - 4 個 host 的 `/web/health` 200 與登入頁。
   - `/websocket` 回 101，探針帶 `--http1.1`＋`Origin`（#1032 plan:126）。
   - OAuth `redirect_uri`、`/woow` 起得來。
   - operator webhook：paas-operator 對 Odoo 的呼叫回 2xx；反方向 Odoo → operator 仍通（cd#144 PR-D 驗收）。
3. 【人類】以內部帳號登入 4 個 host 抽查 ERP 與 `/woow`；請 woow-machines-operator 從 `woowtech-odoo` 確認 mcp-odoo 呼叫正常（cd#144 PR-D 驗收）。
4. 【agent 執行・人類逐步放行】ECPay 收尾（13476 第 9 點）：恢復後等 15 分讓重送進來 → 對 pending tx 用 SDK `query_credit_trade()`／`order_search` 逐筆查（付款後 ≥10 分才查，過快 403 鎖 30 分；13476 第 4 點）→ 有扣款走 `_set_done()`（已 cancel 者走 #864 repair 路徑）→ 查 `woow_invoice_issue_state='issuing'` 殘留 → 最後才開 cleanup cron。

## B8 回退窗口啟動

1. 【agent】在 #1142 與 woow-paas/paas-app-bundle-cd#144 留言記錄窗口起訖：起點＝PR-D 生效時間（新側開始接寫入），訖點＝起點＋14 天（13439 ③；cd#144 PR-D）。
2. 【人類】把 #1392 的 build 模式切到「兩顆都 build」〔待定：#1392（C8）〕。
3. 【agent】窗口內每次平台發版前檢查 `git diff v<cutover 當下版>..v<新版> -- src/migrations/` 為空（以整個 release 連坐，跟版 seed PR 也會被同 release 的 migration 連坐；13439 ③、14356）。
   - 非空 ⇒ 在 #1142 記錄「窗口提前關閉（單向門）」並通知發起人；反向搬遷不再保證（13439 ③）。
4. 【agent】窗口內 pin 規則：新 release 只 pin `paas-odoo-official` 的 digest，舊 release 只允許動 `replicas`（image、env、persistence、`postgresql.*` 凍結；13439 ③；#1392 第 3 點）。
   - 窗口內 build 出的 Bitnami `paas-odoo` 在回切時要不要 pin 上舊 release〔待定：#1392／#1142（C9）〕。
5. 【agent】窗口內 `postgres.extensions.vector` 維持 `false`；回退窗口結束後隨 Step 2 的 CD PR 改回 `true`（R2-Q6）。不做 Step 2（13439 ③）。
6. 【agent】緊急回切入口：[回切 runbook](platform-odoo-cutover-switchback-runbook.md) C1。

## B9 收尾紀錄

1. 【agent】在 cutover issue 寫收尾紀錄：新舊 release 的 helm revision、PR-A～PR-D 與 promote 證明 PR 號、各步時間戳、實測停機時間 vs 彩排量測（[彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md) A8 第 4 步）、Longhorn snapshot 名稱與 `readyToUse` 結果、退役 dump 的 sha256。

## 相關

- [彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)、[回切 runbook](platform-odoo-cutover-switchback-runbook.md)、[teardown runbook](platform-odoo-cutover-teardown-runbook.md)
- [`prod-release-sop.md`](prod-release-sop.md) §5.1.1、[`shared-cluster-paas-scope-guard.md`](shared-cluster-paas-scope-guard.md)
- #1142、#1145、#1163、#1174、#1194、#1392、woow-paas/woow-paas-charts#81／#99／#100／#120、woow-paas/paas-app-bundle-cd#143／#144／#145、woow-paas/paas-odoo-ci#145、woow-paas/woow-machines-operator#4／#5
