"""Tests for low balance notification during subscription auto-invoice."""
from odoo.tests.common import TransactionCase


class TestLowBalanceNotification(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test User',
            'email': 'test@example.com',
            'wallet_balance': 100.0,
        })

    def test_notification_sent_when_insufficient_balance(self):
        """When auto-invoice finds insufficient balance, email must be sent."""
        Subscription = self.env['sh.subscription.subscription']
        partner = self.partner
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Sub fee',
                'quantity': 1,
                'price_unit': 500.0,
            })],
        })

        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        self.assertIsNotNone(
            template,
            "mail_template_low_balance must be defined in data/mail_templates.xml"
        )

        Subscription._notify_low_balance(partner, invoice)

        mails = self.env['mail.mail'].search([
            ('email_to', 'ilike', partner.email),
            ('subject', 'ilike', 'top up'),
        ])
        self.assertTrue(mails, "A top-up notification email should have been queued")

    def test_insufficient_balance_triggers_notify_via_auto_invoice(self):
        """Regression guard: _sh_create_auto_invoice must call _notify_low_balance.

        Guards against regressions where the call site in
        sh_subscription_fix._sh_create_auto_invoice is accidentally removed.
        Uses a source-level assertion because Odoo ORM is hard to mock
        without a full subscription setup.
        """
        import inspect
        from odoo.addons.woow_paas_platform.models.sh_subscription_fix import (
            ShSubscriptionFix,
        )
        source = inspect.getsource(ShSubscriptionFix._sh_create_auto_invoice)
        self.assertIn(
            '_notify_low_balance',
            source,
            "_sh_create_auto_invoice must still call _notify_low_balance "
            "in the insufficient-balance branch."
        )

    def test_attempt_auto_topup_success_credits_wallet(self):
        """When ECPG charge succeeds, helper credits the wallet and returns (True, 'ok')."""
        from unittest.mock import patch

        partner = self.partner
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured in this DB")
        provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })

        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_AUTOTOPUP',
            'ecpg_bindcard_id': 'BC_AUTOTOPUP',
            'ecpg_member_id': str(partner.id),
        })
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(partner.company_id or self.env.company) \
                   .sudo().property_payment_token_id = token

        def _fake_send(self_tx):
            self_tx.state = 'done'
            self_tx.provider_reference = 'FAKE_ECPAY_REF'
            return None

        Subscription = self.env['sh.subscription.subscription']
        before_balance = partner.wallet_balance

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send,
        ):
            ok, msg = Subscription._attempt_auto_topup(partner, 400.0)

        self.assertTrue(ok, f"Expected success, got ({ok}, {msg})")
        self.assertEqual(msg, 'ok')
        partner.invalidate_recordset()
        self.assertEqual(partner.wallet_balance, before_balance + 400.0)

        # Verify a wallet.transaction row was created (add_wallet_points side effect)
        tx_rows = self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'topup'),
        ], order='id desc', limit=1)
        self.assertTrue(tx_rows, "Expected a wallet.transaction row for auto-topup")
        self.assertEqual(tx_rows.amount, 400.0)
