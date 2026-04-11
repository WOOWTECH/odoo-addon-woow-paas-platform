# -*- coding: utf-8 -*-
"""
錢包點數操作測試

測試案例:
- WB-001: 新增點數成功
- WB-002: 扣除點數成功
- WB-003: 點數不足扣款
- WB-004: 取得 Loyalty Card
- WB-005: 餘額計算一致性
"""
from odoo.tests import tagged
from odoo.exceptions import UserError
from .common import WalletTestCommon


@tagged('wallet', 'wallet_balance', '-at_install', 'post_install')
class TestWalletBalance(WalletTestCommon):
    """錢包點數餘額測試"""

    def test_wb001_add_wallet_points_success(self):
        """WB-001: 新增點數成功"""
        # 取得初始餘額
        initial_balance = self.partner_b.wallet_balance

        # 新增點數
        result = self.partner_b.add_wallet_points(1000, '測試儲值')

        # 驗證
        self.assertTrue(result)
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.partner_b.wallet_balance, initial_balance + 1000)

        # 驗證交易記錄
        transactions = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner_b.id),
            ('transaction_type', '=', 'topup'),
        ], order='id desc', limit=1)

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions.amount, 1000)
        self.assertEqual(transactions.state, 'done')
        self.assertIn('測試儲值', transactions.description or '')

    def test_wb002_deduct_wallet_points_success(self):
        """WB-002: 扣除點數成功"""
        # 確保客戶 A 有足夠點數
        self.env.flush_all()
        self.env.invalidate_all()
        initial_balance = self.partner_a.wallet_balance
        self.assertEqual(initial_balance, 1000.0)

        # 扣除點數
        deducted = self.partner_a.deduct_wallet_points(500, '測試扣款')

        # 驗證
        self.assertEqual(deducted, 500)
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.partner_a.wallet_balance, 500.0)

        # 驗證交易記錄
        transactions = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner_a.id),
            ('transaction_type', '=', 'deduct'),
        ], order='id desc', limit=1)

        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions.amount, -500)  # 負數表示扣除
        self.assertEqual(transactions.state, 'done')

    def test_wb003_deduct_insufficient_points(self):
        """WB-003: 點數不足扣款 - 只扣可用餘額"""
        # 設定客戶 B 初始點數為 100
        self.partner_b.add_wallet_points(100, '初始儲值')
        self.env.flush_all()
        self.env.invalidate_all()
        initial_balance = self.partner_b.wallet_balance
        self.assertEqual(initial_balance, 100.0)

        # 嘗試扣除 500 點（超過餘額）
        deducted = self.partner_b.deduct_wallet_points(500, '超額扣款測試')

        # 驗證：只扣除可用的 100 點
        self.assertEqual(deducted, 100)
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.partner_b.wallet_balance, 0.0)

    def test_wb004_get_wallet_loyalty_card(self):
        """WB-004: 取得 Loyalty Card"""
        # 客戶 A 已有 loyalty card
        loyalty_card = self.partner_a.get_wallet_loyalty_card()

        self.assertTrue(loyalty_card)
        self.assertEqual(loyalty_card.partner_id, self.partner_a)
        self.assertEqual(loyalty_card.program_id, self.wallet_program)

    def test_wb004_get_wallet_loyalty_card_create_new(self):
        """WB-004b: 取得 Loyalty Card（自動建立）"""
        # 建立新客戶
        new_partner = self.env['res.partner'].create({
            'name': 'New Test Customer',
            'email': 'new.test@example.com',
        })

        # 取得 loyalty card（應自動建立）
        loyalty_card = new_partner.get_wallet_loyalty_card()

        self.assertTrue(loyalty_card)
        self.assertEqual(loyalty_card.partner_id, new_partner)
        self.assertEqual(loyalty_card.points, 0)

    def test_wb005_balance_consistency(self):
        """WB-005: 餘額計算一致性"""
        # 建立新客戶
        new_partner = self.env['res.partner'].create({
            'name': 'Balance Test Customer',
            'email': 'balance.test@example.com',
        })

        # 多次儲值扣款
        new_partner.add_wallet_points(1000, '第一次儲值')
        new_partner.add_wallet_points(500, '第二次儲值')
        new_partner.deduct_wallet_points(300, '第一次扣款')
        new_partner.add_wallet_points(200, '第三次儲值')
        new_partner.deduct_wallet_points(400, '第二次扣款')

        # 計算預期餘額: 1000 + 500 - 300 + 200 - 400 = 1000
        expected_balance = 1000

        # Flush all ORM writes to DB and clear caches
        self.env.flush_all()
        self.env.invalidate_all()

        # 驗證 wallet_balance 與 loyalty_card.points 一致
        loyalty_card = new_partner.get_wallet_loyalty_card()
        self.assertEqual(new_partner.wallet_balance, expected_balance)
        self.assertEqual(loyalty_card.points, expected_balance)
        self.assertEqual(new_partner.wallet_balance, loyalty_card.points)

    def test_add_points_zero_or_negative_raises_error(self):
        """測試新增零或負數點數應拋出錯誤"""
        with self.assertRaises(UserError):
            self.partner_b.add_wallet_points(0, '零點數')

        with self.assertRaises(UserError):
            self.partner_b.add_wallet_points(-100, '負點數')

    def test_deduct_points_zero_or_negative_raises_error(self):
        """測試扣除零或負數點數應拋出錯誤"""
        with self.assertRaises(UserError):
            self.partner_a.deduct_wallet_points(0, '零點數')

        with self.assertRaises(UserError):
            self.partner_a.deduct_wallet_points(-100, '負點數')
