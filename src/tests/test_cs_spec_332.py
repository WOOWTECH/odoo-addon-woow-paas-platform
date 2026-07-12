# -*- coding: utf-8 -*-
"""#332：CS tier→部署規格映射（plan cs_* 欄位＋runtime overlay）。"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCsSpecFields(TransactionCase):

    def test_fields_exist_default_zero(self):
        plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        for f in ('cs_vcpu', 'cs_ram_gb', 'cs_storage_gb', 'cs_odoo_workers'):
            self.assertIn(f, plan._fields)

    def test_workers_require_ram_constraint(self):
        """M4 防呆：workers>0 且 ram=0 的組合被 constraint 擋。"""
        from odoo.exceptions import ValidationError
        plan = self.env.ref('woow_paas_platform.plan_smarthome_monthly')
        with self.assertRaises(ValidationError):
            plan.write({'cs_odoo_workers': 2})


CS_SPEC_EXPECTED = {
    'plan_odoo_monthly': (2, 2.0, 20, 2), 'plan_odoo_yearly': (2, 2.0, 20, 2),
    'plan_odoo_t2_monthly': (2, 4.0, 30, 3), 'plan_odoo_t2_yearly': (2, 4.0, 30, 3),
    'plan_odoo_t3_monthly': (3, 4.0, 40, 4), 'plan_odoo_t3_yearly': (3, 4.0, 40, 4),
    'plan_n8n_monthly': (1, 1.0, 10, 0), 'plan_n8n_yearly': (1, 1.0, 10, 0),
    'plan_n8n_t2_monthly': (1, 2.0, 20, 0), 'plan_n8n_t2_yearly': (1, 2.0, 20, 0),
    'plan_n8n_t3_monthly': (2, 2.0, 30, 0), 'plan_n8n_t3_yearly': (2, 2.0, 30, 0),
}

# 裁定①（2026-07-12 by Eugene）：T1 storage 採 matrix 值（odoo 20 / n8n 10），
# 為刻意 tier 提升（20>10、10>5，不 regress）。
T1_STORAGE_MATRIX = {'plan_odoo_monthly': 20, 'plan_n8n_monthly': 10}


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

    def test_t1_spec_vcpu_ram_match_min_and_storage_matrix(self):
        """T1 規格斷言（裁定① 2026-07-12）：vcpu/ram == template.min_*（兩維相等
        仍成立）；cs_storage_gb == matrix 常數（odoo 20/n8n 10）**且** >=
        template.min_storage_gb（不縮保證，20>10／10>5 為刻意 tier 提升非回歸）。
        """
        for tpl_xmlid, plan_xmlid in (
            ('cloud_app_odoo', 'plan_odoo_monthly'),
            ('cloud_app_n8n', 'plan_n8n_monthly'),
        ):
            tpl = self.env.ref(f'woow_paas_platform.{tpl_xmlid}')
            t1 = self.env.ref(f'woow_paas_platform.{plan_xmlid}')
            self.assertEqual(t1.cs_vcpu, tpl.min_vcpu, tpl_xmlid)
            self.assertEqual(t1.cs_ram_gb, tpl.min_ram_gb, tpl_xmlid)
            self.assertEqual(t1.cs_storage_gb, T1_STORAGE_MATRIX[plan_xmlid],
                             tpl_xmlid)
            self.assertGreaterEqual(
                t1.cs_storage_gb, tpl.min_storage_gb,
                f'{tpl_xmlid}：T1 storage 不得小於 template 現值')

    def test_xml_and_migration_tracks_identical(self):
        """雙軌防漂移：fresh XML 值經 migration delegate 重跑後必須不變。"""
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
