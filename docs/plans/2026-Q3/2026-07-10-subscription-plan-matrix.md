# WOOW PaaS 訂閱方案總表（2026-07-10）

> **權威來源**：2026-07-10 拍板定案（蓋過 2026-07-01 定價會議 doc 衝突處）。
> **合併來源**：CS／SH／AI 三份分表草稿＋repo 現況掃描；**SA＋AC 草稿缺件（null）**，該節由拍板定案＋seed 現況重建（重建推導處均有標注）。
> **現況基準**（最終審查已逐筆對 `src/data/subscription_products.xml` 驗證）：**20 筆 plan＋20 筆 product**，全包在單一 `<data noupdate="1">`（`subscription_products.xml:3`）；版號 `18.0.1.0.50`（`src/__manifest__.py:3`）；本波 PR base＝**.51**。
> **全域規則（拍板）**：年繳＝月繳×10（機械先 seed、後台可改）；trial 統一 7 天（現況 seed 已全為 7 天，無異動）；每個「檔位×週期」＝一筆獨立 `sh.subscription.plan` record；`sh_override_product=True` ⇒ `plan.sh_plan_price` 為權威價、`product.list_price` 僅影響購物車／發票顯示，**兩者必同步改**。

---

## 0. 審查裁決記錄（草稿間矛盾與最終裁定）

| # | 發現 | 裁決 |
|---|---|---|
| R1 | **CS 草稿宣稱「postgresql／redis／anythingllm 仍 placeholder plan」**——經查 seed **無任何 redis plan/product**（redis 僅有 template `cloud_app_redis`，`cloud_app_templates.xml:112`，從未 seed 過 plan） | 未涵蓋 placeholder ＝ **postgresql 2＋anythingllm 2＝4 筆 plan**（非 6）；redis「有 template 無 plan」列入開放問題 Q9 |
| R2 | CS 草稿開放問題 #1 筆誤「Odoo 499→4990→…」 | 正確為：Odoo 月 499→1000、年 4990→10000 |
| R3 | **SA＋AC 草稿缺件（null）** | 由拍板＋seed 現況重建（§5）。經查 SA 現況為 **4 筆 plan（Entry 100/1000、Pro 300/3000）且 5 個 `sa_*` quota 欄位已 seed**；目標映射＝Entry 對→5 Application、Pro 對→10 Application，全 reprice、**無新 SA plan**；AC 席數建模與 `sa_max_access_emails` seed 值未拍板細節列 Q3／Q6 |
| R4 | AI 草稿稱 `service_type` backfill「涵蓋全 seed 20 筆」；CS/SH 草稿各自表述 | 統一口徑：**既有 20 筆 plan 走 migration backfill**；新增 14 筆由 XML 自帶，無需 backfill |
| R5 | 三份草稿各自重複「全域常數 11 欄」「noupdate 機制」段落 | 去重上收至 §2，各分表僅引用 |
| R6 | CS 草稿 #5 與 AI 草稿 Q3 各自主張「保留舊 xmlid 當低階檔（T1）」 | **採納為全案原則**：既有 xmlid 一律原地沿用為最低檔（改 name 不改 xmlid），避免動既有訂閱外鍵；SA 亦比照（Entry→5 App、Pro→10 App） |
| R7 | 價格 vs 拍板一致性總查 | **零衝突**：Hermes T3=6000（非會議 6500）✓、MCP 統一 3000 ✓、SH 150/400 ✓、SA 1000/2000 ✓、CS 三階 1000/3000/5000 ✓、年繳×10 ✓、trial 7 天 ✓ |
| R8 | 改價撞價陷阱（呼應「SA 年繳撞名」疑點，最終審查坐實） | SA Entry Yearly **舊價 1000＝SA 5 App 新月價 1000**；SA Pro Yearly **舊價 3000＝MCP 新月價 3000**；SH Yearly 舊價 1000 亦撞 → **migration 一律以 XML id 定位，嚴禁用價格／名稱 match** |
| R9 | SH 草稿引用的 `mcp_integration` 欄位與字面值 | 已驗證：`sh_subscription_plan_sh_mcp.py:14` 為 `fields.Char`、default `''`、字面 `'ha_mcp_tools'`，.50 已 landed ✓ |

---

## 1. 跨服務摘要表（服務×檔位×月價×年價）

