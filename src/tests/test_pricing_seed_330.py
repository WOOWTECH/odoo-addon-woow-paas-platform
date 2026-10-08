"""#330 定價 seed 重定（30 檔）＋ plan taxonomy schema 測試。

對應總表 checklist G1–G6（G4 前端 picker 冒煙不在本 PR scope）：
  Task 1: TestPlanTaxonomySchema — service_type Selection 3 值＋條件式 guard
          （orphan 放行）＋ ac_max_access_seats Integer default 0
  Task 2: TestPricingSeedIntegrity（G1）／TestShMcpSeedLiteral（G2）／
          TestPlaceholdersUntouched（G5）／TestServiceTypeSeedCoverage（G6）／
          TestTemplatePlanWiring（E2）
  Task 3: TestPricingMigration330（G3）— 模擬 .51→.52 升級
設計依據：docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md（拍板記錄）
＋ docs/plans/2026-Q3/2026-07-10-subscription-plan-matrix.md（30 檔明細）。
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

MOD = 'woow_paas_platform'

# ---- 30 檔 plan：xmlid → (價格, 名稱)（Wave B reprice 後的 .59 fresh 值） ----
EXPECTED_PLANS = {
    # CS — Odoo 三階（Wave B：改名為中文＋級距語意 4-7/8-11、年繳 ×8）
    'plan_odoo_monthly': (1500, 'Odoo 訂閱服務（1-3 人）'),
    'plan_odoo_yearly': (12000, 'Odoo 訂閱服務（1-3 人）'),
    'plan_odoo_t2_monthly': (4500, 'Odoo 訂閱服務（4-7 人）'),
    'plan_odoo_t2_yearly': (36000, 'Odoo 訂閱服務（4-7 人）'),
    'plan_odoo_t3_monthly': (7500, 'Odoo 訂閱服務（8-11 人）'),
    'plan_odoo_t3_yearly': (60000, 'Odoo 訂閱服務（8-11 人）'),
    # CS — n8n 三階（年繳 ×8）
    'plan_n8n_monthly': (750, 'n8n 雲端訂閱服務（1 人）'),
    'plan_n8n_yearly': (6000, 'n8n 雲端訂閱服務（1 人）'),
    'plan_n8n_t2_monthly': (2250, 'n8n 雲端訂閱服務（3 人）'),
    'plan_n8n_t2_yearly': (18000, 'n8n 雲端訂閱服務（3 人）'),
    'plan_n8n_t3_monthly': (4500, 'n8n 雲端訂閱服務（6 人）'),
    'plan_n8n_t3_yearly': (36000, 'n8n 雲端訂閱服務（6 人）'),
    # CS-MCP — 統一單階（改價 only、不改名；年 30000→24000 對齊 ×8）
    'plan_mcp_odoo_monthly': (3000, 'Odoo MCP 伺服器 月繳'),
    'plan_mcp_odoo_yearly': (24000, 'Odoo MCP 伺服器 年繳'),
    'plan_mcp_n8n_monthly': (3000, 'n8n MCP 伺服器 月繳'),
    'plan_mcp_n8n_yearly': (24000, 'n8n MCP 伺服器 年繳'),
    # CS-MCP — Hermes 三階（封存原價、Wave B 不動；年繳 ×10）
    'plan_mcp_hermes_monthly': (1500, 'Hermes MCP 伺服器 T1（1–3 人）月繳'),
    'plan_mcp_hermes_yearly': (15000, 'Hermes MCP 伺服器 T1（1–3 人）年繳'),
    'plan_mcp_hermes_t2_monthly': (4500, 'Hermes MCP 伺服器 T2（3–6 人）月繳'),
    'plan_mcp_hermes_t2_yearly': (45000, 'Hermes MCP 伺服器 T2（3–6 人）年繳'),
    'plan_mcp_hermes_t3_monthly': (6000, 'Hermes MCP 伺服器 T3（6–10 人）月繳'),
    'plan_mcp_hermes_t3_yearly': (60000, 'Hermes MCP 伺服器 T3（6–10 人）年繳'),
    # SH — Connect（月 150 不動、年 1500→1200）／+MCP（400→300、4000→2400）
    'plan_smarthome_monthly': (150, '智慧家庭連線 月繳'),
    'plan_smarthome_yearly': (1200, '智慧家庭連線 年繳'),
    'plan_smarthome_mcp_monthly': (300, '智慧家庭＋MCP 月繳'),
    'plan_smarthome_mcp_yearly': (2400, '智慧家庭＋MCP 年繳'),
    # SA — 6 組／10 組（改名＋reprice；quota 隨 routes、年繳 ×9）
    'plan_security_access_entry_monthly': (1200, '遠端安全連結訂閱服務（6 組）'),
    'plan_security_access_entry_yearly': (10800, '遠端安全連結訂閱服務（6 組）'),
    'plan_security_access_pro_monthly': (2000, '遠端安全連結訂閱服務（10 組）'),
    'plan_security_access_pro_yearly': (18000, '遠端安全連結訂閱服務（10 組）'),
}

# .52 migration 目標（TestPricingMigration330 專用；凍結 .52 狀態，與 fresh
# seed 的 .59 EXPECTED_PLANS 解耦——.52 歷史 migration 恆產 .52 值，不隨 Wave B
# reprice 漂移）。鍵＝OLD_PLANS_51（16 筆）。
MIG_52_TARGET = {
    'plan_odoo_monthly': (1000, 'Odoo Cloud T1（1–3 人）Monthly'),
    'plan_odoo_yearly': (10000, 'Odoo Cloud T1（1–3 人）Yearly'),
    'plan_n8n_monthly': (1000, 'n8n Cloud T1（1–3 人）Monthly'),
    'plan_n8n_yearly': (10000, 'n8n Cloud T1（1–3 人）Yearly'),
    'plan_smarthome_monthly': (150, 'Smart Home Connect Monthly'),
    'plan_smarthome_yearly': (1500, 'Smart Home Connect Yearly'),
    'plan_security_access_entry_monthly': (1000, 'Security Access 5 Application Monthly'),
    'plan_security_access_entry_yearly': (10000, 'Security Access 5 Application Yearly'),
    'plan_security_access_pro_monthly': (2000, 'Security Access 10 Application Monthly'),
    'plan_security_access_pro_yearly': (20000, 'Security Access 10 Application Yearly'),
    'plan_mcp_odoo_monthly': (3000, 'Odoo MCP Server Monthly'),
    'plan_mcp_odoo_yearly': (30000, 'Odoo MCP Server Yearly'),
    'plan_mcp_n8n_monthly': (3000, 'n8n MCP Server Monthly'),
    'plan_mcp_n8n_yearly': (30000, 'n8n MCP Server Yearly'),
    'plan_mcp_hermes_monthly': (1500, 'Hermes MCP Server T1（1–3 人）Monthly'),
    'plan_mcp_hermes_yearly': (15000, 'Hermes MCP Server T1（1–3 人）Yearly'),
}

# 月/年 檔位配對（年價＝月價×10 機械驗證）
MONTHLY_YEARLY_PAIRS = [
    ('plan_odoo_monthly', 'plan_odoo_yearly'),
    ('plan_odoo_t2_monthly', 'plan_odoo_t2_yearly'),
    ('plan_odoo_t3_monthly', 'plan_odoo_t3_yearly'),
    ('plan_n8n_monthly', 'plan_n8n_yearly'),
    ('plan_n8n_t2_monthly', 'plan_n8n_t2_yearly'),
    ('plan_n8n_t3_monthly', 'plan_n8n_t3_yearly'),
    ('plan_mcp_odoo_monthly', 'plan_mcp_odoo_yearly'),
    ('plan_mcp_n8n_monthly', 'plan_mcp_n8n_yearly'),
    ('plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly'),
    ('plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly'),
    ('plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly'),
    ('plan_smarthome_monthly', 'plan_smarthome_yearly'),
    ('plan_smarthome_mcp_monthly', 'plan_smarthome_mcp_yearly'),
    ('plan_security_access_entry_monthly', 'plan_security_access_entry_yearly'),
    ('plan_security_access_pro_monthly', 'plan_security_access_pro_yearly'),
]

# 4 筆 placeholder（G5：不動——價格/名稱原樣）
PLACEHOLDER_PLANS = {
    'plan_postgresql_monthly': (99, 'PostgreSQL 月繳'),
    'plan_postgresql_yearly': (990, 'PostgreSQL 年繳'),
    'plan_anythingllm_monthly': (299, 'AnythingLLM 月繳'),
    'plan_anythingllm_yearly': (2990, 'AnythingLLM 年繳'),
}

# 34 筆 plan 的 service_type 歸屬（D4 backfill 對照表；含 placeholder）
EXPECTED_SERVICE_TYPE = {}
for _sfx in EXPECTED_PLANS:
    if _sfx.startswith('plan_smarthome'):
        EXPECTED_SERVICE_TYPE[_sfx] = 'smart_home'
    elif _sfx.startswith('plan_security_access'):
        EXPECTED_SERVICE_TYPE[_sfx] = 'security_access'
    else:
        EXPECTED_SERVICE_TYPE[_sfx] = 'cloud_service'
for _sfx in PLACEHOLDER_PLANS:
    EXPECTED_SERVICE_TYPE[_sfx] = 'cloud_service'

# E2：template → plan xmlid 集合（fresh .59 終態；n8n +t4、SA +r1/r3）
EXPECTED_TEMPLATE_PLANS = {
    'cloud_app_odoo': {
        'plan_odoo_monthly', 'plan_odoo_yearly',
        'plan_odoo_t2_monthly', 'plan_odoo_t2_yearly',
        'plan_odoo_t3_monthly', 'plan_odoo_t3_yearly',
    },
    'cloud_app_n8n': {
        'plan_n8n_monthly', 'plan_n8n_yearly',
        'plan_n8n_t2_monthly', 'plan_n8n_t2_yearly',
        'plan_n8n_t3_monthly', 'plan_n8n_t3_yearly',
        'plan_n8n_t4_monthly', 'plan_n8n_t4_yearly',
    },
    'cloud_app_mcp_hermes': {
        'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
        'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
        'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
    },
    'cloud_app_mcp_odoo': {'plan_mcp_odoo_monthly', 'plan_mcp_odoo_yearly'},
    'cloud_app_mcp_n8n': {'plan_mcp_n8n_monthly', 'plan_mcp_n8n_yearly'},
    'cloud_app_postgresql': {'plan_postgresql_monthly', 'plan_postgresql_yearly'},
    'smart_home_home_assistant': {
        'plan_smarthome_monthly', 'plan_smarthome_yearly',
        'plan_smarthome_mcp_monthly', 'plan_smarthome_mcp_yearly',
    },
    'security_access_default': {
        'plan_security_access_r1_monthly', 'plan_security_access_r1_yearly',
        'plan_security_access_r3_monthly', 'plan_security_access_r3_yearly',
        'plan_security_access_entry_monthly',
        'plan_security_access_entry_yearly',
        'plan_security_access_pro_monthly',
        'plan_security_access_pro_yearly',
    },
}

# .52-era template 集合（TestPricingMigration330 E2 專用；凍結——.52 migration
# 只知 .52 期的 plan，不含 Wave B 新增的 n8n t4，故與 fresh 的 cloud_app_n8n 相異）
MIG_52_TEMPLATE_PLANS = {
    'cloud_app_odoo': {
        'plan_odoo_monthly', 'plan_odoo_yearly',
        'plan_odoo_t2_monthly', 'plan_odoo_t2_yearly',
        'plan_odoo_t3_monthly', 'plan_odoo_t3_yearly',
    },
    'cloud_app_n8n': {
        'plan_n8n_monthly', 'plan_n8n_yearly',
        'plan_n8n_t2_monthly', 'plan_n8n_t2_yearly',
        'plan_n8n_t3_monthly', 'plan_n8n_t3_yearly',
    },
    'cloud_app_mcp_hermes': {
        'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
        'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
        'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
    },
    'smart_home_home_assistant': {
        'plan_smarthome_monthly', 'plan_smarthome_yearly',
        'plan_smarthome_mcp_monthly', 'plan_smarthome_mcp_yearly',
    },
}


def _plan(env, suffix):
    return env.ref('%s.%s' % (MOD, suffix))


def _product(env, plan_suffix):
    return env.ref('%s.%s' % (MOD, plan_suffix.replace('plan_', 'product_', 1)))


class TestPlanTaxonomySchema(TransactionCase):
    """B1/B2 schema：service_type（3 值＋guard、orphan 放行）＋ ac_max_access_seats。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Plan = cls.env['sh.subscription.plan']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']

    def _make_plan(self, name, **extra):
        vals = {
            'name': name,
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        }
        vals.update(extra)
        return self.Plan.create(vals)

    def _make_cs_template(self, name):
        return self.Template.create({
            'name': name,
            'helm_repo_url': 'https://charts.example.test',
            'helm_chart_name': 'dummy',
            'helm_chart_version': '1.0.0',
        })

    # ---- service_type 欄位形狀（OQ-A 拍板：3 值、MCP 不設第 4 值） ----

    def test_service_type_is_selection_with_exactly_three_values(self):
        field = self.Plan._fields['service_type']
        self.assertEqual(field.type, 'selection')
        self.assertEqual(
            {key for key, _label in field.selection},
            {'cloud_service', 'smart_home', 'security_access'},
        )

    # ---- ac_max_access_seats（OQ-B 拍板定名；MAX 語意、default 0） ----

    def test_ac_max_access_seats_integer_default_zero(self):
        field = self.Plan._fields['ac_max_access_seats']
        self.assertEqual(field.type, 'integer')
        plan = self._make_plan('AC Default Plan 330')
        self.assertEqual(plan.ac_max_access_seats, 0)

    # ---- 條件式 guard（§3.3）：存值須與 owning template family 一致 ----

    def test_guard_rejects_service_type_mismatching_template_family(self):
        plan = self._make_plan('Guard Mismatch Plan 330')
        template = self._make_cs_template('Guard CS Template 330')
        template.subscription_plan_ids = [(4, plan.id)]
        with self.assertRaises(ValidationError):
            plan.service_type = 'smart_home'

    def test_guard_accepts_service_type_matching_template_family(self):
        plan = self._make_plan('Guard Match Plan 330')
        template = self._make_cs_template('Guard CS Template Match 330')
        template.subscription_plan_ids = [(4, plan.id)]
        plan.service_type = 'cloud_service'
        self.assertEqual(plan.service_type, 'cloud_service')

    def test_guard_allows_orphan_plan_any_value(self):
        """orphan（從未掛任何 template M2M）放行—— families 為空分支。
        AnythingLLM 2 筆 seed 即此形狀，backfill 不可炸。"""
        for value in ('cloud_service', 'smart_home', 'security_access'):
            plan = self._make_plan('Guard Orphan Plan 330 %s' % value)
            plan.service_type = value  # 不得 raise
            self.assertEqual(plan.service_type, value)


