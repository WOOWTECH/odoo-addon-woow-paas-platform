---
title: post-deploy init 的執行模型（欠 init、attempt、lease、CAS、失敗分類）
category: decisions
status: active
created: 2026-09-30
updated: 2026-09-30
related:
  - docs/reference/features/cloud-services.md
  - docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md
  - docs/tracking/pitfalls/reaped-since-single-path-stamp-gc-immunity.md
---

# Decision: post-deploy init 的執行模型

**Date**: 2026-09-30
**Status**: accepted（設計定案：[#1257 resolution c15293](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1257#issuecomment-15293)、[#1159 resolution c15307](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1159#issuecomment-15307)；實作 #1257，執行面 #1159）
**Components**: `woow_paas_platform.cloud_service`（欠 init／lease 欄位、`_run_post_deploy_init`、短 poll 與 init cron）、`woow_paas_platform.cloud_service_init_attempt`、`woow_paas_platform.platform_monitor`（#1159 監控掛點）、`controllers/paas.py`（list/get 純讀、reconfigure、rollback）、`services/paas_operator.py`（init 預算）、`data/cloud_service_cron.xml`、`src/migrations/18.0.1.0.190/`

名詞：**初始化**、**欠初始化／初始化代數**、**結果未知**見 [`CONTEXT.md`](../../../CONTEXT.md)。

## Context

需要 post-deploy init 的服務（template `post_deploy_init_type` 為 `odoo` 或 `n8n`）在 pods ready 之後要再做一次
init：Odoo 刷新模組清單、n8n 建 owner 與 API key。#1257 之前：

- **兩份 init 流程**：controller（list/get 同步執行）與 model（cron）各一份，失敗處理與寫入身分各自漂移。
- **沒有互斥**：list/get 每 5 秒一次、cron 每分鐘一次、prod 還有 2 個 operator replica——同一台服務可能同時被送出多次
  init。core 的 `FOR NO KEY UPDATE SKIP LOCKED` 只保證**同一筆 ir.cron、worker 活著時**不並行，擋不住 worker 被殺
  之後重跑、不同 cron 記錄、HTTP 入口。
- **同步 HTTP**：Odoo init 的 client 預算是 `(120, 900)`，前端 jsonRpc 30 秒就 abort，後端 worker 卻還在等。
- **`success` 布林不夠用**：同一個 `[update_module_list]` step 同時包含「exec timeout（遠端可能已執行）」與
  「腳本明確回報失敗」；n8n 的 Secret patch 失敗時回 `success=false` 卻帶著已建立的 `api_key`，平台直接丟掉它。
- **init 會被跳過**：停權把轉移中的服務記成 `running`（#1266）；reconfigure 不改 addon 時把 `addon_update_pending`
  覆寫成 False、rollback 根本不設它（#1267）——三條路都讓欠 init 的服務直接顯示成健康。

## Decision

### 1. 欠 init 是持久標記，按「代」計額度（Q9／Q24）

- `init_pending`（欠 init）是**唯一**判準：pods ready 之後欠 init 就去 `initializing`，否則 `running`
  （`CloudService._next_state_when_ready`，背景 poll 使用；#1159 起 list/get 純讀、不再 poll）。
  `addon_update_pending` 不再決定去向。
- 每次需要 init 算一代：`_open_init_generation()` 是唯一開代入口，歸零本代計數。

| 會開新一代 | 不開新一代 |
|---|---|
| create（init 類型不是 none）、addon 變更、人工 redeploy、**還欠 init 時的** rollback（含額度用完的 error）、從**額度出口**的 error 出發的 reconfigure（#1159 Q30）、#1222 的 digest 升級 | resume、reset、不改 addon 的 reconfigure、**init 已完成服務的** rollback、restore finalize（待 #1213 確認） |

rollback 的條件是實作期裁定（#1257 review r1 M2 (A)），修訂 Q23／Q24 的字面「rollback 開新一代」：init 已完成的
服務 rollback 不重跑 init（守住 #1267 驗收 5）。否則 n8n 每次 rollback 都多建一把 API key 並重啟，租戶改過 owner
密碼時還會 login 失敗到額度用完、沒有出口。代價：Odoo rollback 到 addon 組合不同的 revision 不會自動
`update_list`——與 #1257 以前的行為一致。

- 只有 init 成功會清 `init_pending`；停權、resume、不改 addon 的 reconfigure 都保留它。

### 2. attempt＋lease：claim 在呼叫 operator 之前 commit（Q2／Q32／Q36）

單一入口 `CloudService._run_post_deploy_init(client)`，只由 init cron（`_cron_run_post_deploy_inits`，#1159）呼叫：

```
commit（poll 剛寫的 state 先落地）
→ 收斂過期 lease（在途 attempt → unknown，legacy／舊一代不計）→ commit
→ 前置條件：仍在 initializing、沒有 redeploy 意圖、額度未用完、等待未逾時
→ claim：單一條件式 UPDATE（lease 為空才取得）＋ 插 attempt → commit
→ 呼叫 operator
→ 先寫 attempt 結果（n8n 拿到的 key 同時存下）→ commit
→ CAS：attempt 仍持有 lease 且服務仍在 initializing 才套用 → commit
```

- 撞到 40001 或 `lock_timeout` 視為「沒拿到」，不算失敗。時鐘一律用 DB（`statement_timestamp()`；`now()` 是交易開始
  時間，cron 的長交易裡會落後）。
- lease 長度＝client 預算（connect＋read）＋180s，直接引用同一組常數：**Odoo 1200s、n8n 900s**。上界要小於承載它的
  runtime 的 cron 硬上限（prod `limit_time_real_cron=1800`，woow-paas/paas-app-bundle-cd#154；遷移後 platform profile
  ≥1800，woow-paas/woow-paas-charts#81）。
- **不承諾 exactly-once**：lease 擋「同一時間兩條連線各送一次」與 worker 被殺之後的立即重跑；「結果未知之後的重送」
  由 unknown 額度封頂。operator 不新增 operation API、不改 init 執行語意，只加 additive 回應欄位（#1263）。

### 3. 失敗分類看「遠端副作用是否可能已發生」（Q3／Q8／Q11）

| outcome | 判定 | 額度 | lease |
|---|---|---|---|
| success | `success=true`（n8n 另須帶 `api_key`） | 清零、`init_pending` 清掉 | 釋放 |
| failed（確定失敗） | operator `error_kind=failed` | 每代 5 次，第 5 次轉 error | 釋放（下一輪可重送） |
| partial（部分成功） | n8n `success=false` 但帶 `api_key`；key 先存 | 與 unknown 共用每代 2 次 | **留到到期** |
| unknown（結果未知） | exec timeout、空輸出、JSON 無效、HTTP 500、read timeout、連線中途斷、lease 到期、**缺 `error_kind` 的舊 operator** | 與 partial 共用每代 2 次，第 2 次轉 error | **留到到期**（重送前必須等） |
| not_executed（等待） | operator `error_kind=not_executed`（`find_pod`／`wait_ready`）；平台端只有 connect 階段失敗（`ConnectTimeout`、`NewConnectionError`） | 不耗 | 釋放 |
| （本地記帳錯誤） | 套用結果時平台自己的例外 | 不耗、`_logger.exception` 告警 | 釋放 |

- **分工**：時間只管等待，次數只管結果。等待的錨點 `init_wait_since`＝「進入 initializing」、「上次實際執行的
  attempt 結束」與「最近一次觀測到 pods ready」三者最晚者（進入的那一刻由 `write()` 集中 stamp；第三項是 #1159
  實作期裁定 D2 對 Q10 的擴充：排在 init cron 佇列裡等候屬「平台容量排隊」，不計時），時限預設 30 分；只在短 poll
  觀測到 not ready 時判，到期轉 error、不耗額度。init cron claim 前只判額度。
- #1192 的 fast-fail 短路（#1159 D1）：not ready 且任一非 Job 擁有的 Pod 自身未 ready、`restarts ≥ 3` ⇒ 轉 error
  （`init_exit='fast_fail'`，不耗額度）。#1192 上線後把 CrashLoopBackOff／initContainer 訊號接進同一判定
  （`_init_fast_fail_pod`）。
- `initializing` 一定會結束，出口四個，都轉 error 並記 `init_exit`：`failed`、`unknown`、`wait_timeout`、`fast_fail`。
- 可調的只有兩個 `ir.config_parameter`：`woow_paas_platform.init_unknown_limit`（預設 2）與
  `woow_paas_platform.init_wait_timeout_minutes`（預設 30）；lease 長度與 failed 上限是常數。
- attempt 記錄（`cloud_service_init_attempt`）只給 `base.group_system`，存截斷並遮罩後的錯誤，**不存 api_key**；保留
  每服務最近 20 筆＋當代全部，超過 90 天（當代除外）每日清掉，服務刪除時 cascade。

### 4. 各入口的守衛（Q9／Q23／Q25／Q27／#1159 Q30）

- **list/get**：**純讀**（#1159 Q1）——不呼叫 operator、不寫入（含 DNS 旗標），回 `observed_at`／`init_phase`。
- **reconfigure／rollback**：lease 持有期間拒絕（可讀錯誤）。reconfigure 不覆寫欠 init；還欠 init 的 rollback 開新一代。
- **error 的 reconfigure**：只有額度出口（`init_exit` 為 `failed`／`unknown`，`_init_reconfigure_exit`）放行並一律開新一代；helm 失敗、等待逾時、fast-fail
  照擋。addon 讓 pod 起不來屬 upgrading 逾時轉 error，租戶出口是 rollback（後端允許從 error rollback）。
- **redeploy（admin 後台）**：lease 在途時不 raise，登記 `init_redeploy_requested` 並回 notification；背景看到意圖就
  停止自動重送，lease 結束後 cron 先執行 redeploy。v1 不做強制撤銷。
- **suspend／resume**：停權如實記下轉移狀態、不清欠 init。resume 時欠 init 且停權前不是 `error` 就導回
  `initializing`（含停權前 `running` 但停權期間開了新一代的情形）；`error` 維持 `error`、額度不重置；不開新一代。

### 5. 上線過渡（Q15／Q26）

`18.0.1.0.190` 的 post-migrate：回填欠 init（`upgrading`＋`addon_update_pending`、`pending`／`deploying` 且 init 類型
不是 none、`initializing`）；每台 `initializing` 寫一筆 outcome=legacy 的合成 attempt 與 1200s lease、`init_retries`
歸 0；`error` 不動；log 列出 init 類型不是 none 的停權服務交人工抽查。新舊 code 並存由 Recreate＋cron-wait 擋住，舊
process 在途的 remote exec 由合成 lease 擋住。

## 執行面（#1159）

#1257（交付順序 3）與 #1159（交付順序 4）分兩個 PR 依序 merge，但**必須同一次 release 上線**，且
woow-paas/paas-app-bundle-cd#154（prod `limit_time_real_cron=1800`）要先上 prod：沒有它，init cron 在 prod 的強制
終止時限是 `limit_time_real`=1200，容不下 wall 預算 1100s＋sleep＋同趟其他 job。

- **短 poll**（`ir_cron_check_deploying_services`，每分鐘）：「由觀測推進、離開轉移狀態」的**唯一**寫入者——
  transition 完成、init 分派（`init_ready_observed`／`init_ready_since`，由 not ready 轉 ready 時 trigger init
  cron）、deleting 404→unlink（#1159 起第一次啟用）。每台服務一個回捲單位。拿到 status 就 stamp `observed_at`。
- **init cron**（`ir_cron_run_post_deploy_inits`，每分鐘＋自我 `_trigger()`）：開頭執行 redeploy 意圖（Q27）；
  依 n8n 優先（Q28a）、其次最早觀測到 ready 的順序 claim；每 tick wall 預算 B＝1100s（已用時間＋該類最壞 attempt：
  Odoo 1020／n8n 720）與軟上限 10 筆；回報 `_notify_progress(remaining=0)`，有積壓就自我 `_trigger()`——不回報
  `remaining>0`（core 會在同一趟、同一個 watchdog 內續跑最多 10 批）。init 最多佔 1 個 cron worker（SKIP LOCKED
  單例），不與短 poll 同 job、同交易（Q4）。
- **list/get 純讀**（Q1）；前端 5 秒輪詢照舊、只讀 DB。`init_phase`：running（lease 在途、attempt 未結束）／
  waiting_ready／retrying（本代有 failed 或 unknown）／queued。前端只在轉移中且最近觀測超過 3 分鐘時顯示「最後更新
  X 前」（秒數由後端算：`observed_at` 是 naive UTC，前端日期層目前把不帶 offset 的字串當本地時間讀，#914）。
- **DNS 旗標**（Q16／D4）：`public_url_dns_unverified` 改由 `ir_cron_reprobe_public_url_dns`（每 15 分）重探後清除。
- **監控**（Q38／D3）：`mail.activity` 給 `base.group_system`，每個監控項用不同 summary 各自去重，全部掛在
  mail.thread 單例 `woow_paas_platform.platform_monitor`（`ir.cron`／`cloud_service` 都不是 mail.thread，
  activity 建立時會呼叫 `message_subscribe`）。短 poll 查 init cron（停用或 `lastcall` > 40 分）、最舊 `observed_at`
  （> 15 分）、24h 內 unknown／partial；init cron 查短 poll（`lastcall` > 10 分）；等待逾時／fast-fail 與 n8n
  ready→owner（> 5 分，Q28b）是事件型，追加一行。
- **租戶出口**（Q30）：initializing 可刪除（確認框註明已送出的 init 仍可能完成）；error 停用 Reset；init 失敗顯示
  固定文案＋attempt 序號＋support 入口；只有額度出口的 error 開放改設定。

## Consequences

- n8n 每次重送一定再建一把 API key：最壞殘留 2 把孤兒 key＋2 次 restart（孤兒 key 能否撤銷未驗）。
- 停權期間已被舊 code 吃掉的 init 沒有資料可推回來，只能靠 migration log 人工抽查。
- init 失敗的出口多了一個「等待逾時」：pod 30 分鐘都沒 ready 的 initializing 服務會轉 error（以前會永遠停在
  initializing）。
- operator 回 401／403／422（例如平台的 operator API key 設錯）會被歸成 unknown（Q11：平台端只有 connect 階段
  失敗算 not_executed）⇒ 所有 initializing 的服務兩輪之後都會轉 error。這是保守分類的已知代價，靠 #1159 Q38
  的監控（24h unknown 次數）及早發現。
- 由觀測推進的轉移最多延遲一個短 poll 週期（約 60 秒）；前端輪詢不再加速收斂。
- fast-fail 以 `restarts ≥ 3` 判斷，已知誤判面：Pod 早期重啟過 3 次、之後只是短暫 not ready 也會命中（租戶出口是
  rollback）。#1192 上線後以 CrashLoopBackOff／initContainer 訊號取代。
- deleting 但 release 一直存在（uninstall 沒送成功）時沒有任何路徑會重送 uninstall（#1279）。
- 監控 activity 建立時 core 會寄通知信給被指派者（`action_notify`）。
- n8n client 預算改為專屬 `(120, 600)`，由 repo-lint `init-n8n-budget-ladder` 從 operator 原始碼的 AST 算 envelope
  （目前 531s）守住「read > envelope」；Odoo 維持 `(120, 900)`（`init-odoo-budget-ladder`，envelope 805s）。

## 未驗（交給上線後觀察，不是待決）

n8n init 實測耗時、兩次 `update_list` 並行時的行為、n8n 孤兒 key 能否撤銷、prod 各 cron 的實際耗時。
