---
title: ECPay server-to-server 回呼在停機窗口內的重送政策與 Odoo 端冪等性
category: scout
status: draft
created: 2026-09-22
updated: 2026-09-22
related:
  - docs/scout/2026-09-18-harumi-odoo-paas-migration-plan.md
  - docs/scout/2026-09-18-odoo-bitnami-coupling-inventory.md
  - docs/decisions/864-wallet-charged-not-credited.md
  - docs/decisions/philosophy/billing-philosophy.md
---

# ECPay server-to-server 回呼在停機窗口內的重送政策與 Odoo 端冪等性

> 偵察日期：2026-09-22（#1163，Part of #1134；來源＝#1142「平台 Odoo 要不要、什麼時候改用新 odoo chart」裁決 ⑧）
> 問題：站台停機 15～60 分期間，ECPay 的 server-to-server 回呼會怎樣——ECPay 端重送政策、回呼失敗後的補救途徑、Odoo 端收到重複回呼是否冪等。
> 產出目標：可直接抄進「平台 Odoo 切換 runbook」與「harumi 搬遷 runbook」的「停機期間金流」段（§5）。
> 方法：全程唯讀。ECPay 官方文件（developers.ecpay.com.tw）逐字引用；vendored addon 讀 `woow-paas/paas-odoo-ci` 的 `addons/`（進 prod image 的版本）；Odoo core 讀 GitHub `odoo/odoo` 18.0 分支；本 repo 讀 worktree `origin/develop`（`66be66c9`）。未跑任何 Odoo、未碰 kubectl、未碰 prod。
> 標記：**〔已查證〕**＝有原文／原始碼可指；**〔推論〕**＝由已查證事實推導、未實測；**〔找不到〕**＝查過但無官方原句。

## 0. 先釐清：兩個站用的是兩套 ECPay 產品

| 站 | 金流 addon | ECPay 產品 | 回呼形態 | 發票 addon |
| --- | --- | --- | --- | --- |
| 平台 Odoo（`woow_paas_platform`） | `payment_ecpay_ecpg`（`src/__manifest__.py:17`） | **站內付 2.0（ECPG）**：卡號在 ECPay iframe 輸入、3D 驗證後 ECPay POST 加密 `ResultData` 到商家 callback | ECPG `pay_callback`（本 repo 有 inbox override：`src/controllers/ecpg_callback.py`） | `ecpay_invoice_tw`（`src/__manifest__.py:18`），只有**出站**、cron 驅動 |
| harumi-odoo（叢集上的 odoo-tenant 站，#1134 搬遷標的） | `payment_ecpay`（nginx log 看到；見 [harumi 移植計畫](2026-09-18-harumi-odoo-paas-migration-plan.md) 證據 [C]） | **全方位金流（AIO）**：導頁到 ECPay 付款頁，付款後 ECPay POST 到 `ReturnURL`／`PaymentInfoURL`／`PeriodReturnURL` | AIO 標準 `1|OK` 協定 | 未盤點（不在本題射程） |

⇒ §1（ECPay 端政策）與 §3（vendored 冪等性）都要分 AIO／ECPG 兩欄看；§5 runbook 段也分兩份。

## 1. ECPay 端：回呼重送政策（一手來源＝官方技術文件）

所有頁面皆 2026-09-22 以 `curl` 抓 `https://developers.ecpay.com.tw/?p=<id>` 後去標籤 grep；「更新日期」＝頁面 JSON-LD 的 `dateModified`。引用為逐字（僅去除 HTML 空白）。

### 1.1 AIO `ReturnURL`（付款結果通知）〔已查證〕

