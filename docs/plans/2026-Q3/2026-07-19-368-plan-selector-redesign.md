---
title: "#368 訂閱方案選擇器重設計 Implementation Plan"
category: plans
status: done
created: 2026-07-19
updated: 2026-07-22
source: docs/superpowers/plans/2026-07-19-issue-368-plan-selector-redesign.md
promoted-at: 2026-07-22
---

# #368 訂閱方案選擇器重設計 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `/woow` Configure／新購／改方案的方案選擇器從「8 張月/年×檔位混排卡片 + AC 席數第二步」重設計為「頂部常駐 cycle toggle 兩段式」，收斂 P1 認知超載、補上 P3 席數增量可見、顯示規格、修掉隨機預設 bug。

**Architecture:** 三 PR 串行。PR-1 純後端 additive-only（plans 端點加 `group_key`/`specs` 欄位、順序不動）。PR-2 前端 `PlanSelector` 重設計（cycle toggle 為 view-state、group_key 分組排序、席數增量、退化規則、`PlanTotalBar`、sticky footer、modal 外殼修復），一套元件走 4 個嵌入點。PR-3 `ChangePlanModal` 強化（後端補 `plan_id`、定位目前方案、canConfirm 差異檢查、760px、下週期生效提示）。全程零 schema、零 migration。

**Tech Stack:** Odoo 18 addon（Python 4-space、`sh.subscription.plan` inherit）；OWL（JS/XML 2-space、受控元件、`@odoo-module`）；SCSS（`.o_woow_` BEM）。前端 E2E＝`playwright-cli --headed`。測試＝Odoo `TransactionCase`（`/woow_paas_platform` tag）。

## Global Constraints

- **零 schema、零 migration**：難題 1 採方案 C（read-path xmlid 反查算 `group_key`），完全不動 seed data → 不觸發 `noupdate=1` 陷阱。全 3 PR 皆無 `src/migrations/` 新目錄。
- **版號隊列**：現版 develop=`.69`。PR-1=`.70`、PR-2=`.71`、PR-3=`.72`。**開每個 PR 前 re-check `src/__manifest__.py` 版號與 open PRs，撞號順延**。
- **受控契約鐵律**：`PlanSelector` 的 `selectedPlanId`（props）是**唯一「選擇」事實源**；父層擁有它。新增的 `state.cycle` 只是 view-state（決定顯示哪組卡片），非 flow-state（不代表選擇進度）。
- **排序鍵一律 `price`，絕不用 `group_key` 字串**：SA 家族 base xmlid 是 `r1/r3/entry/pro`，lexical sort 會亂序（entry/pro 排在 r1/r3 前）；`price` 與 tier 單調（已驗）。`group_key` 只負責 month↔year 身分對映。
- **增量單位一律取 `base.unit`**：年繳增量是折扣後年值（odoo t1 月 +600、年 +4800=600×8），寫死 `/月` 是 12 倍 correctness bug。
- **一套元件走 4 嵌入點，零 `service_type` 分支**：`AppConfigurationPage`（CS 新購長頁）、`CreateSecurityAccessModal`、`CreateSmartHomeModal`、`ChangePlanModal`（改方案）。差異全由退化規則 R1/R2/R3 依資料驅動。
- **Scope 不做**：P2 含稅顯示（#366 已清稅，card==charged）、P4（錢包餘額/儲值引導/試用首扣時點）、第 4 加值軸資料模型。
- **PR 描述 zh-tw**，`Refs #368` ＋引用本文件；不 merge、回報 leader。

## 拍板決策（2026-07-19 Eugene）

| # | 決策 | 理由 |
|---|---|---|
| D1 | **payload 形狀＝flat + `group_key` 欄位**（非 nested） | additive-only 可獨立 merge 優先，是 Wave C C1 驗證過的模式；nested 會讓 PR-1 變 breaking change，否決。有 stable `group_key`，前端分組不脆弱。 |
| D2 | **`plan_id` 放 PR-3** | 唯一 consumer、PR 自足。 |
| D3 | **modal 外殼 CSS 修復夾帶進 PR-2**，附兩條件 | (a) PR-2 描述必須有「夾帶全域 modal CSS 修復」**獨立段落＋影響清單**，不埋在 diff；(b) headed E2E 加一條「其他 modal 不回歸」抽查（挑 2-3 個常用 modal 開一遍）。 |

---

## 設計論證（難題解法，執行前必讀）

### 難題 1 — tier 結構化：方案 C（零 schema）完整論證

**問題**：tier（「1-3 人」「6 組」…）只存在於 `plan.name` 字串，且**月/年同名**（`plan_odoo_monthly` 與 `plan_odoo_yearly` 的 name 都是「Odoo 訂閱服務（1-3 人）」＝P1 病根）。43 筆 base plan 在 `src/data/subscription_products.xml` **手寫**、不經 generator，無 `tier_key`/`sort_order` 欄位；generator 雖有 `tier_key`（`generate_plan_variants.py:22-26` 的 `Variant`）但 emit XML 時丟棄。

**為何不選方案 A（model 欄位 + migration）**：加 `plan_group_key`/`plan_sort` 欄位需要——26 筆手寫 base 補欄位＋generator emit＋既有 251 筆因 `noupdate=1` 不吃 `-u`，須 migration force-write（目錄 ≥ `18.0.1.0.70`）。這是本 repo 反覆踩過的陷阱（見 pitfalls），且與 Wave C「xmlid 反查、零 schema」拍板不一致，blast radius 大、回滾難。

