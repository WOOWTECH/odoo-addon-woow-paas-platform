# MCP Server Cloud Service（paas-platform 端）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 woow_paas_platform 新增 `mcp-hermes` cloud service type（pilot）+ 訂閱方案，讓租戶能像買 odoo cloud service 一樣訂閱、部署一個 per-instance 的 hermes MCP server；驗證後複製 `mcp-odoo` / `mcp-n8n`。

**Architecture:** 純資料/設定層擴充——沿用現有 cloud service 全鏈（controller → cron → `_execute_deploy()` → operator → helm）。新增一個 `category='mcp'`、3 筆 `CloudAppTemplate` seed（指向 paas-charts 的 `mcp-server` OCI chart）、3 組訂閱 plan/product、plan↔template link，及 `_inject_storage_class` path_map 條目。**不改 operator 主流程、不改部署鏈邏輯。**

**Tech Stack:** Odoo 18 addon（Python 4-space / XML 2-space）、`sh_subscription` 系列、pytest 風格的 Odoo `TransactionCase`。

## Global Constraints

- slug 命名：`mcp-hermes` / `mcp-odoo` / `mcp-n8n`（避免與現有 `n8n`/`odoo` template 衝突）。
- category：新增 Selection 值 `('mcp', 'MCP Server')`。
- 定價：三種統一 **月 499 / 年 4990**（與 odoo 同級）。
- `mcp_enabled=False`：MCP 能力由服務本體內建，**非** sidecar；不設任何 `mcp_sidecar_*` 欄位。
- `post_deploy_init_type='none'`：config seed 由 chart 的 initContainer 處理，無部署後動作（待 O-4 最終確認）。
- `default_port=8080`、`ingress_enabled=True`（對外經 subdomain+tunnel，路由到 :8080）。
- seed 檔 noupdate 慣例：template 與 link 用 `noupdate="0"`；plan/product 用 `noupdate="1"`（沿用各檔現狀，勿改）。
- Python 縮排 4 空格、XML 縮排 2 空格、SCSS/JS 2 空格。
- 測試執行環境：k8s-dev-sandbox（CLAUDE.md「Run tests」段）。單一檔可用
  `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`，
  或 sandbox 內 `odoo-bin --test-enable --test-tags /woow_paas_platform --stop-after-init`。
  所有測試（含 Task 5 透過 model 取用 staticmethod）都需 Odoo 環境載入，於 sandbox 執行。

### 🔗 跨 repo 整合 checkpoint（待 pin 的契約值——並行開發用議定值，整合時換真實值）

這些值在三 repo 整合前用下列「議定預設」，**整合驗證時**換成真實值（不是 TODO，是並行開發的已知佔位）：

| 佔位符 | 議定預設（plan 內先用） | 真實值來源 | pin 時機 |
|--------|------------------------|-----------|----------|
| `MCP_CHART_OCI` | `oci://jcr-prod.woowtech.io/woow-paas-charts/mcp-server` | paas-charts O-2 回報 | 整合 Task I-1 |
| `MCP_CHART_VERSION` | `0.1.0` | paas-charts Chart.yaml | 整合 Task I-1 |
| `HERMES_IMAGE_REPO` | `jcr-prod.woowtech.io/woow-paas-docker-local/hermes-mcp-admin` | woow-mcp-server `.gitea/workflows/ci.yml` | 整合 Task I-1 |
| `HERMES_IMAGE_TAG` | `latest`（pilot 暫用，整合時 pin sha/date tag） | woow-mcp-server O-1 | 整合 Task I-1 |
| chart values key 結構（`image.*`/`envPrefix`/`persistence.*`） | 本 plan 採下列 JSON | paas-charts chart values schema | 整合 Task I-1 |

> 整合 Task I-1：三 repo PR 就緒後，maestro 用真實值更新 `cloud_app_templates.xml` 的
> `helm_chart_name` / `helm_chart_version` / `helm_default_values`，重跑 Task 2 測試。

