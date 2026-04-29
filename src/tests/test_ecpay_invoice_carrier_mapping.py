"""Pure-function tests for the carrier-type → ecpay_invoice_tw field mapping.

These tests exercise `wallet_topup._map_carrier_to_ecpay_vals` and
`res_partner._woow_partner_invoice_vals` against each of the 6 supported
carrier types so a future selection rename or ecpay_invoice_tw field rename
fails loudly instead of silently dropping fields on the issuing payload.
"""
from odoo.tests.common import TransactionCase


class TestEcpayCarrierMappingTopup(TransactionCase):
    """wallet_topup-side mapping (per-topup overrides)."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'CarrierUser'})
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'CMP',
            'amount': 100,
            'credits': 100,
            'bonus': 0,
            'active': True,
        })

    def _make_topup(self, **invoice_kwargs):
        return self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 100,
            'credits': 100,
            'bonus': 0,
            'currency_id': self.env.company.currency_id.id,
            **invoice_kwargs,
        })

    def test_email_maps_to_carrier_type_1(self):
        topup = self._make_topup(invoice_carrier_type='email')
        self.assertEqual(topup._map_carrier_to_ecpay_vals(), {'carrierType': '1'})

    def test_mobile_maps_to_carrier_type_3_with_number(self):
        topup = self._make_topup(
            invoice_carrier_type='mobile',
            invoice_carrier='/ABC+123',
        )
        vals = topup._map_carrier_to_ecpay_vals()
        self.assertEqual(vals['carrierType'], '3')
        self.assertEqual(vals['input_carrier_num'], '/ABC+123')

    def test_citizen_maps_to_carrier_type_2_with_number(self):
        topup = self._make_topup(
            invoice_carrier_type='citizen',
            invoice_carrier='AB12345678901234',
        )
        vals = topup._map_carrier_to_ecpay_vals()
        self.assertEqual(vals['carrierType'], '2')
        self.assertEqual(vals['input_carrier_num'], 'AB12345678901234')

    def test_triplicate_maps_to_business_fields(self):
        topup = self._make_topup(
            invoice_carrier_type='triplicate',
            invoice_tax_id='12345678',
            invoice_title='Test Co Ltd',
        )
        vals = topup._map_carrier_to_ecpay_vals()
        self.assertEqual(vals['ecpay_CustomerIdentifier'], '12345678')
        self.assertEqual(vals['ec_ident_name'], 'Test Co Ltd')
        # Business invoices do not set carrierType.
        self.assertNotIn('carrierType', vals)

    def test_triplicate_falls_back_to_partner_name_when_no_title(self):
        topup = self._make_topup(
            invoice_carrier_type='triplicate',
            invoice_tax_id='12345678',
        )
        vals = topup._map_carrier_to_ecpay_vals()
        self.assertEqual(vals['ec_ident_name'], 'CarrierUser')

    def test_paper_maps_to_is_print(self):
        topup = self._make_topup(invoice_carrier_type='paper')
        vals = topup._map_carrier_to_ecpay_vals()
        self.assertTrue(vals.get('is_print'))
        # Paper is mutually exclusive with carrierType / donation.
        self.assertNotIn('carrierType', vals)
        self.assertNotIn('is_donation', vals)

    def test_donation_maps_with_lovecode(self):
        topup = self._make_topup(
            invoice_carrier_type='donation',
            invoice_lovecode='5959',
        )
        vals = topup._map_carrier_to_ecpay_vals()
        self.assertTrue(vals.get('is_donation'))
        self.assertEqual(vals['lovecode'], '5959')


class TestEcpayCarrierMappingPartner(TransactionCase):
    """res.partner-side mapping (used by subscription cycle invoices)."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'PartnerCarrier'})

    def _set(self, **kwargs):
        self.partner.write(kwargs)
        return self.partner

    def test_partner_email_default(self):
        self.assertEqual(
            self.partner._woow_partner_invoice_vals(),
            {'carrierType': '1'},
        )

    def test_partner_mobile_with_number(self):
        self._set(
            woow_invoice_carrier_type='mobile',
            woow_invoice_carrier='/XYZ+456',
        )
        vals = self.partner._woow_partner_invoice_vals()
        self.assertEqual(vals['carrierType'], '3')
        self.assertEqual(vals['input_carrier_num'], '/XYZ+456')

    def test_partner_triplicate(self):
        self._set(
            woow_invoice_carrier_type='triplicate',
            woow_invoice_tax_id='87654321',
            woow_invoice_title='ABC Corp',
        )
        vals = self.partner._woow_partner_invoice_vals()
        self.assertEqual(vals['ecpay_CustomerIdentifier'], '87654321')
        self.assertEqual(vals['ec_ident_name'], 'ABC Corp')

    def test_partner_donation(self):
        self._set(
            woow_invoice_carrier_type='donation',
            woow_invoice_lovecode='12345',
        )
        vals = self.partner._woow_partner_invoice_vals()
        self.assertTrue(vals['is_donation'])
        self.assertEqual(vals['lovecode'], '12345')
