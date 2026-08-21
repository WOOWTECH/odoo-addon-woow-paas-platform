# project 504 platform db backup landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_504_platform_db_backup_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#504 平台 DB 每日備份落地（CD PR#80 merged）：照抄既有站範本會踩的三個坑、raw-manifest bundle 首例、衍生 #512"

2026-08-01 落地 woow_paas_platform#504：paas-system 平台主 DB 建每日 pg-backup。載體 **paas-app-bundle-cd PR#80**（commit `b98ef72` + `d9522d1`，merge `6a7effd`）。

## 照抄既有 18 站範本會踩的三個坑（每個都是「每天靜默失敗」形態）

1. **image 的 postgres major 版本要跟著本站走**：範本是 `postgres:16-alpine`，但 paas-system server 是 17.6 ⇒ `pg_dump: aborting because of server version mismatch`。判版本要看 image/imageID digest，**不能看 chart label**（`app.kubernetes.io/version: 17.5.0`、`helm.sh/chart: postgresql-16.7.21` 都是 chart 中繼資料、與實際 server 版本無關）。`17-alpine` 是 mutable tag，日後 DB 升 18.x 必須同步改。
2. **pod 要帶 `paas-odoo-postgresql-client: "true"`**：#425 A1 把 pg NetworkPolicy 收成白名單，未列名 pod 連 5432 直接被擋（實測不帶 label ⇒ `no response`／pg_isready exit=2）。走 chart 既有 client label 放行，**不必改 values-prod.yaml**，因而不觸發 Odoo helm upgrade 與 `updateStrategy: Recreate` 停機。
3. **`test -s` 不足以證明備份可用**，見 [[reference_pg_restore_l_false_green_truncated_dump]]。

另：**R-1 pg_isready 等待迴圈是每天都在起作用的必要元件**，不是防禦性冗餘。兩次獨立實證：①驗證期間一個省略 guard 的臨時 pod 真的撞上 NP iptables 程式化空窗（`Connection refused`）；②**首次自動排程的 job log 是「第 2 次嘗試可連通」**——第 1 次 `pg_isready` 就沒過。⇒ 沒有這個迴圈，每日排程有相當機率第一次連線即失敗（`backoffLimit` 或能救回，但那是用重試掩蓋，且會讓失敗記錄變常態噪音而淹掉真失敗）。照抄範本時最容易被當成「可以簡化掉的等待」。註解說的「上限 60 秒」其實是 **60 次嘗試**（pg_isready 預設 `--timeout` 3 秒 ⇒ 最壞約 240 秒）。

## Fleet raw-manifest bundle（本叢集首例）

`fleet.yaml` 只寫 `defaultNamespace`、無 `helm:` 區塊 ⇒ Fleet 視為 raw manifests 直接 apply。GitRepo 未設 `paths:`，Fleet 掃的是**含 fleet.yaml 的目錄**而非所有頂層目錄（`docs/` 無 fleet.yaml 也確實沒有對應 bundle，可作反證）。新增目錄即自動成新 bundle，不必改 GitRepo CRD——merge 後實證成立（第 6 個 bundle 1/1）。

**刻意獨立於 `paas-odoo/`**：後者任何改動都觸發 helm upgrade → Recreate 停機；備份與 Odoo release 生命週期無關，獨立 bundle 可零停機上線與回滾。

`longhorn` SC 是 **Immediate** binding（PVC 15 秒內 Bound）；但 `local-path` 與 `longhorn` **兩個 SC 都帶 default 標記**，PVC 必須顯式寫 `storageClassName`，否則可能落到 node-local 的 local-path。

## 驗收手法

job 手動觸發 19 秒 Complete；還原性驗證掛 backup PVC 跑 `pg_restore -f /dev/null` + TOC 內容檢查（`TABLE DATA` 590 筆、res_partner／sale_order／cloud_service 各在）。**與 merge 前實測值逐項一致**才算數——證明產出品質相同而非驗收專用的好運。RWO PVC 在前一個 job pod 僅 Completed（未刪）時仍掛得上，只是 Pending 約 15 秒等 Longhorn detach/attach。

⚠ 手動 job 會更新 `lastSuccessfulTime` 但**不更新 `lastScheduleTime`** ⇒ 「排程本身跑過」要另外等真實排程時間確認，不能用手動觸發代替。2026-08-02 03:30 UTC 首次排程確認：`lastScheduleTime` 出現、job Complete 31s，log 的 `ls -lh` **同時列出兩份 dump**（手動那份＋排程那份）⇒ 一次證明「排程會自己跑」與「dump 持久化在 PVC 不隨 pod 消失」。**#504 已 closed**（方向 2／3 的 Longhorn recurring job／backup-target／移除假訊號 label 未做，屬叢集運維，建議與 #443 合併處理）。

## 衍生

- **#512**：叢集另有 5 個運作中 prod 客戶站完全沒有備份 CronJob。關鍵在 `puhsu-odoo` 建於 2026-08-01 當天也沒有 ⇒ **新站上線流程本身沒配備份**，只補那 5 站下一個新站照樣漏。盤點易漏原因：兩個既有站命名是 `*-backup` 而非 `pg-backup`。歸屬叢集運維。
- 已知邊界（寫進 PR 與 issue，避免「已有備份」變成免死金牌）：無異地副本（replica 同批節點、Longhorn backup-target 空）、失敗無告警（`ttlSecondsAfterFinished` 連 log 一起回收 ⇒ `failedJobsHistoryLimit` 形同虛設，屬 #442）、不含 global objects（需 `pg_dumpall -g`）與 Odoo filestore。

相關：`docs/guides/deployment/prod-release-sop.md`（該筆逐次紀錄已於 2026-08-04 收斂進 SOP §5.1／§7／§9 後刪除）（#504 的發現來源）、[[reference_prod_values_digest_pin_precedence]]
