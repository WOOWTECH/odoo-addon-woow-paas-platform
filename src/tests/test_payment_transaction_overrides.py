from odoo.tests.common import TransactionCase


class TestPaymentTransactionOverrides(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'T'})
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.payment_method = self._ensure_active_payment_method()
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'P', 'amount': 100, 'credits': 100, 'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 100, 'credits': 100,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
        })
        self.tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 100,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TX%d' % self.topup.id,
            'woow_topup_id': self.topup.id,
        })

    def test_set_done_triggers_topup_credit(self):
        self.tx._set_done()
        self.topup.invalidate_recordset()
        self.assertEqual(self.topup.state, 'done')

    def test_set_done_no_op_without_topup_link(self):
        tx2 = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 1,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'SOLO1',
        })
        # Must not raise and must not touch any topup
        tx2._set_done()
        self.assertFalse(tx2.woow_topup_id)

    def test_set_error_triggers_topup_failure(self):
        self.tx._set_error('Card declined by bank')
        self.topup.invalidate_recordset()
        self.assertEqual(self.topup.state, 'failed')
        self.assertEqual(self.topup.failure_code, 'card_declined')

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