class TestPricingSeedIntegrity(TransactionCase):
    """G1：30 筆定案 plan 價格＋年=月×10＋全域常數＋product 綁定/同價。"""

    def test_plan_prices_and_names(self):
        for suffix, (price, name) in EXPECTED_PLANS.items():
            plan = _plan(self.env, suffix)
            self.assertEqual(
                plan.sh_plan_price, price,
                '%s 價格應為 %s（實際 %s）' % (suffix, price, plan.sh_plan_price))
            self.assertEqual(
                plan.name, name,
                '%s 名稱應為 %r（實際 %r）' % (suffix, name, plan.name))

    def test_yearly_equals_monthly_times_ratio(self):
        """年繳倍率（Wave B 混合制）：Hermes ×10（原制封存）、SA ×9（CF Tunnel
        慣例）、其餘 ×8（正式 214 manifest 對齊）。"""
        for m_sfx, y_sfx in MONTHLY_YEARLY_PAIRS:
            monthly = _plan(self.env, m_sfx)
            yearly = _plan(self.env, y_sfx)
            if 'hermes' in m_sfx:
                ratio = 10
            elif 'security_access' in m_sfx:
                ratio = 9
            else:
                ratio = 8
            self.assertEqual(
                yearly.sh_plan_price, monthly.sh_plan_price * ratio,
                '%s 年價應=月價×%d' % (y_sfx, ratio))
            self.assertEqual(monthly.sh_unit, 'month', m_sfx)
            self.assertEqual(yearly.sh_unit, 'year', y_sfx)

    def test_global_constants_all_34_plans(self):
        """11 欄全域常數（勿照 fixture 6 欄最小集 seed 的機械守線）。"""
        for suffix in list(EXPECTED_PLANS) + list(PLACEHOLDER_PLANS):
            plan = _plan(self.env, suffix)
            self.assertTrue(plan.sh_trial, '%s sh_trial' % suffix)
            self.assertEqual(plan.sh_trial_duration, 7, suffix)
            self.assertEqual(plan.sh_trial_unit, 'day', suffix)
            self.assertTrue(plan.sh_override_product, suffix)
            self.assertTrue(plan.sh_never_expire, suffix)
            self.assertEqual(plan.sh_no_of_billing_cycle, -1, suffix)
            self.assertEqual(plan.sh_duration, 1, suffix)
            self.assertFalse(plan.sh_start_immediately, suffix)
            self.assertTrue(plan.sh_is_close_by_customer, suffix)
            self.assertEqual(len(plan.sh_reminder), 2,
                             '%s 應帶 2 筆 reminder' % suffix)

    def test_products_bound_and_price_synced(self):
        for suffix, (price, name) in EXPECTED_PLANS.items():
            plan = _plan(self.env, suffix)
            product = _product(self.env, suffix)
            self.assertEqual(
                product.sh_subscription_plan_id, plan,
                '%s product 應綁定其 plan' % suffix)
            self.assertEqual(
                product.list_price, price,
                '%s product list_price 應與 plan 同步=%s' % (suffix, price))
            self.assertEqual(product.name, name + ' Subscription', suffix)
            self.assertEqual(product.type, 'service', suffix)
            self.assertTrue(product.sh_product_subscribe, suffix)

    def test_sa_quota_values(self):
        """Wave B：6 組檔 sa_max_routes=6（policies 亦 3→6）；10 組檔 routes=10。"""
        for suffix in ('plan_security_access_entry_monthly',
                       'plan_security_access_entry_yearly'):
            plan = _plan(self.env, suffix)
            self.assertEqual(plan.sa_max_routes, 6, suffix)
            self.assertEqual(plan.sa_max_access_policies, 6, suffix)
        for suffix in ('plan_security_access_pro_monthly',
                       'plan_security_access_pro_yearly'):
            self.assertEqual(_plan(self.env, suffix).sa_max_routes, 10, suffix)


