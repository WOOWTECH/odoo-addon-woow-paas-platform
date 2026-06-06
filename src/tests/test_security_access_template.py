from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestSaPlanQuotaFields(TransactionCase):
    """sh.subscription.plan SA quota fields exist with sane defaults."""

    def test_quota_fields_exist_with_defaults(self):
        plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Quota Test Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
        })
        # 存在性 + 預設值（phase-versioned-test-assertion：不硬編未來會變的快照）
        self.assertGreaterEqual(plan.sa_max_routes, 0)
        self.assertGreaterEqual(plan.sa_max_access_policies, 0)
        self.assertGreaterEqual(plan.sa_max_custom_domains, 0)
        self.assertGreaterEqual(plan.sa_max_email_rules, 0)

    def test_quota_fields_writable(self):
        plan = self.env['sh.subscription.plan'].create({
            'name': 'SA Quota Write Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
            'sa_max_routes': 10,
        })
        self.assertEqual(plan.sa_max_routes, 10)
