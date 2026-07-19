---
title: prod / stg 資源辨識性與 Cloudflare 孤兒清理調查報告
category: tracking
created: 2026-06-21
updated: 2026-06-21
related:
  - docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md
  - docs/decisions/patterns/external-resource-env-token-naming.md
status: archived
archived-from: docs/tracking/cf-ns-prod-stg-audit-report.md
archived: 2026-07-19
archived-reason: "一次性 CF 可行性/稽核報告；F-ENV 後續在 tracking/followups"
---

# prod / stg 資源辨識性與 Cloudflare 孤兒清理調查報告

> 調查性質:**唯讀(不動 code)**。範圍:同一 `woow-k3s` cluster 上 prod / stg 共存時,所有產生物(K8s namespace + Cloudflare API 建出的 tunnel / DNS / Access)是否可區分環境、stg 刪除後孤兒能否找回。
> 方法:6 面向平行調查 + 24 條關鍵結論 adversarial 驗證 + 主線親自核對 `cloudflare.py` / `naming.py` / `config.py` / `helm.py` / pitfall doc。

---

## Hero KPIs(讀者最先想知道的數字 / 結論)

- **CF 可辨識 env 的結構化欄位數:0** — tunnel / DNS / Access 的 name、comment、tag 一律 hash 格式,零 env 標記。
- **唯一 env 線索:1 條(隱含)** — DNS 的 `content` 指向哪條 shared tunnel(stg `55953bfa` / prod `328c3a28`)。
- **帳號級反向 GC code path:0** — operator 無 list-all-tunnels / apps / policies,孤兒失去 stored id 後無法被任何 code 發現。
- **可靠的環境分界:namespace 前綴** — prod `paas-ws-` vs stg `paas-ws-stg-`(對新資料可靠,歷史 id 命名 ns 例外)。

---

## 結論速覽(直接回答三問)

| 問題 | 答案(verdict) |
|---|---|
| **問題1**:所有產生物(ns + CF)可否區分 prod / stg? | **部分可靠,無單一權威欄位**。K8s namespace 靠名稱前綴可靠區分(`paas-ws-` vs `paas-ws-stg-`,唯歷史 id 命名 ns 例外);**Cloudflare 物件全部無法靠自身 name / comment / tag 區分 env**,唯一隱含線索是 DNS `content` 指哪條 shared tunnel,且只對走 shared tunnel 的 CloudService route 有效。dedicated tunnel(SA / SmartHome)連這條線索都沒有,只能靠帶外事實「prod 平台 Odoo 未裝 CloudService / SA / SmartHome model → 所有 `paas-cs/sa/sm` 資源都屬 stg」反推。 |
| **問題2**:stg 刪除後 CF 孤兒會不會產生、能否找到並刪除? | **會變永久孤兒;能找到並刪除,但 100% 靠手動帶外操作,無任何自動機制**。`dev-sandbox destroy` 走 `helm uninstall + kubectl delete ns`,完全不經 Odoo unlink → stored id 連同 Odoo DB 一起蒸發;operator 又無帳號級 list-all 反向 GC → 失去 DB 反參照的 CF 資源無任何 code path 能再發現。 |
| **額外**:CF API 有沒有 description / name / comment / tag 可辨識? | tunnel **只有 `name`**(無 description / metadata 欄位);DNS 有 `name` / `content` / **optional `comment`**(comment 寫 canonical name 作 traceability,非 env tag);**Access app payload 連 `tags` / `metadata` 欄位都沒有**;Access policy 只有 `name`(`{canonical_name}: {policy_name}`)。 |

---

## 可辨識欄位對照表(逐物件 — 全部依據已親自核對)