| 服務 | 檔位 | 月價 | 年價（×10） | 現況（月/年） | 異動類型 |
|---|---|---:|---:|---|---|
| CS — Odoo | T1（1–3 人） | 1000 | 10000 | 499 / 4990 | 改價＋改規格＋改名（沿用 xmlid） |
| CS — Odoo | T2（3–6 人） | 3000 | 30000 | — | **新增** |
| CS — Odoo | T3（6–10 人） | 5000 | 50000 | — | **新增** |
| CS — n8n | T1（1–3 人） | 1000 | 10000 | 199 / 1990 | 改價＋改規格＋改名（沿用 xmlid） |
| CS — n8n | T2（3–6 人） | 3000 | 30000 | — | **新增** |
| CS — n8n | T3（6–10 人） | 5000 | 50000 | — | **新增** |
| SH — Smart Home Connect | 純 remote access | 150 | 1500 | 100 / 1000 | 改價（沿用 xmlid） |
| SH — Smart Home + MCP | +HA MCP（`ha_mcp_tools`） | 400 | 4000 | — | **新增** |
| SA — Security Access | 5 Application（`sa_max_routes=5`） | 1000 | 10000 | Entry 100 / 1000 | 改價＋quota 3→5＋改名（沿用 xmlid） |
| SA — Security Access | 10 Application（`sa_max_routes=10`） | 2000 | 20000 | Pro 300 / 3000 | 改價＋改名（沿用 xmlid；routes 已=10） |
| AC — Access 席數加值 | 500／seat／月；內含席數 M=0 | 500/seat | —（未拍板） | — | **新增**（建模待定，Q3） |
| AI — Odoo MCP Server | 統一單階（1vCPU/1G/5G） | 3000 | 30000 | 499 / 4990 | 改價 only |
| AI — n8n MCP Server | 統一單階（1vCPU/1G/5G） | 3000 | 30000 | 499 / 4990 | 改價 only |
| AI — Hermes | T1（1–3 人） | 1500 | 15000 | 499 / 4990 | 改價＋改名（沿用 xmlid） |
| AI — Hermes | T2（3–6 人） | 4500 | 45000 | — | **新增** |
| AI — Hermes | T3（6–10 人） | 6000 ← 定案，取代會議 6500 | 60000 | — | **新增** |
| （參考）CS — PostgreSQL | placeholder，不在本波定案 | (99) | (990) | 99 / 990 | 不動（Q9） |
| （參考）CS — AnythingLLM | placeholder，不在本波定案 | (299) | (2990) | 299 / 2990 | 不動（Q9） |
| （參考）CS — Redis | **無 plan**（僅 template） | — | — | — | 不動（Q9） |

**淨變動統計**：

- 本波定案 plan 檔位：**30 筆 plan**（CS 12＋SH 4＋SA 4＋AI 10）＋對應 30 product
- **改價（沿用既有）＝16 plan＋16 product＝32 筆** migration force-write（CS 4＋SH 2＋SA 4＋AI 6）
- **新增＝14 plan＋14 product＝28 筆**（CS 8＋SH 2＋AI 4；SA 0），`-u` 自建、無需 migration；另 AC 席數 add-on 1 product 建模待定
- 不動 placeholder＝4 plan＋4 product（pg／anythingllm）；**本波不刪任何既有 record**
- PR 後 seed 總量：34 plan＋34 product（＋AC 待定）

---

## 2. 共通 seed 常數與 noupdate／migration 機制（三草稿去重合併）

### 2.1 全域常數（每筆新 plan 缺一即半筆 record）

plan 端 11 欄：`sh_duration=1`、`sh_unit=month|year`、`sh_override_product=True`、`sh_no_of_billing_cycle=-1`、`sh_never_expire=True`、`sh_trial=True`、`sh_trial_duration=7`、`sh_trial_unit=day`、`sh_start_immediately=False`、`sh_is_close_by_customer=True`、`sh_reminder=[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]`。
product 端：`type='service'`、`sale_ok=True`、`purchase_ok=False`、`sh_product_subscribe=True`、`sh_subscription_plan_id=ref(對應 plan)`。product record 的 model 用 **`product.product`**（現有 20 筆皆是；`sh_subscription_plan_id` 定義在 `product.template`、由 delegation 繼承，seed `:394` 註解已標明），勿改用 `product.template`。

### 2.2 noupdate 行為（plan seed 側）

全檔 `<data noupdate="1">` ⇒ `-u woow_paas_platform` **只建新 xmlid record、不覆寫既有 record 任何欄位**：

1. 16 筆改價（＋改名＋SA quota 調整）**必走 migration**（`src/migrations/18.0.1.0.51/post-migrate.py`，命名循 `.42`/`.43` 現例）。
2. 14 筆新增 `-u` 自建，無需 migration。
3. 新欄位（`service_type`、`sa_max_access_emails`）加在 model 上屬 schema 變更，`-u` 自動建欄；但**既有 20 筆 record 的欄位值** noupdate 不會從 XML 回填 → 同支 migration backfill。
4. **定位鐵律**：migration 一律以 XML id（`env.ref('woow_paas_platform.plan_…')`）定位，嚴禁價格／名稱 match（撞價實例見 R8）。

### 2.3 sticky noupdate（template seed 側，另一座山）

`cloud_app_*` template seed 在先裝 DB 帶 sticky `ir.model.data.noupdate=True`（`cloud_app_template.py:232` `_heal_sticky_helm_value_specs` docstring）；該 heal 只清 flag＋重寫 `helm_value_specs`、**不碰 `helm_default_values`、不碰 M2M**。因此「Odoo workers tier-aware」與「template `subscription_plan_ids` M2M wiring」都需**額外 template-side heal/migration**，非 `-u` 自動（詳 §3.4、checklist B6/B7）。

---

## 3. 分表 — Cloud Service（Odoo＋n8n 三階）

> 範圍：本波 CS 三階只涵蓋 **Odoo 與 n8n**。同 category 的 postgresql／anythingllm 仍 placeholder、redis 無 plan（R1），皆不在定案內（Q9）。
> 拍板：各三階月 1000/3000/5000（1–3／3–6／6–10 人）；硬體規格卡人數、不鎖帳號；**T3 軟上限刻意≈T2**（RAM 持平、僅 +1 vCPU，多收的是人數餘裕與支援，非等比算力）。
> ⚠️ 規格欄位（vCPU/RAM/storage/workers）**目前 plan 上無處可掛**——表內「規格」欄＝目標部署值（draft），落地機制見 §3.3；**三階規格生效的硬前提＝修 Odoo template 硬寫 `workers=0` 為 tier-aware**（§3.4）。