---

### Task 1: category 新增 `mcp` 選項

**Files:**
- Modify: `src/models/cloud_app_template.py:33-45`（`category` Selection）
- Test: `src/tests/test_cloud_app_template.py:86-102`（`test_category_options`）

**Interfaces:**
- Consumes: 無
- Produces: `category` Selection 含 `'mcp'`，供 Task 2 的 seed 使用。

- [ ] **Step 1: 改測試先加 `mcp`（失敗測試）**

修改 `src/tests/test_cloud_app_template.py` 的 `test_category_options`，把 `valid_categories` 改為：

```python
    def test_category_options(self):
        """Test that all category options are valid."""
        valid_categories = [
            'ai_llm', 'automation', 'database',
            'analytics', 'devops', 'web', 'container', 'mcp'
        ]

        for category in valid_categories:
            template = self.Template.create({
                'name': f'Test {category}',
                'slug': f'test-{category}',
                'category': category,
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': 'test',
                'helm_chart_version': '1.0.0',
            })
            self.assertEqual(template.category, category)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestCloudAppTemplate.test_category_options --stop-after-init`（sandbox 內）
Expected: FAIL — `ValueError: Wrong value for ... category: 'mcp'`（Selection 尚無此值）

- [ ] **Step 3: model 加 `mcp` 選項**

修改 `src/models/cloud_app_template.py` 的 `category` 欄位 selection，在 `('container', 'Container'),` 後加一行：

```python
    category = fields.Selection(
        selection=[
            ('ai_llm', 'AI & LLM'),
            ('automation', 'Automation'),
            ('database', 'Database'),
            ('analytics', 'Analytics'),
            ('devops', 'DevOps'),
            ('web', 'Web'),
            ('container', 'Container'),
            ('mcp', 'MCP Server'),
        ],
        string='Category',
        help='Application category for filtering',
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestCloudAppTemplate.test_category_options --stop-after-init`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/cloud_app_template.py src/tests/test_cloud_app_template.py
git commit -m "feat: cloud_app_template category 新增 mcp 選項"
```

---

### Task 2: `mcp-hermes` CloudAppTemplate seed

**Files:**
- Modify: `src/data/cloud_app_templates.xml`（在 Odoo template 後、`</data>` 前新增 record）
- Create: `src/tests/test_mcp_server_template_seed.py`

**Interfaces:**
- Consumes: Task 1 的 `category='mcp'`。
- Produces: XML id `woow_paas_platform.cloud_app_mcp_hermes`，供 Task 4 link、整合驗證使用。

- [ ] **Step 1: 寫 seed 斷言測試（失敗測試）**

Create `src/tests/test_mcp_server_template_seed.py`：

```python
"""Seed assertions for MCP server cloud service templates (pilot: hermes)."""
from odoo.tests.common import TransactionCase


class TestMcpServerTemplateSeed(TransactionCase):
    """The mcp-hermes template must be seeded tenant-neutral and sidecar-free."""

    def test_hermes_template_seeded(self):
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertEqual(tmpl.slug, 'mcp-hermes')
        self.assertEqual(tmpl.category, 'mcp')
        self.assertTrue(tmpl.is_active)
        self.assertEqual(tmpl.default_port, 8080)
        self.assertTrue(tmpl.ingress_enabled)

    def test_hermes_template_is_not_sidecar_backed(self):
        """MCP capability is built into the body, NOT a sidecar."""
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertFalse(tmpl.mcp_enabled)
        self.assertFalse(tmpl.mcp_sidecar_image)

    def test_hermes_template_no_post_deploy_init(self):
        """config seed handled by chart initContainer; no post-deploy action."""
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertEqual(tmpl.post_deploy_init_type, 'none')

    def test_hermes_template_uses_mcp_server_chart(self):
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertIn('mcp-server', tmpl.helm_chart_name)
        self.assertTrue(tmpl.helm_chart_name.startswith('oci://'))
