"""Tests for the topup → account.move bridge.

Verifies that _on_payment_done creates a posted out_invoice marked
woow_invoice_issue_state='pending', that the link is idempotent, and that
failures during invoice creation never roll back the wallet credit (the
wallet customer must always receive what they paid for, even if invoicing
hits an unexpected error).
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestEcpayInvoiceTopup(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'TopupInvUser'})
        self.provider = self._get_provider()
        self.payment_method = self._ensure_active_payment_method()
        self.plan = self.env['woow_paas_platform.topup_plan'].create({
            'name': 'TIP',
            'amount': 300,
            'credits': 280,
            'bonus': 20,
            'active': True,
        })
        self.topup = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 300,
            'credits': 280,
            'bonus': 20,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
            'invoice_carrier_type': 'mobile',
            'invoice_carrier': '/MOB+999',
        })
        self.tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 300,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TOPUP%d' % self.topup.id,
            'woow_topup_id': self.topup.id,
        })

    def _get_provider(self):
        p = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not p:
            self.skipTest("ECPay provider not installed")
        return p

    def _ensure_active_payment_method(self):
        method = self.provider.payment_method_ids[:1]
        if not method:
            # active_test=False: the ecpay payment.method ships inactive (Odoo
            # only flips a method active when a provider enabling it is saved),
            # so a default search (active_test=True) misses it under the sandbox
            # full-module install and leaves the required payment_method_id null
            # on the tx → NotNullViolation in setUp (F-ARC-1 / gitea #131).
            method = self.env['payment.method'].with_context(
                active_test=False,
            ).search([('code', '=', 'ecpay')], limit=1)
        if not method:
            self.skipTest("ecpay payment.method not installed")
        if not method.active:
            self.env.cr.execute(
                "UPDATE payment_method SET active = TRUE WHERE id = %s",
                [method.id],
            )
            method.invalidate_recordset(['active'])
        return method

    def test_topup_done_creates_pending_invoice(self):
        # #864 PR-C：儲值發票的開票狀態改由 res.company.auto_invoice 決定（fail-closed resolver），
        # 不再寫死 'pending'。本測試要驗的是「自動開票政策下 cron 撿得到」，故顯式
        # 把政策設成 automatic；manual 政策的對應行為由 test_invoice_issue_policy_864.py 蓋。
        self.env.company.auto_invoice = 'automatic'
        self.topup._on_payment_done(self.tx)
        self.assertTrue(
            self.topup.invoice_account_move_id,
            "Topup should have a linked invoice after payment done",
        )
        move = self.topup.invoice_account_move_id
        self.assertEqual(move.move_type, 'out_invoice')
        self.assertEqual(move.partner_id, self.partner)
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.woow_invoice_issue_state, 'pending')
        # Carrier mapping landed on the move.
        self.assertEqual(move.carrierType, '3')
        self.assertEqual(move.input_carrier_num, '/MOB+999')

    def test_invoice_amount_matches_topup_amount(self):
        self.topup._on_payment_done(self.tx)
        move = self.topup.invoice_account_move_id
        self.assertAlmostEqual(move.amount_untaxed, 300.0)

    def test_ensure_topup_invoice_is_idempotent(self):
        self.topup._on_payment_done(self.tx)
        first_move = self.topup.invoice_account_move_id
        self.assertTrue(first_move)
        # Second call should be a no-op, not create a duplicate.
        self.topup._ensure_topup_invoice()
        self.assertEqual(self.topup.invoice_account_move_id, first_move)

    def test_invoice_creation_failure_does_not_block_wallet_credit(self):
        starting = self.partner.wallet_balance
        # Force _create_topup_invoice to blow up — wallet must still credit.
        with patch.object(
            type(self.topup),
            '_create_topup_invoice',
            side_effect=RuntimeError("boom"),
        ):
            self.topup._on_payment_done(self.tx)
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.topup.state, 'done')
        self.assertFalse(self.topup.invoice_account_move_id)
        self.assertAlmostEqual(self.partner.wallet_balance - starting, 300.0)

    def test_topup_product_is_lazy_created_once(self):
        Product = self.env['product.product'].sudo()
        self.assertFalse(
            Product.search([('default_code', '=', 'WALLET_TOPUP')]),
            "Should not exist before first topup",
        )
        self.topup._on_payment_done(self.tx)
        product = Product.search([('default_code', '=', 'WALLET_TOPUP')])
        self.assertEqual(len(product), 1)
        # Subsequent topup reuses the same product.
        topup2 = self.env['woow.wallet.topup'].create({
            'partner_id': self.partner.id,
            'plan_id': self.plan.id,
            'amount': 100,
            'credits': 100,
            'bonus': 0,
            'currency_id': self.env.company.currency_id.id,
            'state': 'pending',
        })
        tx2 = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.payment_method.id,
            'partner_id': self.partner.id,
            'amount': 100,
            'currency_id': self.env.company.currency_id.id,
            'reference': 'TOPUP%d' % topup2.id,
            'woow_topup_id': topup2.id,
        })
        topup2._on_payment_done(tx2)
        self.assertEqual(
            len(Product.search([('default_code', '=', 'WALLET_TOPUP')])), 1,
        )