### 3.0 現況（4 筆 placeholder，全單階）

| 服務 | XML id | 週期 | 現價 | product（list_price） |
|---|---|---|---:|---|
| Odoo | `plan_odoo_monthly`（`:94`） | 月 | 499 | `product_odoo_monthly`（499，`:437`） |
| Odoo | `plan_odoo_yearly`（`:111`） | 年 | 4990 | `product_odoo_yearly`（4990，`:447`） |
| n8n | `plan_n8n_monthly`（`:24`） | 月 | 199 | `product_n8n_monthly`（199，`:397`） |
| n8n | `plan_n8n_yearly`（`:41`） | 年 | 1990 | `product_n8n_yearly`（1990，`:407`） |

現況部署規格與 plan 無關：吃 template 單一值（`cloud_app_odoo`／`cloud_app_n8n` 的 `helm_default_values`），詳 §3.3。

### 3.1 目標 — Odoo（6 plan）

| Plan 名稱 | XML id | 月價 | 年價 | 規格（目標部署值，draft） | product | 異動 |
|---|---|---:|---:|---|---|---|
| Odoo Cloud T1（1–3 人）Monthly | `plan_odoo_monthly`（沿用） | 1000 | — | 2 vCPU / 2Gi / 20G / **workers=2** | `product_odoo_monthly` 499→1000 | 改價＋改名＋掛 `cs_*` 規格（migration） |
| Odoo Cloud T1（1–3 人）Yearly | `plan_odoo_yearly`（沿用） | — | 10000 | 同上 | `product_odoo_yearly` 4990→10000 | 同上 |
| Odoo Cloud T2（3–6 人）Monthly | **新增** `plan_odoo_t2_monthly` | 3000 | — | 2 vCPU / 4Gi / 30G / **workers=3** | **新增** `product_odoo_t2_monthly`（3000） | 新增（`-u` 自建） |
| Odoo Cloud T2（3–6 人）Yearly | **新增** `plan_odoo_t2_yearly` | — | 30000 | 同上 | **新增** `product_odoo_t2_yearly`（30000） | 新增 |
| Odoo Cloud T3（6–10 人）Monthly | **新增** `plan_odoo_t3_monthly` | 5000 | — | 3 vCPU / 4Gi / 40G / **workers=4**（刻意≈T2：RAM 持平、+1 vCPU、+10G） | **新增** `product_odoo_t3_monthly`（5000） | 新增 |
| Odoo Cloud T3（6–10 人）Yearly | **新增** `plan_odoo_t3_yearly` | — | 50000 | 同上 | **新增** `product_odoo_t3_yearly`（50000） | 新增 |

### 3.2 目標 — n8n（6 plan）

| Plan 名稱 | XML id | 月價 | 年價 | 規格（目標部署值，draft；**n8n 無 workers 概念**） | product | 異動 |
|---|---|---:|---:|---|---|---|
| n8n Cloud T1（1–3 人）Monthly | `plan_n8n_monthly`（沿用） | 1000 | — | 1 vCPU / 1Gi / 10G | `product_n8n_monthly` 199→1000 | 改價＋改名＋掛規格（migration；**漲幅最大 199→1000**，grandfather 決策最敏感） |
| n8n Cloud T1（1–3 人）Yearly | `plan_n8n_yearly`（沿用） | — | 10000 | 同上 | `product_n8n_yearly` 1990→10000 | 同上 |
| n8n Cloud T2（3–6 人）Monthly | **新增** `plan_n8n_t2_monthly` | 3000 | — | 1 vCPU / 2Gi / 20G | **新增** `product_n8n_t2_monthly`（3000） | 新增 |
| n8n Cloud T2（3–6 人）Yearly | **新增** `plan_n8n_t2_yearly` | — | 30000 | 同上 | **新增** `product_n8n_t2_yearly`（30000） | 新增 |
| n8n Cloud T3（6–10 人）Monthly | **新增** `plan_n8n_t3_monthly` | 5000 | — | 2 vCPU / 2Gi / 30G（刻意≈T2） | **新增** `product_n8n_t3_monthly`（5000） | 新增 |
| n8n Cloud T3（6–10 人）Yearly | **新增** `plan_n8n_t3_yearly` | — | 50000 | 同上 | **新增** `product_n8n_t3_yearly`（50000） | 新增 |

### 3.3 規格欄位掛哪（現況矛盾＋落地建議）

現況「同 template、不同 plan → 不同規格」**無任何 schema 支撐**，實證鏈：

1. `allocated_vcpu/ram_gb/storage_gb` 建服務時直接抄 `template.min_*`（`controllers/paas.py:2171-2173`；`cloud_app_template.py:112-126`，純顯示、無 constraint）。
2. 實際 Pod 資源＝`template.helm_default_values.resources`（`cloud_app_templates.xml:170` odoo／`:59` n8n）；PVC＝`persistence.size`，皆單一值。
3. Odoo `workers` 由 template init script 硬寫 0（§3.4）。
4. `cloud_service.py` 對 plan **零欄位消費**；plan 欄位現況：SA 5 個、SH 1 個（`mcp_integration`）、**CS 0 個**。

