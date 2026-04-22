from unittest.mock import patch
from odoo.tests.common import TransactionCase


class TestPaymentTokenCharge(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'CA'})
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.provider.write({'state': 'test'})
        self.payment_method = self.env.ref(
            'payment.payment_method_unknown', raise_if_not_found=False,
        )
        if not self.payment_method:
            self.payment_method = self.env['payment.method'].search([], limit=1)
        self.payment_method.sudo().active = True
        self.token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'payment_details': '**** 4242',
            'ecpg_bindcard_id': 'STUB_BIND_ID',
            'ecpg_card_expire': '2812',
        })

    def test_charge_returns_redirect_when_3ds(self):
        """Helper returns redirect_url when ECPG flow requires 3DS."""
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request',
            return_value={'redirect_url': 'https://ecpay.example/3ds'},
        ):
            result = self.token._charge(
                amount=100,
                reference='TEST1',
                currency_id=self.env.company.currency_id.id,
            )
        self.assertTrue(result['tx'])
        self.assertEqual(result['redirect_url'], 'https://ecpay.example/3ds')
        self.assertIn(result['state'], ('pending', 'draft'))

    def test_charge_wires_purpose_field(self):
        """Caller can attach a correlation object (e.g. a topup order)."""
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self._ensure_stub_plan().id,
            'amount': 100,
            'credits': 100,
            'currency_id': self.env.company.currency_id.id,
        })
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request',
            return_value=None,
        ):
            result = self.token._charge(
                amount=100,
                reference='TEST2',
                currency_id=self.env.company.currency_id.id,
                purpose_field='woow_topup_id',
                purpose_id=topup.id,
            )
        self.assertEqual(result['tx'].woow_topup_id, topup)

    def _ensure_stub_plan(self):
        Plan = self.env['woow_paas_platform.topup_plan']
        existing = Plan.search([('name', '=', 'Stub')], limit=1)
        if existing:
            return existing
        return Plan.create({
            'name': 'Stub', 'amount': 100, 'credits': 100, 'active': True,
        })
