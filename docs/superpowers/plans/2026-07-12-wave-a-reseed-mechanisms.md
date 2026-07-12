# Wave A Re-seed 機制件實作計畫（A1–A6，三 PR）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **落檔備註**：本檔已由 leader 落至正式位置並 commit——Task 0 的 Step 0（copy plan 進 repo）**已完成，跳過**，直接從 Step 1 校驗開始。

**Goal:** 落地 re-seed 波 Wave A 六個機制件——plan 前台隱藏旗標、每帳號限一 enforcement、HA PaaS 0 元 plan、MCP Manage 上架骨架、封存 Hermes、CS tier→部署規格映射——拆三個獨立 PR（版號 .54/.55/.56）。

**Architecture:** 全部循平台既有 add-only 擴充 pattern（`_inherit='sh.subscription.plan'` 各檔加欄位；taxonomy §3.5 佈局）。Seed 改動一律雙軌（noupdate XML＝fresh 軌＋migration force-write＝升級軌、`env.ref()` xmlid 定位）。規格映射採「runtime overlay」而非 template 改寫——避開 template sticky noupdate 這座山。

**Tech Stack:** Odoo 18（Python 4-space）、TransactionCase 測試、migration delegate-to-model 慣例（循 `.52`）。

## Global Constraints（每個 task 隱含適用）

- 版號鏈與 merge 順序綁定：PR-A1＝`18.0.1.0.54`、PR-A2＝`.55`、PR-A3＝`.56`；**migration 目錄版號必須＝該 PR 改完後的 manifest 版號**（F4 鐵律：目錄 ≤ 改動前版號會被靜默跳過）。三 PR 依 A1→A2→A3 順序開與 merge；若順序被打亂，版號與 migration 目錄必須重排。
- Migration 一律 `env.ref('woow_paas_platform.<xmlid>')` 定位，嚴禁用價格／名稱 match（R8 撞價教訓）。
- Migration 本體 delegate 到 model method（循 `.52` 的 `_woow_migrate_pricing_330` 慣例），供單元測試直驅。
- 新 model 檔必須 import 進 `src/models/__init__.py`（漏 import → `-u` 載 seed 直接炸）；新測試檔 import 進 `src/tests/__init__.py`（依既有慣例）。
- 源碼註解、commit message、PR 描述一律 zh-tw；commit prefix 英文小寫（feat:/fix:/test:/docs:）；**commit message 不得出現任何 AI 生成標記**。
- 測試檔命名循 `test_<feature>_<issue#>.py`；TransactionCase；斷言 pattern 參照 `src/tests/test_pricing_seed_330.py`（`EXPECTED_PLANS` dict 模式）。
- **禁止讀取本 repo 以外的任何 sibling repo**（含 `/Users/eugene/Documents/woow/woow-paas-repo/WOOWTECH/sh_subscription/`）——上游欄位語意一律以平台側用法為準；本 plan 的設計**不依賴**上游是否有 `active`／`website_published` 欄位。
- 工作方式：git worktree 隔離（base＝最新 `origin/develop`），不污染主 repo；TDD（先紅後綠）；頻繁 commit。
- 執行期間唯讀參考：`docs/plans/2026-Q3/2026-07-10-subscription-plan-matrix.md`（總表）、`docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md`（taxonomy）。

## 考古事實基礎（WaveA-Scout 2026-07-12，行號以 .53 為準）

| 事實 | 位置 |
|---|---|
| plan 擴充檔四件套（service_type/sa_quota/sh_mcp/ac） | `src/models/sh_subscription_plan_*.py` |
| plan 層**無任何**可見性旗標；前端 plans 入口 3 處 iterate `template.subscription_plan_ids` 零過濾 | `src/controllers/paas.py:919-940`、`smart_home.py:254-287`、`security_access.py:200-227` |
| template 層已有 `is_active`（default True）且三個 template 列表 API 已過濾 | `src/models/cloud_app_template.py:185-189`、`paas.py:797` |
| CS 建立鏈：route `api_workspace_services` → `_create_service(workspace, template_id, name, values, odoo_addon_ids, plan_id)` → `CloudService.create` → `_create_and_confirm_subscription` | `paas.py:1101` → `:2060` → `:2198` → `:2206` |
| partner 導出：`workspace.owner_id.partner_id` | `paas.py:2201` |
| `_has_paid_current_cycle`：查 wallet deduct done；0 元 invoice 走 super() 不產 deduct → gate 恆 False → 退點恆 0（天然安全） | `src/models/sh_subscription_fix.py:43-70`、`:312`、`:529` |
| trial per-plan 可關：`sh_trial` eval、mixin 依 `plan.sh_trial` 分支 | `subscription_products.xml:34-36`、`subscription_lifecycle_mixin.py:168-171` |
| Hermes 6 plans（1500/15000/4500/45000/6000/60000）＋6 products＋M2M | plans `subscription_products.xml:486-576`、products `:912-962`、links `subscription_template_links.xml:27-32` |
| MCP admin template 參照（chart oci mcp-server 0.1.5、image digest 釘死、config.nonSensitive.mcp_server） | `cloud_app_templates.xml:252-292`（`cloud_app_mcp_odoo`） |
| `allocated_*` 建服務時一律抄 `template.min_*` | `paas.py:2171-2173` |
| Odoo workers=0 硬寫在 template seed 的 init script（＋limit_memory 0/0、limit_time 60/120） | `cloud_app_templates.xml:170` |
| `_heal_sticky_helm_value_specs` 只 heal noupdate flag＋helm_value_specs，不碰 helm_default_values | `cloud_app_template.py:232-286` |
| CloudService 無 plan_id；有 `subscription_id`（set null）；plan 反查鏈 `subscription_id.sh_subscription_plan_id` | `subscription_lifecycle_mixin.py:64`、`:152` |
| 版號 `18.0.1.0.53`；migrations 最新 `.52`（delegate 慣例） | `src/__manifest__.py:3` |

---

# Part 0：現狀校驗（開工必跑，一次性）

### Task 0: 校驗清單與基線

**Files:**
- Read only（本 task 不改碼）＋ Copy: 本 plan 檔 → `docs/superpowers/plans/2026-07-12-wave-a-reseed-mechanisms.md`

**Interfaces:**
- Produces: 校驗結果表（填進本 plan 檔尾「Task 0 校驗結果」節），後續 task 引用其中的具名參數

- [ ] **Step 0: 把本 plan copy 進 repo 並 commit**（`docs/superpowers/plans/`；commit `docs: Wave A re-seed 機制件實作計畫`）
- [ ] **Step 1: 逐項校驗並記錄**（與 plan 假設不符者，回報 leader 再動工，不得靜默改設計）：
  1. `CloudService` 的 `state` Selection 完整值集與「刪除終態」值（`src/models/cloud_service.py`，找 `state = fields.Selection`）→ 記為參數 **`TERMINAL_STATES`**（供 Task A2-2 計數 domain 排除）。
  2. `_create_service` 內 helm values 組裝段的確切行號與函式流（`paas.py:2060-2198` 區間内組 values 的段落＋`cloud_service.py` 內接收 helm values 的欄位/方法）→ 記為 **`HELM_ASSEMBLY_SITE`**（供 Task A6-4 落點）。
  3. `cloud_app_odoo`／`cloud_app_n8n` 的 `min_vcpu/min_ram_gb/min_storage_gb` 現值＋`helm_default_values.resources`／`persistence.size` 現值（`cloud_app_templates.xml`）→ 記為 **`T1_BASELINE`**（供 A6 T1 對齊斷言：T1 cs_* 必須等於現行值，不 regress）。
  4. `subscription_products.xml` 既有 plan record 的完整欄位格式範本（取 `plan_smarthome_monthly` @:318 全段）→ 供 A3/A4 新 record 鏡射格式（reminder ref 寫法、eval 格式）。
  5. `src/tests/__init__.py` 的 import 慣例。
  6. 既有 suite 綠基線：跑 `test_pricing_seed_330.py` 全類（沙盒或本地 docker 模式，見 CLAUDE.md「本機 docker-desktop 跑 Odoo 測試」memory 對應 recipe）→ 記錄基線結果。
  7. `api_workspace_services`（`paas.py:1101`）action='create' 分派內 `plan_id` 取值行（Scout 未複驗項）。
  8. Hermes 6 筆行號複驗（:486-576 plans、:912-962 products）。
