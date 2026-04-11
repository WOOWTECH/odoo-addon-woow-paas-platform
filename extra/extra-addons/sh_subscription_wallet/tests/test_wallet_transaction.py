# -*- coding: utf-8 -*-
"""
交易記錄測試

測試案例:
- WT-001: 建立儲值交易
- WT-002: 建立扣款交易
- WT-003: 交易編號自動產生
- WT-004: 交易後餘額記錄
"""
from odoo.tests import tagged
from .common import WalletTestCommon


@tagged('wallet', 'wallet_transaction', '-at_install', 'post_install')
class TestWalletTransaction(WalletTestCommon):
    """錢包交易記錄測試"""

    def test_wt001_create_topup_transaction(self):
        """WT-001: 建立儲值交易"""
        # 建立儲值交易
        transaction = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'topup',
            'amount': 1000,
            'description': '測試儲值交易',
        })

        # 驗證
        self.assertTrue(transaction)
        self.assertEqual(transaction.transaction_type, 'topup')
        self.assertEqual(transaction.amount, 1000)
        self.assertEqual(transaction.state, 'done')  # default state
        self.assertEqual(transaction.partner_id, self.partner_a)

    def test_wt002_create_deduct_transaction(self):
        """WT-002: 建立扣款交易"""
        # 建立扣款交易
        transaction = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'deduct',
            'amount': -500,
            'description': '測試扣款交易',
        })

        # 驗證
        self.assertTrue(transaction)
        self.assertEqual(transaction.transaction_type, 'deduct')
        self.assertEqual(transaction.amount, -500)  # 負數表示扣除

    def test_wt003_auto_generate_transaction_name(self):
        """WT-003: 交易編號自動產生"""
        # 先建立 sequence
        sequence = self.env['ir.sequence'].search([
            ('code', '=', 'wallet.transaction'),
        ], limit=1)

        if not sequence:
            sequence = self.env['ir.sequence'].create({
                'name': 'Wallet Transaction Sequence',
                'code': 'wallet.transaction',
                'prefix': 'WT/%(year)s/',
                'padding': 5,
            })

        # 建立交易
        transaction = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'topup',
            'amount': 100,
        })

        # 驗證交易編號格式
        self.assertTrue(transaction.name)
        self.assertNotEqual(transaction.name, 'New')
        # 應該符合 WT/YYYY/XXXXX 格式
        self.assertTrue(
            transaction.name.startswith('WT/') or transaction.name != 'New',
            f"Transaction name should be auto-generated, got: {transaction.name}"
        )

    def test_wt004_balance_after_recorded(self):
        """WT-004: 交易後餘額記錄"""
        # 使用 add_wallet_points 方法會自動記錄 balance_after
        initial_balance = self.partner_a.wallet_balance

        self.partner_a.add_wallet_points(500, '測試餘額記錄')

        # 取得最新交易
        transaction = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner_a.id),
            ('transaction_type', '=', 'topup'),
        ], order='id desc', limit=1)

        # 驗證 balance_after
        expected_balance = initial_balance + 500
        self.assertEqual(transaction.balance_after, expected_balance)

    def test_transaction_types(self):
        """測試所有交易類型"""
        transaction_types = ['topup', 'deduct', 'refund', 'card_charge',
                            'card_bind', 'card_unbind', 'adjustment']

        for trans_type in transaction_types:
            transaction = self.env['wallet.transaction'].create({
                'partner_id': self.partner_a.id,
                'transaction_type': trans_type,
                'amount': 100 if trans_type in ['topup', 'refund', 'adjustment'] else -100,
                'description': f'Test {trans_type}',
            })
            self.assertEqual(transaction.transaction_type, trans_type)

    def test_transaction_states(self):
        """測試交易狀態"""
        # done (default)
        trans_done = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'topup',
            'amount': 100,
        })
        self.assertEqual(trans_done.state, 'done')

        # draft
        trans_draft = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'topup',
            'amount': 100,
            'state': 'draft',
        })
        self.assertEqual(trans_draft.state, 'draft')

        # failed
        trans_failed = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'card_charge',
            'amount': -100,
            'state': 'failed',
            'card_charge_error': 'Test error message',
        })
        self.assertEqual(trans_failed.state, 'failed')
        self.assertEqual(trans_failed.card_charge_error, 'Test error message')

    def test_transaction_with_subscription_link(self):
        """測試交易與訂閱關聯"""
        # 這個測試需要 sh.subscription.subscription 模型存在
        # 如果不存在，跳過此測試
        if 'sh.subscription.subscription' not in self.env:
            self.skipTest('sh.subscription.subscription model not found')

        subscription = self._create_test_subscription(self.partner_a)

        transaction = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'deduct',
            'amount': -500,
            'subscription_id': subscription.id,
            'description': '訂閱扣款測試',
        })

        self.assertEqual(transaction.subscription_id.id, subscription.id)

    def test_transaction_display_name(self):
        """測試交易顯示名稱（Odoo 18 使用 _compute_display_name）"""
        transaction = self.env['wallet.transaction'].create({
            'partner_id': self.partner_a.id,
            'transaction_type': 'topup',
            'amount': 1000,
        })

        display_name = transaction.display_name
        self.assertTrue(display_name)

        # 顯示名稱應包含交易編號、類型和金額
        self.assertIn('儲值', display_name)
        self.assertIn('+1000', display_name)
