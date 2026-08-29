---
name: decision-864-wallet-charged-not-credited
status: accepted
created: 2026-08-29
updated: 2026-08-29
phase: "#864 A 級（金流路徑）——錢包「已扣款未入帳」修法"
team: repo-leader（woow-paas-platform）
deciders:
  - Eugene（2026-08-29 裁決：#864 留言 10829 / 10913 / 10918 / 10968 / 10970）
issues:
  - "#864（本單。⚠ 本 ADR 只涵蓋它的 A 金流組所衍生的錢包補點修法，不是整張單）"
  - "#948（vendored `payment_ecpay_ecpg` 停在 18.0.0.3.0、上游已 3.3 — PR-F 前置）"
  - "#975（`payment_ecpay_ecpg` AES urlencode 對 `~` 與官方 PHP 不符，既有 prod bug — PR-F 前置）"
  - "#976（[derived] 每日 `QueryTradeMedia` 對帳 ＋ 反向 wallet adjustment；退款真值歸它）"
related:
  - docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md
  - docs/decisions/philosophy/billing-philosophy.md
  - docs/tracking/landings/324-subscription-dup-charge-incident.md
  - docs/guides/deployment/invoice-issuing.md
  - src/models/wallet_topup.py
  - src/models/payment_transaction.py
---

# #864 A 級：錢包「已扣款未入帳」（CHARGED_NOT_CREDITED）的修法決策

> **這份是什麼**：#864 錢包補點修法的**決策與理由**的耐久留存。設計全文（1895 行）住在
> `.scratch/864-wallet-not-credited-fix-design.md`——**該檔會被清掉**，本 ADR 是它的耐久濃縮，
> ⛔ 不是它的索引也不是它的副本。裁決的**逐字全文**留在 Gitea 留言（連結見文末「權威來源」），
> 本 ADR ⛔ 不重抄。
>
> 引用設計全文一律寫 **§節次**（該檔會被清、行號一定會漂）；引用 `src/` 程式碼寫 `path:line`，
> 但**行號一律當會漂，覆核時自己重新定位**。

---

## Context（脈絡）

### 1. 射程：本 ADR 涵蓋 #864 的哪一塊

🔴 **#864 的真實標題**是：

> refactor: controller except 鏈三格判準全端點盤點——catch-all 對交易正確性的影響
> （承 #533，A 金流／B 同型／C 其餘）

本 ADR 只涵蓋 **A 組（金流路徑）**衍生出的「錢包已扣款未入帳」修法。
⛔ **不要**把本 ADR 讀成「#864 就是錢包補點單」——B 組（同型端點）與 C 組（其餘）不在本文射程內。

### 2. 病理：`CHARGED_NOT_CREDITED`

ECPay 已經扣到錢，但錢包點數沒有入帳。#864 盤點出的六格失效（格1／格2／格3a ＋ 標的①②③）
在設計 §一被歸成同一個病的三層：

| 層 | 病理 |
| --- | --- |
| **L1 原子性** | 中途失敗留下半成品，且已 commit |
| **L2 誠實** | 對外（使用者／ECPay）回報的狀態與真實狀態不符 |
| **L3 可稽核** | 失效唯一痕跡在 log，DB 端零記錄、零告警 |

設計給後續 PR 的共同判準：**「這個失效再發生一次時，一個人在 Odoo 裡用 SQL 或 UI 查得到嗎？」——
查不到就不算修好。**

### 3. 為什麼非做不可：兩條「看起來該自癒」的路徑，對它都結構性關閉

這比「應該要自動補」更強——**目前沒有任何機制救得回一筆 `CHARGED_NOT_CREDITED`**（設計 §6.6.0，逐字讀碼）：

| 看似能救的路徑 | 為什麼救不到 |
| --- | --- |
| 遲到的 ECPay callback 呼叫 `_set_done()` | `_set_done` override 的守衛是 `state in ('draft','pending')`，而 `CHARGED_NOT_CREDITED` 的 topup 已被寫成 **`failed`** ⇒ 走 `elif` 分支只 log「skip topup callback」（`src/models/payment_transaction.py`，行號會漂） |
| 既有 stale-pending cleanup cron | search domain 寫死 `state='pending'` ⇒ **永遠掃不到 `failed`**（`src/models/wallet_topup.py`，行號會漂） |

