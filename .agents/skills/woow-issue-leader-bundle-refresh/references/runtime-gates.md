# Runtime gates：stored-only 與 cold-cut

由 [skill Step 3／5](../SKILL.md) 進入；這是 session 分類的唯一權威，不是 recovery script。
目的：安全更新 stored content，不為通過 gate 喚醒舊 sessions，也不把離線當成工作已結束。
本 reference 不授權 archive、resume、rebind、停止 runner／REPL、通知重送或額外 model turn。

## 1. 先完成 scope，再分類

完整分頁列出 shared agent 的所有 bindings（`kind=any`、含 archived），另列 admission roots
與 descendants。Shared PUT 會影響非 admission 的 bindings；封存 root 不會自動封存或解除
其 children 的 agent binding。每個 session 都須分類，不能用 messaging member 數代替 scope。

記錄實際 owner（authenticated list，不是 viewer 權限）、agent ID/version、host／workspace、
parent/archive、runner binding、harness、model override、status 與 pending／background 訊號。
API 沒有欄位、讀取失敗或 supervisor 不能可靠對應時，保留「未知」，不能補成 idle／零。

### 證據要求

- API：status、active response、pending input／elicitation、terminal pending；完整 descendants
  與 receipts／registration 的 dispatching，含會喚醒相關 sessions 的未完成通知／排隊工作。
- Process／supervisor：核對實際 host、runner 身分及 liveness、manager／harness／工作 process
  tree，包含已脫離 parent 的 background work。只記安全 hashes／身分摘要，不輸出 auth env。
  `ps | grep` 沒結果、host online 或 idle 任一單獨訊號都不足。
- Telemetry：依部署版本的 schema 解讀 `background_task_count`。Omnigent 0.14 的 `None`
  表示沒有 tracked shells，不等於所有 OS 工作為零；記錄 harness 覆蓋與 server restart 的
  限制，以 API／process／工作來源的獨立證據補足。其他版本重新查 contract，不套用此語意。
- 工作：列出尚未完成的 issue／交接、外部觸發（user REPL、queue、scheduled tasks、其他
  operator）與續辦方式。保留工作也可 stored-only；不能因無 process 就宣稱工作已完成。

## 2. 分類與優先序

先判已知活動，再判未知；只有兩者均排除，才按 archive／runner 狀態分類。

| 分類 | 必要條件 | 計畫 gate |
| --- | --- | --- |
| `active` | Running／waiting、active response、pending input／approval、terminal pending、實際 background work，或相關 dispatching | `BLOCKED`；另案排空後重做 plan |
| `unknown` | 關鍵 API／binding／工作／process 證據不足、相互矛盾，或 `failed` 未有具體處置 | `BLOCKED`；核對原因，不自行恢复或清狀態 |
| `idle-online` | Runner online，無上述活動／未知；完整 scope／context 證據 | 可規劃，但須有下節的 cold-cut 方案 |
| `dormant-offline` | Runner 明確 offline、session idle、無活動／未知，且符合下節全部 dormant 證據 | 可規劃 stored-only，不要求 wake turn |
| `archived` | 已封存，無活動／未知，完整 collateral 與未來使用安排 | 可列為保留；不 unarchive／喚醒／重送 |

Archived 若仍有 pending elicitation／work，仍先判 `active`；若能證明是無執行可能的歷史記錄，
須附版本 contract、沒有排隊重播／自動觸發的證據及 owner 保留決策，才按 `archived` 處理。
Registration 的歷史 `unknown`／`rejected` 不是自動重送授權；`pending` 是否會在 proposed
scope 下投遞，必須 projection。可能投遞則列為通知副作用；不能標 dormant。

## 3. Dormant stored-only：全部滿足才可規劃

1. API／process／工作證據符合 `dormant-offline`；runner 未在線不代表 host 無法自動 relaunch。
   核對相關 host 的可達性與 future-wake 契約；host 無法查核就 `unknown`。
2. 明確對應 host／runner／supervisor，確認無相關 live harness 或背景工作；manager/spec cache
   的存在或未知另列。沒有 harness process **不是所有 cache／歷史 context 消失的證明**。
3. 計畫涵蓋每個 shared collateral ID，包括非 admission 的 child／archived binding；工作
   未完時仍有交接與下次使用安排，沒有依賴本次立即恢復的工作。
4. 本次保持 offline，無 projected 首次通知／wake／未排空事件可喚醒它；不得以送 message
   測試 liveness。若可能自動喚醒，先另案消除該風險並重做 plan。
5. Owner 核准具體 IDs 的 stored-only 範圍、舊 history／override 的續存、尚未驗證的載入結果
   及下次使用前的 gate。這只是 Step 4 的方案條件，不免除 plan digest／rollback 核准。

Offline anchor 可以在以上條件滿足後作為 API 定位點，不需為 PUT 把 anchor 喚醒。
舊 REPL 能重新送工作時須有可核對的操作凍結安排；需要關閉它則另案核准、執行後重做 plan。
不預設封存 dormant sessions；archive 會改 admission／撤權，屬另一份有完整 IDs 的計畫。

