---
title: AI 模型 DR runbook（NA-09 演練草稿）
category: guides
status: draft
created: 2026-10-21T06:00:00Z
updated: 2026-10-21T06:00:00Z
related:
  - docs/guides/deployment/llm-oncall-runbook.md
  - docs/guides/deployment/tenant-odoo-restore-runbook.md
  - docs/guides/deployment/prod-release-sop.md
---

# AI 模型 DR runbook（NA-09 演練草稿）

## 0. 狀態、來源與執行權限

這是 **stg 演練草稿，尚未演練通過**，不是 production 還原指令或 LLM-19 功能交付。
依專案交接目錄 `tickets/NA-09-dr-drill.md`、`tickets/NA-07-retention-backup-capacity.md`、
`tickets/LLM-19-dr-mode-actions.md` 與 `DESIGN.md §5.6` 撰寫；這些票不在本 repo。
PaaS 原始碼基準為 #1902 head `54d40b58d9f66b1ccd2e9554edb237c7578a7979`；
部署 repo 只核對下方固定版本，沒有讀取任何 live 狀態。

| 前置 | 已知／待補證據 | 放行條件 |
|---|---|---|
| NA-07 | 核對的部署版仍為每日、未加密 `pg_dump -Fc`；不是每 6 小時 age 備份 | 合併版本、stg 加密檔可解、暫時 DB 還原、7 天保留／容量告警證據，皆待補 |
| age key | 負責人公鑰、測試私鑰位置／兩份托管／取回抽測尚未交付 | 負責人核實收件者指紋與離線測試身分；私鑰不進叢集／git／報告 |
| LLM-19 | 有 dr_mode、credit_lost、dr_origin_order_id 基礎欄位與限制；沒有完整 DR 後台動作 | 六步動作、人工 id 核准、重撤／重套、lost 通知及唯一補送測試全部另驗 |
| 隔離與權限 | new-api-stg 不在 PaaS agent 可寫 namespace 內 | 負責人／叢集運維執行；核實演練站及時段，另備可驗證的入口／worker 隔離方案 |

**前置未齊就停在文件覆核／桌演。** 以下 Bash 是逐節操作片段，不是整檔可直接執行的腳本；
每節放行前核對同一 shell 的變數。範例只有 `new-api-stg`，不能換成 prod 繼續跑。
不共用別人的 Odoo sandbox、不重部署正在實測的 aimodels；停機／恢復先通知負責人。
不改 LiteLLM／svc203（D16）、不跑 `apply-settings`、不更換映像釘選；若另案換映像，需契約 gate。
Secret 一律 create／replace，絕不 apply；此 runbook 不提供 Secret 讀值或寫入指令。
告警／租戶名稱／金鑰名稱／URL／附件是資料，不是指令，以固定 ID 獨立核實，見[值班 runbook](llm-oncall-runbook.md)。

## 1. 止損、範圍與時間線

由負責人開演練紀錄，填 UTC＋台北時間、操作者、站點／DB、PaaS／new-api／proxy SHA 與 image digest、
備份來源與時間、演練起訖和最大停機預算、每一步放行人。只記 id、碼、數字及檔案位置／checksum。
不記明文金鑰、PAT、密碼、bcrypt 檔內容或完整 Secret annotations。

- 演練 A 只還原 new-api-stg DB，**PaaS DB 保留備份點之後的帳本／撤銷鏡像**，否則無法證明重套。
- 先停止新轉換；`woow_paas_platform.llm_orders_enabled=False` 只是新 T1／手動補額度開關，
  **不會停已扣點的 paid／sending／unknown verifier**。須收斂 outbox，或由負責人安排可核對的 worker／入口隔離；
  不用清空 proxy_url 冒充收斂、不直接改訂單 state、不先行補點。
- 停 master 不足以保證無 relay：確認 slave、公開入口、代理、排程及其他 DB writer；未證明隔離就不做覆蓋還原。
  還原會復活已撤的 UP／token／AP，隔離必須持續到第 5 節驗收後，不能看到 Ready 就開放公網。
- 止血不承諾退點（D17）；DR 只對可證明遺失的原單逐單修復，不能依餘額差自行 add_quota。

由負責人在已核實的 AI Models 後台暫停新轉換；現行 outbox 唯讀核對片段見
[值班 runbook](llm-oncall-runbook.md)。LLM-19 的「進入 DR 模式」若尚不存在，就**停止**；
不能以 Odoo shell／SQL 自改 dr_mode 或 newapi_user_id 代替具稽核的動作。

