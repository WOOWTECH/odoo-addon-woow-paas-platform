---
title: 平台 Odoo 換 chart：teardown runbook（退役 Bitnami release）
category: guides
status: draft
created: 2026-10-04T08:30:00Z
updated: 2026-10-05T17:00:00Z
related: [docs/guides/deployment/platform-odoo-cutover-runbook.md, docs/guides/deployment/platform-odoo-cutover-switchback-runbook.md, docs/guides/deployment/platform-odoo-cutover-rehearsal-runbook.md, docs/guides/deployment/prod-release-sop.md, docs/guides/deployment/shared-cluster-paas-scope-guard.md]
---

# 平台 Odoo 換 chart：teardown runbook（退役 Bitnami release）

> 四份同族 runbook：[彩排](platform-odoo-cutover-rehearsal-runbook.md)／[cutover](platform-odoo-cutover-runbook.md)／[回切](platform-odoo-cutover-switchback-runbook.md)／**teardown（本檔）**。票：odoo-addons/woow_paas_platform#1231。

## 狀態

`status: draft`——**先草擬、後定稿**（#1231 票面）。須在回退窗口結束前定稿；佔位落地後改 `status: active`，由發起人覆核。

| # | 佔位 | 狀態 |
|---|---|---|
| T1 | 新 release 名 | ✅ `paas-odoo-platform`（cd#145 留言 18089）；PVC `data-paas-odoo-platform-postgresql-0`、`paas-odoo-platform-data` |
| T2 | 穩定 Service 名與 raw bundle 目錄 | ✅ `paas-system/paas-odoo-active`＋`paas-odoo-edge/`（woow-paas/paas-app-bundle-cd#199） |
| T3 | `paas-odoo-backup-pvc` 根目錄舊 `*.pg` 的處置 | 部分定案：7 份未輪替的 `paas_odoo_*.pg` 併入退役 dump（WMO#5 裁決 Q5d）；`woow_prod_*.pg` 的處置〔待裁決：G6〕 |
| T4 | 離叢集 backup target（#443）定案後的遷移 | ✅ 保留到窗口結束後 90 天；到期時 #443 未落地就自動延長到 #443 落地（WMO#5 裁決 Q5c） |
| T5 | teardown 用的 dump push helper pod 形狀 | 〔待裁決：G3〕 |

## 操作者圖例

| 標記 | 意義 |
|---|---|
| 【人類】 | 發起人或叢集運維親手執行（13439 ⑧、map #1134 2026-09-26 裁決）。⚠ 刪 PVC、動 fleet-local secret、離叢集上傳、CD PR merge 一律【人類】。 |
| 【agent】 | agent 可自行執行：唯讀 get（含 cluster-scoped PV 的唯讀盤點）、PR 草稿。 |
| 【agent 執行・人類逐步放行】 | agent 備妥指令，每一步由人類放行。 |
| 【agent 開 PR・人類 merge】 | agent 開 PR，merge 是人工 gate。 |

**步驟行慣例**：以「數字＋句點＋空格」開頭的行是步驟行，每一行帶、而且只帶一個上表標記；其餘說明用 `-` 子項，不帶標記。

## 術語

- 引用 [CONTEXT.md](../../../CONTEXT.md)：**平台 Odoo**（:77）、**備份**（:97；Longhorn snapshot 是「還原錨點」，不叫備份，:122、:125）。
- **退役 dump**：Bitnami release 最後一份離叢集副本（`pg_dump -Fc`＋filestore tar、`age` 加密；map #1134 2026-09-26 裁決）。
- **回退窗口**：cutover 起 14 天（13439 ③）。**停機窗口**不適用本檔：teardown 不停機（D3 驗收）。

出處縮寫：WMO#5 裁決＝[woow-paas/woow-machines-operator#5 留言 18108](https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/5#issuecomment-18108)（Q1～Q6）；13439＝[#1142 resolution](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1142#issuecomment-13439)；13786＝[#1145 resolution](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1145#issuecomment-13786)；map 裁決＝#1134 body「Decisions so far」末條（2026-09-26）；其餘同 [cutover runbook](platform-odoo-cutover-runbook.md) §術語。

## D0 進入條件

1. 【agent】確認以下其一成立，結果寫進 teardown issue：
   - 回退窗口 14 天已結束（起訖見 #1142 與 woow-paas/paas-app-bundle-cd#144 的窗口留言；13439 ③）。
   - 窗口已因含 migration 的 release 提前關閉（單向門），且發起人已在 #1142 確認進 teardown（13439 ③）。
