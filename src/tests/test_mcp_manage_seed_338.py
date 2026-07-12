# -*- coding: utf-8 -*-
"""#338：Odoo Manage MCP 上架骨架 seed。"""
import json
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpManageTemplateSeed(TransactionCase):

    def test_template_seed(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertEqual(tpl.category, 'mcp')
        self.assertFalse(tpl.is_active, 'image 未 ready 前必須隱藏（#338）')
        self.assertFalse(tpl.mcp_enabled, 'MCP 內建於 body、非 sidecar（比照三 MCP admin）')
        values = json.loads(tpl.helm_default_values)
        self.assertIn('odoo-manage-mcp-admin', values['image']['repository'])

    def test_description_mentions_prereq(self):
        """租戶側前置說明（API Key／addon）放 description（#338 工作項 3）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertIn('API Key', tpl.description or '')


@tagged('post_install', '-at_install')
class TestMcpManagePlansSeed(TransactionCase):

    EXPECTED = {
        'plan_mcp_odoo_manage_monthly': (3000, 'month'),
        'plan_mcp_odoo_manage_yearly': (24000, 'year'),
    }

    def test_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price)
            self.assertEqual(plan.sh_unit, unit)
            self.assertEqual(plan.service_type, 'cloud_service')

    def test_products_and_m2m(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        plans = tpl.sudo().subscription_plan_ids
        self.assertEqual(len(plans), 2)
        for xmlid in self.EXPECTED:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertIn(plan, plans)
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.sh_subscription_plan_id, plan)