⇒ 錢進了 ECPay、topup 標成 `failed`，**然後沒有任何東西會再碰它**。

⚠ 更糟的是既有 cron 會**抹平證據**：走另一條路留在 `pending` 的那些，
`_CLEANUP_HARD_CANCEL_MIN = 60`（`src/models/wallet_topup.py:398`，行號會漂）會在 60 分鐘後把 tx
`_set_canceled()`、topup 寫成 `cancelled`——**錢一樣沒入帳，但那一列現在長得像「使用者放棄」**。
`pending` 至少是異常訊號，`cancelled` 是正常終態。

### 4. 三個情境的解法完全不同（設計 §6.6.1）

| 分支 | 狀態 | 解法 | 佔比 |
| --- | --- | --- | --- |
| **A｜列還在** | `charge_via_token` 寫 `state='failed'` 後 `return (False,…)` 不 re-raise ⇒ topup 列與 tx 列都在 | **直接重放** `topup._on_payment_done(tx)`（冪等守衛只擋 `done`／已有 ledger，`failed` 不在守衛內）⇒ **零重建邏輯** | 主要 |
| **B｜列被回捲** | auto-topup 路徑的 savepoint 回捲把 topup／tx／invoice 全吃掉，只剩獨立交易寫下的 incident | **reconstruct-then-replay** | 少數 |
| **C｜真孤兒** | callback 帶來一個我們**沒有 incident** 的 MTN | 🔴 **維持人工**——結構上不知道該補給誰 | 極少數 |

🔴 **依賴關係**：分支 A 只在「使用者手動儲值」那條路上無條件成立；auto-topup 路上，
`CHARGED_NOT_CREDITED` 標記寫在 `_process_subscription_billing` 的 savepoint **之內**，
後續 `_on_payment_failure` 一拋例外就把標記一起回捲 ⇒ 情境從 A **降級成 B**。
⇒ 設計 §7.2(a) 的「拆失敗域」＋ §6.3–6.4 的「獨立交易寫 incident」
**是自動補能運作的前提，不是加分項**。

### 5. 這是上線前的潛伏風險，不是進行中事故

見下方 §Consequences 的「射程界定」。

---

## Options（考慮過、有依據地不採用）

濃縮自設計 §十之一。**這是本 ADR 最有複用價值的一段**——每一條都是「看起來合理、實際會更糟」。

| 做法 | 為什麼否決 |
| --- | --- |
| **用 `cr.postrollback` hook 寫告警**（最省事，不必開新連線） | 🔴 **測起來會綠但實際會消失**：`Cursor._close()` 內部**會再 rollback 一次** ⇒ hook 自己寫的東西被第二次沖掉。Odoo core 自帶測試（`base/tests/test_db_cursor.py`）決定性顯示 rollback 後 log 為 `['preC','preR','postR']`、close 後又一次 `['preR','postR']`。本 repo `src/models/cf_access_policy_reap.py` 的註解已獨立否決過同一條路 |
| **只補 `raise` ／拿掉 catch-all，讓例外自然回捲** | 對標的② **不成立**（`charge exception=0`，與吞例外無關）；ECPay 已扣款，讓例外傳出去只會把「已扣款」的記錄**一起回捲，比現在更糟**。對格1 也不是正解：租戶會收到 500 而不是可讀訊息 |
| **查無 tx 一律回 4xx 讓 ECPay 無限重送** | 真孤兒會變成永久重試風暴，而我們**對 ECPay 重試政策零量測** ⇒ 不知道風暴多大；且把收斂責任外包給一個我們不控制、也觀測不到的系統 |
| **拆掉／改小 `_process_subscription_billing` 的 savepoint，或在 charge 後補 `cr.commit()`** | 那顆 per-subscription savepoint 是目前**唯一**的批次隔離機制，爆炸半徑是所有訂閱扣款，而它**不是根因**；中途 commit 會整個改變批次隔離語意，且改變處無測試覆蓋 |
| **在 `_cron_issue_pending_invoices` 加一道全域 config gate**（設計作者原提案，**已撤回**） | (a) 本家族根因就是「一堆 admin 可改的 gate 疊起來讓金流靜默卡死」⇒ 再加第 8 道同族開關＝**複製根因**；(b) 爆炸半徑遠大於標的，會連帶擋住現在正常運作的儲值發票 |
| **只修 `subscription_lifecycle_mixin.py` 一個入口** | 資源側枚舉顯示共**四個**建票入口共用同一個沒被 stamp 的 `_generate_invoice_vals()` ⇒ 只修一處＝修四分之一，且下一個新入口會再犯 |
| **直接改 vendored `paas-odoo-ci/addons/payment_ecpay_ecpg/controllers/ecpg_callback.py`** | 進 image 的是 `.3.0`、上游另有 `.3.3` ⇒ 就地改會讓兩份分岔，且要跨 repo／跨 image 發版才生效。平台端 controller 子類覆寫可達成同效果（機制已逐字驗證） |

