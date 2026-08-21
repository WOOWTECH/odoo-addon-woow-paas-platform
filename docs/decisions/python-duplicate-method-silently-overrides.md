# reference python duplicate method silently overrides

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_python_duplicate_method_silently_overrides`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：同一 class 內第二個同名 method 定義會靜默覆蓋前一個——在 Odoo model 上等於關掉既有守衛，針對性測試涵蓋不到、只有全套會紅

在既有 Odoo model 上加守衛時，**若該 model 已經 override 過同一個 method，新增第二個定義會靜默覆蓋它**——沒有錯誤、沒有警告、沒有 log。

2026-08-02 實例（#501）：`cloud_service` 已有 `write()` override 承載 #279 的租戶隔離守衛（擋 workspace 重指派）。為了補上限守衛而新增第二個 `write()`，Python 直接讓後者勝出 ⇒ #279 的防護整個消失。

**這個失效形態的關鍵性質：針對性測試涵蓋不到。** 新寫的上限測試全綠（它只測自己那條路徑），舊守衛的測試在**別的測試檔**裡（`TestReachSyncTriggers.test_cs_workspace_reassign_on_live_ns_rejected`，症狀是 `UserError not raised`）。所以：

- 只跑 `--test-tags` 針對性測試 ⇒ 全綠，看不出來
- 只有**全套**會紅
- 而全套若跑在髒基準上（見 [[reference_odoo_sandbox_ci_local_two_track_test]]），這條紅極可能被當成「本來就紅」放過

⇒ 三個條件疊起來，這個回歸有很高機率被合併進 develop。它之所以被抓到，是因為當輪堅持了「乾淨基準 0F/0E、任何一條紅都要逐條解釋、不准套本來就紅」。

**正解**：把新守衛**併進既有的 override**，不要新增第二個定義。併入時要收窄觸發條件——本例只在「**進入**計數態」（`deleting` → 非終態）時重跑閘門，否則計數態之間的正常生命週期轉移每次都會炸，比原缺陷更嚴重；並實查「有沒有合法路徑會踩到新條件」（本例全 repo 只有讀 `state == 'deleting'`、沒有任何地方把它寫回非終態）。

**機械化**：已裁決在 `scripts/audit/repo-lint.py` 加一條 gate，掃「同一 class body 內同名 method 定義超過一次」（限定同 class，跨 class 的繼承覆蓋是正常的）。加守衛前先 `grep "def <method_name>"` 確認該 class 是否已經 override 過，是最低成本的自保。

相關：[[feedback_test_pins_transient_fact_as_contract]]、[[project_test_suite_zero_red_20260728]]（0F/0E 之後任何一條紅都是真信號——本例是那條原則第一次真正兌現價值）
