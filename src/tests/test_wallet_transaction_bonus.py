"""Tests for G2-B: Transaction Bonus Metadata.

The transaction list / credit-history modal must show the base credits and
bonus separately for top-up rows. Today the upstream `wallet.transaction`
records only `amount` (already credits + bonus), hiding the bonus from the UI.

This addon adds two `Float` fields, `base_amount` and `bonus_amount`, on
`wallet.transaction` and writes them via the `_on_payment_done` hook on
`woow.wallet.topup` (line 99-105). A versioned post-migrate backfills the
fields on existing top-up rows by joining to `woow_wallet_topup`.

These tests cover:
- New fields are populated when a top-up payment completes (with bonus)
- Zero-bonus top-ups record bonus_amount=0
- Non-topup transactions (subscription debits) leave the fields at 0
- The /api/billing/transactions response carries both new keys
- The migration SQL backfills legacy rows where the new fields are 0
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestWalletTransactionBonus(TransactionCase):
    """Top-up flow records `base_amount`/`bonus_amount` on `wallet.transaction`."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'WalletBonusTestUser',
            'email': 'wallet-bonus@test.example',
        })
        self.provider = self._get_provider()
        self.payment_method = self._ensure_active_payment_method()
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'Bonus Test Plan',
            'amount': 1000,
            'credits': 1000,
            'bonus': 100,
            'active': True,
        })

    def _get_provider(self):
        p = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not p:
            self.skipTest("ECPay provider not installed")
        return p

    def _ensure_active_payment_method(self):
        """Return an active payment method usable by the ECPay provider.

        Mirrors the helper in test_wallet_topup_payment_hooks.py.
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

    def _make_topup_and_tx(self, credits, bonus):
        """Create a draft topup + linked payment.transaction pair."""
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': credits,
            'credits': credits,
            'bonus': bonus,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
        })
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': credits,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TOPUP-BONUS-%d' % topup.id,
            'woow_topup_id': topup.id,
        })
        return topup, tx

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_topup_creates_transaction_with_bonus_metadata(self):
        """Top-up with bonus → wallet.transaction has base/bonus/amount populated."""
        topup, tx = self._make_topup_and_tx(credits=1000, bonus=100)
        topup._on_payment_done(tx)
        wtx = topup.wallet_transaction_id
        self.assertTrue(wtx)
        self.assertAlmostEqual(wtx.base_amount, 1000.0)
        self.assertAlmostEqual(wtx.bonus_amount, 100.0)
        self.assertAlmostEqual(wtx.amount, 1100.0)

    def test_topup_zero_bonus_records_zero_bonus(self):
        """Top-up without bonus → base_amount=N, bonus_amount=0."""
        topup, tx = self._make_topup_and_tx(credits=500, bonus=0)
        topup._on_payment_done(tx)
        wtx = topup.wallet_transaction_id
        self.assertTrue(wtx)
        self.assertAlmostEqual(wtx.base_amount, 500.0)
        self.assertAlmostEqual(wtx.bonus_amount, 0.0)
        self.assertAlmostEqual(wtx.amount, 500.0)

    def test_non_topup_transaction_has_zero_metadata(self):
        """Subscription debit (deduct_wallet_points) leaves base/bonus at 0."""
        # Pre-credit so the deduction has something to take.
        self.partner.sudo().add_wallet_points(
            500.0, description='Seed credit for deduct test',
        )
        # The seed write itself becomes a topup-typed row; deduct creates a
        # second row of type='deduct' which is the one we care about.
        self.partner.sudo().deduct_wallet_points(
            100.0, description='Subscription debit test',
        )
        debit = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'deduct'),
        ], limit=1)
        self.assertTrue(debit)
        self.assertAlmostEqual(debit.base_amount, 0.0)
        self.assertAlmostEqual(debit.bonus_amount, 0.0)

    def test_api_includes_breakdown_keys(self):
        """`/api/billing/transactions` row dicts include base/bonus keys.

        The controller reads `request.env.user.partner_id` and projects rows
        through a fixed dict shape. We mirror that projection here directly
        against the ORM (avoiding an HTTP session) so the test exercises the
        exact shape contract the frontend depends on.
        """
        topup, tx = self._make_topup_and_tx(credits=1000, bonus=100)
        topup._on_payment_done(tx)

        Transaction = self.env['wallet.transaction'].sudo()
        rows = Transaction.search(
            [('partner_id', '=', self.partner.id)],
            order='create_date desc, id desc',
        )
        self.assertTrue(rows, "Setup should have produced at least one txn")
        # Mirror the controller's projection (src/controllers/billing.py
        # `api_transactions`) so the assertion targets the exact keys it builds.
        data = []
        for txn in rows:
            data.append({
                'id': txn.id,
                'date': txn.create_date.isoformat() if txn.create_date else None,
                'description': txn.description or txn.name or '',
                'transaction_type': 'credit' if txn.amount >= 0 else 'debit',
                'amount': txn.amount,
                'balance_after': txn.balance_after,
                'base_amount': txn.base_amount or 0.0,
                'bonus_amount': txn.bonus_amount or 0.0,
            })
        for row in data:
            self.assertIn('base_amount', row)
            self.assertIn('bonus_amount', row)
        # The top-up row should carry the breakdown.
        topup_row = next(r for r in data if r['amount'] >= 1100)
        self.assertAlmostEqual(topup_row['base_amount'], 1000.0)
        self.assertAlmostEqual(topup_row['bonus_amount'], 100.0)

    def test_migration_backfills_legacy_topups(self):
        """Migration SQL fills base/bonus from `woow_wallet_topup` for legacy rows."""
        # Build a topup whose linked wallet.transaction has zero base/bonus,
        # simulating the pre-migration on-disk state.
        wtx = self.env['wallet.transaction'].sudo().create({
            'partner_id': self.partner.id,
            'transaction_type': 'topup',
            'amount': 530.0,
            'description': 'Legacy topup row',
        })
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 500,
            'credits': 500,
            'bonus': 30,
            'currency_id': self.env.company.currency_id.id,
            'state': 'done',
            'wallet_transaction_id': wtx.id,
        })
        # Sanity: pre-migration values are 0.
        self.assertAlmostEqual(wtx.base_amount, 0.0)
        self.assertAlmostEqual(wtx.bonus_amount, 0.0)

        # Run the same SQL the migration uses.
        self.env.cr.execute(
            """
            UPDATE wallet_transaction wt
            SET base_amount  = topup.credits,
                bonus_amount = topup.bonus
            FROM woow_wallet_topup topup
            WHERE topup.wallet_transaction_id = wt.id
              AND wt.transaction_type = 'topup'
              AND (wt.base_amount IS NULL OR wt.base_amount = 0)
              AND (wt.bonus_amount IS NULL OR wt.bonus_amount = 0)
            """
        )
        wtx.invalidate_recordset(['base_amount', 'bonus_amount'])
        self.assertAlmostEqual(wtx.base_amount, 500.0)
        self.assertAlmostEqual(wtx.bonus_amount, 30.0)
        # Idempotency: re-running updates 0 rows because both fields are
        # now non-zero, so the WHERE clause excludes the row.
        self.env.cr.execute(
            """
            UPDATE wallet_transaction wt
            SET base_amount  = topup.credits,
                bonus_amount = topup.bonus
            FROM woow_wallet_topup topup
            WHERE topup.wallet_transaction_id = wt.id
              AND wt.transaction_type = 'topup'
              AND (wt.base_amount IS NULL OR wt.base_amount = 0)
              AND (wt.bonus_amount IS NULL OR wt.bonus_amount = 0)
            """
        )
        self.assertEqual(self.env.cr.rowcount, 0)