class TestShMcpSeedLiteral(TransactionCase):
    """G2：SH +MCP 檔 mcp_integration 字面精確＝'ha_mcp_tools'；base 檔=''。"""

    def test_mcp_plans_literal(self):
        for suffix in ('plan_smarthome_mcp_monthly', 'plan_smarthome_mcp_yearly'):
            self.assertEqual(
                _plan(self.env, suffix).mcp_integration, 'ha_mcp_tools', suffix)

    def test_base_plans_empty(self):
        for suffix in ('plan_smarthome_monthly', 'plan_smarthome_yearly'):
            self.assertFalse(_plan(self.env, suffix).mcp_integration, suffix)


class TestPlaceholdersUntouched(TransactionCase):
    """G5：不刪任何既有 record——pg／anythingllm 4 筆價格/名稱原樣。"""

    def test_placeholder_plans_untouched(self):
        for suffix, (price, name) in PLACEHOLDER_PLANS.items():
            plan = _plan(self.env, suffix)
            self.assertEqual(plan.sh_plan_price, price, suffix)
            self.assertEqual(plan.name, name, suffix)

    def test_placeholder_products_untouched(self):
        for suffix, (price, name) in PLACEHOLDER_PLANS.items():
            product = _product(self.env, suffix)
            self.assertEqual(product.list_price, price, suffix)
            self.assertEqual(product.name, name + ' Subscription', suffix)


