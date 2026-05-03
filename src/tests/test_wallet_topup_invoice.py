"""Manual topup invoices must also have woow_skip_upstream_hook=True (Path B).

Both manual (user-initiated) and auto (system-triggered) topups go through
_create_topup_invoice, which sets the skip flag unconditionally. This test
makes the contract explicit for the MANUAL path so future refactors that
gate the flag on source won't accidentally break it.

The auto path is covered by test_wallet_topup_auto.py:
  TestChargeViaToken.test_charge_via_token_successful_charge_credits_wallet_and_creates_invoice
"""
from odoo.tests.common import TransactionCase


class TestManualTopupInvoiceSkipsHook(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Manual Topup Test',
            'email': 'manual@test.com',
        })
        # Find or create a topup plan (manual topups always have one)
        self.plan = self.env['woow_paas_platform.topup_plan'].search([], limit=1)
        if not self.plan:
            self.plan = self.env['woow_paas_platform.topup_plan'].create({
                'name': 'Test Plan',
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 0.0,
            })

    def _make_manual_topup(self):
        """Create a manual topup record (source='manual', has plan_id)."""
        twd = self.env.ref('base.TWD')
        return self.env['woow.wallet.topup'].sudo().create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': self.plan.amount,
            'credits': self.plan.credits,
            'bonus': self.plan.bonus,
            'currency_id': twd.id,
            'source': 'manual',
            'state': 'pending',  # ready for callback
        })

    def test_manual_topup_invoice_has_skip_upstream_hook_flag(self):
        """Manual topup's invoice must have woow_skip_upstream_hook=True
        so it bypasses upstream ecpay_invoice_tw inline hook."""
        topup = self._make_manual_topup()
        invoice = topup._ensure_topup_invoice()
        self.assertTrue(invoice, "Invoice must be created")
        self.assertTrue(
            invoice.woow_skip_upstream_hook,
            "Manual topup invoice MUST have skip flag (Path B): "
            "without it, company.auto_invoice='automatic' would trigger "
            "upstream inline hook causing double issuance.",
        )

    def test_manual_topup_invoice_marked_pending_for_cron(self):
        """Manual topup invoice woow_invoice_issue_state='pending' so cron picks it up."""
        topup = self._make_manual_topup()
        invoice = topup._ensure_topup_invoice()
        self.assertEqual(
            invoice.woow_invoice_issue_state, 'pending',
            "Manual topup invoice must be queued for ECPay issuance "
            "(payment was real money, e-invoice required)",
        )
