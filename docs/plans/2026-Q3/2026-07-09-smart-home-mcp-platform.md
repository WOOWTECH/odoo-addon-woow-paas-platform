---
title: Smart Home MCP — 平台側 Implementation Plan
category: plans
status: done
created: 2026-07-09
updated: 2026-07-19
source: docs/superpowers/plans/2026-07-09-smart-home-mcp-platform.md
promoted-at: 2026-07-19
---

# Smart Home MCP — 平台側 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `sh.subscription.plan` 帶一個 flat 的 `mcp_integration` 字串旗標，並把它經由 `smart_home.to_dict()` 與 `ha_api.get_tunnel_token()` 兩個既有同步入口廣播給 HA component（單向、平台不落資料面）。

**Architecture:** 純加性（add-only）Odoo model 擴充 + 一個推導 getter。plan 上新增一個 `Char` 欄位（比照 `sh_subscription_plan_sa_quota.py`），`smart_home` 新增 `_get_mcp_integration()` 沿用既有 `_get_subscription_info` 的 `self.sudo().subscription_id` 推導鏈，兩個回傳點各多掛一個 `mcp` key。無新 model、無新 route、無新 ACL、無 migration、不碰 operator/tunnel。

**Tech Stack:** Odoo 18（Python 3.11、PEP8 4-space）、`odoo.tests.common.TransactionCase`（model 層）、`HttpCase`（endpoint 層，`@tagged('post_install', '-at_install')`）。

**Spec:** `docs/plans/2026-Q3/2026-07-09-smart-home-mcp-design.md`（§4 平台側、§7 驗收 1-4、§4.3 不做的事）。base = local develop `cf99776`。

## Global Constraints

以下每一條都隱含在每個 task 的要求裡：

- **只做 §4 平台側**；`§5` HA sensor component 屬另一 repo（`woow_paas_smart_home`）＋另一 agent，本計畫完全不碰。
- **plan schema ① flat**：`_inherit = 'sh.subscription.plan'` classical extension、**只加欄位、不覆寫任何 std 欄位型別**（比照 `sh_subscription_plan_sa_quota.py`）。不引 `service_type` discriminator（§6 延後）。
- **欄位型別 `Char` 非 `Selection`**：對齊「component 端做 `mcp === "ha_mcp_tools"` 字串比對、未來換後端只改字串」。
- **§4.3 平台不做**：不 provision MCP、不產/保管 token、不開新 port/subdomain/route、不改 tunnel ingress、不回讀 sensor、不做硬 enforcement。本計畫任何一行都不得呼叫 operator client / 建 `@route` / 動 tunnel。
- **不動定價 seed 金額**：測試一律用 fixture plan（在測試碼內 `create` 帶 `mcp_integration='ha_mcp_tools'`），**絕不改** `src/data/` 下任何 seed 金額或建立正式 400 plan record（那屬定價軌、另一 agent/波次）。
- **推導 sudo**：`_get_mcp_integration()` 必須用 `self.sudo().subscription_id`（見下方「與 spec 的差異」）。
- **TDD**：每個 task 先寫失敗測試 → 跑到紅 → 最小實作 → 跑到綠 → commit。commit message 用 zh-tw `{type}: {message}`。
- **推導路徑**：`home.subscription_id.sh_subscription_plan_id.mcp_integration`（沿用 SA `security_access.py:983` 既有 pattern）。

### ⚠️ 與 spec 的一處差異（已向 leader 報告、有據非臆測）

spec §4.2 的 `_get_mcp_integration` 草稿寫 `self.subscription_id...`（未 sudo）。但同一個 `to_dict()` 內的既有 sibling `subscription_lifecycle_mixin._get_subscription_info`（`src/models/subscription_lifecycle_mixin.py:493`）用的是 **`sub = self.sudo().subscription_id`**——因為 `subscription_id` 受 `ir.model.access` 限制，非 owner／非 sudo 的 `to_dict()` 呼叫者（portal user）會 `AccessError`（見 `test_smart_home_subscription_flow.py:498` 註記）。故本計畫的 getter **改用 `self.sudo().subscription_id`**，與既有 sibling 一致。這是對 spec 草稿的修正，非擴大範圍。

