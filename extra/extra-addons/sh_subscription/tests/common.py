# -*- coding: utf-8 -*-
from datetime import date, timedelta

from odoo.tests import tagged, TransactionCase


@tagged('subscription', '-at_install', 'post_install')
class SubscriptionTestCommon(TransactionCase):
    """Base test class for sh_subscription module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # === Company settings ===
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id

        # === Journal ===
        cls.journal = cls.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', cls.company.id),
        ], limit=1)
        if not cls.journal:
            cls.journal = cls.env['account.journal'].create({
                'name': 'Test Sales Journal',
                'type': 'sale',
                'code': 'TSALE',
                'company_id': cls.company.id,
            })
        cls.company.sh_journal_id = cls.journal

        # === Subscription Plan ===
        cls.plan_monthly = cls.env['sh.subscription.plan'].create({
            'name': 'Test Monthly Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 500.0,
            'sh_no_of_billing_cycle': 12,
            'sh_start_immediately': True,
            'sh_is_close_by_customer': True,
        })
        cls.plan_no_expire = cls.env['sh.subscription.plan'].create({
            'name': 'Test Unlimited Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 300.0,
            'sh_no_of_billing_cycle': -1,
            'sh_never_expire': True,
            'sh_start_immediately': True,
        })
        cls.plan_trial = cls.env['sh.subscription.plan'].create({
            'name': 'Test Trial Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 800.0,
            'sh_no_of_billing_cycle': 6,
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })

        # === Cancel Reasons ===
        cls.reason_1 = cls.env['sh.subscription.reason'].create({
            'name': 'Too Expensive',
            'sequence': 1,
        })
        cls.reason_2 = cls.env['sh.subscription.reason'].create({
            'name': 'No Longer Needed',
            'sequence': 2,
        })

        # === Product for invoice generation ===
        cls.product = cls.env['product.product'].create({
            'name': 'Test Subscription Product',
            'type': 'service',
            'list_price': 500.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': cls.plan_monthly.id,
        })

        # === Partners ===
        cls.partner_a = cls.env['res.partner'].create({
            'name': 'Test Customer A',
            'email': 'customer.a@test.com',
        })
        cls.partner_b = cls.env['res.partner'].create({
            'name': 'Test Customer B',
            'email': 'customer.b@test.com',
        })

    @classmethod
    def _create_subscription(cls, partner=None, plan=None, state='draft', **kwargs):
        """Helper to create a subscription with defaults."""
        vals = {
            'sh_partner_id': (partner or cls.partner_a).id,
            'sh_subscription_plan_id': (plan or cls.plan_monthly).id,
            'sh_plan_price': (plan or cls.plan_monthly).sh_plan_price,
            'sh_recurrency': (plan or cls.plan_monthly).sh_duration,
            'sh_unit': (plan or cls.plan_monthly).sh_unit,
            'sh_no_of_billing_cycle': (plan or cls.plan_monthly).sh_no_of_billing_cycle,
            'sh_start_date': date.today(),
            'sh_date_of_next_payment': date.today(),
            'product_id': cls.product.id,
            'sh_qty': 1,
        }
        vals.update(kwargs)
        sub = cls.env['sh.subscription.subscription'].create(vals)
        if state != 'draft':
            sub.state = state
        return sub