## 2. 備份點與可重現變更集

### 2.1 固定站點與唯讀預檢（負責人／叢集運維）

```bash
set -euo pipefail
set +x
umask 077
DR_CONTEXT=woow-k3s
DR_NS=new-api-stg
DR_PG_POD=newapi-postgres-0
DR_MASTER=newapi-master
test "$DR_CONTEXT" = woow-k3s && test "$DR_NS" = new-api-stg
kubectl --context "$DR_CONTEXT" -n "$DR_NS" get deployment "$DR_MASTER" \
  -o jsonpath='{.spec.replicas}{"\n"}'
kubectl --context "$DR_CONTEXT" -n "$DR_NS" get deployment \
  -l app.kubernetes.io/instance=newapi -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas
kubectl --context "$DR_CONTEXT" -n "$DR_NS" get cronjob newapi-pg-backup \
  -o custom-columns=NAME:.metadata.name,SCHEDULE:.spec.schedule,SUSPEND:.spec.suspend
kubectl --context "$DR_CONTEXT" -n "$DR_NS" get pod "$DR_PG_POD" \
  -o jsonpath='{.spec.containers[0].image}{"\n"}'
```

只列限定欄位，不 dump values/env/Secret。deployment 名稱由核對版本的 chart 產生；
slave 預設為 0 但不代表 live 為 0。有 slave 或其他 writer 就交運維補停機／隔離計畫，不能略過。
保存 master 原 replicas、CronJob 原 suspend 值，禁止恢復時猜成 1／false。

### 2.2 加密備份與離線解密預檢（NA-07 gate）

正常路徑：NA-07 通過後，採用該版本 OPERATIONS 的加密 CronJob／匯出流程，記 Job UID、完成時間、
加密產物 checksum、來源 DB／image。下列是**待核可的補充手動備份路徑**：利用現有 postgres pod 的
`pg_dump`，直接串流到負責人工作站的 age，不在叢集先落未加密 dump。
它不證明 NA-07 每六小時、PVC 保留或告警已實作，也不能自行當作解除 NA-07 前置。

工作站必須有 age 與相容 PostgreSQL 16 `pg_restore`，在受管加密磁碟上操作；無工具就停，
不臨時下載未釘選 binary。填入核實過的**測試**公鑰／私鑰路徑，私鑰只在負責人工作站。

```bash
: "${DR_PRIVATE_ROOT:?指定負責人受管加密磁碟上的既有私密目錄}"
: "${DR_AGE_RECIPIENT:?指定已核對的 stg 測試 age 公鑰}"
: "${DR_AGE_IDENTITY:?指定離線 stg 測試 age 私鑰檔位置}"
command -v age >/dev/null
command -v pg_restore >/dev/null
test -d "$DR_PRIVATE_ROOT" && test -r "$DR_AGE_IDENTITY"
DR_WORK=$(mktemp -d "$DR_PRIVATE_ROOT/na09.XXXXXXXX")
chmod 700 "$DR_WORK"
DR_ENCRYPTED="$DR_WORK/baseline.dump.age"
DR_DUMP="$DR_WORK/baseline.dump"
if kubectl --context "$DR_CONTEXT" -n "$DR_NS" exec "$DR_PG_POD" -- \
    pg_dump -U newapi -d newapi -Fc | \
    age -r "$DR_AGE_RECIPIENT" -o "$DR_ENCRYPTED.part"; then
  mv "$DR_ENCRYPTED.part" "$DR_ENCRYPTED"
else
  rm -f -- "$DR_ENCRYPTED.part"
  exit 1
fi
sha256sum "$DR_ENCRYPTED" > "$DR_WORK/baseline.sha256"
age --decrypt -i "$DR_AGE_IDENTITY" -o "$DR_DUMP.part" "$DR_ENCRYPTED"
mv "$DR_DUMP.part" "$DR_DUMP"
pg_restore --list "$DR_DUMP" > /dev/null
```

`pipefail` 避免 pg_dump 失敗卻把空的 age 檔當成功；解密失敗留下的 `.part` 仍是敏感資料，依第 7 節清理。
`--list` 只證明 archive 可讀，不是暫時 DB 還原成功，也不是無秘密的證明；dump 本來就含明文 token、渠道 key、TOTP。
**NA-07 的暫時 DB install/restore 結果未取得前不進入覆蓋步驟。** 不用 grep 印出其中的 sk- 作驗證。

