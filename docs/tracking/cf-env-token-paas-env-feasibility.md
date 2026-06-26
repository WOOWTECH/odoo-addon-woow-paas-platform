---
title: CF 命名加 env token + ns label + PAAS_ENV 可行性報告
category: tracking
status: active
created: 2026-06-21
updated: 2026-06-21
related:
  - docs/tracking/cf-ns-prod-stg-audit-report.md
  - docs/decisions/patterns/external-resource-env-token-naming.md
  - docs/decisions/patterns/su-env-exempt-guard-false-green.md
  - docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md
---

# CF 命名加 env token + ns label + PAAS_ENV 可行性報告

> 調查性質:**唯讀(不動功能 code)**。延續 [`cf-ns-prod-stg-audit-report`](cf-ns-prod-stg-audit-report.md) 的兩大問題,確認使用者提案——「在 CF 命名模板加 env token(stg `paas-stg-sa-` / prod `paas-sa-`)」+「引入 ns label `paas.woowtech.io/env` + `PAAS_ENV=prod/stg`」——的 blast radius、create↔delete 對稱性、能解/不能解哪些問題。
> 方法:6 面向平行調查 + 22 條關鍵結論 adversarial 驗證 + 主線親自核對 `naming.py` / `security_access.py` / `releases.py` / `helm.py` / pattern doc。

---

## Hero KPIs

- **可安全注 env token 的欄位類別:1 種** —— 平台自產的內部標籤(SA `canonical_name`、CS/SM `subdomain`);使用者自填的 SA `subdomain_prefix` 不可注。
- **加 env token 須雙向對稱的危險站點:B 類 1 條** —— operator `generate_subdomain`(重算 name);A 類全靠 stored 值天然對稱。
- **`PAAS_ENV` 真解的問題:辨識性那一類** —— 孤兒治理(發現/不再產生/回收)多半只是 enabler 或不解。
- **落實最致命的雷:2 個** —— immutable guard 測試別 su 豁免、label value 別帶尾 dash。

---

## 結論速覽

**問 1(CF 命名加 env token):可行,但要分清欄位語義。**
若 token 加在 `src/services/naming.py` 單一根(`make_*_subdomain` 前綴層),對 SA 內部 tunnel name / DNS comment / Access app name 是零破壞且 create↔delete 天然對稱(這些值 immutable stored、刪除靠 stored id 不重算)。**最關鍵 caveat:使用者自填的 SA route public hostname(`subdomain_prefix`)不該也不能注 token —— 那是對外 URL,env 區隔要靠 per-env `paas_domain`(`security_access.py:851-857`、`:918-926`)。** 能安全注 token 的只有平台自產的 `canonical_name`(內部標籤)與 CloudService/SmartHome 自產的 `subdomain`。

**問 2(ns label + `PAAS_ENV`):不是「同解很多問題」,而是「真解辨識性、只 enable 反向 GC、完全不解孤兒不再產生」。**
這三件事是同一條「可辨識性」主線,真正解掉的是「一眼辨 env / `kubectl -l` 查詢」;對孤兒治理整類(能被發現、不再產生、自動回收)多半只是 enabler 或完全不解。**最關鍵 caveat:label value 不可用帶尾 dash 的 namespace prefix 字面(`paas-ws-stg-` 非法),必須用乾淨 token `value=stg`/`value=prod`(`helm.py:90` regex 要求英數開頭結尾),否則整批 label 被 k8s atomic 拒、重蹈 #208 教訓。**

---

## 能解 / 不能解矩陣

