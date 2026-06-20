# Pitfall: CloudService/SmartHome 的 Cloudflare + namespace 孤兒會無限累積——清理是 record-driven、無帳號級反向 GC、SmartHome 連 fail-loud guard 都沒有

> 來源：stg Cloudflare/namespace 孤兒清理（2026-06-18~19，唯讀調查 → 逐批清理）。在共用同一 woow-k3s cluster、共用同一 Cloudflare account/zone 的 prod/stg 環境上，盤點 stg 累積的孤兒,清出 **3 reusable policy + 28 DNS + 3 dedicated tunnel + 30 條 shared-tunnel ingress rule + 3 CloudService namespace（含 ~5 Odoo + 1 n8n + 9 PVC）**。這些都是「平台 Odoo 已不再追蹤、但 K8s/Cloudflare 端還活著」的孤兒，且**沒有任何機制會自動清它們**——最舊的 tunnel 建於 2026-03-04、孤兒 ns 跑了 1-2 個月。

## Symptom

- `kubectl get ns` 看到一票 `paas-ws-stg-*` 租戶 namespace（甚至更舊的 `paas-ws-1/5/253`，prefix 與 prod 撞、用 id 命名），裡面有 Running 的 Odoo/PostgreSQL/n8n + PVC，但 **stg Odoo `woow_paas_platform_cloud_service` 已查無對應記錄**。
- Cloudflare account 上一堆 `paas-sm-{hash}` dedicated tunnel（down/inactive）、`paas-cs-*`/`svc-*-mcp` DNS、shared tunnel 的 ingress rule，**後端 service/namespace 早已不存在**。
- 這些孤兒 namespace 上**沒有 TTL / delete-at annotation**（`kubectl get ns -o yaml` 確認），不會自動清。
- 唯一的清理 cron（`subscription_lifecycle_cron`）**完全掃不到它們**——它只遍歷還存在的 Odoo record。
- 全部單元/整合測試（mock client）**綠**——測不到「record 已消失、遠端資源還在」這個狀態。

## Why it happens

### 1. 清理是 record-driven（靠 stored-id），任何「繞過 Odoo unlink」的刪除都製造永久孤兒

operator 的 Cloudflare/helm 清理全是 **id-addressed**：`delete_tunnel(tunnel_id)`（`extra/paas-operator/src/services/cloudflare.py:1020`）、`_delete_access_app(app_id)`、`delete_reusable_policy(policy_id)`、helm uninstall by release——**都必須先知道 stored id**。而這些 id 只存在 Odoo DB（`security_access.tunnel_id` / `security_access_route.{dns_record_id,access_app_id}` / `security_access_access_policy.cf_policy_id` / `smart_home.tunnel_id` / `cloud_service.helm_{namespace,release_name}`）。

唯一的清理驅動是 `src/models/subscription_lifecycle_cron.py:118` 的 `_do_cleanup`——它**遍歷還存在的 Odoo record**。所以一旦 record 先消失（`k8s-dev-sandbox destroy` 直接刪 namespace + Odoo DB、stg DB 重置、手動 `kubectl delete`、或更早的 Odoo 直接刪 record），stored id 隨之蒸發 → 遠端資源（CF tunnel/DNS/Access、helm release、PVC）**沒有 DB 反參照、也沒有 cron 會掃到 → 永久孤兒**。

`SecurityAccess.unlink`（`src/models/security_access.py:488-512`）有 fail-loud guard 擋「Odoo 端直接刪 record 但 CF 沒清乾淨」（見 [[direct-unlink-bypass-cleanup-hook]]），但它**對「整個 DB / namespace 被外力一起銷毀」無能為力**——那條路徑根本不經過 Odoo 的 ORM。

### 2. SmartHome 連 fail-loud guard 都沒有——正常刪除路徑就會漏 tunnel

`SecurityAccess` 至少在 Odoo 端刪除時會 fail-loud（`security_access.py:497-507`：cleanup 後 `tunnel_id` 還在就 `raise UserError`，保留 DB row + CF tunnel 供 retry）。**但 `SmartHome` 完全沒有這層保護**：