| 物件類型 | 帶的辨識欄位 | 值格式 | 能否分 env | 依據 |
|---|---|---|---|---|
| **k8s-namespace (CloudService)** | `name` | `{prefix}{md5(slug)[:8]}` → prod `paas-ws-<hash8>` / stg `paas-ws-stg-<hash8>` | **implicit(前綴)** | `src/services/naming.py:14-16` |
| **k8s-namespace (agent-runtime)** | `name` | `{prefix}pa-{project_hash}` → prod `paas-ws-pa-<hash>` / stg `paas-ws-stg-pa-<hash>` | **implicit(前綴)** | `extra/paas-operator/src/api/agent_runtimes.py:51-60` |
| **k8s-namespace (labels)** | `labels` | 唯一 label:`paas-managed: "true"` | **no** | `extra/paas-operator/src/services/helm.py:683-684` |
| **k8s-namespace (annotations)** | `annotations` | **無**(無 TTL / delete-at / env / owner) | **no** | `helm.py:655+`;`cf-ns-orphan-no-account-gc.md:9` |
| **k8s-namespace (dev-sandbox)** | `name` | `dev-sandbox-{slug}`(如 `dev-sandbox-stg`,woowprd plugin chart 建,非 operator) | **implicit(slug)** | `.claude/skills/deploy-stg/SKILL.md` |
| **cf-tunnel** | `name` | SA `paas-sa-{ws_hash8}-{res_hash8}`、SmartHome `paas-sm-…`、shared tunnel 為固定 id | **no** | `cloudflare.py:756-758`;`naming.py:29-31` |
| **cf-tunnel** | `tunnel_secret` | base64(隨機 32 bytes),與辨識無關 | **no** | `cloudflare.py:754-758` |
| **cf-dns** | `name`(FQDN) | `{subdomain}.woowtech.io`(prod / stg 共用同 base domain) | **no** | `cloudflare.py:184,253` |
| **cf-dns** | **`content`(CNAME target)** | `{tunnel_id}.cfargotunnel.com` → 指 stg `55953bfa` / prod `328c3a28` | **implicit(唯一可用 CF 端線索)** | `cloudflare.py:82-85,200,254` |
| **cf-dns** | `comment` | optional;caller 傳 `canonical_name`(`paas-sa-…`)作 traceability,**非 env tag**;legacy `create_dns_record` 不帶 comment | **no** | `cloudflare.py:286-287,238-239` |
| **cf-dns** | `type`/`proxied`/`ttl` | 恆 CNAME / 恆 True / 恆 1(Auto) | **no** | `cloudflare.py:197-203,279-285` |
| **cf-access-app** | `name` | `app_name`(caller 傳,= route.subdomain_prefix 或退回 canonical_name) | **no** | `cloudflare.py:1335` |
| **cf-access-app** | `domain`/`destinations` | `primary_hostname` / `[{type:public, uri:protected_hostname}]`,皆 FQDN(prod / stg 同 domain → 實質不可分) | **no** | `cloudflare.py:1336-1339` |
| **cf-access-app** | **`tags`/`metadata`** | **無此欄位**(payload 不含 tags / labels / metadata) | **no** | `cloudflare.py:1334-1350` |
| **cf-access-app** | `type` | 恆 `self_hosted`;`auto_redirect_to_identity:False`、`app_launcher_visible:False` | **no** | `cloudflare.py:1336,1342-1343` |
| **cf-access-policy** | `name` | `{canonical_name}: {policy_name}`,例 `paas-sa-xxxx-yyyy: Allow Company` | **no** | `cloudflare.py:1322-1323` |
| **cf-access-policy** | `decision`/`include`/`require`/`exclude` | 由 caller;`reusable` 恆 True | **no** | `cloudflare.py:1159-1166,1425-1456` |

> **核心拓樸事實**:prod / stg **共用同一 CF account + zone + base domain `woowtech.io`**(非 `*.stg.xxx` 設計),所以 hostname 也無法分 env。`DEPLOY_ENV` / `PAAS_ENV` 全域 grep **0 命中** — operator 沒有任何顯式 env 標記。
>
> **adversarial 驗證更正**:原調查「marketplace subdomain 來自 operator `generate_subdomain`(`{release_name}-{ns_suffix}`)」一條被**判 refuted**。實際是 Odoo 端 `make_service_subdomain`(`paas-cs-{ws_hash}-{svc_hash}`,`naming.py:19-21`);`generate_subdomain` 只是 MCP sidecar leg 與 fallback 用途。不影響「subdomain 不帶 env」結論。

---

## 問題1 詳解:prod / stg 區分逐物件分析

### 1. K8s namespace — 可靠(單一例外)

