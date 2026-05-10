"""Tests for SmartHomeTemplate model: CRUD, cross-service-type constraint, ACL."""
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError, AccessError


class TestSmartHomeTemplate(TransactionCase):
    """Behavioural tests for woow_paas_platform.smart_home_template."""

    def setUp(self):
        super().setUp()
        self.SHTemplate = self.env['woow_paas_platform.smart_home_template']
        self.CloudTemplate = self.env['woow_paas_platform.cloud_app_template']
        self.Plan = self.env['sh.subscription.plan']

        self.plan_a = self.Plan.create({
            'name': 'Test Plan A',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        self.plan_b = self.Plan.create({
            'name': 'Test Plan B',
            'sh_plan_price': 200.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })

    def test_create_minimal(self):
        """A template needs only a name to be created."""
        tmpl = self.SHTemplate.create({'name': 'HA Test'})
        self.assertEqual(tmpl.name, 'HA Test')
        self.assertTrue(tmpl.is_active)
        self.assertFalse(tmpl.subscription_plan_ids)

    def test_create_with_plans(self):
        """Plans can be linked at creation via M2M."""
        tmpl = self.SHTemplate.create({
            'name': 'HA with Plans',
            'subscription_plan_ids': [(6, 0, [self.plan_a.id])],
        })
        self.assertIn(self.plan_a, tmpl.subscription_plan_ids)

    def test_default_data_loaded(self):
        """The seed XML record (smart_home_home_assistant) is created on -u and
        carries the expected canonical name + slug."""
        tmpl = self.env.ref(
            'woow_paas_platform.smart_home_home_assistant',
            raise_if_not_found=False,
        )
        self.assertTrue(tmpl, "Default Home Assistant template should be loaded")
        self.assertEqual(tmpl.slug, 'home-assistant')
        self.assertTrue(tmpl.is_active)

    def test_cross_service_type_plan_rejected_from_sh_side(self):
        """Linking a plan that is already on a CloudAppTemplate raises.

        Why: products carry service-type-specific names and a plan binds to a
        single product; sharing would mismatch invoices.
        """
        cs_tmpl = self.CloudTemplate.create({
            'name': 'CS Test',
            'slug': 'cs-test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cs',
            'helm_chart_version': '1.0.0',
            'subscription_plan_ids': [(6, 0, [self.plan_a.id])],
        })
        self.assertIn(self.plan_a, cs_tmpl.subscription_plan_ids)

        with self.assertRaises(ValidationError):
            self.SHTemplate.create({
                'name': 'HA Conflict',
                'subscription_plan_ids': [(6, 0, [self.plan_a.id])],
            })

    def test_cross_service_type_plan_rejected_from_cs_side(self):
        """Symmetric check: linking on the cloud-template side hits the
        cross-type guard when the plan is already on a SmartHomeTemplate."""
        sh_tmpl = self.SHTemplate.create({
            'name': 'HA Existing',
            'subscription_plan_ids': [(6, 0, [self.plan_b.id])],
        })
        self.assertIn(self.plan_b, sh_tmpl.subscription_plan_ids)

        with self.assertRaises(ValidationError):
            self.CloudTemplate.create({
                'name': 'CS Conflict',
                'slug': 'cs-conflict',
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': 'cs',
                'helm_chart_version': '1.0.0',
                'subscription_plan_ids': [(6, 0, [self.plan_b.id])],
            })

    def test_cross_service_type_constraint_silent_on_disjoint_plans(self):
        """No conflict when the two service types use different plans."""
        cs_tmpl = self.CloudTemplate.create({
            'name': 'CS Disjoint',
            'slug': 'cs-disjoint',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cs',
            'helm_chart_version': '1.0.0',
            'subscription_plan_ids': [(6, 0, [self.plan_a.id])],
        })
        sh_tmpl = self.SHTemplate.create({
            'name': 'HA Disjoint',
            'subscription_plan_ids': [(6, 0, [self.plan_b.id])],
        })
        self.assertIn(self.plan_a, cs_tmpl.subscription_plan_ids)
        self.assertIn(self.plan_b, sh_tmpl.subscription_plan_ids)
        self.assertNotIn(self.plan_b, cs_tmpl.subscription_plan_ids)
        self.assertNotIn(self.plan_a, sh_tmpl.subscription_plan_ids)

    def test_acl_portal_user_can_read(self):
        """Portal/internal users have read access — they need to see the
        catalog when creating Smart Homes from the OWL frontend."""
        tmpl = self.SHTemplate.create({'name': 'ACL Read'})

        # Use base.group_user (internal user). Portal users sit lower but
        # the same read row applies — see ir.model.access.csv.
        portal_uid = self.env.ref('base.user_demo').id
        tmpl_as_portal = self.SHTemplate.with_user(portal_uid).browse(tmpl.id)
        # Reading should not raise
        self.assertEqual(tmpl_as_portal.name, 'ACL Read')

    def test_acl_portal_user_cannot_write(self):
        """Non-admin users cannot mutate templates."""
        tmpl = self.SHTemplate.create({'name': 'ACL Write'})

        portal_uid = self.env.ref('base.user_demo').id
        tmpl_as_portal = self.SHTemplate.with_user(portal_uid).browse(tmpl.id)
        with self.assertRaises(AccessError):
            tmpl_as_portal.write({'name': 'Hacked'})
