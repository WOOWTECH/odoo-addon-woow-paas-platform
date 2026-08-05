# reference odoo controller swallowed exception still commits

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_odoo_controller_swallowed_exception_still_commits`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：Odoo controller 用 except 吞掉例外後回 200，交易照常 commit——靠 raise 做 rollback 的 model 層守衛會整個失效

Odoo model 層若靠 `raise ValidationError` 讓交易 rollback 來撤回已 INSERT 的列（`super().create()` 之後才檢查、不通過就 raise），**只要唯一的呼叫端是 `type="json"` controller 且用 `except Exception` 吞掉例外並 `return {'success': False}`，撤回就從未發生**：例外沒傳播到框架層 ⇒ request 正常結束 ⇒ Odoo 在收尾照常 flush + commit（`odoo/service/model.py` `retrying()`），那列留在 DB。

症狀組合很有辨識度：**畫面顯示「建立失敗」，服務／資料卻照樣出現而且是好的狀態**。#520 實例：`max_per_partner` 上限守衛擋下第二個 Hermes Agent，但 cloud_service 列留在 DB，被 `Deploy Pending` cron 撿去 helm install → Running，且因為 raise 中斷了後續步驟，該列**沒有訂閱（不計費）也沒有 admin password secret**（一次性密碼也沒回給使用者）。

診斷要點：

- `create_date` 精準等於報錯時刻、事後查 count 卻不成立 ⇒ 別懷疑判斷式，先想「當時的第二筆後來被刪了」。查 werkzeug log 找耗時數秒的 `POST .../services/<id>`（那是刪除，會呼叫 operator uninstall），再用 operator log 的 orphan PVC 名稱（`svc-xxxx-<chart>-data`）反證那筆的身分。
- id sequence `last_value` 沒跳號 ⇒ 沒有 rollback 掉的列，配合「只有一次 create request」可排除 double-submit。

修法注意（#525 盤點出的共通坑）：

- **`cr.rollback()` 之後 env cache 仍是髒的**，要一併 `env.clear()`，或改用 savepoint 包住目標寫入才安全（只補 rollback 會回捲整個 request 先前的寫入）。
- repo 內既有 savepoint idiom：`src/controllers/ha_paas.py:208`。
- 業務例外（`ValidationError`/`UserError`）要原文回前端，但 **`AccessError` 是 `UserError` 的子類**——單寫 `except UserError` 會把 Odoo 內部 model／欄位技術名吐給租戶。
- 正確參照形態：`_update_mcp_server`（不包 try，讓例外傳到 dispatcher 由框架 rollback）。

相關：[[project_520_create_service_rollback_landing]]、[[project_477_fail_open_family_closeout]]
