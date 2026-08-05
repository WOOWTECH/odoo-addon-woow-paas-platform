# project 318 subscription unlink landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_318_subscription_unlink_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。

2026-07-09 完成 issue #318（billing：CS 刪除/換 plan 未聯動訂閱）實作，PR **#320**（target develop、Closes #318+**#322**、8 commits）ready，狀態＝**測試綠、待 leader review/merge + 手動關 issue**（develop 非預設分支，Closes 不自動關）。branch `feat/318-subscription-unlink`（worktree `.claude/worktrees/`）。

**Review 後 Eugene 追加擴充（同一 PR）**：**#322**（本 PR 一併修）＝刪除聯動補到 **SmartHome / SecurityAccess**（三者共用同 mixin 與同缺口；各自 unlink() 加捕獲+結算，比照 CS；Task 6 TDD 綠）——SH/SA 換 plan 退點早在 Task 4 已修。**#323**（backlog，**不在本 PR**，產品決策）＝使用者從 billing 主動取消訂閱（`api_subscription_cancel` 寫 'close'）**不退點、不拆服務**，與刪除退點不對稱。point「cancel vs close」定案＝**保留 delete='cancel'**（對齊 #318 文字；對已刪服務 cancel/close 只影響報表）。plan-review miniclaude **3 輪** APPROVED（R3 覆核 SH/SA 擴充）。full suite 1504 tests、0 failure（唯 2 個 `TestNamespaceLeftSetUnion` error＝既有 test-isolation flake：agent 測試 `addCleanup(os.environ.pop, AGENT_RUNTIME_NAMESPACE_SECRET)` 無條件刪 env，與本 PR 無關）。

**核心發現（改變任務理解）**：換 plan 其實**早已實作**（`api_service_change_plan` paas.py:942 已有 refund→close old→create new、且靠 Odoo request 單一交易原子），#318 對換 plan 的真缺口只是「退點記成 `topup` 而非 `refund`」。真正沒做的是**刪除聯動**：`_delete_service`→`unlink()` 完全不碰訂閱 → 孤兒 in_progress 訂閱被計費 cron（`_sh_create_auto_invoice` domain `state=in_progress`）續扣。

**5 task 落地**：① `res.partner.refund_wallet_points()`（新，`refund`-typed 退點、帶 subscription_id；上游 `add_wallet_points` 硬編 topup 無型別參數，`refund` enum 上游 wallet_transaction.py:27 早存在但無寫入者）② `sh.subscription.subscription._settle_on_entity_delete()`（取消+按比例退點、冪等只結算 in_progress、partner-less 守衛）③ `CloudService.unlink()` super 前捕獲/super 後結算（catch-all 覆蓋所有刪除路徑；helm fail-loud raise 時全 rollback）④ 換 plan 四入口（CS controller/CS+SH wizard/SH/SA controller）退點正名 refund ⑤ 版號 .48→.49。

**關鍵設計決策**：刪除終態用 `'cancel'`（對齊 issue 文字＋服務終止；換 plan 關舊約用 `'close'` 區分）；退點一律 SSOT `_compute_unused_refund()`（trial→0、自然月天數），不另立公式。**範圍限 CS**；SmartHome/SecurityAccess 刪除聯動＝**follow-up**（共用同 mixin，`_settle_on_entity_delete` 已設計成三者可共用，各自 unlink() 加 2 行即可）。

**驗證**：TDD 紅→綠逐 task（native amd64，見 [[reference_odoo_test_docker_desktop_paas_image]]）；全模組 **1502 tests、我的變更 0 failure**（唯 2 個 `TestNamespaceLeftSetUnion` error＝缺 `AGENT_RUNTIME_NAMESPACE_SECRET` env 的環境問題，設上即 0，非本 PR）。plan-review＝miniclaude 2 輪 APPROVED（3 個 blocking「unverified 上游」用 file:line 坐實＋NULL-partner 守衛＋wizard 測試）。plan：`docs/plans/2026-Q3/2026-07-09-318-cs-delete-changeplan-subscription-linkage.md`。

**2026-07-09 stg 合併 teardown E2E（#317×#318）PASS（leader2 session）**：throwaway n8n（ws1 test1、release svc-8e631caf、sub 31 trial→改日期模擬 mid-cycle 已付）→ `CloudService.unlink()` 一次觸發整條交集鏈：**helm uninstall(respect keep) 成功→super().unlink() 刪 CS record→`_settle_on_entity_delete`**。實證：sub 31 **state→cancel**、`sh_reason='Service deleted'`；partner 錢包 **+132.67**（=199×20/30 pro-rata）；建 **refund-typed wallet.transaction id=1243**（desc「Service deleted refund: n8n Monthly」、綁 sub 31）。#317 leg 同時證：release GONE 但 **PVC svc-8e631caf-n8n SURVIVED(Bound,keep)**。unlink() 內序（uninstall fail-loud→super().unlink()→settle）確保兩腿不互斷。詳 [[project_317_pvc_keep_landing]]。

**⚠️ E2E「PASS」範圍勘誤（advisor 對抗覆核，2026-07-09）**：此 PASS 只對 **happy-path（已付訂閱刪除→按剩餘比例退點）** 成立。因我把 sub 31（trial、從未付款）的 start 日改到過去以逼出 refund>0——這恰好複製了 **`_compute_unused_refund` 以日期當「已付」proxy** 的假設，對「過了 start 但付款靜默失敗」的 [[project_324_subscription_dup_charge_incident]] 類訂閱為假 → 刪服務退**未賺得**點（HIGH 交集缺口，stg 坐實 9 張 ≈4,224 點；詳 #324 memory）。故 E2E 實為該曝險的 PoC，**非** delete-settlement 全路徑乾淨驗證。**殘留（Eugene 決定保留不動）**：sub 31→cancel（正確審計）＋partner3(admin) 錢包 +132.67 phantom；immaterial（admin 測試帳＋TTL sandbox）故不反轉（reverse 腳本 `reverse_residue.py` 已備，未執行）；ws ns quota 已確認回單服務值、無 svc-8e631caf 殘留。

**衍生 [[project_324_subscription_dup_charge_incident]]**（issue #324，priority:high）：E2E 前置盤點 stg 錢包時發現訂閱扣款 failed 每小時無限重試（832 筆）；根因＝上游 `sh_subscription_wallet` 兩支 cron，與本 repo fix 層 `_sh_create_auto_invoice`（dunning-aware）並存冗餘。修法傾向停用/覆寫上游 cron。