⟹ 三階若只改價，**部署出來的規格三階完全相同**。

**建議＝Option B（plan 層 CS 規格欄位，推薦）**：新增 `src/models/sh_subscription_plan_cs_spec.py`（`_inherit='sh.subscription.plan'`，同 `sa_quota`／`sh_mcp` add-only pattern）：`cs_vcpu`(Integer)、`cs_ram_gb`(Float)、`cs_storage_gb`(Integer)、`cs_odoo_workers`(Integer，n8n 留 0)。部署路徑改 `paas.py:2171`（`allocated_*` 改讀選定 plan 的 `cs_*`，`template.min_*` 退為 fallback）＋`cloud_service.py` helm merge（`cs_*` overlay 進 resources／persistence.size／workers）。**前提**＝建立服務當下能解析「選了哪個 tier(plan)」（CloudService 現不存 plan_id、trial 期可能未綁訂閱）→ Q4。
**Option A（per-tier template，備選不推薦）**：每階一個 template（`odoo_t1/t2/t3`）各帶規格＋各綁該階 plan；零部署碼改動，但 template ×3、marketplace 三張卡、JSON 重複漂移、`slug=='odoo'` 假設要改。
補充：採 B 後 `template.min_*` 三欄顯示語意失真，應改由 plan `cs_*` 供顯示、`min_*` 僅留 fallback。

### 3.4 workers tier-aware 修正（Odoo 專屬、三階生效硬前提）

- 現況：`cloud_app_templates.xml:170` 的 `customPostInitScripts["odoo-conf-system.sh"]` 內 `sed -i '/^workers/d'` 後 `echo 'workers = 0'`——硬鎖單 process。不動這行則 T1/T2/T3 全 workers=0，**三階同效能、定價失去依據**。
- 修正：workers 由 plan 的 `cs_odoo_workers`（2/3/4）部署時 templated 注入（如 `echo "workers = ${ODOO_WORKERS}"`）。
- ⚠️ 此改動改的是 **template 記錄的 `helm_default_values`**，撞 §2.3 sticky noupdate：現有 heal 不碰 `helm_default_values` → 需**額外 template-side migration/heal**。
- 同段 script 另設 `limit_memory_hard/soft=0`（不限）＋`limit_time_cpu=60`／`limit_time_real=120`——workers>0 後記憶體不設防問題列 Q8。
- n8n 無此議題（Node 單 process，靠 CPU/RAM 卡效能）。

### 3.5 PG pod 不含在規格內的口徑

- **Odoo**：`cloud_app_odoo` 帶 `postgresql.enabled=true, architecture=standalone`（bitnami subchart）→ 每個 Odoo 部署恆有一顆獨立 PG pod。表列規格＝**odoo app 主容器**的 resources 與 app PVC；**PG pod 走 chart 預設、不計入對外標示規格、不計入 `allocated_*`**（現況 `allocated_*` 只抄 `template.min_*` 本就不含 PG，口徑一致）。
- **n8n**：預設 `config.database.type=sqlite`，無獨立 PG pod（`helm_value_specs` 有 optional postgres）。
- 建議此口徑寫進對外文案與 quota 帳；日後要含 PG 須另立口徑。

---

## 4. 分表 — Smart Home（150／400 × 月/年＝4 plan）

> 拍板：150＝純 remote access（`mcp_integration=''`）；400＝+MCP（`mcp_integration='ha_mcp_tools'`，欄位 .50 已落地）；前端四選項＝Monthly/年繳 × 兩檔。

| # | Plan 名（擬） | XML id | 月價 | 年價 | 關鍵欄位 | product | 異動／migration |
|---|---|---|---:|---:|---|---|---|
| 1 | Smart Home Connect Monthly（純 remote access） | `plan_smarthome_monthly`（`:164`，沿用） | **150** | — | `mcp_integration=''`；`service_type='smart_home'`（擬，Q2） | `product_smarthome_monthly`（`:477`）100→150 | **改價 100→150**：migration force-write plan `sh_plan_price`＋product `list_price` |
| 2 | Smart Home Connect Yearly（純 remote access） | `plan_smarthome_yearly`（`:181`，沿用） | — | **1500** | 同 #1 | `product_smarthome_yearly`（`:487`）1000→1500 | **改價 1000→1500**：同上（⚠️ 舊價 1000 撞多筆，xmlid 定位） |
| 3 | Smart Home + MCP Monthly | **新增** `plan_smarthome_mcp_monthly` | **400** | — | `mcp_integration='ha_mcp_tools'`（字面精確，供 `_get_mcp_integration()`→`to_dict()['mcp']` 與 `ha_api.get_tunnel_token()` 穿透） | **新增** `product_smarthome_mcp_monthly`（400） | 新增，`-u` 自建、無需 migration |
| 4 | Smart Home + MCP Yearly | **新增** `plan_smarthome_mcp_yearly` | — | **4000** | 同 #3 | **新增** `product_smarthome_mcp_yearly`（4000） | 同 #3 |

**400 檔建立要點**：

