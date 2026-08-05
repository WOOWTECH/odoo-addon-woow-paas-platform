# project 327 refund window landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_327_refund_window_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#327 F-BILLING-1 退點 prorata 視窗改以 sh_date_of_next_payment 反推（取代凍結 sh_end_date）：cycle-2+ 長壽已付訂閱中途取消不再恆退 0；PR #334 待 review"

**2026-07-10 landing → PR #334**（target develop、Closes #327、**No Conflicts** 待 review；branch `fix/327-refund-window` worktree 保留）。承 [[project_324_subscription_dup_charge_incident]] 對抗覆核揪出的 F-BILLING-1。

**根因**：`_prorata_unused_refund`（`src/models/sh_subscription_fix.py`）舊公式用建立時一次寫定、之後永不推進的 `[sh_start_date, sh_end_date]` 當當期視窗；但遞迴計費只推進 `sh_date_of_next_payment`（`_on_payment_success` / 上游 `_sh_create_auto_invoice` / `_skip_trial`），`sh_end_date` 生產碼唯二寫入點在 `subscription_lifecycle_mixin.py:214/:245`（建立時 `start+cycle−1day`）→ 凍結。故 cycle-2+（`today>sh_end_date`）已付整期中途取消/刪服務/換 plan → `max(0,end−today)` clamp 恆退 0（少退、非多退）。

**設計題 b 坐實（關鍵交互）**：上游 `_sh_auto_expired_subscription`（`sh_subscription_subscription.py:409`，daily cron `sh_ir_cron_subscription_auto_expired`）用**脆弱 `==` 等值比對**（`sh_end_date==today→expire`）；本 repo **不 override 此方法、不停用其 cron**。長壽 in_progress 成因＝錯過 end_date 當天即永久 miss（隔天 `today>end_date`，`==` 恆 False）→ 訂閱靠 next_payment 續期長存。characterization test 坐實此 `==` miss。

**修法（選 a-1 而非 a-2）**：視窗改以 `[next_payment − cycle_delta, next_payment)` 反推（新增 `_cycle_delta()` helper，`sh_unit+sh_recurrency`；recurrency=0/unit 非法回 None）；`remaining=max(0,min((next_payment−today).days, cycle_days))` → 退點 ∈ [0, 一週期價]。**刻意不碰 sh_end_date** → 上游 auto-expire 交互零影響（對「cron 在特定環境是否 active」完全免疫）＝選 next_payment 推導、否決「續期推進 sh_end_date」的決定性理由。trial→0（`today<sh_start_date` guard 保留）、never-paid→0（#324 gate 不動）語意不變。SSOT 一改，7 退點呼叫者（CS/SH/SA 刪除 `_settle_on_entity_delete` + 4 換 plan `_compute_unused_refund`）自動受惠。

**流程**：plan（`docs/plans/2026-Q3/2026-07-10-327-refund-window-next-payment.md`）→ plan-review miniclaude **R1 APPROVED**（逐一 primary-source 驗證）→ 4-task TDD（本地 docker-desktop paas-odoo image，見 [[reference_odoo_test_docker_desktop_paas_image]]）：Task1 auto-expire 護欄、Task2 核心 red→green（3 核心測翻紅 0≠50/0≠100→綠）、Task3 docstring+呼叫者回歸、Task4 manifest+follow-ups。

**教訓：plan 探索漏 2 個 proration 測試**（`test_cloud_service.py` `TestCloudServiceChangePlanTrialMode` + `test_smart_home_subscription_flow.py` `TestSmartHomeChangePlanTrialMode` 的 `test_active_to_active_regression_proration`）——full suite 抓到（`300≠150`）。根因＝fixture backdate start/end 卻漏 backdate next_payment（自相矛盾：start/end 說中點、next_payment 說剛續期）；舊公式只看 start/end 碰巧得 150，新公式看 billing 真相得全額。修 fixture 補 next_payment 對齊 + 明確 day/30 cycle（斷言穩定）＝正當 fixture 修正非掩蓋 bug。

**版號撞車（#330 定價 seed）**：[[project_pricing_gap_analysis_20260709]] 的 PR #333 並行 landing、同 bump .52 含 `migrations/18.0.1.0.52`。**#333 先 merge**（develop=352bb34）→ leader 定案：rebase 到最新、版號改 **.53**、full suite 於 #333 base 重跑確認無交互回歸。**於 t53 乾淨 install .53（含 #333 seed/schema/新 model service_type/ac）= 0 failed, 2 error of 1555**（2 error=唯一 `TestNamespaceLeftSetUnion` ns flake，缺 `AGENT_RUNTIME_NAMESPACE_SECRET`，非回歸）。版號 .53 無 migration/ACL/seed。

**上游 addon 回歸驗證（Eugene 追問「full suite 只跑 woow？上游 addon 呢？」）**：先前 full suite 是 `--test-tags /woow_paas_platform`（僅本 addon 1555 tests），未含上游。因 `sh_subscription_fix.py` 用 `_inherit` 全域 patch `sh.subscription.subscription`，上游測試若跑到該模型計費/退點路徑會跑到我的版本＝唯一交互面（風險僅限 sh_subscription + sh_subscription_wallet；core account/sale 不知訂閱退點、無風險面）。**實測**：`-u sh_subscription,sh_subscription_wallet --test-tags '/sh_subscription,/sh_subscription_wallet'` → 104 tests **4 failed**（全在 sh_subscription_wallet：`test_subscription_deduct` 3 個扣款餘額不符 + `test_wc014_bind_card_submit` ECPay 綁卡 HTTP 500）。**develop（352bb34）baseline 對照（detach worktree + 獨立容器 odoo-base/tbase）= 逐一相同的 4 failed**＝**#327 新增上游失敗數 0**；那 4 個是 #324 wallet cron neutralize 交互 + ECPay 環境，develop 既有。歸因技巧＝上游碼 baked in image（兩邊同），只換 woow worktree（fix/327 vs develop）即得乾淨 baseline。

**leader 獨立驗收（不盡信 agent、[[feedback_teammate_deliverable_verify_objects]] 鐵律）**：PR #334 逐項實查——base=352bb34（merge-base==develop、已含 #333）、版號 .53、diff 範圍乾淨、修法/測試/expire 交互釘死；**leader 自跑隔離 full suite（獨立容器、含 #333 seed/migration install）＝1555 tests 0 failed、2 error（唯 `TestNamespaceLeftSetUnion` flake）**，與 Prorata 自報吻合。Prorata **自主**處理版號撞車（自 fetch 見 #333 已 merge→自 rebase+bump .53），leader 的 .53 指令因此冗餘（覆蓋待命）。

**2026-07-10 收官**：PR #334 **✅ merged**（Eugene 按未落地→leader 代執行；develop=691d9f8、版號 .53、issue #327 隨 Closes 自動關）；Prorata 已解散、worktree fix-327-refund-window 清除。**部署債（✅ 已清償）**：~~prod/stg 現 .51，develop 累積 #333/.52 + #334/.53 兩波未上線~~ → **2026-07-11 prod 已上 .53 收官**（`docs/guides/deployment/prod-release-sop.md`（該筆逐次紀錄已於 2026-08-04 收斂進 SOP §5.1／§7／§9 後刪除）：一次帶 .52+.53 跨兩 migration、7 phase 全過；退點 cycle-2+ 已付訂閱正確退比例已在 prod 生效、對帳面「開始正確退款」）。prod 追平 develop。