---

## File Structure

| 檔案 | 動作 | 責任 |
| --- | --- | --- |
| `src/models/sh_subscription_plan_sh_mcp.py` | **新增** | `sh.subscription.plan` 的 `mcp_integration` Char 欄位（add-only，比照 sa_quota） |
| `src/models/__init__.py` | 改（+1 import） | 載入新 model 檔（Odoo 靠 `__init__` 載入 model，不進 manifest data） |
| `src/models/smart_home.py` | 改（+getter、+1 dict key） | `_get_mcp_integration()` 推導 + `to_dict()` 多回 `mcp` |
| `src/controllers/ha_api.py` | 改（+1 dict key） | `get_tunnel_token()` response 多回 `mcp`（§4.2 第二入口） |
| `src/tests/test_smart_home_mcp.py` | **新增** | §7 準則 1-4 的 TDD（model 層 TransactionCase + endpoint 層 HttpCase） |
| `src/tests/__init__.py` | 改（+1 import） | 登記新測試模組（Odoo 測試靠 `__init__` 逐一 import） |
| `src/__manifest__.py` | 改（版號 .49→.50） | feature 版號 bump（repo 慣例，每 feature bump） |

**不需要的東西（明列以防蛇足）：**
- ❌ `ir.model.access.csv`：classical `_inherit` 擴充既有 model，不建新 model → 無新 ACL（sa_quota 也沒加）。
- ❌ migration：新 `Char` 欄位由 Odoo ORM 在 `-u` 時自動建欄；既有 plan 列該欄為 NULL，getter `(plan.mcp_integration or '')` 已 coalesce 成 `''`（準則 3 涵蓋），故無需手寫 migration。
- ❌ `__manifest__.py` data 條目：純 model 檔靠 `__init__` 載入。
- ❌ view / OWL：本波不做 UI（§6：若日後要下拉再改 Selection，屬非破壞性演進）。

---

## Task 1: `sh.subscription.plan` 新增 `mcp_integration` 欄位

**Files:**
- Create: `src/models/sh_subscription_plan_sh_mcp.py`
- Modify: `src/models/__init__.py`（在 `from . import sh_subscription_plan_sa_quota` 之後加一行）
- Test: `src/tests/test_smart_home_mcp.py`（新檔，`TestSmartHomeMcpPlanField`）
- Modify: `src/tests/__init__.py`（登記 `test_smart_home_mcp`）

**Interfaces:**
- Produces: `sh.subscription.plan.mcp_integration`（`fields.Char`, default `''`）——Task 2/3 的推導鏈末端讀它。

- [ ] **Step 1: 寫失敗測試（§7 準則 1）**

新建 `src/tests/test_smart_home_mcp.py`：

```python
"""Smart Home MCP（#270 ha_mcp_tools 方案）平台側測試。

§7 平台驗收準則 1-4：
  1. plan 有 mcp_integration 欄位、default ''、不覆寫 std 欄位（本檔 TestSmartHomeMcpPlanField）
  2. 綁 ha_mcp_tools plan 的 home → payload mcp='ha_mcp_tools'（to_dict + endpoint）
  3. 無訂閱 / plan mcp='' → payload mcp=''
  4. 既有 remote-access 行為不變、無新增 tunnel/route/token/port
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase, HttpCase, tagged


class TestSmartHomeMcpPlanField(TransactionCase):
    """§7 準則 1：sh.subscription.plan 新增 add-only 的 mcp_integration Char
    欄位、default ''（比照 sh_subscription_plan_sa_quota）。"""

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']

    def test_mcp_integration_defaults_empty(self):
        plan = self.Plan.create({
            'name': 'MCP Field Test Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.assertEqual(plan.mcp_integration, '')

    def test_mcp_integration_is_char_and_round_trips(self):
        field = self.Plan._fields['mcp_integration']
        self.assertEqual(field.type, 'char')
        plan = self.Plan.create({
            'name': 'MCP Field Test Plan 2',
            'sh_plan_price': 400.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'mcp_integration': 'ha_mcp_tools',
        })
        self.assertEqual(plan.mcp_integration, 'ha_mcp_tools')
```

