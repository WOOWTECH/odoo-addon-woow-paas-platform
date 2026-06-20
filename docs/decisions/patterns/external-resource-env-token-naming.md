# Pattern: 跨環境共用外部帳號的平台資源命名 — 在「單一命名根」注入顯式 env token

> 適用：平台代為在**共用的外部系統**（prod/stg 共用同一 Cloudflare account/zone/domain、共用同一 cluster;未來任何共用帳號的雲端 DNS / SaaS）產生資源時,該資源的命名/標籤**必須帶一個能一眼辨識 prod/stg 的 env token**。否則在共用帳號裡,這些資源無欄位字面記錄環境 → 孤兒清理 / 稽核時無法自證歸屬(見 [`cf-ns-orphan-no-account-gc`](../../tracking/pitfalls/cf-ns-orphan-no-account-gc.md))。實機背景:prod/stg 只靠 tunnel id 與 K8s `namespace_prefix`(prod `paas-ws-` / stg `paas-ws-stg-`)分環境;CF 上的 `paas-cs/sa/sm-{hash}` 命名前綴是 `src/services/naming.py` 寫死字面量,**完全沒有 env 注入路徑** —— 這正是本 pattern 要補的缺口。

## When to use

三者成立時:
1. 資源由**平台代為產生**(非使用者自填),且會落到 prod/stg **共用**的外部系統。
2. 該外部系統**沒有**帳號級「依環境過濾」的原生機制(共用 account → list 出來 prod/stg 混在一起)。
3. 你正在新增/改一條「平台 → 外部系統」的命名(tunnel name、DNS、Access app/policy、雲端資源名/tag…)。

**不適用**:使用者第一手自填的對外識別字(如 SA route 的 `subdomain_prefix`,全域扁平 first-come-first-served)。那種 env 區隔要靠 **per-env domain**(`paas_domain` config_parameter),不該由平台注入 token 改使用者選的 hostname。

## 守則

1. **注入在「平台產生命名的單一根」,不要逐個外部建立點各自 stamp。** 本 repo 全部 CF 命名都衍生自 Odoo 端 `canonical_name`(`src/services/naming.py` 的 `make_*_subdomain`):tunnel name = canonical_name、DNS comment = canonical_name、Access app/policy name 也以它為根。在 naming.py 一處加 env 參數 → 同時覆蓋全部下游,且 Odoo 同時掌握 create 與 delete 兩側(canonical 是 stored 欄位、delete 直接讀同一筆、不重算)→ **雙向對稱免費取得**。
2. **env token 的來源用顯式 config/env,不要從別的字串隱式推導。** Odoo 端用 `namespace_prefix`(已帶 stg 慣例、settings help 明示 `paas-ws-stg-`)或新增專用 env config_parameter。**禁止**在 operator 端寫 `namespace_prefix.removeprefix('paas-ws-')` 靠「prod = 空字串」這種隱式約定 —— operator 全域無顯式 env-var(`DEPLOY_ENV`/`PAAS_ENV` 皆不存在),要補強須先在 cross-repo `paas-app-bundle-cd` Fleet values 新增顯式 `PAAS_DEPLOY_ENV` 注入 operator Deployment env。
3. **只注入「純 traceability 標籤」欄位**(name / comment / tag)。這些**不參與反查**(反查靠 stored id 與 CNAME `content={tunnel_id}.cfargotunnel.com`,見 `cloudflare.py:399/439/1020`、Access 靠 `existing_app_id`/`existing_policy_ids`)→ 注入零破壞。
4. **凡靠「重算 name 反推」做刪除/反查的路徑,改命名必須 create↔delete 雙向對稱。** 本 repo 唯一這種路徑是 CloudService legacy shared-tunnel 的 `cloudflare.py:708 generate_subdomain`(create 與 delete `releases.py:224/233/685/744` 都呼叫它)→ 改**函式本身**即雙向一致,切勿只在單側硬編 env token,否則 delete 算出的 name 與 create 不符 → 永久孤兒。
5. **identity anchor(`canonical_name` 等 immutable 值)的 env token 只能在 create 首算注入,事後不可改**(受 [[su-env-exempt-guard-false-green]] 的 create+write 雙向 guard 保護;測試別加 `env.su` 豁免否則假綠)。