2. 【人類】發起人確認「不再需要回切」，在 teardown issue 留言放行。
3. 【agent】確認 D1 已完成（退役 dump 已離叢集並回填 sha256）——D1 是本檔所有刪除步驟的硬前置（見 D2）。

## D1 退役 dump

1. 【agent 執行・人類逐步放行】在叢集內產出舊 postgres 的最終 `pg_dump -Fc`：`bitnami_odoo`（88 MB，13439 ⑦）與 `woow_prod` 各一份；filestore（舊 PVC `paas-odoo` 的 `/bitnami/odoo/data/filestore/`）打 tar；`paas-odoo-backup-pvc` 根目錄 7 份從未輪替的 `paas_odoo_2026082*～0904*.pg`（約 72 MB）一起打包（WMO#5 裁決 Q5d）；每份附 sha256，dump 另附 `pg_restore -f /dev/null` 結果（13439 ⑦；map 裁決；WMO#5 裁決 Q5b）。
   - 舊 odoo 已是 0 副本、舊 pg 仍 Running（cutover PR-B 只縮 odoo；woow-paas/paas-app-bundle-cd#144 PR-B 驗收）。
   - 只能叢集內 HTTP PUT push，⛔ 不用 `kubectl exec … > file`（[pitfall](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md)）；helper pod 形狀〔待裁決：G3〕。
   - `bitnami_odoo` 若已在 cutover B0 第 4～5 步上傳且 sha256 未變，可沿用、不重產（推論）。
2. 【人類】發起人把上一步產物以 `age` 加密，上傳公司 Google Drive 專用資料夾，回填 sha256 到本 runbook 的「退役 dump 紀錄」表（下方）與 teardown issue；保留到回退窗口結束後再 90 天（map 裁決；13439 ⑦；WMO#5 裁決 Q5b）。
   - `age` 私鑰由發起人保管，另存一份到公司密碼庫（WMO#5 裁決 Q5a）。
   - JCR 在同一座 woow-k3s，不算離叢集，只能當過渡（map 裁決）。
3. 【人類】#443 定出 backup target 後，把 Google Drive 上的退役 dump 遷過去；90 天到期時 #443 若還沒落地，**自動延長**到 #443 落地為止，不刪（WMO#5 裁決 Q5c；map 裁決）。

### 退役 dump 紀錄（D1 第 2 步回填）

| 檔案 | bytes | sha256 | 上傳時間 | 到期 |
|---|---|---|---|---|
| （cutover 後回填） | | | | |

## D2 錨點聲明

1. 【agent】在 teardown issue 寫明錨點聲明：Longhorn snapshot 住在同一顆 volume 內，volume 刪掉 snapshot 就一起沒了（`prod-release-sop.md` §9 坑 17）⇒ D4 刪除舊 PVC 之後，**唯一**的錨點是 D1 的離叢集退役 dump。D1 未完成不得進 D4。
   - cutover B4 建的 `snap-paas-odoo-{pg,fs}-pre…` 兩顆會隨 D4 一起消失，屬預期（`prod-release-sop.md` §5.1.1）。

## D3 移除舊 release bundle

1. 【agent】確認 Ingress 與穩定 Service 都在 raw bundle `paas-odoo-edge/`（`Ingress/paas-odoo-edge`、`Service/paas-odoo-active`；woow-paas/paas-app-bundle-cd#199）、舊 release 的 `ingress.enabled` 已是 `false`（#200）——所以移除舊 bundle 不會變成第二次 cutover（13439 ②）。
2. 【agent 開 PR・人類 merge】移除 `paas-odoo/` bundle 目錄（舊 Bitnami release）。
   - ⛔ 不動 `paas-odoo-backup/`：它在 cutover PR-C 後只剩 `paas-odoo-backup-pvc`，是新 release 備份的 `existingClaim`（woow-paas/paas-app-bundle-cd#144 PR-C）。
   - Fleet 原始碼查證：Bundle 消失 ⇒ helm uninstall（`keepResources: true` 只刪 release secret）；同目錄改 `releaseName` 也會 uninstall 舊 release（cd#145 留言 18010；rancher/fleet v0.10～main）。helm 管的 PVC `paas-odoo` 會一起被刪（PV Retain 會留下 Released），STS 的 `data-paas-odoo-postgresql-0` 不會；所以 D4 仍逐顆確認、不假設。