在 `src/tests/__init__.py` 末尾登記（順序不拘，比照既有逐行 import）：

```python
from . import test_smart_home_mcp
```

- [ ] **Step 2: 跑測試確認失敗**

Run（sandbox 內，`/woow-dev:k8s-dev-sandbox test` 或 docker-desktop paas-odoo image 掛 worktree src；`-u` 讓新欄位建欄）：

```
odoo -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSmartHomeMcpPlanField --stop-after-init
```

Expected: FAIL —— `mcp_integration` 尚不存在，`create({... 'mcp_integration': ...})` 拋 `ValueError: Invalid field 'mcp_integration' on model 'sh.subscription.plan'`，且 `_fields['mcp_integration']` KeyError。

- [ ] **Step 3: 寫最小實作（新 model 檔）**

新建 `src/models/sh_subscription_plan_sh_mcp.py`：

```python
from odoo import fields, models


class ShSubscriptionPlanShMcp(models.Model):
    """Smart Home MCP capability on subscription plans.

    Follows the odoo-std-field-no-override pattern (add-only), mirroring
    sh_subscription_plan_sa_quota. The value NAMES which HA MCP integration
    the plan grants, so the HA component can branch on it. Empty = no MCP.
    """

    _inherit = 'sh.subscription.plan'

    mcp_integration = fields.Char(
        string='SH MCP Integration',
        default='',
        help="Smart Home: which HA MCP integration this plan grants "
             "(e.g. 'ha_mcp_tools'). Empty = no MCP. Read by the HA "
             "woow_paas_smart_home component to decide whether to expose "
             "the MCP sensor entity.",
    )
```

- [ ] **Step 4: 登記 model import**

在 `src/models/__init__.py` 的 `from . import sh_subscription_plan_sa_quota`（第 7 行）之後加一行：

```python
from . import sh_subscription_plan_sh_mcp
```

- [ ] **Step 5: 跑測試確認通過**

Run: 同 Step 2。
Expected: PASS（2 tests）。

> ⚠️ **pattern-guard 提醒**：編輯 `src/models/` 下的 model 檔可能觸發 pattern-guard hook（列出必讀 docs）。照做——讀出的 doc、重試即放行。若 hook 指向的前提與本計畫衝突，**停下問 leader，不臆測**。

- [ ] **Step 6: Commit**

```bash
git add src/models/sh_subscription_plan_sh_mcp.py src/models/__init__.py \
        src/tests/test_smart_home_mcp.py src/tests/__init__.py
git commit -m "feat: #270 sh.subscription.plan 新增 mcp_integration 旗標欄位（flat, add-only）"
```

---

## Task 2: `smart_home._get_mcp_integration()` + `to_dict()` 多回 `mcp`

**Files:**
- Modify: `src/models/smart_home.py`（在 `to_dict()`（line 320）之前加 `_get_mcp_integration()`；在 `to_dict()` 回傳 dict（line 323-341）加 `mcp` key）
- Test: `src/tests/test_smart_home_mcp.py`（加 `TestSmartHomeToDictMcp`）

**Interfaces:**
- Consumes: `sh.subscription.plan.mcp_integration`（Task 1）。
- Produces: `smart_home._get_mcp_integration() -> str`；`smart_home.to_dict()` 回傳 dict 多一個 `'mcp'` key（str，`''` 或 integration 名）——Task 3 endpoint 與 HA component 消費。

- [ ] **Step 1: 寫失敗測試（§7 準則 2 model 層 + 準則 3）**

在 `src/tests/test_smart_home_mcp.py` 加：