- [ ] **Step 2: 把校驗結果表 commit 進 plan 檔尾**（`docs: Wave A Task 0 校驗結果`）

---

# Part 1：PR-A1「plan 可見性＋限一＋HA PaaS 0 元＋封存 Hermes」（.54、Closes #336 平台段）

> PR 標題：`feat: plan 前台隱藏旗標＋每帳號限一＋HA PaaS 0 元 plan＋封存 Hermes（Refs #336）`
> PR 描述必含：Closes 不寫（#336 尚有 OAuth 開通 API 段在別線，寫 `Refs #336`）；out-of-scope 聲明（開通 API＝#337 OAuth 線；HA template seed＝等 Elmo 輕量 image）。

### Task A1-1: `storefront_hidden` 欄位＋三入口過濾

**Files:**
- Create: `src/models/sh_subscription_plan_visibility.py`
- Modify: `src/models/__init__.py`（append import）
- Modify: `src/controllers/paas.py:928` 附近（`api_template_plans` 的 plan 迴圈）
- Modify: `src/controllers/smart_home.py:287` 附近（同）
- Modify: `src/controllers/security_access.py:227` 附近（同）
- Test: `src/tests/test_plan_visibility_336.py`

**Interfaces:**
- Produces: `sh.subscription.plan.storefront_hidden`（Boolean, default False）——A3/A5/後續 wave 消費；三個 plans API 一律排除 `storefront_hidden=True`。

**設計論證（reviewer 請驗）**：不複用 Odoo `active` 慣例——`active=False` 會被 ORM `active_test` 全域過濾，帶封存 plan 的**既存訂閱**在任何 search 路徑（cron、結算、報表）都會靜默漏掉（repo 已有實證教訓）；`storefront_hidden` 語意精確＝「前台購買選項不顯示，其餘機制照常」。

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_plan_visibility_336.py
# -*- coding: utf-8 -*-
"""#336：plan 前台隱藏旗標——storefront_hidden 欄位與 API 過濾。"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPlanStorefrontHidden(TransactionCase):
    """storefront_hidden 欄位語意：前台不顯示、機制照常。"""

    def test_field_exists_default_false(self):
        plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        self.assertIn('storefront_hidden', plan._fields)
        self.assertFalse(plan.storefront_hidden)

    def test_hidden_plan_still_searchable(self):
        """隱藏≠active=False：一般 search 仍看得到（cron／結算不受影響）。"""
        plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        plan.storefront_hidden = True
        found = self.env['sh.subscription.plan'].search([('id', '=', plan.id)])
        self.assertEqual(found, plan)
```

- [ ] **Step 2: 跑測試驗失敗**

Run: 沙盒/本地 docker 跑 `--test-tags /woow_paas_platform:TestPlanStorefrontHidden`
Expected: FAIL（`storefront_hidden` not in `_fields`）

- [ ] **Step 3: 最小實作**

```python
# src/models/sh_subscription_plan_visibility.py
# -*- coding: utf-8 -*-
"""#336：plan 前台可見性控制（add-only 擴充，taxonomy §3.5 佈局）。

storefront_hidden 刻意不用 Odoo `active`：active=False 會被 active_test
全域過濾，帶隱藏 plan 的既存訂閱在 cron／結算 search 會被靜默漏掉。
"""
from odoo import fields, models


class ShSubscriptionPlanVisibility(models.Model):
    _inherit = 'sh.subscription.plan'

    storefront_hidden = fields.Boolean(
        string='前台隱藏',
        default=False,
        help='勾選後此方案不出現在 /woow 前台任何購買選項；'
             '既存訂閱的計費、續期、退點完全不受影響。',
    )
```

`src/models/__init__.py` append（照既有排序位置、鄰近其他 sh_subscription_plan_* import）：

```python
from . import sh_subscription_plan_visibility
```

- [ ] **Step 4: 跑測試驗通過**（同 Step 2 指令，Expected: PASS）
- [ ] **Step 5: Commit**：`feat: sh.subscription.plan 新增 storefront_hidden 前台隱藏旗標（Refs #336）`
- [ ] **Step 6: 寫 API 過濾失敗測試**（append 至同測試檔）

```python
@tagged('post_install', '-at_install')
class TestPlansApiFiltersHidden(TransactionCase):
    """三個 plans API 一律排除 storefront_hidden。"""

    def _hidden_plan_ids_from(self, plans_payload):
        return {p['id'] for p in plans_payload}

    def test_template_plans_helper_excludes_hidden(self):
        """以 CS 家族驗證：隱藏 plan 不進 API 回傳集合。

        直測 controller 私有邏輯較脆——改為斷言過濾語意本身：
        visible = template.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden)
        並斷言三個 controller 皆改用此語意（見 Step 8 grep 斷言）。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        all_plans = template.sudo().subscription_plan_ids
        self.assertTrue(all_plans, '前置：Hermes template 應有掛 plans')
        one = all_plans[0]
        one.storefront_hidden = True
        visible = all_plans.filtered(lambda p: not p.storefront_hidden)
        self.assertNotIn(one, visible)
        self.assertEqual(len(visible), len(all_plans) - 1)
```

- [ ] **Step 7: 跑測試**——此測試對 filtered 語意天然綠；真正的紅綠在 Step 8 的 controller 改動斷言。先確認綠。
- [ ] **Step 8: 修改三個 controller**——在各自 plan 迴圈來源加 `.filtered(lambda p: not p.storefront_hidden)`：

```python
# paas.py api_template_plans（:928 附近），原：
#   for plan in template.sudo().subscription_plan_ids:
# 改為：
    for plan in template.sudo().subscription_plan_ids.filtered(
        lambda p: not p.storefront_hidden
    ):
```

`smart_home.py:287`、`security_access.py:227` 的 plan 迴圈**同樣三行式改法**（各自變數名照舊）。同時在測試檔 append 守衛測試（防未來回退）：

```python
    def test_three_controllers_filter_hidden(self):
        """機械守衛：三個 controller 的 plans 迴圈必須帶 storefront_hidden 過濾。"""
        import inspect
        from odoo.addons.woow_paas_platform.controllers import (
            paas, smart_home, security_access,
        )
        for mod in (paas, smart_home, security_access):
            src = inspect.getsource(mod)
            self.assertIn('storefront_hidden', src,
                          f'{mod.__name__} 的 plans API 漏掉 storefront_hidden 過濾')
```

- [ ] **Step 9: 跑本檔全部測試**（Expected: PASS）
- [ ] **Step 10: Commit**：`feat: 三個 plans API 過濾 storefront_hidden（Refs #336）`

### Task A1-2: 封存 Hermes 6 筆（雙軌）

**Files:**
- Modify: `src/data/subscription_products.xml`（Hermes 6 plan records @:486-576 各加一欄）
- Create: `src/migrations/18.0.1.0.54/post-migrate.py`
- Modify: `src/models/sh_subscription_plan_visibility.py`（加 migration delegate method）
- Modify: `src/__manifest__.py:3`（`18.0.1.0.53` → `18.0.1.0.54`）
- Test: `src/tests/test_plan_visibility_336.py`（append）

**Interfaces:**
- Consumes: Task A1-1 的 `storefront_hidden`
- Produces: `_woow_migrate_hide_hermes_336()`（model method，migration 直驅＋測試直驅）

- [ ] **Step 1: 寫失敗測試**（append）

