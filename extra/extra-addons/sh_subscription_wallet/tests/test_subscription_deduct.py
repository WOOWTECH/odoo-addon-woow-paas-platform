# -*- coding: utf-8 -*-
"""
訂閱扣款流程測試

測試案例:
- SD-001: 點數足夠完全扣款
- SD-002: 點數不足觸發信用卡
- SD-003: 點數為零信用卡全額
- SD-004: 無點數無綁卡失敗
- SD-005: 信用卡扣款失敗重試
- SD-006: Cron 批次扣款
"""
from odoo.tests import tagged
from unittest.mock import patch, MagicMock
from .common import WalletTestCommon


@tagged('wallet', 'subscription_deduct', '-at_install', 'post_install')
class TestSubscriptionDeduct(WalletTestCommon):
    """訂閱扣款流程測試"""

    def setUp(self):
        super().setUp()
        # 建立 Mixin 實例用於測試
        self.mixin = self.env['subscription.wallet.mixin']

    def _create_mock_subscription(self, partner, amount=500.0):
        """建立測試訂閱"""
        return self._create_test_subscription(partner, amount)

    def test_sd001_points_sufficient_full_deduct(self):
        """SD-001: 點數足夠完全扣款"""
        # 設定：客戶 A 有 1000 點，訂閱費 500
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.partner_a.wallet_balance, 1000.0)
        subscription = self._create_mock_subscription(self.partner_a, 500)

        # 執行扣款
        result = self.mixin._process_wallet_payment(subscription, 500)

        # 驗證
        self.assertTrue(result['success'])
        self.assertEqual(result['points_deducted'], 500)
        self.assertEqual(result['card_charged'], 0)
        self.assertIsNone(result['error'])

        # Verify via loyalty card directly (bypasses computed field caching)
        loyalty_card = self.partner_a.get_wallet_loyalty_card()
        loyalty_card.flush_recordset(['points'])
        self.env.invalidate_all()
        self.assertEqual(self.partner_a.wallet_balance, 500.0)

    def test_sd002_points_insufficient_trigger_card(self):
        """SD-002: 點數不足觸發信用卡"""
        # 設定：客戶 A 有 100 點，訂閱費 500，已綁卡
        # 先扣除一些點數讓餘額變成 100
        self.partner_a.deduct_wallet_points(900, '調整餘額')
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.partner_a.wallet_balance, 100.0)
        self.assertTrue(self.partner_a.has_bound_credit_card())

        subscription = self._create_mock_subscription(self.partner_a, 500)

        # Mock ECPay API 成功回應
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            return_value={'RtnCode': 1, 'RtnMsg': 'Success', 'TradeNo': 'TEST123'}
        ):
            result = self.mixin._process_wallet_payment(subscription, 500)

        # 驗證
        self.assertTrue(result['success'])
        self.assertEqual(result['points_deducted'], 100)
        self.assertEqual(result['card_charged'], 400)
        loyalty_card = self.partner_a.get_wallet_loyalty_card()
        loyalty_card.flush_recordset(['points'])
        self.env.invalidate_all()
        self.assertEqual(self.partner_a.wallet_balance, 0.0)

    def test_sd003_zero_points_card_full_charge(self):
        """SD-003: 點數為零信用卡全額"""
        # 建立新客戶，設定零餘額但有綁卡
        new_partner = self.env['res.partner'].create({
            'name': 'Zero Balance Customer',
            'email': 'zero.balance@example.com',
            'ecpay_card_token': 'TEST_TOKEN_ZERO',
            'ecpay_card_last4': '1111',
        })

        self.assertEqual(new_partner.wallet_balance, 0.0)
        subscription = self._create_mock_subscription(new_partner, 500)

        # Mock ECPay API 成功回應
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            return_value={'RtnCode': 1, 'RtnMsg': 'Success', 'TradeNo': 'TEST456'}
        ):
            result = self.mixin._process_wallet_payment(subscription, 500)

        # 驗證
        self.assertTrue(result['success'])
        self.assertEqual(result['points_deducted'], 0)
        self.assertEqual(result['card_charged'], 500)

    def test_sd004_no_points_no_card_fail(self):
        """SD-004: 無點數無綁卡失敗"""
        # 客戶 B 沒有點數也沒有綁卡
        self.assertEqual(self.partner_b.wallet_balance, 0.0)
        self.assertFalse(self.partner_b.has_bound_credit_card())

        subscription = self._create_mock_subscription(self.partner_b, 500)

        # 執行扣款
        result = self.mixin._process_wallet_payment(subscription, 500)

        # 驗證失敗
        self.assertFalse(result['success'])
        self.assertEqual(result['points_deducted'], 0)
        self.assertEqual(result['card_charged'], 0)
        self.assertIn('未綁定信用卡', result['error'])

        # 驗證交易記錄為失敗狀態
        if result['transaction']:
            self.assertEqual(result['transaction'].state, 'failed')

    def test_sd005_card_charge_failed_retry(self):
        """SD-005: 信用卡扣款失敗重試"""
        # 設定客戶有綁卡但餘額不足
        new_partner = self.env['res.partner'].create({
            'name': 'Card Fail Customer',
            'email': 'card.fail@example.com',
            'ecpay_card_token': 'TEST_TOKEN_FAIL',
            'ecpay_card_last4': '9999',
        })

        subscription = self._create_mock_subscription(new_partner, 500)

        # Mock ECPay API 失敗回應
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            return_value={'RtnCode': 0, 'RtnMsg': '餘額不足'}
        ):
            result = self.mixin._process_wallet_payment(subscription, 500)

        # 驗證失敗
        self.assertFalse(result['success'])
        self.assertIn('餘額不足', result['error'])

        # 驗證交易記錄
        if result['transaction']:
            self.assertEqual(result['transaction'].state, 'failed')
            self.assertEqual(result['transaction'].card_charge_status, 'failed')

    def test_sd006_cron_batch_deduct(self):
        """SD-006: Cron 批次扣款"""
        # 這個測試需要實際的訂閱模型和 Cron
        # 暫時跳過，待訂閱模組整合後實作
        if 'sh.subscription.subscription' not in self.env:
            self.skipTest('sh.subscription.subscription model not found')

        # TODO: 實作 Cron 批次扣款測試
        # 1. 建立多筆到期訂閱
        # 2. 執行 Cron job
        # 3. 驗證各訂閱都有處理
        pass

    def test_no_partner_in_subscription(self):
        """測試訂閱沒有關聯客戶"""
        from datetime import date
        plan = self.env['sh.subscription.plan'].search([], limit=1)
        # Create a subscription without partner
        sub = self.env['sh.subscription.subscription'].create({
            'sh_subscription_plan_id': plan.id,
            'sh_plan_price': 500.0,
            'sh_recurrency': plan.sh_duration,
            'sh_unit': plan.sh_unit,
            'sh_no_of_billing_cycle': plan.sh_no_of_billing_cycle,
            'product_id': self.product.id,
            'sh_qty': 1,
            'sh_start_date': date.today(),
            'sh_date_of_next_payment': date.today(),
        })

        result = self.mixin._process_wallet_payment(sub, 500)

        self.assertFalse(result['success'])
        self.assertIn('沒有關聯客戶', result['error'])

    def test_partial_points_deduction(self):
        """測試部分點數扣除"""
        # 設定客戶有 300 點，訂閱費 500
        new_partner = self.env['res.partner'].create({
            'name': 'Partial Points Customer',
            'email': 'partial@example.com',
            'ecpay_card_token': 'TEST_PARTIAL',
            'ecpay_card_last4': '5555',
        })
        new_partner.add_wallet_points(300, '初始儲值')

        subscription = self._create_mock_subscription(new_partner, 500)

        # Mock ECPay API 成功
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            return_value={'RtnCode': 1, 'RtnMsg': 'Success', 'TradeNo': 'PARTIAL123'}
        ):
            result = self.mixin._process_wallet_payment(subscription, 500)

        # 驗證：扣 300 點 + 信用卡扣 200
        self.assertTrue(result['success'])
        self.assertEqual(result['points_deducted'], 300)
        self.assertEqual(result['card_charged'], 200)
        loyalty_card = new_partner.get_wallet_loyalty_card()
        loyalty_card.flush_recordset(['points'])
        self.env.invalidate_all()
        self.assertEqual(new_partner.wallet_balance, 0.0)