---

## Decision（決策）

### D0 🔴 授權依據：auto-heal 為什麼存在

先於本批四則裁決，**2026-08-29 留言 10829（設計 §10.A）第 5 項**已裁決：

> **`CHARGED_NOT_CREDITED` 要自動補（不是告警人工）**；告警降為稽核 ＋ 補失敗的最後防線；
> 「只可見清單」縮到只剩**真孤兒**。

⇒ **整個 auto-heal 設計的存在依據就是這一條**。同批（§10.A）另有 8 項裁決（① 走 C+A 互補、
① 落點＝本 repo route override、② 新增 model、② 不存密文、④ 儲值發票綁 `auto_invoice`、
③ double-charge 冷卻閘要做、格3a 用 choke point、CI 本批落地後升 required），
它們是同一設計的前一批決定，本 ADR 不逐項展開。

### D1 兩半解法

| 半 | 域 / 執行者 | 做什麼 |
| --- | --- | --- |
| ① **ECPG query gate** | pi-ecpay（addon `payment_ecpay_ecpg`） | 補點前呼叫 `CreditDetail/QueryTrade`，**SAFE ＝ Captured-only** ＋ 金額不變式。wrapper 契約已 freeze，🔴 **平台端不得自行解讀 ECPay 原始 status 字串**——唯一判準入口是 `assess_for_credit()` |
| ② **平台 auto-heal** | pi-leader1（本 repo） | 掃描 incident、重查到 Captured 才補點、安全閥、行鎖冪等、runbook ＋ 後台「人工已處理」按鈕 |

verdict 的消費規則：`SAFE` 才准補；`UNSAFE` ⇒ 拒補、把 `assessment.reason` 存進 incident 當
evidence、轉 `needs_human`；`INDETERMINATE` ⇒ 不補，依成因重查或轉人工。

🔴 **安全性的承載者是「ECPay 說 Captured 才補」，不是時鐘**（裁決 10968）。
以前的設計是「時間短到來不及被退刷」，現在時間只負責決定**什麼時候放棄自動、交給人**。

### D2 八項裁決（Eugene，2026-08-29；設計 §10.B）

