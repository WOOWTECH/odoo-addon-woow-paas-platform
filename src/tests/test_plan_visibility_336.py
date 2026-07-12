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
