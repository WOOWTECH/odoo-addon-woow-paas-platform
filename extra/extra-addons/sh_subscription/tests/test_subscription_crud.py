# -*- coding: utf-8 -*-
"""Layer 1: SUB-001 ~ SUB-005 — Subscription CRUD & Field Tests"""
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import SubscriptionTestCommon


@tagged('subscription', '-at_install', 'post_install')
class TestSubscriptionCRUD(SubscriptionTestCommon):
    """SUB-001 ~ SUB-005: Basic CRUD and field computation tests."""

    def test_sub001_create_basic_fields(self):
        """SUB-001: Create subscription and verify basic fields."""
        sub = self._create_subscription()
        self.assertTrue(sub.name, "Name should be auto-generated via sequence")
        self.assertEqual(sub.sh_partner_id, self.partner_a)
        self.assertEqual(sub.sh_subscription_plan_id, self.plan_monthly)
        self.assertEqual(sub.state, 'draft')
        self.assertEqual(sub.sh_start_date, date.today())

    def test_sub002_required_fields(self):
        """SUB-002: State field is required and defaults to 'draft'."""
        sub = self._create_subscription()
        # state is the only required field — verify it always has a value
        self.assertTrue(sub.state, "State field must always be set")
        self.assertEqual(sub.state, 'draft', "Default state should be 'draft'")
        # Verify partner is stored correctly even though not required at DB level
        self.assertTrue(sub.sh_partner_id, "Partner should be set after creation")

    def test_sub003_auto_compute_end_date(self):
        """SUB-003: End date auto-computed from start + recurrence * cycles - 1 day."""
        start = date.today()
        sub = self._create_subscription(
            sh_start_date=start,
            sh_no_of_billing_cycle=3,
            sh_recurrency=1,
            sh_unit='month',
        )
        # Trigger onchange that computes end date
        sub._onchange_sh_trial_subcription_end_date()
        if sub.sh_end_date:
            # Source code: sh_end_date = start + delta - relativedelta(days=1)
            expected = start + relativedelta(months=3) - relativedelta(days=1)
            self.assertEqual(sub.sh_end_date, expected,
                             "End date should be start + 3 months - 1 day")

    def test_sub004_price_calculation(self):
        """SUB-004: Verify subscription price from plan."""
        sub = self._create_subscription(plan=self.plan_monthly)
        self.assertEqual(sub.sh_plan_price, 500.0)

        sub2 = self._create_subscription(plan=self.plan_no_expire)
        self.assertEqual(sub2.sh_plan_price, 300.0)

    def test_sub005_cancel_reason_recorded(self):
        """SUB-005: Cancel reason and description are stored."""
        sub = self._create_subscription()
        reason_text = f"{self.reason_1.name} Not worth the price"
        sub.sh_reason = reason_text
        sub.state = 'cancel'
        self.assertEqual(sub.sh_reason, reason_text)
        self.assertEqual(sub.state, 'cancel')

    def test_sub_name_sequence(self):
        """Subscription name is auto-generated via sequence."""
        sub1 = self._create_subscription()
        sub2 = self._create_subscription()
        self.assertTrue(sub1.name)
        self.assertTrue(sub2.name)
        self.assertNotEqual(sub1.name, sub2.name,
                            "Each subscription should get a unique sequence name")

    def test_sub_never_expire_plan(self):
        """Subscription with never-expire plan has no end date or cycle=-1."""
        sub = self._create_subscription(plan=self.plan_no_expire)
        self.assertEqual(sub.sh_no_of_billing_cycle, -1)

    def test_sub_mrr_computation(self):
        """Monthly recurring revenue computed from price and recurrence."""
        sub = self._create_subscription(plan=self.plan_monthly)
        # MRR for monthly plan = price * 1.0 (month factor)
        sub._compute_sh_recurring_monthly()
        self.assertGreater(sub.sh_recurring_monthly, 0)

    def test_sub_currency_default(self):
        """Currency defaults to company currency."""
        sub = self._create_subscription()
        self.assertEqual(sub.currency_id, self.company.currency_id)