```python
# src/models/smart_home.py:182-202  action_delete
if self.tunnel_id:
    self.write({'state': 'deleting'})
    try:
        client.delete_tunnel(self.tunnel_id)
        ...
    except PaaSOperatorError as e:
        _logger.warning("Failed to delete tunnel %s ...", self.tunnel_id, ...)  # 只 warning
self.unlink()   # ← 無條件 unlink，連 retry 的 DB 參照都不留
```

`delete_tunnel` 一次暫態失敗（operator 重啟 / CF 429 / 網路抖動）就只記 warning，緊接著 `self.unlink()` **無條件**刪掉 DB row → `tunnel_id` 隨之蒸發 → CF tunnel 變永久孤兒。**這不需要 destroy 繞過，正常刪除路徑就會漏。** 本次清出的 3 個 dedicated tunnel 孤兒全是 `paas-sm-*`。

> 精確範圍（別擴大化）：SmartHome **唯一**會漏 tunnel 的是 `action_delete`（`:202` 無條件 `unlink()`）。`_do_cleanup`（`:367` 失敗後 `write({'state':'deleting'})`）、`_do_suspend`/`_do_resume`（`:296`/`:335` 失敗後 `write({'state':'error'})` + `return`）雖也只 `_logger.warning` 不 raise，但**都保留 record（不 unlink）** → `tunnel_id` 還在、**不漏 tunnel**。所以缺的是 `action_delete` 一處的 fail-loud guard，不是「所有路徑都漏」。

### 3. 沒有帳號級反向對帳器（reconciler）

`cloudflare.py` 只有「給定一個已知 tunnel_id，對帳它名下的 DNS」（`reconcile_tunnel_dns`，`:439`）；**沒有** list-all-tunnels / list-all-access-apps / list-all-access-policies。所以無法做「列 CF account 上所有平台資源 vs Odoo 現存 stored-id 取差集」這種帳號級孤兒掃描。孤兒一旦失去 DB 反參照，就再也沒有任何 code path 能發現它。

> 對照：PVC 至少有 grace-period 清理（`config.py` `pvc_grace_period_days`，本 repo default=7；prod 90 來自 cross-repo Fleet override，本 repo 證不到、待證）；但**孤兒 namespace 本身沒有 TTL**，且 PVC grace 也救不了「ns 被整個留下」的情況。

### 配套的清理陷阱（盤/清孤兒時會踩）

- **prod/stg 共用 CF account + zone**（實機 kubectl 比對兩 operator env 證實，見 memory `prod-stg-cf-shared-account-zone`）→ 列 CF 資源會同時撈到 prod/客戶/個人。判 env 要靠 **DNS content 指向哪條 shared tunnel**（stg=`55953bfa` / prod=`328c3a28`）；幸好 prod 平台 Odoo 沒裝 CloudService/SA/SmartHome model（DB 表 `does not exist`），所以 `paas-cs/sa/sm` 資源都是 stg。
- **刪一條 marketplace route 要 DNS + namespace + shared-tunnel ingress rule 三處一起清**：刪 DNS record ≠ 刪 namespace ≠ 刪 tunnel ingress rule。本次刪完 DNS + ns 後，shared tunnel 的 ingress config 仍殘留 30 條指向死 service 的 rule（ingress 無單刪 API，需 GET→filter→PUT 整份 config）。
- **CF list API `per_page=100` 會截斷**：第一次 zone CNAME dump 漏了 15 筆 `svc-*-mcp` 孤兒（在 100 筆之外），全 zone（`per_page=500`）才現形。
- **不能信 Odoo DB 白名單判 Access policy 孤兒**：`sync_access_app` 每次 resync 重建一組 reusable policy + 刪舊，造成 DB `cf_policy_id` 與 CF app 實際引用漂移（實測 SA6 在 CF 有 3 組 test1/2/3 policy、三方對不上）。判 policy 孤兒要用 **CF app 實際引用**（`GET /access/apps/{id}/policies`）為準，靠 CF `409 in-use` guard 兜底——本次正是它擋下 3 個被誤判為孤兒的 active policy。
- **CloudService 的 mcp sidecar route 是 operator 既存的 create/delete 命名不對稱孤兒源**（與本 repo 的 CloudService teardown subdomain fix **解耦**，非該 fix 引入）：deploy 時 mcp route 用 `generate_subdomain(ns,release)+"-mcp"`（`extra/paas-operator/src/api/releases.py:685`），delete 時用 `(subdomain or generate_subdomain)+"-mcp"`（`:233`）。controller `_delete_service`（`src/controllers/paas.py:2332`，**develop 既存**）與對齊後的 `_do_cleanup` 都傳 `subdomain=self.subdomain`（`paas-cs-…`）→ delete 算出 `paas-cs-…-mcp` ≠ deploy 的 `svc-…-{nssuffix}-mcp` → **有開 sidecar 的 service 刪除時漏 mcp CF route**。無 sidecar 的 service（如本次 myodoo2）不受影響。快修在 operator（`:233` 改回 `generate_subdomain(ns,name)+"-mcp"`，**issue #207**），盤 mcp 孤兒須同時涵蓋 `svc-…-mcp` 與 `paas-cs-…-mcp` 兩種命名。