## 4. Idle-online cold-cut

列出凍結哪些 IDs／觸發來源、誰負責、時間窗、如何證明生效，以及下次開放派工的條件。
口頭「不會有人用」不能替代對照現行 queues／REPL／scheduled tasks／operator 的證據。

- 記錄部署版本／實際 harness 的 reload 或 respawn 契約、可重現的 source／runtime 證據。
  Root 的切換不能推論 child 的切換；不以 SDK 或某個 native harness 的證據代表全部 harness。
- 有核准的 turn-boundary 策略才可保留 online runner。沒有可驗證策略、live cached context
  不明或需要立即切到新版時，`BLOCKED`；另案 cold-stop／recovery／fresh migration 後重做 plan。
- 明列 conversation history、model overrides 仍保留；新 stored default 不保證蓋過 override。
  禁止把 PUT、agent version 增加或「無 harness process」當成新版已生效。
- 本次 skill 不替其他 operator 禁用排程或關閉 REPL；方案若需要這些 mutation，先另案授權。

## 5. Freshness、owner gate 與驗收

Plan 每個 ID 記：分類、原因、API／process／工作證據路徑與時間、telemetry 限制、before／
projected-after group、第一通知、cold-cut／future-use 策略與負責者。列明所有 collateral、
分類所依賴的 freezes 與 owner 核准；設定具體 evidence 有效時間窗，不能無期限信任舊 PID。

Apply 前 fresh 核對完整 scope、bindings、活動、觸發來源及方案前提。過期、分類／group／
model override／通知目標／任何切換前提 drift 即停止、重做 plan；owner 核准不補缺證據。
停止 bridge 前再次確認 no dispatching；freeze 需維持到本次內容／rule／continuity 驗收完成。
Freeze 在 plan 階段尚未建立時，只能列作明確的 apply 前置條件；實際 apply 查不到證據就停止。

驗收分兩部分：

- **Stored／admission**：candidate fingerprint、rule 配對、兩輪 fresh discovery、完整
  groups／撤權／新納管結果、credentials／receipts／notice IDs continuity；保留 dormant
  offline 狀態，無未核准通知或 model turn。意外喚醒／活動屬 drift，保存 evidence 並停止。
- **Runtime adoption**：逐 ID 列為「本次未驗證」，或附核准 cold-cut 實際證據。Dormant 下次
  使用前重驗 agent version、harness、override 與載入契約；需要 fresh session／model turn
  時另案核准。不能把 stored-only 的 `DEPLOYED` 寫成所有 sessions 已載入新版。

Pricing／model 可用性證據與 fresh smoke 不因本文件修訂而解除；runtime-only 決策不授權
移除 cost guards。Rollback 只在已核准的 bundle／config／bridge 範圍內，ledger 不回寫。

## 6. Contract 驗收案例

這是 operator／reviewer 的決策案例，不是自動執行的分類器或 live runtime 測試。
PR 覆核時逐案對照上方權威條件；文件 link／字串檢查通過不代表這些判斷已自動驗證。

| 案例 | 輸入／證據 | 必須結果 |
| --- | --- | --- |
| D1 | Offline idle、binding／process／工作及觸發證據完整，無通知；owner 指定 ID 並接受 future-use gate | 可規劃 stored-only；保持 offline，Step 4 核准 digest 後才 apply |
| D2 | D1 但有 pending input／elicitation／dispatching | `active`、`BLOCKED`，不 wake／清除來湊 PASS |
| D3 | 只有 `background_task_count=None`／idle／無 ps 命中 | `unknown`、`BLOCKED`；no tracked shells 不當零活動 |
| D4 | Process 不存在，但 host／runner mapping 無法證實 | `unknown`、`BLOCKED` |
| D5 | 只盤點 admission 22 members，漏掉 shared 39 bindings 的額外 IDs | Scope 不完整、`BLOCKED`，不可生成可 apply plan |
| D6 | Online idle，但 native child reload 契約未知 | `unknown`、`BLOCKED`；不沿用 root 的 respawn 證據 |
| D7 | Online idle、fresh binding／工作證據及可驗證 cold-cut／freeze 完整 | 可規劃 cold-cut；apply 前重驗 freeze 才可更新 |
| D8 | Archived parent 但 child 未封存且仍綁 shared agent | Child 必須獨立分類，不能略過 collateral |
| D9 | Archived 有 pending approval，未證明是無執行可能的歷史記錄 | `active`、`BLOCKED`，不擅自 dismiss／unarchive |
| D10 | Plan 核准後新增 child／runner online／override 改變／通知目標增加 | Drift，停止並重做整份 plan |
| D11 | Owner 只說「接受離線」，缺完整 ID、process 或 future-use 證據 | `unknown`、`BLOCKED`；核准不替代證據 |
| D12 | Dormant 有未完成 issue，owner 保留工作且有不依賴本次恢復的交接 | 可規劃 stored-only；不聲稱 issue 完成或擅自封存 |
