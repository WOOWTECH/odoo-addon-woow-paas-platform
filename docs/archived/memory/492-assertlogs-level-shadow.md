# project 492 assertlogs level shadow（已封存）

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_492_assertlogs_level_shadow`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description："#492 develop 常紅收官——assertLogs 字串 'INFO' 在 Odoo 進程是 25；bisect 撲空＝回頭懷疑斷言本身"

**#492**（2026-07-31）：develop 全套唯一紅 `TestMcpAdminPassword.test_chart_version_drift_is_logged` → **PR#499 merged**（`6f0e7e0a` 已在 origin/develop）、**#492 closed**，leader2 merge 後抽查通過。未動 `src/` 業務碼 ⇒ **不 bump manifest**（與 fix-497 的 #497 PR 無版號衝突）；未碰 `test_cloud_service_deploy_timeout.py`（該檔歸 #497）。

**根因**：`odoo/netsvc.py:338` 的 `logging.addLevelName(logging.RUNBOT, "INFO")`（RUNBOT=25）把 stdlib 的 `logging._nameToLevel['INFO']` 由 20 覆寫成 25，而 `unittest/_log.py:37` 解析 level 查的正是那張表 ⇒ `assertLogs(..., level='INFO')` 實際 `setLevel(25)`，`_logger.info()`(20) 全被門檻擋掉。被測 log（`cloud_service.py:581`）完好、生產照印。已機械化為 repo-lint 第 11 條 `check_assertlogs_numeric_level`＋ pitfall doc `assertlogs-string-level-shadowed-by-odoo.md`，細節不必記在這裡。

**三條可帶走的教訓**（repo 沒記的方法論層）：

1. **bisect 撲空＝回頭懷疑斷言本身**。派工單給的兩個假設（log 丟了／log 搬家）都預設「被測 code 變了」。實測在引入 commit `59e17eb2` 上就紅、訊息一字不差 ⇒ 根本沒有「引入回歸的 commit」可找。候選 commit 全部排除後，下一個懷疑對象是測試自己，不是更早的 commit。
2. **訊息相同 ≠ 成因相同**。「log 被拿掉」與「門檻被設成 25」產生的失敗訊息完全一樣（`no logs of level INFO or higher triggered`），只能靠實驗分辨——我是在 code 裡印 `_logger.getEffectiveLevel()` 看到 **25** 才定案的。純讀 code／純推理會一路往被測 code 追。
3. **改測試讓它綠之前，先做鑑別力驗證**：把被測行為停掉（`if False and ...`），該測試必須立刻回紅。這是「修好斷言」與「拆掉守線」的唯一分界，見 [[feedback_test_asserting_absence_locks_shortcut]]。

**驗收數字**（同 commit `8acea757`、同容器族、乾淨 DB 兩步跑法）：修前 1F/0E of 2082（唯一紅即此條）→ 修後 **0F/0E of 2082**。環境沿用 [[reference_odoo_test_docker_desktop_paas_image]]（`AGENT_RUNTIME_NAMESPACE_SECRET` 必注入）。本機環境已於收工時清除並複驗（容器／network／兩個 worktree／本地分支皆無殘留；遠端分支 `fix/492-chart-drift-log` 保留）。
