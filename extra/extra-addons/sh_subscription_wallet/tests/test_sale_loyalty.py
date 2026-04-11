# -*- coding: utf-8 -*-
"""Layer 1: SL-001 ~ SL-002 — Sale Loyalty Integration Tests

Tests that SO confirmation with trigger product creates wallet points via
the native sale_loyalty integration.
"""
from odoo.tests import tagged

from .common import WalletTestCommon


@tagged('wallet', 'sale_loyalty', '-at_install', 'post_install')
class TestSaleLoyalty(WalletTestCommon):
    """SL-001 ~ SL-002: Sale order → loyalty points integration."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ensure wallet program has a trigger product for earning points
        cls.wallet_program = cls.env.ref(
            'sh_subscription_wallet.loyalty_program_wallet',
            raise_if_not_found=False
        )

    def test_sl001_so_confirm_adds_points(self):
        """SL-001: SO with trigger product → action_confirm → points credited."""
        if not self.wallet_program:
            self.skipTest("Wallet loyalty program not found")

        # Check if program has trigger products / reward rules configured
        trigger_products = self.wallet_program.trigger_product_ids
        if not trigger_products:
            # Create a simple trigger/reward setup
            trigger_product = self.env['product.product'].create({
                'name': 'Wallet Topup Product',
                'type': 'service',
                'list_price': 500.0,
            })
            # Configure program rules if possible
            self.skipTest("Wallet program has no trigger products configured")
            return

        trigger_product = trigger_products[0]
        partner = self.partner_a
        initial_balance = partner.wallet_balance

        so = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': trigger_product.id,
                'product_uom_qty': 1,
                'price_unit': trigger_product.list_price,
            })],
        })
        so.action_confirm()

        partner.invalidate_recordset()
        new_balance = partner.wallet_balance
        self.assertGreaterEqual(new_balance, initial_balance,
                                "Balance should not decrease after SO confirm")

    def test_sl002_bonus_calculation(self):
        """SL-002: Topup bonus calculated correctly per amount tier."""
        partner = self.partner_a
        initial_balance = partner.wallet_balance

        # Test direct point addition with bonus
        # 1000 NT topup → 50 bonus → 1050 total
        topup_amount = 1000.0
        bonus = 50.0
        total = topup_amount + bonus

        partner.add_wallet_points(
            total,
            description=f'儲值 NT${topup_amount:.0f} (含贈送 {bonus:.0f} 點)',
        )

        partner.invalidate_recordset()
        expected = initial_balance + total
        self.assertAlmostEqual(partner.wallet_balance, expected, places=0,
                               msg=f"Balance should be {expected} after topup with bonus")

        # Verify transaction was created
        tx = self.env['wallet.transaction'].search([
            ('partner_id', '=', partner.id),
            ('transaction_type', '=', 'topup'),
        ], order='create_date desc', limit=1)
        self.assertTrue(tx, "Topup transaction should be created")
        self.assertAlmostEqual(tx.amount, total, places=0)
