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
             plan.cs_odoo_workers), (2, 4.0, 20, 0), '#1637：n8n T4 磁碟 40 → 20')

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


# ---- Task B2-2：reprice 雙軌權威表（月/年）＋ SA quota bump ＋改名片段 ----
REPRICED = {
    # xmlid: (新月價或年價, 新名稱片段或 None)
    'plan_odoo_monthly': (1500, '1-3'), 'plan_odoo_yearly': (12000, '1-3'),
    'plan_odoo_t2_monthly': (4500, '4-7'), 'plan_odoo_t2_yearly': (36000, '4-7'),
    'plan_odoo_t3_monthly': (7500, '8-11'), 'plan_odoo_t3_yearly': (60000, '8-11'),
    'plan_n8n_monthly': (750, '1 人'), 'plan_n8n_yearly': (6000, '1 人'),
    'plan_n8n_t2_monthly': (2250, '3 人'), 'plan_n8n_t2_yearly': (18000, '3 人'),
    'plan_n8n_t3_monthly': (4500, '6 人'), 'plan_n8n_t3_yearly': (36000, '6 人'),
    'plan_smarthome_yearly': (1200, None),
    'plan_smarthome_mcp_monthly': (300, None), 'plan_smarthome_mcp_yearly': (2400, None),
    'plan_security_access_entry_monthly': (1200, '6 組'),
    'plan_security_access_entry_yearly': (10800, '6 組'),
    'plan_security_access_pro_monthly': (2000, '10 組'),
    'plan_security_access_pro_yearly': (18000, '10 組'),
    'plan_mcp_odoo_yearly': (24000, None), 'plan_mcp_n8n_yearly': (24000, None),
}

UNTOUCHED = {
    'plan_smarthome_monthly': 150, 'plan_ha_paas_monthly': 0,
    'plan_mcp_odoo_manage_monthly': 3000, 'plan_mcp_odoo_manage_yearly': 24000,
    'plan_mcp_odoo_monthly': 3000, 'plan_mcp_n8n_monthly': 3000,
    'plan_mcp_hermes_monthly': 1500, 'plan_mcp_hermes_t3_yearly': 60000,
}


@tagged('post_install', '-at_install')
class TestRepriceB(TransactionCase):

    def test_repriced_values_and_names(self):
        for xmlid, (price, name_part) in REPRICED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price, xmlid)
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.list_price, price, xmlid)
            if name_part:
                self.assertIn(name_part, plan.name, xmlid)

    def test_sa_entry_quota_bumped(self):
        entry = self.env.ref(
            'woow_paas_platform.plan_security_access_entry_monthly')
        self.assertEqual((entry.sa_max_routes, entry.sa_max_access_policies),
                         (6, 6))

    def test_untouched(self):
        for xmlid, price in UNTOUCHED.items():
            self.assertEqual(
                self.env.ref(f'woow_paas_platform.{xmlid}').sh_plan_price,
                price, xmlid)

    def test_migration_idempotent_and_tracks_identical(self):
        """雙軌防漂移＋冪等：fresh XML 值跑 migration 兩次全部不變。"""
        Plan = self.env['sh.subscription.plan']
        keys = list(REPRICED) + list(UNTOUCHED)
        before = {x: self.env.ref(f'woow_paas_platform.{x}').sh_plan_price
                  for x in keys}
        Plan._woow_migrate_reseed_b_330()
        Plan._woow_migrate_reseed_b_330()
        for x in keys:
            self.assertEqual(
                self.env.ref(f'woow_paas_platform.{x}').sh_plan_price,
                before[x], f'{x}：XML 軌與 migration 軌漂移')