class TestServiceTypeSeedCoverage(TransactionCase):
    """G6：34 筆 plan service_type 全非空；MCP/Hermes 6 筆＝cloud_service；
    分佈 26/4/4；guard 對真 seed orphan（AnythingLLM）放行。"""

    def test_all_34_plans_have_service_type(self):
        for suffix, expected in EXPECTED_SERVICE_TYPE.items():
            plan = _plan(self.env, suffix)
            self.assertTrue(
                plan.service_type,
                '%s service_type 不得為空' % suffix)
            self.assertEqual(plan.service_type, expected, suffix)

    def test_mcp_hermes_six_plans_are_cloud_service(self):
        for suffix in ('plan_mcp_odoo_monthly', 'plan_mcp_odoo_yearly',
                       'plan_mcp_n8n_monthly', 'plan_mcp_n8n_yearly',
                       'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly'):
            self.assertEqual(
                _plan(self.env, suffix).service_type, 'cloud_service', suffix)

    def test_distribution_26_4_4(self):
        counts = {'cloud_service': 0, 'smart_home': 0, 'security_access': 0}
        for suffix, expected in EXPECTED_SERVICE_TYPE.items():
            counts[_plan(self.env, suffix).service_type] += 1
        self.assertEqual(counts,
                         {'cloud_service': 26, 'smart_home': 4,
                          'security_access': 4})

    def test_guard_allows_real_seed_orphan_write(self):
        """AnythingLLM 無 template（orphan）→ 直接 write 不炸＝guard 放行實證。"""
        plan = _plan(self.env, 'plan_anythingllm_monthly')
        plan.service_type = 'cloud_service'  # 不得 raise
        self.assertEqual(plan.service_type, 'cloud_service')


