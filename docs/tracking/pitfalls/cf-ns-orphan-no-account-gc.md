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

> 對照：PVC 名義上有 grace-period 清理（`config.py` `pvc_grace_period_days`，本 repo default=7；prod 90 來自 cross-repo Fleet override，本 repo 證不到、待證），**但實測這條 GC 從上線起就壞、一次都沒生效**（見下方 §4）。且**孤兒 namespace 本身沒有 TTL**，PVC grace 即使能動也救不了「ns 被整個留下」的情況。

### 4. PVC grace-period GC 表面存在、實際從未生效（orphaned-at label 值非法）

teardown 後 operator 想替殘留的 StatefulSet data PVC 貼 `paas.woowtech.io/orphaned-at` 標籤、供 pvc-cleanup cron 之後依 grace-period 回收（`extra/paas-operator/src/api/releases.py:205`），但標籤值用了**帶冒號的 RFC3339 時間戳**：

```python
now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # → "2026-06-20T08:03:00Z"（含冒號）
```

冒號是**非法的 k8s label value**（regex `(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?`）→ `label_pvcs`（`helm.py:1163-1171`）把 3 個標籤放**同一條** `kubectl label --overwrite` → **整條被拒（atomic，3 個全沒貼）**，錯誤被 `helm.py:1173` + `releases.py:219` 雙重吞掉、只留一行 WARNING。

讀取側（pvc-cleanup cron Part 1，CD repo chart）`kubectl get pvc -A -l paas.woowtech.io/orphaned-at` **完全靠這 label 選 PVC** → 永不命中；Part 2 只清 `status.phase==Released` 的 PV，但 PVC 既沒被刪、PV 仍 `Bound` → 也救不了。**兩條路全漏。**

實證（2026-06-20 myodoo2 teardown e2e）：`kubectl get pvc -A -l paas.woowtech.io/orphaned-at` = **0 筆**（配 74 天前就在跑的 cron → 此機制一次沒成功過）；teardown 即時重現上述 WARNING + 留下 `data-svc-127595e1-postgresql-0`（8Gi）Bound 孤兒。**所以本 pitfall 開頭「孤兒只剩 CF/ns」是低估——postgres-backed service（Odoo/n8n/PG…）連 data PVC + 底層 Longhorn volume（計費）都一起永久漏。** 與 [[reaped-since-single-path-stamp-gc-immunity]]、CF/ns 孤兒同「GC 機制看似有、實際對某些到達路徑免疫」家族。

修法：label 值改 **epoch 秒**（colon-free、cron 可直接比較免 `date -d`），跨 **operator code（本 repo `releases.py:205`）** 與 **cronjob template（cross-repo `woow-paas-charts` chart：`charts/paas-operator/templates/pvc-cleanup-cronjob.yaml`；**`paas-app-bundle-cd` 只是 Fleet CD 引用/pin 該 chart、非模板所在**）** 兩處 lock-step；既有無標籤孤兒 PVC 需另行回填（fix 只救未來）。守線靠 **integration test**（單元 mock 測不到真 kubectl 的 label 值驗證——正是 74 天沒被發現之因）。詳見 operator issue #208（orphaned-at PVC label 值帶冒號，與 #207 同 e2e 挖出）。

> **狀態（2026-06-20，branch `fix/operator-issues`）**：operator 端已修——`releases.py` 把 `orphaned-at` 改 **epoch 秒**（抽成 `build_orphan_pvc_labels` 單一真相），並在 `helm.py:label_pvcs` 加 **`validate_label_value` fail-loud 守**（非法 label value 在 shell out 前 `raise`，把偵測從「只有真 kubectl 才暴露」拉到 unit 可攔），補 unit（`test_api`/`test_helm`）+ **integration**（`tests/integration/test_pvc_orphan_labeling.py`：真 kubectl 驗 cron selector 命中 + 真 kubectl 拒 colon 坐實根因）。**⚠️ 部署守線：cronjob（woow-paas-charts）未同步吃 epoch 前，operator epoch image 不可 deploy**——cron 現為 `ORPHANED_EPOCH=$(date -d "$ORPHANED_AT" +%s)`，對純數字 epoch 行為依 image 而定；`-z` 空值 `continue` 守衛使「寬限期內提早刪 PVC」不太可能、但不可依賴。cronjob 變更委派 maestri agent `paas-charts`（woow-paas-charts repo 另 PR + chart 版號 bump + Fleet pin）。

### 配套的清理陷阱（盤/清孤兒時會踩）