**方案 C（採用）**：base xmlid 本身已編碼 tier + cycle：`plan_odoo_t2_monthly`。**group_key = 去掉 `_monthly`/`_yearly` 尾綴** = `plan_odoo_t2`（月/年同 tier 得同 `group_key`），**cycle = `sh_unit`**。在 read-path（controller）用 `ir.model.data` 批次反查即可，與既有 `_attach_variants`（`sh_subscription_plan_visibility.py:32-112`）同機制（R8 xmlid 定位鐵律）。**完全不動 seed data → 不需 migration → 直接規避 `noupdate=1` 陷阱。**

**已驗證事實**（對抗驗證讀 code 坐實）：
1. 43 筆 base xmlid **全部**以 `_monthly`/`_yearly` 結尾，strip 後得 21 個 `group_key`、**零撞名**（含 `plan_smarthome` vs `plan_smarthome_mcp`、`plan_mcp_odoo` vs `plan_mcp_odoo_manage`）。
2. 效能無 N+1：對可見 base 的 group_key 只需**一次**批次 `search([('res_id','in',base_ids)])`，O(1) 次查詢。
3. `plan_ha_paas_monthly` 只有 monthly，但它 **`storefront_hidden=True` ＋ orphan（不掛任何 template M2M，`subscription_template_links.xml:51`）→ 永不進 picker**。故「可見且掛 template」的集合裡，每個 `group_key` 都有 month＋year 完整對 → 難題 4 的 cycle 配對反而更穩。

**三個守線**（不做會靜默出錯）：
- strip 用**錨定正則** `re.sub(r'_(monthly|yearly)$', '', xmlid)`（`$` 錨定只截尾綴，防未來 xmlid 中段含 `_monthly`）。
- `group_key` **只對 base 算**（`ac_max_access_seats == 0 ∧ not storefront_hidden`），別把變體（以 `_ac{n}_` 結尾）納入 strip。
- **cycle 完整性 + 交叉斷言**測試（見 PR-1 T4）：每個可見掛 template 的 base `group_key` 恰有 `{month, year}` 兩筆、且每筆 base xmlid 的 cycle 後綴與 `sh_unit` 一致（防手改 xml 打錯 cycle）。這是難題 4 真正的守線。

### 難題 7 — 總計條位置：sticky footer 完整解法細節

**問題**：`AppConfigurationPage` 是單一長頁、多個 `WoowCard` section 垂直堆疊、Launch 在頁尾 `o_woow_configure_footer`。若把 sticky 總計條貼在 `PlanSelector` 容器（section body），**containing block 是那張卡片** → 捲到下方 section（Network/Application Settings/Addon）時總計條隨卡片離場。這是 rubric 評審實查的疑點。

