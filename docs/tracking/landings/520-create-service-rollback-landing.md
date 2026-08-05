# project 520 create service rollback landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_520_create_service_rollback_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："2026-08-02 #520 create service 吞例外缺陷——PR#523 修復、#525 同構縫盤點，含兩個方法論教訓"

2026-08-02。stg 建 Hermes Agent 顯示失敗、服務卻 Running。根因見 [[reference_odoo_controller_swallowed_exception_still_commits]]。

產出：**#520**（缺陷）→ **PR #523**（修，`except` 拆三分支＋共同 `cr.rollback()`）→ **#525**（同構縫盤點）。stg 殘留 id=61 已按正規路徑刪除（`state=deleting` → operator `uninstall_release` → `unlink`，非直接動 DB），重建的 64 訂閱／試用／secret 三項齊全。

## 教訓一：半綠會被整條紅掩蓋

一條測試的結果是 AND，報告只顯示**第一個失敗的斷言**。於是同一條測試裡其他斷言的兩種失效方式，在「整條紅」的畫面上長得一模一樣：

- 第一個失敗**之前**的斷言：跑了且綠，綠被整條的紅蓋掉
- 第一個失敗**之後**的斷言：assert 一失敗就 raise，根本沒執行

#520 第 5 條測試（AccessError）屬前者——rollback 半在修前會紅，訊息半在修前必然綠（該分支的行為與修前相同），但整條紅讓「訊息半從沒被驗證過」看不出來。

⇒ **GREEN 之後追加的測試要單獨補跑 RED**，且一條測試裡每個斷言各自要有「造得出來的會紅情況」。驗證手法：還原實作補跑（守修前缺陷的那半）＋ 拿掉新分支做注入式驗證（守新增行為的那半），兩段都要貼原始輸出。

## 教訓二：teammate 順手盤的清單不能直接開 issue

PR #523 附了 7 處同構縫清單。我派 9 個 agent 獨立驗證（禁止照抄措辭、要求兩種翻盤情況判 REFUTED）：**行號 9 個全中、無一 REFUTED，但 5 處的因果敘述與程式碼不符**——照 #520 修法照抄過去會是 no-op 或誤傷：

- `_sync_mcp_server`／`_sync_project_mcp_server`：callee 自帶 catch-all，半寫入走的是 **`success: True`** 出口，controller 的 except 幾乎是死碼；在此補 rollback 反而會回捲 `state='error'` 稽核紀錄
- `api_service_reset_admin_password`：程式碼**刻意**把 secret 寫入排在 operator 2xx 之後（有整段設計註解），主要失敗路徑零殘留
- `_list_services`：**不回 `success: False`**（寫死 True）、回應與 DB 自洽，且那些 commit 是輪詢器的設計意圖
- `_update_project_mcp_server`：except 近乎死枝（可觸發的 constrains 已被 controller 自己先擋）

真正確認該修的只有 3 處，其中 `api_service_skip_trial` 最嚴重：殘留可達「**錢包點數已扣、發票 posted、但 account.payment 沒建**」，且脫離試用的 write 在 invoice create 之前 ⇒ 使用者重試會被自己的殘留擋掉，形成不可自我修復的死狀態。

⇒ 副產品清單的價值在「指出哪裡值得看」，不在「說了什麼」。把未取證的斷言寫進 issue，接手的人會拿它當前提直接動工。

相關：[[feedback_teammate_deliverable_verify_objects]]、[[feedback_maestri_dispatch_verify_premise_first]]、[[feedback_negative_verification_needs_positive_control]]
