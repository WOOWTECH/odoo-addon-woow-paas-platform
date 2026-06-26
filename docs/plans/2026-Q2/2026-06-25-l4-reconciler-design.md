---
title: L4 帳號級反向孤兒 Reconciler — 設計步驟（F-ENV-2）
category: plans
status: draft
created: 2026-06-25
updated: 2026-06-25
related:
  - docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md
  - docs/tracking/2026-06-25-fenv2-l4-reconciler-explainer.md
  - docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md
  - docs/tracking/followups/follow-ups.md
---

# L4 帳號級反向 Reconciler — 最終可執行設計步驟（F-ENV-2）

## 安全模型（三問速答）

**會自動刪除嗎？** 首版完全不會、零自動刪除。dry-run-first 是鐵律：CI grep gate 強制 reconcile 路徑零 `delete_*` / `uninstall` / `kubectl delete` / `unlink`，且 `dry_run=False` 在 endpoint 層直接 422 拒收。自動刪除被 DP-MARKER（正向 ownership marker + backfill）/ DP-RACE（同 account serialization）/ DP-UNRECON（三類能否永久人工定調）三個決策點 gate 住，**不在 MVP**。未來即使開自動刪，也只對唯一帶 owner marker 的 SA route DNS（有 owner comment）開，且 dedicated tunnel / Access app / legacy 無 token 存量這三類 un-reconcilable 永久人工、永不自動刪。

**會不會誤刪 prod 的 namespace / Cloudflare 資源？** 首版不刪，故不會。設計的多層守衛正是專防此情境：R3 env-pin fail-closed（reconciler 啟動鎖自己的 `PAAS_ENV`，禁用任何 fallback 預設，判不出 env → abort 零候選）；prod-DB-diff 採「可達 ≠ 對該 model 有權威」（table-not-exist / 查詢回空集 → skip，不當孤兒）；R1 結構 allowlist（非-paas 命名空間的 record 結構上拿不到 `reconcilable=True`，不靠 denylist 認得官網 / apex / MX）；R5 protected denylist tripwire（apex / MX / shared tunnel 本體硬編，ingress 走完整 hostname 精確比對）。namespace 靠 `paas.woowtech.io/env` label 判歸屬，但 F-ENV-3 的 marketplace BARE ns（無 label）必須標 `unknown` 不誤判。**這些守衛是「開自動刪之前必須先 code 落地並在 stg 驗證通過」的硬前置，不是事後補。**

**人工審在哪裡？** 在 Phase D。流程為：`ir.cron` 低頻唯讀反向對帳 → 寫 dry-run inventory → `/woow` 後台唯讀報表頁（OWL，欄位 `kind / id / name / env_hint / reconcilable / reason / scanned_at`）→ 人工檢視清單 → 刪除前對單筆即時重查（防 TOCTOU：報表當時是孤兒、人去刪時可能已被新 provision 重用同 subdomain）→ 人工執行刪除（operator delete API / kubectl，照 runbook）。UI 未做之前，過渡期用 `kubectl exec` 打 `/api/reconcile` 看 JSON。發現與刪除刻意分離，刪除權永遠留在人手上。

---

## 0. 設計原則（鐵律，貫穿全篇，非附註）

### 0.1 dry-run-first 鐵律
- `ReconcileResponse.dry_run` 預設 `True`；首版 `dry_run=False` **直接 422 拒收**（endpoint-level test 守，見「機械守線」節）。
- reconcile 路徑（`src/services/reconciler.py`、operator `src/api/reconcile.py`、cron/報表）pre-merge grep 須**零** `delete_*` / `uninstall` / `kubectl delete` / `unlink`。這是 CI fail-closed gate，不是各 Phase 人工自律。
- 在「正向 ownership marker + backfill」「env-pin fail-closed」「protected denylist」「跨類右集 completeness gate」四者補齊前，L4 只產孤兒 inventory 給人工，**不開任何 delete**。

### 0.2 五條誤刪鐵律

| # | 鐵律 | 落地 Phase |
|---|------|-----------|
| **R1 Allowlist not denylist** | `reconcilable` **結構上預設 `False`**，唯有「前綴 ∈ 已知 paas 命名空間 **AND** ownership 證據成立」才升 `True`。任何 zone-wide 撈回但前綴不在 paas 命名空間的 record（官網 / 行銷 / apex / MX）**結構上**拿不到 `reconcilable=True` — 不靠 denylist 認得它。denylist 降格為 tripwire（見 R5）。 | C（結構防線）+ B（差集只標候選不刪） |
| **R2 雙證且分清 env vs ownership** | 前綴 + 內容雙證；但**明確區分**：(a) **env-attribution**（DNS content 指哪條 shared tunnel / tunnel 物件 name 帶 env token）= 任何人可偽造，只能填 `env_hint`；(b) **ownership-attribution**（我蓋的 owner marker，目前僅 SA route DNS comment `cloudflare.py:286-287`）= 才可授權 `reconcilable=True`。content 指 shared tunnel **不是** ownership。 | C（雙證分流）+ DP-MARKER |
| **R3 env-pin fail-closed** | reconciler 啟動 self-check：要求 `PAAS_ENV` / `deploy_env` 為**顯式非空白名單值（exactly `prod`\|`stg`）**，**禁用任何 fallback 預設**；判不出 → abort，零候選。跨-env skip；prod-DB-diff 連不上或無權威 → skip（fail-safe 非 fail-open）。 | C（self-check + env-pin） |
| **R4 dry-run + TOCTOU 重算 + 分頁走到底** | 刪前對 CF 與 Odoo 雙重查（首版人工刪同樣有 TOCTOU 窗，不只自動刪階段）；cursor 分頁走到底防截斷；跨類右集任一失敗 → 整輪 `partial`、零候選。 | A（cursor）+ B（completeness gate）+ D（人工版 TOCTOU）+ E/未來〔自動 TOCTOU；自動刪本身 gated on DP-MARKER/DP-RACE/DP-UNRECON〕 |
| **R5 protected denylist 兜底 tripwire** | apex / MX / 官網本體 / 行銷子網域 / shared tunnel 本體（stg `55953bfa` / prod `328c3a28`）硬編。角色是 **tripwire**：若某 protected 名稱意外被判 `reconcilable=True` → 立即 abort + fail-loud（代表雙證有 bug）。**不是** Q1 主擋線（主擋線是 R1 結構防線）。永不刪 shared tunnel 本體，只清指向它的 DNS/ingress rule，且 ingress GET-filter-PUT 須精確完整 hostname 比對。 | C（denylist + ingress 精確比對） |

### 0.3 Un-reconcilable 三類（只能 dry-run + 人工，永不自動刪）

| 類別 | 為何雙證失效 | L1-L3/F-ENV-7 後 |
|------|-------------|------------------|
| **Dedicated tunnel**（`paas-{env}-sm-*` / `-sa-*`，CNAME 指自身） | ownership 第二腳「DNS content 指 shared tunnel」永遠不成立（CNAME target = 自身 tunnel id） | env_hint 可從 tunnel 物件 name 判（SM `smart_home.py:138`、SA canonical_name）；**legacy 無 token → env_hint 誠實 `unknown` 不瞎猜 stg**。SM DNS 無 comment（`smart_home.py:181`）→ 反向 GC 須先 join 到 tunnel 物件 name 才判 env |
| **Access app** | CF Access app **無 metadata 欄位**蓋 owner tag（`cloudflare.py:1334-1350`）；policy 僅 name 帶前綴 → allowlist 退化成跟前綴一樣弱 | L1-L3 未改善；canonical_name 前綴只當「弱 env_hint」 |
| **Legacy 無 env token 存量** | record 已 unlink / 無 stored-id / 無 marker / 無 shared-tunnel 內容 → 通不過任何守衛 | token **不溯及既往**；連 reconciler 當初要解的既有孤兒 backlog（2026-06 人工事故那批 3 條 `paas-sm-*` dedicated tunnel）都無法安全自動刪 |

