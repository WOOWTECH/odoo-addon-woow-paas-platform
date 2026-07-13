# -*- coding: utf-8 -*-
"""Wave B base reprice（含 SA 四級 B7 拍板）——新增檔與 reprice 斷言。"""
from odoo.tests.common import TransactionCase, tagged

NEW_PLANS = {
    'plan_n8n_t4_monthly': (7500, 'month'), 'plan_n8n_t4_yearly': (60000, 'year'),
    'plan_security_access_r1_monthly': (200, 'month'),
    'plan_security_access_r1_yearly': (1800, 'year'),
    'plan_security_access_r3_monthly': (600, 'month'),
    'plan_security_access_r3_yearly': (5400, 'year'),
}


@tagged('post_install', '-at_install')
class TestNewTierPlans(TransactionCase):

    def test_new_plans_seeded(self):
        for xmlid, (price, unit) in NEW_PLANS.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price, xmlid)
            self.assertEqual(plan.sh_unit, unit, xmlid)
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.list_price, price, xmlid)

    def test_n8n_t4_spec_and_type(self):
        plan = self.env.ref('woow_paas_platform.plan_n8n_t4_monthly')
        self.assertEqual(plan.service_type, 'cloud_service')
        self.assertEqual(
            (plan.cs_vcpu, plan.cs_ram_gb, plan.cs_storage_gb,
             plan.cs_odoo_workers), (2, 4.0, 40, 0))

    def test_sa_new_tiers_quota(self):
        r1 = self.env.ref('woow_paas_platform.plan_security_access_r1_monthly')
        self.assertEqual(r1.service_type, 'security_access')
        self.assertEqual((r1.sa_max_routes, r1.sa_max_access_policies,
                          r1.sa_max_custom_domains, r1.sa_max_email_rules,
                          r1.sa_max_emails_per_policy), (1, 1, 1, 0, 20))
        r3 = self.env.ref('woow_paas_platform.plan_security_access_r3_monthly')
        self.assertEqual((r3.sa_max_routes, r3.sa_max_access_policies), (3, 3))

    def test_new_plans_linked_to_templates(self):
        n8n_tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        t4 = self.env.ref('woow_paas_platform.plan_n8n_t4_monthly')
        self.assertIn(t4, n8n_tpl.sudo().subscription_plan_ids)

    def test_sa_new_tiers_linked_to_template(self):
        """SA_TEMPLATE_XMLID=security_access_default（Task 0 校驗）：r1/r3 四筆
        皆掛上 SA template（(6,0) 集合已含新兩級）。"""
        sa_tpl = self.env.ref('woow_paas_platform.security_access_default')
        linked = sa_tpl.sudo().subscription_plan_ids
        for xmlid in ('plan_security_access_r1_monthly',
                      'plan_security_access_r1_yearly',
                      'plan_security_access_r3_monthly',
                      'plan_security_access_r3_yearly'):
            self.assertIn(self.env.ref(f'woow_paas_platform.{xmlid}'), linked,
                          xmlid)