- 從測試 fixture（`src/tests/test_smart_home_mcp.py`）只取兩件事：① `mcp_integration` 字面值精確＝`'ha_mcp_tools'`；② price/product 綁定三欄。**⚠️ 勿照 fixture 的 6 欄最小集 seed**——seed 版必須鏡射 `plan_smarthome_monthly` 完整 11 欄（§2.1）再加 `mcp_integration`，否則 MCP 檔漏 trial／reminder／never_expire 等，與 base 檔行為不一致。
- 落點：`subscription_products.xml` Smart Home 區塊（`:163` 註解後、`:198` SA 區塊前），仍在既有 noupdate data 內；product 用 `product.product` model（§2.1）。
- SH plan 無配額欄位（對照 SA 5 個），無 enforcement——tier 差異僅在 `mcp_integration`。

---

## 5. 分表 — Security Access＋Access 席數加值（AC）〔草稿缺件，由拍板＋seed 現況重建〕

> 拍板：SA 本體 **5 Application＝1000／10 Application＝2000**（月繳；`sa_max_routes=5/10`）；AC 加值 **500/seat/月**、內含席數 M=0（從 0 起全加購）；email 計數＝**去重**；CF 成本（帳號超 50 seat 全帳號 PAYG US$7/user/月）轉嫁在售價、不對使用者顯示；硬上限掛**方案層總量＝新欄位 `sa_max_access_emails`**（跨該訂閱全部 route 去重加總＝issue #274 B 路線；**明確否決 per-route 上限**）；既有 per-policy 20（`sa_max_emails_per_policy`）留軟護欄；UI＝簡化版 policy 介面、系統底層產完整 Cloudflare policy。
> CF seat 事實：per-**ACCOUNT** 共用池 50（非 per-domain）；service token 不佔 seat；移除即釋放。

### 5.0 現況（4 筆 plan，quota 已 seed——非草稿假設的無 quota）

| XML id | 現名 | 現價 | 現況 quota（`sa_max_routes`/`policies`/`domains`/`email_rules`/`emails_per_policy`） |
|---|---|---:|---|
| `plan_security_access_entry_monthly`（`:199`） | Security Access Entry Monthly | 100 | 3 / 3 / 1 / 0 / 20 |
| `plan_security_access_entry_yearly`（`:221`） | Security Access Entry Yearly | 1000 | 3 / 3 / 1 / 0 / 20 |
| `plan_security_access_pro_monthly`（`:243`） | Security Access Pro Monthly | 300 | 10 / 10 / 3 / 10 / 20 |
| `plan_security_access_pro_yearly`（`:265`） | Security Access Pro Yearly | 3000 | 10 / 10 / 3 / 10 / 20 |

product：`product_security_access_entry_monthly`（`:498`，100）／`_entry_yearly`（`:507`，1000）／`_pro_monthly`（`:516`，300）／`_pro_yearly`（`:525`，3000）。quota 欄位定義在 `src/models/sh_subscription_plan_sa_quota.py:17-43`（5 欄，add-only pattern）。

### 5.1 目標（4 plan，全沿用 xmlid、全 reprice，無新增）〔映射為 R6 原則之重建推導〕

| Plan 名（擬） | XML id | 月價 | 年價 | quota 異動 | product | 異動／migration |
|---|---|---:|---:|---|---|---|
| Security Access 5 Application Monthly | `plan_security_access_entry_monthly`（沿用） | **1000** | — | `sa_max_routes` **3→5**；`sa_max_access_emails`＝新欄位（seed 值 Q3）；其餘 quota 未拍板不動（Q6） | `product_security_access_entry_monthly` 100→1000 | **改價＋quota＋改名**（migration） |
| Security Access 5 Application Yearly | `plan_security_access_entry_yearly`（沿用） | — | **10000** | 同上 | `product_security_access_entry_yearly` 1000→10000 | 同上（⚠️ 舊價 1000＝新 5App 月價，R8 撞價實例——xmlid 定位） |
| Security Access 10 Application Monthly | `plan_security_access_pro_monthly`（沿用） | **2000** | — | `sa_max_routes`＝10 已符合不動；`sa_max_access_emails` 同上 | `product_security_access_pro_monthly` 300→2000 | **改價＋改名**（migration） |
| Security Access 10 Application Yearly | `plan_security_access_pro_yearly`（沿用） | — | **20000** | 同上 | `product_security_access_pro_yearly` 3000→20000 | 同上（⚠️ 舊價 3000＝MCP 新月價） |

### 5.2 AC 席數加值（500/seat/月）

- **定價機制（已拍板）**：500/seat/月；方案內含席數 M=0 ⇒ 有效 email 席數上限＝0＋已購 AC 席數；計數以 **email 去重**（跨該訂閱全部 route 加總）；硬上限 enforce 在 `sa_max_access_emails`（B 路線）；per-policy 20 留軟護欄；CF 超 50 的 PAYG 成本內含於 500 售價、不對使用者顯示。
- **未拍板（Q3）**：seed 建模——per-seat 計價在 `sh_subscription` 是「qty 可變的 add-on product 掛同一訂閱」還是「獨立 plan」？年繳席數是否存在（×10？）？`sa_max_access_emails` 是靜態 seed 值（0）＋執行期加上已購席數，還是 migration 動態寫入？**本波 PR 先落欄位（default 0）＋SA 4 檔 reprice；AC 席數 product 待建模拍板後補**，不 block 本 PR。

