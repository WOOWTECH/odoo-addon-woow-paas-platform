---
title: 平台 Odoo 換 chart：回切 runbook（反向搬遷）
category: guides
status: draft
created: 2026-10-04T08:30:00Z
updated: 2026-10-05T17:00:00Z
related: [docs/guides/deployment/platform-odoo-cutover-runbook.md, docs/guides/deployment/platform-odoo-cutover-rehearsal-runbook.md, docs/guides/deployment/platform-odoo-cutover-teardown-runbook.md, docs/guides/deployment/prod-release-sop.md]
---

# 平台 Odoo 換 chart：回切 runbook（反向搬遷）

> 四份同族 runbook：[彩排](platform-odoo-cutover-rehearsal-runbook.md)／[cutover](platform-odoo-cutover-runbook.md)／**回切（本檔）**／[teardown](platform-odoo-cutover-teardown-runbook.md)。票：odoo-addons/woow_paas_platform#1231。檔名不用 rollback：CONTEXT.md:125、:128 把「rollback／回退」保留給 helm revision 回退。

## 狀態

`status: draft`——**先草擬、後定稿**（#1231 票面）。須在 13439 ④ 觸發條件成立前定稿；佔位落地＋彩排 A8 量測回填後改 `status: active`，由發起人覆核。

| # | 佔位 | 狀態 |
|---|---|---|
| S1 | 穩定 Service 名與 raw bundle 目錄 | ✅ `paas-system/paas-odoo-active`＋`paas-odoo-edge/`（woow-paas/paas-app-bundle-cd#199；緊急回切 JSON patch 在 `paas-odoo-edge/fleet.yaml` 檔頭） |
| S2 | 新 release 名與 bundle 目錄 | ✅ `paas-odoo-platform`（cd#145 留言 18089） |
| S3 | 搬運 helper pod 形狀與指令 | ✅ [cutover runbook](platform-odoo-cutover-runbook.md) 附錄 T 的 T1～T4（G3）；寫入端指令見 C3 |
| S4 | 停機前要停的三支 cron 的 xml id | ✅ 見 C2 第 2 步（#1163 scout §5.1） |
| S5 | 回切後新 release 的處置 | ✅ 保留 0 副本、PVC 與 Secret 不刪，等原因釐清與下一次 cutover 決策（G5） |

2026-10-05 定稿輪：原「待驗」的反向 filestore owner 與 Bitnami 路徑已依 #1032 plan:112（2026-09-04 prod 實作）填入，彩排 A8 再驗一次；反向 push 的 pod 形狀隨 G3。

## 操作者圖例

| 標記 | 意義 |
|---|---|
| 【人類】 | 發起人或叢集運維親手執行（13439 ⑧）。⚠ 寫入 `paas-system`（緊急 `kubectl patch` 也算）、CD PR merge、動 `woowtech-odoo` ns 一律【人類】。 |
| 【agent】 | agent 可自行執行：唯讀 get、PR 草稿。 |
| 【agent 執行・人類逐步放行】 | agent 備妥指令，每一步由人類放行（13439 ⑧）。 |
| 【agent 開 PR・人類 merge】 | agent 開 PR，merge 是人工 gate。 |

**步驟行慣例**：以「數字＋句點＋空格」開頭的行是步驟行，每一行帶、而且只帶一個上表標記；其餘說明用 `-` 子項，不帶標記。

## 術語

- 引用 [CONTEXT.md](../../../CONTEXT.md)：**平台 Odoo**（:77）、**備份**（:97；Longhorn snapshot 是「還原錨點」，:122、:125）。
- **回切**：回退窗口內，把平台 Odoo 從新 release 反向搬遷回 Bitnami `paas-odoo` release（13439 ⑤）。不是「還原」（CONTEXT.md:124 定義的是同一服務回到某份備份），也不是 rollback。
- **停機窗口**：本次回切的停機時段（13439 ⑧ 的形狀）；⚠ 不套 CONTEXT「維護窗口」（:118）。
- **回退窗口**：cutover 起 14 天（13439 ③）。

出處縮寫同 [cutover runbook](platform-odoo-cutover-runbook.md) §術語；本檔另用：

