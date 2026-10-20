"""PaaS-only sender display; real template rendering/queueing, no SMTP."""
from email.utils import parseaddr
from unittest.mock import patch

from odoo.tests.common import tagged

from .test_billing_mail_auditfix import _BillingMailFixture


@tagged('post_install', '-at_install', 'paas_mail_sender')
class TestPaasMailSender(_BillingMailFixture):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.original_name = '渥屋科技股份有限公司  84006751'
        self.company.with_context(no_vat_validation=True).write({
            'name': self.original_name,
            'vat': '84006751',
            'email': 'platform-sender@example.invalid',
        })
        self.native_sender = self.company.email_formatted
        self.Sub = self.env['sh.subscription.subscription']
        self.partner = self._partner('Sender Recipient', 'en_US')
        self.smtp_guard = self.enterContext(patch.object(
            type(self.env['mail.mail']), 'send',
            side_effect=AssertionError('Sender tests must not send SMTP'),
        ))

    def _assert_clean_sender(self, mail):
        self.assertTrue(mail, 'The native notification must create a mail')
        self.assertEqual(mail.state, 'outgoing')
        self.assertEqual(parseaddr(mail.email_from), (
            '渥屋科技股份有限公司', self.company.email,
        ))
        self.assertEqual(self.company.name, self.original_name)
        self.assertEqual(self.company.partner_id.name, self.original_name)
        self.assertEqual(self.company.vat, '84006751')
        self.assertEqual(self.company.email_formatted, self.native_sender)
        self.smtp_guard.assert_not_called()

    def test_only_whitespace_separated_own_terminal_vat_is_removed(self):
        for name, vat, expected in [
            ('渥屋科技股份有限公司  84006751', '84006751', '渥屋科技股份有限公司'),
            ('公司\t84006751\u3000', '84006751', '公司'),
            ('ACME 84006751', False, 'ACME 84006751'),
            ('ACME 12345678', '84006751', 'ACME 12345678'),
            ('ACME 84006751 Taiwan', '84006751', 'ACME 84006751 Taiwan'),
            ('ACME84006751', '84006751', 'ACME84006751'),
            ('84006751', '84006751', '84006751'),
            ('渥屋科技股份有限公司', '84006751', '渥屋科技股份有限公司'),
        ]:
            with self.subTest(name=name, vat=vat):
                self.company.with_context(no_vat_validation=True).write({
                    'name': name, 'vat': vat,
                })
                self.assertEqual(parseaddr(self.company._woow_paas_mail_sender()), (
                    expected, self.company.email,
                ))
                self.assertEqual(self.company.name, name)
                self.assertEqual(self.company.partner_id.name, name)
                self.assertEqual(self.company.vat, vat)

    def test_empty_email_retains_unconfigured_sender_semantics(self):
        self.company.email = False
        self.assertFalse(self.company._woow_paas_mail_sender())

    def test_low_balance_native_template_queues_clean_sender(self):
        self._assert_clean_sender(self.Sub._notify_low_balance(
            self.partner, self._invoice(self.partner),
        ))

    def test_cancellation_native_template_queues_clean_sender(self):
        subscription = self._subscription(self.partner)
        self._assert_clean_sender(self.Sub._notify_subscription_cancelled(
            subscription, self._invoice(self.partner),
        ))

    def test_billing_hold_native_template_queues_clean_sender(self):
        subscription = self._subscription(self.partner)
        service = self.env['woow_paas_platform.cloud_service'].new({
            'name': 'Synthetic unavailable service',
        })
        self._assert_clean_sender(self.Sub._notify_billing_hold(
            subscription, service,
        ))

    def _sender_company(self, values):
        # Upstream sh_subscription requires a journal before company creation;
        # bootstrap with the fixture's journal, then attach this company's own.
        company = self.env['res.company'].with_context(no_vat_validation=True).create({
            **values, 'sh_journal_id': self.company.sh_journal_id.id,
        })
        company.sh_journal_id = self.env['account.journal'].create({
            'name': 'Sender Fixture Sales', 'code': 'SFS', 'type': 'sale',
            'company_id': company.id,
        })
        return company

    def test_partner_company_sender_is_kept_when_configured(self):
        other = self._sender_company({
            'name': 'Other Company  12345678',
            'vat': '12345678',
            'email': 'other-sender@example.invalid',
        })
        self.partner.company_id = other
        mail = self.Sub._queue_low_balance_mail(self.partner, {})
        self.assertTrue(mail)
        self.assertEqual(parseaddr(mail.email_from), (
            'Other Company', 'other-sender@example.invalid',
        ))
        self.assertEqual(other.name, 'Other Company  12345678')
        self.assertEqual(other.vat, '12345678')
        self.smtp_guard.assert_not_called()

    def test_partner_company_without_email_falls_back_to_user_company(self):
        other = self._sender_company({'name': 'No email company'})
        self.partner.company_id = other
        self._assert_clean_sender(self.Sub._queue_low_balance_mail(self.partner, {}))

    def test_customized_platform_template_senders_are_preserved(self):
        subscription = self._subscription(self.partner)
        invoice = self._invoice(self.partner)
        service = self.env['woow_paas_platform.cloud_service'].new({
            'name': 'Synthetic unavailable service',
        })
        cases = [
            ('mail_template_low_balance', lambda: self.Sub._notify_low_balance(self.partner, invoice)),
            ('mail_template_subscription_cancelled', lambda: self.Sub._notify_subscription_cancelled(subscription, invoice)),
            ('mail_template_billing_hold_never_running', lambda: self.Sub._notify_billing_hold(subscription, service)),
        ]
        for template_id, queue in cases:
            for sender in [
                'Support Team <support@example.invalid>',
                'Platform Support <platform-sender@example.invalid>',
            ]:
                with self.subTest(template=template_id, sender=sender):
                    template = self.env.ref('woow_paas_platform.' + template_id)
                    template.email_from = sender
                    mail = queue()
                    self.assertTrue(mail)
                    self.assertEqual(parseaddr(mail.email_from), parseaddr(sender))
                    self.assertEqual(mail.state, 'outgoing')
        self.smtp_guard.assert_not_called()

    def _capture_status_send(self, subscription, attachment=False):
        captured = []

        def capture(mail, *args, **kwargs):
            mail.ensure_one()
            captured.append((parseaddr(mail.email_from), mail.attachment_ids.ids))

        with patch.object(type(self.env['mail.mail']), 'send', new=capture):
            subscription._sh_send_subscription_email(attachment)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_status_sender_is_clean_at_immediate_send_with_attachment(self):
        subscription = self._subscription(self.partner)
        attachment = self.env['ir.attachment'].create({
            'name': 'Synthetic invoice', 'raw': b'%PDF-1.4',
            'res_model': subscription._name, 'res_id': subscription.id,
        })
        for renewed in [False, True]:
            with self.subTest(renewed=renewed):
                subscription.sh_renewed = renewed
                sender, attachments = self._capture_status_send(subscription, attachment)
                self.assertEqual(sender, ('渥屋科技股份有限公司', self.company.email))
                self.assertIn(attachment.id, attachments)
        self.assertEqual(self.company.name, self.original_name)
        self.assertEqual(self.company.partner_id.name, self.original_name)
        self.assertEqual(self.company.vat, '84006751')

    def test_custom_status_sender_is_preserved_at_immediate_send(self):
        subscription = self._subscription(self.partner)
        template = self.env.ref('woow_paas_platform.mail_template_subscription_status')
        template.email_from = 'Subscription Support <support@example.invalid>'
        sender, attachments = self._capture_status_send(subscription)
        self.assertEqual(sender, ('Subscription Support', 'support@example.invalid'))

    def test_upstream_status_sender_is_untouched_at_immediate_send(self):
        subscription = self._subscription(self.partner, never_expire=False)
        template = self.env.ref('sh_subscription.sh_subscription_customer_email')
        template.email_from = '{{ user.company_id.email_formatted }}'
        sender, attachments = self._capture_status_send(subscription)
        self.assertEqual(sender, (self.original_name, self.company.email))

    def test_non_paas_template_keeps_native_company_display(self):
        template = self.env['mail.template'].create({
            'name': 'Synthetic ERP sender control',
            'model_id': self.env['ir.model']._get_id('res.partner'),
            'subject': 'ERP sender control',
            'email_from': '{{ user.company_id.email_formatted }}',
            'email_to': '{{ object.email }}',
            'body_html': '<p>Synthetic ERP sender control</p>',
        })
        mail = self.env['mail.mail'].browse(template.send_mail(
            self.partner.id, force_send=False,
        ))
        self.assertEqual(parseaddr(mail.email_from), (
            self.original_name, self.company.email,
        ))
        self.smtp_guard.assert_not_called()
