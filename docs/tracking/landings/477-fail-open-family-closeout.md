# project 477 fail open family closeout

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_477_fail_open_family_closeout`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#477 fail-open 家族連鎖收官：PR#481/#487 merged＋#490（F-423-1 Step1）＋#489（Step2 issue）；operator 200＋success:false 同構縫全 repo 盤點"

2026-07-30/31 fail-open 家族一條龍（單一 teammate fix-477 四棒連跑，context 56% 收官）：

1. **#477／PR #481（merged）**：`_update_service` 模組守衛收斂單一裁決點——預檢與寫入路徑同源；operator `success:false`＋非 running 皆 fail-closed。三條「反過度修復錨」修前就綠（防合法路徑誤傷）。operator 側 HTTP 200＋success:false 契約不動（跨發版線）。
2. **同族唯讀掃查**（Eugene 終端緩衝指令觸發）：全 operator client 呼叫點盤點表——~18 處 fail-closed ✓、security_access 示範級、3 處 low 不開 issue；真縫兩個→#484。#472 順帶查證＝現況不騙人（勘誤：build.sh:18 有 set -euo，先前只看前 5 行誤判）＋三個「build exit 0 ≠ 可用 image」前瞻風險留言（②③ code 在 woow-dev plugin 勿混 PR）。
3. **#484／PR #487（merged）**：dns_failed（Smart Home 誤標 active）＋pod_retrieval_error（就緒誤報）fail-closed；**修法打破 two-stage-remote-provision-idempotency pattern doc 拿 SmartHome 當單段對照組的前提→按 doc 規則補 stored-id guard**，順帶關掉 provision 端點無 state 閘的孤兒洞。
4. **F-423-1 評估→拍板修**：pods=[] fail-open；13/15 template init=none 無下游兜底、Recreate 升級真空窗誤標 running；「卡住可見且安全 vs 假就緒不可見且不安全」。誤傷僅一輪 poll（5-60s）、現存 running 機隊零影響（F-423-1 原文高估）。
5. **Step 1／PR #490**（待 merge）：`_is_container_ready` pods=[] → False；**前置閘＝15/15 template `app.kubernetes.io/instance` label 證據表**（live pod 唯讀直查＋helm template 靜態 render 混合，免真部署 14 套）；三條 W5a fixture 把「純 VM 無 pod」錯誤前提編碼進測試→改 fixture（_VIRT_LAUNCHER）非放寬實作；行為變更＝VM 合取真正生效（VMI Ready ∧ virt-launcher ready，skew 時多一輪 poll）。**Step 2＝#489**（age timeout，deployed_at 當錨）。

6. **#489（Step 2）／PR #491**（待 merge）：age timeout——deploying/upgrading/pending-* 超時轉 error＋分型診斷；錨=deployed_at/last_upgraded_at 取較晚者（**三個轉移入口原本漏記錨**：action_redeploy/_reset_admin_password/rollback，同 PR 補）；**UTC 陷阱**（controller 本機時區 datetime.now() 會讓 age 恆負＝保護靜默失效、UTC 容器測不出）→ repo-lint 第 10 gate check_transition_anchor_utc；`18.0.1.0.84` migration 刷轉移中服務錨（Eugene 裁決，消除 -u 一次性誤殺窗）＋manifest 成對 bump；#482 交互=不衝突（已留言含 guard 陷阱提醒：判準用 pre_suspend_state 勿枚舉 state）；順帶發現 #483 的 F-461-2 斷言了 production 未實作的 log 字串（孤兒斷言）。

跨棒教訓：maestri ask 背景通道必被系統砍（訊息送達、砍等待側）＝正常模式，完工偵測靠 Monitor 掃終端 spinner；~~Eugene 會在 teammate 終端輸入框留未送出指令~~（**2026-08-26 更正歸因**：輸入框的未送出文字是 **Claude Code 的輸入建議功能**自動生成，Eugene 確認非本人所留——載體 #908 留言；歸因差別會改變應對：以為是人留的會傾向照做，知道是工具猜的就明白照做＝自己給自己下令。原句「leader 以『自我消毒 ask』放行」的紀律本身仍然正確且保留）；teammate 交棒間 develop 被他人推進→插播提醒 rebase＋baseline 重取（有效）。

關聯 [[project_471_conceal_instances_split]]、[[project_460_reset_button_gate_landing]]。
