# project 464 reset button visibility e2e（已封存）

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_464_reset_button_visibility_e2e`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description："#464（#460 S3）Reset 按鈕可見性 headed E2E 收官：PR#470、四斷言＋紅燈對照 5/5、is_hidden 與 has_admin_gui 正交"

2026-07-29 完成 **#464（#460 S3）**：`tests/e2e/service-reset-button/` headed playwright E2E，證明 #460 S1（PR #465，develop `d368cb64`）的 Reset 密碼按鈕修復**對租戶可達**。→ **PR #470**（target develop，未 merge）。

**結果 5/5 PASS**：hermes-agent／mcp-k8s／mcp-odoo 按鈕可見且 enabled、n8n 不渲染，外加一條**紅燈對照**——`page.route` 攔詳情 payload、只刪掉 `template.has_admin_gui` 一個鍵，同一頁按鈕隨即消失（對照按鈕仍在）。沒有那條，四條全綠仍可能是「按鈕本來就無條件渲染」的假綠。

**環境**：本機 `docker-desktop` **ci-local** sandbox（local/hostPath 模式已死，recipe 見 [[reference_docker_desktop_ci_local_sandbox_image_import]]）。stg 刻意不用——stg image 尚未含 S1，在那裡跑會得到「看起來像失敗、其實是版本落後」的結果。按鈕是純前端渲染，fixture 只要 DB 有 `state=running` 且掛對 template 的 CloudService，**毋須 operator／真 Helm release**。

**最值得記住的產品面發現**：`template.is_hidden=True` 會讓**既有服務的詳情頁整個打不開**（`api_workspace_service` 直接回 `Service not found`），不只是 marketplace 不列。hermes-agent 的 seed 就是 `is_hidden=True`（#424 待定價），所以 fixture 必須先把它翻 False 才觀測得到按鈕。**`is_hidden`（定價／上架閘門）與 `has_admin_gui`（能力判準）正交**——意思是 hermes-agent 在定價解封前 Reset 按鈕對租戶仍不可達，但那是 `is_hidden` 擋的、不是 S1 沒修好。「不可訂購」與「既有實例不可見」是否該拆兩個開關，PR 描述已列為另案。

**交付物**：spec ＋ README ＋ `.claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh`（冪等 fixture seeder，輸出 manifest JSON 給 spec 讀，比照既有 `e2e-seed.sh` 形狀）＋ `docs/tracking/test-reports/464-reset-button-visibility-e2e-2026-07-29.md`。不動任何 `src/`。

**衍生 issue**（2026-07-29 開）：
- **#471** `is_hidden` 連既有服務的詳情／操作端點都擋。實查全 12 處 gate：既有服務的**讀取／操作**端點 8 處擋（詳情＝`paas.py:1189`、rollback、revisions、change-plan、skip-trial×2、check-addon-removal、mcp-servers ＋ `_list_services` 過濾），新建／上架路徑 4 處擋（那 4 處是 #336 明確要的）；**不一致點：`reset-admin-password` 沒有 gate** → API 重設得了、UI 入口卻打不開。
- **#472** `Dockerfile.dev` 已失效——`COPY extra/extra-addons/` 指向 `928613aa`（2026-04-26）刪掉的目錄，且 base `odoo:18.0` 缺 6 個第三方相依；`build.sh:85` 就是走這支。

**易錯事實（我在 #471 內文寫錯過、已發勘誤留言）**：`storefront_hidden` 是**訂閱 plan 層**欄位（擋購買選項），**不在** `cloud_app_template` 上。template 層的是 `is_hidden`。repo 已有「兩層縱深」設計，見 `src/tests/test_hidden_template_chain.py:33` 註解。

E2E 撰寫踩到的坑全數收進 [[reference_woow_playwright_e2e_gotchas]]（其中 #3「只差 hash 的 goto 不重載＋同路由不 remount」卡最久）。上游脈絡見 [[project_449_reset_carrier_landing]]。
