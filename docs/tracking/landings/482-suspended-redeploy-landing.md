# project 482 suspended redeploy landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_482_suspended_redeploy_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#482 停權服務被 redeploy 後無法復權：PR#493（三條 upgrade 路徑一起修）；A/B 實測翻案＝容器型被喚醒非卡死；reset 那條實查不可達＝縱深"

**#482（#423 收官後第一個 fast-follow）→ PR #493 已 merge**（2026-07-31，rebase 到 `f0bcd5ec` 後最終 sha `449d8ba4`，版號 18.0.1.0.85，#482 自動關閉）。衍生 follow-up **issue #497**（既有卡死資料無自癒，見下）。

## 動工前的 A/B 是決勝點，不是形式

issue 的 open question 要求先補容器型實測。**結果推翻了保守假設**：容器型不是卡死，是**被喚醒**（機制見 [[reference_container_upgrade_wakes_suspended_workload]]）＝計費繞過，比 VM 的卡死更嚴重。若照 VM 結果外推「大概也是卡死」，修法就只會處理 state 欄位，漏掉真正在燒錢的那半。

## 實查推翻了 issue 對第三條路的描述

issue 說「reset admin password 也走同一條 values 管線」，實查 `cloud_service.py` 的 `_reset_admin_password` **開頭就有 `if self.state != 'running': raise UserError`** → 對 suspended **不可達**。所以那條修的是**縱深**（防「先被 reconfigure 汙染成 running 而 `pre_suspend_state` 仍在」的二階情境），不是可達洩漏。派工單的敘述照收但要自己驗——三條路的可達性各不相同：

| 路徑 | 出口 | 可達性 |
| --- | --- | --- |
| `action_redeploy` | `upgrade_release` | ✅ 實測喚醒 |
| reconfigure `_update_service` | 背景執行緒 `upgrade_release` | ✅ 可達（入口守衛只擋 pending/deleting/error） |
| `_reset_admin_password` | `reset_release_config` → operator 端仍是 `helm_service.upgrade(reset_values=True)` | ⚠️ 不可達，加縱深 |

## 測試的兩個坑

- **背景執行緒要 patch 對地方**：`_bg_upgrade` 是**函式內 import** `PaaSOperatorClient`，patch controller module 的同名屬性攔不到（會讓它拿真 client 去打 MagicMock 當 URL、測試紅得像修法沒生效）。要 patch `services.paas_operator.PaaSOperatorClient`，並把 `threading.Thread` 換成同步執行。
- **背景那半用自己的 cursor**，讀不到測試 transaction 未 commit 的 row（`svc.exists()` False）→ **背景對 DB 的寫入在測試中不可觀察**。故 state 收斂要放主執行緒才測得到；scale 斷言則靠 client mock 成立。

## Rebase 到 f0bcd5ec（#491 age timeout）撞到的兩件事

1. **#491 是在「#482 缺陷還在」的前提下寫的**：它的 `_timeout_diagnosis` 有 `_SUSPENDED_HINT`，註解明說停權服務被 redeploy 後會卡在 deploying、永遠不會 ready。也就是**修好之前，停權服務會被 age timeout 打成 `error`**——而 error 同樣不是 lifecycle cron 分支 2 認得的復權起點（要求 `state == 'suspended'`）＝#482 換皮重演。修法後兩者不相交（停權服務不再進 `_TIMEOUT_GOVERNED_STATES`），已用 `TestTimeoutInteraction489` 釘住。
   **既有卡死資料仍有殘留**：被打成 error 後，若訂閱仍欠費，cron 分支 3/4 會把它拉回 suspended（自癒）；若訂閱已恢復則卡住。建議 follow-up 用 migration/cron 收斂。
2. 🔴 **#491 的 `test_migration_dir_is_newer_than_manifest_version` 斷言 `manifest == '18.0.1.0.84'`**——把合併當下的巧合鎖成契約，於是**任何後續 bump 版號的 PR 都會踩紅**（本 repo 每個 PR 都 bump）。Odoo 真正的判準是 `latest_version < 目錄版號 <= 新版號`，所以不變式是「目錄不得**高於** manifest」，低於是正常的。改 `assertEqual` → `assertLessEqual`（commit `449d8ba4`，可單獨 revert）。同款反模式見 [[feedback_test_asserting_absence_locks_shortcut]]。

## 殘留邊界（未修，PR 描述已列）

`action_redeploy` 的 **fresh 分支**（`get_release` 失敗＝release 不存在）對停權服務仍寫 `pending` → cron 重新 install → chart 預設把它裝回線上。前提是 release 被外部刪除（停權是 `kubectl scale`、不動 release，正常停權服務走不到），屬災難復原情境。

## 數字

新測試 9F→0F of 15；W5a 守衛 0F of 15；全套本分支 1F/0E of 2013 vs develop baseline 1F/0E of 1998（唯一紅燈 `TestMcpAdminPassword.test_chart_version_drift_is_logged` 兩邊相同＝**develop 既有紅燈**）。真環境 GREEN 對照：redeploy 後 helm revision 2→3 但 replicas 保持 0、卡死服務不必人工改 state 即可 resume。

相關：[[project_423_w6_stg_e2e_round1]]、[[project_423_w63_scale_vm_gate]]、[[feedback_test_asserting_absence_locks_shortcut]]。
