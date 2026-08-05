# project 277 278 w1 ns quota landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_277_278_w1_ns_quota_landing`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：W1 收口

W1「資料/資安止血 P0」最後兩項 landing → **整個 W1 收口**（#280/#281/#268/#279/#278/#277 全完成）。兩項出自同一 peer-reviewed spec `docs/plans/2026-Q3/2026-07-07-w1-ns-quota-governance-design.md`，各自 writing-plans + ultracode subagent workflow（每 task 對抗式 review）執行。

**#278 quota/LimitRange**（PR #312→develop `257d77d`，version .46）：
- CS ns quota **只增不減 drift 修復**：`cloud_service.py` `_compute_ns_quota(workspace)`/`_push_ns_quota()` + delete-path recompute over survivors。
- agent per-project ns 補 ResourceQuota+LimitRange（`agent_runtimes.py` AGENT_QUOTA cpu 3/mem 3Gi/pvc 3/storage 10Gi）——堵「agent ns 無 quota＝cluster DoS 逃逸口」。
- operator `PUT /namespaces/{name}/quota` + `update_namespace_quota()` + `_build_limitrange_manifest()`。

**#277 孤兒 ns reconciler**（PR #313→develop `05ae10c`，version .47）：
- 延伸既有 **L4 reverse-orphan 框架**，加 k8s namespace 為唯讀 dry-run fact。
- operator **env-scoped** `list_managed_namespaces`（`-l paas-managed=true` + PAAS_ENV 有設時 AND `paas.woowtech.io/env`——共叢集消跨 env 噪音）；走 helm.py 唯讀方法（非 reconcile.py inline subprocess，守 delete-gate）。
- Odoo `reconciler.py` `collect_left_set` **union CS.helm_namespace ∪ agent_session.helm_namespace**（排除 deleting、去重）——漏 agent ns 會誤判所有活躍 agent ns 孤兒（災難級，已測試守護）。`reconcilable` 恆 False = report-only 非破壞。
- ns owner label `woow.tw/owner`（CS=workspace_id、agent=project）補孤兒失 record 後的 owner 線索；**created-at 用 k8s 原生 `metadata.creationTimestamp`**（RFC3339 冒號是非法 label value + 會被 re-stamp clobber，故不自訂——偏離 spec 字面但更正確）。

**驗證**：operator 全套 372 passed（native `.venv`）；Odoo #278 T4 17 tests + #277 T3 10 tests 於 **woow-k3s native amd64 實綠**（cp-overlay recipe，見 [[reference_odoo_test_amd64_woowk3s_cp_overlay]]）。

**上線約束**：operator 改動（#277 list-ns / #278 quota PUT）需 **rebuild image + Fleet pin** 才在 stg/prod 生效；operator+Odoo **須同版**（否則 operator 回 `kind=namespace` 但 Odoo 缺對映 → compute_orphans skip+partial 靜默失效）。運維：#277 merge 後一次性清既有孤兒空 ns `paas-ws-stg-ad023482`。

**stg 已完整部署（2026-07-07，operator + Odoo 同版）**：
- **operator**：image `paas-operator:20260707.1305`（build run 834，develop HEAD 05ae10c，digest `sha256:6f5898c9…`）；Fleet pin 委派 `woow-paas-cd` agent 改 `paas-operator-stg/values-stg.yaml:8`（commit db94785→main）→ rollout PASS（digest 吻合、restarts=0、F6 env guard OK PAAS_ENV=stg、quota PUT + reconcile inventory endpoints live）。
- **Odoo**：deploy-stg（tea 建 `stg-20260707.0538` tag→paas-odoo-ci CI success→image `paas-odoo-stg:20260707.0538`）→ **create.sh `--yes` in-place helm upgrade**（REVISION 52，**DB 保留**：6 running CS 未動）→ 新 pod 1/1 Running 0 restart、DB `installed_version 18.0.1.0.47`、reconcile cron active（nextcall 2026-07-10、7d）。
- **真 cron 首份持久化報表**（手動觸發 `_cron_reconcile_report()`）：103 筆（79 tunnel + 10 access_app + 7 access_policy + **7 namespace**，**partial=False**），與 cp-overlay 探針**完全一致**；**零假陽性**（2 個 live ns 5a105e8b/86f34bd4 正確留左集）。7 個孤兒 ns：`paas-ws-stg-ad023482`（CS 孤兒）+ 6× `paas-ws-stg-pa-*`（agent 孤兒）→ **已清（2026-07-07）**：5 個真空 + 2 個持 workspace PVC（agent-runtime 舊版 scaled-0、helm_ns=False Odoo 失聯 → `_cron_gc_reaped` forward-GC 抓不到，使用者裁定一併刪；⚠️ 刪前逐一驗 PVC，2 個非空的先 surface 再由使用者決定）；2 個 live ns 保護未動。**閉環**：重跑 reconcile 報表 103→96、namespace kind 歸 0。

**跨 repo 分工實錄**：operator build＝本 repo CI（tea workflow_dispatch，我）；operator Fleet pin＝CD repo（委派 woow-paas-cd agent via `maestri ask`）；paas-odoo image build＝paas-odoo-ci（deploy-stg skill 的 tea api tag，sanctioned repo-leader 機制，我）；dev-sandbox deploy＝我（create.sh）。**prod 全未動**（另循程序：operator 改 `paas-operator/values-prod.yaml`、Odoo 走 prod 部署路徑）。部署 recipe 見 [[reference_build_operator_deploy_stg]]、[[reference_deploy_stg_gotchas]]、[[reference_odoo_test_amd64_woowk3s_cp_overlay]]（cp-overlay 探針法）。

**gotcha**：ultracode execute workflow 曾撞 `StructuredOutput retry cap exceeded`（單一 impl agent transient，該 agent 已 commit 卻 crash 在 output 層）→ **不 resume**（會 re-run 已 commit 的 agent）、改跑乾淨 recovery workflow 接續 review+後續 task。相關 [[project_275_project_hide_toggle_landing]]。
