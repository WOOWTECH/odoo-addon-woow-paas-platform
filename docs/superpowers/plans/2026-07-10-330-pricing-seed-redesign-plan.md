# #330 定價 seed 重定（30 檔）＋ plan taxonomy schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development per task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依 2026-07-10 拍板（OQ-A 3 值 enum／OQ-B `ac_max_access_seats`／OQ-C capability 不綁 type／Q1 grandfathering 不需要）落地：`service_type` taxonomy schema、30 檔定價 seed 重定（16 沿用改價＋14 新增）、`.52` migration（升級 DB force-write）、template M2M wiring、G1–G6 測試。

**權威輸入（衝突裁決序）：** ① `docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md`（taxonomy 報告，含拍板記錄）＞ ② `docs/plans/2026-Q3/2026-07-10-subscription-plan-matrix.md`（總表，checklist A–G）＞ ③ issue #330 描述。

**Architecture（雙軌一致性，核心設計）：**

Odoo 的 seed（`<data noupdate="1">`）與 migration 是兩條互補路徑，**兩邊都要寫、值必須一致**：

| 路徑 | 覆蓋對象 | 機制 |
|---|---|---|
| **Seed XML in-place 重定**（`subscription_products.xml`） | **新裝 DB**（含 CI/測試 DB——G1/G6 斷言直讀 seed 值） | 新裝時 migration 不會跑，seed 是唯一來源 ⇒ 16 筆改價／改名／SA quota／`service_type` 必須「原地改 XML」；4 筆 placeholder 補 `service_type`；14 筆新增自帶 12 欄 |
| **Migration `.52` force-write** | **已裝 DB**（stg/prod）——noupdate=1 使 `-u` 跳過既有 record 更新 | post-migrate 於 data 載入**之後**執行（Odoo 18 `loading.py:228→233` 已實證），新 plan xmlid 可 `env.ref`；一律 xmlid 定位（R8 撞價鐵律：SA Entry Yearly 舊 1000＝5App 新月價） |

M2M wiring 同理雙軌：`subscription_template_links.xml`（noupdate=0）供新裝；已裝 DB 有 sticky noupdate 風險（§2.3；`_heal_sticky_helm_value_specs` 只保證 cloud_app 系、smart_home 無對應 heal）⇒ **migration 內同步 force-write M2M**（(6,0,…) 全量、xmlid 定位、冪等）。

**Tech Stack:** Odoo 18 addon（Python 4-space；inherit add-only pattern 對照 `sh_subscription_plan_sa_quota.py`）；測試=本地 docker-desktop paas-odoo image bind-mount worktree src（容器名 `odoo330`/`pg330`；recipe=memory `reference_odoo_test_docker_desktop_paas_image`）。

## Global Constraints

- **版號**：`src/__manifest__.py` `18.0.1.0.51` → **`18.0.1.0.52`**；migration 目錄＝`src/migrations/18.0.1.0.52/`（taxonomy 報告 §5.1 順移，蓋過總表 checklist 原文 `.51`）。
- **上游唯讀**：`sh_subscription`（`sh.subscription.plan` 本尊）不動；已查證 image 內全部 inherit 檔（`sh_subscription_wallet`＋本 repo sa_quota/sh_mcp）**無 `service_type`／`ac_max_access_seats` 撞名**。
- **不刪任何既有 record**（G5）；4 筆 placeholder（pg×2＋anythingllm×2）價格/名稱原樣、只補 `service_type='cloud_service'`。
- **既有 3 條 template 互斥 constraint 原封不動**；僅 +1 條條件式 guard（orphan 放行——AnythingLLM 2 plan 無 template）。
- **grandfathering 已拍板不需要**：D1 直接 force-write、無政策碼（D6 移除）。
- **字面精確**：SH +MCP 兩檔 `mcp_integration='ha_mcp_tools'`（.50 已落地欄位）；seed 版鏡射完整 11 欄全域常數＋`service_type`＋`mcp_integration`（勿照 test fixture 6 欄最小集）。
- **commit**：zh-tw、prefix 小寫英文（feat:/test:/docs:/chore:）、無任何 AI 生成標記。
- **PR**：target develop、標題 zh-tw、描述含 `Closes #330`＋D2 改名清單（名稱屬輕決策，列出讓 Eugene 過目）；**開 PR 後停止、不 merge**。
- **本地 TDD 迴圈的 DB 生命週期**：seed XML 屬 noupdate=1——改 seed 後在「既有測試 DB」上 `-u` **不會**回填既有 record。G1/G5/G6 的 seed 斷言只在兩種 DB 狀態成立：(a) **重建 fresh DB**（dropdb→`-i`，走新 seed），或 (b) **.51 舊 DB 經 `-u` 升 .52**（走 migration force-write）。「.52 已裝＋舊 seed」的中間態 DB 永不收斂（僅 dev 迴圈人造態、非真實部署態），Task 2/3 每輪驗證前先重建 DB，勿在中間態 DB 上追鬼。

