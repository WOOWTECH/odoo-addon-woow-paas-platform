# -*- coding: utf-8 -*-
"""#336：plan 前台隱藏旗標——storefront_hidden 欄位與 API 過濾。

#431 起本檔另承載 Hermes 重新上架的斷言（原 TestHermesArchived → 反轉為
TestHermesRelisted，並保留 .54 hide 軌的歷史迴歸）。
"""
import importlib.util
import os

from odoo.tests.common import HttpCase, TransactionCase, tagged

_MIGRATION_79_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', 'migrations', '18.0.1.0.79', 'post-migrate.py',
    )
)


def _load_migrate_79():
    """載入 18.0.1.0.79/post-migrate.py 的 migrate()；檔案缺席回 None。

    鏡射 test_mcp_sidecar_url_invariant_migration.py 的載入慣例——直接跑真
    migration 檔而非只呼叫 model method，這樣「seed 改了但忘了補升級軌」會被
    測到（#431 雙軌缺一不可）。
    """
    if not os.path.exists(_MIGRATION_79_PATH):
        return None
    spec = importlib.util.spec_from_file_location(
        'hermes_relist_migration_18_0_1_0_79', _MIGRATION_79_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


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


@tagged('post_install', '-at_install')
class TestPlansApiFiltersHidden(TransactionCase):
    """三個 plans API 一律排除 storefront_hidden。"""

    def _hidden_plan_ids_from(self, plans_payload):
        return {p['id'] for p in plans_payload}

    def test_template_plans_helper_excludes_hidden(self):
        """過濾語意本身：`filtered(lambda p: not p.storefront_hidden)` 的行為。

        ⚠ 這條**不**證明任何 controller 有用這個語意——它把 lambda 寫在測試自己
        身上，等於在驗 Odoo 的 `filtered()`。三個端點真的有濾，由
        TestPlansApiExcludesHiddenWire 在 wire level 斷言（原本掛在這裡的
        `test_three_controllers_filter_hidden` 是子字串守衛，可被註解餵飽，已移除）。
        """
        template = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        all_plans = template.sudo().subscription_plan_ids
        self.assertTrue(all_plans, '前置：Hermes template 應有掛 plans')
        # 註：本測試只驗 filtered 語意本身，與 seed 當下的可見狀態無關（#336 A5
        # 曾全數封存、#431 又全數解封），故先把 fixture 重置為全可見的已知初始態
        # （TransactionCase 於測試結束會 rollback，不落 DB）。
        all_plans.storefront_hidden = False
        one = all_plans[0]
        one.storefront_hidden = True
        visible = all_plans.filtered(lambda p: not p.storefront_hidden)
        self.assertNotIn(one, visible)
        self.assertEqual(len(visible), len(all_plans) - 1)


@tagged('post_install', '-at_install')
class TestPlansApiExcludesHiddenWire(HttpCase):
    """三個前台 plans 端點：隱藏 plan 不得回傳、可見 plan 必須回傳（wire-level）。

    取代原本的 `inspect.getsource(mod)` + `assertIn('storefront_hidden', src)`
    子字串守衛——**那條可以被註解餵飽**：把 `filtered(lambda p: not
    p.storefront_hidden)` 整段刪掉、只留 `# #336：前台 plans 一律排除
    storefront_hidden` 這行註解，測試照樣全綠。

    2026-08-02 實測（三個 controller 全刪過濾、註解留著）：舊守衛綠，全套 2191
    tests 只紅 3 條，且**那 3 條全部只打 cloud-services 端點**
    （test_plan_group_keys_368 / test_wave_c_variants_attach_330 /
    test_marketplace_price_label_478 各一條，都是順帶打到而非專測過濾）。只注入
    smart-home ＋ security-access 兩處時全套 0F/0E——那兩個端點的過濾在行為層
    是**零覆蓋**。

    🔴 每條測試都同時斷言「可見 plan 有在回傳裡」。少了它，端點回空集合
    （template 被擋掉、plans 沒掛上、jsonrpc 形狀變了）一樣會讓 assertNotIn
    恆真——那只是把「註解可餵飽」換成「空集合恆綠」，等於沒修。

    ⚠ 用合成 fixture 而非 seed 商品：seed 的 `storefront_hidden` 是會變動的
    商業狀態（#336 A5 曾全數隱藏、#431 又全數解封），錨在上面會讓這組測試在
    過濾完全正確的情況下變紅（#478／#494 教訓）。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')

    def _make_plans(self, service_type):
        """一組對照 plan：可見 + 隱藏（皆非席數變體，ac_max_access_seats=0）。"""
        Plan = self.env['sh.subscription.plan']
        common = {
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'service_type': service_type,
        }
        visible = Plan.create(dict(
            common, name='Visible 336 %s' % service_type,
            sh_plan_price=1000, storefront_hidden=False))
        hidden = Plan.create(dict(
            common, name='Hidden 336 %s' % service_type,
            sh_plan_price=2000, storefront_hidden=True))
        return visible, hidden

    def _result(self, url):
        resp = self.url_open(
            url, data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'})
        body = resp.json()
        self.assertIn('result', body, 'resp: %s' % body)
        return body['result']

    def _assert_hidden_excluded(self, url, visible, hidden):
        r = self._result(url)
        self.assertTrue(r.get('success'), '端點應回成功（回 %r）' % r)
        ids = [row['id'] for row in r['data']]

        # 陽性對照（**必須**與否定斷言同在一條測試裡）
        self.assertIn(
            visible.id, ids,
            '可見 plan 不在回傳裡——端點根本沒吐出這個 template 的 plans，'
            '下面的「隱藏 plan 不在」就只是空集合恆綠')
        # 核心：隱藏 plan 不得外洩到前台選購清單
        self.assertNotIn(
            hidden.id, ids,
            'storefront_hidden 的 plan 出現在前台 plans 回傳裡（%s）' % url)
        # 合成 template 只掛這兩筆 ⇒ 可直接鎖死回傳集合
        self.assertEqual(
            set(ids), {visible.id},
            '回傳集合應恰為那一筆可見 plan（實得 %s）' % ids)

    def test_cloud_services_endpoint_excludes_hidden(self):
        visible, hidden = self._make_plans('cloud_service')
        tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'CS Hidden Fixture 336', 'slug': 'cs-hidden-336',
            # helm_* 三欄 required=True（NOT NULL），不給會撞 NotNullViolation
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/cs-hidden-336',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False,
            'subscription_plan_ids': [(6, 0, [visible.id, hidden.id])]})
        self.env.flush_all()
        self._assert_hidden_excluded(
            '/api/cloud-services/templates/%s/plans' % tpl.id, visible, hidden)

    def test_smart_home_endpoint_excludes_hidden(self):
        visible, hidden = self._make_plans('smart_home')
        tpl = self.env['woow_paas_platform.smart_home_template'].create({
            'name': 'SH Hidden Fixture 336', 'slug': 'sh-hidden-336',
            'is_active': True,
            'subscription_plan_ids': [(6, 0, [visible.id, hidden.id])]})
        self.env.flush_all()
        self._assert_hidden_excluded(
            '/api/smart-home/templates/%s/plans' % tpl.id, visible, hidden)

    def test_security_access_endpoint_excludes_hidden(self):
        visible, hidden = self._make_plans('security_access')
        tpl = self.env['woow_paas_platform.security_access_template'].create({
            'name': 'SA Hidden Fixture 336', 'slug': 'sa-hidden-336',
            'is_active': True,
            'subscription_plan_ids': [(6, 0, [visible.id, hidden.id])]})
        self.env.flush_all()
        self._assert_hidden_excluded(
            '/api/security-access/templates/%s/plans' % tpl.id, visible, hidden)


HERMES_PLAN_XMLIDS = [
    'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
    'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
    'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
]


# #431：三階 cs_* 規格（同價位對齊 n8n；月繳／年繳同 tier 一致）。
# (cs_vcpu, cs_ram_gb, cs_storage_gb)
HERMES_CS_SPEC_431 = {
    'plan_mcp_hermes_monthly': (1, 2.0, 20),
    'plan_mcp_hermes_yearly': (1, 2.0, 20),
    'plan_mcp_hermes_t2_monthly': (2, 2.0, 30),
    'plan_mcp_hermes_t2_yearly': (2, 2.0, 30),
    'plan_mcp_hermes_t3_monthly': (2, 4.0, 40),
    'plan_mcp_hermes_t3_yearly': (2, 4.0, 40),
}


@tagged('post_install', '-at_install')
class TestHermesRelisted(TransactionCase):
    """#431：Hermes 6 筆重新上架（前台可見、三階規格分明、價格不動）。

    2026-07-10 定價會議決議暫壓上架 → #336 A5 全設 storefront_hidden=True；
    2026-07-25 改判「MCP 可連外部服務、功能上已可販售」→ 解封。本 class 承接
    原 TestHermesArchived 的斷言並反轉；hide 軌的歷史迴歸移到
    TestHermesHideMigration336Regression。
    """

    def _write_pre_79_state(self):
        """把 6 筆重置為 .78 既有 DB 的實際狀態（hidden + cs_* 全 0）。"""
        for xmlid in HERMES_PLAN_XMLIDS:
            self.env.ref(f'woow_paas_platform.{xmlid}').write({
                'storefront_hidden': True,
                'cs_vcpu': 0, 'cs_ram_gb': 0.0, 'cs_storage_gb': 0,
            })

    def test_hermes_seed_visible(self):
        """seed 軌：fresh DB 載入後 6 筆一律前台可見。"""
        for xmlid in HERMES_PLAN_XMLIDS:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertFalse(plan.storefront_hidden, f'{xmlid} 應為前台可見')

    def test_hermes_prices_untouched(self):
        expected = {
            'plan_mcp_hermes_monthly': 1500, 'plan_mcp_hermes_yearly': 15000,
            'plan_mcp_hermes_t2_monthly': 4500, 'plan_mcp_hermes_t2_yearly': 45000,
            'plan_mcp_hermes_t3_monthly': 6000, 'plan_mcp_hermes_t3_yearly': 60000,
        }
        for xmlid, price in expected.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price)

    def test_hermes_seed_cs_specs(self):
        """seed 軌：三階 cs_* 就位（0＝fallback template.min_* 的病灶已解）。"""
        for xmlid, spec in HERMES_CS_SPEC_431.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb), spec, xmlid)

    def test_hermes_tiers_are_distinct(self):
        """#431 病灶迴歸：三階不得同規格（否則付不同價拿一樣的算力）。

        斷言讀 DB 實值而非模組常數——cs_* 全 0 時三階會 fallback 同一組
        template.min_*，這正是 #431 要解的病灶。
        """
        tiers = set()
        for sfx in ('', '_t2', '_t3'):
            monthly = self.env.ref(
                f'woow_paas_platform.plan_mcp_hermes{sfx}_monthly')
            yearly = self.env.ref(
                f'woow_paas_platform.plan_mcp_hermes{sfx}_yearly')
            spec = (monthly.cs_vcpu, monthly.cs_ram_gb, monthly.cs_storage_gb)
            self.assertEqual(
                spec,
                (yearly.cs_vcpu, yearly.cs_ram_gb, yearly.cs_storage_gb),
                f'plan_mcp_hermes{sfx}：月繳／年繳同 tier 規格必須一致')
            tiers.add(spec)
        self.assertEqual(len(tiers), 3, '三階規格必須彼此不同')

    def test_hermes_no_odoo_workers(self):
        """Hermes 非 Odoo：cs_odoo_workers 一律留 0（設了會誤觸 script 替換）。"""
        for xmlid in HERMES_PLAN_XMLIDS:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.cs_odoo_workers, 0, xmlid)

    def test_migration_79_exists(self):
        self.assertIsNotNone(
            _load_migrate_79(),
            'src/migrations/18.0.1.0.79/post-migrate.py 缺席——'
            'subscription_products.xml 是 noupdate=1，既有 DB 只靠升級軌 '
            'force-write，seed 軌單獨改動對 stg／prod 完全無效。')

    def test_migration_79_force_writes_from_78_state(self):
        """升級軌：.78 既有態（hidden + cs_*=0）跑完 migration 後收斂到新語意。"""
        migrate = _load_migrate_79()
        self.assertIsNotNone(migrate)
        self._write_pre_79_state()
        migrate(self.env.cr, '18.0.1.0.78')
        self.env.invalidate_all()
        for xmlid, spec in HERMES_CS_SPEC_431.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertFalse(plan.storefront_hidden, xmlid)
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb), spec, xmlid)

    def test_migration_79_idempotent(self):
        migrate = _load_migrate_79()
        self.assertIsNotNone(migrate)
        self._write_pre_79_state()
        migrate(self.env.cr, '18.0.1.0.78')
        migrate(self.env.cr, '18.0.1.0.78')
        self.env.invalidate_all()
        for xmlid, spec in HERMES_CS_SPEC_431.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertFalse(plan.storefront_hidden, xmlid)
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb), spec, xmlid)


@tagged('post_install', '-at_install')
class TestHermesHideMigration336Regression(TransactionCase):
    """歷史迴歸：.54 的 hide 軌不得因 #431 解封而被移除。

    src/migrations/18.0.1.0.54/post-migrate.py 仍會呼叫
    _woow_migrate_hide_hermes_336()；刪掉它會讓「從 .53 以前升上來」的 DB 在
    升級當下直接炸。.53 → .79 的 DB 依序跑 .54（hide）→ .79（unhide），終態
    必須落在 #431 的新語意。
    """

    def test_hide_method_still_exists_and_idempotent(self):
        Plan = self.env['sh.subscription.plan']
        self.assertTrue(
            hasattr(Plan, '_woow_migrate_hide_hermes_336'),
            '_woow_migrate_hide_hermes_336 被刪除——18.0.1.0.54/post-migrate.py '
            '仍在呼叫它，舊 DB 升級會 AttributeError。')
        Plan._woow_migrate_hide_hermes_336()
        Plan._woow_migrate_hide_hermes_336()
        for xmlid in HERMES_PLAN_XMLIDS:
            self.assertTrue(
                self.env.ref(f'woow_paas_platform.{xmlid}').storefront_hidden)

    def test_54_then_79_sequence_lands_on_relisted(self):
        Plan = self.env['sh.subscription.plan']
        migrate = _load_migrate_79()
        self.assertIsNotNone(migrate)
        Plan._woow_migrate_hide_hermes_336()
        migrate(self.env.cr, '18.0.1.0.53')
        self.env.invalidate_all()
        for xmlid, spec in HERMES_CS_SPEC_431.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertFalse(plan.storefront_hidden, xmlid)
            self.assertEqual(
                (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb), spec, xmlid)
