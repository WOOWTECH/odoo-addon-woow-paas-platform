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
        self.payment_method = self._ensure_active_payment_method()
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
            'provider_ref': 'STUB_BIND',
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
        # ECPay V4 go-live: resuming a pending top-up charged nothing, so it is
        # NOT a success — the frontend polls the pending top-up instead.
        self.assertFalse(second['success'])
        self.assertEqual(second['code'], 'duplicate_pending')
        self.assertEqual(second['data']['state'], 'pending')
        self.assertEqual(second['data']['topup_id'], first['data']['topup_id'])
        self.assertTrue(second['data'].get('resumed'))

    def test_sync_ecpay_reject_preserves_topup_record(self):
        """Regression: BUG-1.

        When ECPay synchronously rejects a card charge, _send_payment_request
        leaves the payment.transaction in state 'error' (or 'cancel'). The
        controller previously tried to write that raw value back to
        topup.state — but those aren't valid woow.wallet.topup.state values,
        so ValueError rolled back the entire request, including the topup
        INSERT. The fix is to only promote topup.state to 'pending' when the
        tx is still in flight; the _set_done/_set_error overrides handle
        terminal states. This test guards the boundary: even if _charge
        returns state='error', the topup record must persist (so the user
        can see a failure record) and the response must be a clean JSON
        result instead of a 500.
        """
        rejected_tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.user.partner_id.id,
            'amount': 500,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'REJECTED1',
            'state': 'error',
        })
        with patch(
            'odoo.addons.woow_paas_platform.models.payment_token.PaymentToken._charge',
            return_value={
                'tx': rejected_tx,
                'redirect_url': None,
                'state': 'error',
            },
        ):
            result = self._post(
                '/api/billing/wallet/topup', {'plan_id': self.plan.id},
            )['result']

        # Response must be a successful JSON envelope (no 500). Whether
        # success=True or False is implementation detail; what matters is
        # that the topup record survives so we can audit the failure.
        self.assertIn('data', result)
        topup_id = result['data']['topup_id']
        topup = self.env['woow.wallet.topup'].browse(topup_id)
        self.assertTrue(topup.exists(),
                        "Topup record was rolled back by ValueError")
        # State must be one of the valid Selection options — never 'error'.
        self.assertIn(
            topup.state,
            ('draft', 'pending', 'failed'),
            "topup.state was written with a payment.transaction value",
        )

    def _ensure_active_payment_method(self):
        """Return an active payment method usable by the ECPay provider.

        Odoo 18 refuses to activate a payment.method via ORM unless a provider
        already supports it. The ECPay provider is pre-seeded with the 'ecpay'
        method; prefer that. If it's inactive, flip `active` via raw SQL to
        bypass the admin-facing guard (safe in a test db).
        """
        method = self.provider.payment_method_ids[:1]
        if not method:
            method = self.env['payment.method'].search(
                [('code', '=', 'ecpay')], limit=1,
            )
        if not method:
            method = self.env.ref(
                'payment.payment_method_unknown', raise_if_not_found=False,
            ) or self.env['payment.method'].search([], limit=1)
        if method and not method.active:
            self.env.cr.execute(
                "UPDATE payment_method SET active = TRUE WHERE id = %s",
                [method.id],
            )
            method.invalidate_recordset(['active'])
        return method