## 價格／改名對照（單一真相，Task 2/3 共用）

**Reprice 16（plan `sh_plan_price`＋product `list_price` 同步；migration 亦同值；每列＝月＋年兩筆 plan record，8 列＝16 plan＋16 product）：**

| xmlid（plan/product 同尾） | 舊月/年 | 新月/年 |
|---|---|---|
| `*_odoo_monthly` / `*_odoo_yearly` | 499 / 4990 | 1000 / 10000 |
| `*_n8n_monthly` / `*_n8n_yearly` | 199 / 1990 | 1000 / 10000 |
| `*_smarthome_monthly` / `*_smarthome_yearly` | 100 / 1000 | 150 / 1500 |
| `*_security_access_entry_monthly` / `_yearly` | 100 / 1000 | 1000 / 10000 |
| `*_security_access_pro_monthly` / `_yearly` | 300 / 3000 | 2000 / 20000 |
| `*_mcp_odoo_monthly` / `_yearly` | 499 / 4990 | 3000 / 30000 |
| `*_mcp_n8n_monthly` / `_yearly` | 499 / 4990 | 3000 / 30000 |
| `*_mcp_hermes_monthly` / `_yearly` | 499 / 4990 | 1500 / 15000 |

**新增 14 plan＋14 product（`-u` 自建；每 plan 帶 11 欄全域常數＋`service_type`）：**

| xmlid | 價 | service_type | 專屬欄 |
|---|---|---|---|
| `plan_odoo_t2_monthly`/`_yearly` | 3000/30000 | cloud_service | — |
| `plan_odoo_t3_monthly`/`_yearly` | 5000/50000 | cloud_service | — |
| `plan_n8n_t2_monthly`/`_yearly` | 3000/30000 | cloud_service | — |
| `plan_n8n_t3_monthly`/`_yearly` | 5000/50000 | cloud_service | — |
| `plan_smarthome_mcp_monthly`/`_yearly` | 400/4000 | smart_home | `mcp_integration='ha_mcp_tools'` |
| `plan_mcp_hermes_t2_monthly`/`_yearly` | 4500/45000 | cloud_service | — |
| `plan_mcp_hermes_t3_monthly`/`_yearly` | 6000/60000 | cloud_service | — |

（product xmlid＝`product_` 同尾；`product.product` model＋type=service＋sale_ok＋~purchase_ok＋sh_product_subscribe＋sh_subscription_plan_id。）

**D2 改名 12 plan＋12 product（矩陣「Plan 名（擬）」字面；MCP odoo/n8n＝改價 only 不改名）：**

| xmlid | 舊名 | 新名 |
|---|---|---|
| `plan_odoo_monthly`/`_yearly` | Odoo Monthly/Yearly | Odoo Cloud T1（1–3 人）Monthly/Yearly |
| `plan_n8n_monthly`/`_yearly` | n8n Monthly/Yearly | n8n Cloud T1（1–3 人）Monthly/Yearly |
| `plan_smarthome_monthly`/`_yearly` | Smart Home Monthly/Yearly | Smart Home Connect Monthly/Yearly |
| `plan_security_access_entry_monthly`/`_yearly` | Security Access Entry M/Y | Security Access 5 Application Monthly/Yearly |
| `plan_security_access_pro_monthly`/`_yearly` | Security Access Pro M/Y | Security Access 10 Application Monthly/Yearly |
| `plan_mcp_hermes_monthly`/`_yearly` | Hermes MCP Server M/Y | Hermes MCP Server T1（1–3 人）Monthly/Yearly |

（product 新名＝plan 新名＋" Subscription"；新增 14 檔 plan 名同款式：`Odoo Cloud T2（3–6 人）Monthly`、`Smart Home + MCP Monthly`、`Hermes MCP Server T2（3–6 人）Monthly`…）

**D3 quota**：`plan_security_access_entry_monthly`/`_yearly` `sa_max_routes` 3→**5**（其餘 quota 不動＝OQ-I 開放；pro 已=10 不動）。

