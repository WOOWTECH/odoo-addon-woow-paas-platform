# Wave C：AC 席數三步選擇器（implementation plan）

> 配對 design：`2026-07-14-wave-c-seat-picker-design.md`（拍板與架構見該檔，此檔只列執行分解）。
> 歸檔位置：`docs/plans/2026-Q3/`（雙檔隨 PR-C1 落地）。

## PR 序列

三 PR 串行（C1 是 C2/C3 的資料前提；C2/C3 理論可並行，但共改 PlanSelector 周邊，串行免 rebase 地獄）。

### PR-C1：後端 variants 回帶（純後端，暫定 .64）

- [ ] T1 helper `_attach_variants`：批次 ir.model.data 反查（單次 in-query）→ parse `_ac{n}_{cycle}` → 重組 base xmlid → group by base、seats 排序。運行時不匹配＝log error＋跳過。
- [ ] T2 三端點擴充：`paas.py` `api_template_plans`／`security_access.py` `api_sa_template_plans`／`smart_home.py` `api_smarthome_template_plans` 的 base dict 加 `variants`（無變體＝`[]`）。
- [ ] T3 測試：
  - variants 正確性：odoo t1 monthly base → 3 筆（seats 1,2,3）、SA r10 yearly → 10 筆…（抽 tier 代表＋總和斷言）
  - 全庫 xmlid 反解斷言（每筆 hidden 變體可解出存在的 base）——fail-loud 測試層
  - `test_variants_never_reach_storefront` 不動、保持綠
- [ ] T4 雙 docs 落 `docs/plans/2026-Q3/`（從 leader3 scratchpad 收入，內容原樣）
- [ ] T5 版號 bump（開 PR 前 re-check 版號隊列與 open PRs）＋sandbox 跑受影響測試附輸出原文
- [ ] PR 描述 zh-tw、Refs #330（變體母案）＋引用兩 docs；**不 merge、回報 leader**

### PR-C2：前端新購三步（.65）

- [ ] T1 `PlanSelector` 擴充：variants 展開第二步（segmented/stepper；選項=seats 集合＋0=不加購）；`onSelect` emit 最終 plan_id；價格同步切換；無 variants 行為 bit-for-bit 不變
- [ ] T2 三 surface 透傳 variants（`AppConfigurationPage`／`CreateSecurityAccessModal`／`CreateSmartHomeModal`）——payload 不動
- [ ] T3 SCSS：`.o_woow_plan_selector__seats`（BEM、循 `.o_woow_` 前綴）
- [ ] T4 headed E2E（playwright-cli）：CS 選 odoo t2＋3 席下單→訂閱 plan=變體；SA/SH 各一條；mcp 單席；Hermes/postgresql fallback 無感
- [ ] 版號＋PR（Refs #330）；不 merge

### PR-C3：改席數／改方案 UI（.66）

- [ ] T0 查證點 V1（SH changePlan client 存在性）／V2（退款預覽 API）——結果進 PR 描述
- [ ] T1 `ChangePlanModal` 新元件（內嵌 PlanSelector；顯示目前 plan＋席數→新選擇→退點告知文案〔V2 有 API 就上數字〕）
- [ ] T2 三 surface service detail 加「變更方案」入口＋接 `changePlan(planId)`（SH client 缺就補，循 CS/SA 形狀）
- [ ] T3 headed E2E：CS 升席（base→+3）、SA 降席（+6→+1）、SH 換 tier；斷言舊訂閱 close＋新訂閱 plan 正確（#342 語意：不觸發 redeploy，僅計費）
- [ ] 版號＋PR（Refs #330、#342 語意引用）；不 merge

## 驗收 gate（每 PR）

1. sandbox 測試輸出原文（PASS/FAIL 數字）＋E2E 截圖（C2/C3）
2. leader 驗實體：remote 分支＋commit＋PR 序號＋diff 範圍
3. 版號隊列 re-check（撞號順延）
4. worktree 開發、base=最新 origin/develop

## 派工建議

- PR-C1：Sonar（盤點 context 完整、後端小件）
- PR-C2/C3：視 C1 完成後 recruit（前端件、給 design doc＋盤點報告路徑）