- **靠什麼**:namespace **名稱前綴**。prod 用裸 `paas-ws-`、stg 用 `paas-ws-stg-`,由 operator `NAMESPACE_PREFIX` env / Odoo Settings `ir.config_parameter['woow_paas_platform.namespace_prefix']` 顯式設定(`extra/paas-operator/src/config.py:19`、`src/models/res_config_settings.py`)。prod 值在 cross-repo Fleet `values-prod.yaml` 的 `namespacePrefix`,promote 時列為「prod 專屬、不可被 stg 覆蓋」。
- **是否可靠**:**對新資料可靠,對歷史資料不可靠**。operator 對任何 ns 名強制 `startswith(namespace_prefix)`(`helm.py:79-84`),新建 ns 一定帶正確前綴。**但**:① namespace 物件上完全無 env / owner / TTL 的 label 或 annotation,唯一 label 是 `paas-managed: "true"`;② 舊孤兒 ns 曾以 id 命名(`paas-ws-1/5/253`),其前綴與 prod 裸 `paas-ws-` 直接相撞,純前綴在歷史資料上會誤判。
- **正式 pattern**:辨識性缺口已立 pattern `docs/decisions/patterns/external-resource-env-token-naming.md`,明文**禁止**從 `namespace_prefix` 隱式推 env。

### 2. cf-tunnel — 不可靠(自身欄位無 env)

- tunnel `name` 是 hash 格式(SA `paas-sa-…`、SmartHome `paas-sm-…`),不帶任何 env 字樣。
- shared tunnel 本身是 prod / stg 各一條固定 id(prod `328c3a28` / stg `55953bfa`),**這兩條 id 是已知常數可分**;但 dedicated tunnel(每 service 一條)名稱純 hash,無法從 name 判 env,只能靠帶外事實反推。

### 3. cf-dns — 部分可靠(唯一靠 content)

- 唯一可用線索是 `content`(CNAME target = `{tunnel_id}.cfargotunnel.com`)。`name` / `comment` 都是 hash,不帶 env。
- 對走 shared tunnel 的 CloudService route 可靠;對 dedicated tunnel 的 DNS,content 指專屬 tunnel id,無法直接對應 prod / stg。base domain 兩環境完全相同,**hostname 本身無法區分**。

### 4. cf-access-app / cf-access-policy — 不可靠

- app `name` = caller 傳的 app_name;policy `name` = `{canonical_name}: {policy_name}`。皆 hash 格式,無 env。Access app payload **連 tags / metadata 欄位都沒有**。
- prod / stg 共用同一 CF account + zone,Access 物件無任何 env tag。判孤兒歸屬只能看 CF app 實際引用的 domain / destinations(但 base domain 又相同)→ 實務上仍須靠「prod 平台 Odoo 無 SA model」反推。

---

## 問題2 詳解:stg 刪除後 Cloudflare 孤兒的可發現性與清理

### 為什麼會變孤兒(機制)

1. **唯一清理驅動是 record-driven**:`subscription_lifecycle_cron._do_cleanup` 只遍歷現存 Odoo record,靠 stored `helm_namespace` / `tunnel_id` / `dns_record_id` / `access_app_id` / `cf_policy_id` 清(`src/models/subscription_lifecycle_cron.py:34-39,117-118`)。所有 CF / helm 刪除都是 **id-addressed**(必須先有 stored id)。
2. **dev-sandbox destroy 完全不經 Odoo**:woowprd plugin `destroy.sh` 只跑 `helm uninstall` + `kubectl delete pvc/ns/pv`,全程無 odoo-shell / Odoo ORM / CF API 呼叫。而被刪的 `dev-sandbox-stg` 正是平台 Odoo 本身 → Odoo DB 連同所有 stored id 一起蒸發。
3. **operator 無帳號級反向 GC**:`cloudflare.py` 完全沒有 list-all-tunnels / list-all-access-apps / list-all-access-policies。唯一列舉是 DNS 的 `_list_dns_records_by_content`(靠 content filter、且 `per_page=100` 會截斷)。stored id 蒸發後,無任何 code path 能反向發現孤兒。
4. **namespace 無 owner / TTL**:唯一 label `paas-managed: true`,無反向查找鍵、無自動清。
5. **額外漏點**:SmartHome 連正常刪除路徑都會漏 tunnel(`action_delete` 在 try/except 外無條件 `unlink()`、`delete_tunnel` 失敗只 warning,`src/models/smart_home.py:188-202`),不需繞過就能產孤兒。

