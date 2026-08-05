# project 411 reset values landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_411_reset_values_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#411 reuse→reset 統一 values 管線全案：W1–W4 收官（5 PR merged、stg e2e 全綠），W5 prod 已收斂（2026-07-25 隨 .78 rollout，pre/post 皆 CS=0）"

2026-07-25 #411 stg 階段全收官，5 PR 皆 merged 進 develop（tip d57873c5）：

- **#419 主修**：`_assemble_deploy_values()` 統一全量 values 管線（base=svc.helm_values＋4 injectors＋expose；sensitive 刻意不進 helper、呼叫點各自組）；三呼叫點改 reset 語意（action_redeploy／_reset_admin_password／paas.py:2544 reconfigure）；client `reuse_values` 預設翻 False；operator 不動（`ReleaseResetConfigRequest` pydantic extra=ignore，多送 reset_values 欄位安全）。repo-lint AST gate 禁新增 `reuse_values=True`。
- **#418/#420 盤點腳本**：`scripts/audit/audit_411_reset_values.py`（收集＋三分類）＋`dump_computed_values.py`（stg pod 內 odoo shell 取 computed）；報告 `docs/tracking/reports/411-reset-values-audit.md`（#426，1054 行含 audit trail）。
- **#417**：#412-3 刪除重建語意文案（dialog 矛盾句一併修正）。
- **W4 stg 驗證全綠**：e2e 正向＝mcp-k8s 0.1.1→0.1.5 redeploy 後新預設鍵 seedResources 落地（bug class 終結）；負向＝image digest pin／pullSecrets／ingress／user config 全保留＋HA backfill 後 audit (b) 16→0。
- **(c) n8n key 裁決紀錄**：stored≠computed 兩真 JWT → 三重印證（進程 ENV sha256==computed、該 key 打 API 200、stored=len36 stale）＝computed canonical；根因 `cloud_service.py:1289` post-init 回寫 svc.helm_values 但從不 helm upgrade。此型漂移 reset 屬修正性。
- **HA 歷史 release**（pre-#414 開通、helm_values 空）＝(b) 16 筆 backfill 原值照搬。agent-runtime release 定性 out-of-scope（生命週期不經三呼叫點）。

**W5 ✅ 收官（2026-07-25，隨 .78 prod rollout）**：W5-pre gate 與 W5-post 複驗皆 **prod CS=0** → (a)/(b)/(c) 三分類全空集合、全收斂；reset 管線自 `.78` 起在 prod 生效，因零租戶 CS 故無既存 release 被 redeploy，即時風險零。收官紀錄已補在 issue #411 留言（#411 早於本次即已 closed）。見 `docs/guides/deployment/prod-release-sop.md`（該筆逐次紀錄已於 2026-08-04 收斂進 SOP §5.1／§7／§9 後刪除）。

**過程教訓**：teammate session 工具鏈失效（Edit/commit/push 假成功、回報與磁碟不符三輪）→ 停手令＋乾淨 session 唯讀收割 worktree diff 重組交付＝有效 pattern（收割時真跑測試還抓到原作者漏掉的真回歸），呼應 [[teammate-deliverable-verify-objects]]。相關：[[reference_helm_reuse_values_new_defaults_dropped]]、[[reference_operator_reuse_values_drops_new_chart_defaults]]。