```python
class TestSmartHomeToDictMcp(TransactionCase):
    """§7 準則 2（model 層）＋準則 3：to_dict() 依 plan 推導 mcp 字串。"""

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']
        self.SmartHome = self.env['woow_paas_platform.smart_home']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.user = self.env.ref('base.user_admin')
        self.workspace = self.Workspace.create({
            'name': 'MCP ToDict WS',
            'owner_id': self.user.id,
        })

        # 帶 MCP 旗標的 plan（400 檔）＋不帶旗標的 plan（150 檔）
        self.plan_mcp = self.Plan.create({
            'name': 'SH MCP Plan',
            'sh_plan_price': 400.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'mcp_integration': 'ha_mcp_tools',
        })
        self.plan_plain = self.Plan.create({
            'name': 'SH Plain Plan',
            'sh_plan_price': 150.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        # _create_and_confirm_subscription 靠 product.template.sh_subscription_plan_id
        # 解訂閱商品；兩個 plan 各給一個 product。
        for plan, price in ((self.plan_mcp, 400.0), (self.plan_plain, 150.0)):
            self.env['product.template'].sudo().create({
                'name': f'{plan.name} Product',
                'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': price,
            })
        # journal 前置（_create_and_confirm_subscription 需 company.sh_journal_id；
        # 鏡像 test_security_access_subscription_flow.py setUp）
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id

    def _home_bound_to(self, plan):
        home = self.SmartHome.create({
            'name': f'HA for {plan.name}',
            'workspace_id': self.workspace.id,
        })
        home._create_and_confirm_subscription(self.user.partner_id, plan)
        return home

    def test_to_dict_mcp_present_when_plan_grants(self):
        """準則 2：綁 ha_mcp_tools plan → to_dict()['mcp'] == 'ha_mcp_tools'。"""
        home = self._home_bound_to(self.plan_mcp)
        self.assertEqual(home.to_dict()['mcp'], 'ha_mcp_tools')

    def test_to_dict_mcp_empty_when_plan_plain(self):
        """準則 3：綁無旗標 plan → mcp == ''。"""
        home = self._home_bound_to(self.plan_plain)
        self.assertEqual(home.to_dict()['mcp'], '')

    def test_to_dict_mcp_empty_when_no_subscription(self):
        """準則 3：無訂閱 → mcp == ''（且不拋錯）。"""
        home = self.SmartHome.create({
            'name': 'HA no sub',
            'workspace_id': self.workspace.id,
        })
        self.assertEqual(home.to_dict()['mcp'], '')

    def test_get_mcp_integration_helper_direct(self):
        """getter 直測：綁 MCP plan → 'ha_mcp_tools'。"""
        home = self._home_bound_to(self.plan_mcp)
        self.assertEqual(home._get_mcp_integration(), 'ha_mcp_tools')

    def test_to_dict_mcp_readable_by_restricted_owner(self):
        """【MUST-FIX / 真正驗證 sudo fix（decision 1）】

        以「workspace owner 但只有 base.group_user、無 sh_subscription
        manager group」的受限使用者呼叫 to_dict()——這正是 stg 上讓
        smarthome.subscription_id 觸發 AccessError 的 profile（見
        test_smart_home_subscription_flow.py:498）。

        Pre-fix（getter 用 self.subscription_id 未 sudo）：讀 subscription_id
        觸發 AccessError → 本測試紅。
        Post-fix（self.sudo().subscription_id）：讀穿 → mcp 正確。

        關鍵：① owner groups 硬重設為 base.group_user（否則 sandbox 的
        default_user_template 灌 admin 群組讓測試 no-op）；② invalidate_all()
        清掉 admin-context 讀進的 cache，否則 owner-env 讀命中 cache 繞過 ACL、
        遮住 bug（每個 HTTP request 都是空 cache，這裡模擬之）。
        """
        owner = self.env['res.users'].create({
            'name': 'SH MCP Restricted Owner',
            'login': 'sh_mcp_restricted_owner',
            'email': 'sh_mcp_restricted_owner@example.test',
        })
        owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, owner.groups_id)

        ws = self.Workspace.create({
            'name': 'MCP Restricted WS',
            'owner_id': owner.id,
        })
        home = self.SmartHome.create({
            'name': 'HA restricted owner',
            'workspace_id': ws.id,
        })
        home._create_and_confirm_subscription(owner.partner_id, self.plan_mcp)
        self.assertTrue(home.subscription_id)
        # 清 cache，逼 owner-env 重新過 ACL（否則命中 admin 讀進的 cache）。
        self.env.invalidate_all()

        # 受限 owner 讀 to_dict()：pre-fix 這行拋 AccessError，post-fix 回值正確。
        data = home.with_user(owner).to_dict()
        self.assertEqual(data['mcp'], 'ha_mcp_tools')
```

