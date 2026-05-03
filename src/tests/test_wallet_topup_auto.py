"""Tests for wallet.topup.create_auto_topup factory method (Path B Phase 4)."""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestCreateAutoTopup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test',
            'email': 'test@example.com',
        })

    def test_create_auto_topup_returns_topup_record(self):
        """create_auto_topup creates a draft topup with source=auto, no plan_id."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.source, 'auto')
        self.assertEqual(topup.state, 'draft')
        self.assertEqual(topup.amount, 50.0)
        self.assertEqual(topup.credits, 50.0)
        self.assertEqual(topup.bonus, 0.0)
        self.assertFalse(topup.plan_id, "auto-topup must NOT pin a plan")
        self.assertEqual(topup.partner_id, self.partner)

    def test_create_auto_topup_inherits_partner_invoice_carrier(self):
        """The new topup carries the partner's invoice carrier preference."""
        # Use the same field names as res_partner_invoice.py
        # (woow_invoice_carrier_type / woow_invoice_carrier are the partner-side fields)
        if 'woow_invoice_carrier_type' in self.partner._fields:
            self.partner.write({
                'woow_invoice_carrier_type': 'mobile',
                'woow_invoice_carrier': '/ABC1234',
            })
        else:
            self.skipTest("Partner invoice carrier fields not present in this environment")

        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.invoice_carrier_type, 'mobile')
        self.assertEqual(topup.invoice_carrier, '/ABC1234')

    def test_create_auto_topup_zero_amount_raises(self):
        """Negative or zero shortfall must be rejected."""
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=0,
            )

    def test_create_auto_topup_negative_amount_raises(self):
        """Negative amount must be rejected (defensive)."""
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=-10,
            )