class TestTemplatePlanWiring(TransactionCase):
    """E2：template↔plan M2M 白名單（新裝軌＝subscription_template_links.xml）。"""

    def test_template_plan_sets(self):
        for tpl_suffix, plan_suffixes in EXPECTED_TEMPLATE_PLANS.items():
            template = self.env.ref('%s.%s' % (MOD, tpl_suffix))
            expected_ids = {
                _plan(self.env, sfx).id for sfx in plan_suffixes}
            # Wave B 後 template 另掛 208 個 access 變體（ac_max_access_seats>0）；
            # 本 E2 驗 base plan M2M 白名單，故過濾變體只比 base 集（變體↔template
            # 綁定另由 test_plan_variants_generated 全量驗）。
            base_ids = set(template.subscription_plan_ids.filtered(
                lambda p: p.ac_max_access_seats == 0).ids)
            self.assertEqual(
                base_ids, expected_ids,
                '%s 的 base plan M2M 應為 %s' % (tpl_suffix, sorted(plan_suffixes)))


# ---- G3 migration 模擬用：.51 升級前 DB shape（舊價/舊名） ----
OLD_PLANS_51 = {
    'plan_odoo_monthly': (499, 'Odoo Monthly'),
    'plan_odoo_yearly': (4990, 'Odoo Yearly'),
    'plan_n8n_monthly': (199, 'n8n Monthly'),
    'plan_n8n_yearly': (1990, 'n8n Yearly'),
    'plan_smarthome_monthly': (100, 'Smart Home Monthly'),
    'plan_smarthome_yearly': (1000, 'Smart Home Yearly'),
    'plan_security_access_entry_monthly': (100, 'Security Access Entry Monthly'),
    'plan_security_access_entry_yearly': (1000, 'Security Access Entry Yearly'),
    'plan_security_access_pro_monthly': (300, 'Security Access Pro Monthly'),
    'plan_security_access_pro_yearly': (3000, 'Security Access Pro Yearly'),
    'plan_mcp_odoo_monthly': (499, 'Odoo MCP Server Monthly'),
    'plan_mcp_odoo_yearly': (4990, 'Odoo MCP Server Yearly'),
    'plan_mcp_n8n_monthly': (499, 'n8n MCP Server Monthly'),
    'plan_mcp_n8n_yearly': (4990, 'n8n MCP Server Yearly'),
    'plan_mcp_hermes_monthly': (499, 'Hermes MCP Server Monthly'),
    'plan_mcp_hermes_yearly': (4990, 'Hermes MCP Server Yearly'),
}