```

- [ ] **Step 2: 把測試檔登記到 tests `__init__.py`**

修改 `src/tests/__init__.py`，加一行（依字母序附近）：

```python
from . import test_mcp_server_template_seed
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: FAIL — `ValueError: External ID not found ... cloud_app_mcp_hermes`

- [ ] **Step 4: 加 seed record**

在 `src/data/cloud_app_templates.xml` 的 `</data>` 之前（Odoo record 結束後）新增。`helm_chart_name`/`helm_chart_version`/`helm_default_values` 用整合 checkpoint 的議定值（整合 Task I-1 pin 真實值）：

```xml
        <!-- MCP Server: Hermes (pilot) — MCP capability built into the body, NOT a sidecar -->
        <record id="cloud_app_mcp_hermes" model="woow_paas_platform.cloud_app_template">
            <field name="name">Hermes MCP Server</field>
            <field name="slug">mcp-hermes</field>
            <field name="description">Hermes MCP server with admin GUI</field>
            <field name="full_description">Hermes MCP server — a managed Model Context
                Protocol server with an admin GUI and an MCP client endpoint, persisting
                its configuration on a dedicated volume.</field>
            <field name="category">mcp</field>
            <field name="tags">["mcp", "hermes", "mcp-server", "ai"]</field>

            <!-- Helm Configuration (OCI; chart from woow-paas-charts) -->
            <field name="helm_repo_url"></field>
            <field name="helm_chart_name">oci://jcr-prod.woowtech.io/woow-paas-charts/mcp-server</field>
            <field name="helm_chart_version">0.1.0</field>
            <field name="helm_default_values">{"app": "hermes", "image": {"repository": "jcr-prod.woowtech.io/woow-paas-docker-local/hermes-mcp-admin", "tag": "latest"}, "service": {"port": 8080}, "envPrefix": "HERMES_", "persistence": {"enabled": true, "size": "2Gi"}, "updateStrategy": {"type": "Recreate"}}</field>
            <field name="helm_value_specs">{"required": [], "optional": []}</field>

            <!-- Service Configuration -->
            <field name="default_port">8080</field>
            <field name="ingress_enabled" eval="True" />

            <!-- Resource Requirements -->
            <field name="min_vcpu">1</field>
            <field name="min_ram_gb">1.0</field>
            <field name="min_storage_gb">5</field>

            <!-- MCP capability is built into the service body; NO sidecar -->
            <field name="mcp_enabled" eval="False" />
            <field name="post_deploy_init_type">none</field>

            <field name="is_active" eval="True" />
        </record>
```

- [ ] **Step 5: 跑測試確認通過**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: PASS（4 tests）

- [ ] **Step 6: Commit**

```bash
git add src/data/cloud_app_templates.xml src/tests/test_mcp_server_template_seed.py src/tests/__init__.py
git commit -m "feat: 新增 mcp-hermes cloud_app_template seed（pilot）"
```

---

### Task 3: `mcp-hermes` 訂閱 plan + product

**Files:**
- Modify: `src/data/subscription_products.xml`（`noupdate="1"` 區塊內）
- Modify: `src/tests/test_mcp_server_template_seed.py`（加 plan/product 斷言）

**Interfaces:**
- Consumes: 無（plan/product 獨立）。
- Produces: XML ids `plan_mcp_hermes_monthly` / `plan_mcp_hermes_yearly` / `product_mcp_hermes_monthly` / `product_mcp_hermes_yearly`，供 Task 4 link。

- [ ] **Step 1: 寫 plan/product 斷言（失敗測試）**

在 `src/tests/test_mcp_server_template_seed.py` 的類別內加：

