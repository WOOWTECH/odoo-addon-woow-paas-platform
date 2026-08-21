# project 29 389 mixin sudo dedup landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_29_389_mixin_sudo_dedup_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："2026-08-01 leader2 派工雙 PR——#29 mixin cascade sudo（PR#506）＋#389 低餘額 dedup marker 重試（PR#507），兩者都是「側效果沒發生卻記了已發生」"

2026-08-01 leader2 派工，兩支獨立 PR 開在 develop，均**已實跑全套測試**（本機 docker-desktop paas-odoo image + bind-mount worktree src，見 [[reference_odoo_test_docker_desktop_paas_image]]）：

- **PR #506**（`fix/29-mixin-sudo`，Closes #29）：`subscription_lifecycle_mixin` 三個狀態判準裸讀 `self.subscription_id.state` → 只有 `base.group_user` 的 workspace 擁有者在 SH delete/provision、CS update/delete 撞 AccessError。全 mixin 收斂到唯一入口 `_sub()`（`self.sudo().subscription_id`，7 處）＋ repo-lint **第 12 條 gate** `check_mixin_subscription_sudo`（AST，只認裸形態）。27 條測試 9F/18E → 0F/0E；全套 **0F/0E of 2157**。
- **PR #507**（`fix/389-low-balance-dedup`，Refs #389 **不 Closes**）：`_on_payment_failure` 在呼叫通知函式當下就寫 dedup marker。8 條測試 3F/3E → 0F/0E；全套 **0F/0E of 2138**；manifest .88→.89（新欄位）。

## 兩題同一個形狀

**「我做了動作」≠「側效果發生了」**——marker/判準記在動作點，而動作有靜默失敗的出口，結果就是永久性的錯誤狀態。#29 是「cache 命中就以為有權限」，#389 是「排進佇列就以為送到了」。

## #389 的關鍵事實（修 1、2 條救不了 prod）

`force_send=False` **只排入佇列**，投遞是 mail queue cron；失敗的信停在 `mail.mail.state='exception'`，而 **Odoo scheduler 只撿 `outgoing`——沒有任何人會重試它**。所以「template 缺席」「send_mail 丟例外」這兩條就算修好，#389 的 prod 症狀照樣發生（那兩步當時是成功的）。修法必須記住載體（新欄位 `woow_last_low_balance_mail_id`）並看它的下場。

「查不到載體 → 判成已送達」是刻意的：Odoo 對 `auto_delete` 的信送出成功後會刪掉 mail.mail，而升級前的既有資料欄位是 NULL——同一條規則讓上線當天不會對所有欠費租戶補發一輪。重送上限由 dunning threshold（預設 6）天然界定。

## 撞到的兩個 Odoo 測試框架坑

見 [[reference_odoo_assertraises_primes_cache_masks_accesserror]]（assertRaises 餵飽 cache 蓋掉 AccessError）與 [[reference_odoo_test_loader_skips_inherited_methods]]（mixin 基底的 test method 一條都不收，跑成 0 tests 而非紅燈）。兩個都會讓測試**在修復前就綠**，符合 [[feedback_never_fabricate_tool_output]] 的教訓：宣告 RED 之前要真的看到紅、而且要看紅在哪一行。

## 勘誤（2026-08-01 PR#507 獨立 review 查證源碼後）

上面「查不到載體＝送成功」的敘述**只講了三分之一**——見 [[reference_odoo_mail_autodelete_unlink_on_invalid_email]]。且 PR#507 當時**未合併就被判 partial**：`woow_last_low_balance_warned_for` 有第二個寫入者 `src/models/low_balance_warning_cron.py:103`（每日 pre-renewal cron）沒被一起改，它寫同一個 key 卻不寫載體，於是 #389 的實際 prod 時序（D-3 預警先排信 → SMTP 掛 → D 日 dunning 判「有 marker 無載體」）照樣整週期靜音。教訓：**改「判準的語意」時要先盤點該欄位的所有寫入者**，只改讀取端與其中一個寫入端等於沒改。

## R1 回修（2026-08-01，PR#507 v2 = ffbc5f0d）

S1 blocking 已修：判準／排信／蓋章收斂成三個共用 helper（`_queue_low_balance_mail` / `_low_balance_warning_needs_send` / `_stamp_low_balance_warning`），**兩個寫入者**都走同一條。另外三條判斷後全收：

- **S3**：載體排入後顯式 `auto_delete=False`。修在「建立載體」那一點而不是改 template——template 在 `<data noupdate="1">` 內，改 XML 對既有 DB 是 no-op 還要多一支 migration，而且只有被當載體的信才需要這個約束。
- **S4**：`state='cancel'` 從「靠 `!= 'exception'` 剛好涵蓋」改成顯式分支＋warning。行為不變，改的是**被測試保護**。
- **S2**：新增 `_LOW_BALANCE_RETRY_WINDOW_DAYS = 7` 當與設定無關的安全閥——`max_consecutive_payment_failures = 0` 是合法設定，停用後重送就無上限。**測試原本硬寫 threshold=0 當「可以多跑幾 tick」的免費午餐，那正好是唯一上限被抽掉的那個設定**，已改成預設帶 6。

RED 撲空一次值得記：三條 pre-renewal 測試第一版紅在 `0 != 1`，看起來像抓到缺陷，其實是 fixture 壞掉——**直接 `create` 訂閱不會跑 onchange 鏈，`sh_plan_price` 留 0，而預警 cron 第一道 guard 就是 `price <= 0: return`**。紅了不等於紅對地方，要逐條看失敗訊息是不是「它要抓的那個缺陷」。

全套 **0F/0E of 2171**。

## 待辦（不在這兩支 PR 內）

- #389 主體：prod SMTP root cause（`ir.mail_server` / 憑證 / 叢集 egress）＋積壓 mail queue。
- SMTP 修好後若要補發歷史受害者，需一次性 script 清掉受影響訂閱的 `woow_last_low_balance_warned_for`（既有資料新欄位是 NULL，會被判成已送達）。