- **prod/stg 共用 CF account + zone**（實機 kubectl 比對兩 operator env 證實，見 memory `prod-stg-cf-shared-account-zone`）→ 列 CF 資源會同時撈到 prod/客戶/個人。判 env 要靠 **DNS content 指向哪條 shared tunnel**（stg=`55953bfa` / prod=`328c3a28`）；幸好 prod 平台 Odoo 沒裝 CloudService/SA/SmartHome model（DB 表 `does not exist`），所以 `paas-cs/sa/sm` 資源都是 stg。
- **刪一條 marketplace route 要 DNS + namespace + shared-tunnel ingress rule 三處一起清**：刪 DNS record ≠ 刪 namespace ≠ 刪 tunnel ingress rule。本次刪完 DNS + ns 後，shared tunnel 的 ingress config 仍殘留 30 條指向死 service 的 rule（ingress 無單刪 API，需 GET→filter→PUT 整份 config）。
- **CF list API `per_page=100` 會截斷**：第一次 zone CNAME dump 漏了 15 筆 `svc-*-mcp` 孤兒（在 100 筆之外），全 zone（`per_page=500`）才現形。
- **不能信 Odoo DB 白名單判 Access policy 孤兒**：`sync_access_app` 每次 resync 重建一組 reusable policy + 刪舊，造成 DB `cf_policy_id` 與 CF app 實際引用漂移（實測 SA6 在 CF 有 3 組 test1/2/3 policy、三方對不上）。判 policy 孤兒要用 **CF app 實際引用**（`GET /access/apps/{id}/policies`）為準，靠 CF `409 in-use` guard 兜底——本次正是它擋下 3 個被誤判為孤兒的 active policy。
- **CloudService 的 mcp sidecar route 是 operator 既存的 create/delete 命名不對稱孤兒源**（與本 repo 的 CloudService teardown subdomain fix **解耦**，非該 fix 引入）：deploy 時 mcp route 用 `generate_subdomain(ns,release)+"-mcp"`（`extra/paas-operator/src/api/releases.py:685`），delete 時用 `(subdomain or generate_subdomain)+"-mcp"`（`:233`）。controller `_delete_service`（`src/controllers/paas.py:2332`，**develop 既存**）與對齊後的 `_do_cleanup` 都傳 `subdomain=self.subdomain`（`paas-cs-…`）→ delete 算出 `paas-cs-…-mcp` ≠ deploy 的 `svc-…-{nssuffix}-mcp` → **有開 sidecar 的 service 刪除時漏 mcp CF route**。無 sidecar 的 service（如本次 myodoo2）不受影響。快修在 operator（`:233` 改回 `generate_subdomain(ns,name)+"-mcp"`，**issue #207**），盤 mcp 孤兒須同時涵蓋 `svc-…-mcp` 與 `paas-cs-…-mcp` 兩種命名。**已修（branch `fix/operator-issues`：`:233` 對齊 deploy `:685` 的 `generate_subdomain` scheme A + `test_api.py` 釘住 mcp 用 scheme A／主 route 仍用 caller subdomain 的對稱性）**。註：選快修而非「deploy 改吃 `expose_config.subdomain`」清修——因對外 MCP URL 實際走 cluster 內部位址（`_compute_sidecar_url_hint`，CF 不 route sidecar port）、`get_mcp_endpoint_url` 為 dead code，清修會把現存 scheme-A route 全變孤兒且零收益。現存 `paas-cs-…-mcp` 歷史孤兒仍須帳號級反向 GC（見 §2）。

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

### 5. 修好 PVC orphaned-at label，讓 grace-period GC 真的能動

把 `releases.py:205` 的 `orphaned-at` 標籤值從帶冒號 RFC3339 改成 **epoch 秒**（合法 label value）；cronjob template（cross-repo **`woow-paas-charts`** chart `charts/paas-operator/templates/pvc-cleanup-cronjob.yaml`，**非 `paas-app-bundle-cd`**——後者只是 Fleet CD pin 該 chart）同步把 `ORPHANED_EPOCH=$(date -d "$ORPHANED_AT" +%s ...)` 改成直接吃 epoch（`ORPHANED_EPOCH="$ORPHANED_AT"` + 全數字守衛）兩處 lock-step。**部署守線：兩處未一起上線前 operator epoch image 不可 deploy**（cron 對純數字 `date -d` 行為依 image 而定；`-z` 守衛使提早刪不太可能但不可依賴）。補 operator **integration** test：teardown 後真 kubectl `get pvc -l orphaned-at` 須命中 + 真 kubectl 拒 colon 坐實根因（單元 mock 測不到 → 這是 74 天沒被發現的根因）；另在 `label_pvcs` 加 `validate_label_value` fail-loud 守，讓同類非法 label value 在 unit 層即可攔。fix 只救未來，既有無標籤孤兒 PVC 須一次性回填（反查「paas-ws-* ns 內無對應 live helm release 的 PVC」直接刪，不靠 label）。詳見 §4 與 operator issue #208。

## 相關

- [[direct-unlink-bypass-cleanup-hook]] — Odoo 直接 unlink 繞過 cleanup hook（SecurityAccess 的 fail-loud guard pattern，SmartHome 缺的就是這個）
- [[operator-cf-access-delete-202-mock-contract]] — CF Access delete 回 202、先脫鉤再刪 policy
- [[reaped-since-single-path-stamp-gc-immunity]] — 同「孤兒 GC 免疫」家族：下游 gate 只在單一路徑 stamp → 其他路徑到達同狀態的 row 永久免疫回收
- operator issue #208（orphaned-at PVC label 值帶冒號，§4）+ #207（mcp sidecar route 命名不對稱）— 同屬 operator teardown 孤兒 bug、同由 2026-06-20 myodoo2 teardown e2e 挖出
- memory `prod-stg-cf-shared-account-zone`、`stg-cf-ns-orphan-cleanup` — 拓樸實證與本次清理判定法/成果
