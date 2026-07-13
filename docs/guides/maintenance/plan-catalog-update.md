# Plan 目錄更新指南（改價／加級距／換 manifest 的標準流程）

> 適用：`sh.subscription.plan` 目錄的任何價格、級距、AC 變體調整。
> 核心原則：**單一事實源＋生成不手改＋雙軌 seed＋版號鐵律**。看完本檔即可把整個更新任務交給 AI agent 執行（文末有現成 prompt 模板）。

## 1. 架構：plan 目錄的三層結構

| 層 | 檔案 | 管理方式 |
|---|---|---|
| **Base plans**（各服務基礎檔位） | `src/data/subscription_products.xml`＋`src/data/subscription_template_links.xml` | **手寫**；改價必雙軌（改 XML＋migration force-write） |
| **AC 席數變體**（`+N access`，208 筆） | `src/data/subscription_products_variants.xml`＋`src/data/subscription_template_links_variants.xml`＋`src/tests/test_plan_variants_generated.py` | **全自動生成，嚴禁手改**（檔頭有 GENERATED 標記）；改價＝改 manifest 重跑 generator |
| **升級軌** | `src/migrations/18.0.1.0.NN/post-migrate.py` → delegate 到 `src/models/sh_subscription_plan_*.py` 的 `_woow_migrate_*` method | 每次 base 改價新開一支（版號＝該 PR 改完的 manifest 版號） |

**單一事實源**：`docs/plans/2026-Q3/2026-07-12-cloud-services-214-manifest.md`（Elmo 官方定價 manifest）。所有變體價格只能來自它；base 價格的權威對照表在 `docs/superpowers/plans/2026-07-13-wave-b-reseed-generator.md` 的「PR-B2 base reprice 權威表」。

## 2. Scripts

| script | 用途 | 指令 |
|---|---|---|
| `scripts/seed/generate_plan_variants.py` | manifest → 三個生成檔（變體 plans/products XML、M2M links XML、全量斷言測試） | `python3 scripts/seed/generate_plan_variants.py`（於 repo 根執行；冪等——同輸入重跑 byte-identical） |
| `scripts/seed/test_generate_plan_variants.py` | generator 自測（214 對帳、AC 公式 +N×600/+N×4800、年繳 ×8／SA ×9、xmlid 規則、noupdate 鐵律、冪等） | `cd scripts/seed && python3 -m unittest test_generate_plan_variants -v` |
| `scripts/audit/repo-lint.py` | 守線 lint（controller-sudo／owl-template／data-noupdate） | `python3 scripts/audit/repo-lint.py` |

generator 內建下列權威對映表／常數（改結構才需要動它們，皆在 `scripts/seed/generate_plan_variants.py` 模組層）：`_FAMILY_RULES`（manifest 名稱前綴→家族＋tier 判定）、`TIER_BASE_XMLID`（(家族,tier)→base plan xmlid 字首）、`CS_SPECS`（odoo/n8n 的 cs_* 繼承規格）、`SA_QUOTA`（SA quota 繼承）、`FAMILY_SERVICE_TYPE`（家族→service_type）、`_TEMPLATE_ORDER`（M2M links 檔 template 落地順序）。（`mcp_integration` 非表驅動，於 emit 時對 `sh`/`p2`〔智慧家庭方案二〕硬編 `ha_mcp_tools`。）

## 3. 場景別 SOP

### 3a. Elmo 交付新版 manifest（改變體價／變體數）

1. 新 manifest 存 `docs/plans/2026-Q3/YYYY-MM-DD-cloud-services-NNN-manifest.md`（保留日期檔名，舊檔不刪＝歷史）。
2. 把 manifest 路徑指向新檔——**兩處硬編需同改**：generator `main()` 內的 `manifest = root / 'docs/plans/2026-Q3/…-manifest.md'`（`scripts/seed/generate_plan_variants.py`，local 變數、非模組級常數）＋自測檔模組級 `MANIFEST` 常數（`scripts/seed/test_generate_plan_variants.py`）。只改一處會讓步驟 4 的 generator 自測仍對舊 manifest 對帳（舊檔已刪則自測直接炸）。
3. 重跑 generator → `git diff` 檢視三個生成檔的變化是否符合預期（只有價格列變＝改價；record 數變＝級距增減）。
4. 跑 generator 自測＋Odoo 生成測試（`--test-tags /woow_paas_platform:TestPlanVariantsGenerated`）。
5. 版號 bump＋（若既有變體 record 改價）migration——**變體是 noupdate=1，改價同樣要雙軌**：generator 產的 XML 是 fresh 軌；升級軌需一支 migration delegate 對變體 reprice（讓 agent 依 base reprice 前例照抄模式）。
6. 走 worktree → PR → 人工 merge。

