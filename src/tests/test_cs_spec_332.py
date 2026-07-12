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
