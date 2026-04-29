"""Tests for the subscription → cycle invoice ECPay handoff.

Verifies that when `_sh_create_auto_invoice` builds a new cycle invoice for a
subscription, the partner's stored carrier preference lands on the move and
the move is marked `woow_invoice_issue_state='pending'` so the issuing cron
will pick it up. Reuse-of-existing-invoice behaviour is already covered in
test_subscription_failure_handling — here we only exercise the new-invoice
path.
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestEcpayInvoiceSubscription(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']
        cls.Move = cls.env['account.move']

    def _make_partner_with_carrier(self, carrier_type='mobile', **kwargs):
        return self.env['res.partner'].create({
            'name': 'SubInvUser',
            'wallet_balance': 1000.0,
            'woow_invoice_carrier_type': carrier_type,
            **kwargs,
        })

    def _make_sub(self, partner, price=100.0):
        today = date.today()
        return self.Sub.sudo().create({
            'sh_partner_id': partner.id,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'sh_plan_price': price,
            'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress',
            'sh_trial': False,
        })

    def test_cycle_invoice_inherits_partner_mobile_carrier(self):
        partner = self._make_partner_with_carrier(
            carrier_type='mobile',
            woow_invoice_carrier='/SUB+111',
        )
        sub = self._make_sub(partner)
        invoice, is_new = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertTrue(is_new)
        self.assertEqual(invoice.carrierType, '3')
        self.assertEqual(invoice.input_carrier_num, '/SUB+111')

    def test_cycle_invoice_inherits_partner_triplicate(self):
        partner = self._make_partner_with_carrier(
            carrier_type='triplicate',
            woow_invoice_tax_id='12345678',
            woow_invoice_title='Sub Co Ltd',
        )
        sub = self._make_sub(partner)
        invoice, _ = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertEqual(invoice.ecpay_CustomerIdentifier, '12345678')
        self.assertEqual(invoice.ec_ident_name, 'Sub Co Ltd')

    def test_cycle_invoice_marked_pending(self):
        partner = self._make_partner_with_carrier()
        sub = self._make_sub(partner)
        invoice, _ = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertEqual(invoice.woow_invoice_issue_state, 'pending')

    def test_existing_invoice_reuse_does_not_change_state(self):
        partner = self._make_partner_with_carrier()
        sub = self._make_sub(partner)
        first, is_new1 = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertTrue(is_new1)
        # Manually advance state as if cron had already issued.
        first.write({'woow_invoice_issue_state': 'issued'})
        # Reuse path — must return the same record without rewriting state.
        second, is_new2 = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertFalse(is_new2)
        self.assertEqual(first, second)
        self.assertEqual(second.woow_invoice_issue_state, 'issued')
