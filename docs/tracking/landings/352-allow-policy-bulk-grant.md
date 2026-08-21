# project 352 allow policy bulk grant

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_352_allow_policy_bulk_grant`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："2026-08-02 #352 PR#526——allow 政策的 include 禁用 email_domain/everyone；判準收窄成 allow×include 讓爆炸半徑從 67 降到可控"

2026-08-02 leader2 派工，PR **#526**（`fix/352-allow-policy-seat-bypass`，Closes #352，**開完停在 Eugene 核准**）。全套 **0F/0E of 2192**；新測試 16 條修前 10F。

## 缺陷形狀

CF Access 席位是 **per-ACCOUNT 全平台共用 50 席**、**使用者認證登入時**才消耗。AC 按人數計費而人數＝數 `email_list` 位址 ⇒ `email_domain` / `everyone` 計 0 人卻放進不特定多數人。**受害者是其他租戶**（第 51 人登入被擋）且他們零 workaround——這是判斷「該不該 fail-loud」的關鍵，不是單純少收錢。

## 判準收窄是這題的技術核心

擋 **allow × include** 而非「allow 全部」：include 是 OR（命中即放行）＝繞過入口；require（AND）／exclude（NOT）只縮小範圍，「allow 但排除 evil.com」是正當寫法。bypass 豁免（不驗證身分＝不消耗席位，且無差別禁用 everyone 會與 `_check_decision_selector_compat` 打架讓 bypass 只剩兩種寫法）；deny 不受限。

收窄不只是語意正確，也直接決定**爆炸半徑**：第一版全套 4F/63E（幾乎全是既有 fixture 隨手挑 email_domain），收窄＋逐處判定後降到 0。

## 兩個「改 constraint 必做」的動作

1. **rule 子模型的 create/write 要顯式回呼**：policy 上的 `@api.constrains` **不會**因為「單獨建/改一條 rule」而觸發（既有的 `_check_emails_per_policy` / `_check_decision_selector_compat` 就是這樣接的）。漏掉＝只擋得到走 `policy.rule_ids` 的寫法，「先建合法 policy 再單獨塞一條 include」整條繞過。
2. **fixture 判定不要盲目字串取代**：逐處往上回溯最近的 `group` / `decision` 標記，先印 REPLACE/SKIP 清單覆核再套用。三處必須手動——尤其 `test_policy_rule_value_validation` 的 `@acme.com`：機械替換會把它變成 email_list，而 `'@acme.com'` **含 @ 所以 email_list 驗證會通過**，被測物從「格式驗證」偷換成新 constraint ⇒ 假綠。改用 deny 政策保留 email_domain 才對。

## 交付邊界（都寫進 PR，等 Eugene 裁決）

- **擋繞過 ≠ 席數計費成立**：全 repo grep `ac_max_access_seats` 只在 seed 測試與 plan 變體判別出現，授權/計費路徑一處都沒有（#521）。
- **第三、四條同型繞過未做**：allow+include 的 `ip_range`（rule 驗證只查 CIDR 合法、**無前綴長度下限**，`0.0.0.0/0` 過關）與 `country`（只驗 ISO-2、不限數量）。與已擋兩條**只有繞過方便程度的差別**。建議交給 #521 的席數 enforcement，因為 IP/國家的寬窄是連續光譜，用 constraint 切一刀必然武斷。
- stg SA 6 底下兩筆存量違規（prod 0 筆）建議清除但**未自行動手**；`@api.constrains` 不回溯，所以不會立刻爆，但**任何觸碰該 SA 的寫入都會 raise**。

## 收官（2026-08-02）

PR#526 **merged**（develop `13c6a752`、#352 auto-closed、`.91`、merge 後全套 0F/0E of 2192）。stg 存量依裁決刪掉 policy 6＋9（rule 3→1、policy 3→1），policy 8／SA 6 記錄／SA 5 均未動。

CF 一度沒被同步（gate 靜默略過，見 [[reference_sa_suspended_access_sync_gate_orphans_cf_policy]]）；Eugene 裁決後以 `sa_force_access_sync=True` ＋ 手動補回 `sa_unlinked_cf_policy_ids` 收斂完成：operator 回 `orphaned_policy_ids: []`，且用 `get_reconcile_inventory()` 做**獨立第二信源**確認兩條 doomed policy 已不在帳號級 inventory 內。SA 未被喚醒（state/tunnel/service_url 未變、curl 仍 530）。無新孤兒。成因追蹤留在 #528／#385。

## 踩到

[[reference_rebase_silently_eats_manifest_bump]]——rebase 後 manifest bump 變 no-op。
