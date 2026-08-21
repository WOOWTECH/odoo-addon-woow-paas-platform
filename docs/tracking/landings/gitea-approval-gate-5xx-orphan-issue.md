# reference gitea approval gate 5xx orphan issue

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_gitea_approval_gate_5xx_orphan_issue`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：paas-odoo-ci 的 prod build 人工審核 gate 建 approval issue 時遇 Gitea 5xx 會讓 job 陣亡，但 issue 其實已建立成功——伺服器端成功、客戶端判定失敗，留下沒人輪詢的孤兒 issue

`woow-paas/paas-odoo-ci` 的 prod build 有人工審核 gate（`scripts/wait-for-approval.sh`）：建一個 `[prod-approval] Build <image>:<tag>` issue → 輪詢留言 → 授權清單內的人留**精確等於** `/approve` 才放行。`APPROVERS` 來自 `vars.PROD_APPROVERS`，`vars.ALLOW_SELF_APPROVE=true` 時 tag pusher 可自核。

**故障模式**（repo issue #43 已記載，2026-07-31 prod `.88` 發版再次撞到）：

```
::notice::ALLOW_SELF_APPROVE=true — tag pusher 自核已開啟
curl: (22) The requested URL returned error: 500
##[error]Process completed with exit code 22.
```

建 issue 的 `curl -fsS -X POST .../issues` 收到 500 → `-f` 使 exit 22 → `set -e` 中止 job（腳本刻意 fail-closed，不 `|| true`）。job 存活 4 秒。

**關鍵**：500 發生在**回應已寫入 DB 之後**——issue 實際建立成功了（author `Gitea Actions`）。所以留下一顆**沒有任何 process 在輪詢**的孤兒 issue，在上面留 `/approve` 完全沒有效果。清掉它並註記，別讓下一個人對著它按。

**繞過**：改走 `workflow_dispatch` 重跑即可。⚠ 重跑**必須顯式帶 `addon_ref`**，否則會 build 出倒退 image——見 [[reference_paas_odoo_ci_addon_ref_main_stale]]。

**修法建議**（寫在 #43 留言）：重試前先 `GET /issues?state=open&q=<title>` 查同名 issue，命中就接手輪詢（冪等）；無條件重建會每次重試多留一顆孤兒。

**連帶成本**：gate 佔用**唯一的 act-runner**（repo issue #30），等待審核期間全實例 CI 停擺，逾時上限 180 分鐘。所以觸發後要儘快找人按；別在無人值守時觸發 prod build。

判定 CI 成敗一律看 `Conclusion` 不看 `Status`——失敗的 run 在 `tea actions runs list` 也顯示 `completed`（[[reference_tea_actions_runs_list_completed_only]]）。

關聯 `docs/guides/deployment/prod-release-sop.md`（該筆逐次紀錄已於 2026-08-04 收斂進 SOP §5.1／§7／§9 後刪除）、[[reference_gitea_single_runner_approval_gate_blocks_ci]]。