# .51 DB 的既有 20 筆（migration D4 backfill 的作用集）
PRE_EXISTING_20 = list(OLD_PLANS_51) + list(PLACEHOLDER_PLANS)

# .51 DB 的 template M2M（升級前舊白名單；sticky noupdate 情境）
OLD_TEMPLATE_PLANS_51 = {
    'cloud_app_odoo': ['plan_odoo_monthly', 'plan_odoo_yearly'],
    'cloud_app_n8n': ['plan_n8n_monthly', 'plan_n8n_yearly'],
    'cloud_app_mcp_hermes': ['plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly'],
    'smart_home_home_assistant': ['plan_smarthome_monthly', 'plan_smarthome_yearly'],
}


def _migrate_fn():
    # migration 目錄不是 python package，比照 test_security_access_p2_5_migration
    # 以檔案路徑載入。
    import importlib.util
    import os
    here = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(here, 'migrations', '18.0.1.0.52', 'post-migrate.py')
    spec = importlib.util.spec_from_file_location('p330_post_migrate', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


class TestPricingMigration330(TransactionCase):
    """G3：模擬 .51→.52 升級——fixture 重建升級前 DB shape（舊價/舊名/
    routes=3/service_type 空/舊 M2M），驅動 migrate()，斷言 D1–D4＋E2。"""

    def setUp(self):
        super().setUp()
        self.migrate = _migrate_fn()

    def _revert_to_51_shape(self):
        """把 fresh-seed DB 倒回 .51 升級前狀態（xmlid 定位，同 R8 鐵律）。"""
        for sfx, (price, name) in OLD_PLANS_51.items():
            _plan(self.env, sfx).write(
                {'sh_plan_price': price, 'name': name})
            _product(self.env, sfx).write(
                {'list_price': price, 'name': name + ' Subscription'})
        for sfx in ('plan_security_access_entry_monthly',
                    'plan_security_access_entry_yearly'):
            _plan(self.env, sfx).sa_max_routes = 3
        for sfx in PRE_EXISTING_20:
            _plan(self.env, sfx).service_type = False
        for tpl_sfx, plan_sfxs in OLD_TEMPLATE_PLANS_51.items():
            template = self.env.ref('%s.%s' % (MOD, tpl_sfx))
            template.subscription_plan_ids = [
                (6, 0, [_plan(self.env, s).id for s in plan_sfxs])]

    def _assert_target_state(self):
        # D1 reprice＋D2 rename（16 plan＋16 product；xmlid 逐筆）
        # 目標＝.52 凍結值（MIG_52_TARGET），與 fresh .59 EXPECTED_PLANS 解耦。
        for sfx in OLD_PLANS_51:
            price, name = MIG_52_TARGET[sfx]
            plan = _plan(self.env, sfx)
            self.assertEqual(plan.sh_plan_price, price, sfx)
            self.assertEqual(plan.name, name, sfx)
            product = _product(self.env, sfx)
            self.assertEqual(product.list_price, price, sfx)
            self.assertEqual(product.name, name + ' Subscription', sfx)
        # D3 quota
        for sfx in ('plan_security_access_entry_monthly',
                    'plan_security_access_entry_yearly'):
            self.assertEqual(_plan(self.env, sfx).sa_max_routes, 5, sfx)
        for sfx in ('plan_security_access_pro_monthly',
                    'plan_security_access_pro_yearly'):
            self.assertEqual(_plan(self.env, sfx).sa_max_routes, 10, sfx)
        # D4 backfill（20 筆；anythingllm orphan 寫入成功＝guard 放行實證）
        for sfx in PRE_EXISTING_20:
            self.assertEqual(
                _plan(self.env, sfx).service_type,
                EXPECTED_SERVICE_TYPE[sfx], sfx)
        # E2 M2M（四個變動 template 恢復 .52-era 全量 6/6/6/4；用凍結集合，
        # 因 fresh cloud_app_n8n 已含 Wave B t4，.52 migration 不產出 t4）
        for tpl_sfx in OLD_TEMPLATE_PLANS_51:
            template = self.env.ref('%s.%s' % (MOD, tpl_sfx))
            expected_ids = {
                _plan(self.env, s).id
                for s in MIG_52_TEMPLATE_PLANS[tpl_sfx]}
            self.assertEqual(
                set(template.subscription_plan_ids.ids), expected_ids, tpl_sfx)
        # G5：placeholder 價格/名稱不受 migration 影響
        for sfx, (price, name) in PLACEHOLDER_PLANS.items():
            plan = _plan(self.env, sfx)
            self.assertEqual(plan.sh_plan_price, price, sfx)
            self.assertEqual(plan.name, name, sfx)

    def test_migration_upgrades_51_shape_to_target(self):
        self._revert_to_51_shape()
        self.migrate(self.env.cr, '18.0.1.0.51')
        self._assert_target_state()

    def test_migration_idempotent(self):
        self._revert_to_51_shape()
        self.migrate(self.env.cr, '18.0.1.0.51')
        self.migrate(self.env.cr, '18.0.1.0.51')
        self._assert_target_state()

    def test_migration_noop_on_fresh_install(self):
        """version 為空（fresh install 不跑 migration 腳本；防禦性 guard）→
        不動任何值。"""
        self._revert_to_51_shape()
        self.migrate(self.env.cr, None)
        for sfx, (price, name) in OLD_PLANS_51.items():
            plan = _plan(self.env, sfx)
            self.assertEqual(plan.sh_plan_price, price, sfx)
            self.assertEqual(plan.name, name, sfx)