**推論**：未來若開自動刪（DP-MARKER/DP-RACE/DP-UNRECON 拍板後），唯一結構上**可能成為自動清候選**的只有「**未來新洩漏 + 帶完整 owner marker**」的資源（目前僅 **SA route DNS**〔有 owner comment〕乾淨）；**首版仍全程 dry-run、不執行刪除**。CS shared-tunnel DNS / ns 在 DP-MARKER backfill 前一律 `reconcilable=False`。上述三類永遠 dry-run + 人工。

### 0.4 L4 範圍校正（≠ 全新）
- PVC `orphaned-at` grace GC 已由 operator #207/#208 修復上 stg（epoch 秒 + `validate_label_value`）。L4 只涵蓋**殘餘 Released PV / Longhorn volume** 計費 + **namespace 無 TTL**（真待建項）。L1-L3 + F-ENV-7 已 ship、F-ENV-4 止血 RESOLVED。

---

## Phase A — operator 帳號級 list-all 能力 + reconcile router

> **動 `extra/paas-operator/` → build operator image（`operator-v*` tag → CI）+ `paas-app-bundle-cd` Fleet bump（pin 雙檔 `paas-operator-stg/values-stg.yaml`〔stg〕+ `paas-operator/values-prod.yaml`〔prod〕），deploy-stg 帶不上。**

**目標**：補齊現況為 0 的帳號級反向發現能力。現行 `CloudflareService` 全 by-id / per-tunnel；`_tunnels_url(None)`（`:96`）能組 collection URL 但無 method 呼叫它做 list。

### A.0 阻塞前置（第一個 commit，作 AC，不留「或」）
- **結構化關閉 async-skip**：`extra/paas-operator/` 現有 `pytest.ini` 含 `--strict-markers --disable-warnings -m "not integration"`、**無 `asyncio_mode`**、`pytest-asyncio==0.25.2` → strict mode + 警告被 `--disable-warnings` 吞 = #208 假綠溫床。第一步：
  - `pytest.ini` 加 `asyncio_mode = auto`；
  - **移除 `--disable-warnings`**，改 `filterwarnings = error`（把 async-skip warning 升 error）；
  - 加 collection meta-test：斷言「collected async test 數 == 預期數、且無 test 進 skipped」。
  - 驗收：故意刪某 reconcile async test 的 `await`/marker → CI 須**紅**而非綠。

### A.1 改動站點

| 站點 | 改動 |
|------|------|
| `extra/paas-operator/src/services/cloudflare.py` | ① `list_all_tunnels()`（`GET accounts/{aid}/cfd_tunnel`，重用 `_tunnels_url(None):96`；**過濾 `deleted_at != null` 軟刪**；cursor 分頁到底）② `list_all_access_apps()`（`GET accounts/{aid}/access/apps`）③ `list_policies_of_app(app_id)`（`GET access/apps/{id}/policies`）+ `list_all_reusable_policies()`（`GET access/policies`）④ zone 全量 DNS：**修 `_list_dns_records_by_content:409` 寫死 `per_page=100` → 加 cursor 分頁迴圈**（事故漏 15 筆 `*-mcp` 即此因） |
| `extra/paas-operator/src/api/reconcile.py` **（新建 router）** | **✅ TS-3a 已實作（2026-06-26）**：`APIRouter(prefix="/api/reconcile")`；唯讀 `GET /inventory`。**方向 A**：operator 只回 raw 右集（`InventoryResponse`，未分類），差集/分類/刪除全在 Odoo。**`PAAS_ENV` fail-CLOSED**：route 層 `Depends(require_pinned_env)` 驗 `settings.paas_env` ∈ {`prod`,`stg`}，否則 **503**（在打 CF 之前擋下，不沿用 operator ns-label 的 fail-OPEN）。逐類 list-all 獨立 try：任一類 `CloudflareException` → `partial=True`+`failed_kinds` 記該類、該類缺漏不混入（completeness gate）；非預期錯 → 500 fail-loud。在 delete-gate 監看清單內（已驗對抗式注入 `subprocess` → 紅） |
| `extra/paas-operator/src/models/schemas.py` | **✅ TS-3a**：新增方向 A 的 operator→Odoo wire `CfResource{id,name,kind}` + `InventoryResponse{paas_env, partial, failed_kinds:[str], resources:[CfResource]}`（golden `tests/fixtures/inventory_response_golden.json`）。**`ReconcileResponse{...orphan_candidates[...env_hint,reconcilable,reason]}` 改定位為 Odoo `reconciler.py` 的產出（分類後 inventory → 報表/UI），非 operator 端 wire**；`ReconcileRequest{stored_ids}` 在方向 A 下 operator inventory 不收（param-free），暫保留供未來 server-side action endpoint。`dry_run=False` 422 gate 隨之移至未來 action endpoint（operator 唯讀無此 param，見 §機械守線） |

### A.2 TDD 重點
- **async marker 結構守線**（見 A.0）— 不靠每測手貼。
- **分頁三測**（直接對標 `*-mcp` 漏 15 筆事故）：① **漏帶分頁參數 = 紅**（mock 驗請求 params 必含 cursor/page key）② **末頁終止**（mock 末頁回空 `next_cursor` → 迴圈停、不重複請求）③ **150 筆全撈 `count==150` 非 100**。
- **DP-PAGINATION 前置阻塞**：寫任何分頁測試**之前**，用真 CF sandbox zone 確認 `next_cursor` vs page offset 語意，**不可邊猜邊測**（mock 對了實作也對了但都對在錯假設 = 雙重假綠）。語意確認前 Phase A 標 `blocked-on DP-PAGINATION`。
- **list-all 失敗 fail-closed**：沿用 `get_dns_record` 的 `raise_on_error`（`cloudflare.py:114`），list 異常**絕不**回空清單（空清單下游當「無孤兒」→ 在不完整清單上差集刪除）。
- **軟刪過濾**：mock 回含 `deleted_at != null` tunnel → 斷言不入候選。
- **CF 真契約守線（硬 AC，非降級）**：把 `tests/integration/README.md:91` 已規劃的 `CLOUDFLARE_*` gated marker 從 Known-gap 升為 AC — 新增 `tests/integration/test_reconcile_live.py`（`@pytest.mark.cloudflare_live`），對 sandbox zone 建已知 fixture（>100 筆觸發分頁 / 含軟刪 / 含 apex+MX 干擾），斷言 `list_all_*` shape + 過濾。若短期無 live zone，**誠實降級**：Phase A/B/C 的 live 點標 manual-only、無自動回歸、記入 follow-up 盲區（不可寫得像已守住）。
- **`stg kubectl exec`** 列為**部署後 smoke**，**不是契約守線**（README:96 明文 fallback 只有人工肉眼比對 = 無回歸防護）。

### A.3 跨 repo lock-step
- operator image（含 `/api/reconcile`）**必須先部署**，Odoo client（Phase B）後上線，否則 404（BUG-8 class）。
- **operator rollout 兩拍**：stg Fleet bump（`operator-v*` tag → CI → `paas-app-bundle-cd` 的 `paas-operator-stg/values-stg.yaml` pin）驗證通過 → prod 為**獨立第二次 merge**（`paas-operator/values-prod.yaml`，**Fleet 無自動 promote**）。

### A.4 驗證點
- stg operator 升級後 `kubectl exec` 打 `GET /api/reconcile/...`（dry-run）能列 CF-side 全量；zone >500 筆驗無截斷。
- **prod token scope 是 prod rollout 硬阻塞**（見「prod 前置硬阻塞清單」節），dry-run 只需 Read；對 403 **fail-loud**（回明確錯誤非空候選，否則「無孤兒」與「沒權限」無法區分）。

---

## Phase B — Odoo client 左集 + 差集引擎

> **不動 `extra/paas-operator/`**（純 Odoo client），但**依賴 Phase A image 已 live**。一般 addon 部署。

**目標**：蒐集 Odoo-side stored-id 白名單（左集）、呼叫 Phase A list-all（右集 = CF ground truth）、取差集產孤兒候選。差集方向（CF 有、白名單沒有 = 孤兒）是 record-driven `subscription_lifecycle_cron.py:39` 永遠做不到的反向。

