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

    def _setup_ecpg_token(self, bindcard_id):
        """Provision an ECPG-ready provider + token for self.partner and set default.

        Returns: (provider, token) or calls self.skipTest if no ECPay provider exists.
        """
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
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** ' + bindcard_id[-4:],
            'provider_ref': bindcard_id,
            'ecpg_bindcard_id': bindcard_id,
            'ecpg_member_id': str(self.partner.id),
        })
        if 'property_payment_token_id' in self.partner._fields:
            self.partner.with_company(self.partner.company_id or self.env.company) \
                        .sudo().property_payment_token_id = token
        return provider, token

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
        """Regression guard: the auto-invoice flow must call _notify_low_balance.

        Source-level guard against accidental removal. T19 split the cron
        body into helpers (``_on_payment_failure`` is the call site now);
        check that helper instead of the top-level cron entrypoint.
        """
        import inspect
        from odoo.addons.woow_paas_platform.models.sh_subscription_fix import (
            ShSubscriptionFix,
        )
        source = inspect.getsource(ShSubscriptionFix._on_payment_failure)
        self.assertIn(
            '_notify_low_balance',
            source,
            "_on_payment_failure must still call _notify_low_balance "
            "in the insufficient-balance branch.",
        )

    def test_attempt_auto_topup_success_credits_wallet(self):
        """When ECPG charge succeeds, helper credits the wallet and returns (True, 'ok')."""
        from unittest.mock import patch

        provider, token = self._setup_ecpg_token('BC_AUTOTOPUP')

        def _fake_send(self_tx):
            self_tx.state = 'done'
            self_tx.provider_reference = 'FAKE_ECPAY_REF'
            return None

        Subscription = self.env['sh.subscription.subscription']
        before_balance = self.partner.wallet_balance

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send,
        ):
            ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)

        self.assertTrue(ok, f"Expected success, got ({ok}, {msg})")
        self.assertEqual(msg, 'ok')
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.wallet_balance, before_balance + 400.0)

        tx_rows = self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'topup'),
        ], order='id desc', limit=1)
        self.assertTrue(tx_rows, "Expected a wallet.transaction row for auto-topup")
        self.assertEqual(tx_rows.amount, 400.0)

    def test_attempt_auto_topup_returns_no_token_when_partner_has_no_default(self):
        """Partner without a default payment token → (False, 'no_token')."""
        Subscription = self.env['sh.subscription.subscription']
        # self.partner has no property_payment_token_id in setUp.
        ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def test_attempt_auto_topup_returns_3ds_required_when_ecpg_redirects(self):
        """ECPG charge that needs 3DS → (False, '3ds_required'), no wallet change."""
        from unittest.mock import patch

        provider, token = self._setup_ecpg_token('BC_3DS')

        def _fake_send_3ds(self_tx):
            # Simulate ECPG override returning a redirect dict for 3DS.
            return {'redirect_url': 'https://3ds.example/verify'}

        before = self.partner.wallet_balance
        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send_3ds,
        ):
            Subscription = self.env['sh.subscription.subscription']
            ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)

        self.assertFalse(ok)
        self.assertEqual(msg, '3ds_required')
        self.partner.invalidate_recordset()
        self.assertEqual(self.partner.wallet_balance, before, "Wallet must be unchanged on 3DS")

    def test_attempt_auto_topup_returns_failure_state_when_ecpg_errors(self):
        """When _send_payment_request leaves tx.state != 'done', return (False, state)."""
        from unittest.mock import patch

        provider, token = self._setup_ecpg_token('BC_FAIL')

        def _fake_send_error(self_tx):
            self_tx.state = 'error'
            return None

        with patch.object(
            type(self.env['payment.transaction']),
            '_send_payment_request', _fake_send_error,
        ):
            Subscription = self.env['sh.subscription.subscription']
            ok, msg = Subscription._attempt_auto_topup(self.partner, 400.0)

        self.assertFalse(ok)
        self.assertEqual(msg, 'error')

    def test_auto_invoice_attempts_auto_topup_before_giving_up(self):
        """Regression guard: settlement must try _attempt_auto_topup before the
        failure path runs. T19 moved this into _try_settle_invoice."""
        import inspect
        from odoo.addons.woow_paas_platform.models.sh_subscription_fix import ShSubscriptionFix
        source = inspect.getsource(ShSubscriptionFix._try_settle_invoice)
        self.assertIn(
            '_attempt_auto_topup', source,
            "_try_settle_invoice must call _attempt_auto_topup before declaring failure",
        )
        # _handle_invoice_creation must be reachable on BOTH the wallet-cover
        # and the auto-topup-success branches.
        self.assertGreaterEqual(
            source.count('_handle_invoice_creation'), 2,
            "_try_settle_invoice must call _handle_invoice_creation in both "
            "the balance-sufficient path AND the auto-topup-success path",
        )