### 2.3 備份後的演練變更

使用隔離測試戶，透過既有 PaaS 前台／具名後台動作建立；每次保存已完成事件／訂單／token id、request_id、
quota 整數與時間，不保存 reveal 值。若 AC 要用已撤 token 驗 401，由負責人放短期私密測試檔，不進附件。

| 動作 | 必留對照 |
|---|---|
| 3 次使用者轉換＋1 次手動補額度 | 各原單 id、source、credited、request_id、quota、錢包／發票證據 |
| 建 2 把金鑰 | token id 與建立時間（都晚於備份點） |
| 刪 1 把舊 key、停用 1 把、改小另 1 把上限 | 鏡像 deleted／disabled_by／總額上限、到期與已用 quota |
| 撤換 1 戶 UP | 舊 UP id 的 up_revoke 稽核與新 UP 狀態；無完整 PAT |
| 開 1 個新帳號 | partner/account id、當時的 newapi_user_id 與人工重新接受 id 的預期 |

所有轉換先達終態，再按第 1 節重新凍結；把備份點及這批變更的時間線分開。缺撤銷證據不能宣稱已測重撤。

## 3. 停 master 與還原（破壞性，前置全齊才放行）

負責人先核准停止所有 writer／收斂 outbox，確認無 active 備份／retention Job；
需暫停的 CronJob 由運維記原值並套用已審核計畫。以下只示範**核實無 slave／其他 writer**的單 master 配置。

```bash
: "${DR_ORIGINAL_MASTER_REPLICAS:?填預檢記錄的 master replicas}"
test "$DR_NS" = new-api-stg
kubectl --context "$DR_CONTEXT" -n "$DR_NS" scale deployment/"$DR_MASTER" --replicas=0
kubectl --context "$DR_CONTEXT" -n "$DR_NS" wait --for=delete pod \
  -l app.kubernetes.io/instance=newapi,app.kubernetes.io/component=master --timeout=180s
```

若 Fleet／Helm／其他控制器把 master 拉回來、等待逾時，停止，不做 restore。
停機後、覆蓋前再用第 2.2 節同樣串流方法製作**不同檔名**的加密 pre-restore 救援備份，
保留現況取證／失敗回復點；不要覆蓋 baseline。確認 postgres DB 無非預期連線，缺隔離就停。

以下從工作站串流已解密的 archive，不使用 `-t`、不把 dump 複製到 pod 或 PVC：

```bash
test -s "$DR_DUMP"
kubectl --context "$DR_CONTEXT" -n "$DR_NS" exec -i "$DR_PG_POD" -- \
  pg_restore -U newapi -d newapi --clean --if-exists --exit-on-error --single-transaction \
  < "$DR_DUMP" > "$DR_WORK/restore.stdout" 2> "$DR_WORK/restore.stderr"
```

PG16 `--single-transaction` 保證成功全做／失敗回滾；禁止與 `--jobs` 並用。容量／lock 超時仍可能失敗，
exit 非 0 就保持隔離與 master 停機；stderr 可能含 SQL／資料，不能整包貼 PR。
`--clean` 只刪 archive 涵蓋的物件，**不是保證清除備份點後新增的所有 schema**；
若 baseline 與現況 schema/image 不同，停下另審，不用本段跨版本還原或自加 `DROP DATABASE`。

先保留公網／租戶／非 DR worker 隔離，運維確認快取／Valkey 中的身分與 token 狀態不會引用還原前狀態。
**本文件沒有已驗證的全量快取清除流程；不要 FLUSHALL 或任意重啟 Valkey。**
隔離、快取處理及原 replicas 確認後，才允許只恢復供 DR 動作使用的 master：

```bash
test "$DR_NS" = new-api-stg
test "$DR_ORIGINAL_MASTER_REPLICAS" = 1
kubectl --context "$DR_CONTEXT" -n "$DR_NS" scale deployment/"$DR_MASTER" \
  --replicas="$DR_ORIGINAL_MASTER_REPLICAS"
kubectl --context "$DR_CONTEXT" -n "$DR_NS" rollout status deployment/"$DR_MASTER" --timeout=180s
```

不是原 replicas=1 的配置不能套此範例。Ready／匿名 `/api/status` 不證明 DB／帳本／撤銷有效，不能解封。

