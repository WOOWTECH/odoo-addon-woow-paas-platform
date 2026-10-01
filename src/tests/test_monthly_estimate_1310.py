"""#1310：工作區「預估費用」原本寫死 $0/month；改由訂閱資訊的 monthly_estimate 加總。

monthly_estimate＝含稅單期總額依計費週期換算成每月（年繳 ÷12）。
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase, tagged

from ..models.subscription_lifecycle_mixin import _monthly_estimate


@tagged('post_install', '-at_install')
class TestMonthlyEstimate1310(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Monthly Estimate 1310'})
        self.tax_5 = self.env['account.tax'].create({
            'name': 'Monthly Estimate Tax 5%',
            'amount_type': 'percent',
            'amount': 5.0,
            'type_tax_use': 'sale',
            'price_include_override': 'tax_excluded',
        })

    def _sub(self, price, unit, recurrency=1, taxes=None):
        plan = self.env['sh.subscription.plan'].create({
            'name': 'Monthly Estimate %s %s' % (unit, price),
            'sh_duration': 1,
            'sh_unit': unit,
            'sh_plan_price': price,
            'sh_no_of_billing_cycle': -1,
        })
        return self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': plan.id,
            'sh_plan_price': price,
            'sh_recurrency': recurrency,
            'sh_unit': unit,
            'sh_start_date': date.today(),
            'sh_date_of_next_payment': date.today() + timedelta(days=30),
            'sh_taxes_ids': [(6, 0, taxes.ids if taxes else [])],
            'state': 'in_progress',
        })

    def test_monthly_plan_is_its_own_price(self):
        self.assertAlmostEqual(_monthly_estimate(self._sub(4500.0, 'month')), 4500.0, places=2)

    def test_yearly_plan_is_divided_by_twelve(self):
        self.assertAlmostEqual(_monthly_estimate(self._sub(12000.0, 'year')), 1000.0, places=2)

    def test_tax_is_included(self):
        self.assertAlmostEqual(
            _monthly_estimate(self._sub(1500.0, 'month', taxes=self.tax_5)), 1575.0, places=2)

    def test_free_plan_is_zero(self):
        self.assertEqual(_monthly_estimate(self._sub(0.0, 'month')), 0.0)
