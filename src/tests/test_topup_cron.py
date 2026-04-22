from datetime import timedelta
from odoo import fields
from odoo.tests.common import TransactionCase


class TestTopupCron(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Cron'})
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1)
        if not self.provider:
            self.skipTest("ECPay provider not installed")
        self.payment_method = self._ensure_active_payment_method()
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'Cron', 'amount': 100, 'credits': 100, 'active': True,
        })

    def _make_pending_topup(self, tx_state='pending', age_minutes=35):
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 100,
            'currency_id': self.env.company.currency_id.id,
            'reference': f'CRON{fields.Datetime.now().timestamp()}',
        })
        tx.state = tx_state
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 100, 'credits': 100,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
            'payment_transaction_id': tx.id,
        })
        # backdate via SQL (create_date is normally readonly)
        old = fields.Datetime.now() - timedelta(minutes=age_minutes)
        self.env.cr.execute(
            "UPDATE woow_wallet_topup SET create_date = %s WHERE id = %s",
            (old, topup.id),
        )
        topup.invalidate_recordset(['create_date'])
        return topup, tx

    def test_fresh_pending_ignored(self):
        topup, _ = self._make_pending_topup(age_minutes=5)
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        self.assertEqual(topup.state, 'pending')

    def test_catches_up_when_tx_already_done(self):
        topup, tx = self._make_pending_topup(age_minutes=35)
        tx.state = 'done'
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        topup.invalidate_recordset()
        self.assertEqual(topup.state, 'done')

    def test_marks_failed_when_tx_errored(self):
        topup, tx = self._make_pending_topup(age_minutes=35)
        tx.state = 'error'
        tx.state_message = 'Card declined'
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        topup.invalidate_recordset()
        self.assertEqual(topup.state, 'failed')

    def test_cancels_long_stuck_pending(self):
        topup, tx = self._make_pending_topup(age_minutes=70)
        self.env['woow.wallet.topup']._cron_cleanup_pending_topups()
        topup.invalidate_recordset()
        self.assertEqual(topup.state, 'cancelled')

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
