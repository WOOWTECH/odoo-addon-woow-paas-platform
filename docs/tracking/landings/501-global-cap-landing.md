# project 501 global cap landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_501_global_cap_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#501 HAOS 全域上限收官（PR#517 merged, .96）：advisory lock 在 Odoo 的 REPEATABLE READ 下零貢獻、SerializationFailure 被 except Exception 吃掉、write 路徑 off-by-one；衍生 #515/#518/#519/#533"

2026-08-02 落地 woow_paas_platform#501（HAOS 全域實例上限＋停售開關脫離發版）。**PR#517 merged**（develop `d8bfcf32`、manifest `18.0.1.0.96`）。

## 交付與裁決

Eugene 拍板 A+B+C 三案；**C（開通前容量 pre-check）因需 operator 新增叢集容量端點、跨到另一條發版線而拆成 #515**。上限 8 但可由 ops param 調整、售罄後商品仍顯示並標示、waitlist 不程式化、營運介面做進 `res.config.settings`。

**B3（`write()` 供給閘門）最終決定移除**，繞過問題整個歸 #519。三個理由，第三個是實作階段才浮現且最根本：

1. **誤傷刪除失敗的善後**——`_delete_service` 先寫 `deleting`、uninstall 失敗後在 except 寫 `error`，於是停售期間任何刪除失敗都被擋、整筆 rollback、診斷訊息遺失。且直接違反本 PR 自己設定畫面寫的「只擋新下單、不影響已開通的服務」。
2. **擋不完**——`template_id` 原地改指、`state=False` 跳板都能繞（租戶對 `cloud_service` 有全 CRUD，見 #519）。
3. **write 路徑本來就有 off-by-one**——`_enforce_supply_gate` 的判定式是嚴格大於且預期「計數已含剛落庫的這筆」，**那個前提只在 `create()` 成立**；`write()` 的守衛在 `super().write()` 之前求值，該筆仍是 `deleting`、不被計數 ⇒ `cap > cap` 為假 ⇒ 放行。它就算沒有誤傷、就算擋得完，在該路徑上本來就不會運作。

⇒ 與「`_reject_reason_for_new_instance`（語意＝再收一筆**新的**）被用在既有服務轉移」是同一病灶：**複用了為 create 設計的裁決點，而它的計數時點假設在 write 路徑不成立**。

## 三個關鍵技術發現

1. **Odoo 的 cursor 是 REPEATABLE READ**（`sql_db.py` 的 `set_isolation_level`），不是守衛 docstring 假設的 READ COMMITTED ⇒「取 advisory lock 後重新計數」在同一交易內拿不到新 snapshot，**該防護零貢獻**。實測重現超賣（cap=1、兩並發、兩邊取鎖後計數都是 1、最終落 2 列；只把隔離等級改成 READ COMMITTED 就正確擋下）。那個錯誤前提自 #345 起寫在 docstring 被當成已驗證事實傳承——連派工時都照抄。**已開 #518**（`max_per_partner` 的同型缺陷待修）。
   ⇒ 正解不是 advisory lock，而是**讓並發第二者撞 write-write 衝突拿到 40001，交給 `retrying()` 以新 tx 新 snapshot 重試**。實測：只用 `UPDATE` 不要 advisory lock 也正確 ⇒ 鎖是冗餘的，已移除。
2. **`psycopg2.errors.SerializationFailure` 是 `Exception` 子類**（MRO：`→ OperationalError → DatabaseError → Error → Exception`）⇒ controller 的裸 `except Exception` 在 `retrying()` **內層**，會先吃掉 40001，重試永遠不會發生；且當下交易已 aborted，後續 flush 會撞 `InFailedSqlTransaction`。⇒ 修法必須包含 controller 重拋並發類例外。
3. **write 路徑 off-by-one**（見上）。

## 假綠與守線

- **刪掉一條零鑑別力的測試，又在同一個 commit 造了一條**：新測試的失敗訊息寫「write() 出現了上限守衛」，但把守衛加回去它照樣綠（同一個 off-by-one 死因）。救回它的是覆核要求「修完要確認仍有鑑別力」。⇒ **「知道某個假綠形態」不等於「不會再造出來」——造測試時看的是要守什麼，不是它會不會紅。**
- **鑑別力驗證的正確做法**：用 `git show` 取回守衛**原文**注入（不是自己重寫的等價物）、注入後先 `grep -c` marker 確認真的套上、`0F/0E → 0F/2E（正是那兩條）→ 移除後 0F/0E 無殘留`。
- 全套雙跑：baseline `0F/0E of 2237`、本分支 `0F/0E of 2268`，**差值 +31 正好等於新增測試方法數——差值對得上同時證明沒有既有測試被移除**（若有被移除，差值會小於 31）。

## 衍生 issue

| # | 內容 |
| --- | --- |
| #515 | 開通前容量 pre-check（需 operator 端點，跨發版線） |
| #516 | create() 守衛被吞掉 ⇒ **closed，與 #520 重複**（#520 從真實 stg 現象反推、且抓到我方漏掉的「連 admin 密碼都沒設定」） |
| #518 | `max_per_partner` 的並發防護零貢獻（per-partner 部分待修） |
| **#519** | 租戶可直接 ORM create 服務、無訂閱無扣款、cron 照樣部署——**優先級高於 #501 本身**；三條已證實繞過路徑已記錄 |
| #533 | `api_service_skip_trial` 吞掉並發類例外（#525 同構縫） |

「controller 吞掉例外」家族共四個切面：#516/#520（業務例外於 create）、#525（業務例外於 skip-trial）、#518（並發例外於 create）、#533（並發例外於 skip-trial）。

## 流程缺口

本 PR 在**第三輪覆核完成之前**就被 merge（補做後 14 項全過，結果是好的但那是運氣）。根因：**Gitea 只顯示「技術上可 merge」，不顯示「覆核是否通過」**。⇒ 需要覆核把關的 PR，**開 PR 當下就把勿 merge 標示寫進標題**，不要等接近可 merge 時才加——那個「接近」的判斷本身會落後於實際狀態。

⇒ 2026-08-02 leader5（PR#540）再撞一次（而且是**沒預見**的那一半），規則已升級為「**預設帶著、通過才移除**」並獨立成檔：[[feedback_pr_review_label_default_on]]。

相關：[[feedback_state_transition_reachability_needs_control_flow]]、[[feedback_parallel_leaders_same_file_collision]]（#516/#520 撞車）、[[reference_rebase_silently_eats_manifest_bump]]、[[feedback_comparison_experiment_freeze_recipe_not_just_baseline]]