### B.0 阻塞前置（開工前，照 gap-chain D1 checklist 第 1 條）
- **驗 stg operator image tag ≥ 含 `/api/reconcile` 的版本**：`kubectl -n paas-stg-system get deploy paas-operator-stg -o jsonpath='{..image}'` 看 **operator** image tag（**不是** Odoo image）。未達 → 不開工（否則 client 404）。
- **client `reconcile_orphans` 對 404 / 連線失敗 fail-loud raise，絕不降級回空候選**（與 Phase A 端 fail-closed 對稱，補在 client 半邊；否則整條對帳靜默回空 → 下游當「無孤兒」）。

### B.1 改動站點

| 站點 | 改動 |
|------|------|
| `src/services/paas_operator.py`（新增 `reconcile_orphans(stored_ids)`，~`:625` 鄰近，現無此 method） | 蒐集三表 stored-id 後呼叫 Phase A endpoint；沿用 `DEFAULT_TIMEOUT`/`HELM_OPERATION_TIMEOUT`（`paas_operator.py:16,18`）的 timeout pattern；404/連線失敗 fail-loud（見 B.0） |
| **左集：SA（CRITICAL 2 層巢狀 walk）** | `tunnel_id`（`security_access.py:94`）+ walk `route_ids` → `route.dns_record_id`（`:889`）+ `route.access_app_id`（`:906`）+ **再 walk `route.access_policy_ids` → `policy.cf_policy_id`**（`security_access_access_policy.py:69`，巢狀走法抄 `security_access.py:778-779`）。**漏走任一層 → 活躍 policy/DNS/app 當孤兒 = 最高衝擊**。**另補 id-independent 第二鍵**：`route.subdomain_prefix`（UNIQUE，`security_access.py:844-845,857`）→ hostname，覆蓋 `dns_record_id` 未回填的 in-flight route（DNS 反查比 id 可靠，hostname 由 prefix 決定且 immutable；對拍邏輯見 `security_access.py:557-563`） |
| **左集：CS** | `subdomain`（`cloud_service.py:133`）+ `helm_namespace`（`:151`）+ **`helm_release_name`（`:147`，CRITICAL）**。**MCP sidecar route 必須複刻 operator transform**：`mcp_hostname_base = f"{helm_release_name}-{helm_namespace.replace('paas-ws-','')}-mcp"`（operator `generate_subdomain` = `f"{release_name}-{ns_suffix}"`，`cloudflare.py:708-720`；deploy/delete **同源**），加入「自家 DNS」+「自家 shared-tunnel ingress rule」兩集合。**CS 無 dedicated tunnel_id**（走 shared tunnel）。**`custom_domain`（`:137`）**：先確認 deploy 是否為它建 CF DNS（grep `create_route`/`create_dns` 帶 custom_domain）；若建 → 納左集 hostname；若不建 → design 註記「使用者自管、不在 reconcile 範圍」+ 列 denylist 兜底 |
| **左集：SM** | `tunnel_id`（`smart_home.py:79`，dedicated tunnel 反查鍵）+ `subdomain`（`:105`，DNS）+ `tunnel_name`（`:84`，env 歸屬判讀用，非額外 walk 層），皆主 model 一層 |
| 差集引擎（新 `src/services/reconciler.py`） | 集合差集 + resource-class 分類器（前綴辨 cs/sm/sa）+ **dual-format 認舊新前綴**（`paas-{sa,cs,sm}-{hash}` 無 token + `paas-{stg}-{...}-` 有 token；漏認舊格式 → 舊資源變孤兒）。**修正草案事實錯誤**：CS MCP route **無** `svc-*-mcp` / `paas-cs-*-mcp` 雙格式（codebase 無此二字面，deploy/delete 同源 `generate_subdomain`）→ MCP route **不靠前綴認**，改靠「左集重建出的 `mcp_hostname` 精確比對」+「DNS content 指 shared tunnel」 |

### B.2 跨類右集 completeness gate（R4 升級版）
- `reconcile_orphans` 蒐集右集的**每一類**（tunnels / DNS / apps / policies / ns）**任一失敗**（含分頁中途 raise、403、timeout）→ 整輪標 `partial=True` 並**不產任何 `reconcilable=True` 候選**（最多產 `env_hint=unknown` 的 inventory 供人看，全 `reconcilable=False`）。差集運算開頭斷言 `completeness==True` 否則 abort，**嚴禁把左集全判孤兒**（右集退化成空 → 差集 = 左集全體 = 全標孤兒 = 災難）。

### B.3 TDD 重點
- **SA 2 層 walk 完整性（最高優先，鎖 intent 非 implementation）**：左集 `cf_policy_id` 集合 **== 從 DB 直接 `SELECT` 所有 `route.access_policy_ids.cf_policy_id` 的集合**（ORM 全撈對拍當 ground truth，非硬編「走 2 層」）。同理 `dns_record_id` / `access_app_id` 都用 DB 全撈對拍 — 未來加巢狀層時 ground truth 自然涵蓋新層，漏走會自動紅（避 phase-versioned-test-assertion 假綠）。刻意只走到 route 不展開 `access_policy_ids` 的版本必須紅。
- **不可誤用 `security_access_id` related 當唯一路徑**：policy 的 `security_access_id` 是 store=True related（`security_access_access_policy.py:38-41`，只供 ir.rule/quota），真正 binding 是 `route_id`；左集 walk 走 `route_id` 鏈。
- **每個左集鍵一個負向測試**：活躍資源用該鍵的**真實 operator 生成值**放右集，斷言 **NOT in 孤兒候選**。特別是 **MCP route**（右集放 operator 真實 `{release}-{wshash}-mcp` DNS + ingress rule；只收 `subdomain`+`helm_namespace` 的版本必須紅）+ **SA route 多 policy 巢狀** + **SA route `subdomain_prefix` 第二鍵**（`dns_record_id` 空但 prefix hostname 在右集 → 不誤判）。
- **completeness gate 對位**（B.2）：mock 其中一類 list-all 在第 2 頁 raise → 斷言整個 `ReconcileResponse` 不含任何 `reconcilable=True`（而非只那一類受影響）；右集為空但 `completeness=False` → 同樣 abort，不得把左集全判孤兒。
- **空 stored-id 不入左集**：`False`/`''` 不算「持有該 id」；對應 CF/ns 資源可能仍存在（in-flight provision 中途失敗）→ 差集對此另立 dry-run/grace（不誤刪 in-flight）。
- **保留供 retry 的活躍 id 仍算左集**：SA error state 保留 `tunnel_id`、CS keep `helm_release_name` → 算左集（資源還在不該刪），與真孤兒區分（判定來源：state 機 + id 存在性）。

### B.4 跨 repo lock-step / 驗證點
- 依賴 Phase A image live；Phase B 純 Odoo 走一般 addon 部署。
- stg live：`kubectl exec` 觸發 reconcile，比對左集（Odoo DB 三表全撈）vs 右集，人工抽查確認活躍資源（**含 MCP route**）**不在**孤兒候選。

---

## Phase C — 誤刪守衛：env-pin self-check + 雙證分流 + protected denylist

> **主體純 Odoo（env-pin / denylist / 分類器）走一般 addon 部署。** `DP-SM-COMMENT`（補 SM DNS comment）**抽出成獨立 operator-touching 變更**，自帶 build + Fleet + stg→prod gate，**不混進本 Phase**，以維持 C 單一部署形態。

**目標**：把五條鐵律從紀律落地成 code。補齊前 L4 維持 dry-run-only。Q1（zone-wide 撈到官網 apex/MX/行銷 → DB 查無 → 誤判孤兒）與 Q2（stg 殺 prod）是本 Phase 的唯一防線。

### C.1 改動站點

