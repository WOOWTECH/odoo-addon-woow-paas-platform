# project 525 a1 skip trial rollback landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_525_a1_skip_trial_rollback_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#525 A1 skip-trial 金流殘留修好（PR#527）：cr.savepoint() 自帶 cr.clear()、零 mock 的扣點後注入法、AND 掩蓋的破解法"

2026-08-02 落地 #525 A1（`api_service_skip_trial` 吞例外 → 錢包已扣／發票已 posted 卻回失敗、且重試被自己的殘留鎖死）＝ **PR#527**（develop，manifest .90→.91，`Refs #525` 不 close，因為 #525 還涵蓋另外 7 個位置）。issue #525 仍 open。

**三個可重用的事實**（做 #525 剩下的 A2／A3／B1-B5 時直接套）：

1. 🔴 **`request.env.cr.savepoint()` 已經幫你清髒 cache**——`sql_db.py` 的 `_FlushingSavepoint.rollback()` 是 `self._cr.clear()` **再** `ROLLBACK TO SAVEPOINT`，而 `BaseCursor.clear()` → `Transaction.clear()` → `cache.clear()` ＋ `tocompute.clear()` ＋ `precommit.clear()`。所以 #525「共通修法注意」講的「rollback 後 env cache 仍髒，要一併 env.clear()」**只適用於裸 `cr.rollback()`**；改用 savepoint 就不必自己補。進 context manager 時還會先 `cr.flush()`，savepoint 邊界是精確的。另：`ROLLBACK TO SAVEPOINT` 能救回被 psycopg2 錯誤弄壞的 transaction，裸 rollback 則是整條丟掉。
   選 savepoint 而非 #520／PR#523 那種整個 request 的 `cr.rollback()` 的判準＝**殘留跨幾個 model／try 之前有沒有要保留的寫入**。A1 殘留橫跨訂閱、account.move（含明細、已 posted）、loyalty.card、wallet.transaction 四個 model ⇒ 界定在「這一段」比界定在「整個 request」穩健，不必依賴「try 之前永遠沒有別的寫入」這個會隨程式碼演進失效的前提。repo 內既有 idiom：`src/controllers/ha_paas.py:208`。

2. 🔴 **要在「錢已扣走之後」造失敗，不必 mock**——把公司的 `sh_paid_subscription_journal` 清掉＋停用該公司所有 bank／cash journal，`account_move.py::_pay_from_wallet` 就會走到它本來就有的 `raise UserError("No payment journal configured for company ...")`。那一行的位置在 `action_post()`（發票 posted、序號消耗）與 `deduct_wallet_points()`（點數已扣＋wallet.transaction 已 INSERT）**之後**，正好是最深的殘留形狀，且是真實會發生的設定錯誤。只有需要指定例外**型別**（AccessError／未預期）時才另外 patch `type(env['account.payment']).action_post`——同樣落在扣點之後。取證要看 log 有 `[INV] _pay_from_wallet no journal invoice=`，確認真的走到那條而不是被更早的前置檢查擋掉。

3. **HttpCase 能看見 request 內的寫入**是因為 `tests/common.py` 的 `Opener.request()` 每次請求前跑 `cr.flush()` ＋ `cr.clear()`，且 request 用的是包在同一條 transaction 上的 `TestCursor`（`commit`/`rollback` 被實作成 savepoint 操作）。所以 fixture 改完不必手動 flush，但 request 回來後要 `env.invalidate_all()` 才讀得到新值。

**AND 掩蓋的破解法（本次實際做法）**：11 條測試每條只放**一個**核心斷言 ⇒ RED 一次跑出 9 條各自紅在預期斷言上，沒有互相掩蓋。修前就綠的兩條（陽性對照、業務訊息 lock）**另外用注入式變異證明會紅**：變異 A＝`with cr.savepoint() as sp: ...; sp.close(rollback=True)`（成功也回捲）→ 只有陽性對照紅；再逐一拿掉它前面的斷言重跑三次，證明四個探針**各自**都會紅（點數／wallet.transaction／account.move／sh_trial）。變異 B＝業務分支改回泛用訊息 → 只有 lock 那條紅。這比「宣稱它會紅」強一個等級，見 [[feedback_post_hoc_test_lacks_red_phase]]、[[project_520_create_service_rollback_landing]]。

🔴 **最重要的一課（rebase 後自我稽核才發現）：注入深度是「宣稱」不是「斷言」＝整組守線的假綠開關。** 檔頭寫著「失敗點落在 `action_post()` 與 `deduct_wallet_points()` 之後」，但**沒有任何斷言在守這句話**。把 `deduct_wallet_points` 整段移到 `_pay_from_wallet` 末尾（模擬有人重排）＋**controller 維持修復前版本**實測：`9 failed of 13` → `7 failed of 13`，翻綠的正好是四條靠「錢包被扣過」當前提的斷言（does_not_charge／leaves_no_wallet_transaction／access_error_still_rolls_back／unexpected_error_still_rolls_back）——**缺陷完全沒修、守線卻報綠**。修法＝把 `deduct_wallet_points` 包成**記錄呼叫次數**的版本（行為不變、不是 stub），斷言 `len(debits) == 1`；計數是 Python 物件不被 savepoint 回捲，所以修好後仍讀得到「扣點曾經發生又被撤回」。**通則：凡是「注入在 X 之後 ⇒ 殘留存在 ⇒ 斷言沒有殘留」的測試，都要有一條把「注入真的在 X 之後」釘成斷言的 scaffold 測試，否則 X 一被搬動整組退化成恆真。**

同輪另補：三條訊息面測試的 `assertNotIn(x, result.get('error') or '')`／`assertNotEqual(error, GENERIC)` 在 error 缺席或空字串時恆真（#529 D 組同構）⇒ 改成**先斷言訊息是預期的那一則**再斷言它不含不該有的東西；業務訊息那條綁 `company.name` 這個**資料**而非措辭。

**rebase 實例**：#526 已先把 develop bump 到 `.91`＝我原本 bump 的同值 ⇒ 無衝突、diff 空白、manifest 檔整個從 commit 消失，改 bump `.92`（見 [[reference_rebase_silently_eats_manifest_bump]]）。另：注入腳本的錨點 `return {'success': False, 'error': message}` 在 paas.py 有**兩處**（#523 的 `_create_service` 也是這行）⇒ `assert count == 1` 失敗、變異沒套用，那次 `0 failed` 是沒變異的重跑、不算數——**變異驗證要先確認變異真的套上了**（grep marker）。

全套對照（本機 docker-desktop 兩步法、各自乾淨 DB）：
- 初版：baseline `develop@7f177057` = **0F/0E of 2176**；修法後 = **0F/0E of 2187**（＝2176＋11）。
- rebase 到 `453f6e69`（含 #526／#529）後重量：baseline = **0F/0E of 2191**；修法後 = **0F/0E of 2204**（＝2191＋13）。**別沿用舊基準**——#529 增刪測試、#526 也加了一組。

順手發現但**沒動**：同檔 `api_service_skip_trial_estimate` 也是 `str(e)` 無差別原文回傳（同樣的內部技術名外洩面），但它 side-effect-free、不在 #525 盤點的 8 個位置內。

相關：[[project_520_create_service_rollback_landing]]、[[reference_odoo_controller_swallowed_exception_still_commits]]、[[feedback_negative_verification_needs_positive_control]]