| 格 | 裁決 | 一句理由 | 留言 |
| --- | --- | --- | --- |
| **#12** 安全閥四數字 | **(B)** 用保守起始值先上線、留 log 觀察再調 | ⚠ 閥的單位是**鑄出的點數（`credits+bonus`）不是 TWD**；每輪計數 log 就是「觀察」的載體，沒有那行 log 這個裁決無法執行 | 10913 |
| **#14** 自動補時間窗 | **(A) 15 分鐘**，依 10918 **降為 defense-in-depth** | 主判準改為 query gate，時鐘不再是唯一防線 | 10913 → 10918 |
| **#15** ECPG Query API | **納入本批**當補點前 gate；日對帳 backstop（`QueryTradeMedia`）不納本批 | 🔴 **前提翻了**——官方**有** Query API，是本 repo 的 addon 沒包（調查見留言 10916） | 10918 |
| **#16** incident 存 partner 識別 | **(A) 可以**：`partner_ref`(Integer 純值) ＋ `partner_email`(Char 快照) | 裁決②的標的是 PCI／密文，partner id 不是卡片資料 ⇒ 分支 B 自動補與真孤兒人工清單都成立 | 10913 |
| **#17** 客服 runbook | **(A)** 定死一個標準動作 ＋ incident 後台加顯式「人工已處理」按鈕 | 上線前定死 | 10913 |
| **#18** 15 分窗語意 | **(B) 改軟**（defense-in-depth）＋ 人工 fallback 拉長 | 官方建議「付款後 10 分才查得準」⇒ 硬 15 分只剩 5 分可用窗，會逼自動補幾乎全轉人工，**與裁決 ③ 相反** | 10968 |
| **#20** T2 具體值 | **4 小時** | 吃得下兩次 403 shared circuit、明顯 > 官方 40 分建議、< T3 daily recon | 10970 |
| **#7** cleanup cron 收斂 | **准** | hard-cancel 前先查有無未 resolved 的 auto-heal／inbox 記錄，有就跳過 ＋ 記 `skip_reason`，把「放棄」的決定權**收斂到 T2 一處** | 10970 |

⚠ **不要混進第 9 項**：設計 §10.B 表格還有一列 **#13 kill-switch（預設開、off-state 有聲）**，
那是**實作側可決、issue-leader 確認**的，**不是 Eugene 裁決**——來源不同，但它是安全閥的一環，
⛔ 不可略過。

### D3 四層時間模型（裁決 10970 定案）

| 層 | 名稱 | 值 | 語意 |
| --- | --- | --- | --- |
| **T0** | `query-not-before` | **10 分** | 官方建議信用卡付款後 10 分才查得準；之前**不呼叫** wrapper，incident 標 `TOO_EARLY`（由我們標，不是 wrapper 的事） |
| **T1** | `prefer-resolve-by`（**軟**） | **15 分** | **不是 cutoff**。逾時**不放棄**，只升高 log／報表可見度 |
| **T2** | auto-heal 重試預算上限（**硬**） | **4 小時** | 到此仍未 `SAFE` ⇒ 停止查詢、轉 `needs_human` ＋ 告警 |
| **T3** | 外界（結構性） | 當日 **#976** daily recon 那趟 | 退款真值歸它，auto-heal ⛔ 不與它競爭 |

⛔ **「incident 過期」那一格就是 T2，不是另一個數字**——兩處各寫一個值＝遲早漂掉。

### D4 T2 ＝ 4 小時是**約束求解**，不是偏好

設計把它寫成六條約束的交集（官方 10 分不准查、官方 40 分可見性錨點、一次 403 吃掉 ≥30 分零 HTTP、
403 是 shared circuit 要能吃兩次、退款風險隨時間上升、必須明顯早於 T3），交集是
「**大於 1 小時、遠小於 1 天**」，取 4 小時。

🔴 **保留這個框架的用意**：日後要調 T2，**改的是其中一條約束，不是憑感覺換數字**。
兩個現成備選：更保守 ⇒ **2 小時**（只吃得下一次 403）；更寬 ⇒ **與 T3 對齊**（涵蓋率最高、退款曝險最大）。

### D5 PR 切法與卡點

設計把工作切成 A–H 八顆 PR（每顆獨立可回退）。與本 ADR 直接相關的是：
**D**（`woow.billing.incident` model，後面三顆的地基）→ **E**（拆失敗域 ＋ 冷卻閘，**F 的前提，⛔ 不可跳過**）
→ **F**（安全自動補點，風險最高——**它會自動把錢送出去**）。
**G**（callback 預登記 ＋ inbox ＋ replay）與 F 共用互斥形狀：兩者都會導致入帳、choke point 都是
`topup._on_payment_done(tx)`、互斥點都是對 topup 列的 `FOR UPDATE NOWAIT`
⇒ **先落地的那顆定義形狀，後落地的必須沿用，⛔ 不要各寫一套**。