| 站點 | 改動 | 鐵律 |
|------|------|------|
| **reconciler self-check（最關鍵，擋 Q2②）** | 啟動要求 `PAAS_ENV`（operator）/ `deploy_env`（Odoo `ir.config_parameter`）為**顯式 `prod`\|`stg`**。**禁用 fallback**：Odoo 端**不可** `get_param(key,'prod')`，須 `get_param(key)` 後判 falsy → abort；operator 端不沿用 `paas_env=""` fail-open。**理由（已驗）**：operator `config.py:107` `paas_env` 預設 `''`（`main.py:37-43` fail-OPEN）、Odoo `res_config_settings.py:36`/`smart_home.py:155`/`security_access.py:211` 預設 `'prod'` — 既有預設讓「漏設」偽裝成「成功讀到 prod」，naive self-check 的 abort 分支永遠跑不到（su-env-exempt-false-green 同家族）。判不出 → abort，零刪除候選 | **R3** |
| 差集引擎結構防線（Q1 主擋線） | 每個候選 `reconcilable` **預設 `False`**，唯「前綴 ∈ 已知 paas 命名空間 **AND** ownership 證據成立」才 `True`。zone-wide 撈回但前綴不在 paas 命名空間者（官網/行銷/apex/MX）**結構上**拿不到 `True`（不靠 denylist 認得） | **R1** |
| env 分流器 | **env-attribution 機制**：DNS content 指哪條 shared tunnel（stg `55953bfa`/prod `328c3a28`）；**SA**：canonical_name DNS comment（`security_access.py:553`）；**SM dedicated**：讀 `tunnel_name` env token（**不可讀 subdomain**，F-ENV-7 後 subdomain 刻意 env-token-FREE）；**CS MCP route 特例**：hostname `{release}-{ns_suffix}-mcp` **無 env token** → **只能靠 DNS content 判 env，prefix `paas-{env}-cs-` 不適用**（以 `-mcp` 結尾且 content 指 shared tunnel 者走 content-only，不因 prefix 不符而降級 unknown）；**legacy 無 token → env_hint 誠實 `unknown`** | **R2/R3** |
| dedicated tunnel env via prod-DB-diff | **prod DB 可達性是「可被推翻的前提」**：跑 diff 前先驗。**「可達」判準須含「目標 model table 存在且查詢回非錯誤」**：prod 平台 Odoo 沒裝 CS/SA/SM model → table-not-exist / 權限錯 / 空集 **不可當「prod 確實沒持有 → 是孤兒」**，須視為「無權威 → skip」。**prod-DB-diff 對 dedicated tunnel 只填 `env_hint`，永不單獨授權 `reconcilable=True`**（三類 un-reconcilable 本就 dry-run only）。做在 operator 端 API 還是 Odoo 端 = `DP-DIFF` | **R3** |
| ownership marker 雙證（分清 env vs ownership） | **只有帶真 owner marker 的資源可升 `reconcilable=True`** — 目前**僅 SA route DNS comment**（`cloudflare.py:286-287`）。CS shared-tunnel DNS / ns 即使有 env-attribution，在 `DP-MARKER`（owner marker 載體 + backfill 存量）定案前一律 `reconcilable=False`（dry-run）。「content 指 shared tunnel」任何人可偽造 → 是 env-attribution **不是** ownership | **R1/R2** |
| protected denylist（tripwire，非主擋線） | apex / MX / 官網本體 / 行銷子網域 / shared tunnel 本體（`55953bfa`/`328c3a28`）硬編。**角色 = assert tripwire**：若某 protected 名稱意外被判 `reconcilable=True` → 立即 abort + fail-loud 告警（代表雙證有 bug）。只清指向 shared tunnel 的 DNS/ingress rule，**永不刪 shared tunnel 本體** | **R5** |
| ingress rule 清理精確比對 | shared tunnel ingress 是含多 rule 的單一物件（無單刪 API，GET→filter→PUT，`cf-ns-orphan` L70「三處一起清」/ L92「清理操作紀律」）。GET-filter-PUT 須**精確比對完整 hostname（非前綴）** + 對照該 rule 的 stored subdomain 確在孤兒清單；PUT 前 diff 出「本次將移除的 rule 集合」供 dry-run 顯示與人工確認。防 Q1 在 ingress 層的變體（誤刪其他活躍/非-paas rule） | **R5** |

### C.2 TDD 重點
- **env-pin negative test（擋假綠）**：`deploy_env` param **不存在** → self-check **abort**（**非** fallback 成 prod）；`PAAS_ENV` 空/漏注入 → operator reconcile 路徑 fail-closed（非 ns-label fail-open）。
- **Q1 防線（證明 R1 結構擋住、不依賴 denylist 列舉完整）**：注入「**未在 denylist、且非 paas 前綴**」的假行銷子網域（如 `promo2026.woowtech.io`）→ 斷言 `reconcilable=False`。再注入 apex/MX/官網 → 全部不入 reconcilable。
- **Q2 防線**：① prod DB 不可達 / table-not-exist / 回空集 → dedicated tunnel 判定 skip（**不誤判孤兒**，「可達 ≠ 對該 model 有權威答案」）② `PAAS_ENV` 漏注入 → self-check abort ③ legacy 無 token 的 prod 資源在 stg 跑 → 標 `unknown` 不刪。
- **三類 un-reconcilable 鎖死**：dedicated tunnel / Access app / legacy 一律 `reconcilable=False`，即使前綴對也不升級；prod-DB-diff 對 dedicated tunnel 只填 `env_hint` 不授權刪。
- **ownership vs env 分離**：CS shared-tunnel DNS 即使 content 指對 shared tunnel（env-attribution 成立），在 DP-MARKER 前 `reconcilable=False`（無 owner marker）；SA route 帶 DNS comment owner → 可 `True`。
- **denylist tripwire**：人為構造一筆 protected 名稱被誤升 `reconcilable=True` 的情境 → 斷言 abort + 告警（驗 tripwire 真的會炸，不是擺設）。
- **ingress 精確比對**：shared tunnel config 含 1 孤兒 rule + N 活躍 rule → 斷言只孤兒 rule 被 filter、活躍 rule 原封回寫；前綴撞名（同 ws_hash 前綴）不誤刪。
- **label/comment 值過 `validate_label_value`（`helm.py:93`，禁 `:`）**：拆兩層並標環境 — ① 純 Python 邊界測試（含 `:`/epoch 秒 #207/#208 場景，任何環境可跑）② **真 kubectl apply 非法 label 被 server 拒且錯誤不被吞**（標 `@integration`，須先確認 docker-desktop kind 的 label admission 是否等價 woow-k3s，否則上 woow-k3s）。**不可用①冒充②**關閉 #208 盲區。
- **policy 孤兒不信 DB 白名單（收窄）**：以 CF app 實際引用（`GET access/apps/{id}/policies`）為 ground truth（`sync_access_app` resync 重建 policy 造成 DB `cf_policy_id` 漂移）+ 409 in-use 兜底。**但「不信 DB」僅對 policy 孤兒成立**；dedicated tunnel env 分類反而**要靠** prod DB diff（一概不信 DB 會錯失此路徑）。漂移風險一般化到任何 resync 重建過的 stored-id（`dns_record_id`/`access_app_id`）。

### C.3 跨 repo lock-step / 驗證點
- 主體純 Odoo，**唯 TS-4 S1（`shared_tunnel_id`）動 operator**（見 §C.4，自帶 build + Fleet + stg→prod gate，與 inventory endpoint 首個 operator image 同批）。`DP-SM-COMMENT` 落地時走獨立 operator 變更。env-pin 要求 operator `PAAS_ENV` ↔ Odoo `deploy_env` 兩端一致（已有 F-ENV-1b SOP）。
- stg live dry-run：注入假 apex / 假 prod dedicated tunnel / 假非-paas 行銷子網域 → 確認 inventory 標 `un-reconcilable` / `protected` / `cross-env-skip` / 結構性 `reconcilable=False`，非 reconcilable。

### C.4 TS-4 修訂（2026-06-26，對抗式設計批判後 — 3 critics 全判 needs-revision，本節為權威）