**滾動容器鏈（已查證）**：`.o_woow_layout(overflow:hidden)` → `.o_woow_main(overflow:hidden)` → **`.o_woow_content(flex:1; overflow-y:auto; min-height:0; padding:1.5rem)＝真正的 scrollport**。`AppConfigurationPage` 直接掛進 `.o_woow_content`，中間無 wrapper。`.o_woow_configure_page` 只有 `max-width:800px; margin:0 auto`、**無 position/overflow/transform**（`40_configure.scss:8-11`）→ 不攔截 sticky、不成裁切上下文。

**解法（頁面層 sticky footer）**：`.o_woow_configure_footer` 是 `.o_woow_configure_page` 的**最後直接子**（`AppConfigurationPage.xml:230`），對它加 `position:sticky; bottom:0`，containing block ＝整個 page → pin 在 scrollport 底、捲到頁尾 release 歸位，標準「常駐底欄」pattern。**優於** `position:fixed`（會脫離 800px 置中欄與 sidebar `margin-left`，要手算 left/width 且隨斷點變）與「PlanSelector 放頁尾」（仍會捲走、破壞表單順序、對 modal 無幫助）。

**三個必補項（漏了就壞）**：
1. `.o_woow_configure_footer` 目前**無 background/z-index/position**（`40_configure.scss:344`）。sticky 疊在滾動內容上是透明的 → 必補 **opaque 背景（`$woow-card-light`）＋ z-index ＋ top box-shadow/border**，否則下方卡片透出、總價讀不清。
2. **Modal「天然貼底」是假**：`.o_woow_modal` **無 max-height**（`30_components.scss:242`）、`.o_woow_modal_body` **只有 padding、無 overflow**（`:290`），backdrop 是 `position:fixed; align-items:center`。內容一長，modal 撐破 viewport 且 fixed 置中不可捲 → footer（含總價）被裁到畫面外、SA 8 卡 / ChangePlanModal advisory 在矮 viewport 極易觸發。**必修 modal 外殼**：`.o_woow_modal` 加 `display:flex; flex-direction:column; max-height:calc(100dvh - 2rem)`；`.o_woow_modal_body` 加 `overflow-y:auto; flex:1; min-height:0`；`.o_woow_modal_footer` 加 `flex-shrink:0`。這是 modal 端總計條成立的**前提**（D3 夾帶項）。
3. **Mobile**：`.o_woow_bottom_nav` 是 `position:fixed; bottom:0; z-index:100`。sticky footer 無 z-index 時可能與它疊、Launch 被蓋 → 需明確層疊（desktop sticky / mobile 改回正常流倚賴 `.o_woow_content` 既有 `padding-bottom`），headed 實測。

**職責分離＋去重**：`PlanSelector` **不含**總價；抽**展示型共用子元件 `PlanTotalBar`**（4 處內嵌），只共用 `computePlanTotal` 邏輯會造成 4× markup 漂移（某 surface 漏 cycle 後綴／`pts/mo` vs `pts/月`）＝正是本 epic 要消滅的 P1 漂移。總價與席數 chip **取價共用同一 `resolveSelectedPrice`**，確保顯示同一個絕對總價、不漂出兩個數字。

### 難題 4 — 受控契約矛盾的化解

`PlanSelector` 現況零 `setup`/零 state（`PlanSelector.js:24-105`，全由 props 反推）。新增 `state.cycle` 是它首次持有內部 state。**化解**：`state.cycle` 是 view-state（顯示哪組），非 flow-state；`selectedPlanId` 仍是唯一「選擇」事實源。**desync 守線**：用 `onWillUpdateProps` 當 `selectedPlanId` 從外部變化時同步 `state.cycle = 該 plan 的 unit`（否則 `CreateSmartHomeModal` 切模板時 cycle 停舊值 → 高亮卡被藏，現況僅靠 loading spinner remount 巧合遮住）。**未選態（`selectedPlanId == null`）toggle 只切顯示、不 emit**（不 arm `canConfirm`、不誤 churn）。cycle 切換用 `group_key` O(1) 對映到同 tier 同席數、保留檔位。

### 難題 8 — 定位目前方案的資料鏈

`_get_subscription_info`（`subscription_lifecycle_mixin.py:499`，三 surface 共用）**無 `plan_id`** → ChangePlanModal 拿不到 currentPlanId。**PR-3 補 `plan_id`**。目前方案若是席數變體（`storefront_hidden=True`），它雖不作頂層卡片，但**已在 `base.variants` 子陣列裡**（`_attach_variants` 回全變體）→ 前端有 currentPlanId 即可反推 base+cycle+seats，**不需後端額外回 group_key/seats/cycle**。前提：該變體的 base 可見（除 orphan 外皆是）。

### 難題 6/9 — 增量與規格資料流

- 增量 chip ＝ `variant.price - base.price` + `base.unit`（現行 `formatSeatPrice` 已用 `basePlan.unit`，沿用勿退化）。不加購標「已含」（不顯示 `+0`）。`variants.length===1 && variants[0].seats===1`（mcp 固定 1 席）降級 checkbox。**change-plan 情境總計條顯示絕對總價為主、增量 badge 為輔**（避免 increment-over-base 被誤讀為 increment-over-current）。
- 規格集中到單一 model method `sh.subscription.plan._get_display_specs(template=None)` → `[{icon,label}]`。CS 用 `cs_vcpu or template.min_vcpu` / `cs_ram_gb or template.min_ram_gb`（**解 postgresql/anythingllm `cs_vcpu=0` 空白**）；SA 用 routes/policies；SH p2 用 MCP、p1 空。變體規格恆等於同 tier base，`_attach_variants` 不動（維持 `{seats,plan_id,price}`）。`PlanSelector` `plan.specs?.length` 才渲染、`service_type` 無感。

---

## PR-1：後端 additive-only（`.70`）

**目標**：plans 端點每個 base dict 加 `group_key`（cycle 對映用）＋ `specs`（規格顯示用）。**端點順序維持不動**（id 升序）。零 migration。

**Files:**
- Modify: `src/models/sh_subscription_plan_visibility.py`（加 `_group_keys` ＋ `_get_display_specs`）
- Modify: `src/controllers/paas.py:938`、`src/controllers/security_access.py:214`、`src/controllers/smart_home.py:293`（dict 加 2 欄）
- Test: `src/tests/test_plan_group_keys_368.py`（新建）
- Modify: `src/__manifest__.py`（版號 `.70`）

**Interfaces（PR-2/PR-3 依賴）:**
- Produces: 三端點 base dict 新增 `'group_key': str|None`（月/年同 tier 同值；無 xmlid=None）、`'specs': [{'icon': str, 'label': str}]`（無規格=`[]`）。
- Produces: `recordset._group_keys() -> {plan_id: group_key}`；`plan._get_display_specs(template=None) -> [{'icon','label'}]`。

- [ ] **T1 Step 1：寫 `_group_keys` 的失敗測試**（`src/tests/test_plan_group_keys_368.py`）

```python
# -*- coding: utf-8 -*-
from odoo.tests.common import TransactionCase, tagged

MOD = 'woow_paas_platform'


@tagged('post_install', '-at_install')
class TestPlanGroupKeys368(TransactionCase):

    def test_group_key_pairs_month_and_year(self):
        """每個可見掛 template 的 base group_key 恰有 {month, year} 兩筆，
        且 cycle 後綴與 sh_unit 一致（交叉斷言防手改 xml 打錯 cycle）。"""
        Plan = self.env['sh.subscription.plan']
        imd = self.env['ir.model.data'].sudo()
        # 收集三個 template model 掛載的可見 base
        bases = Plan.browse([])
        for tpl_model in ('woow_paas_platform.cloud_app_template',
                          'woow_paas_platform.smart_home_template',
                          'woow_paas_platform.security_access_template'):
            for tpl in self.env[tpl_model].sudo().search([]):
                bases |= tpl.subscription_plan_ids.filtered(
                    lambda p: p.ac_max_access_seats == 0 and not p.storefront_hidden)
        gk = bases._group_keys()
        # 反查 xmlid 供 cycle 後綴斷言
        rows = imd.search([('model', '=', 'sh.subscription.plan'),
                           ('module', '=', MOD), ('res_id', 'in', bases.ids)])
        name_by_id = {r.res_id: r.name for r in rows}
        by_group = {}
        for base in bases:
            key = gk.get(base.id)
            self.assertIsNotNone(key, name_by_id.get(base.id))
            suffix = name_by_id[base.id].rsplit('_', 1)[-1]  # monthly|yearly
            self.assertEqual(suffix, 'monthly' if base.sh_unit == 'month' else 'yearly',
                             'cycle 後綴與 sh_unit 不一致：%s' % name_by_id[base.id])
            by_group.setdefault(key, set()).add(base.sh_unit)
        for key, units in by_group.items():
            self.assertEqual(units, {'month', 'year'},
                             'group %s 缺月/年完整對：%s' % (key, units))
