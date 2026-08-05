# reference security access stg e2e

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_security_access_stg_e2e`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：SA P1 真 Cloudflare E2E recipe（JSON-RPC driver + 四路證據）與 stg domain mismatch 發現

**SA P1 stg 真 Cloudflare E2E（2026-06-07 首驗全通，image 20260606.2328 = develop@d0237ae）**

E2E driver = JSON-RPC（admin session）對 `https://stg.woowtech.io`，比 odoo shell 更貼近正式（走 controller gating + 訂閱建立）：
1. 認證：`POST /web/session/authenticate`（admin 密碼從 `dev-sandbox-stg/stg-odoo-dev-sandbox-secret` key `odoo-admin-password`，command substitution 不落地）→ cookie jar。
2. create：`POST /api/workspaces/{wid}/security-accesses` `{action:create, name, template_id:1, plan_id:11(trial)}` → canonical `paas-sa-{ws8}-{ref8}`、trial 訂閱 in_progress。
3. provision：detail endpoint `{action:provision}`（admin 有 `base.group_system` bypass 訂閱 gate；`tunnel_token` 是 groups 欄位、非 system caller 會 AccessError fail-closed by design）。
4. route：routes endpoint `{action:create, subdomain_prefix, service_url}` → 自動觸發 `PUT /api/tunnels/{id}/routes` 宣告式同步。
5. delete：detail `{action:delete}` → cleanup_cloudflare + unlink（fail-loud：tunnel 清不掉就 raise 保 DB row）。

**四路 CF 證據**（驗證不用碰 CF token）：① operator `GET /api/tunnels/{id}`（live 查 CF）② operator `GET /api/routes/availability?subdomain=X`（live 查 CF DNS，taken/free）③ `dig +short {host} A`（公開解析到 CF edge = record 存在；proxied 記錄看不到 CNAME 目標）④ operator pod log（httpx 對 api.cloudflare.com 的每個 call + 狀態碼）。operator 訪問：`kubectl --context woow-k3s port-forward -n paas-stg-system svc/paas-operator-stg 18000:80`，API key 在 `paas-stg-system/paas-operator-stg-secret` key `api-key`。

**主發現 — stg domain lock-step contract 缺口（待修）**：Odoo `paas_domain=stg.woowtech.io` vs stg operator `CLOUDFLARE_DOMAIN=woowtech.io`。`create_tunnel` 收全名 hostname（建 `.stg.woowtech.io` DNS），但 `PUT /routes` 只收 prefix、operator 接自己 domain → 同 provision 內 reconcile 把 `.stg` record 刪掉、改建 `.woowtech.io`。淨效果：**Odoo 顯示的 primary_hostname（.stg.woowtech.io）懸空，實際 DNS/ingress 全在 *.woowtech.io**（2026-06-07 實證：`.stg` 建後 2 秒被 reconcile 刪）。隱含契約 = 兩值必須相等；stg 修法二選一：operator values（paas-app-bundle-cd）把 `CLOUDFLARE_DOMAIN` 改 `stg.woowtech.io`（zone 不變仍 woowtech.io zone id），或 stg Odoo `paas_domain` 改 woowtech.io（但會動 cloud services 顯示）。prod 兩值同為 woowtech.io 無此問題。

**次要觀察**：① CF tunnel 是 soft-delete——DELETE cascade 200 後 GET 仍回 200 `inactive`（operator 未映射 `deleted_at`；驗清理以 DELETE 200 + DNS availability=free 為權威）② `action_delete` 不關訂閱（與 smart_home 同 pattern，刪實例後訂閱續 in_progress 到期；產品決策待定，E2E 後手動 close）③ route `to_dict` 不含 `dns_record_id`（DB 有存）④ tunnel status=`inactive` 是常態——沒人拿 token 跑 cloudflared connector。

相關：[[deploy-stg-gotchas]]、[[agent-runtime-stg-verify]]
