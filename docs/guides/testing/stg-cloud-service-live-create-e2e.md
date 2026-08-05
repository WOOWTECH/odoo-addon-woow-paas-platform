# reference stg cloud service live create e2e

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_stg_cloud_service_live_create_e2e`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：stg 上實際建立 cloud service（如 n8n）驗證部署的 E2E recipe + odoo shell commit 陷阱 + n8n 8gears chart 匿名拉取

在 dev-sandbox-stg（DB `woow_stg`、admin/admin）實際建立 cloud service 驗證完整部署的 recipe：

**存取**：`kubectl --context woow-k3s port-forward -n dev-sandbox-stg svc/stg-odoo-dev-sandbox-nginx 8091:80`

**認證**：`POST /web/session/authenticate` body `{"jsonrpc":"2.0","method":"call","params":{"db":"woow_stg","login":"admin","password":"admin"}}`（curl `-c cookiejar`）。admin(uid2) 對 ws 1(`test1`)/2/7 有 owner。

**建立**：`POST /api/workspaces/<ws_id>/services` body params `{"action":"create","template_id":<id>,"name":"...","plan_id":<id>,"values":{"_init.owner_email":"...","_init.owner_password":"Test1234abc"}}`。n8n（template slug `n8n`=`cloud_app_n8n`，id 2）需 `_init.owner_email/password`（>=8 字、含大寫、含數字），plan 1/2 皆 7 天 trial（不立即扣款）。成功→state deploying→running，operator 跑 n8n owner setup + 建 API key。

**刪除/teardown**：`POST /api/workspaces/<ws>/services/<sid>` `{"action":"delete"}`。operator 會刪 CF route+DNS（主 subdomain + `-mcp` sidecar）+ helm uninstall + MCP svc。**但不刪 per-workspace namespace（`paas-ws-stg-<hash>`，空的留著）** → 測完手動 `kubectl delete ns`。PVC 被 orphan-label（7 天 grace，pvc-cleanup cron 收）。

**⚠️ odoo shell 陷阱**：`odoo shell -c /etc/odoo/odoo.conf -d woow_stg --no-http` 的 `write()` **不自動 commit**，shell 退出即 rollback → 改資料（如取消訂閱）**必須 `env.cr.commit()`**。odoo binary 在 `/opt/bitnami/odoo/bin/odoo`（不在 PATH）。

**F-N8N-3 已失效（2026-07-23 實證）**：#318/#322 刪除聯動 merge 後，cloud service delete **會連帶取消 trial subscription**（stg 實測 open-design SUB-00038 delete 後 state=cancel，無孤兒）。舊記載「留 in_progress 孤兒」僅適用歷史殘留；ns 不清的部分仍然成立（見上）。

n8n 完整應用 chart = `oci://8gears.container-registry.com/library/n8n`（**公開、匿名可拉** n8n:2.0.1）。operator `_ensure_oci_login` 修後只登入自家 `registry_server`（見 [[reference_agent_runtime_respawn_mechanism]] 同 operator 發版鏈：build-operator tag→Fleet paas-app-bundle-cd `values-stg.yaml` bump image.tag→pod roll）。