```python
HERMES_PLAN_XMLIDS = [
    'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
    'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
    'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
]


@tagged('post_install', '-at_install')
class TestHermesArchived(TransactionCase):
    """A5：Hermes 6 筆封存（前台不可見、價格與既存訂閱不動）。"""

    def test_hermes_seed_hidden(self):
        for xmlid in HERMES_PLAN_XMLIDS:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertTrue(plan.storefront_hidden, f'{xmlid} 應為前台隱藏')

    def test_hermes_prices_untouched(self):
        expected = {
            'plan_mcp_hermes_monthly': 1500, 'plan_mcp_hermes_yearly': 15000,
            'plan_mcp_hermes_t2_monthly': 4500, 'plan_mcp_hermes_t2_yearly': 45000,
            'plan_mcp_hermes_t3_monthly': 6000, 'plan_mcp_hermes_t3_yearly': 60000,
        }
        for xmlid, price in expected.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price)

    def test_migration_idempotent(self):
        """migration delegate 可重跑（升級軌）。"""
        Plan = self.env['sh.subscription.plan']
        Plan._woow_migrate_hide_hermes_336()
        Plan._woow_migrate_hide_hermes_336()
        for xmlid in HERMES_PLAN_XMLIDS:
            self.assertTrue(
                self.env.ref(f'woow_paas_platform.{xmlid}').storefront_hidden)
```

- [ ] **Step 2: 跑測試驗失敗**（Expected: FAIL——seed 未帶 hidden、method 不存在）
- [ ] **Step 3: 實作**——(a) XML：Hermes 6 筆 plan record 各加（放進各 record 既有欄位區塊尾）：

```xml
<field name="storefront_hidden" eval="True"/>
```

(b) delegate method（append 至 `sh_subscription_plan_visibility.py`）：

```python
    def _woow_migrate_hide_hermes_336(self):
        """A5 封存 Hermes：升級軌 force-write（noupdate XML 不會回填既有 record）。

        一律 env.ref() xmlid 定位（R8 鐵律）；xmlid 不存在（fresh DB 未載 seed）
        時跳過不炸。冪等。
        """
        xmlids = [
            'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
            'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
            'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
        ]
        for xmlid in xmlids:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.storefront_hidden = True
```

(c) migration（循 `.52` delegate 慣例）：

```python
# src/migrations/18.0.1.0.54/post-migrate.py
# -*- coding: utf-8 -*-
"""#336 Wave A：封存 Hermes 6 筆（storefront_hidden force-write）。"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_hide_hermes_336()
```

(d) `src/__manifest__.py:3` 版號 → `'18.0.1.0.54'`

- [ ] **Step 4: 跑測試驗通過**（本檔全類 PASS；並跑 `test_pricing_seed_330.py` 確認無回歸——EXPECTED_PLANS 斷言 record 價/名，不受 hidden 影響，應綠）
- [ ] **Step 5: Commit**：`feat: 封存 Hermes 6 筆 plan（storefront_hidden 雙軌 seed+migration .54，Refs #336）`

### Task A2-1: CloudAppTemplate `max_per_partner` 欄位

**Files:**
- Modify: `src/models/cloud_app_template.py`（`is_active` @:185-189 附近 append 欄位）
- Test: `src/tests/test_template_max_per_partner_336.py`

**Interfaces:**
- Produces: `cloud.app.template.max_per_partner`（Integer, default 0＝不限）——Task A2-2 enforcement 消費；HA PaaS template（未來 seed）設 1。

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_template_max_per_partner_336.py
# -*- coding: utf-8 -*-
"""#336：每帳號（partner）限一 enforcement。"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError


@tagged('post_install', '-at_install')
class TestMaxPerPartnerField(TransactionCase):

    def test_field_exists_default_zero(self):
        tpl = self.env['cloud.app.template'].search([], limit=1)
        self.assertTrue(tpl, '前置：seed 應有 template')
        self.assertIn('max_per_partner', tpl._fields)
        self.assertEqual(tpl.max_per_partner, 0)
```

- [ ] **Step 2: 跑測試驗失敗**（FAIL：欄位不存在）
- [ ] **Step 3: 實作**（`cloud_app_template.py`，緊接 `is_active` 欄位後）

```python
    max_per_partner = fields.Integer(
        string='每帳號實例上限',
        default=0,
        help='同一 partner（帳號）最多可持有的本範本活躍實例數；'
             '0＝不限制。HA PaaS 免費方案設 1。',
    )
```

- [ ] **Step 4: 跑測試驗通過**
- [ ] **Step 5: Commit**：`feat: cloud.app.template 新增 max_per_partner 每帳號實例上限欄位（Refs #336）`

### Task A2-2: model 層 enforcement＋冪等查詢 helper

**Files:**
- Modify: `src/models/cloud_service.py`（create override＋helper）
- Test: `src/tests/test_template_max_per_partner_336.py`（append）

**Interfaces:**
- Consumes: `max_per_partner`（A2-1）、Task 0 的 **`TERMINAL_STATES`**
- Produces: `CloudService._find_active_for_partner(template, partner)`（recordset；供未來 #337 開通 API 冪等「已有則回既有」）；create 超限 raise `ValidationError`

**設計論證（reviewer 請驗）**：enforcement 放 **model create**（所有入口——controller、未來開通 API、shell——都吃到），controller 不另做早驗（Odoo JSON-RPC 對 ValidationError 已有標準錯誤封裝；避免同一規則兩份漂移）。計數口徑＝該 partner 名下、該 template、state 不在刪除終態的 CloudService 數。

- [ ] **Step 1: 寫失敗測試**（append；fixture 用 throwaway template＋workspace，不依賴 seed）

```python
@tagged('post_install', '-at_install')
class TestMaxPerPartnerEnforcement(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner_a = self.env['res.partner'].create({'name': '甲'})
        self.partner_b = self.env['res.partner'].create({'name': '乙'})
        self.user_a = self.env['res.users'].create({
            'name': '甲', 'login': 'limit_a@test', 'partner_id': self.partner_a.id,
        })
        self.user_b = self.env['res.users'].create({
            'name': '乙', 'login': 'limit_b@test', 'partner_id': self.partner_b.id,
        })
        # workspace／template fixture：欄位集以 Task 0 校驗的實際必填為準補齊
        self.ws_a = self.env['woow_paas_platform.workspace'].create({
            'name': 'ws-a', 'owner_id': self.user_a.id,
        })
        self.ws_b = self.env['woow_paas_platform.workspace'].create({
            'name': 'ws-b', 'owner_id': self.user_b.id,
        })
        self.tpl = self.env['cloud.app.template'].create({
            'name': 'limit-tpl', 'slug': 'limit-tpl',
            'max_per_partner': 1,
            # 其餘必填欄位依 Task 0 校驗補齊（chart 名等）
        })

    def _svc_vals(self, ws):
        return {
            'name': 'svc', 'workspace_id': ws.id, 'template_id': self.tpl.id,
            # 其餘必填欄位依 Task 0 校驗補齊
        }

    def test_second_create_blocked(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        with self.assertRaises(ValidationError):
            Svc.create(self._svc_vals(self.ws_a))

    def test_different_partner_not_blocked(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        svc_b = Svc.create(self._svc_vals(self.ws_b))
        self.assertTrue(svc_b)

    def test_zero_means_unlimited(self):
        self.tpl.max_per_partner = 0
        Svc = self.env['woow_paas_platform.cloud_service']
        Svc.create(self._svc_vals(self.ws_a))
        self.assertTrue(Svc.create(self._svc_vals(self.ws_a)))

    def test_find_active_for_partner(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        svc = Svc.create(self._svc_vals(self.ws_a))
        found = Svc._find_active_for_partner(self.tpl, self.partner_a)
        self.assertEqual(found, svc)
        self.assertFalse(Svc._find_active_for_partner(self.tpl, self.partner_b))
```

註：model 名（`woow_paas_platform.workspace`／`woow_paas_platform.cloud_service`）與必填欄位以 Task 0 校驗為準；如 fixture 缺欄位在 setUp 補齊，**測試斷言本身不得改弱**。

- [ ] **Step 2: 跑測試驗失敗**（FAIL：helper 不存在、create 不擋）
- [ ] **Step 3: 實作**（`cloud_service.py`；`TERMINAL_STATES` 用 Task 0 校驗值，此處以 `('deleted', 'terminated')` 為佔位示意——**實作時必須換成校驗值並在 commit message 註明**）

```python
    @api.model
    def _find_active_for_partner(self, template, partner):
        """回傳該 partner 名下、該 template 的活躍實例（排除刪除終態）。

        供 max_per_partner enforcement 與開通 API 冪等（#337）共用。
        """
        return self.sudo().search([
            ('template_id', '=', template.id),
            ('workspace_id.owner_id.partner_id', '=', partner.id),
            ('state', 'not in', list(TERMINAL_STATES)),
        ])

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            limit = rec.template_id.max_per_partner
            if limit and rec.workspace_id.owner_id:
                partner = rec.workspace_id.owner_id.partner_id
                count = len(self._find_active_for_partner(rec.template_id, partner))
                if count > limit:
                    raise ValidationError(_(
                        '「%(tpl)s」每個帳號最多 %(limit)s 個實例，'
                        '您已達上限。', tpl=rec.template_id.name, limit=limit))
        return records
```

（若 `cloud_service.py` 已有 create override，把限一檢查併入既有 override，勿疊兩個。）

- [ ] **Step 4: 跑測試驗通過**（4 條全綠）
- [ ] **Step 5: Commit**：`feat: CloudService 每帳號實例上限 enforcement＋冪等查詢 helper（Refs #336）`

### Task A3-1: HA PaaS 0 元 plan seed

**Files:**
- Modify: `src/data/subscription_products.xml`（SH 區塊尾、SA 區塊前插入）
- Test: `src/tests/test_ha_paas_zero_plan_336.py`

**Interfaces:**
- Consumes: `storefront_hidden`（A1-1）
- Produces: xmlid `plan_ha_paas_monthly`／`product_ha_paas_monthly`（0 元；未掛 M2M＝orphan，service_type guard 放行）

**設計定案**：`service_type='cloud_service'`（HA PaaS instance＝CloudService record、未來掛 `cloud_app_template`，依 taxonomy §3.3 guard family 一致）；**`sh_trial=False`**（#336 拍板關 trial，偏離 §2.1 全域常數之處）；`sh_no_of_billing_cycle=-1`＋`sh_never_expire=True`＝無期限原生表達；每月照常跑 0 元。**不掛 M2M links**（HA template 等 Elmo image，plan 先為 orphan——本來就不該出現在任何 template 的 plans API）。

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_ha_paas_zero_plan_336.py
# -*- coding: utf-8 -*-
"""#336：HA PaaS 0 元 plan seed＋0 元計費路徑坐實。"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHaPaasZeroPlanSeed(TransactionCase):

    def test_seed_fields(self):
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.assertEqual(plan.sh_plan_price, 0)
        self.assertEqual(plan.service_type, 'cloud_service')
        self.assertTrue(plan.storefront_hidden)
        self.assertFalse(plan.sh_trial, 'HA PaaS 關 trial（#336 拍板）')
        self.assertTrue(plan.sh_never_expire)
        self.assertEqual(plan.sh_no_of_billing_cycle, -1)
        self.assertEqual(plan.sh_unit, 'month')

    def test_seed_product(self):
        product = self.env.ref('woow_paas_platform.product_ha_paas_monthly')
        self.assertEqual(product.list_price, 0)
        self.assertEqual(
            product.sh_subscription_plan_id,
            self.env.ref('woow_paas_platform.plan_ha_paas_monthly'))

    def test_orphan_no_template_links(self):
        """未掛任何 template M2M（HA template 等 Elmo image）。"""
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        linked = self.env['cloud.app.template'].sudo().search_count(
            [('subscription_plan_ids', 'in', plan.id)])
        self.assertEqual(linked, 0)

    def test_invisible_even_if_linked(self):
        """可見性契約閉環：未來掛上 HA template 後、storefront_hidden 仍擋前台。

        模擬：把 HA PaaS plan 掛上任一 template，斷言過濾語意仍排除它
        （縱深——orphan 與 hidden 兩道各自獨立成立）。
        """
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        tpl = self.env['cloud.app.template'].sudo().search([], limit=1)
        tpl.write({'subscription_plan_ids': [(4, plan.id)]})
        visible = tpl.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden)
        self.assertNotIn(plan, visible)
