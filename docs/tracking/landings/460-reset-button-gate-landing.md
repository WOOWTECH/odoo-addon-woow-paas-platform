# project 460 reset button gate landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_460_reset_button_gate_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#460 Reset 按鈕 slug 白名單→has_admin_gui 三票收官：PR#465/#468/#469 merged、#470 E2E、衍生 #471/#472；spec→tickets→三 teammate 流水線"

2026-07-29 #460（F-449-4）三票流水線收官。spec 發於 #460 comment（to-spec skill），拆 #462/#463/#464 三張 tracer-bullet 票（`ready-for-agent` label 首用）。

- **S1 #462／PR #465（merged）**：service payload template 子物件補 `has_admin_gui`（唯一入口、直接鏡射）；前端 getter isMcpAdminService→`hasAdminGui`（`=== true` fail-closed）；合約測試 4 支。迴歸 136 測試 0F/0E（woow-k3s throwaway sandbox＋stg image）。
- **S2 #463／PR #468＋#469（merged）**：repo-lint 第 7 gate `capability-gate`——形態（slug 陣列/常數繞法/XML 直寫）＋anchor（按鈕 t-if 鏈必須追到 has_admin_gui，抓得到 category 型第三判準）雙保險；isOdooService 對照組不誤傷；pattern doc `capability-gate-single-source-field.md`＋pattern-guard-map 新 class。**#469＝CI paths 盲點**：repo-lint workflow paths 原只有 src/**，改 gate 本身不觸發 gate，補 scripts/audit/**。
- **S3 #464／PR #470**：headed E2E 五斷言 PASS（四代表模板＋第 5 條 fail-closed 紅燈對照＝攔 payload 抽欄位按鈕即消失，防「無條件渲染」假綠）；ci-local sandbox（docker-desktop local hostPath 模式已死＝kind node 無 /Users，同舊 memory）；產出可重複 spec＋fixture＋報告。
- **衍生**：#471（is_hidden 連既有服務詳情/操作端點都回 Service not found——一旗標載兩意圖：HA PaaS 刻意 vs hermes-agent 暫下架副作用；stg 升版翻回隱藏期間 svc60 詳情頁打不開）、#472（Dockerfile.dev 自 928613aa 重構後失效）。
- 教訓：two-dot diff 對 stale-develop 誤報「反向刪除」——驗 worktree base 要看 **merge-base＋three-dot**（本例 merge-base 正確、虛驚）。maestri ask 背景通道會被殺，teammate 不受影響，完工偵測用 Monitor 掃終端 spinner。