```python
    def test_hermes_plans_seeded_at_odoo_pricing(self):
        monthly = self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_mcp_hermes_yearly')
        self.assertEqual(monthly.sh_plan_price, 499)
        self.assertEqual(monthly.sh_unit, 'month')
        self.assertEqual(yearly.sh_plan_price, 4990)
        self.assertEqual(yearly.sh_unit, 'year')

    def test_hermes_products_link_plans(self):
        prod_m = self.env.ref('woow_paas_platform.product_mcp_hermes_monthly')
        self.assertEqual(prod_m.type, 'service')
        self.assertTrue(prod_m.sh_product_subscribe)
        self.assertEqual(
            prod_m.sh_subscription_plan_id,
            self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly'),
        )
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: FAIL — `External ID not found ... plan_mcp_hermes_monthly`

- [ ] **Step 3: 加 plan records**

在 `src/data/subscription_products.xml` 的 Plans 區段（Security Access Plans 之後、Products 註解之前）新增：

```xml
        <!-- MCP Server: Hermes Plans (odoo-level pricing) -->
        <record id="plan_mcp_hermes_monthly" model="sh.subscription.plan">
            <field name="name">Hermes MCP Server Monthly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">month</field>
            <field name="sh_plan_price">499</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_never_expire" eval="True"/>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="sh_start_immediately" eval="False"/>
            <field name="sh_is_close_by_customer" eval="True"/>
            <field name="sh_reminder"
                   eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
        </record>

        <record id="plan_mcp_hermes_yearly" model="sh.subscription.plan">
            <field name="name">Hermes MCP Server Yearly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">year</field>
            <field name="sh_plan_price">4990</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_never_expire" eval="True"/>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="sh_start_immediately" eval="False"/>
            <field name="sh_is_close_by_customer" eval="True"/>
            <field name="sh_reminder"
                   eval="[(4, ref('reminder_3days_before')), (4, ref('reminder_1day_before'))]"/>
        </record>
```

- [ ] **Step 4: 加 product records**

在同檔的 Products 區段（Security Access Products 之後、`</data>` 之前）新增：

```xml
        <!-- MCP Server: Hermes Products (one per plan) -->
        <record id="product_mcp_hermes_monthly" model="product.product">
            <field name="name">Hermes MCP Server Monthly Subscription</field>
            <field name="type">service</field>
            <field name="list_price">499</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="sh_subscription_plan_id" ref="plan_mcp_hermes_monthly"/>
        </record>

        <record id="product_mcp_hermes_yearly" model="product.product">
            <field name="name">Hermes MCP Server Yearly Subscription</field>
            <field name="type">service</field>
            <field name="list_price">4990</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="sh_subscription_plan_id" ref="plan_mcp_hermes_yearly"/>
        </record>
```

- [ ] **Step 5: 跑測試確認通過**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/data/subscription_products.xml src/tests/test_mcp_server_template_seed.py
git commit -m "feat: 新增 mcp-hermes 訂閱 plan/product（月499/年4990）"
```

---

### Task 4: plan ↔ template link

**Files:**
- Modify: `src/data/subscription_template_links.xml`（`noupdate="0"` 區塊內）
- Modify: `src/tests/test_mcp_server_template_seed.py`（加 link 斷言）

**Interfaces:**
- Consumes: Task 2 的 `cloud_app_mcp_hermes`、Task 3 的 `plan_mcp_hermes_*`。
- Produces: template ↔ plan M2M link。

- [ ] **Step 1: 寫 link 斷言（失敗測試）**

在 `src/tests/test_mcp_server_template_seed.py` 加：

```python
    def test_hermes_template_linked_to_plans(self):
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        linked = tmpl.subscription_plan_ids
        self.assertIn(self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly'), linked)
        self.assertIn(self.env.ref('woow_paas_platform.plan_mcp_hermes_yearly'), linked)

    def test_hermes_plans_not_shared_with_other_service_types(self):
        """The cross-service-type constraint must not be tripped by the seed."""
        monthly = self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly')
        sh = self.env['woow_paas_platform.smart_home_template'].search(
            [('subscription_plan_ids', 'in', monthly.ids)])
        sa = self.env['woow_paas_platform.security_access_template'].search(
            [('subscription_plan_ids', 'in', monthly.ids)])
        self.assertFalse(sh)
        self.assertFalse(sa)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed.test_hermes_template_linked_to_plans --stop-after-init`
