# project 461 reset async landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_461_reset_async_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#461 reset 改非同步＋預算階梯 → PR#483 已 merged（be090c92）；helm --atomic 失敗吃 2n 的既有缺陷、Recreate 才量得到 2n、stg 驗收硬條件必 headed"

**#461（F-449-3）reset 密碼逾時**：2026-07-30 交付 **PR#483 已 merge**（eugene 親按；三訊號驗過=merged:True＋#461 closed＋`ddb0bc95`∈origin/develop@be090c92）。期間 #481 先進 develop 曾致 README 計數行衝突，fix-461 解掉＋rebase 全量重驗（range-diff 驗僅 context 位移）。**剩 rollout 待辦**：operator 先發、平台後發；stg 驗收必 headed 走 UI。插曲：merge 前有一句來源不明的「merge 好了請清理」出現在 leader 終端畫面——fix-461 以 object 實查（tea pr＋is-ancestor 兩訊號）識破未照做，救回 worktree。真 merge 後才清：worktree `wt-461-reset-async` 與本機分支已移除（remote 分支 Gitea 未自動刪，依裁決留著不動）。

三件事：rollout 等待移出 request（提交點左移＝**C11′ commit-on-secret**：operator 回 2xx＝密碼已是權威值且不再自動回退）＋預算階梯（前端 90s > 平台 `RESET_CONFIG_TIMEOUT` 75s > operator `RESET_HARD_CAP_SECONDS` 65s，repo-lint 第 9 條 `check_reset_budget_ladder` 跨四檔常數折疊驗算）＋順修既有缺陷。設計與論證已在 repo：`docs/plans/2026-Q3/2026-07-30-461-reset-async-design.md`、pattern `docs/decisions/patterns/reset-async-budget-ladder.md`（第 31 條）。

**本輪不在 repo、值得記住的三點**：

1. **`--atomic` 的 2n 只有 `Recreate` 才量得到**。合成 chart 用 RollingUpdate 時舊 pod 會撐到新 pod Ready ⇒ helm 的 rollback wait 瞬間通過、只量到 **1n（15.4s）**，斷言等於沒有牙；改成 Recreate（對齊真 chart，#459 §0 四顆皆 Recreate）才量到 **30.4s ≈ 2n**。要驗「失敗路徑吃兩次 wait」的 fixture，strategy 是**實質參數**不是細節。
2. **既有缺陷比 #461 本身更嚴重**（已修 reset 路徑，非 reset 路徑留 **F-461-3**）：改動前 `helm.upgrade()` 不傳 `--timeout`（helm 預設 5m）而 subprocess 上限 `settings.helm_timeout=300s` ⇒ `2×300 > 300`，atomic 失敗路徑本來就在 rollback 開始前被 SIGKILL ⇒ release 卡 `pending-rollback`、Secret 停在沒人持有的新密碼。紅燈對照（subprocess 改回 `n+10`）真機重現。
3. **stg 驗收硬條件＝必 headed 走 UI**（leader3 裁決，已寫進 PR 描述 §stg 驗收）：#459 就是繞過 UI 直打 API 才漏掉「前端 30s」那層，同型盲點不可重演。headed E2E 本輪獲准不補跑（modal 只加純文字），代價轉嫁到 stg。

**衍生**：F-461-1（`upgrading` 停滯無上限）、F-461-2（chart 版號漂移時重按不收斂，只加了 log）、F-461-3（同上第 2 點）。發版順序＝**operator 先、平台後**（[[project_449_reset_carrier_landing]] 的 F-449-2 同型硬約束）。

驗證手法沿用：[[reference_odoo_test_docker_desktop_paas_image]]（本輪用既有 `woow-e2e-464:local` 映像 bind-mount worktree src，另起 baseline 容器掛 `origin/develop` 乾淨 src 對照取差集——測試數差 3 精確歸因到 PR#476 的 6 支 − 新增 3 支）、[[reference_operator_pytest_venv_py311]]。
