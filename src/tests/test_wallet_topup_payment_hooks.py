from odoo.tests.common import TransactionCase


class TestTopupPaymentHooks(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'TopupUser'})
        self.provider = self._get_provider()
        self.payment_method = self._ensure_active_payment_method()
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'P1', 'amount': 200, 'credits': 180, 'bonus': 20, 'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 200,
            'credits': 180,
            'bonus': 20,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
        })
        self.tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 200,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TOPUP%d' % self.topup.id,
            'woow_topup_id': self.topup.id,
        })

    def _get_provider(self):
        p = self.env['payment.provider'].sudo().search([('code', '=', 'ECPay')], limit=1)
        if not p:
            self.skipTest("ECPay provider not installed")
        return p

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

    def test_on_payment_done_credits_wallet_and_flips_state(self):
        starting = self.partner.wallet_balance
        self.topup._on_payment_done(self.tx)
        self.assertEqual(self.topup.state, 'done')
        self.assertTrue(self.topup.wallet_transaction_id)
        self.assertAlmostEqual(self.partner.wallet_balance - starting, 200.0)

    def test_on_payment_done_is_idempotent(self):
        self.topup._on_payment_done(self.tx)
        first_wtx = self.topup.wallet_transaction_id
        first_balance = self.partner.wallet_balance
        self.topup._on_payment_done(self.tx)  # replay
        self.assertEqual(self.topup.wallet_transaction_id, first_wtx)
        self.assertEqual(self.partner.wallet_balance, first_balance)

    def test_on_payment_failed_records_failure(self):
        self.tx.state_message = 'Card declined by bank'
        self.topup._on_payment_failed(self.tx)
        self.assertEqual(self.topup.state, 'failed')
        self.assertEqual(self.topup.failure_code, 'card_declined')
        self.assertEqual(self.topup.failure_message, 'Card declined by bank')

    def test_on_payment_failed_no_op_when_done(self):
        self.topup._on_payment_done(self.tx)
        self.topup._on_payment_failed(self.tx)
        self.assertEqual(self.topup.state, 'done')