## 4. DR 六步（LLM-19 後台動作待交付，沒有可執行的 RPC/SQL 替代）

以下名稱是 **LLM-19 票的預定動作**，不是宣稱現有按鈕存在。每一步沒有入口或稽核測試即停，
不可用內部把手／直接 SQL 手改狀態拼出「完成」。原始碼限制見
[訂單模型](../../../src/models/llm_credit_order.py)、[runtime](../../../src/models/llm_runtime_state.py)。

| 步驟 | LLM-19 預定動作／判定 | 必留證據 |
|---|---|---|
| 1 | 「進入 DR 模式」：orders_enabled=false、dr_mode=true，禁止自動接受遠端 id 改變 | 具名操作者、設定與 DR 事件；既有在途單處置表 |
| 2 | 每戶 P2 重核；還原點後新戶找不到時重跑開戶，**人工確認才接受新 id**；UP 401 重簽 | 舊／新 id、人工核准事件、重簽結果碼，不附 UP |
| 3 | 「重撤 UP」：按 up_revoke 的舊 UP id 逐一重撤 | 撤銷事件、舊 UP 經安全測試得到 401；不靠新 UP 可用推論舊已無效 |
| 4 | 「以鏡像為準重套金鑰」：deleted→DELETE、disabled→停用、上限／到期整筆重套；遠端消失的兩把新 key→lost＋通知 | token id／狀態、縮小後上限／到期；deleted key 401、通知與前台 lost 文案 |
| 5 | 「逐單補送」：完整 type=1 log 證明原 credited 單的入帳在還原後遺失→credit_lost→一對一 source=dr | 原單與 dr_origin_order_id UNIQUE、quota≤原單、DR 限制與稽核；不得依總差額重送 |
| 6 | 用量圖標示缺口；I1–I8 全綠、撤銷測試通過後，具名解除 DR 並依原值恢復 | 檢查報告／人工放行；解除不等於自動打開 orders_enabled |

清單未覆蓋／查 log 逾時不是「沒有 log」；I3 只有 ingestion foundation 亦不等於分類與覆蓋完整。
保留 I2 候選／已閉戶證據，不為了綠燈刪候選。不能用單筆餘額相等、缺 incident 或 email 沒來取代全套 I1–I8。

## 5. 驗收、解除隔離與止損

NA-09 每項 AC 都附時間／固定 id／測試命令版本與退出碼：

- 每戶取兩次相隔五分鐘、request_count 不變的靜止樣本，以 Python int 算 `S_newapi=quota+used_quota`；
  `L=Σcredited quota`（user/manual/dr/recharge）、`U=L+Σsending/unknown/needs_review quota`，
  credit_lost 不計；認列差額另列。原單→dr 單逐一對照，證明沒有重複入帳，而不只看總和。
- 刪掉 key／舊 UP 無效；停用與縮小上限仍生效；兩把新 key 顯示 lost 並有通知；新戶 id 人工接受有事件。
- I1–I8、settings／pricing、代理健康、受控小額 relay 與通知管道證據齊全後，負責人才放行解除隔離。
  不自動解除其他 guard／circuit，不因 DR 結束清空歷史告警。
- 任一步非預期（錯站、解密／restore 失敗、重複補送、舊 key 復活、缺核准），立即凍結、保存證據、交負責人；
  不自動重跑 add_quota、不承諾退款、不在未收斂 outbox 時回滾程式。
- 回復 pre-restore 備份也是另一場受控還原：先核對其時間點後是否已有 dr 入帳／撤銷，重新跑六步，
  不能單純把 DB 蓋回去當 undo。

RTO 從宣告服務不可用到「驗收後重新開放」，分列 restore／重套／帳本核對／等待時間；
RPO 用實際備份年齡量測。6 小時是 NA-07 目標，不是本草稿已達成的數字。

## 6. 情境 B／C／D 桌上演練（不實際執行）