---

## Invariants（本 ADR 收斂的關鍵守線）

⚠ 設計全文另有其他 invariant（atomic-idempotent credit gate ＋ partial UNIQUE index、唯一入帳
choke point、獨立 cursor 的三條硬約束、kill-switch off-state 有聲、§6.6.7b 的七個洞…）。
這裡**只收「違反了會直接造成資損或悄悄推翻裁決」的六條**——
⛔ 這不是設計文件裡一份叫「6 條守線」的既有清單，那份不存在。

1. **🔴 query 要在開 credit 交易之前做。**
   wrapper 的 timeout 是 30 秒級，包進已持行鎖的交易 ⇒ 一筆 incident 就能把那條 cursor 與該 topup
   的行鎖壓住 30 秒。這是 **#525 家族**「不可逆外呼包在可回捲交易裡」的**同型錯誤**
   （見 [`patterns/post-commit-bookkeeping-rollback-unit.md`](patterns/post-commit-bookkeeping-rollback-unit.md)）。
   正確順序：交易外 query → assess → 只有 `SAFE` 才開獨立 cursor → `FOR UPDATE NOWAIT` 鎖列
   → 重讀狀態／冪等檢查 → 補點 → 標 resolved → commit。

2. **🔴 T1 ⛔ 不可寫成 `if now > t1: return`。**
   那會把裁決 #18 ＝ B **悄悄改回 A**（硬 cutoff），而且沒有任何機器守線抓得到。
   ⇒ 要配一條測試釘住「**過了 15 分仍會重查**」。

3. **🔴 `SAFE` ≠ refund-free。**
   契約只保證「查詢當下具有完整 `Captured`／金額／`CloseData` capture evidence」，
   官方 9088 **沒有定義 Action `R` 退刷的呈現方式**。Query 與補點**不是原子的**，
   「補點後才退款」只有日對帳抓得到 ⇒ **退款真值靠 #976，不靠單筆查詢**；
   平台自己仍須 atomic／idempotent credit gate（含 DB 層 partial UNIQUE index）。

4. **🔴 TWD 與點數兩種金額 ⛔ 不可交叉用。**
   `assess_for_credit(expected_amount=...)` 吃的是 **TWD 正整數**；安全閥的上限單位是
   **鑄出的點數（`credits + bonus`）**。閥若用 TWD，`bonus` 打錯一個 0 照樣過閥。
   ⚠ `loyalty.card.points` 是純 `Float`、**無任何 constraint** ⇒ **DB 層對「補太多」零防線**，
   安全閥就是全部的防線。
   ⚠ `woow.wallet.topup.amount` 是 `Float` ⇒ 傳給 `expected_amount` 前的取整規則要寫成一條測試；
   取整錯了會 fail closed（安全）但**安靜地擋掉合法的自動補**——症狀是「自動補都不動作」。

5. **🔴 `_CLEANUP_HARD_CANCEL_MIN = 60` 約束的不是這裡。**
   （`src/models/wallet_topup.py:398`，行號會漂，自己重新定位。）
   它**看起來**與 T2 ＝ 4 小時打架，但逐分支看只有一條真的撞：
   分支 A（topup `failed`）**不會**——那顆 cron 的 search 是 `state='pending'`，根本掃不到 `failed`；
   分支 B（重建）會，但已由 `repair_incident_id` 標記處理；
   🔴 真正會撞的是 **① 的 inbox-replay 路徑**（tx 與 topup 都停在 `pending`、零 incident）。
   ⇒ 裁決 #7 就是為此：hard-cancel 前先看有無未 resolved 記錄，有就跳過 ＋ 記 `skip_reason`，
   **⛔ 不要讓兩個機制各自有一個放棄時鐘**。