對抗式設計審查（workflow `ts4-recon-critique`，5 recon + 3 critic）抓到原 TS-4 草案的數個 HIGH 缺陷，TS-4 切分與守衛機制據此修訂如下（與上方 C.1/C.2 不衝突，是其落地細化）：

1. **shared tunnel 保護權威 = operator-reported（HIGH，最關鍵）**：原草案硬編 `SHARED_TUNNEL_IDS={stg:'55953bfa…',prod:'328c3a28…'}` 並對 `CfResource.id` 做 `==`。但 pitfall doc 的 `55953bfa`/`328c3a28` 是 **8-char 簡寫**，`CfResource.id`（來自 CF API `item["id"]`）是**完整 32-char UUID** → exact match **永不命中** → denylist + tripwire 靜默 **no-op** → shared tunnel 本體（必出現在 `list_all_tunnels` 右集、不在 SM/SA 左集）落回泛用 `reason='結構孤兒候選'` → Phase D 人工照單刪 = **cf-ns-orphan 事故重演**。**修正**：權威來源改 **operator inventory 回 `shared_tunnel_id`**（operator runtime `cloudflare.py:40 self.tunnel_id` ← `config.py:96 CLOUDFLARE_TUNNEL_ID`，跑 `list_all_tunnels` 的同一 service，隨環境/輪替自動對齊）。reconciler 動態把 `inventory.shared_tunnel_id` 標 `reconcilable=False, reason='protected: operator-reported shared tunnel 本體'`。**operator 缺報 `shared_tunnel_id` → fail-loud abort**（無法保證 shared tunnel 不被誤判，不靜默放行）。硬編完整 UUID 的 belt-and-suspenders cross-check（operator 值須 == `SHARED_TUNNEL_IDS[deploy_env]`，可抓「兩端一致地接錯 CF account」）**defer**：需先從 live cluster 取得真實完整 UUID（本機無法取）→ follow-up。
2. **`env_hint=='unknown' ⟹ reconcilable=False` 硬不變式（HIGH）**：reconcilable predicate 與 env_hint 原本獨立。prod（空 token）與 legacy 名稱 **byte-identical**（`naming.py:36-37` prod→空 token）→ 都 parse 成無 token → `env_hint='unknown'`；cross-env 守衛只對 explicit token≠pinned 生效、對 unknown **不 fire** → 真 prod 資源在 stg-pinned 對帳下唯一防線是此耦合。**修正**：predicate 末端強制 `reconcilable = reconcilable AND env_hint!='unknown'`；tripwire assert「無候選 (reconcilable=True ∧ env_hint=='unknown')」。**推論並明文凍結**：prod-pinned 對帳因 prod 空 token 恆落 unknown → **prod 永不可 auto-reconcile**（故意 safe，禁後續用 inventory cross-check「修正」成可刪）。
3. **`parse_paas_name` regex 用 literal `(stg)` 白名單（HIGH）**：原草案 `^paas-(?:([a-z0-9]+)-)?(sa|sm|cs)-` over-accept 非白名單 env（`paas-foo-sa-`→env_hint='foo' 違反 S2 才建立的 env∈{prod,stg} 不變量），且 `_env_token` 可吐多 label token（`'foo bar'→paas-foo-bar-sa-`）`[a-z0-9]+` 吃不到 `-` → 整條 match 失敗靜默 unknown ⇒「鏡像 `_env_token`」宣稱不成立。**修正**：regex `^paas-(?:(stg)-)?(sa|sm|cs)-`（literal 白名單；prod 由空 token 落 unknown）；註解改述「鏡像 `_env_token` 對 {prod,stg} 白名單輸出」。`cs` 分支對現行 3 類 inventory（tunnel/access_*）是 DNS 預留 dead-branch，註記。
4. **in-flight provision 為一級誤刪向量（HIGH，文件化）**：provision「先建 CF→再寫 Odoo stored-id」窗內活躍資源以正規 `paas-{env}-{type}-` name 出現在右−左 = 候選，且 S3 給它 confident `env_hint=pinned`（非 unknown）→ 反而更符合任何「confident env→可刪」predicate，方向與守衛相反。**首版結構全 un-reconcilable（恆 False）已擋住刪除**；明文記為一級向量，未來開 reconcilable=True 前須補：① 無法被 in-flight 偽冒的 ownership marker（DP-MARKER）② first-seen grace（候選需連續 N 次對帳出現，跨度 > 一次 provision 最長落地，需 Phase D 持久化）。
5. **両端一致須擴 `_parse_inventory`（MEDIUM）**：現行 `_parse_inventory` 只回 `(resources, partial)`、丟棄 `paas_env`、必填鍵不含它（與自身 malformed fail-loud 哲學矛盾）。**修正**：必填鍵加 `paas_env`+`shared_tunnel_id`（缺/非 str → `RuntimeError` fail-loud）；回 `(resources, partial, paas_env, shared_tunnel_id)`。`reconcile_orphans` 先讀 `deploy_env`（不帶 default、falsy→abort）→ 打 operator → assert `paas_env==deploy_env` 否則 abort。此 check 語意是「兩端 env 標籤一致」（shared CF account 下 operator 不依 env 過濾資源，cross-env 資源照常出現 = S3 分類的事），**非**資源範圍/外洩防護。
6. **C.2 policy resync 漂移補償（MEDIUM）**：access_policy 已在 inventory；resync 重建 policy → 新 `cf_policy_id` 活躍卻不在左集 → 誤標孤兒（false positive）。真正交叉驗證（CF app 實際引用 ground truth）依賴 operator `list_policies_of_app` app→policy 映射，**現行 `InventoryResponse` 未帶 → 與 DNS 同級 defer**。TS-4 立即補償：policy kind 候選 `reason` 加註「可能為 resync 漂移後仍活躍的 policy，人工刪前須核對 CF app 實際引用」。
7. **no-silent-drop 須下游呈現 invariant（MEDIUM）**：cross-env 候選「保留 + reconcilable=False」的 no-silent-drop 保證，**只在報表/UI 不以 reconcilable 過濾候選時成立**（否則上游保留、下游濾掉＝假性 no-silent-drop，orm-test-green-endpoint-leak 同家族）。Phase D 報表層 invariant：不得以 reconcilable 過濾、須全列呈現附 reason+env_hint。
8. **access app env_hint 為 low-trust（MEDIUM）**：Access app CF name = `subdomain_prefix（user 可控）OR canonical_name`；user 可把 prefix 命名成 `paas-stg-sa-` 形狀 → 假 env_hint。目前 access app 恆 un-reconcilable 僅 cosmetic，但 latent coupling：**明文 subdomain_prefix-sourced env_hint 永不可作為升 reconcilable=True 證據**。
9. **TS-4c/d True-path 為 synthetic-only（LOW，誠實標註）**：首版三類全 un-reconcilable + DNS 未進 inventory → reconcilable 對「真實 inventory 項」恆 False；reconcilable=True 分支 + tripwire 的「意外 True」只能靠測試合成注入觸發（未來防禦腳手架）。pipeline 順序：**protected-override 必在 predicate 之後**（即使現在 predicate 不產 True，順序是正確性結構一部分）。`reconcile_response_golden.json` 在 TS-4 後仍全 False。
10. **範圍澄清（LOW）**：`validate_label_value`（`helm.py:93`）守的是 label/comment/owner-marker **寫入**路徑，TS-4 四片純-Odoo 分類、不寫任何 marker → **正確的範圍外非遺漏**（marker 寫入屬 DP-MARKER/Phase E）。apex/MX 是 DNS 類、DNS 未進 inventory → 與 shared-tunnel（tunnel kind 已在 inventory、real/now、必測）**拆開**，隨 DNS 一起 defer（常數可佔位但首版不寫測試、不宣稱守住）。

---

## Phase D — dry-run endpoint + UI/cron 報表

> **不動 `extra/paas-operator/`**（選項 A 純 Odoo；報表若需 operator 唯讀彙整 endpoint 才動）。一般 addon 部署。**這是 DP5 拍板點。**

