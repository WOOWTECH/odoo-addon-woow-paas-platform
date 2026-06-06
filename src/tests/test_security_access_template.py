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


class TestSecurityAccessTemplate(TransactionCase):
    """Template catalog + 三方 cross-service-type plan 隔離."""

    def _make_plan(self, name):
        return self.env['sh.subscription.plan'].create({
            'name': name,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 100,
            'sh_no_of_billing_cycle': -1,
        })

    def test_create_template_with_plans(self):
        plan = self._make_plan('SA Tmpl Plan A')
        tmpl = self.env['woow_paas_platform.security_access_template'].create({
            'name': 'Security Access',
            'slug': 'security-access',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        self.assertTrue(tmpl.is_active)
        self.assertIn(plan, tmpl.subscription_plan_ids)

    def test_plan_shared_with_smart_home_template_rejected(self):
        plan = self._make_plan('SA Cross SH Plan')
        self.env['woow_paas_platform.smart_home_template'].create({
            'name': 'SH Cross Tmpl',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.security_access_template'].create({
                'name': 'SA Cross Tmpl',
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })

    def test_plan_shared_with_cloud_template_rejected(self):
        plan = self._make_plan('SA Cross CS Plan')
        self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'CS Cross Tmpl',
            'slug': 'cs-cross',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cs',
            'helm_chart_version': '1.0.0',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.security_access_template'].create({
                'name': 'SA Cross Tmpl 2',
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })

    def test_symmetric_smart_home_side_rejects_sa_plan(self):
        """對稱鎖：SA 先佔的 plan，SH template 後綁也要爆。"""
        plan = self._make_plan('SA First Plan')
        self.env['woow_paas_platform.security_access_template'].create({
            'name': 'SA First Tmpl',
            'subscription_plan_ids': [(6, 0, [plan.id])],
        })
        with self.assertRaises(ValidationError):
            self.env['woow_paas_platform.smart_home_template'].create({
                'name': 'SH Late Tmpl',
                'subscription_plan_ids': [(6, 0, [plan.id])],
            })