6. **🔴 403 是 shared circuit，不是單筆的事。**
   `ECPGQueryForbiddenError` 帶 `retry_after_seconds=1800`，而 403 **不一定**是 rate-limit
   （也可能是權限／HashKey／HashIV）⇒ fail closed：scope 至少 `(MerchantID, egress identity)`、
   該 scope **≥30 分零 HTTP**、circuit 必須 **durable 且跨 worker 可見**（DB 一列），
   🔴 **寫要用獨立 cursor，讀也要用新 cursor**（RR 快照盲區）。
   circuit 開著時被跳過的 incident 要寫 `skip_reason='query_circuit_open'`，⛔ 不可以只是 `continue`；
   **重試後仍 403 要升級告警**（那多半是憑證／權限壞了）。

---

## Consequences（後果）

### 1. 跨 repo 實作鏈：只有 PR-F 被卡

依設計 §9.1 的**六步表**（⚠ 這個順序與早期口頭轉述不同，**以 §9.1 為準**）：

| 步 | 誰做 | 內容 | 狀態（2026-08-29 實測） |
| --- | --- | --- | --- |
| 1 | issue-leader | freeze wrapper public interface | ✅ 已 freeze |
| 2 | **#948** | vendored `payment_ecpay_ecpg` 由 `18.0.0.3.0` **完整追平** upstream `3.3`，跑 ECPG + AIO regression | **open** |
| 3 | **#975** | upstream 先修 AES baseline（`~` urlencode 與官方 PHP 不符）含 official-vector test；**再把 exact upstream commit vendor 進 `paas-odoo-ci`** | **open**（既有 prod bug） |
| 4 | pi-ecpay | 在 `ecpay_odoo18` upstream 實作 query wrapper、review、merge、manifest 升版 | 未開工 |
| 5 | — | `paas-odoo-ci` 從該 exact upstream commit／vendor tag 同步**整個** addon | — |
| 6 | **PR-F** | 平台消費端 | 🔴 **要等 5 完成才合併** |

- 🔴 **只有 PR-F 被卡**——A／B／C／D／E／G 都不依賴 wrapper，可以照常推進。
- ⛔ **不可為趕進度手抄 `ecpg_client.py` 到 stale 的 3.0**：upstream 3.3 已有 HTTP error taxonomy
  與 central `RtnCode` enforcement，3.0 沒有 ⇒ 手抄會產生**兩套不同的 error contract**，
  而 query gate 正是照 typed error 分流的。
- ⚠ 版本號**不預先寫死**，以實際 merge order 為準。
- ⚠ #948／#975／#976 **都在本 repo**；跨的是**分工**（ECPay 域 addon 鏈歸 pi-ecpay、
  平台 #864 側實作交 pi-leader1，見留言 10970 末段），不是 repo。

### 2. 仍待決 3 項（設計 §10.C）

| # | 待決 | 為什麼要拍板 |
| --- | --- | --- |
| **5′** | 縮小後的人工清單仍需 **on-call 承諾**（真孤兒對帳、格1 歷史半成交沖銷、`manual` 手動開票、自動補失敗落 `needs_human` 的那些） | **誰 on-call、SLA 多久、清單空不空由誰簽**仍未指定 |
| **19** | **PR-F 要不要拆兩刀**：先落地非 query 部分，query gate 留 stub（一律 `INDETERMINATE` ⇒ 不補、轉人工） | 這樣拆＝**暫時退回「只可見不自動補」**，與裁決 ③ 意圖不符。這是**排程**決定不是技術判斷 |
| **11** | 要不要在本批一起補「ECPay 一手 contract」 | 本輪部分被 query gate 取代；只剩 ① 腿1（callback 回可重送）的窗口長度仍取決於它。屬對外協調 |

### 3. 這份 ADR ⛔ 不受 pattern-guard 強制閱讀

repo-lint 規則 #15（pattern-doc-registry）／#20（doc-symbol-anchor）與 pattern-guard hook
**都只吃 `docs/decisions/patterns/` 底下的檔**。本 ADR 放**根層**（它是一次性的決策紀錄、不是可複用
pattern，符合 [`README.md`](README.md) 的分類）⇒ **不會有任何 hook 強制改 `wallet_topup.py`
的人讀它**。
🔴 **若日後要讓上面六條守線被機器強制**，需另立 `patterns/` doc 並 append
`.claude/hooks/pattern-guard-map.json`。

