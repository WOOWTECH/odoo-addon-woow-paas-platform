# project 525 a2a3 workspace invite rollback

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_525_a2a3_workspace_invite_rollback`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#525 A2/A3（PR#540）：孤兒 workspace 的 slug 後果與 issue 寫的相反；注入點選錯被前提斷言擋下兩次"

2026-08-02 落地 #525 **A2**（`_create_workspace` 孤兒 workspace）＋**A3**（`_invite_member` 已入群卻回失敗）＝ **PR#540**（develop，兩個獨立 commit，manifest .92→.93，`Refs #525`）。修法沿用 A1／PR#527 的 `cr.savepoint()` 形態。全套：baseline `develop@cfe5cd7a` **0F/0E of 2212** → fix **0F/0E of 2223**（＋11）。

**🔴 勘誤：#525 A2 對 slug 後果的敘述是錯的。** issue 寫「`_generate_slug` 會靜默改成 name-1、缺陷不會浮現只會累積」。實測相反——`unique_slug` 是 DB 層 constraint 不看 ir.rule，但 `_generate_slug` 用的是**使用者身分**的 `search_count`，而 `workspace_rule_member_access` 的 domain 是 `access_ids.user_id` ⇒ 孤兒對使用者不可見 ⇒ 產生器回**同一個** slug → 撞 unique → `IntegrityError` → controller 吞掉但交易已毀 → request 尾端 `cr.commit()` 撞 **`InFailedSqlTransaction` → 框架層 500**。同名重試每次硬失敗，`-1` 後綴只在孤兒**看得見**時才走得到。⇒ 照 issue 敘述寫測試會斷言到錯誤的行為；**別人盤點出來的「後果」跟「行號」一樣要自己驗**（同 [[project_520_create_service_rollback_landing]] 的 5 處因果不符）。

**🔴 注入點選錯兩次，都是前提斷言擋下的**（[[feedback_absence_assertion_needs_its_premise_asserted]] 的實戰價值）：

1. A3 初版 patch `_validate_fields` **沒帶欄位過濾** ⇒ request 尾端 stored related 欄位（`user_name`/`user_email`）重算時**二次觸發**，例外跑到 controller 的 `try` **之外**、被框架回捲 ⇒ 殘留歸零、回應變成框架錯誤，根本沒測到 bug class。真正的 `@api.constrains('role','workspace_id')` 因欄位不相交不會被那次重算叫到 ⇒ 注入也要照做欄位過濾。**另注**：`_validate_fields` 迭代的 `self._constraint_methods` 存的是 registry setup 當下抓下的 function 物件 ⇒ patch `_check_owner_count` 這個類別屬性**完全不生效**（靜默沒注入）；要掛在 `_validate_fields` 本身。
2. A3 「回應組裝失敗」那條測試的注入是**假的**：`_invite_member` 的組裝只讀 `new_access.id`（純記憶體）與已 sudo 的 `target_user`，我讓 create 回傳 `browse(2**31-1)` 根本不會拋 ⇒ 端點照樣回 `success: True` 帶假 id。**造不出可達失敗就別硬造**——移除該測試、保留 narrowing 並在程式與測試檔註明「這一半沒有測試及其原因」。

Odoo 殘留視窗的**權威位置**（18.0）：`models.py` `_create` 尾端 `records._validate_fields(...)`（:5275）→ `records.check_access('create')`（:5276）→ `return`。ir.rule 的 create 檢查在 INSERT **之後**，ACL/`check_access` 的欄位權限在之前。

**A2 的變異驗證**：M1′＝把 owner-access 的 create 提到主表列 INSERT 之前（用 `create([])`，未被 patch 時是 no-op 才不會打壞成功路徑）＋**pre-fix controller** ⇒ 兩條殘留斷言**翻成假綠**、只有前提斷言紅（`Lists differ: [0] != [1]`）。M2＝`_list_workspaces` 不再從 `workspace_access` 反查 ⇒ 只有孤兒可見性那條紅。

相關：[[project_525_a1_skip_trial_rollback_landing]]、[[reference_odoo_test_docker_desktop_paas_image]]、[[reference_zsh_no_word_splitting_verification_script_noop]]
