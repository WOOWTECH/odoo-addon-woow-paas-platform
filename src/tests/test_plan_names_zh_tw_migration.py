"""方案名稱中文化 migration（18.0.1.0.214）：`_woow_migrate_plan_names_zh_tw`。

守的四件事：
1. 舊英文原名的方案與商品都改成新中文名（xmlid 定位）。
2. 營運在後台手改過名的方案／商品不覆蓋。
3. 冪等：連跑兩次結果相同、第二次回 (0, 0)。
4. 新裝 seed 的名稱＝對照表的新名（seed 與 migration 雙軌一致）。
"""
from odoo.tests.common import TransactionCase, tagged

MOD = 'woow_paas_platform'


@tagged('post_install', '-at_install')
class TestPlanNamesZhTwMigration(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Plan = self.env['sh.subscription.plan']
        self.table = self.Plan._PLAN_NAMES_ZH_TW

    def _plan(self, sfx):
        return self.env.ref('%s.plan_%s' % (MOD, sfx))

    def _tmpl(self, sfx):
        return self.env.ref('%s.product_%s' % (MOD, sfx)).product_tmpl_id

    def _revert_to_english(self):
        for sfx, (old, _new, pold, _pnew) in self.table.items():
            self._plan(sfx).write({'name': old})
            self._tmpl(sfx).with_context(lang='en_US').write({'name': pold})

    def test_fresh_seed_already_uses_new_names(self):
        for sfx, (_old, new, _pold, pnew) in self.table.items():
            self.assertEqual(self._plan(sfx).name, new, sfx)
            self.assertEqual(self._tmpl(sfx).with_context(lang='en_US').name, pnew, sfx)

    def test_renames_english_plans_and_products(self):
        self._revert_to_english()
        plans, products = self.Plan._woow_migrate_plan_names_zh_tw()
        self.assertEqual((plans, products), (len(self.table), len(self.table)))
        for sfx, (_old, new, _pold, pnew) in self.table.items():
            self.assertEqual(self._plan(sfx).name, new, sfx)
            self.assertEqual(self._tmpl(sfx).with_context(lang='en_US').name, pnew, sfx)

    def test_does_not_overwrite_manual_rename(self):
        self._revert_to_english()
        self._plan('smarthome_monthly').write({'name': '營運自訂名稱'})
        self._tmpl('smarthome_monthly').with_context(lang='en_US').write({'name': '營運自訂商品'})
        self.Plan._woow_migrate_plan_names_zh_tw()
        self.assertEqual(self._plan('smarthome_monthly').name, '營運自訂名稱')
        self.assertEqual(
            self._tmpl('smarthome_monthly').with_context(lang='en_US').name, '營運自訂商品')
        # 其他仍是舊名的照改
        self.assertEqual(self._plan('smarthome_yearly').name, self.table['smarthome_yearly'][1])

    def test_idempotent(self):
        self._revert_to_english()
        self.Plan._woow_migrate_plan_names_zh_tw()
        self.assertEqual(self.Plan._woow_migrate_plan_names_zh_tw(), (0, 0))
        for sfx, (_old, new, _pold, _pnew) in self.table.items():
            self.assertEqual(self._plan(sfx).name, new, sfx)