## 本 repo 可行性表(改 CF 命名前對照)

| 外部資源 | 命名根在 | 能注入? | 機制 | 反查破壞? |
|---|---|---|---|---|
| CF Tunnel name(SA dedicated) | Odoo `canonical_name`(透傳至 `cloudflare.py:742`) | ✅ | naming.py 注入即生效 | 否(反查靠 tunnel_id + CNAME content) |
| CF DNS comment | = canonical_name(`security_access.py:547`) | ✅ | 隨 canonical_name 帶 | 否(反查靠 CNAME content) |
| CF Access app name / `canonical_name` 欄位 | Odoo canonical_name | ✅ | 隨 canonical_name 帶 | 否(反查靠 existing_app_id) |
| CF reusable policy name | operator 加工 `f'{canonical}: {name}'`(`cloudflare.py:1322`) | ✅ | canonical 帶 env 即自動前綴 | 否(反查靠 existing_policy_ids) |
| CloudService subdomain(legacy shared-tunnel) | create=Odoo `make_service_subdomain` / delete=operator `generate_subdomain`(兩條不同源!) | ⚠️ | 須統一兩條命名鏈 | **是 — 唯一靠重算 name 反推**,必雙向對稱 |
| SA route `subdomain_prefix`(使用者自填 hostname) | 使用者 | ❌ | 不適用,env 靠 `paas_domain` | — |
| K8s namespace(對照組,**已帶 env**) | Odoo `make_namespace`(prefix 來自 config_parameter) | ✅(已做) | 部署設 `paas-ws-stg-` | — |

## 注入點:為什麼是 Odoo `naming.py`,不是 operator prefix

直覺上「operator 是唯一 per-env 元件,所以在 operator 端注入」—— **方向(要帶 env token)對,但注入點反了**。K8s ns 帶 env 是因為 `make_namespace` 吃 `ns_prefix`(config_parameter);但 **CF 命名走的是 `canonical_name` 這條完全沒接 prefix 的另一根**。所以「比照 k8s namespace」不是把同一個 prefix 接過去,而是「在 CF 的命名鏈裡比照 ns 一樣注入一個 env token」,而最乾淨的注入點是 **Odoo `naming.py` 單一根**:(a) 一改全到位(全部 CF 命名同源)、(b) create/delete 天然對稱(canonical 是 stored 值)、(c) env 來源顯式(config_parameter)。operator 端注入是「次佳補強」,且需先補顯式 `PAAS_DEPLOY_ENV`(別從 namespace_prefix 推)。

## Caveats

- **既有 record 不回填**:canonical_name immutable,注入只對新 record 生效 → DB 內新舊命名混用。**孤兒清理 / 帳號級 reconciler 必須同時認得「有 env token」與「無 env token」兩種格式**,否則舊 record 反而變辨識不到的孤兒。
- **本 pattern 只解「標籤可辨識」,不解「無帳號級反向 GC」**。env token 讓人/工具一眼分 env,但 CF 仍無 list-all 反查、租戶 ns 仍無 TTL;繞過 Odoo unlink 的刪除照樣產生孤兒(見 [`cf-ns-orphan-no-account-gc`](../../tracking/pitfalls/cf-ns-orphan-no-account-gc.md)),只是較好辨認。兩者要一起補。

## 相關

- [[two-stage-remote-provision-idempotency]] — 同「平台建外部資源」家族(provision 冪等 / stored-id guard)
- [[su-env-exempt-guard-false-green]] — canonical_name 是它保護的 immutable anchor;注入 env token 須在 create 首算、測試別加 su 豁免
- pitfall [`cf-ns-orphan-no-account-gc`](../../tracking/pitfalls/cf-ns-orphan-no-account-gc.md) — 沒有 env token 時孤兒無法自證歸屬的實證;memory `prod-stg-cf-shared-account-zone`、`stg-cf-ns-orphan-cleanup`
