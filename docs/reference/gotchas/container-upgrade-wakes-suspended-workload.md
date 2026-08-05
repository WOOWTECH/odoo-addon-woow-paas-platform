# reference container upgrade wakes suspended workload

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_container_upgrade_wakes_suspended_workload`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：容器型停權服務被 helm upgrade（reset_values）喚醒的機制——平台 values 不帶 replicas、chart 預設 replicas:1 復活 workload；且容器型不能照抄 VM 的 values 注入解法

**平台的 suspend 是外科式的（operator `kubectl scale` / `patch vm`，不動 helm release），而三條 upgrade 路徑都走 `reset_values=True`（chart values.yaml 為 base）→ chart 預設 `replicas: 1` 會把停權（欠費）中的容器直接喚醒。**

2026-07-31 docker-desktop 實測（真 operator + helm 3.13.3 + 真 seed template／chart，#482）：

| 階段 | n8n（Deployment） | postgresql（StatefulSet） |
| --- | --- | --- |
| `_do_suspend()` | replicas 0 | replicas 0 |
| `action_redeploy()` | **replicas 1** | **replicas 1** |

喚醒是實質的：pod 實測 `Running/ready=true`。**Deployment 與 StatefulSet 兩型都中。**

**VM 型不會**——`_assemble_deploy_values` 只在 `workload_type == 'vm'` 注入 `vm.runStrategy`（W5a.4 守衛），所以 VM 只會卡死在 state 汙染，不會被喚醒。這個非對稱正是「容器型不可從 VM 結果外推」的原因。

## 🔴 容器型不能照抄 VM 的 values 注入解法

replicas 的 values 鍵名**因 chart 而異**（bitnami＝`primary.replicaCount`、n8n 又是另一套），平台對任意第三方 chart 無法猜鍵名。VM 能注入是因為 haos 是自家 chart、鍵固定。

正解＝**upgrade 後外科式 `scale_release(replicas=0)`**：與 chart 無關，且對 VM 冪等（operator 把 `replicas=0` 映射成 `runStrategy: Halted`，見 [[project_423_w63_scale_vm_gate]] 的三 workload 語意）。

## 判讀線索

- 平台送出的 values 有沒有帶 replicas，直接印 `svc._assemble_deploy_values()['values']` 找 `'replica'` 子字串即可（實測兩型都 False）。
- `subscription_lifecycle_cron` 是 **1 day** 間隔 → 被喚醒後最壞白燒約一天；若租戶在窗口內付款（sub→`in_progress`），cron 分支 2 要求 `state == 'suspended'` 才 resume，state 已被汙染就**既不 resume 也不再 suspend＝永久卡住且容器永久在跑**。

## ⚠ 被喚醒後的 state 不會停在 `deploying`（盤存量時最容易漏的一點）

上面 A/B 表停在 `deploying`，是因為那是手動實驗、沒讓 poll cron 跑。真實環境 poll 是 **1 分鐘**間隔且 `_poll_and_update_status` **不看 `_is_halted()`**——容器一 ready 就依 template 把 state 推成 `initializing` → `running`。所以 prod 存量裡容器型那批多半已經**不在 `deploying`**（2026-07-31 #497 實查）。

盤點停權汙染存量的正確 domain 是**旗標**而不是某個 state：
`[('pre_suspend_state','!=',False),('state','not in',['suspended','deleting'])]`。

收官見 [[project_482_suspended_redeploy_landing]]、[[project_497_stuck_suspended_converge_landing]]（後者含「收斂 state 會關掉唯一 scale 0 路徑」的二階陷阱）。