> **Fixture 輕重（leader decision 3）**：`_home_bound_to` 先嘗試最輕路徑（直建 `sh.subscription.subscription` 帶 `sh_subscription_plan_id` 不 confirm、直接指派 `home.subscription_id`）；若上游 required-field 太 fiddly，fallback 到本檔已寫的 `_create_and_confirm_subscription`（proven）。**但 `test_to_dict_mcp_readable_by_restricted_owner` 需要一個真實、受 ACL 保護的 subscription**，故該測試維持用 `_create_and_confirm_subscription`（＋`invalidate_all()`）——這是驗 sudo fix 的必要條件，不可省。

- [ ] **Step 2: 跑測試確認失敗**

Run:

```
odoo -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSmartHomeToDictMcp --stop-after-init
```

Expected: FAIL —— `to_dict()` 無 `'mcp'` key（`KeyError: 'mcp'`）、`_get_mcp_integration` 不存在（`AttributeError`）。

- [ ] **Step 3: 寫最小實作（getter + to_dict key）**

在 `src/models/smart_home.py` 的 `def to_dict(self):`（line 320）**正上方**插入：

```python
    def _get_mcp_integration(self):
        """Return the MCP integration string granted by the active plan, or ''.

        Mirrors _get_subscription_info's ``self.sudo().subscription_id`` read:
        subscription_id is ACL-restricted, so a non-sudo to_dict() caller
        (portal user) would hit AccessError without the sudo. Derivation
        follows the SA pattern (sub.sh_subscription_plan_id.<field>).
        """
        self.ensure_one()
        sub = self.sudo().subscription_id
        plan = sub.sh_subscription_plan_id if sub else False
        return (plan.mcp_integration or '') if plan else ''
```

在 `to_dict()` 回傳 dict 內、`'subscription': self._get_subscription_info(),`（line 340）**之後**加一行：

```python
            'mcp': self._get_mcp_integration(),
```

即該 dict 尾段變成：

```python
            'subscription': self._get_subscription_info(),
            'mcp': self._get_mcp_integration(),
        }
```

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2。
Expected: PASS（5 tests，含 restricted-owner sudo 驗證）。

- [ ] **Step 5: Commit**

```bash
git add src/models/smart_home.py src/tests/test_smart_home_mcp.py
git commit -m "feat: #270 smart_home.to_dict() 依 plan 推導多回 mcp 字串（sudo 讀 subscription）"
```

---

## Task 3: `ha_api.get_tunnel_token()` response 多回 `mcp`（§4.2 第二入口）

**Files:**
- Modify: `src/controllers/ha_api.py`（`get_tunnel_token()` 回傳 dict，line 247-251，加 `mcp` key）
- Test: `src/tests/test_smart_home_mcp.py`（加 `TestSmartHomeMcpEndpoints`，HttpCase）

**Interfaces:**
- Consumes: `smart_home._get_mcp_integration()`（Task 2）。
- Produces: `GET /api/smarthome/homes/<id>/tunnel-token` response 多一個 `mcp` key；`GET /api/smarthome/homes/<id>` 因回 `to_dict()` 亦含 `mcp`。

