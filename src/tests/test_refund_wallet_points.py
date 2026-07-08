"""Tests for res.partner.refund_wallet_points (issue #318)."""
from odoo.tests.common import TransactionCase
from odoo.exceptions import UserError


class TestRefundWalletPoints(TransactionCase):
    """refund_wallet_points 建立 refund 型交易並加點（對比 add_wallet_points 硬編 topup）。"""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': '#318 Refund Partner'})
        self.WalletTx = self.env['wallet.transaction']

    def _refund_txns(self):
        return self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund'),
        ])

    def test_refund_creates_refund_typed_transaction(self):
        card = self.partner.get_wallet_loyalty_card()
        before = card.points
        ok = self.partner.refund_wallet_points(
            amount=120.0, description='Service deleted refund: n8n')
        self.assertTrue(ok)
        card.invalidate_recordset(['points'])
        self.assertEqual(card.points, before + 120.0)
        txns = self._refund_txns()
        self.assertEqual(len(txns), 1)
        self.assertEqual(txns.transaction_type, 'refund')
        self.assertEqual(txns.amount, 120.0)
        self.assertEqual(txns.balance_after, before + 120.0)

    def test_refund_links_subscription_for_reconciliation(self):
        plan = self.env['sh.subscription.plan'].create({
            'name': '#318 Refund Plan', 'sh_plan_price': 100.0,
            'sh_duration': 1, 'sh_unit': 'month', 'sh_no_of_billing_cycle': -1,
            'sh_trial': False,
        })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': plan.id,
            'sh_plan_price': 100.0,
        })
        self.partner.refund_wallet_points(
            amount=50.0, description='x', subscription=sub)
        txn = self._refund_txns()
        self.assertEqual(txn.subscription_id.id, sub.id)

    def test_refund_rejects_non_positive_amount(self):
        with self.assertRaises(UserError):
            self.partner.refund_wallet_points(amount=0.0)
        with self.assertRaises(UserError):
            self.partner.refund_wallet_points(amount=-5.0)
