# reference stg cloud service live create e2e

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_stg_cloud_service_live_create_e2e`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：stg 上實際建立 cloud service（如 n8n）驗證部署的 E2E recipe + odoo shell commit 陷阱 + n8n 8gears chart 匿名拉取

在 Fleet 管理的 `paas-stg-system/paas-odoo-platform-stg`（DB `woow_stg`）實際建立 cloud service 驗證完整部署的 recipe。
舊 `dev-sandbox-stg` 已於 [cd#289](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/289) 退役；
部署與驗收正本見 [stg runbook](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/src/branch/main/docs/platform-odoo-stg-runbook.md)。
下方 template／plan／workspace id 為歷史範例，重跑時先查現值與權限；建立、刪除會寫入 stg，須先取得授權。

**存取**：`https://stg.woowtech.io/woow`；內部調試可用 `kubectl --context woow-k3s port-forward -n paas-stg-system svc/paas-odoo-active-stg 8091:80`（80 → web nginx 8080）。

**認證**：`POST /web/session/authenticate` params `{"db":"woow_stg","login":"<核准的測試帳號>","password":"<私下提供>"}`（JSON-RPC；cookie jar 視為憑證保護，密碼不落檔、不印 log）。不假設 admin/admin 或固定 workspace owner，也不以 chart 的 master／DB 密碼推斷使用者登入密碼。

**建立**：`POST /api/workspaces/<ws_id>/services` body params `{"action":"create","template_id":<id>,"name":"...","plan_id":<id>,"values":{"_init.owner_email":"...","_init.owner_password":"Test1234abc"}}`。n8n（template slug `n8n`=`cloud_app_n8n`，id 2）需 `_init.owner_email/password`（>=8 字、含大寫、含數字），plan 1/2 皆 7 天 trial（不立即扣款）。成功→state deploying→running，operator 跑 n8n owner setup + 建 API key。

**刪除/teardown**：`POST /api/workspaces/<ws>/services/<sid>` `{"action":"delete"}`。operator 會刪 CF route+DNS（主 subdomain + `-mcp` sidecar）+ helm uninstall + MCP svc。**但不刪 per-workspace namespace（`paas-ws-stg-<hash>`，空的留著）** → 測完手動 `kubectl delete ns`。PVC 被 orphan-label（7 天 grace，pvc-cleanup cron 收）。

**⚠️ odoo shell 陷阱**：`odoo shell -c /etc/odoo/odoo.conf -d woow_stg --no-http` 的 `write()` **不自動 commit**，shell 退出即 rollback → 改資料（如取消訂閱）**必須 `env.cr.commit()`**。新 release 的 odoo binary 為 PATH 上的 `odoo`（官方 image），不再使用舊 Bitnami 路徑；選 pod 時限定 `app.kubernetes.io/instance=paas-odoo-platform-stg,app.kubernetes.io/component=server` 並指定 `-c odoo`。

**F-N8N-3 已失效（2026-07-23 實證）**：#318/#322 刪除聯動 merge 後，cloud service delete **會連帶取消 trial subscription**（stg 實測 open-design SUB-00038 delete 後 state=cancel，無孤兒）。舊記載「留 in_progress 孤兒」僅適用歷史殘留；ns 不清的部分仍然成立（見上）。

n8n 完整應用 chart = `oci://8gears.container-registry.com/library/n8n`（**公開、匿名可拉** n8n:2.0.1）。operator `_ensure_oci_login` 修後只登入自家 `registry_server`（見 [[reference_agent_runtime_respawn_mechanism]] 同 operator 發版鏈：build-operator tag→Fleet paas-app-bundle-cd `values-stg.yaml` bump image.tag→pod roll）。