---

## 6. 分表 — AI 類（MCP CS＋Hermes）

> 範圍：MCP Server（Odoo MCP／n8n MCP，**統一 3000**、規格 1vCPU/1G/5G）＋ Hermes（三階 **1500/4500/6000**）。AnythingLLM 屬 CS 類 placeholder（§1 參考列），不在本分表。
> 定案 4 要點：① 現況 6 plan＋6 product 全 499/4990 placeholder、全面漂移；② MCP 統一規格 **#268 已對齊，template 不動、只改價**；③ Hermes 三階規格是否分階＝開放問題（Q4 Gate A/B）；④ **Hermes T3＝6000 取代會議記錄 6500**。

### 6.0 現況 placeholder 盤點

| 服務 | 現價 月/年 | plan XML id | product XML id |
|---|---|---|---|
| Hermes（單一階，缺 T2/T3） | 499 / 4990 | `plan_mcp_hermes_monthly`（`:288`）／`_yearly`（`:305`） | `product_mcp_hermes_monthly`（`:536`）／`_yearly`（`:546`） |
| Odoo MCP | 499 / 4990 | `plan_mcp_odoo_monthly`（`:323`）／`_yearly`（`:340`） | `product_mcp_odoo_monthly`（`:557`）／`_yearly`（`:567`） |
| n8n MCP | 499 / 4990 | `plan_mcp_n8n_monthly`（`:358`）／`_yearly`（`:375`） | `product_mcp_n8n_monthly`（`:578`）／`_yearly`（`:588`） |

### 6.1 目標（每列＝月＋年兩筆 plan record）

| Offering | XML id | 月價 | 年價（×10） | 規格 | product | 異動／migration |
|---|---|---:|---:|---|---|---|
| **Odoo MCP Server** | `plan_mcp_odoo_monthly`／`_yearly`（沿用） | **3000** | **30000** | template `cloud_app_mcp_odoo`＝1vCPU/1G/5G（#268 已對齊，不動）；plan 無 quota 欄位；`service_type`（Q2：`'mcp'` vs `'mcp_odoo'`） | `product_mcp_odoo_monthly`／`_yearly` 499→3000／4990→30000 | **改價 only**（migration force-write 4 筆） |
| **n8n MCP Server** | `plan_mcp_n8n_monthly`／`_yearly`（沿用） | **3000** | **30000** | 同上（`cloud_app_mcp_n8n`） | `product_mcp_n8n_monthly`／`_yearly` 499→3000／4990→30000 | 同上 |
| **Hermes T1（1–3 人）** | `plan_mcp_hermes_monthly`／`_yearly`（沿用，原地改為 T1，既有訂閱自動歸 T1） | **1500** | **15000** | template `cloud_app_mcp_hermes` 現為 flat 1vCPU/1G/5G；三階是否分規格未定（Q4 Gate A/B） | `product_mcp_hermes_monthly`／`_yearly` 499→1500／4990→15000 | **改價＋改名**（加 Tier 1 標籤；migration） |
| **Hermes T2（3–6 人）** | **新增** `plan_mcp_hermes_t2_monthly`／`_yearly` | **4500** | **45000** | 規格未定（Q4）；建立即帶 `service_type` | **新增** `product_mcp_hermes_t2_monthly`／`_yearly`（4500／45000） | 新增（`-u` 自建；須帶 §2.1 全域常數） |
| **Hermes T3（6–10 人）** | **新增** `plan_mcp_hermes_t3_monthly`／`_yearly` | **6000** ← 定案，取代會議 6500 | **60000** | 同 T2 | **新增** `product_mcp_hermes_t3_monthly`／`_yearly`（6000／60000） | 同 T2 |

**AI 類淨改動**：reprice 6 plan＋6 product＝12 筆（migration）；新增 4 plan＋4 product＝8 筆（`-u`）。
**sanity**：MCP 年 30000、Hermes T3 年 60000 為 ×10 直算大額，seed 時 double-check 非筆誤（機械規則、後台可改）。

---

## 7. 統一開放問題清單（按拍板急迫度排序）