| 項目 | 判定 | 理由(附證據) |
|---|---|---|
| **P1a 辨識性**(CF 物件一眼辨 prod/stg) | **solves(tunnel/DNS/policy)、partial(app name)** | `naming.py` 是 tunnel name / DNS comment / **Access policy name** 的單一根,注 token 一改全到位且 create/delete 對稱(`naming.py:19,24,29`;tunnel `security_access.py:368`、DNS comment `:547`、policy name `cloudflare.py:1322`)。**例外:Access app `name` 主路徑 = 使用者自填 `subdomain_prefix`(`security_access.py:1203`,非 canonical_name),不該注 token —— 見 public-URL 專節。** 目前 CF 可辨識 env 的結構化欄位數 = 0。 |
| **P1b ns 可查詢**(`kubectl -l env=stg`) | **solves** | 目前唯一 ns label 是 `paas-managed=true`,無 env(`helm.py:683-684`)。加 env label 即可查詢化;label 屬純 metadata、不參與反查、零破壞。 |
| **P2a 孤兒可發現**(反向 GC reconciler) | **partial(token 只是 enabler)** | 真正缺的是 operator 無任何 list-all-tunnels/apps/policies(`cloudflare.py:399,439` 僅單-id 列舉)。env token 只在「已有 reconciler」後讓它判 env 更乾淨,自身找不回一筆孤兒。 |
| **P2b 孤兒不再產生** | **no(完全不解)** | 根因是銷毀繞過 Odoo unlink(`destroy.sh` / DB 重置 / 手動 `kubectl delete` 不經 ORM),stored id 蒸發(`subscription_lifecycle_cron.py:118` record-driven)。與命名/標籤正交;要堵須加前置 Odoo teardown 或 SmartHome 補 fail-loud guard(`smart_home.py:202` 無條件 unlink)。 |
| **P2c ns TTL 自動回收** | **partial(label 只是 enabler)** | ns 加 owner/env label 是「讓對帳 cron 能掃」的前置,但 label 不會自動過期;自動回收仍須另做 TTL annotation + 對帳 cron。 |
| **P3 跨環境碰撞** | **partial** | prod/stg 共用同一 CF account+zone+base domain(`woowtech.io`,非 `*.stg` 設計)。token 注在 traceability 欄位解的是「碰撞後能辨認」,不解「不碰撞」。對外 hostname 真正分環境靠 per-env `paas_domain`(`res_config_settings.py:20-22`)。**但若 token 併入 hash 種子/前綴(改變實際命名空間),則對 CloudService/SmartHome 自產 subdomain 能結構性保證跨環境零碰撞** —— 這是 token 比 label 多出的硬價值。 |
| **P4 reconciler enabler** | **solves(作為 enabler)** | 共用 account list-all 撈出 prod/stg 混在一起,目前只能靠「DNS content 指哪條 shared tunnel」脆弱反推(dedicated tunnel/Access 連這線索都沒)。顯式 `PAAS_ENV` stamp 後 reconciler 可直接按 env 欄位過濾。前提是先補 list-all(見 P2a)。 |
| **P5 既有回填** | **no 且引入新風險** | `canonical_name` immutable、ns 名已固定 → 注入只對新 record 生效,DB 新舊混用。更糟:reconciler 必須同時認得「有 token」與「無 token」雙格式,否則舊 record 反成辨識不到的孤兒(`external-resource-env-token-naming.md:40-41`)。歷史 id 命名 ns(`paas-ws-1/5/253`)前綴與 prod 裸 `paas-ws-` 直接相撞,token 無法回頭救。 |

**一句話總結**:辨識性整類(P1a/P1b solves、P3 部分)是真解;孤兒治理整類(P2a/P2c enabler、P2b/P5 no)token+label 多半幫不上。**「讓孤兒較好辨認 ≠ 讓孤兒被發現/不再產生/自動回收」。**

---

## create↔delete 對稱性專節(加 env token 最大的正確性陷阱)

命名改動最容易炸的點:**delete 端若「重算 name」反查,而 create 端用了新命名規則、delete 端沒同步 → 算出舊 name 找不到資源 → 永久孤兒。**

### A 類:delete 靠 stored id / stored 欄位(天然對稱,注 token 零破壞)—— 佔絕大多數

| 模型 | create 算 name | delete 反查方式 | 對稱性 |
|---|---|---|---|
| SecurityAccess | `canonical_name = make_security_access_subdomain(...)` 首算存檔(`security_access.py:207,225-237`) | `delete_tunnel(self.tunnel_id)`、Access 靠 `existing_app_id`/`existing_policy_ids`;delete 路徑零呼叫 `_generate_canonical_name` | ✅ 天然對稱 |
| SmartHome | `subdomain = make_smarthome_subdomain(...)` 存(`smart_home.py:161`) | `delete_tunnel(self.tunnel_id)`(`:191`)讀 stored id | ✅ 天然對稱 |
| CloudService 主 route | `subdomain = make_service_subdomain(...)` 存(`paas.py:2005,2068`) | `uninstall_release(subdomain=self.subdomain)` 讀 stored 值(`cloud_service.py:1111`、`paas.py:2329-2332`) | ✅ 天然對稱 |
| K8s namespace | `make_namespace(prefix=ns_prefix)` 存 `helm_namespace` | delete 全程讀 `self.helm_namespace` stored 值 | ✅ 天然對稱 |