### 結論:能找到並刪除,但全靠手動帶外操作,無自動機制

### 具體清理操作步驟(手動帶外,唯一可行路徑)

> 前置:需 CF account 級 token(Access 寫路徑需**帳戶級** Access:Apps&Policies(Edit),非區域級)+ `kubectl --context woow-k3s` + tailscale VPN。

1. **步驟 A — 判 env 歸屬**:prod / stg 共用同一 CF account + zone,判 env 靠 **DNS record `content` 指向哪條 shared tunnel**(stg `55953bfa` / prod `328c3a28`)。帶外事實:**prod 平台 Odoo 沒裝 CloudService / SA / SmartHome model(DB 表 does not exist)→ 所有 `paas-cs-` / `paas-sa-` / `paas-sm-` 前綴資源都可斷定屬 stg**。
2. **步驟 B — 列舉 CF 端孤兒(必須分頁)**:CF list 一律用 `per_page=500` 或手動分頁。**operator 內建的 `per_page=100` 會截斷漏孤兒**(曾漏 15 筆 `svc-*-mcp`)。直接列 tunnels(過濾 name 前綴 `paas-sa-/paas-sm-/paas-cs-`)、DNS records(過濾 content 指 stg shared tunnel)、Access apps / policies。
3. **步驟 C — 判 Access policy 孤兒(不可信 Odoo DB 白名單)**:`sync_access_app` resync 會重建 policy 造成 DB↔CF 漂移,故判孤兒**須以 CF app 實際引用為準**:`GET /access/apps/{id}/policies`,靠 `409 in-use` guard 兜底。
4. **步驟 D — 清除順序(一條 route 三處一起)**:清一條 marketplace / CloudService route 必須 **DNS record + namespace + shared-tunnel ingress rule 三處同時清**。ingress rule **無單刪 API**,須 `GET tunnel config → filter 掉該 rule → PUT 回去`。刪 Access:**先脫鉤再刪舊 policy**(detach-before-delete);Access app / policy DELETE 真實成功碼是 `202 Accepted`。刪 tunnel:`delete_tunnel` 用 `cascade=true`,但其 DNS cleanup 是 best-effort、無分頁 → 需另行手動驗 DNS 已清乾淨。
5. **步驟 E — 清除 namespace 孤兒(反向比對)**:`kubectl --context woow-k3s get ns -l paas-managed=true` 撈出全部租戶 ns → 逐一比對 stg Odoo `woow_paas_platform_cloud_service` 有無對應 record;無對應者即孤兒,`kubectl delete ns paas-ws-stg-<x>`。盤 mcp 孤兒須同時涵蓋 `svc-…-mcp` 與 `paas-cs-…-mcp` 兩種命名。
6. **步驟 F — PVC 孤兒(GC 表面存在實際曾失效)**:PVC grace-period GC 曾因 `orphaned-at` label 用帶冒號 RFC3339(非法 k8s label value)→ `label_pvcs` 整批被拒 → cron 永不命中。`fix/operator-issues` 分支已改 epoch 秒 + `validate_label_value` fail-loud,但**部署守線**:cross-repo woow-paas-charts 的 cronjob 未同步前,operator epoch image 不可單獨 deploy。

---

## 風險與缺口

### 無法可靠區分 env 的物件

- **所有 Cloudflare 物件的自身 name / comment / tag**:tunnel name、DNS name / comment、Access app / policy name 全是 hash 格式,**零 env 標記**。Access app payload **連 tags / metadata 欄位都不存在**。
- **dedicated tunnel(SA / SmartHome)的 DNS**:content 指向專屬 tunnel id,無法直接對應 prod / stg。
- **hostname / base domain**:prod / stg 共用 `woowtech.io`,hostname 完全無法區分。
- **歷史用 id 命名的 namespace**(`paas-ws-1/5/253`):前綴與 prod 撞,前綴辨識失效。
- 唯一可靠的 env 信號鏈是「DNS content 指向哪條 shared tunnel」+ 帶外事實「prod 平台無 SA model」 — **沒有任何單一、結構化、自證歸屬的 env 欄位**。

### 無法被反向發現的物件

