"""Subscription cycle invoices must be not_required + skip upstream hook (Path B).

Per docs/architecture/billing-philosophy.md Principle 3-5:
- Cycle invoices are paid via wallet (which was already topped up via real money —
  that topup ALREADY issued an ECPay e-invoice).
- The Odoo invoice itself MUST still be created as the internal accounting
  ledger record for the wallet deduction event.
- But it MUST NOT trigger ANOTHER ECPay e-invoice (would double-bill the user).

The two flags work together:
- ``woow_invoice_issue_state='not_required'`` — async ``ecpay_invoice_cron`` will
  not pick the move up.
- ``woow_skip_upstream_hook=True`` — even if ``company.auto_invoice='automatic'``
  the inline upstream hook in ``action_post()`` won't issue either.
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestSubscriptionInvoiceState(TransactionCase):
    """Verify _get_or_create_cycle_invoice marks invoices to skip ECPay issuance."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']

    def _make_partner(self, **kwargs):
        return self.env['res.partner'].create({
            'name': 'CycleSkipUser',
            'wallet_balance': 1000.0,
            **kwargs,
        })

    def _make_sub(self, partner, price=100.0):
        today = date.today()
        return self.Sub.sudo().create({
            'sh_partner_id': partner.id,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'sh_plan_price': price,
            'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress',
            'sh_trial': False,
        })

    def test_cycle_invoice_marked_not_required(self):
        """Fresh cycle invoice must NOT request ECPay issuance."""
        partner = self._make_partner()
        sub = self._make_sub(partner)
        invoice, is_new = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertTrue(is_new, "Should be a fresh invoice, not reused")
        self.assertEqual(
            invoice.woow_invoice_issue_state, 'not_required',
            "Cycle invoice must NOT request ECPay issuance "
            "(paid via wallet — wallet topup already issued an e-invoice)",
        )

    def test_cycle_invoice_skip_upstream_hook_set(self):
        """Fresh cycle invoice must isolate the inline upstream ECPay hook."""
        partner = self._make_partner()
        sub = self._make_sub(partner)
        invoice, _ = self.Sub._get_or_create_cycle_invoice(sub)
        self.assertTrue(
            invoice.woow_skip_upstream_hook,
            "Cycle invoice must skip upstream ecpay_invoice_tw inline hook "
            "so company.auto_invoice='automatic' does not double-issue",
        )
