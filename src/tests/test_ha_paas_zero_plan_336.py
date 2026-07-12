# -*- coding: utf-8 -*-
"""#336：HA PaaS 0 元 plan seed＋0 元計費路徑坐實。"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHaPaasZeroPlanSeed(TransactionCase):

    def test_seed_fields(self):
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.assertEqual(plan.sh_plan_price, 0)
        self.assertEqual(plan.service_type, 'cloud_service')
        self.assertTrue(plan.storefront_hidden)
        self.assertFalse(plan.sh_trial, 'HA PaaS 關 trial（#336 拍板）')
        self.assertTrue(plan.sh_never_expire)
        self.assertEqual(plan.sh_no_of_billing_cycle, -1)
        self.assertEqual(plan.sh_unit, 'month')

    def test_seed_product(self):
        product = self.env.ref('woow_paas_platform.product_ha_paas_monthly')
        self.assertEqual(product.list_price, 0)
        self.assertEqual(
            product.sh_subscription_plan_id,
            self.env.ref('woow_paas_platform.plan_ha_paas_monthly'))

    def test_orphan_no_template_links(self):
        """未掛任何 template M2M（HA template 等 Elmo image）。"""
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        linked = self.env['woow_paas_platform.cloud_app_template'].sudo().search_count(
            [('subscription_plan_ids', 'in', plan.id)])
        self.assertEqual(linked, 0)

    def test_invisible_even_if_linked(self):
        """可見性契約閉環：未來掛上 HA template 後、storefront_hidden 仍擋前台。

        模擬：把 HA PaaS plan 掛上任一 template，斷言過濾語意仍排除它
        （縱深——orphan 與 hidden 兩道各自獨立成立）。
        """
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        tpl = self.env['woow_paas_platform.cloud_app_template'].sudo().search([], limit=1)
        tpl.write({'subscription_plan_ids': [(4, plan.id)]})
        visible = tpl.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden)
        self.assertNotIn(plan, visible)
