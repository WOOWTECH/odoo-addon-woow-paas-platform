# project wave b reseed build

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_wave_b_reseed_build`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description：Wave B re-seed 主體收官——三 PR 全 merged（develop=.60、251 plans）；generator 治理與 M2M noupdate 鐵律實證

**Wave B re-seed 收官（2026-07-13）**：三 PR 序列 merged——**#347**（B1 generator＋`docs/guides/maintenance/plan-catalog-update.md` 更新指南）→ **#348**（B2 base reprice＋SA 四級 B7、`.59`、migration delegate `_woow_migrate_reseed_b_330`）→ **#349**（B3 變體 208、`.60`、無 migration、60b5942b）。**develop=`.60`、251 plans、與正式 214 manifest（docs/plans/2026-Q3/2026-07-12-…）完全對齊**。plan＝`docs/plans/2026-Q3/2026-07-13-wave-b-reseed-generator.md`（APPROVED rounds=2）。**#330 鏈上 #331（AC carrier）/#335（跨家族 glue）已收斂關閉**。

**Leader 驗收軌跡**：#347 自測 15 綠＋純 scripts 零 src 影響；#348 容器 30 tests 綠（fresh install）；#349 冪等零 diff（PR head 重跑 generator）＋雙 `-u` upgrade 存活實證（M2M 斷言二輪綠）。Forge 全套 0 failed／2 known ns flake of 1641。

**留給未來的機制**（詳見 guide）：
- 改價 SOP＝改 manifest（或 generator 對映表）重跑 `scripts/seed/generate_plan_variants.py`——生成檔嚴禁手改；guide 附三個 AI agent prompt 模板。
- **M2M noupdate 鐵律（實證）**：變體 links 檔 `noupdate=0`（隨 base `(6,0)` 重設後重新 append）、變體 plans/products 檔 `noupdate=1`；驗證法＝同 DB 連跑兩次 `-u`。
- 新增家族動點：`_FAMILY_RULES`＋`TIER_BASE_XMLID`＋`FAMILY_SERVICE_TYPE`＋`CS_SPECS`/`SA_QUOTA`＋**`_TEMPLATE_ORDER`（漏加＝emit_links_xml 靜默略過該家族 M2M）**＋函式內嵌 `_template_xmlid`/`_template_model`。
- 變體判別器＝`ac_max_access_seats>0`（`storefront_hidden` 不可靠——HA PaaS base 也隱藏）。
- 變體全 `storefront_hidden=True`，**Wave C picker 上線才翻開**。

**執行偏離（已隨 PR 描述回報並驗收）**：n8n t4 cs-spec 為外推（manifest 無明列）；migration delegate 補 product name 後綴防雙軌漂移；`.52` migration 測試與 `.59` EXPECTED 解耦（凍結 `MIG_52_TARGET`）；B3 回歸修復 551f279（兩個精確集斷言改以變體判別器過濾）。

**B5 stg 真升級驗證（2026-07-13）✅ 全綠 PASS**（dev-sandbox-b5、expires 2026-07-20、留 Eugene inspect；db `woow_b5` admin/admin）：`.53`(34 plans) fresh install → set image `.60` 跑 `-u` 真升級。**證據雙軌**：(1) **marker log**——k3s 會秒清 Completed 容器的 container log（`kubectl logs`＋`--previous` 皆 0 行），改在升級 Job args 加 `--logfile=/root/.local/share/Odoo/upgrade.log`（掛 filestore PVC，Job pod 結束釋放後由 `.60` pod cat）→ 抓到 `.54→.56→.59` 依序執行（953→961→973ms）、error/traceback=0、乾淨收尾；(2) **leader 五項間接證據**（noupdate=1 下 `-u` 的 XML 不動既有 record，唯 migration delegate 能改）——`storefront_hidden`/`cs_vcpu` 欄位 `.53` **ABSENT**→`.60` 有值（`_fields` 檢查）；base 舊價 odoo 1000/3000/5000·n8n 1000/3000/5000 → 新價 1500/4500/7500·750/2250/4500；**`create_date=13:53:02` 升級後不變且早於 T_UP 14:08:23**（排除 job drop 重建 fresh-install 假綠）；`latest_version=.60`。**Phase 4 a-f 全過**（251／208 變體掛三 model M2M／reprice spot-check 含 SA 四級 200/600/1200/2000·yearly×9／Hermes 6 hidden／HA 0元 never_expire+hidden／二次 `-u` M2M 208 存活）。**Phase 5 三 surface picker HTTP**（CS/SA/SH 三 route 皆 `filtered(not storefront_hidden)`，無 bypass）：Odoo 6·Hermes 0·SA 8·SH 4，162 變體全排除。變體對帳 CS 88+SH 40+SA 80=208。

**殘留**：Wave C picker（三步選擇器＋變體翻開）；#340 prod 部署波——正式 image 已備（`v18.0.1.0.60`→`paas-odoo:20260713.1231`，B5 驗的同顆、approval gate Eugene 已親按過 build 關）。

關聯：[[project_wave_a_reseed_landed]]（前波）、[[wave-a-reseed-landed]]。