```

- [ ] **Step 2: 跑測試驗失敗**（FAIL：xmlid 不存在）
- [ ] **Step 3: 實作 seed**（格式鏡射 Task 0 校驗的 `plan_smarthome_monthly` 全段範本；欄位值如下——11 常數欄中 `sh_trial` 改 False、去 trial duration 兩欄意義）

```xml
<!-- ============ HA PaaS（#336：0 元、前台隱藏、無期限、關 trial） ============ -->
<record id="plan_ha_paas_monthly" model="sh.subscription.plan">
  <field name="name">HA PaaS Free</field>
  <field name="sh_plan_price">0</field>
  <field name="sh_duration">1</field>
  <field name="sh_unit">month</field>
  <field name="sh_override_product" eval="True"/>
  <field name="sh_no_of_billing_cycle">-1</field>
  <field name="sh_never_expire" eval="True"/>
  <field name="sh_trial" eval="False"/>
  <field name="sh_start_immediately" eval="False"/>
  <field name="sh_is_close_by_customer" eval="True"/>
  <field name="sh_reminder" eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
  <field name="service_type">cloud_service</field>
  <field name="storefront_hidden" eval="True"/>
</record>

<record id="product_ha_paas_monthly" model="product.product">
  <field name="name">HA PaaS Free</field>
  <field name="type">service</field>
  <field name="list_price">0</field>
  <field name="sale_ok" eval="True"/>
  <field name="purchase_ok" eval="False"/>
  <field name="sh_product_subscribe" eval="True"/>
  <field name="sh_subscription_plan_id" ref="plan_ha_paas_monthly"/>
</record>
```

（實際欄位名/格式若與範本 record 有出入，以 Task 0 校驗的範本為準對齊，斷言不變。）

- [ ] **Step 4: 跑測試驗通過**（3 條綠；並複跑 `TestServiceTypeSeedCoverage` 之類全量斷言——若該測試斷言「34 筆」總數會紅，**同 commit 更新該總數斷言並在 commit message 說明 +1 HA PaaS**）
- [ ] **Step 5: Commit**：`feat: HA PaaS 0 元 plan seed（隱藏＋關 trial＋無期限，Refs #336）`

### Task A3-2: 0 元計費路徑坐實測試

**Files:**
- Test: `src/tests/test_ha_paas_zero_plan_336.py`（append；純測試 task，無產品碼——坐實既有機制語意，#336 工作項 3）

**Interfaces:**
- Consumes: `plan_ha_paas_monthly`（A3-1）、`_has_paid_current_cycle`（`sh_subscription_fix.py:43`）

- [ ] **Step 1: 寫測試（預期一次綠——坐實而非驅動；若紅＝發現真 bug，回報 leader）**

```python
@tagged('post_install', '-at_install')
class TestZeroPricePaths(TransactionCase):
    """0 元訂閱：invoice 不產 deduct、payment gate 恆 False、退點恆 0。"""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': '免費仔'})
        self.plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.product = self.env.ref('woow_paas_platform.product_ha_paas_monthly')
        self.sub = self.env['sh.subscription.subscription'].create({
            'partner_id': self.partner.id,
            'product_id': self.product.id,
            'sh_subscription_plan_id': self.plan.id,
            # 其餘必填欄位依既有訂閱測試 fixture（test_change_plan_refund.py）慣例補齊
        })

    def test_has_paid_current_cycle_false(self):
        """0 元訂閱永無 done deduct → gate 恆 False（#324 邊界坐實）。"""
        self.assertFalse(self.sub._has_paid_current_cycle())

    def test_zero_invoice_no_wallet_deduct(self):
        self.sub._try_settle_invoice()
        deducts = self.env['wallet.transaction'].search([
            ('subscription_id', '=', self.sub.id), ('type', '=', 'deduct'),
        ])
        self.assertFalse(deducts, '0 元結算不得產生任何扣點交易')

    def test_refund_zero_on_delete(self):
        """退點恆 0：_compute_unused_refund 對 never-paid 訂閱回 0。"""
        self.assertEqual(self.sub._compute_unused_refund(), 0)
```

