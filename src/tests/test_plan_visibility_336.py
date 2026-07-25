# -*- coding: utf-8 -*-
"""#336：plan 前台隱藏旗標——storefront_hidden 欄位與 API 過濾。

#431 起本檔另承載 Hermes 重新上架的斷言（原 TestHermesArchived → 反轉為
TestHermesRelisted，並保留 .54 hide 軌的歷史迴歸）。
"""
import importlib.util
import os

from odoo.tests.common import TransactionCase, tagged

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
        """以 CS 家族驗證：隱藏 plan 不進 API 回傳集合。

        直測 controller 私有邏輯較脆——改為斷言過濾語意本身：
        visible = template.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden)
        並斷言三個 controller 皆改用此語意（見 test_three_controllers_filter_hidden）。
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