**目標**：把 dry-run 孤兒 inventory 暴露給人。

### D.1 改動站點

| 站點 | 改動 |
|------|------|
| `src/models/`（新 `woow_paas_platform.reconcile_report` 或 transient） | 存/呈現 dry-run inventory；欄位 `kind / id / name / env_hint / reconcilable / reason / scanned_at` |
| `src/data/`（新 cron，選項 A） | `ir.cron` 低頻反向對帳（**只報告不刪**），與 `subscription_lifecycle_cron` 區隔（L4 是較低頻反向 net，不取代日常 subscription cron） |
| `src/controllers/` + `src/static/src/paas/`（選項 A） | `/woow` 後台 UI 唯讀報表頁（OWL；**OWL 假綠紀律**：component prop 不加 `t-att-`、無 `.enter` modifier，headed 瀏覽器實走 console 零容忍） |

### D.2 TDD 重點
- inventory 純唯讀：grep 報表/cron 路徑**零** `delete_*`/`uninstall`/`unlink CF`（並由「機械守線」節的 grep gate 兜底）。
- **人工版 TOCTOU（R4，首版人工刪同樣有時間窗）**：每筆候選顯示 `scanned_at`；UI/手冊明文要求「人工刪除前對該筆**即時重查 CF 現況 + Odoo stored-id 雙查**」，或提供一鍵「重新驗證此筆是否仍為孤兒」（唯讀重算單筆、不刪）。防「報表上當時是孤兒、人去刪時已被新 provision 重用同 subdomain（hash 碰撞/retry 重建）→ 刪活躍資源」。
- **`DP-RACE` 前移**：cron 與 `subscription_lifecycle_cron` 在同一 CF account 操作 → 首版雖零自動刪 race 低，但「人工刪 + 既有 `subscription_lifecycle_cron` 的自動刪（非 L4）」同 account 仍可能互踩，手冊須要求人工刪前確認該筆不在另一 cron 處理中（Phase E 接管自動刪前須補 serialization/lease guard）。
- **OWL 報表 merge release gate（升格，非一句測試重點）**：merge 前用 `playwright-cli --headed` 開 `/woow` reconcile 報表頁，**console error 零容忍 + 截圖附 PR**，零 error 才可 merge。文件承認此為**人工守線**（本 repo 無 JS 測試框架 = 無自動回歸）。

### D.3 驗證點
- stg `/woow` 後台看到 inventory，三類 un-reconcilable / protected / cross-env skip / 結構性 `reconcilable=False` 清楚標記；人工照 inventory 手動清理（＝2026-06 人工事故做法，但有自動發現清單 + 單筆即時重查）。

---

## Phase E — ns/PVC 反向 GC

> **動 `extra/paas-operator/` + cross-repo cronjob template → build + Fleet bump + lock-step。** operator rollout 兩拍（stg→prod 獨立 merge）。

**目標**：補 **namespace 無 TTL** 的反向清（L4 真待建項）。PVC GC 已由 #207/#208 修復，L4 只涵蓋殘餘 Released PV + 底層 Longhorn volume 計費。租戶 ns 現只有 `paas-managed`+`env` label + ResourceQuota，**無 TTL / owner annotation / 反向對帳**（對比 dev-sandbox 有 TTL）。

### E.0 阻塞前置（第一步，照 cross-repo-artifact-landing 紀律）
- **勘查 PVC `orphaned-at` deleter cron 實體落點**（疑 `paas-app-bundle-cd` Fleet 或 cluster ops repo，**不在本 repo**）：依「**修改誰的 deployment / 哪個 Fleet bundle 監控此 cron**」定位，**非按 feature 名**。定位完成才動 operator code。

### E.1 改動站點

| 站點 | 改動 |
|------|------|
| `extra/paas-operator/src/services/helm.py:679`（`create_namespace`）+ `api/agent_runtimes.py:81`（`def _ensure_namespace`） | 建 ns 時加 `owner`（workspace/record-id）+ TTL annotation（值過 `validate_label_value`） |
| `extra/paas-operator/src/services/helm.py` / cloudflare | `list_all_namespaces`（`kubectl get ns -l paas-managed=true`）→ 對 Odoo `helm_namespace` 左集取差集；殘餘 Released PV / Longhorn volume 反向掃 |
| **cross-repo cronjob template** | ns GC cron manifest 與本 repo operator code **lock-step**（漏一邊重炸 fleet-split / 部署漂移；operator image bump 與 cron template 變更須**同一批次驗到端到端**） |

### E.2 TDD 重點
- ns 反向 GC 仍 dry-run-only；ns label 可靠性受 **F-ENV-3 open** 影響（marketplace `--create-namespace` 繞過 `build_managed_ns_labels` 建 BARE ns，`releases.py:58`（`create_namespace=request.create_namespace`）→`helm.py:261` 加 flag 但不帶 label）→ 測試須涵蓋「**無 label 的 BARE ns 不可誤判 / 標 unknown**」。
- label/annotation 拆兩層 + 真 kubectl integration test（同 C.2，unit mock subprocess 遮蔽 kubectl 自身驗證 = #208 盲區）。
- 殘餘 PV/Longhorn：對 #207/#208 已修的 PVC path **不重複**，只測殘餘。

### E.3 跨 repo lock-step / 驗證點
- operator image（ns owner/TTL + list-all-ns）build + Fleet bump（stg→prod 兩拍）；ns GC cron template 與本 repo operator lock-step。
- **operator ServiceAccount cluster RBAC 盤點**（見「prod 前置硬阻塞清單」節）：`namespaces list` / `persistentvolumes list` / Longhorn volume read，未就位則 prod 該類 skip + fail-loud 標 reason（**不回空候選**）。
- stg：ns 帶 owner/TTL，list-all-ns 對左集取差集列出孤兒 ns（dry-run）；殘餘 Released PV 入 inventory。

---

## prod 前置硬阻塞清單（獨立於 stg 驗證點）

| 項目 | 要求 | 影響 Phase |
|------|------|-----------|
| **CF 帳戶級 token scope** | `Access:Apps&Policies(Read)` + `Tunnel:Read` + `DNS Read`（帳戶級非區域級）須在 **prod Fleet secret 就位且 live 驗 200（非 403）** 才可 prod 跑 reconcile。prod/stg 共用同一 CF account（`cf-ns-orphan` 已載） | A（前置） |
| **operator ServiceAccount cluster RBAC** | `namespaces list` / `persistentvolumes list` / Longhorn volume read。現有 operator SA RBAC 未盤 → prod 上不足同樣 fail-closed 成空清單 | E（前置） |
| **跨 A/E 共用 invariant** | `403` / RBAC 不足 → **fail-loud 標 `reason`，絕不回空候選**（與權限不足無法區分時，「無孤兒」是危險假象） | A + E |
| **operator rollout 兩拍** | stg Fleet bump 驗證通過 → prod 為**獨立第二次 merge**（`paas-app-bundle-cd/paas-operator/values-prod.yaml`，無自動 promote） | A + E |

---

## 機械守線（貫穿 5 Phase，CI fail-closed 非人工自律）

- **reconcile 路徑 pre-merge grep gate**：在 `src/services/reconciler.py`、operator `src/api/reconcile.py`、cron/報表檔上掛 pre-merge test，assert 路徑內**零** `delete_*` / `uninstall` / `kubectl delete` / `unlink`。首版「零刪除」從紀律變 CI fail-closed（尤其 Phase E 動 operator helm，delete 字樣語意上更近，最易漏）。
  - **TS-1 已實作 operator 端**（`extra/paas-operator/tests/test_reconcile_delete_gate.py`，2026-06-25；對抗式審查後精煉）：① 用 `tokenize` 先抹掉「註解 + 所有字串字面值」**只掃可執行 code** → reconcile.py 合法的免責 docstring / runbook 註解 / dry-run log·reason 字串 / `deleted_at` 軟刪過濾 / `orphan_candidates` 命名一律不誤判；② pattern 修 `\bdelete_` 漏 leading-underscore 私有刪除（`_delete_access_app` / `_delete_dns_record_by_id`，CF 真實刪除向量）→ 改 `(?<![A-Za-z0-9])_?delete_`；③ 擴及「無 delete 字眼但會抹除資源」的破壞向量：`update_tunnel_config`（PUT-erase ingress）/ `configure_tunnel_ingress([])` / `reconcile_tunnel_dns([])` / `scale_(deployment|statefulset)`（scale-to-zero）/ `rollback` / `subprocess`（直接 shell out）。**本質是 fail-closed tripwire 非完備證明**（只認 operator 既有破壞動詞；新動詞須同步補 pattern；偵測非虛設由 detector self-test 擔保）。權威 pattern 清單以該 test 檔為準，避免雙寫漂移。Odoo 端（`src/services/reconciler.py` + cron/報表）的對等 gate 於 TS-3 / Phase D 檔案落地時補。