- [ ] **Step 1: 寫失敗測試（§7 準則 2 wire ＋ §4.2 第二入口 ＋ 準則 4 no-regression）**

在 `src/tests/test_smart_home_mcp.py` 加（HttpCase，鏡像 `test_ha_api_security_access.py` 的 oauth token + `url_open` pattern）：

```python
@tagged('post_install', '-at_install')
class TestSmartHomeMcpEndpoints(HttpCase):
    """§7 準則 2（wire）＋ §4.2 第二入口（tunnel-token）＋準則 4（無迴歸）。"""

    def setUp(self):
        super().setUp()
        self.user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'MCP EP WS',
            'owner_id': self.user.id,
        })
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.user.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.user.id, 'role': 'owner'})

        self.plan_mcp = self.env['sh.subscription.plan'].sudo().create({
            'name': 'SH MCP EP Plan', 'sh_plan_price': 400.0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'mcp_integration': 'ha_mcp_tools',
        })
        self.env['product.template'].sudo().create({
            'name': 'SH MCP EP Product', 'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan_mcp.id, 'list_price': 400.0,
        })
        company = self.env.company.sudo()
        if not company.sh_journal_id:
            journal = self.env['account.journal'].sudo().search(
                [('type', '=', 'sale')], limit=1)
            if journal:
                company.sh_journal_id = journal.id

        self.home = self.env['woow_paas_platform.smart_home'].sudo().create({
            'name': 'MCP EP HA',
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-mcp-ep',
            'tunnel_token': 'tok-mcp-ep',
            'subdomain': 'paas-sm-mcpep01-02030405',
            'tunnel_status': 'connected',
        })
        self.home._create_and_confirm_subscription(
            self.user.partner_id, self.plan_mcp)

        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')
        self.token = self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': 'mcp-ep-bearer-token',
            'scope': 'smarthome:read smarthome:tunnel',
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': self.user.id,
        })

    def _get(self, url):
        return self.url_open(
            url,
            headers={'Authorization': f'Bearer {self.token.access_token}'},
        )

    def test_get_home_payload_contains_mcp(self):
        """準則 2（wire）：GET /api/smarthome/homes/<id> payload 含 mcp。"""
        resp = self._get(f'/api/smarthome/homes/{self.home.id}')
        self.assertEqual(resp.status_code, 200)
        home = resp.json()['home']
        self.assertEqual(home['mcp'], 'ha_mcp_tools')

    def test_tunnel_token_contains_mcp_and_keeps_existing_keys(self):
        """§4.2 第二入口 ＋ 準則 4：tunnel-token 多回 mcp、既有 key 不變。"""
        resp = self._get(f'/api/smarthome/homes/{self.home.id}/tunnel-token')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # 新增 mcp
        self.assertEqual(data['mcp'], 'ha_mcp_tools')
        # 準則 4：既有回傳合約不動（只加 mcp、不增刪其他）
        self.assertEqual(
            set(data.keys()),
            {'tunnel_token', 'tunnel_id', 'subdomain', 'mcp'},
        )
        self.assertEqual(data['tunnel_token'], 'tok-mcp-ep')
        self.assertEqual(data['tunnel_id'], 'tun-mcp-ep')
```

- [ ] **Step 2: 跑測試確認失敗**

Run:

```
odoo -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSmartHomeMcpEndpoints --stop-after-init
```

Expected: FAIL —— `test_get_home_payload_contains_mcp` 依賴 Task 2（若單獨跑 Task 3 前）；`test_tunnel_token_...` 因 tunnel-token response 無 `mcp` → `KeyError: 'mcp'` / key set 不符。

- [ ] **Step 3: 寫最小實作（tunnel-token 加 mcp key）**

在 `src/controllers/ha_api.py` 的 `get_tunnel_token()` 回傳（line 247-251）加 `mcp`：

```python
        return _json_response({
            'tunnel_token': home.tunnel_token or '',
            'tunnel_id': home.tunnel_id or '',
            'subdomain': fqdn,
            'mcp': home._get_mcp_integration(),
        })
```