### 3b. 改 base 檔位價格／名稱／quota

1. 依「PR-B2 base reprice 權威表」模式：改 `subscription_products.xml`（fresh 軌）＋新增 `_woow_migrate_*` delegate＋`src/migrations/18.0.1.0.NN/`（升級軌）。
2. **F4 鐵律**：migration 目錄版號＝該 PR 改完後的 manifest 版號（目錄 ≤ 改動前版號會被靜默跳過）。
3. **R8 鐵律**：migration 一律 `env.ref('woow_paas_platform.<xmlid>')` 定位，嚴禁價格／名稱 match。
4. 同 commit 更新 `src/tests/test_pricing_seed_330.py` 的 `EXPECTED_PLANS` 等既有斷言（否則測試紅）。
5. 若 base 的 cs_*／sa quota 變了，同步更新 generator 的 `CS_SPECS`（cs_* 規格）／`SA_QUOTA`（SA quota）（變體繼承 base 規格）並重跑 generator。

### 3c. 新增 tier／新家族

1. base plan/product record 手寫新增（鏡射同家族既有 record 全欄位；帶 §2.1 全域常數 11 欄＋service_type＋專屬欄）＋M2M links。
2. generator 三張對映表加對應項 → 重跑。
3. 版號 bump（純新增走 `-u` 自建、無 migration）。

### 3d. 守線清單（agent 執行時逐條核）

- 生成檔嚴禁手改；改價 SOP＝改 manifest／對映表重跑。
- 雙軌：noupdate=1 的 record 改值必 XML＋migration 成對。
- **雙檔 M2M noupdate 語意**：變體 links 檔必 `noupdate="0"`（隨 base `(6,0)` 重設後重新 append）；變體 plans/products 檔必 `noupdate="1"`。manifest data 清單順序＝載入順序，variants links 必排 base links 之後。驗證法＝同 DB 連跑兩次 `-u` 後 M2M 斷言仍綠。
- 版號隊列先查 open PR 與其他團隊佔用（版號序＝merge 序、manifest 不可倒退）。
- 前台可見性：變體目前全 `storefront_hidden=True`（等 picker 三步選擇器上線才翻開）。

## 4. 叫 AI agent 執行的 prompt 模板

以下 prompt 貼給 Claude Code（repo 根目錄開 session）即可執行完整更新。依場景取用並填入 `<>`：

### 改價（新 manifest 到貨）

```
Elmo 交付了新版定價 manifest：<新檔絕對路徑>。
請照 docs/guides/maintenance/plan-catalog-update.md 的 SOP 3a 執行：
把 manifest 收進 docs/plans/2026-Q3/（日期檔名）→ 更新 generator 指向 →
重跑 scripts/seed/generate_plan_variants.py → git diff 核對變化並向我摘要 →
generator 自測與 Odoo 生成測試全綠 → 若既有變體改價，補雙軌 migration（版號照 F4 鐵律錨定）。
守線照該 guide §3d 逐條核。git worktree 隔離、TDD、PR 開好給我 merge、絕不自行 merge。
```

### 改 base 檔位

```
請照 docs/guides/maintenance/plan-catalog-update.md 的 SOP 3b 調整 base 檔位：
<列出：xmlid／新價／新名／quota 變化>。
雙軌（XML＋migration delegate、env.ref 定位、F4 版號鐵律）、
同 commit 更新 EXPECTED 斷言、若涉 cs_*/quota 同步 generator CS_SPECS／SA_QUOTA 並重跑。
worktree＋TDD＋PR 待我 merge。
```

### 新增 tier／家族

```
請照 docs/guides/maintenance/plan-catalog-update.md 的 SOP 3c 新增 <家族/tier 描述、價格、規格>：
base record 手寫（鏡射同家族欄位集）＋generator 對映表擴充＋重跑生成、
版號 bump、全量測試綠。worktree＋TDD＋PR 待我 merge。
```

> 執行紀律通則（所有模板已隱含）：agent 不得 merge PR；發現 plan/現實不符先停下回報；tea 指令帶 `--repo odoo-addons/woow_paas_platform`；commit／PR 一律 zh-tw、無 AI 生成標記。

## 5. 歷史脈絡（為什麼長這樣）

- 甲路線全展開（2026-07-12 拍板）：每個「服務×tier×AC 席數」＝獨立 plan，訂閱 header-only 結算原樣。
- 設計文件：`docs/superpowers/plans/2026-07-13-wave-b-reseed-generator.md`（generator＋reprice）、`docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md`（欄位佈局）、`docs/decisions/patterns/template-vs-plan-property-boundary.md`（何時開新 template）。
