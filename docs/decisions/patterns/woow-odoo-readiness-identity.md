---
title: "woow-odoo readiness 依 Pod 身分判定（必要組＋例行 Job 豁免＋其他 fail-closed）"
category: decisions
status: active
created: 2026-09-30T02:05:31Z
updated: 2026-09-30T02:45:00Z
related:
  - docs/decisions/patterns/capability-gate-single-source-field.md
---

# woow-odoo readiness 依 Pod 身分判定（必要組＋例行 Job 豁免＋其他 fail-closed）

> 適用：改 **woow-odoo 服務「什麼時候算就緒」** 的任何一段——`CloudService._is_release_ready`
> 的分流、[`src/services/woow_odoo_readiness.py`](../../../src/services/woow_odoo_readiness.py)、
> `_TIMEOUT_HINTS`，或 #1222 D11／#1213 finalize 要判斷「這是不是 woow-odoo release」的地方。
> 也適用於 woow-paas-charts 改動 woow-odoo 的 component／task label、Pod controller kind、
> PG 可否關閉、`replicas`／Recreate、chart 名時（chart 固定契約的下游，見 charts#81）。

## 目的

#1156 的通用規則是「排除收尾中的 Pod 後，所有 active Pod 都 Running＋ready」。woow-odoo
chart 同一個 release 底下還有 CronJob 產生的例行 Job，這條規則對它兩頭都錯（#1251 票面）：

| 負例 | 通用規則的結果 | 應有結果 |
|---|---|---|
| 1：server 不存在，只剩 Running 的 backup Job（或只剩 PG） | `(True, pods ready)`——假綠，而且黏住（`running` 不再被 poll） | 未就緒 |
| 2：server／PG 健康，housekeeping Job Pending | 未就緒——轉移被例行維護卡住 | 就緒 |

本文記錄 2026-09-29 局部設計定案（#1251 resolution c15319，Q5／Q6／Q17／Q18／Q19／Q45）
落地後的規則，以及改這一區時容易寫錯的地方。

## 規則

### 1. 受不受 woow-odoo 契約管理：看**實際** `release.chart`（Q17）

`woow_odoo_contract(status, template.helm_chart_name)` 是唯一的判定，#1251、#1222 D11 分流、
#1213 finalize 共用：

- 同一輪 `get_status` 回應的 `release.chart == 'woow-odoo'` ⇒ **managed**。這個值來自 operator
  對 `helm list` 的 `"<name>-<version>"` 做 `rsplit("-", 1)`，舊 operator 也有；chart 版號因此
  禁止 pre-release 後綴（charts `Chart.yaml`）。
- template 期望 woow-odoo（`helm_chart_name` 最後一段），實際卻不是（含欄位缺席）⇒
  **mismatch** ⇒ `HARD: chart mismatch`。in-place upgrade 換不了 chart，要 fresh install（#1128）。
- 其餘 ⇒ `None`，照舊走 `_is_container_ready`／VM 判定。

⛔ **不要用 `template.slug == 'odoo'` 或 `post_deploy_init_type == 'odoo'` 分流。** cutover 期間
seed 的 `cloud_app_odoo`（slug `odoo`）仍是 Bitnami chart，slug 會同時命中兩種實例；#1222 body
原本的 slug 分流已被 Q17 取代。

### 2. 必要組：server＋PG，身分＝label＋直接 controller owner kind（Q5／Q19）

| component（`app.kubernetes.io/component`） | 直接 owner kind | 條件 |
|---|---|---|
| `server` | `ReplicaSet` | 至少 1 顆 active，且全部 Running、ready n/n（n>0） |
| `postgresql` | `StatefulSet` | 同上 |

label 對、owner 不對（例：Job 擁有、帶 `component=server` 的 Pod）**不算**必要組——沿用 #1129
`validate_fallback_pod` 的語意，不追到 Deployment UID。chart 0.2.2 下 server `replicas ≤ 1`＋
Recreate、PG 寫死 1 副本且不可關閉，所以嚴格定義不會卡死。

### 3. 例行 Job 豁免白名單（Q6）

`component=db-backup` 且 `woow.paas/task ∈ {backup, housekeeping}`，且直接 owner 是 `Job`（與必要組
同一套身分證據，Q19；帶同一組 label 的 ReplicaSet／bare Pod 不是 CronJob 產物，照 §4 fail-closed）。
active 的豁免 Pod **不論 phase**（Pending／Running／Unknown）都不參與判定，但列進 detail
（`exempt routine jobs: …`），讓「server Pending、旁邊正跑著備份 Job 佔著 RWO PVC」這類根因看得出來。
收尾中的 Pod（Succeeded／Failed／帶 deletion_timestamp）照 #1156 一開始就濾掉，不參與也不列——它們不佔
RWO。

- 豁免是安全的：兩支 Job 都用 required podAffinity 綁 server，server 缺席／不可排程／NotReady 時
  必要組會獨立抓到。
- daily 與 pre-upgrade 備份的 label 完全相同，wire 上分不出來。目標狀態下 pre-upgrade 在
  `upgrade_backup` state 執行、不進 poll，所以不需要分——**前提是 operator D11 不早於 #1222
  `upgrade_backup` 上線**。
- 「維護異常」不從 readiness 推導（readiness 只在轉移狀態跑，與 02:00–04:59 的例行 Job 幾乎不
  重疊），改由備份新鮮度另行觀察（#1265）。

### 4. 其他 active Pod：fail-closed（HARD）

