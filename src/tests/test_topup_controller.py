import json as json_lib
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestTopupController(HttpCase):
    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.provider.write({'state': 'test'})
        self.payment_method = self.env.ref(
            'payment.payment_method_unknown', raise_if_not_found=False,
        )
        if not self.payment_method:
            self.payment_method = self.env['payment.method'].search([], limit=1)
        self.payment_method.sudo().active = True
        self.user = self.env['res.users'].create({
            'name': 'Topup Tester', 'login': 'topup_test', 'password': 'topup_test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.user.partner_id.id,
            'payment_details': '**** 4242',
            'ecpg_bindcard_id': 'STUB_BIND',
            'ecpg_card_expire': '2812',
        })
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'TopupCtl', 'amount': 500, 'credits': 500, 'active': True,
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_payment_enabled', 'True',
        )
        self.authenticate('topup_test', 'topup_test')

    def _post(self, path, payload):
        response = self.url_open(
            path,
            data=json_lib.dumps({'params': payload}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        return response.json()

    def test_happy_path_returns_redirect_url(self):
        stub_tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.user.partner_id.id,
            'amount': 500,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'STUBREF',
        })
        with patch(
            'odoo.addons.woow_paas_platform.models.payment_token.PaymentToken._charge',
            return_value={
                'tx': stub_tx,
                'redirect_url': 'https://ecpay.example/3ds',
                'state': 'pending',
            },
        ):
            result = self._post(
                '/api/billing/wallet/topup', {'plan_id': self.plan.id},
            )['result']
        self.assertTrue(result['success'])
        self.assertIn('topup_id', result['data'])
        self.assertEqual(result['data']['redirect_url'], 'https://ecpay.example/3ds')

    def test_rejects_when_flag_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.topup_payment_enabled', 'False',
        )
        result = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'payment_not_configured')

    def test_rejects_when_no_card(self):
        self.token.active = False
        result = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'no_card')

    def test_rejects_plan_not_found(self):
        result = self._post(
            '/api/billing/wallet/topup', {'plan_id': 99999},
        )['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['code'], 'plan_not_found')

    def test_duplicate_pending_returns_existing_topup(self):
        first_tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.user.partner_id.id,
            'amount': 500,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'FIRSTREF',
        })
        with patch(
            'odoo.addons.woow_paas_platform.models.payment_token.PaymentToken._charge',
            return_value={
                'tx': first_tx,
                'redirect_url': 'https://ecpay.example/first',
                'state': 'pending',
            },
        ):
            first = self._post(
                '/api/billing/wallet/topup', {'plan_id': self.plan.id},
            )['result']
        topup = self.env['woow.wallet.topup'].browse(first['data']['topup_id'])
        topup.state = 'pending'
        second = self._post(
            '/api/billing/wallet/topup', {'plan_id': self.plan.id},
        )['result']
        self.assertTrue(second['success'])
        self.assertEqual(second['data']['topup_id'], first['data']['topup_id'])
        self.assertTrue(second['data'].get('resumed'))
