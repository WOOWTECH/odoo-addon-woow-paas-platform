# project 484 named failure fields

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_484_named_failure_fields`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#484 operator 具名失敗欄位被忽略兩縫（PR#487）；改 fail-closed 會打破「單段免 guard」前提而必須補 stored-id guard"

2026-07-30。#484＝[[project_477_update_service_fail_closed]] 收官後同族盤點開出的兩縫，**PR#487**（`fix/484-named-failure-fields` → develop，base `a0cb49a9`，未 merge）。

同族但換載體：operator 把失敗放進**具名欄位**而非 `success`，所以「呼叫端沒檢查」不是漏了 `else`，而是**那個欄位 grep 零命中**。

- **`dns_failed`**：`smart_home.action_provision` 改成不寫 `active`、寫 `error`＋指名 hostname/tunnel id，**tunnel 欄位照樣落地**（已計費，丟了＝永久孤兒）。
- **`pod_retrieval_error`**：`_is_container_ready` 前置 `HARD: pod lookup failed` fail-closed，與 `_is_vm_ready` 對稱。**F-423-1 的 `pods=[]` 決議不動**——分界寫成 characterization 測試而非註解。

**最可複用的一課（本案的真正難點，不在 issue 原文的驗收方向裡）**：把一個**單段** remote-provision 改成 **fail-closed**，等於把它變成**雙結果**——`error` 從此可能與一條活著的遠端資源共存，於是 [[two-stage-remote-provision-idempotency]] 要求的 stored-id guard 變成**必要組成**。那份 pattern doc 當時正是拿 SmartHome 當「單段、免 guard 也安全」的對照組，我的修法親手打破了它引用的前提。**改 fail-closed 前先問：這個 method 的「免 guard 安全」是不是靠單段性換來的？** 順帶查出既存孤兒洞：`/api/smart-homes` 的 `action='provision'` 沒有 state 閘（只驗 OWNER/ADMIN＋訂閱），對已 active 的服務重打會建第二條 tunnel。

guard 分支與 SA 刻意不同：SA 能只重跑第二段收斂回 active；SmartHome **沒有**「對既有 tunnel 補 DNS」的 operator 端點（只有 `create_tunnel` 與 routes sync 會建 DNS，前者建新 tunnel、後者要 SmartHome 沒建模的 route payload）→ 平台修不好就不能假裝可以，guard **fail-loud** 並指出兩條安全出路。

流程教訓：本輪 develop **前進兩次**（#481 merge → #461/#485），且 #485 動到我的目標檔 `cloud_service.py`＋repo-lint 加了第 9 個 gate。**rebase 後 baseline 必須用 merge 後的 develop 重取**（舊 baseline 少一條 `TestMcpAdminPassword` 紅，直接比會誤判為新增紅燈）。RED 7/13 → GREEN 57/57 scoped；全量 1997 vs baseline 1984，兩向差集皆空（18 條既有紅）。
