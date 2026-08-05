# reference odoo assertraises primes cache masks accesserror

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_odoo_assertraises_primes_cache_masks_accesserror`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Odoo 的 assertRaises 只在預期 AccessError 時清 cache，寫成 assertRaises(UserError) 會把該冒出來的 AccessError 蓋掉

Odoo `BaseCase._assertRaises`（`odoo/tests/common.py:472-492`）進場先開 `self.env.cr.savepoint()`，**savepoint 會 flush，flush 把大量記錄餵回 cache**。Odoo 自己知道這會遮 ACL，所以緊接著補一句 `if issubclass(exception, AccessError): self.env.cr.clear()`——**只在預期例外是 AccessError 時才清**。

後果：`with self.assertRaises(UserError):` 包住的 block，裡面本來會冒 AccessError 的讀取會**命中 cache 而不查 ACL**，於是拿到的是更下游的那個業務例外。2026-08-01 #29 實測同一情境：用 `assertRaises(UserError)` 得到 `UserError('...suspended...')`、改手寫 `try/except` 得到 `AccessError`。這條讓「有擋到、而且擋的原因不是 ACL」的斷言**在修復前也綠**。

- 要驗「被擋下且原因不是權限」→ 手寫 `try/except Exception as exc` 再對 `exc` 斷言，不要用 `assertRaises`。
- `AccessError` 是 `UserError` 的子類別（`odoo/exceptions.py:64`），所以除了型別要 `assertNotIsInstance(exc, AccessError)`，訊息也要一起釘。
- 反過來，`assertRaises(AccessError)` 是安全的（Odoo 有清 cache），拿來做「這個 fixture 使用者真的沒權限」的鑑別力驗證沒問題。

這是 [[reference_odoo_sudo_getter_cache_priming_test]] 的同族——差別在污染源是**測試框架自己**，不是測試碼裡的前置讀取。相關落地見 [[project_29_389_mixin_sudo_dedup_landing]]。
