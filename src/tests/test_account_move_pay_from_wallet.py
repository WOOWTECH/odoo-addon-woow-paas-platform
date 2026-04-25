"""Unit tests for account.move._pay_from_wallet helper (Phase 2c #5)."""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayFromWallet(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Admin partner is the one exercised across the payment flow in this
        # repo; keep consistent with TestPaymentMethodsApi / TestProfileApi.
        cls.partner = cls.env.ref('base.partner_admin')
        # wallet_balance is a computed non-stored field backed by
        # loyalty.card.points (sh_subscription_wallet). Writing to it is a
        # no-op — credit/debit must go through add_wallet_points /
        # deduct_wallet_points.
        # Ensure at least one bank/cash journal for the fallback branch.
        cls.journal = cls.env['account.journal'].sudo().search(
            [('company_id', '=', cls.env.company.id),
             ('type', 'in', ('bank', 'cash'))],
            limit=1,
        )
        if not cls.journal:
            cls.journal = cls.env['account.journal'].sudo().create({
                'name': 'Test Bank',
                'type': 'bank',
                'code': 'TBNK',
                'company_id': cls.env.company.id,
            })

    def _make_invoice(self, amount=100.0):
        product = self.env['product.product'].sudo().search([], limit=1)
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': 'Test Product',
                'list_price': amount,
            })
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'quantity': 1,
                'price_unit': amount,
                'tax_ids': [(5, 0, 0)],  # no taxes to keep amount simple
            })],
        })
        return invoice

    def _credit_wallet(self, amount):
        """Credit the wallet via the official API (loyalty.card-backed)."""
        self.partner.sudo().add_wallet_points(amount, description='Test credit')
        # wallet_balance is computed from loyalty.card.points; invalidate so
        # subsequent reads recompute.
        self.partner.invalidate_recordset(['wallet_balance'])

    def test_pay_from_wallet_posts_draft_invoice(self):
        invoice = self._make_invoice(100.0)
        self._credit_wallet(100.0)
        self.assertEqual(invoice.state, 'draft')
        result = invoice._pay_from_wallet()
        self.assertTrue(result)
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.payment_state, 'paid')

    def test_pay_from_wallet_deducts_wallet_amount(self):
        invoice = self._make_invoice(250.0)
        self._credit_wallet(250.0)
        balance_before = self.partner.wallet_balance
        invoice._pay_from_wallet()
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertAlmostEqual(
            self.partner.wallet_balance, balance_before - 250.0, places=2,
        )

    def test_pay_from_wallet_invalidates_balance_cache(self):
        """Regression: BUG-2.

        partner.wallet_balance is a non-stored compute backed by
        loyalty.card.points. _pay_from_wallet -> deduct_wallet_points
        mutates loyalty.card.points but the recordset cache from before
        the call still holds the pre-deduction value. Without the model
        invalidating the cache, callers that re-read balance in the same
        request (e.g. api_invoice_pay returning new_balance to the
        frontend) lie about the post-payment balance. This test reads
        balance straight after _pay_from_wallet without an explicit
        invalidate — the model is responsible for dropping its own cache.
        """
        invoice = self._make_invoice(80.0)
        self._credit_wallet(120.0)
        balance_before = self.partner.wallet_balance
        invoice._pay_from_wallet()
        # No invalidate_recordset here — the fix in _pay_from_wallet does it.
        self.assertAlmostEqual(
            self.partner.wallet_balance, balance_before - 80.0, places=2,
            msg="wallet_balance still cached at pre-deduction value",
        )
        tx = self.env['wallet.transaction'].sudo().search(
            [('partner_id', '=', self.partner.id)], order='id desc', limit=1,
        )
        self.assertEqual(tx.amount, -250.0)
        self.assertIn(invoice.name or '', tx.description)

    def test_pay_from_wallet_creates_reconciled_payment(self):
        invoice = self._make_invoice(150.0)
        self._credit_wallet(150.0)
        invoice._pay_from_wallet()
        payment = self.env['account.payment'].sudo().search(
            [('partner_id', '=', self.partner.id)], order='id desc', limit=1,
        )
        self.assertTrue(payment)
        self.assertEqual(payment.amount, 150.0)
        # Odoo 18 transitions account.payment through draft → in_process → paid;
        # the final 'paid' state requires bank-statement matching (out of scope
        # here). Accept either since both are valid post-action_post states.
        self.assertIn(payment.state, ('in_process', 'paid'))
        self.assertEqual(payment.payment_type, 'inbound')
        receivable_lines = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        )
        self.assertTrue(all(l.reconciled for l in receivable_lines))
        # Invoice-level paid assertion is the stable semantic check.
        self.assertEqual(invoice.payment_state, 'paid')

    def test_pay_from_wallet_raises_when_insufficient(self):
        invoice = self._make_invoice(500.0)
        self._credit_wallet(100.0)  # less than invoice total
        with self.assertRaises(UserError):
            invoice._pay_from_wallet()
        # invoice untouched
        self.assertEqual(invoice.state, 'draft')

    def test_pay_from_wallet_uses_fallback_journal_when_sh_paid_missing(self):
        """When company.sh_paid_subscription_journal is unset we fall back to
        the first bank/cash journal available on the company."""
        company = self.env.company.sudo()
        company.sh_paid_subscription_journal = False
        invoice = self._make_invoice(75.0)
        self._credit_wallet(75.0)
        result = invoice._pay_from_wallet()
        self.assertTrue(result)
        self.assertEqual(invoice.payment_state, 'paid')