| # | 問題 | 涵蓋服務 | 急迫度理由 |
|---|---|---|---|
| **Q1** | **既有訂閱改價回溯行為＋grandfathering 政策**：`sh_plan_price` 是訂閱建立時 snapshot 還是即時讀取（下一週期直接漲）？16 筆 reprice（n8n 199→1000 漲 5 倍最敏感）對 active 訂閱是無聲漲價——與 #324 重複扣款 incident 同一失效類別（stg 已知有訂閱掛在 placeholder plan 上）。migration 前**必查** stg/prod active subscriptions on 這 16 個 plan，政策寫死在 migration 內、不可預設 | 全部 | **最高**——錢＋既有客戶，migration 跑下去即生效 |
| **Q2** | **`service_type` discriminator enum 值定案**：欄位 repo 尚不存在、本波 PR 才加。擬值 `cloud_service`／`smart_home`／`security_access` 較無疑義；AI 類爭點＝Odoo/n8n MCP 同為 `'mcp'` 還是拆 `'mcp_odoo'`/`'mcp_n8n'`？Hermes 用 `'hermes'` 還是併 `'mcp'`？影響前端篩選與 template M2M 檢查（`_check_no_cross_service_type_plans`，`cloud_app_template.py:191`） | 全部 | **PR 本體 blocker**——enum 不定，欄位寫不出來 |
| **Q3** | **AC 席數 add-on 建模＋`sa_max_access_emails` seed 值**：500/seat/月已拍板，但 per-seat 在 `sh_subscription` 的載體（qty add-on product vs 獨立 plan）、年繳席數存不存在、`sa_max_access_emails` 是靜態值＋執行期疊加已購席數還是動態寫入——皆未定。欄位本身可先落（default 0）不 block PR | SA＋AC | **高**——AC 已拍板收費卻無法落地；欄位先落、product 後補 |
| **Q4** | **tier→部署規格映射機制**：(a) CS Option B（plan 掛 `cs_*`，推薦）vs Option A（per-tier template）拍板；(b) 建服務當下「選了哪個 plan」的解析（CloudService 不存 plan_id、trial 期可能未綁訂閱）；(c) Odoo workers=0→tier-aware（三階生效硬前提，撞 template sticky noupdate）；(d) Hermes Gate A（純價格分階 vs 規格分階——定案未給 Hermes 規格階梯）＋Gate B（單一 template 無法承載三組 resources，同 CS 未完成工程）。**不 block 改價 seed，但不解則三階＝同規格不同價** | CS＋Hermes | **高**——定價正當性；可與 seed PR 拆開但要有明確 follow-up |
| **Q5** | **前端 picker 曝光驗證**：`/woow` 各建立畫面如何查詢可選 plan（依 `service_type`？product？name 前綴？）——SH 新 MCP 兩檔、CS/Hermes 新 8 檔必須符合過濾條件才會渲染；另 template `subscription_plan_ids` M2M seed 現況未填，re-seed 要 wire 否則 marketplace 抓不到三階（撞 template sticky noupdate，§2.3） | SH＋CS＋AI | 中高——seed 建了前端不顯示＝白做 |
| **Q6** | **SA 5 Application 檔其餘 quota 值**：拍板只給 `sa_max_routes=5/10`；Entry 現況 `policies=3`/`domains=1`/`email_rules=0` 是否隨檔調整未拍板（10 App 檔現值 10/3/10 同問） | SA | 中——不影響價格，影響檔位價值感 |
| **Q7** | **顯示名策略統一**：SH base 是否改「Smart Home Connect」、Hermes T1 加「Tier 1」、SA「Entry/Pro」→「5/10 Application」、CS T1 加 tier 名——rename 全被 noupdate 擋，與改價共用同支 migration，一次定案 | 全部 | 中——cosmetic 但須趕同支 migration |
| **Q8** | **Odoo workers>0 後記憶體上限**：init script 現設 `limit_memory_hard/soft=0`（不限），workers>0 後是否隨 tier RAM 給上限，避免單 worker 撐爆 pod OOM | CS-Odoo | 中——Q4(c) 落地時一併 |
| **Q9** | **CS 其餘 template 處置**：postgresql（99/990）、anythingllm（299/2990）placeholder 是否併三階／維持單階／下架；redis **有 template 無 plan**（`cloud_app_templates.xml:112`）要補 seed 還是下架 template | CS | 中低——不在本波，須有歸宿 |
| **Q10** | **T3 軟上限可持續性**：T3（6–10 人）硬體刻意≈T2，實跑 6–10 人是否夠用待觀察；定價高於等比算力，產品面確認可接受（拍板已認可方向，留觀察項） | CS＋Hermes | 低——上線後觀察 |
| **Q11** | **MCP per-user 多份訂閱（backlog）**：會議提 Odoo MCP「per account 拿個人 Token、每人各開一次、可帶多份訂閱費」——同 workspace 對同 plan 多實例屬 `sh_subscription` 語意擴充，影響 AI 類是否需 per-seat 建模 | AI | 低——backlog |

---

## 8. 「定價 seed 重定 PR」落地 checklist（base `.51`）

### A. 版本與前置

- [ ] **A1** `src/__manifest__.py:3` 版號 `18.0.1.0.50` → **`18.0.1.0.51`**
- [ ] **A2**（Q1，merge 前必做）查 stg／prod active subscriptions 掛在 16 個 reprice plan 上的清單；grandfathering 政策拍板並寫入 migration
- [ ] **A3**（Q2，寫 code 前必做）`service_type` enum 值拍板

### B. Schema（新欄位，`-u` 自動建欄）

- [ ] **B1** `sh.subscription.plan` 加 **`service_type`** Selection discriminator（新 inherit 檔或併現有；add-only pattern，同 `sa_quota`／`sh_mcp`）
- [ ] **B2** `src/models/sh_subscription_plan_sa_quota.py` 加 **`sa_max_access_emails`**（Integer，default 0；help 註明「跨訂閱全 route email 去重加總之硬上限；0＋AC 席數」語意）
- [ ] **B3**（若 Q4 拍 Option B）新增 `src/models/sh_subscription_plan_cs_spec.py`：`cs_vcpu`／`cs_ram_gb`／`cs_storage_gb`／`cs_odoo_workers`——可與 seed PR 拆開成 follow-up

### C. Seed XML 新增（`subscription_products.xml`，noupdate 內新 xmlid，`-u` 自建，28 筆）