（method 名／fixture 欄位以 `sh_subscription_fix.py:43-114` 與既有 `test_change_plan_refund.py` 實際簽章為準對齊；**三條斷言語意不得改弱**。）

- [ ] **Step 2: 跑測試**（Expected: PASS＝坐實；任何 FAIL 停下回報）
- [ ] **Step 3: Commit**：`test: 坐實 HA PaaS 0 元計費路徑（不扣點／gate False／退點 0，Refs #336 #324）`

### Task A1-PR: 收尾與開 PR（PR-A1）

- [ ] **Step 1: 全 suite**：跑模組全測試（含既有），0 failed 才續（已知 2 個 agent-env ns flake 除外）。
- [ ] **Step 2: repo-lint**：`python3 scripts/audit/repo-lint.py` 綠。
- [ ] **Step 3: push worktree branch `feat/336-wave-a-visibility-limit-hapaas`**，開 PR（target `develop`）：標題 `feat: plan 前台隱藏旗標＋每帳號限一＋HA PaaS 0 元 plan＋封存 Hermes（Refs #336）`；描述含：四件事清單、雙軌說明（XML+migration .54）、out-of-scope（開通 API=#337 線／HA template=等 Elmo image／stg 驗證）、`Refs #336`。**不自行 merge**。
- [ ] **Step 4: 回報 leader**（PR 連結＋測試輸出原文）。

---

# Part 2：PR-A2「MCP Manage 上架骨架」（.55、Refs #338）

> 前提：PR-A1 已 merge（版號基準 .54）。本 PR 純新增 seed（`-u` 自建）、**無 migration**。
> Blocked-by 聲明：image `odoo-manage-mcp-admin` 未 build（woow-mcp-server#12）→ template seed `is_active=False` 隱藏上架，等 image ready 後 follow-up 翻開＋釘 digest。

### Task A4-1: Manage template seed（隱藏骨架）

**Files:**
- Modify: `src/data/cloud_app_templates.xml`（`cloud_app_mcp_odoo` @:252-292 之後插入新 record）
- Test: `src/tests/test_mcp_manage_seed_338.py`