[付款結果通知 `?p=2878`](https://developers.ecpay.com.tw/?p=2878)（更新 2026-02-04）：

> 若未正確回應 1|OK，系統會隔 5~15 分鐘後重發訊息給特店，當天重複發送四次。

> 若特店持續收到綠界回傳付款資訊，此時請檢查是否未正確回應 1|OK 給綠界，常見錯誤回傳值為("1|OK"、1|ok、_OK 、1\OK、空白 )。

> 1|OK 僅是廠商回應綠界是否收到通知，並不會改變付款狀態。若因授權時間過久未收到付款結果通知訊息時，請使用查詢訂單API查詢後再顯示付款結果。

> 特店務必判斷檢查碼[CheckMacValue]是否正確，以及是否已經對該筆訂單的付款通知，做過相對應的處理，以免造成交易狀態無法同步的損失。

交叉驗證（通用 FAQ [`?p=61944`](https://developers.ecpay.com.tw/61944/)，更新 2025-11-28；[`?p=61983`](https://developers.ecpay.com.tw/61983/)，更新 2025-11-21）：

> 若您的伺服器有問題或其他因素導致無法接收到付款結果(API串接有正確設置ReturnURL) Ecpay將主動再次通知，時間為5~15分鐘1次，共發送4次。若還是未能收到，可參考以下方式進行作業及反查

**總時長**：官方只給「間隔 5~15 分鐘」＋「當天共 4 次」，**沒有**「X 小時內完成」這種句子〔找不到〕。「隔 5~15 分鐘」是相對前一次還是相對第一次也沒明講。

### 1.2 AIO `PaymentInfoURL`（ATM／超商取號結果通知）〔已查證〕

[ATM/CVS/BARCODE 取號結果通知 `?p=2881`](https://developers.ecpay.com.tw/?p=2881)（更新 2026-06-25）——與 1.1 逐字相同：

> 若未正確回應1|OK，系統會隔5~15分鐘後重發訊息給特店，當天重複發送四次。

> 收到綠界回傳結果通知，請回傳1|OK，此訊息僅代表特店回應是否已收到綠界回傳通知，並不會影響訂單的狀態。

### 1.3 AIO `PeriodReturnURL`（定期定額每期扣款）〔已查證：**不重送**〕

[定期定額 `?p=15162`](https://developers.ecpay.com.tw/?p=15162)（更新 2026-01-27）：

> PeriodReturnURL 每期僅會收到一次通知，如未收到通知，請利用信用卡定期定額訂單查詢取得實際授權結果。

[信用卡定期定額 `?p=2868`](https://developers.ecpay.com.tw/?p=2868)（更新 2026-05-21）：

> 若未設定接收定期定額的執行結果回傳PeriodReturnURL時，請特店要自行到綠界廠商管理後台確認每次授權狀態為成功時，才進行出貨。

這是唯一一組官方明講「不重送、請用查詢 API 補單」的回呼。⚠ 本題兩個站都**沒用** ECPay 定期定額（§3.2：vendored 無 `PeriodReturnURL` route；平台訂閱扣款是 Odoo 自己的 cron 逐期建 tx），列出僅供完整。

### 1.4 ECPG（站內付 2.0）`ReturnURL`〔已查證：政策與 AIO 相同〕

[ReturnURL `?p=9058`](https://developers.ecpay.com.tw/?p=9058)（站內付 2.0 文件樹；更新 2026-05-28）：

> Step1.綠界：以ServerPost方式傳送付款結果訊息至特店的Server網址(ReturnURL) Step2.特店：收到綠界的付款結果訊息，回應1|OK

> 若未正確回應 1|OK，系統會隔 5~15 分鐘後重發訊息給特店，當天重複發送四次。

> 1|OK 僅是廠商回應綠界是否收到通知，並不會改變付款狀態。ReturnURL 和 OrderResultURL 沒有固定的先後順序，會依當下連線速度與系統執行速度而定

站內付 2.0 專屬 FAQ [`?p=61973`](https://developers.ecpay.com.tw/?p=61973)（更新 2025-11-21）重複同一句「時間為5~15分鐘1次，共發送4次」。

傳輸格式：官方頁面給的 body 是 JSON 信封 `{"MerchantID":…, "RpHeader":{"Timestamp":…}, "TransCode":1, "TransMsg":"Success", "Data":"<AES 加密 JSON>"}`；vendored controller 是從 form 欄位 `ResultData` 讀出這個 JSON 字串再取其中 `Data` 解密（`ecpg_callback.py:269-296`）——官方頁面正文**沒出現** `ResultData` 這個外層欄位名〔找不到〕，只有 vendored 程式碼（與其 sandbox 測試）這樣寫；不影響本題結論。

### 1.5 電子發票 `B2CInvoice/Issue`〔已查證：**同步 API、無回呼**〕

[一般開立發票 `?p=7896`](https://developers.ecpay.com.tw/?p=7896)（更新 2026-05-20）：參數表沒有任何 callback／通知 URL 欄位；回應是同步 JSON（解密後 `RtnCode`／`RtnMsg`／`InvoiceNo`）：

> RtnCode Int 回應代碼：1 代表 API 執行成功，其餘代碼均為失敗。

> InvoiceNo String(10) 發票號碼：若開立成功，則會回傳一組發票號碼 若開立失敗，則會回傳空值

`RelateNumber` 唯一性：

> RelateNumber String(50) 特店自訂編號 必填 需為唯一值不可重複使用 注意事項：請勿使用特殊符號 大小寫英文視為相同 (e.g. 123abc456=123ABC456)

⚠ 「RelateNumber 重複時回哪個錯誤碼」〔找不到〕：查過 [交易訊息代碼一覽表 `/5740/`](https://developers.ecpay.com.tw/5740/)、`?p=24319`、[錯誤代碼查詢 `?p=13853`](https://developers.ecpay.com.tw/?p=13853)（只列字軌／配號用罄 `5070350`、`1500047`），完整發票錯誤碼在廠商後台登入後頁面，公開文件無。⇒ 「同 RelateNumber 重送會被擋」只能當〔推論〕，而 vendored 每次重試都是**新的** RelateNumber（§3.2.3），這個去重根本不會被觸發。

### 1.6 對停機窗口的意義〔推論，依 1.1／1.4 算〕

- 停機期間 Cloudflare Tunnel 仍在、origin 不在 ⇒ ECPay 拿到 5xx／逾時而非 `1|OK` ⇒ 觸發重送（官方沒定義「連不上」與「回錯字串」的差別，但兩者都不是 `1|OK`）。
- 重送 4 次、每次間隔 5~15 分：若「隔 5~15 分鐘」是相對前一次：4 次重送最早在第一次後 **20 分**耗盡、最晚 **60 分**。
  - 停機 **< 20 分**：至少一次重送必落在恢復後 ⇒ **ECPay 會自己補**。
  - 停機 **20～60 分**：可能耗盡也可能沒耗盡，**不可保證**，恢復後必須對帳。
  - 停機 **> 60 分**：4 次重送幾乎確定全部落在停機內 ⇒ 通知**丟定了**，只剩 §2 的補救。
- 若「隔 5~15 分鐘」是相對第一次（4 次都在 5~15 分內），則停機 > 15 分就丟定了。官方沒講清楚 ⇒ runbook 一律**以「停機 ≥ 15 分就當作會丟」保守處理**，恢復後對帳不可省。
- 客戶端 redirect（`OrderResultURL`／`ClientBackURL`）在停機期間也是 5xx，**沒有重送**（官方沒有任何前景回傳重送的敘述）⇒ 客戶看到的是錯誤頁，但這不影響金流事實。

## 2. ECPay 端：回呼失敗後的補救途徑（手動重送 vs 查詢 API）

### 2.1 廠商管理後台手動重送〔已查證，僅信用卡授權單〕

[`?p=61944`](https://developers.ecpay.com.tw/61944/)／[`?p=61983`](https://developers.ecpay.com.tw/61983/)（AIO FAQ）與 [`?p=61973`](https://developers.ecpay.com.tw/?p=61973)（站內付 2.0 FAQ）逐字相同：

> 若還是未能收到，可參考以下方式進行作業及反查 1.建議您至廠商管理後台進行相關操作以利您的出貨及對帳作業。頁面路徑：廠商管理後台(可點擊另開)→信用卡收單→交易明細查詢 ●輸入授權單號，開始搜尋。●點選表格內藍色有底線之「授權單號」，即可見「授權回傳(補觸發)」按鈕。點擊以進行補授權作業。

- 入口＝**廠商管理後台 → 信用卡收單 → 交易明細查詢 → 點「授權單號」→「授權回傳(補觸發)」**。AIO 與 ECPG 共用同一支（後台是跨產品線的信用卡收單層）。
- 〔找不到〕ATM／CVS／BARCODE 取號通知（`PaymentInfoURL`）的手動重送按鈕：查過 `?p=61944`、`?p=61983`、`?p=9168`、`?p=2881` 皆無。
- 「模擬付款」按鈕（後台 → 一般訂單查詢 → 全方位金流訂單）是**測試** ReturnURL 用（送 `SimulatePaid=1`），不是對真實訂單重送。

### 2.2 查詢 API〔已查證〕

| API | URL（正式） | 文件 | 必要參數 | 狀態值域 |
| --- | --- | --- | --- | --- |
| AIO `QueryTradeInfo/V5` | `https://payment.ecpay.com.tw/Cashier/QueryTradeInfo/V5` | [`?p=2890`](https://developers.ecpay.com.tw/?p=2890)（更新 2026-06-29） | `MerchantID`、`MerchantTradeNo`、`TimeStamp`（3 分鐘內有效）、`CheckMacValue` | `TradeStatus`：`0` 交易訂單成立未付款／`1` 交易訂單成立已付款／`10200095` 交易訂單未成立、消費者未完成付款作業 |
| ECPG `Cashier/QueryTrade` | `https://ecpayment.ecpay.com.tw/1.0.0/Cashier/QueryTrade` | [`?p=9083`](https://developers.ecpay.com.tw/?p=9083)（更新 2026-06-12） | `MerchantID`、`RqHeader.Timestamp`（10 分鐘內有效）、`Data`（AES 加密，內含 `MerchantTradeNo`） | `TradeStatus`：`0` 未付款／`1` 已付款（**只有兩種**） |
| ECPG `CreditDetail/QueryTrade` | `https://ecpayment.ecpay.com.tw/1.0.0/CreditDetail/QueryTrade` | [`?p=9088`](https://developers.ecpay.com.tw/?p=9088)（更新 2026-08-12） | 同上 | `Status`：未關帳 `Canceled`／`Unauthorized`／`Authorized`；已關帳 `To be captured`／`Captured`／`Operation canceled` |
| ECPG `Cashier/QueryTradeMedia` | `https://ecpayment.ecpay.com.tw/1.0.0/Cashier/QueryTradeMedia` | [`?p=16406`](https://developers.ecpay.com.tw/?p=16406)（更新 2026-02-03） | `StartDate`／`EndDate` 日期區間 | 撥款對帳檔（CSV）；「此API有限制IP存取」、「僅開放一分鐘下載一個對帳媒體檔」、「建議每日呼叫…查詢昨日交易訂單結果進行對帳」 |
| 定期定額查詢 | `https://ecpayment.ecpay.com.tw/1.0.0/Cashier/QueryTrade` | [`?p=9093`](https://developers.ecpay.com.tw/?p=9093) | `MerchantID`、`MerchantTradeNo` | `ExecStatus` 0 已取消／1 執行中／2 執行完成；`ExecLog` 每次授權明細（本題未用到） |

**官方明講「未收到通知就用查詢 API 補單」**（`?p=2890` AIO 與 `?p=9083` ECPG 幾乎同句）：

> 當取得付款結果通知時，請呼叫查詢訂單API驗證付款結果 在訂單產生後未收到綠界通知的付款結果 信用卡、Apple Pay及銀聯卡付款方式 請於付款後10分鐘使用API查詢付款結果。若遇到銀行尚未回覆的情況，查詢結果中的TradeStatus交易狀態為0：未付款，請再過10分鐘後重新查詢訂單。或在付款後40分鐘後再呼叫API查詢付款結果，以避免重複呼叫查詢API。 ATM、CVS、BARCODE付款方式 因為為離線付款方式，請待綠界通知的付款結果。

Rate limit（跨 API 通用 [介接注意事項 `?p=9168`](https://developers.ecpay.com.tw/9168/)，更新 2025-09-17）：

> 假如程式呼叫 API 的速度太快，會收到 HTTP Status Code 403 的狀態碼，此時請降低呼叫 API 的速度，並等候 30 分鐘後再重新呼叫，如果有高速存取 API 的需求，請先確認您具備「特約賣家」身分，並聯繫所屬業務人員。

⚠ [#864 ADR](../decisions/864-wallet-charged-not-credited.md) 把「403 等 30 分」歸在 `CreditDetail/QueryTrade` 脈絡下，正確出處是這份通用注意事項頁；「40 分鐘」出處是查詢訂單頁（`?p=2890`／`?p=9083`），不在 `?p=9088`。結論不變、只是引用要指對頁。

### 2.3 哪個是官方建議的補救

- **查詢 API 是官方建議的主路徑**（1.1、1.3、2.2 三處都寫）；「授權回傳(補觸發)」是 FAQ 裡的**次要人工手段**，且只覆蓋信用卡。
- 電子發票：查 [`B2CInvoice/GetIssue` `?p=7923`](https://developers.ecpay.com.tw/?p=7923)（以 `RelateNumber` 或 `InvoiceNo`＋`InvoiceDate` 查）、作廢 [`B2CInvoice/Invalid` `?p=7906`](https://developers.ecpay.com.tw/?p=7906)。

## 3. Odoo 端冪等性

### 3.1 Odoo 18 core `payment` 模組：state machine 本身就是冪等 guard〔已查證〕

來源：`https://raw.githubusercontent.com/odoo/odoo/18.0/addons/payment/models/payment_transaction.py`（2026-09-22 抓取；行號以該日 18.0 HEAD 為準）。

- 通知入口 `_handle_notification_data()`（L637-647）＝ `_get_tx_from_notification_data()` 找 tx → `tx._process_notification_data()`；provider addon 覆寫後者，最終呼叫 `_set_pending/_set_authorized/_set_done/_set_canceled/_set_error`。
- 五個 `_set_*` 全部走 `_update_state(allowed_states, target_state, state_message)`（L765-823）。它把 recordset 分三類：
  - `state in allowed_states` → 才真的 `write()`（L817-822）；
  - `state == target_state` → **跳過**，只留 INFO log `tried to write on transaction with reference %s with the same value for the state`（L799-804）；
  - 其餘 → **跳過**，留 WARNING log `... illegal value for the state (previous state: ..., target state: ..., expected previous state to be in: ...)`（L805-816）。
- 各 target 的 `allowed_states`（L687／704／721／739／757）：

  | target | allowed source states | 含義 |
  | --- | --- | --- |
  | `pending` | `draft` | |
  | `authorized` | `draft`, `pending` | |
  | `done` | `draft`, `pending`, `authorized`, **`error`** | 失敗後再收到成功 → **會**轉 done |
  | `cancel` | `draft`, `pending`, `authorized` | **done → cancel 不允許**（被 WARNING 跳過） |
  | `error` | `draft`, `pending`, `authorized` | **done → error 不允許**（被 WARNING 跳過） |

- ⇒ core 層的結論：
  1. **同一筆 tx 重複收到「成功」通知**：第二次 `_set_done()` 是 no-op（INFO log），`_log_received_message()` 也只對「真的轉移」的 recordset 呼叫（L727）⇒ chatter 不會重複。**安全**。
  2. **先失敗後成功**（例：3D 驗證超時 → error，稍後 ECPay 補送成功）：`error → done` 允許 ⇒ 會補成 done。**安全（且是我們要的）**。
  3. **先成功後失敗／取消**：`done → error/cancel` 被 WARNING 跳過 ⇒ tx 留在 done。**安全**（不會把已入帳的單改成失敗）。
  4. ⚠ **`cancel → done` 不允許**：若我方先把 tx 設成 `cancel`（例如本 repo 的 cleanup cron 60 分 hard-cancel，見 §3.3），之後 ECPay 才送成功通知 ⇒ 被 WARNING 跳過、**錢已扣但 tx 永遠 cancel**。這是停機情境最要小心的一格（§5）。
- 入帳／開票不在 `_set_done()` 裡，而在 post-processing：`_update_state()` 每次真的轉移都把 `is_post_processed=False`（L821）；`_post_process()`（L879-889）由 `/payment/status/poll` controller（`payment/controllers/post_processing.py` L51-53）或 cron `payment.cron_post_process_payment_tx`（每 10 分、`_cron_post_process()` L849-877，只撿 `last_state_change` 4 天內未 post-process 的 tx）觸發。
  - `account_payment` 覆寫（`addons/account_payment/models/payment_transaction.py` L99-133）：`state == 'done'` 且 **`not tx.payment_id`** 才 `_create_payment()`（L117-122）⇒ 同一筆 tx 不會建兩筆 `account.payment`。draft invoice 自動 `action_post()`（L109）本身冪等（posted 不會再 post）。
  - `sale` 覆寫（`addons/sale/models/payment_transaction.py` L39-105）：done → `_check_amount_and_confirm_order()` 只確認 `draft/sent` 的 SO；`sale.automatic_invoice` 開著才 `_invoice_sale_orders()`（L194-220，只對 `state == 'sale'` 且 `_is_paid()` 的 SO 開 final invoice；SO 已 fully invoiced 時 `_create_invoices(final=True)` 帶 `raise_if_nothing_to_invoice=False` 不會再開）。
  - ⇒ **重複通知不會重複建 payment／invoice**；但 `is_post_processed` 只在「真的狀態轉移」時 reset，被 `_update_state` 跳過的重複通知不會再觸發 post-process，這是正確的。
- ⚠ 停機對 core 的影響〔推論〕：停機期間客戶端的 redirect（AIO `OrderResultURL`／`ClientBackURL`，ECPG 的前景回傳）也一起丟 ⇒ `/payment/status/poll` 不會被打；恢復後靠 `cron_post_process_payment_tx`（10 分一趟、4 天內）補 post-process。**前提是 tx 已經被 server-to-server 通知推到 done**——通知本身丟了，cron 撿不到任何東西（tx 還在 draft/pending）。

### 3.2 vendored addon（進 prod image 的版本）〔已查證〕

來源＝Gitea `woow-paas/paas-odoo-ci` default branch `main` HEAD **`93ab61bb`**（2026-09-04）的 `addons/`（`tea api` 列目錄＋ raw 下載）。本機 upstream checkout `~/woow-repos/WOOW/ecpay_odoo18/`（`0cb38c2`）只當對照：`payment_ecpay_ecpg` 落後（`18.0.0.3.3` vs vendored `18.0.0.3.5`，差異只在 SDK 新增 `query_credit_trade()`／`assess_for_credit()` 與 `aes_crypto.py` 的 `~` urlencode 方向，`controllers/`、`models/` 逐字相同）；其餘三個 addon 皆 `18.0.1.1.1`、只有 CRLF 差異。

| addon | vendored version | 說明 |
| --- | --- | --- |
| `payment_ecpay_ecpg` | `18.0.0.3.5` | depends `payment, payment_ecpay, portal, mail` ⇒ AIO addon 在平台 prod **必然一起安裝**、其 route 是活的 |
| `payment_ecpay`（AIO） | `18.0.1.1.1` | `installable: True`、無 `auto_install` |
| `ecpay_invoice_tw`／`ecpay_invoice_website` | `18.0.1.1.1` | |

#### 3.2.1 ECPG `payment_ecpay_ecpg`（平台 Odoo 走的那條）

`addons/payment_ecpay_ecpg/controllers/ecpg_callback.py`（行號＝vendored `93ab61bb`）：

- Route：`@http.route("/payment/ecpg/pay_callback", type="http", auth="public", methods=["GET","POST"], csrf=False)`（L28-29）。ECPay 3D 驗證完成後 POST form 欄位 `ResultData`（JSON 內含 AES 加密 `Data`）。綁卡另有 `/payment/ecpg/bind_callback`（L13-14）。**沒有** PaymentInfoURL／PeriodReturnURL（純信用卡綁卡扣款；定期扣款是 Odoo 自己的 `ecpay.subscription` cron 逐期建 tx，平台未用）。
- **不走** Odoo core 的 `_handle_notification_data` 鏈；controller 自己 search tx 後直接 `tx._set_done()`／`tx._set_error()`。
- 找 tx（L85-90）：以解密後 `OrderInfo.MerchantTradeNo` 比 `ecpg_merchant_trade_no`，fallback 比 `reference`；`limit=1, order="id desc"`。不比金額。
- **回應矩陣**：

  | 分支 | 行 | 回應 | ECPay 會不會重送（依 §1） |
  | --- | --- | --- | --- |
  | `ResultData` 解析失敗 | L38-40 | `0\|INVALID_RESULT_DATA`，HTTP 400 | 會 |
  | provider 找不到／AES 解密失敗／缺 MerchantTradeNo | L42-83 | 寫 dead-letter `ecpay.callback.log`（status `provider_not_found`／`decrypt_fail`／`missing_mtn`）＋ **`1\|OK`** | **不會** |
  | **tx 找不到** | L91-101 | dead-letter `unmatched_tx` ＋ **`1\|OK`** | **不會** |
  | tx 已在 `done`／`cancel`／**`error`** | L109-111 | 只 INFO log `already in state=…, skipping` ＋ `1\|OK`；**不寫任何欄位、不寫 dead-letter** | 不會 |
  | `RtnCode == 1` | L113-116 | `provider_reference = TradeNo`、`_set_done()`、`1\|OK` | 不會 |
  | `RtnCode != 1` | L117-119 | `_set_error(...)`、`1\|OK` | 不會 |

- 這個「失敗也回 `1|OK`」是刻意設計，`models/ecpay_callback_log.py:11-19` docstring 原句：
  > ECPay's ReturnURL contract is "we'll send the callback once or twice; once you ACK with `1|OK` we stop retrying". If our controller can't match the callback to a local record (…) it still has to ACK to prevent a retry storm — but doing so silently would drop money-affecting events with no audit trail.

  ⚠ 「once or twice」是 addon 作者的說法、**不是官方原文**；官方原文見 §1。
- **冪等性**：
  - 同一筆成功通知重複送達 → L109 guard 擋下（tx 已 `done`），零寫入。即使 guard 不在，core `_update_state` 仍是第二道（§3.1）。**安全**。
  - 成功後才收到失敗 → L109 擋下。**安全**。
  - 🔴 **失敗後才收到成功**：L109 把 `error` 也當終態 ⇒ 晚到的成功通知被**靜默丟棄**（只 INFO log，不寫 dead-letter）。註解 L103-108 說明理由：cron 對 MIT 3DS-pending 會主動 `_set_error()` 當放棄決定，不想被晚到的 callback 翻回 done；vendored test `tests/test_ecpg_payment.py:615 test_pay_callback_does_not_resurrect_error_tx_to_done` 釘住此行為。**這與 core 語意相反（core 允許 `error → done`）**。停機情境下若 3D 驗證 callback 晚到、而 tx 已被任何流程設成 error/cancel ⇒ 錢扣了、Odoo 永遠不知道（平台層 inbox 只在 HTTP 抵達時才有證據，見 §3.3）。
  - 沒有 CheckMacValue（ECPG 靠 AES-128-CBC 解密驗證，`sdk/aes_crypto.py:50-62`）；解密失敗不會誤入帳。
- 事後對帳痕跡：`ecpay.callback.log`（**只有失敗分支**寫；成功與「已終態跳過」**不寫**；cron 保留 90 天）、`payment.transaction.state/state_message/last_state_change/provider_reference`、Odoo server log（`already in state` INFO、core `_update_state` INFO/WARNING）。
- 主動查詢：SDK `ECPGClient.query_credit_trade(merchant_trade_no)`（`sdk/ecpg_client.py:451-517`，打 `ecpayment.ecpay.com.tw/1.0.0/CreditDetail/QueryTrade`）＋ `assess_for_credit(expected_amount)` 回 `SAFE/UNSAFE/INDETERMINATE`（L263-342）。**addon 內沒有任何呼叫者**（無 cron、無按鈕）；唯一呼叫者是平台 `src/models/wallet_topup.py:1352-1362`（§3.3）。⇒ **沒有現成「掃 pending tx → 查 ECPay → `_set_done`」流程**。

#### 3.2.2 AIO `payment_ecpay`（harumi 走的那條；平台 prod 也裝著）

`addons/payment_ecpay/controllers/main.py`：

- Routes：`/payment/ecpay/result_notify`（L25-31，POST／public／csrf False；＝`ReturnURL`）、`/payment/ecpay/info_notify`（L65-71；＝`PaymentInfoURL`）、`/payment/ecpay/website_return`（L103-110，GET/POST；＝`ClientBackURL` 與 `OrderResultURL`）。四個 URL 在 `models/payment_transaction.py:84-87` 設定。**沒有 `PeriodReturnURL` route**、rendering 也不送 Period 參數 ⇒ 未用 ECPay 定期定額。
- `result_notify`（L41-63）：先 `ecpay_check_mac_value()`（`models/payment_transaction.py:215-229`）；通過且 `SimulatePaid=="0" or RtnCode=="1" or result=="test"` 才 `_handle_notification_data("ECPay", post)` ＋ `order.ecpay.model.order_paid_record(post)`；回 `"1|OK"`。MAC 不符回 `"0|error"`；**任何例外也回 `"0|error"`**（L61-63）。
- 找 tx（`models/payment_transaction.py:116-144`）：以 **`CustomField1` ＝ `reference`** search；找不到或多筆 **raise `ValidationError`** → controller 捕捉 → `0|error` ⇒ **ECPay 會重送**（與 ECPG 相反）。`MerchantTradeNo`＝`odoo{N}{MMDDHHMMSS}`（L79）只當 ECPay 側訂單號、不用來匹配。
- 冪等性：
  - 重複成功通知 → `_process_notification_data` L160-161 `_set_done()` → core `_update_state` 只 log；`is_post_processed` 不重置 ⇒ 不重跑 SO confirm／auto invoice／`account.payment`。**但 `order.ecpay.model.order_paid_record`（`models/order_ecpay_model.py:81-105`）每次都 `(0,0,vals)` 新增一列** 掛到 SO `ecpay_info_ids`，無去重 ⇒ 重複通知會多一列「綠界支付資訊」（純資訊、無金流副作用；順帶 L105 有殘留 `print(result)`）。
  - 失敗後成功 → core 允許 `error → done` ⇒ **會補成 done**（與 ECPG 不同）。成功後失敗 → core 擋。**安全**。
  - `info_notify`（ATM／超商取號）→ `_set_pending()`；`order_info_record`（L33-79）以 `MerchantTradeNo` 去重（有則更新）。**安全**。
- ⚠ 側發現（非本題，建議另開 issue）：`/payment/ecpay/website_return` L123 對任何非空 POST 直接 `_handle_notification_data` **不驗 CheckMacValue**，且 `OrderResultURL` 也指向它 ⇒ 〔推論〕未驗證的 `CustomField1=<ref>&RtnCode=1&PaymentType=Credit` POST 可把 tx 設 done。
- 事後對帳痕跡：每次通知 `_logger.info(pprint(post))`（L41、L83）、`order.ecpay.model` 列、tx state。無 dead-letter model。
- 主動查詢：SDK 有 `order_search`（`QueryTradeInfo/V5`，`sdk/ecpay_payment_sdk.py:604-606`）與 `search_single_transaction`（`CreditDetail/QueryTrade/V2`，L768-770），**addon 內無呼叫者**。

#### 3.2.3 電子發票 `ecpay_invoice_tw`／`ecpay_invoice_website`

- **無 inbound controller**：兩個 addon 的 `controllers/main.py` 各只有 `/payment/ecpay/save_invoice_type`（前端存載具偏好），ECPay 發票 API 是同步請求／回應，沒有 ECPay → Odoo 的發票回呼 ⇒ 停機**不會丟發票回呼**（因為根本沒有）。
- 開立觸發點：(a) `account.move.action_post()` 後 hook（`models/account_invoice.py:107-117`，`company.auto_invoice == 'automatic'` 才開；平台 PaaS 發票被 `woow_skip_upstream_hook` 短路，改走 (d)）；(b) 表單按鈕；(c) 折讓 wizard；(d) 平台 cron（§3.3）。**都不掛在 `payment.transaction` 上** ⇒ 重複付款回呼不會觸發開票。
- 重複開立 guard：`create_ecpay_invoice()` L221-222 `if self.uniform_state == 'invoiced' or self.ecpay_invoice_id: raise UserError('此發票已開立電子發票，不可重複開立！')`；成功後 L297-298 先寫 `ecpay_invoice_id`／`uniform_state='invoiced'` 再呼叫 `get_ecpay_invoice_info()`（註解明寫是為了防「後者失敗導致重複開立」）。**同 move 重複 post／重複點按鈕 → 安全**。
- 🔴 **`RelateNumber` 每次隨機**：`uniform.invoice.related_number` 預設 `'ECPAY' + YYYYmmddHHMMSS + random(1e9..2^31)`（`models/uniform_invoice.py:25-26`），不是 move name 也不是 tx reference。`Check_Out()` 是單次 `requests.post(timeout=120)`（`sdk/ecpay_main.py:153`），逾時／連線錯／非 200 一律 `raise UserError`（L176-190）⇒ **ECPay 已開票但回應遺失**（例如 pod 在等回應時被 SIGTERM）時 L297-298 不會執行、下次重試會用**新的 RelateNumber** 再開一張 ⇒ ECPay 端無法靠 RelateNumber 去重（其去重語意見 §1）；沒有「先用 `InvoiceRemark`（＝move name）或前次 RelateNumber 打 `GetIssue` 再決定要不要重送」的前置。失敗留下的 numberless `uniform.invoice` 列會殘留。
- 查詢：`uniform.invoice.get_ecpay_invoice_info()`（`models/uniform_invoice.py:94-141`）以 RelateNumber 打 `B2CInvoice/GetIssue`，只在開票成功後與作廢後被呼叫；無 cron、無「用 move 找發票」的入口。

### 3.3 本 repo `woow_paas_platform` 對回呼的擴充（平台 Odoo 才有；harumi 沒有）〔已查證〕

行號以 worktree `origin/develop`（`66be66c9`）為準。

| 元件 | 位置 | 行為 | 對停機／重複回呼的意義 |
| --- | --- | --- | --- |
| `pay_callback` inbox override | `src/controllers/ecpg_callback.py:50-57` | 在交回 vendored `pay_callback` **之前**，用獨立交易把 `MerchantTradeNo`／`RtnCode`／`TradeNo` 寫進 `woow.billing.incident`（`_log_callback_evidence`）；**不改對外回應**（`return super().pay_callback(**kw)`） | 只要 HTTP 有到 Odoo，事實就落庫；停機期間 HTTP 根本到不了 ⇒ inbox 也是空的 |
| `payment.transaction._set_done/_set_error` 覆寫 | `src/models/payment_transaction.py:20-80` | super 之後對 `woow_topup_id` 呼叫 `topup._on_payment_done(tx)`／`_on_payment_failed(tx)`，只在 topup `state in ('draft','pending')` 時 | 即使 core `_update_state` 跳過（重複通知），這裡仍會呼叫 `_on_payment_done`，靠下一列的 guard 擋 |
| `woow.wallet.topup._on_payment_done` | `src/models/wallet_topup.py:543-671` | 平台唯一入帳 choke point：`SELECT … FOR UPDATE`（`lock_timeout` 1s）→ `invalidate_recordset` 重讀 → `if self.state == 'done' or self.wallet_transaction_id: return`（L614-619）→ `add_wallet_points()` → `write(state='done')` → `_ensure_topup_invoice()` | **重複回呼零重複入帳**（#864 PR-G，`tests/integration/test_callback_replay_864.py` 雙向競態實測）。等鎖逾時 → `LockNotAvailable` 往上拋 → 對 ECPay 是失敗回應、「由它重送」（L574-575，⚠ 這句依賴 ECPay 真的會重送，見 §1） |
| cleanup cron `_cron_cleanup_pending_topups` | `src/models/wallet_topup.py:1038-1151`；`src/data/cron_topup.xml`（每 10 分、`noupdate="1"`） | 先跑 `_cron_replay_callback_inbox()`（L1060）與 PR-F auto-recredit（L1067），再掃 `state='pending'` 且 `create_date` 早於 30 分（`_CLEANUP_GRACE_MIN`, L698）的 topup：tx done → 補入帳；tx error/cancel → topup failed；tx 仍 draft/pending 且早於 **60 分**（`_CLEANUP_HARD_CANCEL_MIN`, L699）→ 除非 inbox 有未 resolved 的 `rtn_code='1'` intent 或 `charged_not_credited`（`_pending_reconciliation_blocker`, L985-1036），否則 **`tx._set_canceled()` ＋ topup `cancelled`**（L1140-1151） | 🔴 **hard-cancel 前沒有主動向 ECPay 查單**，只看 inbox。停機 ≥ 60 分且 ECPay 不重送時：客戶已扣款、callback 從未抵達、inbox 空 ⇒ 恢復後第一趟 cron 就把 tx 設 `cancel`；之後就算 ECPay 補送成功，core `cancel → done` 不允許（§3.1 第 4 點）⇒ 錢扣了、點數沒進、且**沒有 incident**（inbox 只在 callback 抵達時寫） |
| PR-F QueryTrade auto-recredit | `src/models/wallet_topup.py:1153-1362`（`_auto_recredit_query` L1352 用 vendored `ECPGClient.query_credit_trade()` ＋ `assess_for_credit()`）；`src/models/ecpg_query_circuit.py`（403 shared circuit） | 只對 **`charged_not_credited`** incident（tx 已 done、入帳失敗）查 `CreditDetail/QueryTrade` 後補點；T0=10 分不查、T2=4 小時上限（[#864 ADR](../decisions/864-wallet-charged-not-credited.md) §D3） | **不是**「callback 遺失偵測」——candidate 來源是 incident 表，callback 沒到就沒有 incident。但它證明 vendored SDK 已有 `query_credit_trade()` 可用來做停機後對帳（§5） |
| 發票 cron `_cron_issue_pending_invoices` | `src/models/ecpay_invoice_cron.py:34-133`；`src/data/ecpay_invoice_cron.xml`（每 5 分） | 撿 posted `out_invoice` 且 `woow_invoice_issue_state in (pending, failed-且到重試時間)` → 寫 `issuing` → `create_ecpay_invoice()` → 寫 `issued`；開立前 `_woow_ecpay_issuance_confirmed()` 有真號碼就拒絕重開（L97-104） | 發票是**出站、非同步**：停機只是延後開立，不會丟。⚠ `issuing` 狀態**不在**候選 domain（L38-50）⇒ 若 pod 在「ECPay 已開立、Odoo 尚未 commit `issued`」之間被砍：同交易 rollback 回 `pending` 會再開一次（是否重複開票取決於 vendored `create_ecpay_invoice()` 是否中途 commit 與 ECPay 對 `RelateNumber` 的去重，見 §3.2／§1）；若 vendored 有中途 commit 則卡 `issuing` 無人撿。⇒ runbook：停機前先停這支 cron、確認沒有 `issuing` 列 |

另外，[#864 ADR](../decisions/864-wallet-charged-not-credited.md) §Options 與 `src/controllers/ecpg_callback.py:14-16` 都明寫「**我們對 ECPay 的重送政策零量測證據（只有 addon 註解）**」——本 scout §1 就是要把這個缺口用官方原文補上。

## 4. 情境對照表

| 情境 | ECPay 端 | 平台 Odoo（ECPG） | harumi（AIO） | 判定 |
| --- | --- | --- | --- | --- |
| 同一筆成功通知重複送達（重送落在恢復後、或人工「補觸發」） | 重送 ≤ 4 次 | vendored L109 guard（tx 已 done）→ 零寫入；core `_update_state` 第二道；平台 `_on_payment_done` FOR UPDATE＋state 守衛第三道 | core `_update_state` 只 log；`is_post_processed` 不重置 ⇒ 不重建 payment／invoice；**但 `order.ecpay.model` 多一列資訊列** | **安全**〔已查證〕 |
| 通知在停機內全部耗盡（停機 > 60 分或間隔取短） | 不再送 | tx 留 `pending`；30 分後 cleanup cron 看 inbox（空）⇒ **60 分 hard-cancel** `tx=cancel`＋topup `cancelled`；之後不會再有任何自動補救 | tx 留 `draft/pending`；SO 不確認；沒有自動 cancel（core `_cron_post_process` 不動 pending） | **平台有風險**（錢扣了、Odoo 當使用者放棄、零 incident）；harumi 只是「掛著」等人工 |
| 重送在恢復後抵達，但 tx 已被 cron `_set_canceled`／`_set_error` | 收到 `1\|OK` 停送 | vendored L109 把 `cancel`／`error` 當終態 ⇒ **靜默丟棄**（INFO log、無 dead-letter；平台 inbox 會記到 `rtn_code=1`，replay 對 `cancelled` topup 轉 `needs_human`） | core `error → done` 允許 ⇒ 會補成 done；`cancel → done` 不允許 | 平台：**有風險**但 inbox 有痕跡可人工補；harumi：error 可自癒、cancel 不行 |
| tx 不存在（DB 從舊 dump 還原、或 tx 在通知抵達前尚未 commit） | 收到 `1\|OK` 停送（ECPG）／收到 `0\|error` 重送（AIO） | dead-letter `unmatched_tx`＋`1\|OK`；平台自家 topup 有 intent 預登記＋inbox replay 補洞 | `ValidationError` → `0\|error` ⇒ ECPay 重送到當天 4 次為止 | 平台 topup 路徑已補；非 topup MTN 落 `callback_tx_missing` 人工；harumi 靠重送 |
| 解密／CheckMacValue 失敗 | — | dead-letter `decrypt_fail`＋`1\|OK`，不入帳 | `0\|error` 重送；⚠ `website_return` 不驗 MAC | ECPG 安全；AIO `website_return` 另開 issue |
| 發票開立時 pod 被砍（ECPay 已開、Odoo 未 commit） | 同步 API 無回呼；RelateNumber 唯一但 vendored 每次重試換新號 | 同交易 rollback 回 `pending` ⇒ 下一趟 cron 用新 RelateNumber **再開一張** | 同（`company.auto_invoice` 為 automatic 時掛在 `action_post`） | **有風險**〔推論〕：用 ECPay 後台以 `InvoiceRemark`（＝`move.name\|invoice_origin`，`account_invoice.py:256`）＋時間窗人工比對 |
| 停機期間到期的訂閱扣款（平台自家 cron） | — | cron 沒跑就沒建 tx、沒打 ECPay；恢復後 cron 補跑 ⇒ **延後、不重複**（`next_run` 推進＋`FOR UPDATE SKIP LOCKED`） | N/A | 安全〔已查證〕 |

## 5. 可直接抄進 runbook 的「停機期間金流」段

> 下面兩段以「可直接貼」為目標；括號內〔已查證〕／〔推論〕標記保留，抄進 runbook 時可刪。所有指令只描述要做什麼，不含 prod 連線細節（各 runbook 自己的 §連線 recipe）。

### 5.1 平台 Odoo 切換 runbook —「停機期間金流」

**事實基線**

- ECPay ECPG `ReturnURL` 未收到 `1|OK` 時「隔 5~15 分鐘後重發訊息給特店，當天重複發送四次」〔已查證，`?p=9058`〕。停機 < 20 分幾乎必有一次重送落在恢復後；20～60 分不保證；> 60 分視同全丟〔推論〕。
- 重複回呼對平台是**冪等**的：vendored `pay_callback` 終態 guard ＋ Odoo core `_update_state` ＋ 平台 `_on_payment_done` FOR UPDATE 守衛，實測零重複入帳〔已查證〕。⇒ **不需要**為了怕重複而擋回呼。
- 兩個會漏帳的洞（都不是「重複」造成的）：
  1. 平台 cleanup cron 對 `pending` 超過 60 分且 inbox 無證據的 topup 會 `_set_canceled`，之後晚到的成功回呼被 vendored 當終態丟棄（無 dead-letter）〔已查證〕。
  2. 發票 cron 在 ECPay 已開立、Odoo 未 commit 之間被砍，會用新 RelateNumber 重開〔推論〕。
- 發票 API 是同步、無回呼 ⇒ 停機只延後開立，不丟〔已查證〕。訂閱扣款是自家 cron，恢復後補跑、不重複〔已查證〕。

**T-1（停機前）**

1. 停用 cron（比照 harumi runbook 2.4 的 neutralize 清單，但這裡是 prod 真的要停）：`Woow PaaS: Cleanup stale pending topups`（`cron_topup_cleanup`）、`Issue pending ECPay e-invoices`（`src/data/ecpay_invoice_cron.xml`）、`Payment: Post-process transactions`（core）。**⛔ 只停這幾支，不改 `payment_provider.state`**——provider 從 enabled 改成 disabled／test 時 core 會 archive 所有綁卡 token（`payment/models/payment_provider.py:311-315` `_archive_linked_tokens()`），綁卡全丟。
2. 記錄基線：`SELECT id, reference, state, create_date FROM payment_transaction WHERE provider_code='ECPay' AND state IN ('draft','pending')`、`SELECT id, state FROM woow_wallet_topup WHERE state='pending'`、`SELECT id, name, woow_invoice_issue_state FROM account_move WHERE woow_invoice_issue_state IN ('pending','issuing','failed')`。這三張清單是恢復後對帳的 diff 基準。
3. 等發票 cron 當下那一趟跑完（log `[ECPAY_INV] cron processing` 之後沒有 `issuing` 殘留）再切。
4. 停機開始時間 **T0 寫進 runbook**（ECPay 重送窗口從這裡算）。

**T+0（恢復後、cron 仍停用）**

5. 先讓 ECPay 的重送進來：恢復後**至少等 15 分鐘**再開 cleanup cron（一個重送間隔上限）。這段時間 `/payment/ecpg/pay_callback` 已經在收，平台 inbox（`woow.billing.incident`）會記下 `rtn_code`／`trade_no`。
6. 對帳（停機 ≥ 15 分必做；< 15 分建議也做）：對步驟 2 清單裡**每一筆** `pending` tx／topup，以及停機窗口 `[T0−15min, 恢復+15min]` 內 `create_date` 的新 tx：
   - 用 vendored SDK `ECPGClient.query_credit_trade(merchant_trade_no)`（`payment_ecpay_ecpg/sdk/ecpg_client.py:451`）查 `CreditDetail/QueryTrade`，看 `assess_for_credit()`／`Status`：`Authorized`／`Captured`／`To be captured` ⇒ ECPay 有扣款；`Unauthorized`／`Canceled` ⇒ 沒扣。官方要求付款後 ≥ 10 分才查、同一筆 10 分內不重查、太快會 403 鎖 30 分〔已查證，`?p=9083`／`?p=9168`〕⇒ 批次間隔放慢，一次 403 就停 30 分。
   - 有扣款但 tx 仍 `pending` ⇒ 用 platform choke point 補：`tx._set_done()`（平台 `_set_done` override 會連動 `topup._on_payment_done`），**不要**直接寫 wallet。這一步在 cleanup cron 重新開啟**之前**做完，否則 60 分 hard-cancel 會先把它關掉。
   - 有扣款但 tx 已 `cancel`／`error`（例如恢復後 cron 先跑了）⇒ core 不允許 `cancel → done`；走 #864 的 reconstructed-row／query-gated repair 路徑（`repair_incident_ref`），⛔ 不要繞過 guard 直接 `write({'state':'done'})`。
   - 沒扣款 ⇒ 留給 cleanup cron 正常 cancel。
   - 對帳結果（筆數、MTN、處置）寫進 runbook 報告。
7. 發票：查 `account_move.woow_invoice_issue_state='issuing'`——正常應為 0 筆；有就代表 cron 在 ECPay 呼叫中被砍，先到 ECPay 廠商後台以 `InvoiceRemark`＝`<move.name>|<invoice_origin>` 與時間窗查有沒有已開立的發票，有就人工把號碼綁回 `uniform.invoice`／`ecpay_invoice_id` 再改 `issued`；沒有才改回 `pending` 讓 cron 重開。
8. 手動補觸發（備用）：ECPay 廠商管理後台 → 信用卡收單 → 交易明細查詢 → 授權單號 →「授權回傳(補觸發)」〔已查證，`?p=61973`〕。只在步驟 6 的查詢 API 打不通（403 鎖住、憑證問題）時用；回呼進來後走的仍是 `pay_callback` 同一條冪等路徑。
9. 依序重新啟用 cron：發票 cron → core post-process → cleanup cron（最後）。

**觀測點**：Odoo log `[ECPG_INBOX]`（inbox 寫入）、`ECPG pay_callback: tx … already in state=…, skipping`（重複通知被擋，正常）、`[TOPUP] cron cleanup hard-cancel stuck 3DS`（⚠ 恢復後 15 分內不該出現）、`[TOPUP_REPLAY] scanned=… credited=…`。

### 5.2 harumi 搬遷 runbook —「停機期間金流」

**事實基線**

- harumi 用 AIO `payment_ecpay`（`ReturnURL`／`PaymentInfoURL`／`ClientBackURL`／`OrderResultURL`）；重送政策同 5.1（`?p=2878`／`?p=2881`）〔已查證〕。無 `PeriodReturnURL` route（未用 ECPay 定期定額）〔已查證，以 paas-odoo-ci vendored 版為準；⚠ harumi 現場安裝的 `payment_ecpay` 版本尚未從 Phase 0 dump 核對〕。
- AIO 的重複回呼**安全**：core `_update_state` 守門，`account.payment`／auto invoice 不重建；只會在 SO 上多一列「綠界支付資訊」〔已查證〕。`error → done` 允許（與 ECPG 不同）⇒ 晚到的成功通知可自癒。
- AIO 對「tx 找不到」回 `0|error` ⇒ ECPay 會重送到當天 4 次為止〔已查證〕。⇒ **若新站 DB 是 T0 之前的 dump，T0 之後在舊站產生的 tx 在新站不存在**，ECPay 重送 4 次都會被 `0|error` 拒收後放棄；這正是 runbook 3.3「T0 先關帳、確認沒有 open session 與 draft order」的金流版理由——**T0 之後不得再有人走 ECPay 付款**。
- ATM／超商（若 harumi 有開）：`PaymentInfoURL` 取號通知同樣重送 4 次；付款本身是離線的，ECPay 說「請待綠界通知的付款結果」〔已查證，`?p=2890`〕，沒有手動重送按鈕〔找不到〕⇒ 停機 > 60 分內落地的超商／ATM 繳款通知只能靠 `QueryTradeInfo/V5` 補。

**T-1**

1. 依 runbook 3.1／3.3：`ir_cron` 全停（含 core `Payment: Post-process transactions`）；`payment_provider` **在新站演練時** disabled（3.1 neutralize），**正式切換時不要 disable**（會 archive token；harumi 若有綁卡才有影響，無綁卡則無差）。
2. T0 關帳後，記錄 `payment_transaction WHERE provider_code='ECPay' AND state IN ('draft','pending')` 清單（AIO 沒有平台那種 inbox，這張清單就是唯一基準）＋ 記錄 T0。
3. 確認 ECPay 廠商後台的 `ReturnURL`／`PaymentInfoURL` 設定的是 **hostname 而非 IP**，且切換後 hostname 不變（runbook D5／R8）——回呼是打 hostname，DNS／tunnel 切過去回呼就跟著過去。

**T+0**

4. 新站起來後先**不要**啟用 core post-process cron；等 15 分讓重送進來（AIO `result_notify` 每筆會 `_logger.info` 印整包 POST，`grep "ECPay payment notification received"` 就是收到的證據）。
5. 對步驟 2 清單 ＋ 停機窗口內的每一筆 `pending` tx：打 `QueryTradeInfo/V5`（SDK `order_search`，`payment_ecpay/sdk/ecpay_payment_sdk.py:604`；addon 內無現成呼叫者，要在 odoo shell 手動呼叫）看 `TradeStatus`：`1` 已付款 ⇒ `tx._set_done()` 後跑 `tx._post_process()`（或開 cron 讓它撿）；`0` 未付款／`10200095` 未成立 ⇒ 留著或 `_set_canceled()`。同樣遵守「付款後 ≥ 10 分才查、403 等 30 分」。
6. 若 harumi 有開 ATM／超商：停機窗口內的取號通知丟了會讓 SO 缺 `PaymentInfoURL` 的帳號／代碼（客戶端 redirect 也丟）⇒ 用 `QueryTradeInfo/V5` 對 `pending` tx 逐筆查；已繳款者 `_set_done()`。
7. 手動補觸發同 5.1 步驟 8（信用卡限定）。
8. 對帳完成、清單清空後才啟用 `Payment: Post-process transactions` 與其餘 cron。
9. **rollback 截止點**（runbook 4.4）不變：新站出現第一筆真實 ECPay 交易前可回退；之後回退會讓新站已收到的回呼在舊站變成「tx 找不到」⇒ 只能 forward-fix。

### 5.3 兩份 runbook 共同的「不要做」

- ⛔ 不要為了怕重複回呼而在停機期間把 ReturnURL 指到別處或回假的 `1|OK`——回 `1|OK` 就是告訴 ECPay「別再送」〔已查證〕，通知會真的丟。維持 5xx 反而讓 ECPay 重送。
- ⛔ 不要在對帳前先開 cleanup cron（平台）。
- ⛔ 不要 disable `payment_provider`（會 archive 綁卡 token）。
- ⛔ 不要直接 `UPDATE payment_transaction SET state='done'`；一律走 `_set_done()` 讓 post-process／topup 連動與 log 落地。

## 6. 後續建議（不在本 scout 射程內拍板）

1. **平台 cleanup cron 的 hard-cancel 前加 query gate**：`_cron_cleanup_pending_topups` 在 `_set_canceled()` 前先 `query_credit_trade()`，有扣款就走 `_on_payment_done`。SDK 已有、PR-F 的 circuit／T0 節奏可直接沿用；這會把「停機 > 60 分 ⇒ 漏帳」變成自癒。屬 #864 PR-H 射程（`ecpg_callback.py:14-16`）。
2. **vendored ECPG `pay_callback` 對 `error` 終態的處理**：至少寫 dead-letter（目前只 INFO log），讓晚到的成功通知有 DB 痕跡。upstream `ecpay_odoo18` issue。
3. **發票 RelateNumber 改為確定性**（例如 `move.name` 衍生）或開立前先 `GetIssue`：讓 ECPay 端的唯一性約束真的能擋重複。upstream issue。
4. **AIO `website_return` 不驗 CheckMacValue**（§3.2.2 側發現）：另開 issue。
5. #976（每日 `QueryTradeMedia` 對帳）落地後，本文 §5 的人工對帳可降級為「確認當日對帳檔無差異」。

## 7. 查過的來源清單

### ECPay 官方（developers.ecpay.com.tw，皆 2026-09-22 curl）

| 頁 | 用途 | 結果 |
| --- | --- | --- |
| `?p=2878` 付款結果通知（AIO ReturnURL） | §1.1 | 有用 |
| `?p=2881` ATM/CVS/BARCODE 取號結果通知 | §1.2 | 有用 |
| `?p=15162` 定期定額、`?p=2868` 信用卡定期定額、`?p=9093`／`/46100/` 定期定額查詢、`?p=12130` | §1.3、§2.2 | 有用 |
| `?p=9058` ReturnURL（站內付 2.0）、`?p=9040` 付款、`?p=9053` 建立交易、`?p=9035` 簡介、`?p=35630`／`?p=51330` 幕後交易授權 | §1.4 | 有用 |
| `?p=61944`、`?p=61983`（AIO FAQ）、`?p=61973`（站內付 2.0 FAQ） | §1.1 交叉、§2.1 | 有用 |
| `?p=2890` 查詢訂單（AIO QueryTradeInfo/V5） | §2.2 | 有用 |
| `?p=9083` 查詢訂單（ECPG Cashier/QueryTrade）、`?p=9088` 查詢信用卡單筆明細、`?p=9073` 信用卡請退款、`?p=16406` 下載撥款對帳檔 | §2.2 | 有用 |
| `?p=9168` 介接注意事項 | §2.2 rate limit | 有用 |
| `?p=2872` ATM 虛擬帳號、`/2874/` CVS、`?p=2876` BARCODE、`?p=64707`、`/64719/`、`/45927/`、`/37942/`、`/64715/`、`?p=2860` | 幕後／前景回傳定義、產品線區分 | 查過、部分引用 |
| `?p=7896` 一般開立發票、`?p=7923` 查詢發票明細、`?p=7906` 作廢發票 | §1.5、§2.3 | 有用 |
| `/14850/` 開立發票 | 實為 B2B，排除 | 無用 |
| `/5740/`、`?p=24319` 交易訊息代碼一覽表、`?p=13853` 錯誤代碼查詢 | RelateNumber 重複錯誤碼 | **找不到** |
| `?p=8993` | | 404 |

未查：ECPay 官方 GitHub（`github.com/ECPay`）README／PDF、`www.ecpay.com.tw/Content/files/gw_p120.pdf`——developers 站頁面已足夠回答。

### 原始碼

| 來源 | 版本 | 取法 |
| --- | --- | --- |
| vendored `payment_ecpay_ecpg` 18.0.0.3.5、`payment_ecpay` 18.0.1.1.1、`ecpay_invoice_tw`／`ecpay_invoice_website` 18.0.1.1.1 | Gitea `woow-paas/paas-odoo-ci` `main`@`93ab61bb`（2026-09-04） | `tea api` 列目錄＋ `curl …/raw/addons/<addon>/<path>` |
| upstream `~/woow-repos/WOOW/ecpay_odoo18/` | `0cb38c2`（ecpg 18.0.0.3.3） | 本機 checkout，只作 diff 對照 |
| Odoo core `payment`、`account_payment`、`sale` | GitHub `odoo/odoo` 18.0 branch（2026-09-22 raw） | `curl -sL https://raw.githubusercontent.com/odoo/odoo/18.0/addons/…` |
| `woow_paas_platform` | worktree `origin/develop`@`66be66c9` | 本 repo |

### 內部文件

- [#864 ADR](../decisions/864-wallet-charged-not-credited.md)＋ Gitea #864 留言 10916（ECPG 查單 API 調查）、10918、10970。
- [harumi 移植計畫](2026-09-18-harumi-odoo-paas-migration-plan.md)（runbook 3.x／4.4 步驟編號依此）。