| 縮寫 | 位置 |
|---|---|
| 13439、14356、14701 | [#1142 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1142#issuecomment-13439) |
| 13786 | [#1145 resolution](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1145#issuecomment-13786) |
| 14201、14686 | [#1174 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1174#issuecomment-14201) |
| 14632（R2-Qn） | [woow-paas/woow-paas-charts#81 Round 2 resolution](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/issues/81#issuecomment-14632) |
| 14662 | [#1231 留言](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1231#issuecomment-14662) |
| #1032 plan | [`2026-09-03-woowtech-odoo-paas-migration-implementation.md`](../../plans/2026-Q3/2026-09-03-woowtech-odoo-paas-migration-implementation.md) |
| WMO#5 裁決 | [woow-paas/woow-machines-operator#5 留言 18108](https://git-prod.woowtech.io/woow-paas/woow-machines-operator/issues/5#issuecomment-18108)（Q3 snapshot 操作者、Q4 保留） |

## C0 適用與失效條件

1. 【agent】確認仍在回退窗口內（cutover 起 14 天；13439 ③），且窗口內**沒有**發過含新 `src/migrations/<X>/` 的 release：`git diff v<cutover 當下版>..v<現行版> -- src/migrations/` 為空（以整個 release 連坐；14356）。
   - 非空、或已在 #1142 記錄「窗口提前關閉」⇒ 反向搬遷不再保證，**停**，交發起人決定（13439 ③）。
2. 【agent】唯讀查新 `woow_prod` 有沒有 `vector` 型別欄位與 `vector` extension，必須都為 0（R2-Q6 回切前斷言；13439 ⑤）。

   ```sql
   SELECT count(*) FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
    WHERE t.typname = 'vector' AND a.attnum > 0 AND NOT a.attisdropped;
   SELECT count(*) FROM pg_extension WHERE extname = 'vector';
   ```

   - 任一非 0 ⇒ 對應表反向 restore 必然失敗（舊側 `bn_odoo` 不是 superuser、建不了 vector；#1032 plan:75），**停**。
3. 【agent】讀方向檢查訊號：woow-odoo 升級閘的方向檢查是 **exit 78**、訊息 `DB core newer than image`（chart `files/upgrade-gate.sh` 步驟 d）。窗口內 Step 2 未做（13439 ③）⇒ 新 release 不應出現 78；若出現，表示有人改了 image 或 marker，先查清楚再回切。
   - 反向目的地 Bitnami 沒有這道閘；Bitnami 側以 `odooSkipInstall`／`ODOO_SKIP_MODULES_UPDATE` 行為為準（`prod-release-sop.md` §5.1）。

## C1 兩種模式

1. 【人類】(a) 緊急回切：直接 `kubectl --context woow-k3s -n paas-system patch service paas-odoo-active` 套用 `paas-odoo-edge/fleet.yaml` 檔頭的緊急回切 JSON patch（含 `test` op；ports 順序不符會被拒），**同時**把 selector 與兩個 targetPort 改回舊形狀，24h 內補 CD PR 對齊（13439 ⑧；woow-paas/paas-app-bundle-cd#199）。
   - 舊形狀：`app.kubernetes.io/instance: paas-odoo`＋`app.kubernetes.io/name: odoo`（無 component）；80→8069、8072→8072，targetPort 寫數字（cd#143 設計確認，2026-10-05 live 實查；cutover PR-D 描述另附全文）。
   - 只改 selector 不改 targetPort ⇒ 流量打到舊 pod 上不存在的 8080（cd#143 PR-1）。
   - patch 後 Bundle 轉 `Modified` 屬預期；任何新 commit 都會把 git 版本 apply 回去，對齊 PR 必須先於其他變更，處理期間可暫停 GitRepo（cd#143 PR-1）。
   - ⚠ 緊急模式只省略「切流量走 PR」這一步，**不省略 C2～C3**。舊 pod 在 C3 完成前仍是 0 副本；若要立刻恢復服務而跳過反向搬遷，就是丟掉 cutover 後的全部寫入，須人類在 #1142 明示接受（推論自 13439 ⑤＋⑧）。
2. 【agent 開 PR・人類 merge】(b) 計畫回切：照 C2～C7 依序走，C4 的 selector 變更走 CD PR。

## C2 凍結新側

1. 【agent】確認新 release 沒有 Running 的 `component=db-backup` Job（含 `woow.paas/task=housekeeping`；13786 D2；chart README §備份／還原）。
2. 【agent 執行・人類逐步放行】ECPay 前置：停用 `woow_paas_platform.cron_topup_cleanup`、`woow_paas_platform.ir_cron_issue_pending_ecpay_invoices`、`payment.cron_post_process_payment_tx`，記下 pending tx 清單與 T0；⛔ 不 disable provider（#1163 留言 13476 第 9 點；scout §5.1）。
3. 【agent 開 PR・人類 merge】`paas-odoo-platform/` 的新 release **同時** scale `replicas: 0` 與 `cron.replicas: 0`（R2-Q3；14686；14701）。
4. 【agent】驗新 web 與 cron pod 都已消失，新 postgres 仍 Running。
5. 【人類】發起人（eugene）親跑、叢集運維備援：新 release 兩顆 PVC `data-paas-odoo-platform-postgresql-0`、`paas-odoo-platform-data` 的 Longhorn snapshot（名稱 `snap-paas-odoo-platform-{pg,fs}-preswitchback-{YYYYMMDD}`（2026-10-05 發起人裁決 G4，#1495））；保留到回退結束後的處置定案（WMO#5 裁決 Q3、Q4；G5）。
6. 【agent】唯讀驗兩顆 `readyToUse=true` 且 `markRemoved=false`；任一不符 ⇒ STOP（`prod-release-sop.md` §5.1.1）。

## C3 反向搬遷

1. 【agent 執行・人類逐步放行】建 [cutover runbook](platform-odoo-cutover-runbook.md) 附錄 T 的 T1～T3，跑 T4 來源 Job，來源改為新 pg（`paas-odoo-platform-postgresql`）的 `woow_prod` 與新 PVC `paas-odoo-platform-data` 的 `/var/lib/odoo/filestore/woow_prod`；每個 PUT 的 sha256 與來源相符，`pg_restore -f /dev/null` 通過（2026-10-05 發起人裁決 G3，#1495）。⛔ 不用 `kubectl exec … > file`（[pitfall](../../tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md)）。
2. 【agent 執行・人類逐步放行】舊 pg 上把現有 `woow_prod` 改名保留（例 `woow_prod__pre_switchback`），再以 `bn_odoo` 建空的 `woow_prod`（`bn_odoo` 有 `rolcreatedb`，2026-09-04 prod 實作即如此；#1032 plan:112），接著 `pg_restore --no-owner --no-privileges -L <list>`（13439 ⑤；#1032 plan:112）。
   - `<list>`：濾掉 `COMMENT - EXTENSION`、`EXTENSION`、vector 相關條目（13439 ⑤；14201 D2）。
   - `bn_odoo` 不是 superuser（#1032 plan:75、:112）。
   - ⛔ 不用 `--no-comments`（Odoo 18 `_sql_constraints` 存在 `COMMENT ON CONSTRAINT`；14201 D2）。
   - 改名保留是本檔的推論（比照 14201 的「改名切換、`__old` 保留」）；舊庫在 C7 確認回切成功後才 DROP。
   - 執行位置：一次性 Job，`paas-postgres` image，唯讀掛 T1、讀 `paas-odoo-postgresql` Secret 的 `bn_odoo` 密碼（secretKeyRef，不上 argv），label `app.kubernetes.io/component: transfer-source`（T3 已放行到舊 pg）；先跑 `sha256sum --strict -c SHA256SUMS` 與 `pg_restore -f /dev/null` 再動 DB（附錄 T）。
   - 驗反向 restore 零錯誤。
3. 【agent】唯讀驗：舊 pg 上 `database.secret`／`database.uuid`、admin login／password、`ir_module_module` 全表指紋，與新側凍結當下相同。
4. 【agent 執行・人類逐步放行】filestore 全量 push（不用增量 rsync）新 PVC `/var/lib/odoo/filestore/woow_prod` → 舊 PVC `paas-odoo` 的 `/bitnami/odoo/data/filestore/woow_prod`（先把現有目錄改名保留，與 C3 第 2 步同理）；檔數＋`find -type f -printf '%s\n'` 位元組總和兩端相等；再 `chown -R 999:0`（2026-09-04 prod 實作值，#1032 plan:112；13439 ⑤ 原寫 `999:999`，以實作為準，彩排 A8 第 2 步再量一次）。
5. 【agent】確認 Bitnami DR 槍不會擊發：舊 PVC 仍在、`ODOO_DATABASE_NAME=bitnami_odoo`＋`odooSkipInstall=false` 在 PVC 存在時不會 init（13439 ③；#1137 G05）。

## C4 切回流量

1. 【agent 開 PR・人類 merge】`paas-odoo/values-prod.yaml` 刪掉 `replicaCount: 0`（或改 1）——窗口內唯一允許對舊 release 做的變更種類（13439 ③；cd#144 PR-B）。
   - 舊 release 維持 cutover 前的 image，⛔ 不改 pin：窗口內不雙 build、禁含 migration 的發版，所以回切後只有 addon 程式碼較舊，DB 結構不受影響（2026-10-04 發起人裁決，維持 13439 ③）。
2. 【agent】驗舊 odoo pod Ready、log 沒有 init（DR 槍未擊發）。
3. 【agent 開 PR・人類 merge】穩定 Service selector 鍵集合＋targetPort 回到舊形狀（80→8069、8072→8072）。⚠ 不可用具名 targetPort（13439 ②）。緊急模式已 patch 者，本步就是 24h 內的對齊 PR。
4. 【agent】驗收同 [cutover runbook](platform-odoo-cutover-runbook.md) B7 第 2 步：endpoints 非 0 且只含舊 pod IP、4 host `/web/health` 200、`/websocket` 101（`--http1.1`＋`Origin`，#1032 plan:126）、OAuth、`/woow`、operator webhook。
5. 【人類】以內部帳號登入抽查；請 woow-machines-operator 確認 mcp-odoo 正常。
6. 【agent 執行・人類逐步放行】ECPay 收尾同 cutover B7 第 4 步（13476 第 9 點）。

## C5 備份回復

1. 【agent 開 PR・人類 merge】同一張 PR（避免兩支 CronJob 共寫 `paas-odoo-backup-pvc`、互相輪替刪檔；13786 D7）：
   - 重新 apply `paas-odoo-backup/pg-backup-cronjob.yaml`（取 cutover PR-C 描述記下的最後 commit sha；13786 D7「回切 runbook 必含」）。
   - 新 release `backup.enabled: false`（同時停用 backup 與 housekeeping；chart README）。
2. 【agent】驗：`paas-system` 內指向 `paas-odoo-backup-pvc` 的 CronJob 只有一支；手動觸發一次舊 CronJob 成功（`paas-odoo-backup/` 形狀見 woow-paas/paas-app-bundle-cd#144 背景「備份」）。

## C6 消費者

1. 【agent】確認不需改的消費者：穩定 Service 名不變 ⇒ paas-operator `odooBaseUrl`、mcp-odoo、raw Ingress 都不動（13439 ②）。
2. 【agent】再盤點一次 prod 存活 agent-runtime pod 的 `ODOO_BASE_URL`（只取該 env 的 jsonpath；13439 checklist 補充項）。

## C7 事後

1. 【agent】在 #1142 與 woow-paas/paas-app-bundle-cd#144 留言記錄：回切原因、各步時間戳、實測停機 vs 彩排量測、C0 斷言結果。
2. 【人類】新 release 維持兩個 component 0 副本，PVC 與 fleet-local secret 不刪，等原因釐清與下一次 cutover 決策（2026-10-05 發起人裁決 G5，#1495）；若之後決定放棄，另走 teardown 同形的人工刪 PVC／Released PV 流程。
3. 【agent 執行・人類逐步放行】**再次 cutover 前**，重寫或刪除 `woow_prod` 的 `woow_paas_platform.upgrade_done`／`upgrade_attempt`：預寫的 marker 會隨反向 dump 回到 Bitnami 側；第二次 cutover 若換了 image digest，升級閘會自動 `-u all`（R2「本輪不裁」第 2 條；14662 C7）。
4. 【人類】確認回切穩定後，DROP C3 第 2 步改名保留的舊庫；回退窗口重算（下一次 cutover 從頭起算 14 天，13439 ③）。

## 相關

- [cutover runbook](platform-odoo-cutover-runbook.md)、[彩排 runbook](platform-odoo-cutover-rehearsal-runbook.md)、[teardown runbook](platform-odoo-cutover-teardown-runbook.md)
- [`prod-release-sop.md`](prod-release-sop.md)、[`shared-cluster-paas-scope-guard.md`](shared-cluster-paas-scope-guard.md)
- #1142、#1145、#1163、#1174、#1217、#1392、woow-paas/paas-app-bundle-cd#143／#144
