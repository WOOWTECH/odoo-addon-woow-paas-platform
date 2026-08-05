# project docs audit 20260719

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_docs_audit_20260719`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："2026-07-19 docs/ 全面審查（4 agent 稽核）＋兩批執行 PR #378/#379；主結構 −25%、follow-ups 拆分 −38%、pattern-guard 26/28"

2026-07-19 docs/ 審查（304 檔/7.6MB，4 平行 agent：plans/tracking/grd/link）＋執行。**✅ PR #378（D1 減量）與 #379（D2 載入手術）皆已 merged（merge commits 6a8be6c3／be26cad8，develop，2026-07-19 當晚）**，工作分支已清除。

**審查結論**：plans 零真重複（design+impl 是慣例）；真問題＝(1) archive 政策從未執行（archived/plans/ 不存在）、(2) follow-ups.md 340KB 三冠破口（第一跳成本 82%＋入度 71 最高＋唯一斷鏈源）、(3) pattern-guard 只覆蓋 9/28 且漏掉入度最高的、(4) tracking 根層 22 檔導覽黑洞。

**D1（#378）**：26 plans→archived/plans/2026/（27 候選複驗剔 1：billing-issue-fix-design 被 sprint3 標 API contract SoT）；42 tracking 報告→archived/tracking/（58 檔 inbound 全改寫）；4 html 移出版控＋gitignore；api-spec archive；6 README 修正。主結構 273→204 檔（−25%）。
**D2（#379）**：follow-ups 340K→211K（State 歷史 102K＋14 已收官 F 段→follow-ups-archive.md，冷熱輪替慣例入 tracking/README）；pattern-guard 9→26/28（3 新 class：subscription-billing/std-model-override/test-discipline，hook 模擬 deny 實測）；subscription 藍圖歸位 plans；getting-started 指針化；wikilink 慣例修訂（解析域=全 docs）。

**Why**: 漸近載入三層（CLAUDE.md 常駐→hook 觸發→README 索引）架構本身是好的，破口在單檔肥大與 hook 覆蓋錯位。
**How to apply**: 未來歸檔必過 brace-aware inbound 掃描（`{design,implementation}` 花括號引用 naive grep 抓不到——本次實抓 5+1 例，政策已入 plans/README）；`--test-tags woow_paas_platform` 與 `/woow_paas_platform` 語意等價（Odoo 模組名自動成 tag）勿當 bug 修；[[project_superpowers_docs_promote_20260719]] 的 promote 政策與本次 archive 政策互補。git add 一律明確 pathspec（本次 `git add docs` 曾誤收 untracked 的 issue-368 plan，amend 補救）。
