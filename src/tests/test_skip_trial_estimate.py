"""Tests for G2-A: Skip Trial Estimate API.

The estimate endpoint is side-effect-free: it returns the breakdown
(untaxed / tax / total) plus wallet sufficiency info for the modal to
render before the user confirms the (irreversible) skip-trial action.

These tests cover:
- Breakdown shape and tax computation
- Wallet sufficiency / shortage flags
- Side-effect freedom (no invoice, no subscription advance)
- Precondition propagation from the underlying _skip_trial mirror
"""
from datetime import date, timedelta

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkipTrialEstimate(TransactionCase):
    """`_skip_trial_estimate` returns the upcoming-invoice breakdown."""

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.CloudService = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Plan = self.env['sh.subscription.plan']
        self.ProductTemplate = self.env['product.template']
        self.Subscription = self.env['sh.subscription.subscription']
        self.Tax = self.env['account.tax']

        self.partner = self.env['res.partner'].create({
            'name': 'Skip Trial Test User',
            'email': 'skip-trial-estimate@test.example',
        })

        self.workspace = self.Workspace.create({
            'name': 'Skip Trial Estimate WS',
        })

        self.template = self.Template.create({
            'name': 'Skip Trial Estimate App',
            'slug': 'skip-trial-estimate-app',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://test/chart',
            'helm_chart_version': '1.0.0',
        })

        # 15% tax matching the production fixture (the BUG that prompted G2-A
        # was 499 pts displayed but 573.85 charged — a 15% sales tax).
        self.tax_15 = self.Tax.sudo().create({
            'name': 'Skip Trial Test Tax 15%',
            'amount_type': 'percent',
            'amount': 15.0,
            'type_tax_use': 'sale',
            'price_include_override': 'tax_excluded',
        })

        # Plan + product wired via sh_subscription convention.
        self.plan = self.Plan.sudo().create({
            'name': 'Skip Trial Estimate Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 499.0,
            'sh_no_of_billing_cycle': -1,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })

        self.product_tmpl = self.ProductTemplate.sudo().create({
            'name': 'Skip Trial Estimate Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'list_price': 499.0,
            'taxes_id': [(6, 0, [self.tax_15.id])],
        })

    def _credit_wallet(self, amount):
        """Credit the wallet via the official loyalty-card-backed API."""
        self.partner.sudo().add_wallet_points(
            amount, description='Skip trial estimate test credit',
        )
        self.partner.invalidate_recordset(['wallet_balance'])

    def _make_service_in_trial(self):
        """Build a cloud_service whose subscription is in trial with the 15%-tax plan.

        Bypasses the lifecycle helper because we want a controllable fixture:
        a sub in `state='in_progress'` with `sh_trial=True`, sh_plan_price=499,
        and sh_taxes_ids = [tax_15].
        """
        product = self.product_tmpl.product_variant_id
        today = date.today()
        sub = self.Subscription.sudo().create({
            'sh_partner_id': self.partner.id,
            'product_id': product.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 499.0,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
            'sh_trial_end_date': today + timedelta(days=6),
            'sh_start_date': today + timedelta(days=7),
            'sh_date_of_next_payment': today + timedelta(days=7),
            'sh_taxes_ids': [(6, 0, [self.tax_15.id])],
            'state': 'in_progress',
        })
        service = self.CloudService.sudo().create({
            'name': 'Skip Trial Estimate Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subscription_id': sub.id,
        })
        return service, sub

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_estimate_returns_breakdown_with_tax(self):
        """Sufficient wallet → breakdown carries 15% tax = 74.85 + total = 573.85."""
        service, sub = self._make_service_in_trial()
        self._credit_wallet(1000.0)

        result = service._skip_trial_estimate()

        self.assertEqual(result['untaxed'], 499.0)
        self.assertAlmostEqual(result['tax_rate'], 0.15, places=4)
        self.assertAlmostEqual(result['tax_amount'], 74.85, places=2)
        self.assertAlmostEqual(result['total'], 573.85, places=2)
        self.assertEqual(result['currency'], 'pts')
        self.assertTrue(result['sufficient'])
        self.assertEqual(result['shortage'], 0.0)
        # billing_cycle_start must be today; end must be +1 month.
        self.assertEqual(result['billing_cycle_start'], date.today().isoformat())

    def test_estimate_flags_insufficient_balance(self):
        """Wallet < total → sufficient=False, shortage = total - wallet."""
        service, sub = self._make_service_in_trial()
        self._credit_wallet(100.0)

        result = service._skip_trial_estimate()

        self.assertFalse(result['sufficient'])
        # 573.85 - 100 = 473.85
        self.assertAlmostEqual(result['shortage'], 473.85, places=2)
        self.assertAlmostEqual(result['wallet_balance'], 100.0, places=2)

    def test_estimate_does_not_create_invoice(self):
        """Estimate is side-effect-free: no account.move record gets created."""
        service, sub = self._make_service_in_trial()
        self._credit_wallet(1000.0)
        AccountMove = self.env['account.move']
        before_count = AccountMove.sudo().search_count(
            [('partner_id', '=', self.partner.id)],
        )

        service._skip_trial_estimate()

        after_count = AccountMove.sudo().search_count(
            [('partner_id', '=', self.partner.id)],
        )
        self.assertEqual(before_count, after_count)

    def test_estimate_does_not_advance_subscription(self):
        """Estimate must not flip sh_trial or shift sh_date_of_next_payment."""
        service, sub = self._make_service_in_trial()
        self._credit_wallet(1000.0)
        before_trial = sub.sh_trial
        before_next = sub.sh_date_of_next_payment

        service._skip_trial_estimate()

        sub.invalidate_recordset()
        self.assertEqual(sub.sh_trial, before_trial)
        self.assertEqual(sub.sh_date_of_next_payment, before_next)

    def test_estimate_raises_when_not_in_trial(self):
        """Same precondition as _skip_trial: UserError when sh_trial is False."""
        service, sub = self._make_service_in_trial()
        self._credit_wallet(1000.0)
        sub.sudo().write({'sh_trial': False})

        with self.assertRaises(UserError):
            service._skip_trial_estimate()