### 4. 與既有文件的關係

- [`patterns/post-commit-bookkeeping-rollback-unit.md`](patterns/post-commit-bookkeeping-rollback-unit.md)
  ——「提交點之後的每一步都是『記帳』，不是『這次操作成功了沒』的判準」（源自 #461／#525）。
  🔴 **本 ADR ＝ 把那條 pattern 實例化到 ECPay 扣款網域**，守線 #1 就是它的同型。
- [`philosophy/billing-philosophy.md`](philosophy/billing-philosophy.md) —— wallet-first 哲學與
  auto-topup 觸發時機，是本題的背景脈絡。
- [`../tracking/landings/324-subscription-dup-charge-incident.md`](../tracking/landings/324-subscription-dup-charge-incident.md)
  —— 同網域先例事故（重複扣款／retry 風暴）。
- [`../guides/deployment/invoice-issuing.md`](../guides/deployment/invoice-issuing.md)
  —— §10.A #6（儲值發票綁 `res.company.auto_invoice`、manual 要真生效）與該檔直接相關。
  ⚠ **本批不改它**，但實作落地時要一起更新。

### 5. 🔴 射程界定（⛔ 不外推）

照設計 §十三：

1. 證據基準是 `2287e7e5`（develop）＋ image `paas-odoo-stg:20260820.0807` 的 vendored `.3.0`。
2. **R 組證明的是「撞到時會怎樣」，不是「常常撞到」**——3DS 的 callback 通常在使用者完成驗證數秒後
   才送出，自然狀態下遠晚於 commit。⛔ 不得把本設計讀成「線上正在大量發生」。
3. prod `account_move` 全表只有 1 筆 `out_invoice/draft`、**posted 0 筆**
   ⇒ 以上全屬**上線前必修的潛伏風險，不是進行中事故**。
4. mock 之下「ECPay 已扣款」是**設定不是觀測**。
5. 設計全文的所有「量測」皆為讀碼／讀 core 原始碼／`git ls-remote`；該輪**未跑任何 Odoo、
   未碰 kubectl**。

---

## 權威來源（⛔ 本 ADR 不重抄逐字全文）

**裁決逐字**（Gitea 留言，`odoo-addons/woow_paas_platform` #864）：

| 留言 | 內容 |
| --- | --- |
| [10829](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864#issuecomment-10829) | §10.A 前一批裁決（含 #5「要自動補」＝ auto-heal 的授權依據） |
| [10913](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864#issuecomment-10913) | §10.B 四格：#12／#14／#16／#17 |
| [10916](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864#issuecomment-10916) | ECPG 查單 API 調查結果（pi-ecpay 查、issue-leader 覆核）——**#15 翻案的依據**，非裁決 |
| [10918](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864#issuecomment-10918) | #14 + #15 ＝ **C（折中）** |
| [10968](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864#issuecomment-10968) | #18 ＝ **B（軟窗）** |
| [10970](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864#issuecomment-10970) | #20（T2 ＝ 4 小時）＋ #7（cleanup cron 收斂）＋ 四層時間模型定案 ＋ 實作交派 |

**設計全文**：`.scratch/864-wallet-not-credited-fix-design.md`（1895 行）
——⚠ **該檔會被清掉**，本 ADR 是它的耐久濃縮。引用一律寫 §節次，⛔ 不寫行號。

**wrapper 契約**：`ecpay_odoo18/.scratch/864-ecpg-credit-detail-query-wrapper-design.md` §2／§2.3／§2.4／§7／§11
——同樣是 `.scratch`，會被清。

**一頁摘要 artifact**：<https://claude.ai/code/artifact/91bcd876-ff28-40ff-9405-11b23a5f1d00>

**相關 issue**：[#864](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/864)、
[#948](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/948)、
[#975](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/975)、
[#976](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/976)（2026-08-29 實測皆 open）
