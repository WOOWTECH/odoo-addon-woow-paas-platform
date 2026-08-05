# reference helm atomic failure double timeout

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_helm_atomic_failure_double_timeout`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：helm --atomic 失敗路徑吃 2× --timeout（per-operation），subprocess 不蓋 2n+O 會腰斬 rollback 卡 pending-*

helm 的 `--timeout` 是 **per-operation**：`--atomic` upgrade 失敗時的自動 rollback 是另一個 operation，會**再吃一次** wait。失敗路徑牆鐘＝`2n + O`（n＝--timeout、O＝非-wait 開銷：OCI pull/render/apply×2，實測上界 ~15s）。包 helm 的 subprocess timeout 若只給 `n+slack`，會在 rollback **進行中** SIGKILL helm ⇒ release 卡 `pending-rollback`/`pending-upgrade`、Secret 停在新值＝狀態不明——正確性 bug 不是調參（#461 真機 I4 實證）。

- **既有缺陷仍在**：operator 非 reset 路徑的 atomic 呼叫不傳 `--timeout`（helm 預設 5m×2=600s）而 subprocess=settings.helm_timeout=300s ⇒ 今天的失敗路徑 rollback 根本沒機會跑就被 kill。記錄於 follow-ups F-461-3，勿當成新 regression。
- **量測陷阱**：2n 只有 **Recreate** strategy 的 fixture 量得到；RollingUpdate 下舊 pod 撐到新 pod Ready、rollback wait 瞬間通過，只量到 1n＝斷言沒牙。deployment strategy 是實質測試參數不是細節。
- 本機 brew helm 已是 v4（--atomic 改名 --rollback-on-failure、--post-renderer 改吃 plugin 名）——跑 operator integration 必用 Dockerfile 釘的 3.13.3（CLAUDE.md 已載）。

出處：#461 審查（[[project_461_reset_async_landing]]）。
