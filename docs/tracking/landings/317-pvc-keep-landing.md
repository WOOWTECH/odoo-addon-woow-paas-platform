# project 317 pvc keep landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_317_pvc_keep_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。

**#317**（CS 刪除時 chart-owned PVC 被 helm uninstall 直接刪、90 天 orphan grace 落空、資料丟失）→ **PR #321**（git-prod odoo-addons/woow_paas_platform，target develop，2026-07-08 送出，**未 merge**）。

**pivot 教訓（核心）**：issue 原拍板「delete 時對 live PVC 打 keep annotation」，我照做（新增 `annotate_pvcs` + `delete_release` 時序重排「先標後拆」），過了 plan-review + 全單元測試綠——但 **Task 3 整合測試抓到真 bug**：真實 helm 下 chart-owned PVC 仍被刪（mock 掉 helm 的單元測試結構上看不到）。根因見 [[reference-helm-resource-policy-stored-manifest]]（helm 讀 stored manifest 非 live）。→ 停下請 leader 決策（用 AskUserQuestion），Eugene 改拍板 **install-time post-renderer**。舊的兩個無效 commit `git reset --hard` 清掉、重寫 plan、重跑 plan-review。

**最終修法**（PR #321，install-time approach）：
- `extra/paas-operator/src/pvc_keep_post_renderer.py`：render 時給**每個 PVC** 加 `helm.sh/resource-policy: keep`（scope 只 PVC、其他資源逐字不動、`setdefault` 不覆蓋 chart 明確設定）。
- helm `install()` + `upgrade()` 都帶 `--post-renderer <sys.executable> --post-renderer-args <script>`。
- `delete_release` **不改**：留存 PVC 由既有 orphan-label 流程接手（標 orphan + 90 天 grace，cron 清）。
- 測試：單元 381 passed（含 post-renderer 純函式 7 + install/upgrade flag 2）；整合 15 passed（真 helm 3.13.3 + docker-desktop，經 operator HTTP 端到端）。plan-review miniclaude round 2 APPROVED（plan：`docs/plans/2026-Q3/2026-07-08-317-chart-owned-pvc-keep.md`）。

**待辦（ops follow-up，leader 排，不在本 PR）**：既存 stg 4 顆 CS backfill（`helm upgrade --post-renderer` 注入 keep，只有新 install 自動受保護）；operator image rebuild + Fleet pin + 同版。**已知 concern**：rollback 到 pre-keep revision 會失保護（過渡期風險，rollback 罕用）。

**2026-07-09 收尾（leader2 session）**：PR #321 **已 merged**（8577944）、operator image 8577944 上 stg+prod（Fleet）。**backfill 完成**：實際是 **6 顆**（非 4）——ws `5a105e8b` svc-16e7ac1a(n8n)；ws `86f34bd4` svc-5b78f97b(n8n)/svc-06b55af1(odoo)/svc-0246586d,svc-83fa4bf9,svc-8e4f5042(mcp-server)。作法＝operator `PATCH /api/releases/{ns}/{name}`（`X-API-Key`、body `{chart:<OCI>, version:<釘現況部署版>, reuse_values:true}`）；**version 必釘現況**（template 的 n8n/odoo version 欄為空→照抄會偷升級 latest）；chart OCI ref 從 Odoo `cloud.app.template.helm_chart_name` 撈（權威）。canary(svc-16e7ac1a)先驗再放行 5：**rev+1、stored manifest 各 1 筆 keep、live PVC=keep、pod 零 roll**。STS volumeClaimTemplate PVC（`data-svc-06b55af1-postgresql-0`）維持空＝正確（非 helm-owned、uninstall 本就不刪）。**合併 teardown E2E（#317×#318）PASS**：throwaway n8n（ws1，release svc-8e631caf）→ 全新 install 的 PVC 直接帶 keep（install-time post-renderer 對新裝也生效）→ `CloudService.unlink()` → **release GONE 但 PVC svc-8e631caf-n8n SURVIVED(Bound,keep)**、workload 拆除。cleanup 只刪該 throwaway PVC（同 ns 有 svc-16e7ac1a 故**絕不刪 ns**）。詳見 [[project_318_subscription_unlink_landing]]（settle leg）與 [[reference_helm_resource_policy_stored_manifest]]。