**D4 backfill 20 筆**：cloud_service **14**（odoo 2＋n8n 2＋pg 2＋anythingllm 2＋mcp_odoo 2＋mcp_n8n 2＋hermes 2）／smart_home **2**／security_access **4**。

**E2 M2M（(6,0) 全量）**：`cloud_app_odoo`↔6、`cloud_app_n8n`↔6、`cloud_app_mcp_hermes`↔6、`smart_home_home_assistant`↔4；不動：`cloud_app_mcp_odoo`↔2、`cloud_app_mcp_n8n`↔2、`cloud_app_postgresql`↔2、`security_access_default`↔4。

---

## Task 1 — Schema：`service_type`＋guard＋`ac_max_access_seats`（B1/B2＋A1 版號）

**RED** `src/tests/test_pricing_seed_330.py`::`TestPlanTaxonomySchema`：

- [ ] `service_type` 為 Selection、恰 3 值 `{cloud_service, smart_home, security_access}`
- [ ] `ac_max_access_seats` 為 Integer、default 0（新建 plan 不給值＝0）
- [ ] guard-負向：建 plan 掛進 `cloud_app_template` M2M 後寫 `service_type='smart_home'` → `ValidationError`
- [ ] guard-正向：同 plan 寫 `'cloud_service'` → 通過
- [ ] guard-orphan：**測試內新建、從未掛任何 template M2M** 的 plan 寫任意 3 值 → 不炸（G6 之 guard 面；exercise `families 為空 → 放行` 分支）
- [ ] 跑 `--test-tags /woow_paas_platform:TestPlanTaxonomySchema` 確認紅（欄位不存在）

**GREEN**：

- [ ] `src/__manifest__.py` 版號 → `18.0.1.0.52`
- [ ] 新檔 `src/models/sh_subscription_plan_service_type.py`：`_inherit='sh.subscription.plan'`（add-only）＋ Selection 3 值（taxonomy §3.1 字面）＋ `_TEMPLATE_FAMILY` map ＋ `@api.constrains('service_type')` 條件式 guard（依 §3.3 草稿：反查三 template M2M，`families` 空＝orphan 放行；`service_type` 有值且不在 families → raise）
- [ ] 新檔 `src/models/sh_subscription_plan_ac.py`：`ac_max_access_seats` Integer default 0，help 註明「跨該訂閱全部 route 的 email_list **去重**位址數上限＝0＋已購 AC 席數；MAX/上限語意、非內含席數」。**Seed 全 34 檔一律不寫此欄（＝default 0）**；「0＋已購席數」的執行期疊加與 enforcement 接線屬 OQ-D carrier 實作（follow-up），本 PR 僅落欄位佔位——seed 值本身不足以表達 MAX 語意，後續實作者勿誤判
- [ ] `src/models/__init__.py` 兩行 import（緊接 `sh_subscription_plan_sh_mcp` 之後）
- [ ] 測試轉綠；commit `feat: #330 plan taxonomy schema（service_type 3 值＋條件式 guard＋ac_max_access_seats）`

## Task 2 — Seed 重定：in-place 16＋placeholder 4＋新增 14×2＋M2M links（C1–C4＋E2 XML 軌）

**RED** 同檔新增（先寫斷言、跑紅——現行 seed 舊價/舊名/無 service_type/無新 xmlid）：

- [ ] `TestPricingSeedIntegrity`（G1）：30 筆 plan 價格逐筆斷言（上表字面）；15 對月/年 檔位 `年價==月價×10` 機械驗證；34 筆 plan `sh_trial=True`＋`sh_trial_duration=7`＋`sh_trial_unit='day'`＋`sh_override_product=True`；30 筆 product `list_price==plan.sh_plan_price` 且 `sh_subscription_plan_id` 指向對應 plan
- [ ] `TestShMcpSeedLiteral`（G2）：`plan_smarthome_mcp_monthly`/`_yearly` `mcp_integration=='ha_mcp_tools'` 字面；base 兩檔＝`''`
- [ ] `TestPlaceholdersUntouched`（G5）：pg 99/990、anythingllm 299/2990、名稱原樣、product 原價
- [ ] `TestServiceTypeSeedCoverage`（G6）：34 筆 plan `service_type` 全非空；MCP/Hermes 6 筆＝`cloud_service`；分佈 26/4/4；對真 seed orphan `plan_anythingllm_monthly` 直接 write `service_type='cloud_service'` 不炸（guard 放行實證）。（RED 成立理由：既有 20 筆 seed 此欄空值→「全非空」斷言 fail；14 新 xmlid 尚不存在→`env.ref` 拋錯亦屬紅）
- [ ] `TestTemplatePlanWiring`（E2）：odoo↔6、n8n↔6、hermes↔6、SH↔4、mcp_odoo/mcp_n8n/pg↔2、SA↔4（xmlid 集合斷言）

