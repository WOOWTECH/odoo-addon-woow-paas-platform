"""Tests for /api/billing/payment-methods/* wrapper endpoints."""
from unittest.mock import patch

from odoo.tests.common import HttpCase


class TestPaymentMethodsApi(HttpCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not self.provider:
            self.skipTest("ECPay provider not configured in this DB")
        self.provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })
        self.authenticate('admin', 'admin')

    def test_prepare_bind_returns_token(self):
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'Token': 'FAKE_TOKEN_ABC123',
            'TokenExpireDate': '2026/12/31 23:59:59',
        }
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.get_token_by_binding_card',
            return_value=fake_result,
        ) as mock_get_token:
            response = self.url_open(
                '/api/billing/payment-methods/prepare-bind',
                data='{"jsonrpc":"2.0","method":"call","params":{}}',
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['token'], 'FAKE_TOKEN_ABC123')
        self.assertIn('member_id', body['data'])
        self.assertIn('trade_no', body['data'])
        self.assertEqual(body['data']['merchant_id'], '3002607')
        self.assertTrue(body['data']['sandbox'])
        mock_get_token.assert_called_once()
        kwargs = mock_get_token.call_args.kwargs
        self.assertIn('/woow#/settings/payment-methods', kwargs['order_result_url'])
        self.assertIn(f'trade_no={body["data"]["trade_no"]}', kwargs['order_result_url'])

    def test_prepare_bind_no_provider_returns_error(self):
        self.provider.enable_ecpg = False
        response = self.url_open(
            '/api/billing/payment-methods/prepare-bind',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'provider_not_found')

    def test_create_bind_returns_three_d_url(self):
        """create_bind calls CreateBindCard and returns ThreeDURL if 3DS required."""
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'BindCardID': 'BC12345',
            'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/verify?id=xyz'},
        }
        partner = self.env.user.partner_id
        member_id = self.provider._ecpg_build_member_id(partner)
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.create_bind_card',
            return_value=fake_result,
        ) as mock_create:
            response = self.url_open(
                '/api/billing/payment-methods/create-bind',
                data=(
                    '{"jsonrpc":"2.0","method":"call","params":{'
                    f'"member_id":"{member_id}",'
                    '"trade_no":"BIND0001",'
                    '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'
                ),
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        mock_create.assert_called_once()
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['three_d_url'], 'https://3ds.example/verify?id=xyz')
        self.assertEqual(body['data']['bind_card_id'], 'BC12345')

    def test_create_bind_member_id_mismatch(self):
        """Member ID from request must match the expected one for the current user."""
        response = self.url_open(
            '/api/billing/payment-methods/create-bind',
            data=(
                '{"jsonrpc":"2.0","method":"call","params":{'
                '"member_id":"WRONG_MEMBER",'
                '"trade_no":"BIND0001",'
                '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'
            ),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'member_id_mismatch')

    def test_list_cards_returns_payment_tokens(self):
        """List cards reads from payment.token with ecpg_bindcard_id set."""
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.env.user.partner_id.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TEST_1',
            'ecpg_bindcard_id': 'BC_TEST_1',
            'ecpg_member_id': 'M_1',
            'ecpg_card_expire': '1226',
            'ecpg_issuing_bank': 'TEST BANK',
        })
        response = self.url_open(
            '/api/billing/payment-methods',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertTrue(body['success'])
        ids = [c['id'] for c in body['data']]
        self.assertIn(token.id, ids)
        entry = next(c for c in body['data'] if c['id'] == token.id)
        self.assertEqual(entry['last4'], '4242')
        self.assertEqual(entry['expire'], '12/26')  # formatted MM/YY
        self.assertEqual(entry['bank'], 'TEST BANK')

    def test_delete_archives_token(self):
        """Delete archives the payment.token (active=False), triggering ECPay delete via hook."""
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.env.user.partner_id.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TO_DELETE',
            'ecpg_bindcard_id': 'BC_TO_DELETE',
            'ecpg_member_id': 'M_1',
        })
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.delete_member_bind_card',
            return_value={'RtnCode': 1},
        ):
            response = self.url_open(
                f'/api/billing/payment-methods/{token.id}/delete',
                data='{"jsonrpc":"2.0","method":"call","params":{}}',
                headers={'Content-Type': 'application/json'},
            )
        body = response.json()['result']
        self.assertTrue(body['success'])
        token.invalidate_recordset()
        self.assertFalse(token.active)
