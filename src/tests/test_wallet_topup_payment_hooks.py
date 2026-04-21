from odoo.tests.common import TransactionCase


class TestTopupPaymentHooks(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'TopupUser'})
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
            'provider_id': self._get_provider().id,
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