**Interfaces:**
- Produces: xmlid `cloud_app_mcp_odoo_manage`（category='mcp'、is_active=False）

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_mcp_manage_seed_338.py
# -*- coding: utf-8 -*-
"""#338：Odoo Manage MCP 上架骨架 seed。"""
import json
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpManageTemplateSeed(TransactionCase):

    def test_template_seed(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertEqual(tpl.category, 'mcp')
        self.assertFalse(tpl.is_active, 'image 未 ready 前必須隱藏（#338）')
        self.assertFalse(tpl.mcp_enabled, 'MCP 內建於 body、非 sidecar（比照三 MCP admin）')
        values = json.loads(tpl.helm_default_values)
        self.assertIn('odoo-manage-mcp-admin', values['image']['repository'])

    def test_description_mentions_prereq(self):
        """租戶側前置說明（API Key／addon）放 description（#338 工作項 3）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertIn('API Key', tpl.description or '')
```

- [ ] **Step 2: 跑測試驗失敗**（FAIL：xmlid 不存在）
- [ ] **Step 3: 實作 seed**——完整鏡射 `cloud_app_mcp_odoo`（:252-292）record 結構（chart `oci://jcr-prod.woowtech.io/woow-paas-docker-local/mcp-server` 版本照抄現值、`service.port`、`envPrefix`、`persistence`、`updateStrategy: Recreate`、`resources` 全照抄），差異只有：

  - `id="cloud_app_mcp_odoo_manage"`、`slug`＝`mcp-odoo-manage`、name＝`Odoo Manage MCP`
  - `is_active` eval="False"
  - `image.repository` 尾段 `odoo-mcp-admin` → `odoo-manage-mcp-admin`；`image.tag`＝`latest`（佔位；上架前 follow-up 釘 digest——**在 XML 該行加註解標明**）
  - `config.nonSensitive.mcp_server` 的 command 先照抄 odoo 版樣式、把模組名換為 Manage 版預期入口（`mcp_server_odoo_manage.server:server`——**加 XML 註解：待 woow-mcp-server#12 image 定案校正**）
  - `description`：Manage 版定位（API Key 連接、全模組 CRUD、無個人權限、小團隊適用）＋租戶前置需求（目標 Odoo 需開 API Key、安裝對應 addon）——zh-tw 撰寫

- [ ] **Step 4: 跑測試驗通過**
- [ ] **Step 5: Commit**：`feat: Odoo Manage MCP template seed 骨架（is_active=False 待 image，Refs #338）`

### Task A4-2: Manage plans＋products＋M2M（兩版同價快照）

**Files:**
- Modify: `src/data/subscription_products.xml`（MCP 區塊尾插 2 plan＋2 product）
- Modify: `src/data/subscription_template_links.xml`（新 template ↔ 2 plans）
- Modify: `src/__manifest__.py:3`（`.54` → `18.0.1.0.55`）
- Test: `src/tests/test_mcp_manage_seed_338.py`（append）

**Interfaces:**
- Consumes: `cloud_app_mcp_odoo_manage`（A4-1）
- Produces: `plan_mcp_odoo_manage_monthly`(3000)／`plan_mcp_odoo_manage_yearly`(24000)＋products＋M2M

**價格聲明（PR 描述必寫）**：年繳 24000＝Elmo 07-12 定價 HTML 權威值（ID 1202/1203、×8）；既有 MCP odoo/n8n 年繳 30000（×10）**本 PR 不動**——全平台年繳倍率統一至 ×8 屬 Wave B base reprice。

- [ ] **Step 1: 寫失敗測試**（append）

```python
@tagged('post_install', '-at_install')
class TestMcpManagePlansSeed(TransactionCase):

    EXPECTED = {
        'plan_mcp_odoo_manage_monthly': (3000, 'month'),
        'plan_mcp_odoo_manage_yearly': (24000, 'year'),
    }

    def test_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price)
            self.assertEqual(plan.sh_unit, unit)
            self.assertEqual(plan.service_type, 'cloud_service')

    def test_products_and_m2m(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        plans = tpl.sudo().subscription_plan_ids
        self.assertEqual(len(plans), 2)
        for xmlid in self.EXPECTED:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertIn(plan, plans)
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.sh_subscription_plan_id, plan)
```

- [ ] **Step 2: 跑測試驗失敗**
- [ ] **Step 3: 實作**——plan 2 筆帶 §2.1 完整 11 常數欄（`sh_trial=True`＋7 天，照 MCP 既有檔）＋`service_type='cloud_service'`；product 2 筆（3000/24000）；`subscription_template_links.xml` 照 `:34-37`（mcp_odoo）樣式 append：

```xml
<record id="cloud_app_mcp_odoo_manage" model="cloud.app.template">
  <field name="subscription_plan_ids"
         eval="[(6, 0, [ref('plan_mcp_odoo_manage_monthly'), ref('plan_mcp_odoo_manage_yearly')])]"/>
</record>
```

＋manifest 版號 → `.55`

- [ ] **Step 4: 跑測試驗通過**（本檔全綠＋`test_pricing_seed_330.py` 無回歸；總數斷言若紅同 commit 更新並說明 +2）
- [ ] **Step 5: Commit**：`feat: Odoo Manage MCP 兩檔 plan/product seed＋M2M（3000/24000，Refs #338）`

### Task A4-PR: 收尾與開 PR（PR-A2）

- [ ] **Step 1: 全 suite 0 failed＋repo-lint 綠**
- [ ] **Step 2: push branch `feat/338-mcp-manage-skeleton`**，開 PR（target `develop`）：標題 `feat: Odoo Manage MCP 上架骨架（template+plan seed，is_active=False 待 image，Refs #338）`；描述含 blocked-by（woow-mcp-server#12）、翻開條件（image ready→釘 digest→is_active=True→stg healthz+GUI 驗證）、24000 vs 30000 併存聲明。**不自行 merge**。
- [ ] **Step 3: 回報 leader**。

---

# Part 3：PR-A3「CS tier→部署規格映射」（.56、Closes #332）

> 前提：PR-A1、PR-A2 已 merge（版號基準 .55）。
> **核心設計＝runtime overlay（reviewer 請重點驗）**：部署時把選定 plan 的 `cs_*` 疊進 helm values（resources／persistence.size／workers 字串替換），template seed 的 `helm_default_values` **完全不動**＝不撞 sticky noupdate、不需 template heal/migration（matrix E1 原案的替代解）。template 值退居 fallback（cs_* 全 0 的 plan＝行為與現狀完全相同）。
> Out-of-scope（PR 描述必寫）：change-plan 後的規格 re-apply（是否觸發 redeploy 未查證）→ 開 follow-up issue；Hermes 規格分階（Gate A/B）→ 已隨 A5 封存消滅，flat 規格留原樣。

### Task A6-1: `cs_*` 規格欄位

**Files:**
- Create: `src/models/sh_subscription_plan_cs_spec.py`
- Modify: `src/models/__init__.py`
- Test: `src/tests/test_cs_spec_332.py`

**Interfaces:**
- Produces: `cs_vcpu`(Integer)／`cs_ram_gb`(Float)／`cs_storage_gb`(Integer)／`cs_odoo_workers`(Integer)——全 default 0＝「未定規格、fallback template.min_*」

- [ ] **Step 1: 寫失敗測試**

```python
# src/tests/test_cs_spec_332.py
# -*- coding: utf-8 -*-
"""#332：CS tier→部署規格映射（plan cs_* 欄位＋runtime overlay）。"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCsSpecFields(TransactionCase):

    def test_fields_exist_default_zero(self):
        plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        for f in ('cs_vcpu', 'cs_ram_gb', 'cs_storage_gb', 'cs_odoo_workers'):
            self.assertIn(f, plan._fields)
```

- [ ] **Step 2: 跑測試驗失敗**
- [ ] **Step 3: 實作**

```python
# src/models/sh_subscription_plan_cs_spec.py
# -*- coding: utf-8 -*-
"""#332：CS 部署規格欄位（type-specific，taxonomy §3.2；OQ-H Option B）。

全欄 default 0＝「本 plan 未定義規格」→ 部署碼 fallback template.min_*，
既有 plan 行為零變。
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ShSubscriptionPlanCsSpec(models.Model):
    _inherit = 'sh.subscription.plan'

    cs_vcpu = fields.Integer(
        string='CS vCPU', default=0,
        help='部署主容器 CPU（顆）；0＝沿用 template 預設。')
    cs_ram_gb = fields.Float(
        string='CS RAM (GB)', default=0,
        help='部署主容器記憶體（GB）；0＝沿用 template 預設。')
    cs_storage_gb = fields.Integer(
        string='CS Storage (GB)', default=0,
        help='PVC 容量（GB）；0＝沿用 template 預設。')
    cs_odoo_workers = fields.Integer(
        string='Odoo workers', default=0,
        help='Odoo 專屬：worker process 數（tier-aware）；'
             '0＝沿用 template init script 預設（workers=0 threaded）。'
             'n8n 等單 process 服務一律留 0。')

    @api.constrains('cs_odoo_workers', 'cs_ram_gb')
    def _check_workers_require_ram(self):
        """workers>0 而 ram 未定＝worker 記憶體上限算不出來（OOM 風險）——禁。"""
        for plan in self:
            if plan.cs_odoo_workers > 0 and not plan.cs_ram_gb:
                raise ValidationError(_(
                    'cs_odoo_workers 大於 0 時必須同時設定 cs_ram_gb，'
                    '否則無法計算 per-worker 記憶體上限（OOM 風險）。'))
```

Step 1 測試同步加一條（append 至 `TestCsSpecFields`）：

```python
    def test_workers_require_ram_constraint(self):
        """M4 防呆：workers>0 且 ram=0 的組合被 constraint 擋。"""
        from odoo.exceptions import ValidationError
        plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')
        with self.assertRaises(ValidationError):
            plan.write({'cs_odoo_workers': 2})
```

＋`__init__.py` import

- [ ] **Step 4: 跑測試驗通過** → **Step 5: Commit**：`feat: sh.subscription.plan 新增 cs_* 部署規格欄位（Refs #332）`

### Task A6-2: CS 12 筆 seed（雙軌）

**Files:**
- Modify: `src/data/subscription_products.xml`（Odoo 6＋n8n 6 筆 plan 各加 cs_* 四欄）
- Create: `src/migrations/18.0.1.0.56/post-migrate.py`
- Modify: `src/models/sh_subscription_plan_cs_spec.py`（delegate method）
- Modify: `src/__manifest__.py:3`（`.55` → `18.0.1.0.56`）
- Test: `src/tests/test_cs_spec_332.py`（append）

**Interfaces:**
- Produces: `_woow_migrate_cs_spec_332()`；規格表（matrix §3.1/3.2 拍板值）：

| plan xmlid（月/年同值） | vcpu | ram | storage | workers |
|---|---|---|---|---|
| `plan_odoo_monthly`／`_yearly`（T1） | 2 | 2 | 20 | 2 |
| `plan_odoo_t2_monthly`／`_yearly` | 2 | 4 | 30 | 3 |
| `plan_odoo_t3_monthly`／`_yearly` | 3 | 4 | 40 | 4 |
| `plan_n8n_monthly`／`_yearly`（T1） | 1 | 1 | 10 | 0 |
| `plan_n8n_t2_monthly`／`_yearly` | 1 | 2 | 20 | 0 |
| `plan_n8n_t3_monthly`／`_yearly` | 2 | 2 | 30 | 0 |

⚠️ T1 的 vcpu/ram/storage 必須＝Task 0 校驗的 **`T1_BASELINE`**（template 現值）——不相等即停下回報（表值來自 matrix draft，以「T1 不 regress 現狀」為最高原則）。T1 workers=2 是**刻意的行為修正**（現狀 0 為 threaded 鎖死，#332 工作項 4）。**此 gate 不只靠 prose——Step 1 的 `test_t1_equals_template_baseline` 是活契約**：T1 斷言直接對 `template.min_*` 比對（非對硬編碼常數），任何不一致＝測試紅＝機械擋下。

- [ ] **Step 1: 寫失敗測試**（append）

```python
CS_SPEC_EXPECTED = {
    'plan_odoo_monthly': (2, 2.0, 20, 2), 'plan_odoo_yearly': (2, 2.0, 20, 2),
    'plan_odoo_t2_monthly': (2, 4.0, 30, 3), 'plan_odoo_t2_yearly': (2, 4.0, 30, 3),
    'plan_odoo_t3_monthly': (3, 4.0, 40, 4), 'plan_odoo_t3_yearly': (3, 4.0, 40, 4),
    'plan_n8n_monthly': (1, 1.0, 10, 0), 'plan_n8n_yearly': (1, 1.0, 10, 0),
    'plan_n8n_t2_monthly': (1, 2.0, 20, 0), 'plan_n8n_t2_yearly': (1, 2.0, 20, 0),
    'plan_n8n_t3_monthly': (2, 2.0, 30, 0), 'plan_n8n_t3_yearly': (2, 2.0, 30, 0),
}


@tagged('post_install', '-at_install')
class TestCsSpecSeed(TransactionCase):

    def test_cs_12_plans_spec(self):
        for xmlid, (vcpu, ram, storage, workers) in CS_SPEC_EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb,
                 plan.cs_odoo_workers),
                (vcpu, ram, storage, workers), xmlid)

    def test_migration_idempotent(self):
        Plan = self.env['sh.subscription.plan']
        Plan._woow_migrate_cs_spec_332()
        Plan._woow_migrate_cs_spec_332()
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        self.assertEqual(plan.cs_vcpu, 3)

    def test_non_cs_plans_untouched(self):
        for xmlid in ('plan_smarthome_monthly',
                      'plan_security_access_entry_monthly'):
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb), (0, 0.0, 0))

    def test_t1_equals_template_baseline(self):
        """T1 不 regress 的活契約：對 template.min_* 直接比對（非硬編碼常數）。

        紅＝matrix draft 與 template 現值不一致 → 停下回報 leader，
        嚴禁改斷言遷就（機械 gate，取代 prose 版「不相等即停下」）。
        """
        for tpl_xmlid, plan_xmlid in (
            ('cloud_app_odoo', 'plan_odoo_monthly'),
            ('cloud_app_n8n', 'plan_n8n_monthly'),
        ):
            tpl = self.env.ref(f'woow_paas_platform.{tpl_xmlid}')
            t1 = self.env.ref(f'woow_paas_platform.{plan_xmlid}')
            self.assertEqual(t1.cs_vcpu, tpl.min_vcpu, tpl_xmlid)
            self.assertEqual(t1.cs_ram_gb, tpl.min_ram_gb, tpl_xmlid)
            self.assertEqual(t1.cs_storage_gb, tpl.min_storage_gb, tpl_xmlid)

    def test_xml_and_migration_tracks_identical(self):
        """雙軌防漂移：fresh XML 值經 migration delegate 重跑後必須不變。

        XML（fresh 軌）與 _CS_SPEC_332 dict（升級軌）是兩份手寫來源，
        本測試在 fresh DB 上跑 migration、斷言 12 筆值零變＝兩份一致。
        """
        Plan = self.env['sh.subscription.plan']
        before = {
            xmlid: (p.cs_vcpu, p.cs_ram_gb, p.cs_storage_gb, p.cs_odoo_workers)
            for xmlid in CS_SPEC_EXPECTED
            for p in [self.env.ref(f'woow_paas_platform.{xmlid}')]
        }
        Plan._woow_migrate_cs_spec_332()
        for xmlid, snap in before.items():
            p = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (p.cs_vcpu, p.cs_ram_gb, p.cs_storage_gb, p.cs_odoo_workers),
                snap, f'{xmlid}：XML 軌與 migration 軌漂移')
```

- [ ] **Step 2: 驗失敗** → **Step 3: 實作**——XML 12 筆各加四欄（fresh 軌）；delegate（升級軌）：

```python
    _CS_SPEC_332 = {
        'plan_odoo_monthly': (2, 2.0, 20, 2), 'plan_odoo_yearly': (2, 2.0, 20, 2),
        'plan_odoo_t2_monthly': (2, 4.0, 30, 3), 'plan_odoo_t2_yearly': (2, 4.0, 30, 3),
        'plan_odoo_t3_monthly': (3, 4.0, 40, 4), 'plan_odoo_t3_yearly': (3, 4.0, 40, 4),
        'plan_n8n_monthly': (1, 1.0, 10, 0), 'plan_n8n_yearly': (1, 1.0, 10, 0),
        'plan_n8n_t2_monthly': (1, 2.0, 20, 0), 'plan_n8n_t2_yearly': (1, 2.0, 20, 0),
        'plan_n8n_t3_monthly': (2, 2.0, 30, 0), 'plan_n8n_t3_yearly': (2, 2.0, 30, 0),
    }

    def _woow_migrate_cs_spec_332(self):
        """#332 升級軌：CS 12 筆 backfill cs_*（xmlid 定位、冪等）。"""
        for xmlid, (vcpu, ram, storage, workers) in self._CS_SPEC_332.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.write({'cs_vcpu': vcpu, 'cs_ram_gb': ram,
                            'cs_storage_gb': storage,
                            'cs_odoo_workers': workers})
```

migration `src/migrations/18.0.1.0.56/post-migrate.py`（同 .54 樣式 delegate）＋manifest → `.56`

- [ ] **Step 4: 驗通過** → **Step 5: Commit**：`feat: CS 12 筆 plan 規格 seed 雙軌（XML+migration .56，Refs #332）`

### Task A6-3: `allocated_*` 改讀 plan（fallback template）

**Files:**
- Modify: `src/controllers/paas.py:2171-2173`
- Test: `src/tests/test_cs_spec_332.py`（append）

**Interfaces:**
- Consumes: `_create_service` 簽章內既有 `plan`（`plan_id` 參數解出的 record，:2086 已驗屬 template）
- Produces: allocated_* 語意＝`plan.cs_* or template.min_*`

- [ ] **Step 1: 寫失敗測試**（append；fixture 沿用 A2-2 的 throwaway 模式——直接以 vals 模擬 `_create_service` 的 allocated 決策函式）

實作前先把決策抽成純函式（可測性）：

```python
@tagged('post_install', '-at_install')
class TestAllocatedFromPlan(TransactionCase):

    def test_allocated_prefers_plan_spec(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        vals = Svc._allocated_vals_from(tpl, plan)
        self.assertEqual(vals, {
            'allocated_vcpu': 3, 'allocated_ram_gb': 4.0,
            'allocated_storage_gb': 40,
        })

    def test_allocated_fallback_template(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        zero_plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')  # cs_* 全 0
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        vals = Svc._allocated_vals_from(tpl, zero_plan)
        self.assertEqual(vals, {
            'allocated_vcpu': tpl.min_vcpu,
            'allocated_ram_gb': tpl.min_ram_gb,
            'allocated_storage_gb': tpl.min_storage_gb,
        })
```

- [ ] **Step 2: 驗失敗**（method 不存在）
- [ ] **Step 3: 實作**——`cloud_service.py` 加純函式：

```python
    @api.model
    def _allocated_vals_from(self, template, plan):
        """#332：規格顯示值——plan cs_* 優先、0 值 fallback template.min_*。"""
        return {
            'allocated_vcpu': plan.cs_vcpu or template.min_vcpu,
            'allocated_ram_gb': plan.cs_ram_gb or template.min_ram_gb,
            'allocated_storage_gb': plan.cs_storage_gb or template.min_storage_gb,
        }
```

`paas.py:2171-2173` 的三行 `template.min_*` 直抄改為 `**CloudService._allocated_vals_from(template, plan)`（merge 進 service_vals；`plan` 即 `_create_service` 內既有已驗 record）。

- [ ] **Step 4: 驗通過** → **Step 5: Commit**：`feat: allocated_* 改讀選定 plan 規格、template 退 fallback（Refs #332）`

### Task A6-4: helm runtime overlay（resources／persistence／workers＋memory limits）

**Files:**
- Modify: `src/models/cloud_service.py`（新純函式＋接進 **`HELM_ASSEMBLY_SITE`**（Task 0 校驗落點））
- Test: `src/tests/test_cs_spec_332.py`（append）

**Interfaces:**
- Consumes: `cs_*`（A6-1/2）、Task 0 的 `HELM_ASSEMBLY_SITE`
- Produces: `CloudService._apply_plan_spec_overlay(values: dict, template, plan) -> dict`（純函式、不 mutate 輸入）

**Overlay 規則（全部條件式——cs_* 為 0 的維度不碰，行為與現狀 bit-for-bit 相同）**：
1. `cs_vcpu>0` → `values['resources']['requests']['cpu']=f'{cs_vcpu}'`＋`['limits']['cpu']` 同值
2. `cs_ram_gb>0` → requests/limits `memory=f'{cs_ram_gb}Gi'`（整數化：`int(cs_ram_gb)` 若為整數值，否則 `f'{int(cs_ram_gb*1024)}Mi'`）
3. `cs_storage_gb>0` → `values['persistence']['size']=f'{cs_storage_gb}Gi'`
4. `cs_odoo_workers>0` **且** values 帶 `customPostInitScripts`（即 Odoo template）→ 對 `customPostInitScripts` 內每支 script 字串做正則替換：
   - `workers = 0` → `workers = {N}`（pattern：`^workers = \d+$`，multiline）
   - `limit_memory_hard = 0` → `limit_memory_hard = {int(ram_bytes*0.85/N)}`；`limit_memory_soft = 0` → `limit_memory_soft = {int(ram_bytes*0.70/N)}`（`ram_bytes = int(cs_ram_gb*1024**3)`；Q8：workers>0 後不設限會 OOM，係數採 Odoo 官方部署建議比例，per-worker）
   - ram 未設（cs_ram_gb=0）而 workers>0：**此組合已被 A6-1 的 `_check_workers_require_ram` constraint 在資料層禁止**——overlay 此分支僅為防禦性保留（memory limits 不動、僅換 workers），正常永不可達

- [ ] **Step 1: 寫失敗測試**（append）

```python
import copy
import json


@tagged('post_install', '-at_install')
class TestPlanSpecOverlay(TransactionCase):

    def _base_values(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        return json.loads(tpl.helm_default_values)

    def test_overlay_resources_and_persistence(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        base = self._base_values()
        out = Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, plan)
        self.assertEqual(out['resources']['requests']['cpu'], '3')
        self.assertEqual(out['resources']['requests']['memory'], '4Gi')
        self.assertEqual(out['persistence']['size'], '40Gi')

    def test_overlay_workers_substitution(self):
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        plan = self.env.ref('woow_paas_platform.plan_odoo_t3_monthly')
        out = Svc._apply_plan_spec_overlay(self._base_values(), tpl, plan)
        scripts = json.dumps(out.get('customPostInitScripts', {}))
        self.assertIn('workers = 4', scripts)
        self.assertNotIn('workers = 0', scripts)
        self.assertNotIn('limit_memory_hard = 0', scripts)

    def test_zero_spec_plan_is_noop(self):
        """cs_* 全 0 → overlay 輸出必須與輸入完全相同（現狀零變保證）。"""
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        zero_plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')
        base = self._base_values()
        out = Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, zero_plan)
        self.assertEqual(out, base)

    def test_n8n_no_workers_touch(self):
        """n8n plan（workers=0）：不碰 script；resources 照 overlay。"""
        Svc = self.env['woow_paas_platform.cloud_service']
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        plan = self.env.ref('woow_paas_platform.plan_n8n_t3_monthly')
        base = json.loads(tpl.helm_default_values)
        out = Svc._apply_plan_spec_overlay(copy.deepcopy(base), tpl, plan)
        self.assertEqual(out['resources']['requests']['cpu'], '2')
        self.assertEqual(
            out.get('customPostInitScripts'), base.get('customPostInitScripts'))
```

- [ ] **Step 2: 驗失敗** → **Step 3: 實作**

```python
    @api.model
    def _apply_plan_spec_overlay(self, values, template, plan):
        """#332 runtime overlay：把 plan cs_* 疊進 helm values。

        刻意不改 template seed（避開 sticky noupdate）；cs_* 為 0 的維度
        一律不碰＝與現狀 bit-for-bit 相同。回傳同一 dict（就地疊加）。
        """
        # （`import re` 放 cloud_service.py 檔頭 import 區，非函式內）
        if plan.cs_vcpu:
            res = values.setdefault('resources', {})
            for key in ('requests', 'limits'):
                res.setdefault(key, {})['cpu'] = str(plan.cs_vcpu)
        if plan.cs_ram_gb:
            mem = (f'{int(plan.cs_ram_gb)}Gi'
                   if float(plan.cs_ram_gb).is_integer()
                   else f'{int(plan.cs_ram_gb * 1024)}Mi')
            res = values.setdefault('resources', {})
            for key in ('requests', 'limits'):
                res.setdefault(key, {})['memory'] = mem
        if plan.cs_storage_gb:
            values.setdefault('persistence', {})['size'] = f'{plan.cs_storage_gb}Gi'
        scripts = values.get('customPostInitScripts')
        if plan.cs_odoo_workers and scripts:
            n = plan.cs_odoo_workers
            for name, body in list(scripts.items()):
                body = re.sub(r'^workers = \d+$', f'workers = {n}',
                              body, flags=re.M)
                if plan.cs_ram_gb:
                    ram_bytes = int(plan.cs_ram_gb * 1024 ** 3)
                    body = re.sub(r'^limit_memory_hard = \d+$',
                                  f'limit_memory_hard = {int(ram_bytes * 0.85 / n)}',
                                  body, flags=re.M)
                    body = re.sub(r'^limit_memory_soft = \d+$',
                                  f'limit_memory_soft = {int(ram_bytes * 0.70 / n)}',
                                  body, flags=re.M)
                scripts[name] = body
        return values
```

接線（**具體錨點，Task 0 只校驗、不重新發明**）：注入點＝`_create_service`（`paas.py:2060-2198` 區間）內「以 `template.helm_default_values` 為 base 組出最終 helm values」的那一步**完成之後、該 values 被寫進 service record／送 operator 之前**，插入一行：

```python
        values = self.env['woow_paas_platform.cloud_service']._apply_plan_spec_overlay(
            values, template, plan)
```

（`plan` 即 `_create_service` 簽章內既有已驗 record，A6-3 已用。）若 Task 0 校驗發現 values 組裝實際發生在 `cloud_service.py` 的某方法（而非 controller），注入點改為該方法內 values 定案的最後一行之後——兩種情況擇一，**不得兩處都插**（重複 overlay 雖冪等仍屬錯誤結構）。接線後 append 守衛測試：

```python
    def test_overlay_wired_into_create_path(self):
        """接線守衛：overlay 必須在部署組裝路徑上被呼叫（定義 1 次＋呼叫 ≥1 次）。"""
        import inspect
        from odoo.addons.woow_paas_platform.controllers import paas
        from odoo.addons.woow_paas_platform.models import cloud_service
        combined = (inspect.getsource(paas) + inspect.getsource(cloud_service))
        self.assertGreaterEqual(
            combined.count('_apply_plan_spec_overlay'), 2,
            'overlay 已定義但未接進 _create_service 部署組裝路徑')
```

- [ ] **Step 4: 驗通過（5 條）＋全 suite 無回歸** → **Step 5: Commit**：`feat: helm runtime overlay——plan 規格疊進 resources/persistence/workers（Refs #332）`

### Task A6-PR: 收尾與開 PR（PR-A3）

- [ ] **Step 1: 全 suite 0 failed＋repo-lint 綠**
- [ ] **Step 2: push branch `feat/332-cs-spec-mapping`**，開 PR（target `develop`）：標題 `feat: CS tier→部署規格映射（plan cs_*＋runtime overlay，Closes #332）`；描述含：runtime overlay vs template heal 的設計取捨、T1 workers 0→2 行為修正聲明、out-of-scope（change-plan re-apply→follow-up issue、Hermes Gate A/B 隨封存消滅）、`Closes #332`。**不自行 merge**。
- [ ] **Step 3: 開 follow-up issue**：`feat: change plan 後 CS 規格 re-apply（#332 殘段）`——內容：change plan 只換訂閱不觸發 redeploy 的現狀查證＋re-apply 機制提案；`Refs #332`。
- [ ] **Step 4: 回報 leader**（PR 連結＋follow-up issue 連結＋測試輸出原文）。

---

## Task 0 校驗結果（執行 agent 填寫）

| # | 項目 | 結果 |
|---|---|---|
| 1 | TERMINAL_STATES | （待填） |
| 2 | HELM_ASSEMBLY_SITE | （待填） |
| 3 | T1_BASELINE（odoo／n8n） | （待填） |
| 4 | plan record 格式範本確認 | （待填） |
| 5 | tests/__init__ 慣例 | （待填） |
| 6 | 既有 suite 綠基線 | （待填） |
| 7 | plan_id dispatch 行 | （待填） |
| 8 | Hermes 行號複驗 | （待填） |

<!-- peer-reviewed: 2026-07-12T09:35:30Z rounds=2 verdict=approved by=miniclaude -->