3. 【agent】驗：4 個 host（`paas.woowtech.io`、`aiot.woowtech.io`、`www.designsmart.com.tw`、`woowtech-ds.woowtech.io`）`/web/health` 200、`/websocket` 101；4 個 TLS Secret 的 `creationTimestamp` 不變、沒有新的 CertificateRequest／Order（未重簽）；穩定 Service endpoints 不變、只含新 web pod（woow-paas/paas-app-bundle-cd#143 驗收 B3／B4）。

## D4 儲存與 Secret 清理

1. 【人類】刪除舊 PVC `data-paas-odoo-postgresql-0` 與 `paas-odoo`（若 D3 未一併回收）。⚠ **不可逆**；執行前再看一次 D1 的 sha256 回填。
   - PV 為 Retain（#1137 G30），刪 PVC 後會留下 Released PV。
2. 【agent】唯讀盤點 Released PV（cluster-scoped），依 [guard doc](shared-cluster-paas-scope-guard.md) 的分流／溯源做法確認歸屬；回收走 #319 SOP，不在本檔順手刪。
3. 【人類】刪除舊 fleet-local secret `paas-odoo-fleet-secrets`（`fleet-local` 只碰 `paas-*` 前綴資源；CLAUDE.md §kubectl）。
4. 【人類】處置 `paas-odoo-backup-pvc` 根目錄的舊檔：7 份 `paas_odoo_*.pg` 已併入 D1 退役 dump，sha256 回填後刪除（WMO#5 裁決 Q5d）；`woow_prod_*.pg`（舊 `pg-backup` CronJob 的 7 份輪替 dump）〔待裁決：G6，建議最新一份併入退役 dump、其餘刪除〕。
   - 新 release 的備份寫在 `daily/`、`weekly/`、`pre-upgrade/` 子目錄（chart README §備份格式與驗證），不會動到根目錄舊檔。

## D5 保留物

1. 【agent】確認以下物件**不刪**，並在 teardown issue 列出：
   - JCR 內的 Bitnami 鏡像：`bitnamilegacy/odoo:18.0.20250805-debian-12-r9`、`bitnamilegacy/postgresql:17.6.0-debian-12-r4`、`bitnamicharts/odoo:28.2.10`（13439 ⑦；woow-paas/paas-odoo-ci#146）。
   - 穩定 Service 與 raw Ingress（常駐；13439 ②）。
   - `paas-odoo-backup-pvc`（新 release 備份的 `existingClaim`；13786 D7）。
   - Google Drive 上的退役 dump（保留到窗口結束後 90 天；map 裁決）。

## D6 後續銜接

1. 【人類】若平台發版尚未切到只 build `paas-odoo-official`（[cutover runbook](platform-odoo-cutover-runbook.md) B8 第 2 步；〔待裁決：G2〕），在本步切換；之後不再 build Bitnami `paas-odoo`（#1392 留言 17382）。
2. 【agent】列待修清單並另開票（本檔不改寫 SOP；#1231「不在範圍」）：
   - `prod-release-sop.md` 的 S1 三處 pin（:37）對新 chart 已不成立：新 chart 沒有 `module-version-gate` initContainer 寫死的 image（chart README §Pod 啟動順序）。
   - `prod-release-sop.md` §5.1.1 的 PVC 名改成新 release 的 `data-paas-odoo-platform-postgresql-0`／`paas-odoo-platform-data`（#1137 G35），並寫入「每顆 volume 保留最近 3 次發版前 snapshot」（WMO#5 裁決 Q4）與「升級閘 `-u all` 前不自動備份 ⇒ 發版前人工 snapshot＋pre-upgrade 備份」（charts#99 留言 18097）。
   - `prod-release-sop.md` §5.1 的兩段式 `ODOO_SKIP_MODULES_UPDATE` 流程，對 woow-odoo 升級閘不再適用（升級閘以 marker 判斷；14363）。
3. 【agent】Step 2 core 升版指向 #1227 與 woow-paas/paas-odoo-ci#133，本檔不寫；Step 2 的 CD PR 同時把 `postgres.extensions.vector` 改回 `true`（R2-Q6）。

## 相關

- [cutover runbook](platform-odoo-cutover-runbook.md)、[回切 runbook](platform-odoo-cutover-switchback-runbook.md)、[彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)
- [`prod-release-sop.md`](prod-release-sop.md)、[`shared-cluster-paas-scope-guard.md`](shared-cluster-paas-scope-guard.md)
- #1142、#1145、#1227、#1392、#443、woow-paas/paas-app-bundle-cd#143／#144、woow-paas/paas-odoo-ci#146、woow-paas/woow-machines-operator#5
