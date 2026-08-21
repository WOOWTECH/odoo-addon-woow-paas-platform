# project subscription tax bug and selector redesign 20260718

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_subscription_tax_bug_and_selector_redesign_20260718`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：訂閱方案選擇器重設計討論挖出 prod 稅務 bug＋trial 重設 bug；開

2026-07-18：從「重設計訂閱方案選擇器（使用者看不出要花多少 pts）」的討論，挖出並在 stg+prod 實查確認**兩個計費 bug**。

**Bug 1 稅務（#366）**：seed 建訂閱 product 未明寫 `taxes_id` → 繼承 `company.account_sale_tax_id`。prod/stg 的 Odoo company **從未正式設定**（name=`My Company`、country=US、預設銷項稅＝殘留 **15%**，非台灣 5%）。實測：**prod 237/251 plans 掛 15% 錯稅**（stg 241/251）、**prod 前台 22/36 卡片 card≠charged**（n8n 1席卡片1350→實扣1552.5、使用者以為+600 實際+802.5；Odoo 8-11人年繳 60000→69000）。**prod 0 張已過帳發票＝無計費事故、零風險修改窗口**，但須趕在真實收費前。修法三層：① migration 強清 251 product 的 taxes_id（**空集合非 0% 稅**，noupdate=1 擋 -u 必走 migration、且 migration 目錄須 >目前版號否則靜默 no-op）② seed 明寫空稅防復發 ③ 公司預設稅**不動**（波及整個 company/官網 ERP）靠②隔離。

**Bug 2 trial 重設（#367）**：**251/251 plan 全 sh_trial=True** + `_create_and_confirm_subscription` 對 trial plan 強制 `_force_trial_dates_from_plan`（`subscription_lifecycle_mixin.py:164-171`）、蓋掉第三方 `sub_exists`「已訂閱過不再給試用」保護 → 老客戶換方案重啟 7 天免費、近期扣 0（Issue #9 同類 bug 換時間維度）。

**Eugene 決策**：pts 不課稅；trial 當 bug 修（已訂閱換方案不重啟試用）；卡片顯示 vCPU/RAM（變更模式加「資源於下個週期生效」註記、待 #342 落地拿掉）。

**三 issue**（git-prod `odoo-addons/woow_paas_platform`，tea CLI）：**#366** 稅務、**#367** trial、**#368** 選擇器重設計（Depends #366/#367、Refs #342）。

**選擇器 rubric（workflow：4設計×3評審×7面向）勝案＝toggle 兩段式（24.3/35）**：週期改頂部常駐 toggle（是 view-state 非 flow-state，保住 PlanSelector 單一 selectedPlanId 無內部 state 契約）；**P2 隨 #366 清稅自動消失**；只做 P1（8卡收斂）+P3（席數增量 +600）+規格顯示+決定性預設（現況 plans[0].id 是隨機預設 bug）；擴充性（笛卡兒積 AC 一軸 43→251）劃 scope 外另開架構單。原提案裁決：三階段保留後兩段、週期改 toggle；`{最低價}+` 降級卡片級且只在有變體時渲染；**「改主方案會 redeploy」是 leader 誤植的事實錯誤**（`api_service_change_plan` paas.py:952-1050 零 helm 呼叫，與 #342 定案一致）。P2 正解（若未來真要收稅）＝抽 `_taxes_for_plan`/`_plan_total_with_tax` 與建訂閱同源、非兩套公式。

**執行**：recruit worker **Ledger**（`claude --model opus --effort ultracode --permission-mode auto`，worktree `fix/366-subscription-tax-clear`、TDD）做 WI-1；踩到 repo 已知 worktree stale base 坑（EnterWorktree 吃 origin/HEAD=main `e68d2f2b`、非實際 develop `7c5ac767`）已導正，見 [[feedback_agent_worktree_stale_base_verify_before_pr]]。

**全案三單收官 ✅（2026-07-19）**：#366 PR#369、#367 PR#370（per-partner→per-entity 返工）、**#368 三棒 PR#371(.70 後端 group_key/specs)/PR#372(.71 前端 toggle 兩段式+PlanTotalBar+全域 modal CSS 夾帶)/PR#374(.72 ChangePlanModal 定位+churn 防護+760px+下週期提示)** 全 merged，develop=cc14f4a5=.72。#368 執行模式=兩階段（計畫文件 `docs/superpowers/plans/2026-07-19-issue-368-plan-selector-redesign.md` 經 6 路對抗驗證+leader 全文審批）→三 PR 逐棒驗收；核心設計=零 schema（group_key=xmlid strip `_(monthly|yearly)$`、43 base 21 組零撞名、全程無 migration 繞開 noupdate 陷阱）+頁面層 sticky footer（.o_woow_content 是真 scrollport）+PlanTotalBar 防 4 處 markup 漂移。Eugene 在 Ledger 終端 /rc 快速通道直接 merge PR-1/PR-3（leader 驗 PR-2/實體覆核全部）；**幽靈緩衝殘句模式**=Ledger 輸入框多次出現替決策者說話的未送出文字（含與 Eugene 後續決策相反的），每次派工必消毒、獨立驗證。E2E 用 dev-sandbox-pr368+pod 內第二 server --dev=assets recipe，收尾 sandbox/worktree/branch 全清（實體驗證）。

**#366 已收官 ✅（2026-07-18）**：PR #369 merged（develop=`9461860c`、.69）、#366 自動關單。leader 親驗實體：remote 分支 `1fb2a801`、diff 三層齊（migration 18.0.1.0.69 post-migrate + seed 43+208 明寫 `[(6,0,[])]` + generator 同步 + 測試 131 行）、**沙盒兩 DB 親跑稽核 PASS**（升級態 `woow_fix366` .68→.69 與 fresh 態 `woow_fresh69` 皆 251/251 掛稅 0、card≠charged 落差 0、company 15% 未動＝Layer C 如設計）。沙盒 `dev-sandbox-fix366` 留 48h TTL。殘留回歸 3f+2e/1724＝已知 flake（proration 日期 + agent-runtime env）非回歸。新環境事實：**Docker Desktop k8s 已改 kind-based node（desktop-control-plane），/Users 不掛進 node → k8s-dev-sandbox local 模式（hostPath 掛 worktree）架構上不可行**，Ledger 改走 woow-k3s overlay pod + fresh DB 驗證。**Ledger 已 /clear 接 #367**（worktree `fix/367-trial-reset-guard`、base 須 9461860c、修 `_force_trial_dates_from_plan` 尊重 sub_exists、change-plan 先關舊建新勿誤判新客戶、seed sh_trial=True 不動、無 data 變更不 bump）。

**稽核 recipe**：odoo shell 唯讀查稅——stg pod `dev-sandbox-stg/stg-odoo-dev-sandbox-odoo-*`（conf=`/etc/odoo/odoo.conf`、db=`woow_stg`）、prod pod `paas-system/paas-odoo-*`（conf=`/opt/bitnami/odoo/conf/odoo.conf`、db=`bitnami_odoo`）。稅路徑：`product.template.search([('sh_product_subscribe','=',True),('sh_subscription_plan_id','=',plan.id)]).product_variant_id.taxes_id`。**prod `account.move`（發票）讀取被 auto-mode classifier 擋** → 改 `!` 前綴由 Eugene 親跑（見 [[reference_kubectl_woow_k3s]]、[[reference_maestri_recruit_resume_pitfall]]）。