## How to avoid

### 1. SmartHome 補 fail-loud guard，對齊 SecurityAccess

`SmartHome.action_delete` 的 `delete_tunnel` 失敗時，**保留 `tunnel_id` 不要 unlink、raise（或至少不清 record）讓 cron/使用者 retry**（`_do_cleanup`/suspend/resume 失敗已是「只 write state、不 unlink」→ 不漏 tunnel；要補的是 `action_delete:202` 那一處無條件 `unlink()`），對齊 `SecurityAccess.unlink` 的契約（[[direct-unlink-bypass-cleanup-hook]] 的 cloud-service-unlink-override-cleanup pattern）。「遠端資源不可比 record 先消失」這條不變式應該在每個建立遠端資源的 model 上對稱成立，不能只有 SecurityAccess 有。

### 2. 加帳號級 Cloudflare reconciler（反向 GC）

operator 新增 list-all-tunnels（`GET /accounts/{aid}/cfd_tunnel`，過濾 name 前綴 `paas-sa-`/`paas-sm-`/`paas-cs-`）、list-all-access-apps、list-all-access-policies，配合「拉 Odoo 現存 stored-id 集合取差集」產 **dry-run 孤兒清單**。env 分流以 DNS content=shared tunnel_id 為準；dedicated tunnel/Access 靠對照仍存在的 prod DB 取差集（共用 account 的限制）。理想上 CF 物件建立時就 stamp 結構化 owner/env tag（tunnel metadata / Access tags / DNS comment 加 `env=stg;sa=<canonical>`），讓孤兒能自證歸屬、免去靠對照另一環境 DB。

### 3. 租戶 namespace 上 TTL / owner label + 對帳，或在銷毀流程加前置 teardown

- 租戶 ns 蓋 `owner`/`reference-id` annotation + TTL，配對帳 cron 掃「Odoo 已無對應 record 的 ns」。
- `k8s-dev-sandbox destroy` / stg DB 重置流程加「先觸發 Odoo 端 CF/helm teardown 再刪 ns/DB」的前置步驟，避免繞過 unlink guard。

### 4. 清理操作紀律

清 marketplace/CloudService route = **DNS + ns + shared-tunnel ingress rule 三處一起**；ingress 用 GET→filter→PUT（先 dry-run 印 keep/remove）；CF list 一律 `per_page=500`/分頁；刪前先 GET 驗 content/name/connections，靠 CF guard（policy `409 in-use`、Access delete `202`、見 [[operator-cf-access-delete-202-mock-contract]]）兜底。

## 相關

- [[direct-unlink-bypass-cleanup-hook]] — Odoo 直接 unlink 繞過 cleanup hook（SecurityAccess 的 fail-loud guard pattern，SmartHome 缺的就是這個）
- [[operator-cf-access-delete-202-mock-contract]] — CF Access delete 回 202、先脫鉤再刪 policy
- [[reaped-since-single-path-stamp-gc-immunity]] — 同「孤兒 GC 免疫」家族：下游 gate 只在單一路徑 stamp → 其他路徑到達同狀態的 row 永久免疫回收
- memory `prod-stg-cf-shared-account-zone`、`stg-cf-ns-orphan-cleanup` — 拓樸實證與本次清理判定法/成果
