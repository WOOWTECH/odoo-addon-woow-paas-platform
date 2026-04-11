# -*- coding: utf-8 -*-
"""
ECPay Token 扣款測試

測試案例:
- ET-001: Token 扣款成功
- ET-002: Token 扣款失敗
- ET-003: 儲存綁定資訊
- ET-004: 解除綁定
"""
from odoo.tests import tagged
from odoo.exceptions import UserError
from unittest.mock import patch
from .common import WalletTestCommon


@tagged('wallet', 'ecpay_token', '-at_install', 'post_install')
class TestECPayToken(WalletTestCommon):
    """ECPay Token 扣款測試"""

    def setUp(self):
        super().setUp()
        self.mixin = self.env['subscription.wallet.mixin']

    def test_et001_token_charge_success(self):
        """ET-001: Token 扣款成功"""
        # 設定客戶已綁卡
        self.assertTrue(self.partner_a.ecpay_card_token)

        # Mock ECPay API 成功回應
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            return_value={'RtnCode': 1, 'RtnMsg': 'Success', 'TradeNo': 'ECPAY123456'}
        ):
            result = self.mixin._charge_ecpay_token(self.partner_a, 500)

        # 驗證成功
        self.assertTrue(result['success'])
        self.assertEqual(result['trade_no'], 'ECPAY123456')
        self.assertTrue(result['merchant_trade_no'])

    def test_et002_token_charge_failed(self):
        """ET-002: Token 扣款失敗"""
        # 設定客戶已綁卡
        self.assertTrue(self.partner_a.ecpay_card_token)

        # Mock ECPay API 失敗回應
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            return_value={'RtnCode': 0, 'RtnMsg': '信用卡授權失敗'}
        ):
            result = self.mixin._charge_ecpay_token(self.partner_a, 500)

        # 驗證失敗
        self.assertFalse(result['success'])
        self.assertIn('授權失敗', result['error'])

    def test_et002_token_charge_no_token(self):
        """ET-002b: 沒有 Token 的扣款失敗"""
        # 客戶 B 沒有綁卡
        self.assertFalse(self.partner_b.ecpay_card_token)

        result = self.mixin._charge_ecpay_token(self.partner_b, 500)

        # 驗證失敗
        self.assertFalse(result['success'])
        self.assertIn('未綁定信用卡', result['error'])

    def test_et003_save_binding_info(self):
        """ET-003: 儲存綁定資訊"""
        # 建立新客戶
        new_partner = self.env['res.partner'].create({
            'name': 'Binding Test Customer',
            'email': 'binding.test@example.com',
        })

        # 模擬 ECPay 綁定成功後的資料儲存
        binding_data = {
            'ecpay_card_token': 'NEW_TOKEN_789',
            'ecpay_card_last4': '1234',
            'ecpay_card_expiry': '06/28',
            'ecpay_card_bound_date': '2026-02-10 15:30:00',
        }

        new_partner.write(binding_data)

        # 驗證資料儲存正確
        self.assertEqual(new_partner.ecpay_card_token, 'NEW_TOKEN_789')
        self.assertEqual(new_partner.ecpay_card_last4, '1234')
        self.assertEqual(new_partner.ecpay_card_expiry, '06/28')
        self.assertTrue(new_partner.has_bound_credit_card())

        # 記錄綁卡交易
        self.env['wallet.transaction'].create({
            'partner_id': new_partner.id,
            'transaction_type': 'card_bind',
            'amount': 0,
            'description': '信用卡綁定 ****1234',
        })

        # 驗證交易記錄
        bind_trans = self.env['wallet.transaction'].search([
            ('partner_id', '=', new_partner.id),
            ('transaction_type', '=', 'card_bind'),
        ])
        self.assertTrue(bind_trans)

    def test_et004_unbind_card(self):
        """ET-004: 解除綁定"""
        # 確保客戶 A 已綁卡
        self.assertTrue(self.partner_a.ecpay_card_token)

        # 解除綁定
        result = self.partner_a.action_unbind_credit_card()

        # 驗證
        self.assertTrue(result)
        self.assertFalse(self.partner_a.ecpay_card_token)
        self.assertFalse(self.partner_a.ecpay_card_last4)
        self.assertFalse(self.partner_a.ecpay_card_expiry)
        self.assertFalse(self.partner_a.has_bound_credit_card())

        # 驗證解綁交易記錄
        unbind_trans = self.env['wallet.transaction'].search([
            ('partner_id', '=', self.partner_a.id),
            ('transaction_type', '=', 'card_unbind'),
        ], order='id desc', limit=1)

        self.assertTrue(unbind_trans)
        self.assertEqual(unbind_trans.amount, 0)

    def test_unbind_without_card_raises_error(self):
        """測試未綁卡時解除綁定應拋出錯誤"""
        # 客戶 B 沒有綁卡
        self.assertFalse(self.partner_b.ecpay_card_token)

        with self.assertRaises(UserError):
            self.partner_b.action_unbind_credit_card()

    def test_has_bound_credit_card(self):
        """測試 has_bound_credit_card 方法"""
        # 有綁卡
        self.assertTrue(self.partner_a.has_bound_credit_card())

        # 沒有綁卡
        self.assertFalse(self.partner_b.has_bound_credit_card())

    def test_generate_ecpay_trade_no(self):
        """測試交易編號產生"""
        trade_no = self.mixin._generate_ecpay_trade_no()

        # 驗證格式
        self.assertTrue(trade_no.startswith('SW'))
        self.assertGreater(len(trade_no), 10)

        # 驗證唯一性
        trade_no_2 = self.mixin._generate_ecpay_trade_no()
        self.assertNotEqual(trade_no, trade_no_2)

    def test_charge_with_test_provider(self):
        """測試使用測試環境 Provider 的扣款"""
        # 確保 provider 是測試模式
        self.ecpay_provider.state = 'test'

        # 使用實際的 _call_ecpay_period_charge_api（測試模式應回傳模擬成功）
        result = self.mixin._call_ecpay_period_charge_api(
            provider=self.ecpay_provider,
            partner=self.partner_a,
            amount=100,
            merchant_trade_no='TEST_TRADE_001',
        )

        # 測試模式應回傳成功
        self.assertEqual(result['RtnCode'], 1)
        self.assertEqual(result['RtnMsg'], 'Success')

    def test_exception_handling_in_charge(self):
        """測試扣款時的例外處理"""
        # Mock 拋出例外
        with self._mock_ecpay_provider_search(), \
             patch.object(
            type(self.mixin), '_call_ecpay_period_charge_api',
            side_effect=Exception('Network error')
        ):
            result = self.mixin._charge_ecpay_token(self.partner_a, 500)

        # 應該捕獲例外並回傳失敗
        self.assertFalse(result['success'])
        self.assertIn('Network error', result['error'])
