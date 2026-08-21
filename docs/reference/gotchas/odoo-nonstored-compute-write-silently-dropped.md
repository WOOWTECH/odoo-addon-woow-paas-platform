# reference odoo nonstored compute write silently dropped

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_odoo_nonstored_compute_write_silently_dropped`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：對 non-stored compute 欄位 create/write 不拋例外、值直接丟棄（DB 連欄位都沒有）——fixture 這樣灌前提會靜默不成立；res.partner.wallet_balance 是本 repo 實例

`fields.X(compute=..., store=False)` 且**無 `inverse=`** 的欄位，`create()` 與 `write()`
**都不拋例外**，值直接被丟棄。2026-08-02 於 `res.partner.wallet_balance`（`sh_subscription_wallet`）
實測（odoo shell，flush_all + invalidate_all 後讀回）：

```
WB5 field: store=False compute=_compute_wallet_balance inverse=None readonly=True
WB4 res_partner 有無 wallet_balance 實體欄位: []          ← 連落點都沒有
WB1 create({wallet_balance:1000.0}) 讀回 = 0.0            ← 靜默 no-op
WB2 陽性對照 add_wallet_points(1000.0) 讀回 = 1000.0      ← 探針有鑑別力
WB3 write({wallet_balance:500.0}) 未拋例外，讀回 = 0.0     ← write 路徑同樣靜默
```

**要先分清兩種形狀，失效方式不同**：

- **non-stored compute（本例）**：DB 沒有該欄位 ⇒ 值**沒有落點**、直接丟棄。查法：
  `select column_name from information_schema.columns where table_name=... ` 回空集合。
- **stored compute without inverse**：值寫得進 DB，但下次 recompute 會蓋掉 ⇒ 症狀是
  「先讀對、後來變了」，跟前者的「一開始就是 0」完全不同。

⇒ 判準用 `Model._fields['x']` 的 `store` / `inverse` 屬性讀，別用肉眼看欄位定義
（可能被別的 addon 繼承覆寫；本例實查全 addons 路徑只有一處定義才敢下結論）。

**為什麼危險**：測試 fixture 常用 `create({'balance': 100.0})` 建立「餘額不足／足夠」的前提。
前提靜默不成立時，測試不會紅，只會**測到別的東西**。本 repo 唯一有效的灌錢包途徑是
`partner.sudo().add_wallet_points(amount, description=...)`。

**但「有這個寫法」≠「那條測試壞了」**——2026-08-02 逐處實測本 repo 4 處，**後果全部不成立**：

| 位置 | 值 | 為什麼無害 |
| --- | --- | --- |
| `test_low_balance_notification.py:15`（舊 class setUp） | 100.0 | 讀它的兩處都是**相對值**（`before` / `before + 400.0` / assert unchanged），0.0 起跳照樣成立 |
| `test_ecpay_invoice_subscription.py:25` | 1000.0 | 全檔沒有任何地方讀 `wallet_balance`；測的是 invoice carrier |
| `test_subscription_failure_handling.py:38` | 參數化 | 7 個呼叫端**全用預設 0.0**，與實際值一致 |
| 同檔 `:253` `partner.wallet_balance = 10000` | 10000 | 「客戶儲值了」的語意由 `_patch_settle(succeed=True)` 承載——它用 lambda 整個取代 `_try_settle_invoice`，餘額不參與 |
| `test_low_balance_dedup_retry_389.py:85` | 0.0 | 值與實際相同 |

⇒ 這是**誤導性死設定**（讀者會以為前提成立），不是缺陷。當時我把它當缺陷提報，被要求
「先驗證再開單」，實測後撤回——正是 [[project_520_create_service_rollback_landing]]
「行號全中但因果與程式碼不符」的同一個坑，**自己標了「這仍是對方的宣稱」就必須把它當待辦而不是免責**。

相關：[[reference_odoo_empty_string_vs_false_vacuous_assertnotin]]、
[[reference_odoo_cr_clear_discards_pending_writes]]、
[[feedback_negative_verification_needs_positive_control]]、
[[project_t1_autotopup_behaviour_guard_verification]]