> `home` 來自 `_get_home_with_access()`（`src/controllers/ha_api.py:419`）已是 `.sudo()` browse，故 `_get_mcp_integration()` 內的 `self.sudo()` 為安全冗餘。

- [ ] **Step 4: 跑測試確認通過**

Run: 同 Step 2。
Expected: PASS（2 tests）。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ha_api.py src/tests/test_smart_home_mcp.py
git commit -m "feat: #270 ha_api tunnel-token 回傳多帶 mcp（§4.2 第二入口）"
```

---

## Task 4: 版號 bump ＋ 全套件迴歸

**Files:**
- Modify: `src/__manifest__.py`（`"version": "18.0.1.0.49"` → `"18.0.1.0.50"`）

- [ ] **Step 1: bump 版號**

`src/__manifest__.py`：

```python
    "version": "18.0.1.0.50",
```

- [ ] **Step 2: 跑本 feature 全部三個測試 class**

Run:

```
odoo -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSmartHomeMcpPlanField+TestSmartHomeToDictMcp+TestSmartHomeMcpEndpoints \
  --stop-after-init
```

Expected: PASS（9 tests, 0 failures）。

- [ ] **Step 3: 跑全模組測試套件（迴歸 — 準則 4 既有行為不變）**

Run:

```
odoo -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform --stop-after-init
```

Expected: 綠（0 failure）。已知非本 PR 的 flake：agent-runtime env `ns-left-set`（缺 `AGENT_RUNTIME_NAMESPACE_SECRET` 環境變數，非回歸，見 `docs/guides/testing/odoo-test-docker-desktop-paas-image.md`）。**除該既有 flake 外任何紅都要停下排查**（用 systematic-debugging，不硬過）。

- [ ] **Step 4: Commit**

```bash
git add src/__manifest__.py
git commit -m "chore: #270 版號 bump 18.0.1.0.50（Smart Home MCP 平台側）"
```

---

## 驗收準則 ↔ Task 對照（self-review：spec coverage）

| §7 準則 | 覆蓋處 |
| --- | --- |
| 1. plan 有 `mcp_integration`、default ''、不覆寫 std | Task 1 `TestSmartHomeMcpPlanField`（2 tests）＋ model 檔 `_inherit` add-only |
| 2. 綁 `ha_mcp_tools` plan → payload `mcp:"ha_mcp_tools"` | Task 2 `test_to_dict_mcp_present_when_plan_grants`（model）＋ Task 3 `test_get_home_payload_contains_mcp`（wire）|
| 3. `mcp=""` 或無訂閱 → payload `mcp:""` | Task 2 `test_to_dict_mcp_empty_when_plan_plain` ＋ `test_to_dict_mcp_empty_when_no_subscription` |
| 4. 既有行為不變、無新 tunnel/route/token/port | Task 3 `test_tunnel_token_contains_mcp_and_keeps_existing_keys`（key set 鎖死只加 mcp）＋ Task 4 全套件迴歸；設計上 getter 純 ORM 讀、零 operator client / 零 `@route` |
| §4.2 第二入口（tunnel-token 也帶 mcp） | Task 3 `test_tunnel_token_contains_mcp_and_keeps_existing_keys` |
| decision 1（sudo fix 真正被測到，非 admin-only PASS） | Task 2 `test_to_dict_mcp_readable_by_restricted_owner`（受限 owner + invalidate_all，pre-fix 紅 / post-fix 綠）|

**Placeholder scan**：無 TBD/TODO；每個 code step 附完整程式碼。
**Type consistency**：`_get_mcp_integration() -> str` 在 Task 2 定義、Task 3 消費，簽章一致；`mcp` key 型別 str 一致。

---

## Execution Handoff

計畫寫好後，回報 leader（`repo-agent-team-leader #3`）review；leader OK 才實作。實作採 **subagent-driven-development**（每 task 一個 fresh subagent + task 間 review）或 **executing-plans**（本 session 批次 + checkpoint），由 leader 指定。