Expected: FAIL — `subscription_plan_ids` 為空（assertIn 失敗）

- [ ] **Step 3: 加 link record**

在 `src/data/subscription_template_links.xml` 的 cloud_app 區段（`cloud_app_odoo` link 之後）新增：

```xml
        <record id="cloud_app_mcp_hermes" model="woow_paas_platform.cloud_app_template">
            <field name="subscription_plan_ids"
                   eval="[(6, 0, [ref('plan_mcp_hermes_monthly'), ref('plan_mcp_hermes_yearly')])]"/>
        </record>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add src/data/subscription_template_links.xml src/tests/test_mcp_server_template_seed.py
git commit -m "feat: 連結 mcp-hermes template 與訂閱方案"
```

---

### Task 5: `_inject_storage_class` 加 mcp-* path_map

**Files:**
- Modify: `src/models/cloud_service.py:624-640`（`path_map`）
- Test: `src/tests/test_cloud_service.py`（新增 storageClass 注入測試）

**Interfaces:**
- Consumes: 無（純 staticmethod）。
- Produces: `_inject_storage_class(values, 'mcp-hermes', sc)` 在 `values['persistence']['storageClass']` 寫入 sc。

- [ ] **Step 1: 寫 staticmethod 測試（失敗測試）**

在 `src/tests/test_cloud_service.py` 檔尾新增一個獨立測試類別（test-tags 用 `TestInjectStorageClass`，與 Step 2/4 一致）。檔案開頭若尚未 import `TransactionCase` 則補上 `from odoo.tests.common import TransactionCase`：

```python
class TestInjectStorageClass(TransactionCase):
    """_inject_storage_class must map mcp-* slugs to persistence.storageClass."""

    def test_inject_storage_class_mcp_hermes(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        out = CloudService._inject_storage_class({}, 'mcp-hermes', 'longhorn-delete')
        self.assertEqual(out['persistence']['storageClass'], 'longhorn-delete')

    def test_inject_storage_class_mcp_odoo_and_n8n(self):
        CloudService = self.env['woow_paas_platform.cloud_service']
        for slug in ('mcp-odoo', 'mcp-n8n'):
            out = CloudService._inject_storage_class({}, slug, 'sc-x')
            self.assertEqual(out['persistence']['storageClass'], 'sc-x')
```

- [ ] **Step 2: 跑測試確認（先觀察 default fallback 行為）**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestInjectStorageClass --stop-after-init`
Expected: 由於 default 已 fallback 到 `[('persistence','storageClass')]`，這兩個測試**可能直接 PASS**。若 PASS，仍執行 Step 3 把 mcp-* 顯式登記（防未來改 default 漂移、文件化意圖），測試作回歸守線。

- [ ] **Step 3: path_map 顯式加 mcp-***

修改 `src/models/cloud_service.py` 的 `path_map`，在 `'redis'` 條目後加：

```python
            'redis': [
                ('master', 'persistence', 'storageClass'),
                ('replica', 'persistence', 'storageClass'),
            ],
            'mcp-hermes': [
                ('persistence', 'storageClass'),
            ],
            'mcp-odoo': [
                ('persistence', 'storageClass'),
            ],
            'mcp-n8n': [
                ('persistence', 'storageClass'),
            ],
        }
```

- [ ] **Step 4: 跑測試確認通過**

Run: `odoo-bin --test-enable --test-tags /woow_paas_platform:TestInjectStorageClass --stop-after-init`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/cloud_service.py src/tests/test_cloud_service.py
git commit -m "feat: _inject_storage_class 支援 mcp-* slug 的 storageClass 注入"
```

