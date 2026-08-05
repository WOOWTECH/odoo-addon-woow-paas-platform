# project prod canary smoke 20260708

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_prod_canary_smoke_20260708`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：2026-07-08 prod 首租戶 canary 全鏈煙測 PASS_WITH_FINDINGS：五項 prod 首驗里程碑 + 2 新 findings（PVC orphan-label chart-owned 失效、SC=longhorn Retain）+ 人工收尾清單與 canary recipe

**2026-07-08 prod 首租戶 canary 全鏈煙測 PASS_WITH_FINDINGS**（3 段 workflow wf_a79fc397-ca9，對抗覆核 agree）。canary＝workspace id=1「canary-smoke-w1」+ n8n CS（ns paas-ws-b2e1a1ad、release svc-cf042cbf、subdomain paas-cs-b2e1a1ad-36cf44b8）。時間軸：建立 committed→helm install→sidecar patch→n8n init succeeded 全程 **~90 秒**→HTTPS 200。

**Prod 首驗里程碑（全過）**：#278 quota（8/8Gi/100Gi/pvc10）+ **LimitRange 500m/512Mi＝0.1.17 RBAC 修復 prod 首活驗**；#279 baseline 4 NP（allow-mcp-reach 雙 selector env=prod + reach-ws-1）；ns 四標實值（paas-managed/env=prod/workspace-id=1/owner=1，#277）；**#207 CF route 對稱刪除 prod 實證**（teardown 後 check_route_availability=free，CF API 實查非 odoo record）；**#277 ns leg prod 首份有效報表**——run#1（canary 活）ns 候選 0＝左集正確；run#2（unlink 後）**恰 1 筆** paas-ws-b2e1a1ad reconcilable=False 人工審＝report-only 活證；hermes-mcp-admin 等 6 顆裸 ns 無 paas label 不進報表（safe-miss 符設計）。**零真金流**：20 個 plan 全 sh_trial=True 7 天、INVOICES_CREATED=0、兩支計費 cron domain 只抓 state=in_progress（cancel 態零扣款，覆核 inspect 原始碼實證）。

**2 個真 findings（待開 issue）**：
1. **#281 orphan-label 對 chart-owned PVC 不生效**：8gears n8n chart PVC 無 `helm.sh/resource-policy: keep` → helm uninstall 直接刪 PVC，90 天 grace 無標的可標（releases.py:224 先 uninstall 後標 remaining，code comment 明示只涵蓋 StatefulSet volumeClaimTemplate 型）。定性＝**spec/承諾 gap 非 code regression**；修法時序注意：須 uninstall **前**先對 instance-scoped PVC 打 keep annotation/orphan 標。後果：n8n 類用戶 delete service＝資料立即丟失（本次靠 PV Retain 意外兜住）。
2. **租戶 PVC 實吃 cluster default SC=`longhorn`（Retain/Immediate）非 code default `longhorn-delete`**：storage_class_name param 只影響 operator 側，chart 未指定 SC 時吃 cluster default。雙面刃：意外保資料，但每次 delete 累積 Released PV——cluster 已有 **~47 顆歷史 Released PV**。需 SC 策略決策（chart values 明示 storageClassName）+ Released PV 清理 SOP。

**已知 gap 實證**：F-N8N-3 unlink 不取消訂閱（本次手動 write state='cancel' 補救）——**真用戶 UI 刪服務會留活訂閱繼續扣款，上線前建議修**（第三張候選 issue）。

**人工收尾 ✅ 完成（2026-07-08 晚；Eugene 刪 ns + 切出 auto mode 逐提示放行）**：run#3 **namespace 候選歸 0**（106→105 列，CF 三類噪音持平＝tunnel 83/access_app 12/access_policy 10）；canary PV + Longhorn volume 兩步皆刪（volume 步涉 longhorn-system，由 Eugene 放行）；workspace id=1 unlinked（count 0）；SUB-00001 cancel 留審計。47 顆歷史 Released PV 經歸屬實查大多屬其他部署（僅 6 dev-sandbox + 2 paas-system 屬平台側），清理走 issue #319 scope SOP。

**Canary recipe 要點（可複用）**：odoo shell（/opt/bitnami/odoo/bin/odoo shell -d bitnami_odoo -c /opt/bitnami/odoo/conf/odoo.conf；import 路徑 odoo.addons.woow_paas_platform.services.*、寫入必 env.cr.commit）ORM 建 workspace + 走 controller 同邏輯建 CS；n8n 模板（id=2）匿名可拉最適 canary；部署由 ir_cron_deploy_cloud_services（1 分鐘）推 pending→running；reconcile 觸發＝`env['woow_paas_platform.reconcile_report']._cron_reconcile_report()`（snapshot-replace 語意）。附帶發現：prod SMTP 全 exception（mail.mail 7 封 digest 全卡，既存問題另查）；operator PVC_GRACE_PERIOD_DAYS=90（非 code default 7）。證據目錄 scratchpad evidence/（operator.log 含已死 canary 憑證，閱後即焚）。相關：[[project_e2e_271_279_blocked_rbac_limitranges]]、[[project_277_278_w1_ns_quota_landing]]、[[reference_stg_cloud_service_live_create_e2e]]。