- **`dry_run=False` 422 endpoint test**：**方向 A 下再延後**。原計畫掛在 operator `ReconcileRequest{dry_run}` 上，但方向 A 的 operator inventory 端點是 **GET / 唯讀 / param-free**（無 `dry_run` 可傳，POST 同路徑 → 405 已測），故 operator 端無 422 可守；此 gate 隨「未來 server-side action endpoint（接 stored_ids 真動手）」一起落地，在 DP-MARKER 拍板前不存在。**operator 唯讀不變式改由 delete-gate（grep）守**（已涵蓋 `src/api/reconcile.py`，對抗式驗證過）。
- **兩個 golden 契約（方向 A，BUG-8 防護）**：
  - **`InventoryResponse`（operator→Odoo wire，TS-3a）**：operator 回的 raw 右集（`{paas_env,partial,failed_kinds,resources:[{id,name,kind}]}`）。golden = `tests/fixtures/inventory_response_golden.json`；operator 端 `test_reconcile_inventory_schema.py` 斷言「序列化 == golden」+「`CfResource` **不得**帶 env_hint/reconcilable/reason」（intent-lock 方向 A：operator 不分類）。Odoo client 端（TS-3d）用同一份 golden 反序列化讀回 + stg smoke 實打一次。
  - **`ReconcileResponse`（Odoo `reconciler.py` 產出 → 報表/UI，TS-2 既有 golden）**：分類後 inventory（`orphan_candidates:[{id,name,kind,env_hint,reconcilable,reason}]`）。golden = `tests/fixtures/reconcile_response_golden.json`（operator 端仍持 schema 定義 + golden，Odoo `reconciler.py` 產出鏡像此 shape）。
  - 任一端改欄位 → 對應 golden 對不上 → 其一必紅。比「部署順序紀律」實質（後者只防 404，防不了 payload shape drift）。

---

## 守線提醒（medium/low，收於此節）

