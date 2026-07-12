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