**GREEN**：

- [ ] `src/data/subscription_products.xml`：16 筆既有 plan 原地改（價＋名＋SA entry `sa_max_routes` 5＋全 20 筆補 `service_type`）；16 筆 product 原地改（價＋12 筆改名）；SH 區塊尾插 2 筆 MCP plan、各分區插 12 筆 CS/Hermes plan；product 區同步 14 筆
- [ ] `src/data/subscription_template_links.xml`：odoo/n8n/hermes 三行 (6,0) 清單擴為 6 筆、smart_home 擴為 4 筆
- [ ] 測試轉綠（含 Task 1 檔案迴歸）；commit `feat: #330 定價 seed 重定（16 改價＋14 新增＋service_type 全帶＋template M2M links）`

## Task 3 — Migration `.52`（D1–D4＋E2 migration 軌）＋G3

**RED** 同檔新增 `TestPricingMigration330`（G3；比照 `test_security_access_p2_5_migration.py` 直驅 pattern＋`.42`/`.43` delegate 慣例）：

- [ ] fixture：以 ORM 把 16 plan＋16 product 寫回**舊價**、12+12 筆寫回**舊名**、SA entry routes 寫回 3、20 筆既有 plan `service_type` 寫 `False`、四 template M2M 清空——重建 `.51` 升級前 DB shape
- [ ] 斷言 migrate() 後：D1 全 32 筆新價；D2 全 24 筆新名；D3 entry 兩檔 routes=5、pro 兩檔仍 10；D4 20 筆 backfill 值分佈 14/2/4（anythingllm orphan 也成功寫入=guard 放行實證）；E2 四 template M2M 恢復 6/6/6/4
- [ ] 邊界：placeholder 4 筆價格/名稱遷移後仍原樣（99/990、299/2990）；**冪等**＝migrate() 連跑兩次結果不變；`version=None`（新裝）→ early-return 不動任何值
- [ ] 跑紅（migration 檔不存在→import 失敗）

**GREEN**：

- [ ] `sh_subscription_plan_service_type.py` 加 `@api.model _woow_migrate_pricing_330()`：xmlid 定位（`env.ref(..., raise_if_not_found=False)`＋缺 xmlid log-skip 不 abort）依序 D1 reprice→D2 rename→D3 quota→D4 backfill（force-write，無 grandfathering 碼）→E2 M2M (6,0) 全量；冪等
- [ ] 新檔 `src/migrations/18.0.1.0.52/post-migrate.py`：docstring 說明雙軌＋`if not version: return` guard＋delegate（比照 `.43`）
- [ ] 測試轉綠；commit `feat: #330 migration 18.0.1.0.52（32 筆 reprice＋24 筆改名＋SA quota＋service_type backfill＋M2M heal）`

## Task 4 — 全套件驗證＋PR

- [ ] 本地 docker（`odoo330`/`pg330`＋`AGENT_RUNTIME_NAMESPACE_SECRET` env）：fresh install `-i` → 全 test-tags `/woow_paas_platform` 跑綠（已知 2 個 `TestNamespaceLeftSetUnion` env flake 非回歸；帶 secret 後應 0）
- [ ] `push origin feat/330-pricing-seed-redesign`；`tea pulls create`（flags 在前）target develop：標題 zh-tw、描述含 `Closes #330`＋scope 摘要＋測試證據＋**D2 改名清單全列**＋裁決記錄（版號 .52 順移；G4 picker 冒煙不在本 PR scope——issue 列 G1–G6 但派工 scope 明示 G1/G2/G3/G5/G6）
- [ ] **開 PR 後停止、不 merge**

## 明確不做（issue「不含」＋派工 scope 外）

OQ-D AC carrier product（C5）／OQ-H `cs_*` 規格欄位＋workers tier-aware（B3/D5/E1/F 段）／OQ-E Email Routing／OQ-F Backup 佔位／G4 前端 picker 冒煙／`mcp_integration` 非空⟹smart_home 硬 guard（§3.4 建議待拍）。

<!-- peer-reviewed: 2026-07-10T10:35:37Z rounds=2 verdict=approved by=miniclaude -->