### B 類:delete 靠「重算 name」反推(唯一須雙向對稱的危險點)

- **`operator generate_subdomain`(`cloudflare.py:708`)**:`{release_name}-{ns_suffix}`,`ns_suffix = namespace.replace("paas-ws-","")`。**不吃 `naming.py`,是 operator 端獨立重算字串。** 用於:
  - (a) CloudService 主 route delete fallback(`releases.py:236`,僅 caller 未傳 subdomain 時)
  - (b) **mcp sidecar route** deploy(`releases.py:703-704`)與 delete(`releases.py:251`)兩側

### #207 mcp route 現況(pattern doc 已同步更新)

> **已修復**:`releases.py:251`(delete)已改成 `generate_subdomain(namespace, name) + "-mcp"`,與 deploy(`:703-704`)同源,`:245-250` 有註解明示「mcp leg 須與 deploy 對稱」契約,regression test `test_api.py:221 test_delete_release_mcp_route_matches_deploy_naming` 鎖住。git commit `c43cce4`(2026-06-20,「fix: operator teardown 孤兒修復 #207 mcp route 命名 + #208 PVC label epoch」)。**盤點時不應再當作 open 的不對稱站點**;pattern doc `external-resource-env-token-naming.md:19` 的標註已於本次同步更新為「已修復、留作活教材」。

### 為何 `naming.py` 單一根注入天然對稱

`generate_subdomain`(B 類)**不依賴 `naming.py`** —— 只消費 namespace 字串 + ns prefix replace。所以**只動 `naming.py` 注 env token,根本碰不到 B 類**,A 類又全靠 stored 值,故「Odoo 端 `naming.py` 注入 → create/delete 雙向對稱免費」。

⚠️ **唯一須留意的既存行為(非本次新增)**:若把 namespace prefix 從 `paas-ws-` 改成 `paas-ws-stg-`,`generate_subdomain` 的 `replace("paas-ws-", "")` 對 stg 會殘留 `stg-` 在 `ns_suffix`(`"paas-ws-stg-pa-x".replace("paas-ws-","")` = `"stg-pa-x"`)。這是既存行為,不是本次風險,但動 prefix 時須意識到。

---

## public URL vs 純 traceability 專節

**關鍵分水嶺:使用者的 `paas-stg-sa-` 到底指哪個欄位?**

### 安全(注 token 不改任何對外 URL):SA `canonical_name`(內部 tunnel name / 標籤)

- `canonical_name` 在 SA 路徑**絕不對外曝光成 hostname**。程式碼註解一字不差:「`canonical_name is only the tunnel's CF identity (its name), never surfaced as the hostname`」(`security_access.py:133-135`)。
- 它只當三種純 traceability 用途:CF tunnel name(`:368`)、DNS comment(`:547`)、Access app/policy name(`:594-595`)。三者皆不參與 routing / 反查(反查靠 stored `tunnel_id` / `app_id` / CNAME content)。
- **結論:注 env token 到 `canonical_name` 對 SA 零破壞、不改任何 public URL,只改 CF dashboard 內部標籤。**

### 不該注 token(會改使用者 URL):SA route `subdomain_prefix`(public hostname)

- 對外 hostname = `{subdomain_prefix}.{paas_domain}`(`security_access.py:918-926`)。
- `subdomain_prefix` 是**使用者自填**、required、全平台扁平 first-come-first-served、create 後 immutable(`:851-857`、UNIQUE 約束 `:838-840`)。
- pattern doc 明確劃為「不適用」env-token 注入(`external-resource-env-token-naming.md:12`):**env 區隔該靠 per-env `paas_domain`,不該由平台注入 token 改使用者選的 hostname。**
- **連帶:Access app `name`(display name)主路徑就是這個 `subdomain_prefix`**(主 sync `security_access.py:1203`、teardown `:595`,僅 prefix 空時才 fallback canonical_name)→ 同樣**不該注 token**。**Access 物件能「穩定帶 env」的只有 policy name**(`{canonical_name}: …`,`cloudflare.py:1322`),**不是 app name** —— 早前對照表把 app name 標成 canonical 根屬不精準,已於 `external-resource-env-token-naming.md:28` 與本表 P1a 同步校正。

### 介於兩者之間:CloudService / SmartHome `subdomain`(平台自產 hash,但對外)

- 它們的 `subdomain` 欄位本身就是 `make_*_subdomain` 算出的 hash,且直接拼成對外 hostname(`cloud_service.py:984`、`smart_home.py:281`)。
- 因平台自產(非使用者自填),是 pattern doc 適用注 token 的候選;**但動它＝改使用者 URL,要慎**(CloudService legacy shared-tunnel 路徑前述 A 類已驗證走 stored 值,對稱成立)。

**給使用者的直接回答**:若 `paas-stg-sa-` 指 **SA route 對外 subdomain**,不該注 token(那是使用者自填 hostname,env 靠 `paas_domain`);只有 SA 的 **`canonical_name`(內部 tunnel name / 標籤)** 才是可安全注 env token 的對象,且不改任何對外 URL。

---

## PAAS_ENV 是什麼角色

**`PAAS_ENV` 是「單一顯式 env 來源原語(primitive)」,不是功能本身。**

它解鎖三件下游:① ns label `paas.woowtech.io/env`;② CF naming token;③ 未來 reconciler 按 env 過濾。但**它本身不建 reconciler、不加 ns TTL、不堵孤兒產生**。

**現況事實(grep 證實)**:operator 全域無 `PAAS_ENV` / `DEPLOY_ENV`(`config.py` 0 命中,只有 `namespace_prefix="paas-ws-"` @ `:19`)。

**設計守則(pattern doc:17 + verify confirmed)**:
- Odoo 端 env token 來源用既有 config_parameter `woow_paas_platform.namespace_prefix`(已帶 stg 慣例 `paas-ws-stg-`,`res_config_settings.py:28-33`)或新增專用 config_parameter,在 `naming.py` 單一根 stamp。
- operator 端若也要帶 env,必須在 cross-repo `paas-app-bundle-cd` Fleet CD values 新增**顯式** `PAAS_DEPLOY_ENV` 注入 operator Deployment env(`config.py:76-77` 註解明示 operator 自身 Deployment env 住那裡、非本 repo chart 渲染)。
- **禁止**在 operator 寫 `namespace_prefix.removeprefix("paas-ws-")` 靠「prod=空字串」隱式推導(pattern 守則 2)。

**誠實分類「同解很多問題嗎」**:
- **真解**:P1a 辨識、P1b ns 查詢、P4(作為 reconciler 的 env 過濾 enabler,定位精準)。
- **只是 enabler**:P2a(要先有 list-all reconciler)、P2c(要先有 TTL+對帳 cron)。
- **完全不解**:P2b(孤兒產生根因是繞過 ORM)、P5(既有不回填,且引入 dual-format 負擔)。

### 兩個注入機制 × 各 CF / k8s 欄位歸屬(逐欄位該走哪條)

env 標記其實只收斂成**兩個注入機制**,不是「每個欄位各引入一次 PAAS_ENV」。判斷一個欄位該走哪條,就看「它在 Odoo 算還是 operator 算」:

| 欄位 / 物件 | 注入機制 | env 來源 | 改哪裡 | 需要 operator `PAAS_ENV`? |
|---|---|---|---|---|
| SA tunnel name | **① Odoo naming.py 單一根** | `canonical_name` | `naming.py:29`(一處) | ❌ |
| SA DNS comment | **① Odoo naming.py 單一根** | `canonical_name` | `naming.py:29`(同上,**免費繼承**) | ❌ |
| SA Access **policy name** | **① Odoo naming.py 單一根** | `canonical_name`(operator 接 `f"{canonical}: {name}"`) | `naming.py:29`(同上,**免費繼承**;`cloudflare.py:1322` 不動) | ❌ |
| CS / SM `subdomain`(平台自產 hash) | **① Odoo naming.py 單一根** | `make_service/smarthome_subdomain` | `naming.py:19/24` | ❌(但動它＝改對外 URL,慎) |
| K8s namespace name | ① Odoo(prefix 參數,**已做**) | `namespace_prefix` config_parameter | `make_namespace` 的 prefix | ❌(prod `paas-ws-` / stg `paas-ws-stg-`) |
| **ns env label** `paas.woowtech.io/env` | **② operator stamp** | `PAAS_ENV`(operator env) | `helm.py:683` + `agent_runtimes` ns labels | ✅ |
| **reconciler 按 env 過濾** | **② operator stamp** | `PAAS_ENV`(operator env) | operator 新增 list-all + filter | ✅ |
| SA `subdomain_prefix`(對外 hostname) | **❌ 不注** | —(env 靠 per-env `paas_domain`) | — | — |
| Access app `name`(主路徑 = `subdomain_prefix`) | **❌ 不注** | —(隨 `subdomain_prefix`) | — | — |

**一眼結論**:
- **機制 ①(改 `naming.py` 一處)→ tunnel name + DNS comment + policy name 三個一起帶 env**,operator 與 `PAAS_ENV` 完全用不到 —— 它們都是 `canonical_name` 的下游,免費繼承。
- **只有機制 ②(ns label、reconciler)才真的需要 operator `PAAS_ENV`**,因為那是 operator 自己獨立 stamp 的東西。
- **`subdomain_prefix` / Access app `name` 兩個不注 token**(使用者自填 / 對外 URL),env 靠 per-env `paas_domain`。
- 對應落實藍圖:機制 ① = **L3**、機制 ② 的 label = **L2** / reconciler = **L4**;`PAAS_ENV` 原語本身 = **L1**(只有機制 ② 依賴它)。

---

## 落實藍圖(唯讀提案,不改 code)

### 分層 + 跨 repo lock-step

| 層 | 落點 repo / 檔案 | 動作 | effort | 風險 |
|---|---|---|---|---|
| **L1 PAAS_ENV 原語** | Odoo: 沿用 `namespace_prefix` config_parameter(`res_config_settings.py:28`)或新增專用;operator: `config.py:19` 加 Field + Fleet values 注入 | 建立單一顯式 env 來源 | 低(Odoo 側)/中(operator 需重 build + Fleet PR) | 跨 repo drift;Fleet 同名 map 跨 valuesFiles+valuesFrom 拆 key 會非 deep-merge 掉 key(registry secret drift 前例) |
| **L2 ns label** | operator image: `helm.py:683-684`、`agent_runtimes.py` 各加 `paas.woowtech.io/env` key | ns metadata 可查詢化 | 中(改 operator image + 發 operator-v* tag) | **label value 合法性**(見下);`create_namespace` 路徑目前不經 `validate_label_value`(只有 `label_pvcs` 經過),須補同守衛 |
| **L3 CF naming token** | Odoo: `naming.py:19,24,29` 前綴層加 env 參數 | CF 物件自證 env | 低(單一根,create/delete 對稱免費) | immutable canonical guard(見風險節);測試斷言會紅;既有不回填 |
| **L4 reconciler** | operator: 新增 list-all-tunnels/apps/policies + dual-format 認別 | 反向 GC(真正解 P2a) | 高(新功能 + 帳號級對帳邏輯) | 須認「有/無 token」雙格式;per_page 截斷漏孤兒 |

### 建議順序

**先 L1(PAAS_ENV 原語)→ L2(ns label,立即拿 `kubectl -l` 查詢價值)→ L3(CF naming token,拿 CF 辨識)→ L4(reconciler,才是孤兒真解但 effort 最高)。**

理由:L1 是 L2/L3/L4 的共同前置;L2 風險可控、立即見效;L3 對稱免費但有 immutable/回填 caveat;**L4 才是孤兒治理的真答案,但不依賴前三層也能獨立做**(前三層只讓它判 env 更乾淨)。

---

## 風險與前置

1. **immutable `canonical_name` guard(create 首算、測試別 su 豁免)**:
   - env token 只能在 create 的 `_generate_canonical_name` 內算進去,**不能事後 write 注入** —— create() 對所有人(含 superuser)pop 掉 caller 傳入值(`security_access.py:221-237`),write() guard 用 `sa_internal_identity_write` context-key 鎖死(`:248-253`),**刻意不放行 `env.su`**(因 `TransactionCase` 是 su env,放行會讓 deny 分支跑不到 → 假綠)。
   - **守線測試紀律**:新增 env-token 邏輯時不可把 guard 改成 `not env.su` 形態,否則同時破壞既有守線(`su-env-exempt-guard-false-green.md:46-67`)。`subdomain_prefix` 同屬 immutable anchor,rename = delete+recreate。

2. **既有 record 不回填 → dual-format reconciler**:
   - `canonical_name` immutable、ns 名已固定 → 注入只對新 record 生效,DB 新舊命名永久混用。任何未來 reconciler / 孤兒清理器**必須同時認得「有 env token」與「無 env token」兩種格式**,否則舊 record 反成辨識不到的孤兒(`external-resource-env-token-naming.md:40-41`)。

3. **label value 合法性(stg 尾 dash 非法、用 `value=stg`)**:
   - `helm.py:90` regex `^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$` 要求英數開頭結尾。實測:`stg`/`prod` 合法,`paas-ws-stg-`/`stg-`(尾 dash)**非法**,`paas-ws-stg`(無尾 dash)合法。
   - **label value 必須用乾淨 env token(`stg`/`prod`),不可把 `namespace_prefix` 字面當 value**,否則 k8s atomic 拒整批(重蹈 #208 colon-orphaned-at:單一 bad value 靜默丟整批、錯誤被吞成 WARNING、74 天沒生效)。
   - 前置:`create_namespace`(`helm.py:683`)與 `agent_runtimes` ns labels 目前不經 `validate_label_value`,新增 env label 須同步把守衛拉到這些 apply 點 fail-loud。

4. **測試斷言會紅的範圍(fixture-coupling)**:
   - `src/tests/test_naming.py`(~11 條格式斷言:`startswith paas-cs-/sm-/sa-`、`len`、`split parts`)會直接因加 env token 變紅。
   - `src/tests/test_security_access.py`(`canonical_name startswith`/格式/注入剝離 6+ 條)。
   - operator `tests/test_access_sync.py` / `test_tunnel_routes_sync.py` 大量硬編 `paas-sa-x-y` / `paas-ws-test` fixture(多為 mock 輸入值,只在 naming 輸出被斷言時才紅)。
   - **須 lock-step 改,否則假綠**(credential-claim / phase-versioned pitfall 同類)。

5. **migration 需求**:
   - `canonical_name` immutable → 回填只能新增不能改舊值(`src/migrations/18.0.1.0.40/post-migrate.py` 為 backfill 範本)。
   - 既有 ns 無 env label → 若要回填須對活躍 ns 補打 label(新建 ns 才自帶);歷史 id 命名 ns(`paas-ws-1/5/253`)前綴與 prod 撞,無法靠 token 回頭救。

---

## 一句話給決策者

問 1 **可行且推薦** —— 但只注在平台自產的內部標籤(`canonical_name` / CS·SM `subdomain`),**碰使用者自填的 SA `subdomain_prefix` 是錯的**(那要靠 per-env `paas_domain`)。問 2 的三件套是**辨識性的真解 + 反向 GC 的部分 enabler**,**不是孤兒問題的解藥**;要真正治孤兒,L4 reconciler 與堵 ORM-bypass(P2b)才是核心,且不依賴前三層。落實時最致命的兩個雷是 **immutable guard 測試別 su 豁免** 與 **label value 別帶尾 dash**。

---

## 資料來源(已親自核對)

- `src/services/naming.py:14-31` —— `make_namespace` / `make_*_subdomain` 命名單一根
- `src/models/security_access.py` —— `canonical_name` create 首算(`:207,225-237`)+ immutable guard(`:248-253`)+ tunnel/DNS/Access 用點(`:368,547,594-595`)+ `subdomain_prefix` public hostname(`:851-857,918-926`)
- `extra/paas-operator/src/api/releases.py:236,251,703-704,762` —— `generate_subdomain` 重算路徑;mcp route #207 修復(commit `c43cce4`)
- `extra/paas-operator/tests/test_api.py:221` —— mcp route 對稱 regression test
- `extra/paas-operator/src/services/helm.py:90,683-684` —— label value regex + ns 唯一 label
- `extra/paas-operator/src/config.py:19,76-77` —— `namespace_prefix`、無 `PAAS_ENV`、operator Deployment env 落 Fleet
- `docs/decisions/patterns/external-resource-env-token-naming.md` —— env-token 注入守則(本次同步更新 #207 標註)
- `docs/decisions/patterns/su-env-exempt-guard-false-green.md` —— immutable anchor guard 測試紀律
- `docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md` —— 孤兒 GC 缺口(P2 整類根因)
- adversarial 驗證:22 條關鍵結論逐一回核(verify 階段標出 pattern doc #207 stale + 「canonical 是 stored」非全模型通則的 partial 校正)
