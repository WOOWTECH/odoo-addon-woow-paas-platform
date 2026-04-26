"""Regression tests for BUG-5: change_plan refund overshoot during trial.

Before the fix, the refund formula
``price * (sh_end_date - today) / (sh_end_date - sh_start_date)``
overshot when ``today < sh_start_date`` (trial period not yet over).
With a 7-day trial and a 30-day cycle, ``remaining_days (29)`` exceeded
``total_days (22)``, producing refund = 1.318 × price for a customer
who had not paid anything yet.

The fix lives on ``sh.subscription.subscription._compute_unused_refund``
(``models/sh_subscription_fix.py``); both the Smart Home change-plan
controller and the back-office Change-Plan wizard call it. These tests
exercise the helper directly (the source of truth) plus the wizard's
compute, since the controller path requires an HTTP request fixture.
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestChangePlanRefund(TransactionCase):
    """BUG-5: ``_compute_unused_refund`` returns 0 during trial / pre-start."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env.ref('base.partner_admin')
        cls.Sub = cls.env['sh.subscription.subscription'].sudo()

    def _make_sub(self, start_offset_days, end_offset_days, price=100.0):
        """Build a sub record without going through confirm/onchange.

        Skips the upstream onchange chain (which would generate invoices /
        send mail) so the test stays a pure unit test of the helper.
        """
        today = date.today()
        return self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today + timedelta(days=start_offset_days),
            'sh_end_date': today + timedelta(days=end_offset_days),
            'sh_plan_price': price,
            'sh_unit': 'month',
            'sh_recurrency': 1,
        })

    # --- BUG-5 regressions ----------------------------------------------- #

    def test_refund_zero_during_trial_period(self):
        """today < sh_start_date — customer has not paid → refund = 0."""
        sub = self._make_sub(start_offset_days=7, end_offset_days=29, price=100.0)
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_refund_zero_when_today_equals_start_date(self):
        """today == sh_start_date — paid for full cycle, none consumed yet."""
        sub = self._make_sub(start_offset_days=0, end_offset_days=29, price=100.0)
        # remaining = total = 29, refund = price.
        self.assertEqual(sub._compute_unused_refund(), 100.0)

    # --- happy path ------------------------------------------------------ #

    def test_refund_prorated_mid_paid_cycle(self):
        """Half-way through a paid cycle → ~half refund."""
        sub = self._make_sub(start_offset_days=-15, end_offset_days=15, price=100.0)
        # total=30, remaining=15 → 50.0
        self.assertEqual(sub._compute_unused_refund(), 50.0)

    def test_refund_zero_at_end_of_cycle(self):
        """today == sh_end_date — fully consumed → refund = 0."""
        sub = self._make_sub(start_offset_days=-30, end_offset_days=0, price=100.0)
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_refund_zero_after_end_date(self):
        """Past end_date — clamp to 0, do not go negative."""
        sub = self._make_sub(start_offset_days=-40, end_offset_days=-5, price=100.0)
        self.assertEqual(sub._compute_unused_refund(), 0)

    # --- guards ---------------------------------------------------------- #

    def test_refund_zero_when_dates_missing(self):
        """sh_never_expire plans leave sh_end_date unset → refund = 0."""
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id,
            'sh_start_date': date.today() - timedelta(days=5),
            'sh_end_date': False,
            'sh_plan_price': 100.0,
        })
        self.assertEqual(sub._compute_unused_refund(), 0)

    # --- wizard parity --------------------------------------------------- #

    def test_wizard_refund_uses_helper_during_trial(self):
        """Wizard's ``refund_amount`` mirrors helper output (0 during trial).

        Smoke test that the wizard's compute routes through the same helper
        and so inherits the BUG-5 fix.
        """
        # Use real cloud_service + sub so the wizard's onchange chain works.
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        ws = Workspace.create({
            'name': 'BUG5 ws', 'slug': 'bug5ws',
            'owner_id': self.partner.id,
        })
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().search([], limit=1)
        if not template:
            self.skipTest("No cloud_app_template available in test DB")
        svc = Service.create({
            'name': 'BUG5 svc',
            'workspace_id': ws.id,
            'template_id': template.id,
            'subdomain': 'bug5svc',
            'state': 'pending',
        })
        # Build sub directly with trial dates (today < sh_start_date).
        today = date.today()
        sub = self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_start_date': today + timedelta(days=7),
            'sh_end_date': today + timedelta(days=29),
            'sh_plan_price': 100.0,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'state': 'in_progress',
        })
        svc.subscription_id = sub.id
        plan_other = self.env['sh.subscription.plan'].sudo().search([], limit=1)
        wiz = self.env['woow_paas_platform.change_plan_wizard'].sudo().create({
            'cloud_service_id': svc.id,
            'current_plan_id': plan_other.id,  # placeholder, refund logic uses sub
            'new_plan_id': plan_other.id,
        })
        wiz._compute_refund()
        self.assertEqual(
            wiz.refund_amount, 0,
            "Wizard should not refund during trial (BUG-5)",
        )