---

### Task 6: 模組整體升級驗證（fresh + upgrade 路徑）

**Files:** 無（驗證型 task）

**Interfaces:**
- Consumes: Task 1-5 全部。
- Produces: 確認 `-i`（fresh）與 `-u`（upgrade）兩條路徑都正確套用 seed 與 link（R3 守線）。

- [ ] **Step 1: fresh install 驗證**

Run（sandbox，全新 DB）: `odoo-bin -i woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: PASS — seed/plan/link 全部就位。

- [ ] **Step 2: upgrade 路徑驗證（先裝後升，模擬 stg/prod）**

Run: 先 `odoo-bin -i woow_paas_platform --stop-after-init`，再 `odoo-bin -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestMcpServerTemplateSeed --stop-after-init`
Expected: PASS — link（`noupdate="0"`）在 `-u` 不被 sticky skip（R3）；plan/product（`noupdate="1"`）首裝後不被覆寫。

- [ ] **Step 3: 全模組測試無回歸**

Run: `./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`
Expected: 既有測試全綠（特別是 `test_cloud_app_template`、`test_cloud_service`、`test_subscription_lifecycle`）。

- [ ] **Step 4: Commit（若有修正）**

```bash
git add -A && git commit -m "test: mcp-hermes seed fresh+upgrade 路徑驗證"
```

---

### Task 7（pilot 驗證後）：複製 mcp-odoo / mcp-n8n

> **前置**：pilot hermes 端到端整合驗證（charts + image + platform）通過後再執行。沿用 Task 2/3/4 的格式，把 `hermes`→`odoo`/`n8n`、`HERMES_`→`""`（odoo/n8n 的 `env_prefix` 為空）、image repo 換對應 `*-mcp-admin`。n8n 須先取得 woow-mcp-server O-6（`/mcp` vs `/sse` transport path）結論再補 client URL 文件。

**Files:**
- Modify: `src/data/cloud_app_templates.xml`（`cloud_app_mcp_odoo` / `cloud_app_mcp_n8n`）
- Modify: `src/data/subscription_products.xml`（odoo/n8n 的 plan+product，均 499/4990）
- Modify: `src/data/subscription_template_links.xml`（兩條 link）
- Modify: `src/tests/test_mcp_server_template_seed.py`（參數化或複製 hermes 斷言到 odoo/n8n）

- [ ] **Step 1-N**：對 odoo/n8n 重複 Task 2 Step 4、Task 3 Step 3-4、Task 4 Step 3 的 record（改名/改 env_prefix/改 image repo），每個 service type 各自寫 seed 斷言測試 → 失敗 → 加 record → 通過 → commit。
- [ ] **整合**：各自走 Task 6 的 fresh+upgrade 驗證 + 端到端部署驗證。

---

## 整合 Task I-1：跨 repo 契約 pin（三 PR 就緒後，maestro 執行）

- [ ] 從 paas-charts PR 取真實 `MCP_CHART_OCI` + `MCP_CHART_VERSION`，更新 `cloud_app_mcp_hermes` 的 `helm_chart_name` / `helm_chart_version`。
- [ ] 從 woow-mcp-server PR 取真實 `HERMES_IMAGE_REPO` + `HERMES_IMAGE_TAG`，更新 `helm_default_values` 的 `image.repository` / `image.tag`。
- [ ] 對齊 paas-charts chart 的 values key（`image.*` / `envPrefix` / `persistence.*` / `service.port`）與 `helm_default_values` JSON；若 key 名不同則改 JSON 對齊真實 chart。
- [ ] 對齊 `helm_value_specs`（使用者可配置欄位）與 woow-mcp-server O-1 的 config schema（哪些走 Secret、哪些讓使用者填）。
- [ ] 重跑 Task 2 測試 + 端到端（k8s-dev-sandbox 部署 mcp-hermes）。
```
