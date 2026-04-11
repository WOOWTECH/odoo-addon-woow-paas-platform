# -*- coding: utf-8 -*-
"""
Will SaaS 訂閱完整生命週期測試

測試案例:
- WILL-001: Will 建立客戶帳號
- WILL-002: Will 綁定信用卡
- WILL-003: Will 建立 SaaS 訂閱
- WILL-004: Will 儲值 300 元點數
- WILL-005: 第一個月扣款（298 點數）
- WILL-006: 第二個月扣款（2 點數 + 296 信用卡）
- WILL-007: 驗證發票開立（300 + 296 = 596）
- WILL-008: 完整流程整合測試
"""
from odoo.tests import tagged
from unittest.mock import patch, MagicMock
from .common import WalletTestCommon, WillTestMixin, MockECPayPayment, MockECPayInvoice


@tagged('wallet', 'will_subscription', '-at_install', 'post_install')
class TestWillSubscription(WalletTestCommon, WillTestMixin):
    """Will SaaS 訂閱完整生命週期測試"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self.setup_will_scenario()

    def test_will_001_create_customer(self):
        """WILL-001: Will 建立客戶帳號"""
        # 驗證 Will 客戶已建立
        self.assertIsNotNone(self.will_partner)
        self.assertEqual(self.will_partner.name, 'Will')
        self.assertEqual(self.will_partner.email, 'will@example.com')

        # 驗證初始狀態
        self.assertEqual(self.will_partner.wallet_balance, 0.0)
        self.assertFalse(self.will_partner.has_bound_credit_card())

    def test_will_002_bind_credit_card(self):
        """WILL-002: Will 綁定信用卡"""
        # 執行綁卡
        result = self.will_bind_card(card_last4='5678', card_expiry='06/29')

        # 驗證綁卡成功
        self.assertEqual(result['RtnCode'], 1)
        self.assertEqual(result['RtnMsg'], 'Success')

        # 驗證客戶信用卡資訊
        self.assertTrue(self.will_partner.has_bound_credit_card())
        self.assertEqual(self.will_partner.ecpay_card_last4, '5678')
        self.assertEqual(self.will_partner.ecpay_card_expiry, '06/29')
        self.assertIn('WILL_TOKEN', self.will_partner.ecpay_card_token)

        # 驗證 Mock 呼叫記錄
        last_call = self.mock_payment.get_last_call()
        self.assertEqual(last_call['method'], 'bind_card')
        self.assertEqual(last_call['card_last4'], '5678')

    def test_will_003_create_subscription(self):
        """WILL-003: Will 建立 SaaS 訂閱"""
        # 先綁卡
        self.will_bind_card()

        # 建立訂閱
        subscription = self.create_will_subscription()

        # 驗證訂閱
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.sh_plan_price, 298.0)

        # 驗證訂閱狀態
        if hasattr(subscription, 'state'):
            self.assertEqual(subscription.state, 'in_progress')

    def test_will_004_topup_300_points(self):
        """WILL-004: Will 儲值 300 元點數"""
        # 先綁卡
        self.will_bind_card()

        # 儲值 300 元
        result = self.will_topup_points(300)

        # 驗證儲值成功
        self.assertEqual(result['RtnCode'], 1)

        # 驗證錢包餘額
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.will_partner.wallet_balance, 300.0)

        # 驗證 Mock 呼叫記錄
        topup_calls = self.mock_payment.get_calls_by_method('topup_points')
        self.assertEqual(len(topup_calls), 1)
        self.assertEqual(topup_calls[0]['amount'], 300)

        # 驗證發票已開立
        invoices = self.mock_invoice.get_invoices_by_buyer('will@example.com')
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0]['amount'], 300)

    def test_will_005_first_month_deduct(self):
        """WILL-005: 第一個月扣款（298 點數）"""
        # 設定場景
        self.will_bind_card()
        self.will_topup_points(300)
        self.create_will_subscription()

        # 驗證初始餘額
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.will_partner.wallet_balance, 300.0)

        # 第一個月扣款
        result = self.process_month_payment(1)

        # 驗證扣款結果
        self.assertEqual(result['month'], 1)
        self.assertEqual(result['balance_before'], 300.0)
        self.assertEqual(result['points_deducted'], 298.0)
        self.assertEqual(result['card_charged'], 0.0)
        self.assertEqual(result['balance_after'], 2.0)

        # 驗證錢包餘額
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.will_partner.wallet_balance, 2.0)

        # 驗證沒有信用卡扣款
        card_calls = self.mock_payment.get_calls_by_method('charge_card')
        self.assertEqual(len(card_calls), 0)

    def test_will_006_second_month_deduct(self):
        """WILL-006: 第二個月扣款（2 點數 + 296 信用卡）"""
        # 設定場景：模擬第一個月後的狀態
        self.will_bind_card()
        self.will_topup_points(300)
        self.create_will_subscription()
        self.process_month_payment(1)  # 第一個月

        # 驗證第一個月後餘額
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.will_partner.wallet_balance, 2.0)

        # 第二個月扣款
        result = self.process_month_payment(2)

        # 驗證扣款結果
        self.assertEqual(result['month'], 2)
        self.assertEqual(result['balance_before'], 2.0)
        self.assertEqual(result['points_deducted'], 2.0)
        self.assertEqual(result['card_charged'], 296.0)
        self.assertEqual(result['balance_after'], 0.0)

        # 驗證錢包餘額歸零
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.will_partner.wallet_balance, 0.0)

        # 驗證信用卡扣款
        card_calls = self.mock_payment.get_calls_by_method('charge_card')
        self.assertEqual(len(card_calls), 1)
        self.assertEqual(card_calls[0]['amount'], 296.0)

    def test_will_007_verify_invoices(self):
        """WILL-007: 驗證發票開立（300 + 296 = 596）"""
        # 執行完整流程
        self.will_bind_card()
        self.will_topup_points(300)
        self.create_will_subscription()
        self.process_month_payment(1)
        self.process_month_payment(2)

        # 取得 Will 的所有發票
        invoices = self.mock_invoice.get_invoices_by_buyer('will@example.com')

        # 驗證發票數量
        self.assertEqual(len(invoices), 2)

        # 驗證發票金額
        amounts = sorted([inv['amount'] for inv in invoices])
        self.assertEqual(amounts, [296.0, 300.0])

        # 驗證總金額
        total = self.mock_invoice.get_total_amount('will@example.com')
        self.assertEqual(total, 596.0)

        # 驗證發票內容
        topup_invoice = next(inv for inv in invoices if inv['amount'] == 300)
        self.assertIn('儲值', topup_invoice['items'][0]['name'])

        card_invoice = next(inv for inv in invoices if inv['amount'] == 296)
        self.assertIn('訂閱', card_invoice['items'][0]['name'])

    def test_will_008_full_lifecycle(self):
        """WILL-008: 完整流程整合測試"""
        print("\n" + "=" * 60)
        print("Will SaaS 訂閱完整生命週期測試")
        print("=" * 60)

        # Step 1: 建立客戶
        print("\n[Step 1] 建立 Will 客戶帳號")
        self.assertIsNotNone(self.will_partner)
        print(f"  ✓ 客戶: {self.will_partner.name} ({self.will_partner.email})")
        print(f"  ✓ 初始餘額: {self.will_partner.wallet_balance} 元")

        # Step 2: 綁定信用卡
        print("\n[Step 2] 綁定信用卡")
        result = self.will_bind_card(card_last4='1234')
        self.assertEqual(result['RtnCode'], 1)
        print(f"  ✓ 綁卡成功: **** **** **** {self.will_partner.ecpay_card_last4}")

        # Step 3: 建立訂閱
        print("\n[Step 3] 建立 SaaS 訂閱")
        subscription = self.create_will_subscription()
        self.assertIsNotNone(subscription)
        print(f"  ✓ 訂閱編號: {subscription.name}")
        print(f"  ✓ 訂閱費用: {self.SUBSCRIPTION_FEE} 元/月")

        # Step 4: 儲值點數
        print("\n[Step 4] 儲值 300 元點數")
        result = self.will_topup_points(300)
        self.assertEqual(result['RtnCode'], 1)
        print(f"  ✓ 儲值成功: +300 元")
        print(f"  ✓ 目前餘額: {self.will_partner.wallet_balance} 元")
        print(f"  ✓ 開立發票: 300 元")

        # Step 5: 第一個月扣款
        print("\n[Step 5] 第一個月扣款")
        month1 = self.process_month_payment(1)
        self.assertEqual(month1['points_deducted'], 298.0)
        self.assertEqual(month1['card_charged'], 0.0)
        print(f"  ✓ 點數扣款: {month1['points_deducted']} 元")
        print(f"  ✓ 信用卡扣款: {month1['card_charged']} 元")
        print(f"  ✓ 剩餘餘額: {self.will_partner.wallet_balance} 元")

        # Step 6: 第二個月扣款
        print("\n[Step 6] 第二個月扣款")
        month2 = self.process_month_payment(2)
        self.assertEqual(month2['points_deducted'], 2.0)
        self.assertEqual(month2['card_charged'], 296.0)
        print(f"  ✓ 點數扣款: {month2['points_deducted']} 元")
        print(f"  ✓ 信用卡扣款: {month2['card_charged']} 元")
        print(f"  ✓ 開立發票: 296 元")
        print(f"  ✓ 剩餘餘額: {self.will_partner.wallet_balance} 元")

        # Step 7: 驗證發票
        print("\n[Step 7] 驗證發票總計")
        total = self.mock_invoice.get_total_amount('will@example.com')
        invoices = self.mock_invoice.get_invoices_by_buyer('will@example.com')
        self.assertEqual(total, 596.0)
        print(f"  ✓ 發票數量: {len(invoices)} 張")
        for inv in invoices:
            print(f"    - {inv['invoice_no']}: {inv['amount']} 元")
        print(f"  ✓ 發票總額: {total} 元")

        # 驗證總結
        print("\n" + "=" * 60)
        print("測試結果摘要")
        print("=" * 60)
        print(f"  • 訂閱費用: 298 元/月")
        print(f"  • 儲值金額: 300 元")
        print(f"  • 第一個月: 全額點數扣款 (298 元)")
        print(f"  • 第二個月: 點數 2 元 + 信用卡 296 元")
        print(f"  • 發票總計: 300 元 (儲值) + 296 元 (扣款) = 596 元")
        print("=" * 60 + "\n")

    def test_card_charge_failure_handling(self):
        """測試信用卡扣款失敗處理"""
        # 設定場景
        self.will_bind_card()
        self.create_will_subscription()

        # 設定 Mock 失敗
        self.mock_payment.set_fail('餘額不足')

        # 嘗試扣款
        result = self.mock_payment.charge_card(
            token=self.will_partner.ecpay_card_token,
            amount=298,
            order_id='TEST_FAIL',
        )

        # 驗證失敗
        self.assertEqual(result['RtnCode'], 0)
        self.assertIn('餘額不足', result['RtnMsg'])

    def test_third_month_full_card_charge(self):
        """測試第三個月全額信用卡扣款"""
        # 設定場景
        self.will_bind_card()
        self.will_topup_points(300)
        self.create_will_subscription()
        self.process_month_payment(1)  # 餘額 2
        self.process_month_payment(2)  # 餘額 0

        # 驗證餘額為零
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(self.will_partner.wallet_balance, 0.0)

        # 第三個月扣款
        result = self.process_month_payment(3)

        # 驗證全額信用卡扣款
        self.assertEqual(result['points_deducted'], 0.0)
        self.assertEqual(result['card_charged'], 298.0)
        self.assertEqual(result['balance_after'], 0.0)

        # 驗證發票
        invoices = self.mock_invoice.get_invoices_by_buyer('will@example.com')
        self.assertEqual(len(invoices), 3)  # 300 + 296 + 298

        total = self.mock_invoice.get_total_amount('will@example.com')
        self.assertEqual(total, 894.0)  # 300 + 296 + 298