- **custom_domain 推理缺口封閉**：若 deploy 不為 `custom_domain`（`cloud_service.py:137`）建 CF DNS，design 須顯式註記「使用者自管、不在 reconcile 範圍」+ 列 denylist 兜底，避免未來啟用時靜默變孤兒誤判源。
- **SA route in-flight 覆蓋**：除 stored-id walk 外，補 `subdomain_prefix`→hostname 第二鍵（已編入 B.1），覆蓋 sync 半完成 / id 未回填的活躍 route。
- **DB↔CF 分歧殘留（`DP-DBCF-DRIFT`）**：sync 半途 rollback（best-effort cleanup 只 catch `CloudflareException` → httpx 暫態錯逸出 → savepoint rollback → 半完成 CF 物件殘留）L4 如何判定，未設計；首版 dry-run only 風險低，記入 follow-up。
- **`_get_paginated` 的 `result:null` 截斷盲點（low，TS-3a 對抗審發現）**：`cloudflare.py` `_get_paginated` 用 `batch = data.get("result") or []`，把 `success:true` + `result:null` 當「空頁」。正常 CF 不會這樣回，但**若** CF 在 `total_count>0` 時回 `result:null`（契約違反），空頁 fallback 會提早停 → 該類**靜默少撈**而 `partial` 仍 `False` → 下游漏報孤兒（少報非誤刪，dry-run 風險低）。屬 TS-2 既有 `_get_paginated` 行為（非 TS-3a inventory 端點引入），未在本片改；建議 follow-up 讓 `_get_paginated` 對「`result` 鍵缺失/`null` 但 `total_count>0」fail-loud raise（與「`result:[]` 合法空」區分）。
- **kind vs woow-k3s admission 等價性**：MEMORY 已記 docker-desktop 是 kind、部分 kubectl 行為與真 cluster 不同；`@integration` 的 label server-side 拒絕測試須先確認 kind admission 等價，否則上 woow-k3s（不可用 kind 冒充 #208 守線）。
- **ingress GET-filter-PUT 一般化**：把「刪前 GET 驗證」具體化到 ingress rule（精確 hostname + dry-run diff + 回寫驗證），已編入 C.1。
- **OWL 報表人工守線**：本 repo 無 JS 測試框架 = 無自動回歸，merge gate 是 playwright-cli headed（已升格 D.2），文件須誠實承認無覆蓋。

---

## 待決策點（DP）

| ID | 決策點 | 現況/傾向 |
|----|--------|-----------|
| **DP5** | L4 介面：cron+`/woow` UI 報表（plan 建議）vs operator endpoint + Odoo client 手動觸發 | **Phase D 拍板**；傾向 A（自動定期對帳、零自動刪、人工確認 + 單筆即時重查） |
| **DP-PAGINATION** | CF pagination contract（`next_cursor` vs page offset） | **✅ RESOLVED（TS-2，2026-06-26，權威三來源：CF API docs + OpenAPI schema + cloudflare-python SDK 回傳型別互證）**：5 個 endpoint（`dns_records` / `cfd_tunnel` / `access/apps` / `access/apps/{id}/policies` / `access/policies`）**全部 `page`/`per_page` offset 分頁，無 cursor、無不分頁**，皆有 `result_info.total_count`（`cfd_tunnel` 缺 `total_pages`）。實作 `_get_paginated`：**`total_count` 主停 + 空頁 fallback + `max_pages` 防無限**（對「server 偷壓低 per_page」「缺 total_pages」皆 robust，與官方 SDK 行為一致）。per_page：`cfd_tunnel` 硬上限 1000；DNS 上限極大（用 1000）；**access 三端點硬上限 OpenAPI 未明訂 → 保守 50**（`total_count` 迴圈補齊，避免過大 per_page 首跑 400）。唯一 live-待確認：access per_page 真實上限（fail-loud、非靜默漏資料）。 |
| **DP-MARKER** | 正向 ownership marker 載體 + backfill（DNS comment / Access tags / tunnel metadata）+ 對存量補 env token | 未設計；**是開任何 CS `reconcilable=True` 的硬前置**；Access app/dedicated tunnel 結構性無載體 |
| **DP-DIFF** | dedicated tunnel env 判定（prod DB diff）做在 operator 端 API 還是 Odoo 端 | **範圍校正（2026-06-26）**：DP-DIFF **只**指「dedicated tunnel 的 prod-DB-diff env 分類」（TS-4/Phase C 層），**不**含 TS-3 的結構差集位置——後者 B.1 已定在 **Odoo `reconciler.py`**（方向 A：operator 唯讀回 `InventoryResponse`，Odoo 做集合差集），TS-3a operator 端已實作。本 DP 仍未定（prod-DB-diff 跑哪端）；prod DB「可達 ≠ 對該 model 有權威」，table-not-exist/空集→skip；**只填 env_hint 不授權刪**，留 TS-4 拍板 |
| **DP-SM-COMMENT** | 是否補 SM `create_tunnel` 傳 DNS comment（對稱 SA）讓 SM DNS 自證 env，還是以 tunnel 物件 name 為 env 判定起點 | 未定；F-ENV-7 後 SM tunnel name 已帶 token（零 operator 改動可判 env）→ **DP-SM-COMMENT 非必需**；若做則為**獨立 operator 變更**（不混 Phase C） |
| **DP-UNRECON** | dedicated tunnel / Access app 能否**永久定調只走人工** | 傾向永久人工，未拍板 |
| **DP-RACE** | L4 cron vs `subscription_lifecycle_cron` 同 account serialization / lease guard | 未設計；首版零自動刪風險低，**Phase E 接管自動刪前須補** |
| **DP-NS-TTL** | 租戶 ns 補 owner + TTL + 對帳 cron vs 維持純 record-driven | Phase E 拍板 |
| **DP-DBCF-DRIFT** | sync 半途 rollback 的 DB↔CF 分歧殘留 L4 如何判定 | 未設計，記 follow-up |

---

## effort 估計與建議切分

**進度（2026-06-26）**：✅ **TS-1 + TS-2 + TS-3 + TS-4 全數完成並合併 develop** = 首版範圍切分達標（「dry-run 反向 net + 誤刪守衛標記層」，零刪除）。可 `kubectl exec` 打 `GET /api/reconcile/inventory` + Odoo `reconcile_orphans(env)` 看**分類後**孤兒清單（env_hint / reconcilable / reason）。PR：TS-1 #234、TS-2 #235、TS-3a #236、TS-3b #238、TS-3c/d #239、TS-4 設計修訂 #242、TS-4 S1（operator shared_tunnel_id）#244、TS-4 S2/3/4（Odoo 分類層）#247。TS-4 經對抗式設計批判（§C.4 十條）+ 對抗式 code review（option A cross-env 結構 gate + 4 處硬化）雙重守線；49 Odoo 測試 + standalone 全綠。

| 切片 | 內容 | effort | 為何先做 |
|------|------|--------|---------|
| ✅ **TS-1：async 結構修 + 機械 grep gate**（Phase A.0 +「機械守線」節） | `pytest.ini` `asyncio_mode=auto` + 移 `--disable-warnings` + `filterwarnings=error` + collection meta-test + reconcile 路徑 grep gate | **S（半天）** | 純測試基建，無功能風險，立刻關掉 #208 假綠溫床，且是後續所有 async test 的地基 |
| ✅ **TS-2：list-all + 分頁修 + golden 契約**（Phase A.1/A.2 +「機械守線」節 golden） | `list_all_tunnels/access_apps/policies` + `_list_dns_records_by_content` 分頁 + golden fixture | **M** | list-all 是 0→1 的反向發現地基；分頁修直接消 `*-mcp` 漏 15 筆同型 bug；唯讀無刪除 |
| ✅ **TS-3：operator inventory 端點 + Odoo 左集 + 差集（dry-run only）**（方向 A：operator 唯讀回 inventory、Odoo 做差集）| **TS-3a** operator `GET /api/reconcile/inventory`（env-pin fail-closed + completeness gate）；**TS-3b** `collect_left_set`（SA 2 層 walk + CS MCP 重建 + SM）+ `reconstruct_mcp_subdomain` + Odoo delete-gate；**TS-3c/d** `compute_orphans`（右−左差集）+ client `get_reconcile_inventory` + `_parse_inventory` body fail-loud | **M-L** | 純運維價值（人工照單清，比 2026-06 純人工強）；MCP/SA walk 是最高衝擊正確性點。對抗式審查抓到並修：operator completeness gate 漏 timeout、delete-gate 盲抄 operator 漏 `uninstall_release`、diff body-shape silent-failure |
| ✅ **TS-4：env-pin self-check + 結構防線 + protected tripwire**（Phase C 核心，**不含 DP-MARKER / DP-SM-COMMENT**，修訂見 §C.4）| **4 片**：S1（#244 operator）`InventoryResponse.shared_tunnel_id`；S2（Odoo）env-pin self-check + 両端一致 + `_parse_inventory` 擴 paas_env/shared_tunnel_id 必填；S3（Odoo）env attribution（`parse_paas_name` literal-`stg` 白名單 + `classify_env_hint`）；S4（Odoo）`classify_candidates`：結構 allowlist + `unknown`/cross-env⟹reconcilable=False 雙硬不變式（code review option A）+ operator-reported shared tunnel protected + 三條 tripwire（S2/3/4 = #247）| **L** | Q1/Q2 防線，**仍 dry-run only**（三類全 un-reconcilable、恆 reconcilable=False）；shared tunnel 保護權威 = operator-reported（非 8-char 硬編＝對完整 UUID no-op 災難）。對抗式 code review 修：env-pin 假綠測試（assertRaisesRegex 釘源）、cross-env 結構 gate、tripwire pipeline wiring、resources 非 dict fail-loud |

**建議延後 / 需 DP 拍板才動**：
- **DP-MARKER backfill** — 開任何 CS 自動刪的硬前置，未設計前不碰自動刪。
- **Phase D UI/cron** — 介面 DP5 拍板後做；thin slice 期間可先用 `kubectl exec` 打 endpoint 看 inventory，不必先做 UI。
- **Phase E ns/PVC GC** — 動 operator + cross-repo cron lock-step + cluster RBAC，最重；E.0 勘查落點先行，code 與 cron 同批次驗端到端。
- **自動刪除（任何 Phase）** — 在 DP-MARKER + DP-RACE + DP-UNRECON 三者拍板前**不開**。

**切分建議**：TS-1→TS-2→TS-3 為「dry-run 反向發現 MVP」（約 1-1.5 週），可獨立交付運維價值（自動孤兒清單）；TS-4 為「誤刪守衛層」（再約 1 週），把 inventory 標記精準化但仍零刪除。Phase D/E 與自動刪除全部 gated on DP，建議獨立 follow-up 排程，不綁進 MVP。

**首版範圍切分建議**：首版 = TS-1+TS-2+TS-3+TS-4（dry-run 反向 net + 守衛標記，零刪除），明確**不含**自動刪除、不含 ns owner/TTL 寫入（Phase E）、不含 DP-MARKER backfill。CS/dedicated tunnel/Access app/legacy 首版全程 `reconcilable=False`，唯 SA route DNS（有 owner comment）可標 `reconcilable=True` 但首版仍不執行刪除（dry-run）。

---

**相關來源檔（絕對路徑，已驗）**：
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/extra/paas-operator/src/config.py`（`paas_env=""` fail-open，`:107`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/extra/paas-operator/src/main.py`（`:37-43` fail-OPEN 註解）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/extra/paas-operator/pytest.ini`（`--strict-markers --disable-warnings`、無 `asyncio_mode`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/extra/paas-operator/src/services/cloudflare.py`（list-all 全缺、`generate_subdomain:708-720`、`_list_dns_records_by_content:409` per_page=100）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/extra/paas-operator/tests/integration/README.md`（`:75-96` CF 零覆蓋 + `:91` gated marker 計畫）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/src/models/res_config_settings.py`（Odoo 欄位 `woow_deploy_env`〔Selection，:36-45〕，`config_parameter` = `woow_paas_platform.deploy_env`，`default='prod'` 在 `:40`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/src/models/security_access.py`（SA 2 層 walk `:778-779`、`subdomain_prefix` UNIQUE `:844-857`、DNS 對拍 `:557-563`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/src/models/security_access_access_policy.py`（`cf_policy_id:69`、binding 走 `route_id` 非 `security_access_id` related `:38-41`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/src/models/cloud_service.py`（`subdomain:133`、`custom_domain:137`、`helm_release_name:147`、`helm_namespace:151`，無 dedicated tunnel）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/src/models/smart_home.py`（`tunnel_id:79`、`tunnel_name:84`、`subdomain:105`、`deploy_env` 預設 `'prod':155`、DNS 無 comment `:181`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/src/services/paas_operator.py`（`reconcile_orphans` 待新增，~`:625`）
- `/Users/eugene/Documents/woow/AREA-odoo/woow-addons/.maestri/floors/woow_paas_platform--fixname-system/.claude/skills/build-operator/SKILL.md`（Fleet repo = `paas-app-bundle-cd`，pin `paas-operator/values-prod.yaml`，`:94`）