`db-restore`（只該出現在 `restoring`，#1213）、未知 component、缺 label、身分證據對不上的 Pod ⇒
`HARD: … unexpected pod outside the woow-odoo contract: <name>[component=… task=… owner=…]`。

### 5. operator 沒回 Pod 身分：fail-closed（Q18）

以 `pod_identity_included` echo 旗標為準、**`is True` 比對**：缺 key（#1263 之前的 operator 的真實
形狀）與 `False` 都回 `HARD: operator lacks pod identity`。上線順序是 operator（#1263）先。

⛔ 不要退回通用判定（負例 1 會回來）；不要從 Pod 的 `component is None` 推斷新舊 operator。

### 6. HARD 的可見性（Q45）

不持久化。每輪 poll 印 warning；`deploying`／`upgrading` 超過 #489 門檻（容器 30 分）後寫進
`error_message`，附 `_TIMEOUT_HINTS` 的方向。⚠ `initializing` 不受 #489 管轄（既有行為，非本票引入）：
woow-odoo 在 `initializing` 期間遇到 HARD 只會一直印 warning，上限由 #1257／#1159 的等待時限（Q10）補上。

woow-odoo 專屬的 hint 有四筆，全部**排在 `_TIMEOUT_HINTS` 最前面**（子字串比對、先比中先用，
至少要在通用的 `pods not ready` 之前）：

| detail 前綴 | 方向 | 來源 |
|---|---|---|
| `HARD: chart mismatch` | fresh install（#1128） | Q45 |
| `HARD: operator lacks pod identity` | 先升級 operator（#1263） | Q45 |
| `HARD: required component missing:` | 查 ReplicaSet／StatefulSet events 的 FailedCreate（ResourceQuota、admission）——只擋 server 時仍有 PG Pod，`no pods found` 蓋不到 | 實作期 S2 裁定 |
| `required pods not running:` | 查 pod events（排程、Multi-Attach、image pull）；detail 列出的豁免備份 Job 可能佔著 RWO PVC | 實作期 S2 裁定 |

Q45 字面只定前兩筆；後兩筆是 review 發現「woow-odoo 最常卡住的兩種情形」原本沒有 hint 或被通用 hint
帶去看 container log，由使用者裁定補上（2026-09-30 實作期裁決，理由記在本票 PR 描述）。其餘 detail：

- `unexpected pod outside the woow-odoo contract:` 刻意不附 hint（detail 已點名是哪顆 Pod）。
- 「Pod 在、Running 但未 ready」的 `required pods not ready: …` **刻意**含 `pods not ready`，
  沿用查 container log／probe 的通用 hint。
- 一顆 Pod 都沒有時沿用通用的 `HARD: no pods found for this release`，接 quota 的 hint。
- 新增 detail 前綴時，`TestWoowOdooContractPredicate.test_each_new_detail_is_matched_by_its_own_hint_first`
  用判定本身產生的 detail 驗「先比中的是自己那筆」——不要手抄字串進測試。

## 分流點只有一個

判定分流在 `CloudService._is_release_ready`——poll（`_poll_and_update_status`）的單一入口
（#423 的教訓：當時 controller 另有一份 poll，只修一邊，租戶打開服務頁就會用另一邊把狀態蓋回去；
#1159 起 list/get 純讀、controller 那份已刪除）。⛔ 不要在 poll 之外另寫 woow-odoo 判定。
mismatch 直接回傳、不跑其後的 DNS 閘；managed 與通用判定就緒後都照舊接 DNS 閘（#648）。

## 刻意不在這裡處理

- **升級時「Recreate 開始前」的提早收斂**：一般 PATCH upgrade 不帶 `--wait`，Deployment controller
  開始 Recreate 前舊 server 仍健康，可能被判就緒。轉給 #1222 當 verify item（可參考 Pod
  `creation_timestamp` 晚於 `last_upgraded_at`，#1263 已在 wire 上）。
- **halted（server=0）**：停權中的 upgrade 維持 `suspended`，cron 只撈轉移狀態，所以不會進這條判定
  （測試釘住）。#1213 finalize 共用本判定時要對 halted（`target_replicas=0`）另開例外。
- **platform profile**：由 Fleet 安裝、不是 cloud_service，不經這條 poll；tenant 沒有 `cron` component。
- **其他 chart**：#1156 通用規則不變。

## 測試

- [`src/tests/test_woow_odoo_readiness_1251.py`](../../../src/tests/test_woow_odoo_readiness_1251.py)：
  cron 與 controller 兩個入口都跑；Pod 一律用 `operator_contract.pod_info()` 組；Q18 同時測缺 key
  （`status_response(pod_identity_included=ABSENT)`）與 `False`——只測 `False` 抓不到
  `.get('pod_identity_included', True)` 這種寫錯的預設。
- wire 形狀的守線在 #1263：`scripts/audit/test_operator_contract_shape.py` 對帳 `PodInfo`、
  status golden。

## 相關

- 設計定案：Gitea `odoo-addons/woow_paas_platform#1251` resolution（c15319）。
- wire：#1263（`PodInfo.component／task／owner_kind`、`pod_identity_included`）。
- 下游共用者：#1222（D11 分流、提早收斂 verify item）、#1213（finalize 共用＋halted 例外）、
  #1265（備份新鮮度）。
- 前身規則：#1156（通用 readiness）、#1129（operator 的 server 身分規則）、#489（轉移逾時與 hint）。