- **失去 stored id 的所有 CF 資源**:operator 無 list-all-tunnels / apps / policies,stored id 蒸發即永久失蹤。
- **繞過 Odoo unlink 刪除的租戶 namespace**:無 owner / TTL / 反向 GC。
- **best-effort cleanup 漏掉的 DNS**(per_page 截斷、cascade 失敗只 warning)。
- 此即 `docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md` pitfall 的核心。

---

## 建議

### A. 唯讀 / 手動可立即做的對帳方法(不動 code)

1. **定期帳號級對帳腳本(帶外)**:用 CF API(`per_page=500` 或分頁)列出全部 tunnel / DNS / Access app / policy,與 stg Odoo `cloud_service` / `security_access` / `smart_home` 的 stored id 取差集,dry-run 列出孤兒候選。判 env 用「DNS content = shared tunnel id」+「`paas-cs/sa/sm` 前綴 ⇒ stg」雙重條件。
2. **namespace 對帳**:`kubectl --context woow-k3s get ns -l paas-managed=true` ↔ stg Odoo record 取差集,找出無主租戶 ns。
3. **Access policy 判孤兒一律以 CF 為準**:`GET /access/apps/{id}/policies`,**不信 Odoo DB 白名單**,靠 `409 in-use` 兜底。
4. **清除遵守順序**:route 三處(DNS + ns + ingress rule)一起;Access 先脫鉤再刪;tunnel cascade 後手動驗 DNS。
5. **銷毀前置 teardown 紀律**:在跑 `k8s-dev-sandbox destroy` / stg DB 重置**前**,先透過 Odoo 觸發 CF teardown(讓 stored id 還在時就清遠端),再銷毀沙盒,避免製造新孤兒。**這是避免製造新孤兒最有效的一步。**

### B. 未來需改 code 才能根治(標註:需改 code)

1. **【需改 code】CF 物件建立時 stamp 結構化 owner / env tag**:在 create payload 加 `comment` / tag 寫入 `env=stg|prod` + `owner=<reference_id>`,讓孤兒能自證歸屬,不再依賴 content 反推。注意 tunnel payload 目前只有 name,須評估 CF API 是否支援 tunnel 級 metadata。
2. **【需改 code】operator 增帳號級反向 reconciler**:新增 list-all-tunnels / apps / policies + 與 Odoo stored-id 取差集的 dry-run GC endpoint,並修正 `per_page=100` 截斷。
3. **【需改 code】租戶 namespace 加 owner / reference-id annotation + TTL**:配對帳 cron,使 namespace 可反向查找與自動過期。
4. **【需改 code】SmartHome 補對稱 fail-loud guard**:對齊 `SecurityAccess.unlink`(cleanup 後 tunnel 仍在即 `raise UserError` 保留供 retry),消除正常刪除路徑漏 tunnel。
5. **【需改 code】顯式 env 設定**:考慮在 operator 加 `DEPLOY_ENV`,但須遵守既有 pattern `external-resource-env-token-naming.md` 的禁令(不可用 prefix 隱式推),改為顯式編入物件標記。

> 註:`fix/operator-issues` 分支已修 PVC label epoch(B 類部分項目進行中),部署需與 cross-repo woow-paas-charts cronjob 同步,prod rollout 另有 Fleet split-values secret drift 前置問題(`docs/tracking/pitfalls/fleet-split-registry-values-merge-drift.md`),與本調查的清理操作獨立。

---

## 資料來源(已親自核對)

- `extra/paas-operator/src/services/cloudflare.py`(全檔)— tunnel / DNS / Access create payload
- `extra/paas-operator/src/config.py` — `namespace_prefix`、`cloudflare_domain`、無 `DEPLOY_ENV`
- `src/services/naming.py` — ns / subdomain 命名
- `extra/paas-operator/src/services/helm.py:79-84,655+,683-684` — ns 前綴強制 + 唯一 label
- `docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md` — 孤兒成因與清理紀律(2026-06-18~19 清過 3 policy + 28 DNS + 3 tunnel + 30 ingress rule + 3 ns)
- `docs/decisions/patterns/external-resource-env-token-naming.md` — 禁止前綴隱式推 env 的 pattern
- adversarial 驗證:24 條關鍵結論逐一回核,更正 1 處(marketplace subdomain 來源)
