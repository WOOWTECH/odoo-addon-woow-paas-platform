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