```

- [ ] **T1 Step 2：跑測試確認失敗** — `AttributeError: _group_keys`。

- [ ] **T1 Step 3：實作 `_group_keys`**（`sh_subscription_plan_visibility.py`，`_attach_variants` 之後）

```python
# 錨定 $ 只截 cycle 尾綴（module-level，_VARIANT_XMLID_RE 旁）
_CYCLE_SUFFIX_RE = re.compile(r'_(monthly|yearly)$')

    def _group_keys(self):
        """回 {base_plan_id: group_key}。group_key = base xmlid 去 _monthly/_yearly
        尾綴（月/年同 tier 得同 group_key，前端據此配對 cycle toggle）。

        只對【可見 base】（ac_max_access_seats==0 ∧ not storefront_hidden）算，
        單次批次 ir.model.data 反查（禁 N+1）。無 xmlid／不符 _cycle 慣例 →
        _logger.error＋略過（該 plan 前端 fallback 用 id 當獨立 group，端點不炸）。
        """
        bases = self.filtered(
            lambda p: p.ac_max_access_seats == 0 and not p.storefront_hidden)
        if not bases:
            return {}
        imd = self.env['ir.model.data'].sudo()
        rows = imd.search([
            ('model', '=', 'sh.subscription.plan'),
            ('module', '=', _SEED_MODULE),
            ('res_id', 'in', bases.ids),
        ])
        result = {}
        for row in rows:
            gk = _CYCLE_SUFFIX_RE.sub('', row.name)
            if gk == row.name:
                _logger.error(
                    '_group_keys：base xmlid %s 無 _monthly/_yearly 尾綴，略過',
                    row.name)
                continue
            result[row.res_id] = gk
        return result
```

- [ ] **T1 Step 4：跑測試確認通過** — `pytest`/odoo test `test_group_key_pairs_month_and_year` PASS。

- [ ] **T2 Step 1：寫 `_get_display_specs` 失敗測試**（同檔加 method）

```python
    def test_display_specs_by_service_type(self):
        """CS fallback template.min_*、SA routes/policies、SH p2 MCP。"""
        odoo_t1 = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        specs = odoo_t1._get_display_specs(tpl)
        self.assertTrue(any('vCPU' in s['label'] for s in specs))
        # postgresql cs_vcpu=0 → fallback template.min_vcpu 非空白
        pg = self.env.ref('woow_paas_platform.plan_postgresql_monthly')
        # （cloud_app_postgresql template min_vcpu 存在即應有 vCPU label）
        sa = self.env.ref('woow_paas_platform.plan_security_access_r1_monthly')
        sa_specs = sa._get_display_specs()
        self.assertTrue(any('route' in s['label'].lower() for s in sa_specs))
```

- [ ] **T2 Step 2：跑測試確認失敗** — `AttributeError: _get_display_specs`。

- [ ] **T2 Step 3：實作 `_get_display_specs`**（`sh_subscription_plan_visibility.py`）

```python
    def _get_display_specs(self, template=None):
        """前台卡片規格 [{'icon','label'}]，service_type 分流；一套元件走天下。

        CS：vCPU/RAM（plan.cs_* 為 0＝沿用 template 預設 → fallback template.min_*）。
        SA：routes/policies。SH：p2（mcp_integration 非空）顯示 MCP 加密、p1 無規格。
        變體規格恆等於同 tier base，故此 method 只需對 base 呼叫。
        """
        self.ensure_one()
        specs = []
        if self.service_type == 'cloud_service':
            vcpu = self.cs_vcpu or (template.min_vcpu if template else 0)
            ram = self.cs_ram_gb or (template.min_ram_gb if template else 0.0)
            if vcpu:
                specs.append({
                    'icon': 'memory',
                    'label': '%s vCPU · %s GB RAM' % (vcpu, ram),
                })
        elif self.service_type == 'security_access':
            specs.append({
                'icon': 'route',
                'label': '%s routes · %s policies' % (
                    self.sa_max_routes, self.sa_max_access_policies),
            })
        elif self.service_type == 'smart_home':
            if self.mcp_integration:
                specs.append({'icon': 'lock', 'label': 'MCP 加密連線'})
        return specs
```

- [ ] **T2 Step 4：跑測試確認通過**。

- [ ] **T3 Step 1：三端點加 `group_key` + `specs`（不動順序）**。`paas.py:api_template_plans` 迴圈前加 `group_keys = plan_recs._group_keys()`，dict 內加：

```python
                'group_key': group_keys.get(plan.id),
                'specs': plan._get_display_specs(template),
