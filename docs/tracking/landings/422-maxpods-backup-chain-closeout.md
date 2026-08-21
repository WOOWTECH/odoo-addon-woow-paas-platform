# project 422 maxpods backup chain closeout

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_422_maxpods_backup_chain_closeout`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#422 全案收官（2026-07-26）：maxPods 110→200（叢集 440→800）＋意外挖出並修復 18 家租戶備份鏈六層故障；零停機、#422 closed"

**#422 收官（2026-07-26，單日完成）**。原案「maxPods 110→250」執行時發現「Pod 額滿」只是第六層症狀，實際是租戶備份鏈六層故障：①備份 CronJob 掛 app 自己的 RWO data PVC＋control-plane nodeSelector→跨節點永久卡 ContainerCreating（98 顆 100% CROSS-NODE、佔 CP 三台 29% 額度）②腳本無 set -e→假成功且 0-byte 檔把有效舊 dump 從保留輪替擠掉（**壞備份主動摧毀好備份**）③Allow＋無 activeDeadline→每天疊 15 顆 ④[[reference-woow-k3s-netpol-fresh-pod-race]] 空窗 REJECT ⑤4 家 pg17 server vs pg16 pg_dump（**拒匯出比自己新的 server**；woowtech 從未成功的真因）⑥torchmedia 兩套 postgres 混接（host=Odoo、帳密=Hermes）。

**處置**（逐閘、每寫入 Eugene 核可）：閘1 suspend 15 CronJob→閘2 刪 144 active Job（**刪 Job 非刪 pod**，pod 會被 controller 重生）→閘3 方案 A（14 顆專用 backup PVC＋spec 7 項修正）＋硬化（set -euo pipefail＋test -s）＋R-1（pg_isready 60s 重試，NP 空窗對策）→P0（3 家升 postgres:17-alpine、torchmedia T-A 改備 Odoo DB）→閘4 kubelet drop-in `10-maxpods.conf`（maxPods:200）逐台 z0v3w→59mqj→31zix→pggrg（etcd leader 最後）。

**成果**：Pending 147→3、四台 200/200（上限 800）、18/18 家備份當日真實 dump（原僅 4 家有效）、294 pod 零重啟、實際停機 0。**maxPods=200 非 250**：podCIDR /24 僅 ~253 IP，250 餘裕 1.2%；實測就有 24 顆 stale CNI 租約佐證。回滾硬規則＝節點 pod 數 <110 才可移除 drop-in（kubelet restart 重跑 admission、超額 OutOfpods）。

**載體**：woow-machines-operator PR#1（5 commits `6589390`→`ddeff02`，含 manifest source of truth `resources/manifests/odoo-tenant-db-backup/`，已 merge）＋PR#2（review 修正：備份記錄歸檔至 process-log`12. odoo-tenant-db-backup`、maxPods 篇在`2. init-k3s`；**歸屬更正＝這些 tenant 是 elmo 以 agent 直建的 resource、非 PaaS 平台開通**，勿再寫成 paas 租戶）。衍生 #440 備份事件（殘餘：明日 03:30 自然驗證、Hermes DB 備份與否、客戶上報）、#441 kasim fs 損壞（fsck 待排）、#442 監控（驗 dump 非空）、#443 異地備份、#444 R-3 NP 評估。

**教訓**：①lastSuccessfulTime／Job Complete 可以長期謊報——健康指標必須驗產出物本體（dump 非空）②Longhorn `actualSize` 含 fs metadata，**不能當「有資料」證據**（實測 96MiB actualSize＝20K 檔案）③teammate 中途停下回報（Step 3 前 4 家 3 敗即停）比硬跑完珍貴——每次停點都挖出新根因 ④snapshot 由 Eugene 親跑（SOP §5.1.1）、CSI snapshot CRD 不存在→用 longhorn.io/v1beta2 Snapshot CR。相關：[[reference-killmode-process-ha-stack-self-restart]]、[[reference-maestri-recruit-resume-pitfall]]（Gantry 全程用 07-12 標準 command 乾淨無 resume）。