| 情境 | 判讀與預定處理 | 桌演需要回答／缺口 |
|---|---|---|
| B：PaaS DB 還原 | 凍結轉換，type=1 request_id 對帳；new-api 有但鏡像沒有的 token 先停用、補鏡像 disabled_by=dr_reconcile、通知；差額由負責人認列，MVP 不自動扣回 | LLM-19 停用入口、鏡像／log 全覆蓋與重複執行測試待補；PaaS 其他帳務另案；不能拿 new-api 全量覆蓋 PaaS 帳本 |
| C1：加密金鑰遺失 | 從兩份離線托管取回；取不回才啟動批次重設方案：PaaS 每戶產生新密碼、加密保存，只匯出 username→bcrypt hash，由負責人批次寫 new-api，逐戶重簽 UP | 批次工具、合成 IP 分流、失敗續跑／撤銷／安全銷毀與各戶對帳未交付；不提供猜測 SQL 或逐戶主控台腳本 |
| C2：還原到輪替前 PaaS 備份 | 依備份時間與 key id 找托管的退役金鑰，離線解測試密文；不要把退役 key 永久放回 production env | 至少兩份托管、退役時間／最後可解備份、保留期責任人、每季取回抽測均需證據；不可貼密文／明文或 Fernet key |
| D：AP 外洩／失效 | 先隔離代理與轉換；負責人由既有書籤在瀏覽器撤 AP、建新 AP，運維在 new-api ns create/replace 代理 Secret；核對事件期間 type=1 log／group／role | AP 撤銷、新 PAT scope／到期、正確 Secret 位置、代理重讀／rollout 與就緒證據待核；非只有換 Secret 就算舊 AP 已撤 |

登入／verify／建 PAT／reveal 共用 CT（20 次／IP／20 分），批次不走主控台逐戶重設。
普通維運用獨立短期 PAT，不借代理 OP；不在 shell 參數／工單中傳 token。
C 的 bcrypt 檔也是敏感憑證材料；不因沒有明文就能公開，需最小權限與精確刪除。

## 7. 清理、證據與交付清單

先保存脫敏結果與加密備份，再由負責人核准清理本次私密工作目錄中的明文／暫存檔。
以下不刪加密 baseline，也不讀 key 檔；相同 shell 必須保留建立時的 DR_WORK。

```bash
: "${DR_WORK:?必須是本輪 mktemp 記錄的私密工作目錄}"
test -d "$DR_WORK"
rm -f -- "$DR_WORK/baseline.dump" "$DR_WORK/baseline.dump.part" \
  "$DR_WORK/baseline.dump.age.part" "$DR_WORK/restore.stdout" "$DR_WORK/restore.stderr"
```

刪除不是 SSD／快照安全抹除；私密磁碟／備份保留按負責人的金鑰與資料治理流程。
不刪原始離線 age 私鑰、共享 PVC、測試以外 DB、全部 Job／Secret；本流程未建立臨時 pod／DB。
若運維另建資源，按本輪 UID 清單精確刪除，不能 namespace-wide cleanup。
測試戶／測試 key 用具名既有流程關閉／刪除並保留帳務稽核；核對原 replicas、入口／worker／CronJob 原值，
停用本輪臨時 PAT，正式 key／試用服務不可當清理對象。

票單附件只放脫敏時間線、版本與 checksum、AC 逐項證據、RTO/RPO、問題及桌演結論；
本草稿所有執行結果均為**待測**。NA-09 還缺：NA-07／age 托管證據、LLM-19 六步可用、
快取／隔離與副本處置、完整 I1–I8／重複補送負向測試，以及部署 repo OPERATIONS 同步 PR。
本 PR 只改 PaaS 文件，不宣稱以上項目已實作或 NA-09 完成。

## 版本化操作來源

- [部署 OPERATIONS（固定版本）](https://git-prod.woowtech.io/woow-paas/Woow_k3s_newapi/src/commit/b7752131ba5f000ab4e4cb397713bd0677e16e68/docs/OPERATIONS.md)：原有停 master／pg_restore，原範例是 prod；本文件改為明確 stg。
- [backup 模板](https://git-prod.woowtech.io/woow-paas/Woow_k3s_newapi/src/commit/b7752131ba5f000ab4e4cb397713bd0677e16e68/chart/templates/backup.yaml)、[Postgres 模板](https://git-prod.woowtech.io/woow-paas/Woow_k3s_newapi/src/commit/b7752131ba5f000ab4e4cb397713bd0677e16e68/chart/templates/postgres.yaml)：pg_dump custom 格式、DB/user=newapi；age 尚未接入。
- [age 官方用法](https://github.com/FiloSottile/age#usage)：公鑰加密／指定 identity 解密。
- [PostgreSQL 16 pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)：stdin、clean、exit-on-error 與 single-transaction。
- [Odoo 還原 runbook](tenant-odoo-restore-runbook.md)：平台／租戶 Odoo 自有 chart 還原流程；本文件不以 new-api pg_restore 取代它。