```

`security_access.py:api_sa_template_plans`：迴圈前 `group_keys = template.subscription_plan_ids._group_keys()`，dict 加 `'group_key': group_keys.get(p.id)` 與 `'specs': p._get_display_specs(template)`（SA template 無 min_*，`_get_display_specs` 的 SA 分支不吃 template，安全）。`smart_home.py:api_smarthome_template_plans` 同理（SH 分支不吃 template）。

- [ ] **T3 Step 2：neutrality guard 測試 — 斷言端點順序與 baseline 不變**

```python
    def test_endpoint_order_unchanged(self):
        """PR-1 additive-only：三端點回傳的可見 base 順序＝id 升序（今日行為），
        新增欄位不得重排。鎖死 neutrality，防日後把 UX 重排偷渡進後端。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        visible = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden)
        expected_ids = visible.sorted('id').ids
        # 直接比對 model 層順序（controller 迴圈保持 recordset 序，不 .sorted()）
        actual_ids = tpl.sudo().subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden).ids
        self.assertEqual(actual_ids, expected_ids)
```

- [ ] **T3 Step 3：跑全套 PR-1 測試** — 3 個新測試 PASS，`test_wave_c_variants_attach_330`／`test_plan_variants_generated` 保持綠。

- [ ] **T4：版號 bump `.70`** — `src/__manifest__.py` `"version": "18.0.1.0.70"`（開 PR 前 re-check 隊列）。

- [ ] **T5：sandbox 跑受影響測試附輸出原文** — `/woow-dev:k8s-dev-sandbox test`（模組測試），貼 PASS/FAIL 數字。

- [ ] **T6：開 PR** — zh-tw、`Refs #368`、引用本文件；描述註明「additive-only，端點順序不動、零 migration」。不 merge、回報 leader。

---

## PR-2：前端 `PlanSelector` 重設計（`.71`）

**目標**：cycle toggle 兩段式、group_key 分組排序、席數增量/checkbox 降級、規格顯示、退化規則 R1/R2/R3、`PlanTotalBar`、AppConfigurationPage sticky footer、**modal 外殼修復（D3 夾帶）**。一套元件走 3 個新購/config 嵌入點。

**Files:**
- Create: `src/static/src/paas/components/subscription/plan_selector_utils.js`（`pickDefaultPlan`、`resolveSelectedPrice`、`groupPlansByCycle`）
- Create: `src/static/src/paas/components/subscription/PlanTotalBar.js` + `.xml`
- Modify: `src/static/src/paas/components/subscription/PlanSelector.js` + `.xml`
- Modify: `src/static/src/paas/pages/configure/AppConfigurationPage.js:78`（改用 `pickDefaultPlan`）+ `.xml`（footer 內嵌 `PlanTotalBar`）
- Modify: `src/static/src/paas/components/modal/CreateSecurityAccessModal.js:64`、`src/static/src/paas/components/smart-home/CreateSmartHomeModal.js:85`（改用 `pickDefaultPlan`）+ 各 `.xml`（footer 內嵌 `PlanTotalBar`）
- Modify: `src/static/src/paas/components/subscription/subscription.scss`（toggle/增量/PlanTotalBar）、`src/static/src/paas/styles/pages/40_configure.scss`（sticky footer）、`src/static/src/paas/styles/30_components.scss`（modal 外殼 + `.o_woow_modal_lg`）
- Modify: `src/__manifest__.py`（版號 `.71`；註冊新 JS/XML 至 `assets_paas`）

**Interfaces（PR-3 依賴）:**
- Consumes（from PR-1）: plan dict 的 `group_key`、`specs`。
- Produces: `pickDefaultPlan(plans) -> id|undefined`；`resolveSelectedPrice(plans, id) -> number|null`；`PlanTotalBar` props `{plans, selectedPlanId, mode?}`（`mode='change'` 時絕對價為主）；`PlanSelector` 新增 props `currentPlanId?`（PR-3 用，PR-2 先加 optional 佔位不消費）。

- [ ] **T1 Step 1：`pickDefaultPlan`/`resolveSelectedPrice` 失敗測試**（若走 JS 單測；否則以 E2E 覆蓋，此處先寫 util）

```javascript
// plan_selector_utils.js
/** 決定性預設：最低 price × month × 0 席（base 自身）。month 組優先；
 *  組內 price 升序、id tie-breaker。空清單回 undefined。 */
export function pickDefaultPlan(plans) {
  const months = plans.filter((p) => p.unit === "month");
  const pool = months.length ? months : plans;
  if (!pool.length) return undefined;
  return [...pool].sort((a, b) => a.price - b.price || a.id - b.id)[0].id;
}

/** 選中 plan（base 或變體）的絕對總價；找不到回 null。總價條與席數 chip 共用。 */
export function resolveSelectedPrice(plans, selectedPlanId) {
  for (const plan of plans) {
    if (plan.id === selectedPlanId) return plan.price;
    const v = (plan.variants || []).find((x) => x.plan_id === selectedPlanId);
    if (v) return v.price;
  }
  return null;
}

/** 依 group_key 收攏 plans，每 group 含 month/year 兩 cycle 的 base。
 *  group_key 為 null（無 xmlid）時 fallback `id:<id>` 當獨立 group。
 *  回 [{groupKey, name, byCycle:{month, year}}]，group 依 base price 升序。 */
export function groupPlansByCycle(plans) {
  const groups = new Map();
  for (const p of plans) {
    const key = p.group_key || `id:${p.id}`;
    if (!groups.has(key)) groups.set(key, { groupKey: key, name: p.name, byCycle: {} });
    groups.get(key).byCycle[p.unit] = p;
  }
  return [...groups.values()].sort((a, b) => {
    const pa = (a.byCycle.month || a.byCycle.year).price;
    const pb = (b.byCycle.month || b.byCycle.year).price;
    return pa - pb;
  });
}
```

- [ ] **T1 Step 2：`PlanSelector.js` 重設計 — cycle view-state + 對映 + 退化規則**

```javascript
/** @odoo-module **/
import { Component, useState, onWillUpdateProps } from "@odoo/owl";
import { resolveSelectedPrice, groupPlansByCycle } from "./plan_selector_utils";

export class PlanSelector extends Component {
  static template = "woow_paas_platform.PlanSelector";
  static props = {
    plans: { type: Array },
    selectedPlanId: { type: Number, optional: true },
    onSelect: { type: Function },
    currentPlanId: { type: Number, optional: true }, // PR-3 用（PR-2 佔位）
  };

  setup() {
    this.state = useState({ cycle: this._cycleOf(this.props.selectedPlanId) || "month" });
    // desync 守線：selectedPlanId 從外部變化（如切模板/PR-3 定位）時同步 cycle。
    onWillUpdateProps((next) => {
      if (next.selectedPlanId !== this.props.selectedPlanId) {
        const c = this._cycleOf(next.selectedPlanId, next.plans);
        if (c) this.state.cycle = c;
      }
    });
  }

  // ---- cycle 反推（view-state 唯一來源）----
  _cycleOf(planId, plans = this.props.plans) {
    if (planId == null) return null;
    for (const p of plans) {
      if (p.id === planId) return p.unit;
      if ((p.variants || []).some((v) => v.plan_id === planId)) return p.unit;
    }
    return null;
  }

  get cycles() {
    return [...new Set(this.props.plans.map((p) => p.unit))];
  }
  get showCycleToggle() { return this.cycles.length > 1; } // R1

  // 當前 cycle 的分組卡片；R2＝groups.length<=1 前端不特判（仍渲染那一組）
  get visibleGroups() {
    return groupPlansByCycle(this.props.plans)
      .map((g) => g.byCycle[this.state.cycle])
      .filter(Boolean);
  }

  onCycleToggle(cycle) {
    this.state.cycle = cycle;
    const sel = this.props.selectedPlanId;
    if (sel == null) return; // 未選態：只切顯示，不 emit（不 arm canConfirm）
    const owner = this._ownerBase(sel);
    if (!owner) return;
    const seats = this._selectedSeats(sel, owner);
    const target = this._findInCycle(owner.group_key, cycle, seats);
    if (target != null) this.props.onSelect(target); // 保留檔位（同 tier 同席數）
  }

  // ---- 選中反推（受控，維持 selectedPlanId 唯一事實源）----
  _ownerBase(planId) {
    for (const p of this.props.plans) {
      if (p.id === planId) return p;
      if ((p.variants || []).some((v) => v.plan_id === planId)) return p;
    }
    return null;
  }
  _selectedSeats(planId, base) {
    if (planId === base.id) return 0;
    const v = (base.variants || []).find((x) => x.plan_id === planId);
    return v ? v.seats : 0;
  }
  _findInCycle(groupKey, cycle, seats) {
    if (!groupKey) return null; // 無 xmlid 的獨立 group 不支援跨 cycle 對映
    const target = this.props.plans.find(
      (p) => p.group_key === groupKey && p.unit === cycle);
    if (!target) return null; // 該 group 在此 cycle 無 base（R1 已擋 toggle，理論不達）
    if (seats === 0) return target.id;
    const v = (target.variants || []).find((x) => x.seats === seats);
    return v ? v.plan_id : target.id; // 該席數在新 cycle 不存在 → 退 0 席（E2E 需可見退場提示）
  }

  isSelected(base) { return this._ownerBase(this.props.selectedPlanId)?.id === base.id; }

  selectedBaseWithVariants() {
    const owner = this._ownerBase(this.props.selectedPlanId);
    return owner && owner.variants && owner.variants.length ? owner : null;
  }

  // R3：席數層渲染條件；variants.length===1 && seats===1 → checkbox
  isSeatCheckbox(base) {
    return base.variants && base.variants.length === 1 && base.variants[0].seats === 1;
  }
  isSeatSelected(base, variant) {
    const target = variant ? variant.plan_id : base.id;
    return this.props.selectedPlanId === target;
  }

  _unitLabel(u) { return u === "month" ? "mo" : u === "year" ? "yr" : u; }
  formatPrice(p) { return `${p.price} pts/${this._unitLabel(p.unit)}`; }
  // 保留既有 trial 徽章（重寫勿遺漏——251 plans 全 sh_trial=True；PR-2 review 抓到此遺漏）
  formatTrial(plan) {
    if (!plan.has_trial) return "";
    const unit = plan.trial_unit === "day" ? "day" : plan.trial_unit;
    return `${plan.trial_duration}-${unit} free trial`;
  }
  // 增量：一律 base.unit（年繳為年值，寫死 /月＝12 倍 bug）；不加購不顯示 +0
  formatSeatDelta(base, variant) {
    const delta = variant.price - base.price;
    return `+${delta} pts/${this._unitLabel(base.unit)}`;
  }

  onPlanClick(base) { this.props.onSelect(base.id); }
  onSeatSelect(base, variant) { this.props.onSelect(variant ? variant.plan_id : base.id); }
  onSeatCheckbox(base) {
    const v = base.variants[0];
    this.props.onSelect(this.props.selectedPlanId === v.plan_id ? base.id : v.plan_id);
  }
}
```

- [ ] **T1 Step 3：`PlanSelector.xml` — cycle toggle + 卡片（規格）+ 席數（增量/checkbox）**。關鍵片段：頂部 `t-if="showCycleToggle"` 的月/年 segmented control（綁 `onCycleToggle`）；卡片迴圈 `visibleGroups`（顯示 name/price + `t-foreach plan.specs` 一列 icon+label）；席數層 `selectedBaseWithVariants()` → `isSeatCheckbox` 分支渲染 checkbox（label 用 `formatSeatDelta`），否則渲染 chip 列（「不加購」標「已含」+ 各 variant `formatSeatDelta`）。

- [ ] **T2：`PlanTotalBar.js` + `.xml`**（展示型；`resolveSelectedPrice` 取價）

```javascript
/** @odoo-module **/
import { Component } from "@odoo/owl";
import { resolveSelectedPrice } from "../subscription/plan_selector_utils";

export class PlanTotalBar extends Component {
  static template = "woow_paas_platform.PlanTotalBar";
  static props = {
    plans: { type: Array },
    selectedPlanId: { type: Number, optional: true },
    mode: { type: String, optional: true }, // 'change'＝絕對價為主
  };
  get total() { return resolveSelectedPrice(this.props.plans, this.props.selectedPlanId); }
}
```

- [ ] **T3：三嵌入點改用 `pickDefaultPlan`** — `AppConfigurationPage.js:78`（`this.state.selectedPlanId = pickDefaultPlan(this.state.plans);`）、`CreateSecurityAccessModal.js:64`、`CreateSmartHomeModal.js:85` 同改；XML footer 內嵌 `<PlanTotalBar .../>`。

- [ ] **T4：SCSS — sticky footer**（`40_configure.scss:344`）

```scss
.o_woow_configure_footer {
    position: sticky;
    bottom: 0;
    z-index: 10;                       // 高於卡片、低於 modal(1000)/bottom-nav(100) 見 mobile 處理
    background: $woow-card-light;      // opaque，防下方卡片透出
    box-shadow: 0 -4px 12px rgba(0, 0, 0, 0.06);
    // 保留原 display:flex; justify-content:flex-end; gap; border-top
    align-items: center;
    justify-content: space-between;    // 左總價、右按鈕
    padding: 1rem 0;
    @media (max-width: 768px) {        // mobile 與 fixed .o_woow_bottom_nav(z-index:100) 共存
        position: static;              // 改回正常流，倚賴 .o_woow_content padding-bottom
    }
}
```

- [ ] **T5：SCSS — modal 外殼修復（D3 夾帶）**（`30_components.scss:242`）

```scss
.o_woow_modal {
    display: flex;
    flex-direction: column;
    max-height: calc(100dvh - 2rem);   // 內容長時 body 自捲、footer 常駐
    // 保留 background/border-radius/max-width/margin/box-shadow/animation
    &.o_woow_modal_lg { max-width: 760px; } // ChangePlanModal 加寬（新 class，不動 _sm）
}
.o_woow_modal_body { overflow-y: auto; flex: 1; min-height: 0; } // 加在原 padding 上
.o_woow_modal_footer { flex-shrink: 0; } // 加在原屬性上
```

- [ ] **T6：headed E2E（playwright-cli）** — 見下方「測試/E2E 矩陣」PR-2 段全部場景。

- [ ] **T7：版號 `.71` + 註冊 assets + 開 PR**。**PR 描述須含「夾帶全域 modal CSS 修復」獨立段落**（D3-a）：列出改動的 `.o_woow_modal`/`_body`/`_footer` 屬性、影響全 app modal、已抽查不回歸的 modal 清單。不 merge、回報 leader。

---

## PR-3：`ChangePlanModal` 強化（`.72`）

**目標**：後端補 `plan_id` → 前端定位目前方案、canConfirm 差異檢查、760px、下週期生效提示。

**Files:**
- Modify: `src/models/subscription_lifecycle_mixin.py:499`（`_get_subscription_info` 加 `plan_id`）
- Modify: `src/static/src/paas/components/modal/ChangePlanModal.js` + `.xml`
- Modify: `src/static/src/paas/pages/service/ServiceDetailPage.xml:219`、`security-access/SecurityAccessDetailPage.xml:410`、`smart-home/SmartHomeDetailPage.xml:200`（傳 `currentPlanId`）
- Test: `src/tests/test_subscription_info_plan_id_368.py`
- Modify: `src/__manifest__.py`（版號 `.72`）

**Interfaces:**
- Consumes（from PR-2）: `PlanSelector` 的 `currentPlanId` prop、`PlanTotalBar` 的 `mode='change'`。

- [ ] **T1 Step 1：`_get_subscription_info` 加 `plan_id` 失敗測試**

```python
    def test_subscription_info_has_plan_id(self):
        # 對一個已有訂閱的 cloud service 斷言 _get_subscription_info 帶 plan_id
        svc = ...  # 既有 fixture 或建一個帶訂閱的 service
        info = svc._get_subscription_info()
        self.assertIn('plan_id', info)
        self.assertEqual(info['plan_id'], svc.subscription_id.sh_subscription_plan_id.id)
```

- [ ] **T1 Step 2：實作** — `subscription_lifecycle_mixin.py:502` 附近加：

```python
            'plan_id': sub.sh_subscription_plan_id.id if sub.sh_subscription_plan_id else None,
```

- [ ] **T2：`ChangePlanModal.js` 強化**

```javascript
  static props = {
    service: { type: Object },
    currentPlanId: { type: Number, optional: true }, // 定位目前方案 + canConfirm 差異
    loadPlans: { type: Function },
    confirmChange: { type: Function },
    onClose: { type: Function },
    onChanged: { type: Function },
  };

  setup() {
    this.state = useState({
      plans: [],
      // 開啟即定位目前方案（cycle×group×seats 由 PlanSelector 反推）；
      // undefined（無 currentPlanId）維持未選、避免誤 churn。
      selectedPlanId: this.props.currentPlanId ?? undefined,
      loadingPlans: true, plansError: null, changing: false, error: null,
    });
    onMounted(() => this.loadPlans());
  }

  get canConfirm() {
    return (
      !this.state.loadingPlans && !this.state.plansError &&
      !!this.state.selectedPlanId &&
      this.state.selectedPlanId !== this.props.currentPlanId && // 不可 churn 成同方案
      !this.state.changing
    );
  }
```

- [ ] **T3：`ChangePlanModal.xml`** — 外層 `o_woow_modal_sm` 改 `o_woow_modal_lg`；`PlanSelector` 加 `currentPlanId="props.currentPlanId"`；footer 前加 `<PlanTotalBar plans="state.plans" selectedPlanId="state.selectedPlanId" mode="'change'"/>`；advisory 區加一行「**資源將於下個計費週期生效**」：

```xml
        <!-- #342 未落地前的誠實處置：換方案的規格於下個週期才 re-apply。
             TODO(Refs #342)：#342 落地後移除此行。 -->
        <div class="o_woow_change_plan__notice">
          <WoowIcon name="'schedule'" size="14"/>
          <span>資源將於下個計費週期生效。</span>
        </div>
```

- [ ] **T4：三 detail page 傳 `currentPlanId`** — 各 `<ChangePlanModal>` 加 `currentPlanId="serviceForModal.subscription?.plan_id"`（SA=`state.sa.subscription?.plan_id`、SH=`home.subscription?.plan_id`）。

- [ ] **T5：headed E2E** — 見矩陣 PR-3 段。

- [ ] **T6：建 `Refs #342` follow-up 追蹤**（`docs/tracking/followups/follow-ups.md` 加一條：#342 落地後移除 ChangePlanModal「下週期生效」提示 + PR-1 change-plan re-apply）＋ 版號 `.72` ＋ 開 PR。不 merge、回報 leader。

---

## 測試 / headed E2E 矩陣

### PR-1（Odoo test）
- `test_group_key_pairs_month_and_year`：每可見掛 template base group_key 恰 {month,year} + cycle 後綴與 sh_unit 一致。
- `test_display_specs_by_service_type`：CS fallback template.min_*、SA routes/policies、SH p2 MCP。
- `test_endpoint_order_unchanged`：neutrality guard（端點順序＝id 升序不變）。
- 回歸綠：`test_wave_c_variants_attach_330`、`test_plan_variants_generated`。

### PR-2（headed E2E，playwright-cli）
1. 新購三步 + cycle toggle **保留檔位**：選 odoo t2+3 席（月）→ 切年 → 應為 t2+3 席（年）被選、總價即時更新。
2. 決定性預設：進 Configure（odoo）→ 預選＝最低價 month base（1-3 人月繳）。
3. **SA tier 正序**：CreateSecurityAccessModal 卡片由上到下 r1(1組)→r3(3組)→entry(6組)→pro(10組)（唯一暴露 group_key 字串排序 bug 的家族）。
4. 退化規則：mcp → checkbox「+1 access seat」；postgresql/anythingllm（R2 單 tier）→ 無分組層仍正常；HA（R1 只 monthly）→ 無 cycle toggle。
5. sticky footer：scroll **top/mid/bottom** 三處斷言總價條完整可見（mid 抓原 bug）；捲到中段點席數/切 cycle → 總價即時更新且與選中 chip 同數字；背景不透明。
6. 增量單位：odoo t1 選 1 席，月顯示 `+600/mo`、切年顯示 `+4800/yr`（**非 +4800/mo**）。
7. 規格 fallback：postgresql/anythingllm 卡片顯示 vCPU/RAM（template.min_*）非空白。
8. **矮 viewport modal（D3-b）**：1280×640 / 390×720 開 CreateSecurityAccessModal（8 卡）→ footer 總價+Create 可見可點、modal 未撐破 viewport（body 自捲）。
9. **其他 modal 不回歸抽查（D3-b）**：挑 2-3 個常用 modal（如 `AdminPasswordModal`、`SkipTrialModal`、`AddPolicyModal`）各開一遍，確認外殼 flex/max-height 改動未破版。

### PR-3（headed E2E）
1. ChangePlanModal 開啟 → 目前方案被**預選高亮**（依 plan_id 定位）、760px 寬版。
2. **未選態不 arm**：（若 currentPlanId 缺）不選任何方案切 toggle → 確認鈕維持 disabled。
3. **不可 churn 成同方案**：selectedPlanId===currentPlanId 時 canConfirm=false。
4. 改席數/改 cycle：選不同方案 → confirm enabled；mount/toggle 當下**無** change-plan RPC（監看 network），按確認才送出。
5. 「資源於下個週期生效」提示顯示。

## 版號 / migration 計畫
- PR-1 `.70` / PR-2 `.71` / PR-3 `.72`（開 PR 前逐一 re-check 隊列與 open PRs、撞號順延）。
- **全程無 migration**（方案 C 零 schema）——難題 1「noupdate=1 陷阱」的最佳解＝不動 seed data 就不觸發。

## Follow-ups / Scope 注意
- **modal 外殼 CSS（D3）** 是全域 blast radius 的夾帶 pre-existing bug 修復；PR-2 描述獨立段落＋E2E 抽查其他 modal 不回歸。
- **`Refs #342`**：ChangePlanModal「下週期生效」提示 + 生效 re-apply，待 #342 落地後移除（PR-3 T6 建 follow-up 條目）。
- SA/SH 端點原有 `max_routes` 等欄位前端 0 消費者；`specs` 是新 payload，不破壞既有。

## Self-Review
- **Spec coverage**：P1（8卡→toggle，難題1/2/4/5）✅、P3（席數增量，難題6）✅、規格（難題9）✅、決定性預設（難題3）✅、ChangePlanModal（難題8）✅、總計條（難題7）✅、退化規則 R1/R2/R3 × 4 嵌入點 ✅（E2E 矩陣覆蓋）。
- **Scope 不做**：P2 含稅/P4/第4軸 — 全計畫無觸碰 ✅。
- **型別一致**：`group_key: str|None`、`specs: [{icon,label}]`、`pickDefaultPlan→id|undefined`、`resolveSelectedPrice→number|null`、`currentPlanId?: Number` — PR 間簽章一致 ✅。
- **決定性預設「sort_order」落差**：issue 字面「最低 sort_order」映射為「最低 price」（方案 C 無 sort_order 欄位），已於難題3/D 記錄。
