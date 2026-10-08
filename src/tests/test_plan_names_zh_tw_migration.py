"""方案名稱中文化 migration（18.0.1.0.214）：`_woow_migrate_plan_names_zh_tw`。

守的四件事：
1. 舊英文原名的方案與商品都改成新中文名（xmlid 定位）。
2. 營運在後台手改過名的方案／商品不覆蓋。
3. 冪等：連跑兩次結果相同、第二次回 (0, 0)。
4. 新裝 seed 的名稱＝對照表的新名（seed 與 migration 雙軌一致）。
5. AC 席數變體：舊格式「+Naccess月繳」／「MCP Server」改成「+N 存取席位 月繳」／「MCP 伺服器」，
   且新裝 seed 已無舊格式（generator display_name() 與 migration 同規則）。
"""
import re

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

    def test_variants_renamed_to_seat_wording(self):
        cases = {
            'plan_mcp_hermes_ac1_monthly': (
                'Hermes MCP Server T1（1–3 人）+1access月繳',
                'Hermes MCP 伺服器 T1（1–3 人）+1 存取席位 月繳'),
            'plan_smarthome_ac2_yearly': (
                '智慧家庭連結訂閱服務（方案一：對外網址）+2access年繳',
                '智慧家庭連結訂閱服務（方案一：對外網址）+2 存取席位 年繳'),
        }
        for xmlid, (old, new) in cases.items():
            plan = self.env.ref('%s.%s' % (MOD, xmlid))
            plan.write({'name': old})
            tmpl = self.env.ref('%s.%s' % (MOD, xmlid.replace('plan_', 'product_', 1))).product_tmpl_id
            tmpl.with_context(lang='en_US').write({'name': old + ' Subscription'})
        self.Plan._woow_migrate_plan_names_zh_tw()
        for xmlid, (_old, new) in cases.items():
            self.assertEqual(self.env.ref('%s.%s' % (MOD, xmlid)).name, new, xmlid)
            tmpl = self.env.ref('%s.%s' % (MOD, xmlid.replace('plan_', 'product_', 1))).product_tmpl_id
            self.assertEqual(tmpl.with_context(lang='en_US').name, new + ' Subscription', xmlid)
        self.assertEqual(self.Plan._woow_migrate_plan_names_zh_tw(), (0, 0))

    def test_fresh_seed_has_no_old_variant_wording(self):
        old = re.compile(r'\+\d+access(月|年)繳|MCP Server')
        imd = self.env['ir.model.data'].search([('module', '=', MOD), ('model', '=', 'sh.subscription.plan')])
        bad = [p.name for p in self.Plan.browse(imd.mapped('res_id')).exists() if old.search(p.name or '')]
        self.assertFalse(bad, bad[:5])