每筆 plan 帶 §2.1 完整 11 欄＋`service_type`＋各自專屬欄；product 用 `product.product`＋4 常數欄＋`sh_subscription_plan_id`：

- [ ] **C1** CS-Odoo：`plan_odoo_t2_monthly`(3000)／`plan_odoo_t2_yearly`(30000)／`plan_odoo_t3_monthly`(5000)／`plan_odoo_t3_yearly`(50000)＋同名 `product_odoo_t2/t3_*`
- [ ] **C2** CS-n8n：`plan_n8n_t2_monthly`(3000)／`plan_n8n_t2_yearly`(30000)／`plan_n8n_t3_monthly`(5000)／`plan_n8n_t3_yearly`(50000)＋同名 `product_n8n_t2/t3_*`
- [ ] **C3** SH：`plan_smarthome_mcp_monthly`(400，`mcp_integration='ha_mcp_tools'` 字面精確)／`plan_smarthome_mcp_yearly`(4000，同)＋`product_smarthome_mcp_monthly/yearly`；落點 `:163` SH 區塊內、`:198` SA 區塊前
- [ ] **C4** AI-Hermes：`plan_mcp_hermes_t2_monthly`(4500)／`_t2_yearly`(45000)／`_t3_monthly`(6000)／`_t3_yearly`(60000)＋`product_mcp_hermes_t2/t3_*`
- [ ] **C5**（Q3 拍板後，可後補不 block）AC 席數 add-on product（500/seat/月）

### D. Migration `src/migrations/18.0.1.0.51/post-migrate.py`（命名循 `.42`/`.43` 現例；**一律 `env.ref()` xmlid 定位**，R8）

- [ ] **D1** reprice 16 plan `sh_plan_price`＋16 product `list_price`（32 筆）：
  - CS：`plan_odoo_monthly` 499→1000、`plan_odoo_yearly` 4990→10000、`plan_n8n_monthly` 199→1000、`plan_n8n_yearly` 1990→10000（product 同步）
  - SH：`plan_smarthome_monthly` 100→150、`plan_smarthome_yearly` 1000→1500（product 同步）
  - SA：`plan_security_access_entry_monthly` 100→1000、`_entry_yearly` 1000→10000、`_pro_monthly` 300→2000、`_pro_yearly` 3000→20000（product 同步）
  - AI：`plan_mcp_odoo_monthly/yearly` 499→3000／4990→30000、`plan_mcp_n8n_monthly/yearly` 同、`plan_mcp_hermes_monthly/yearly` 499→1500／4990→15000（product 同步）
- [ ] **D2** rename（依 Q7 定案）：CS T1 ×4、SH base ×2、SA ×4（Entry/Pro→5/10 Application）、Hermes T1 ×2＋對應 product name
- [ ] **D3** SA quota：`plan_security_access_entry_monthly/yearly` 的 `sa_max_routes` 3→**5**（Pro 已=10 不動；其餘 quota 依 Q6）
- [ ] **D4** `service_type` backfill **既有 20 筆 plan**（含 pg／anythingllm 4 筆 placeholder 也要歸類）；新 14 筆 XML 自帶免 backfill
- [ ] **D5**（若 B3 同 PR）CS T1 既有 4 筆 backfill `cs_*` 規格值
- [ ] **D6** grandfathering 處置碼（依 A2 拍板）

### E. Template-side（另一支 heal/migration，撞 §2.3 sticky noupdate，非 `-u` 自動）

- [ ] **E1** `cloud_app_odoo.helm_default_values` 的 init script `workers=0` → tier-aware（`cloud_app_templates.xml:170`；比照 `_heal_sticky_helm_value_specs` 加專門重寫 `helm_default_values` 的 heal `<function>`）——**三階規格生效硬前提**（可拆 follow-up，但要與改價 PR 明確掛鉤）
- [ ] **E2** template `subscription_plan_ids` M2M wiring：odoo↔6 plan、n8n↔6 plan、mcp-odoo/mcp-n8n↔各 2、mcp-hermes↔6、（smarthome／SA 無 template 不涉）——同撞 sticky noupdate，需 migration；不 wire 則 marketplace 抓不到新檔（Q5）

### F. 部署碼（若 Q4 拍 Option B，可拆 follow-up PR）

- [ ] **F1** `controllers/paas.py:2171-2173` `allocated_*` 改讀選定 plan `cs_*`（`template.min_*` 留 fallback）
- [ ] **F2** `cloud_service.py` helm merge overlay `cs_*` → resources／persistence.size／workers
- [ ] **F3** tier(plan) 解析規則落地（CloudService 存 plan 參照或查 active subscription）

### G. 測試與驗證

- [ ] **G1** seed 完整性測試：30 筆定案 plan 價格斷言＋年=月×10 機械驗證＋trial=7
- [ ] **G2** SH MCP 檔 `mcp_integration='ha_mcp_tools'` 字面斷言（對齊 `test_smart_home_mcp.py` 既有 fixture）
- [ ] **G3** migration 測試：模擬 .50→.51 升級，驗 reprice／backfill／quota（noupdate DB 上 `-u` 後斷言）
- [ ] **G4** 前端 picker 冒煙（Q5）：SH 四選項、CS/Hermes 三階有渲染
- [ ] **G5** 不刪任何既有 record 斷言（pg／anythingllm 4 筆 placeholder 原價原樣）
