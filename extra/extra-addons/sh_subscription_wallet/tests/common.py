# -*- coding: utf-8 -*-
from odoo.tests import TransactionCase, tagged
from unittest.mock import MagicMock
from datetime import datetime, timedelta


class MockECPayPayment:
    """ECPay 付款 API 模擬器"""

    def __init__(self):
        self.call_history = []
        self.should_succeed = True
        self.fail_message = None

    def bind_card(self, partner_id, card_token, card_last4, card_expiry):
        """模擬綁定信用卡"""
        call_record = {
            'method': 'bind_card',
            'partner_id': partner_id,
            'card_token': card_token,
            'card_last4': card_last4,
            'card_expiry': card_expiry,
            'timestamp': datetime.now().isoformat(),
        }
        self.call_history.append(call_record)

        if self.should_succeed:
            return {
                'RtnCode': 1,
                'RtnMsg': 'Success',
                'Token': card_token,
            }
        return {
            'RtnCode': 0,
            'RtnMsg': self.fail_message or '綁卡失敗',
        }

    def charge_card(self, token, amount, order_id, description=''):
        """模擬信用卡扣款"""
        call_record = {
            'method': 'charge_card',
            'token': token,
            'amount': amount,
            'order_id': order_id,
            'description': description,
            'timestamp': datetime.now().isoformat(),
        }
        self.call_history.append(call_record)

        if self.should_succeed:
            return {
                'RtnCode': 1,
                'RtnMsg': 'Success',
                'TradeNo': f'MOCK_{order_id}_{datetime.now().strftime("%Y%m%d%H%M%S")}',
                'Amount': amount,
            }
        return {
            'RtnCode': 0,
            'RtnMsg': self.fail_message or '扣款失敗',
        }

    def topup_points(self, partner_id, amount, payment_method='credit_card'):
        """模擬儲值點數付款"""
        call_record = {
            'method': 'topup_points',
            'partner_id': partner_id,
            'amount': amount,
            'payment_method': payment_method,
            'timestamp': datetime.now().isoformat(),
        }
        self.call_history.append(call_record)

        if self.should_succeed:
            return {
                'RtnCode': 1,
                'RtnMsg': 'Success',
                'TradeNo': f'TOPUP_{partner_id}_{datetime.now().strftime("%Y%m%d%H%M%S")}',
            }
        return {
            'RtnCode': 0,
            'RtnMsg': self.fail_message or '儲值失敗',
        }

    def set_fail(self, message='模擬失敗'):
        """設定下次呼叫失敗"""
        self.should_succeed = False
        self.fail_message = message

    def set_success(self):
        """設定下次呼叫成功"""
        self.should_succeed = True
        self.fail_message = None

    def get_last_call(self):
        """取得最後一次呼叫記錄"""
        return self.call_history[-1] if self.call_history else None

    def get_calls_by_method(self, method):
        """取得指定方法的所有呼叫"""
        return [c for c in self.call_history if c['method'] == method]


class MockECPayInvoice:
    """ECPay 發票 API 模擬器"""

    def __init__(self):
        self.issued_invoices = []
        self.should_succeed = True
        self.fail_message = None
        self._invoice_counter = 1

    def issue_invoice(self, buyer_name, buyer_email, amount, items, tax_type='1'):
        """模擬開立發票"""
        invoice_record = {
            'invoice_no': f'AA{self._invoice_counter:08d}',
            'buyer_name': buyer_name,
            'buyer_email': buyer_email,
            'amount': amount,
            'items': items,
            'tax_type': tax_type,
            'issue_date': datetime.now().isoformat(),
            'status': 'issued' if self.should_succeed else 'failed',
        }

        if self.should_succeed:
            self._invoice_counter += 1
            self.issued_invoices.append(invoice_record)
            return {
                'RtnCode': 1,
                'RtnMsg': 'Success',
                'InvoiceNo': invoice_record['invoice_no'],
                'InvoiceDate': datetime.now().strftime('%Y-%m-%d'),
            }
        return {
            'RtnCode': 0,
            'RtnMsg': self.fail_message or '開立發票失敗',
        }

    def void_invoice(self, invoice_no, reason=''):
        """模擬作廢發票"""
        for inv in self.issued_invoices:
            if inv['invoice_no'] == invoice_no:
                inv['status'] = 'voided'
                inv['void_reason'] = reason
                inv['void_date'] = datetime.now().isoformat()
                return {
                    'RtnCode': 1,
                    'RtnMsg': 'Success',
                }
        return {
            'RtnCode': 0,
            'RtnMsg': '發票不存在',
        }

    def get_invoice(self, invoice_no):
        """查詢發票"""
        for inv in self.issued_invoices:
            if inv['invoice_no'] == invoice_no:
                return inv
        return None

    def get_invoices_by_buyer(self, buyer_email):
        """取得買家的所有發票"""
        return [inv for inv in self.issued_invoices if inv['buyer_email'] == buyer_email]

    def get_total_amount(self, buyer_email=None):
        """取得發票總金額"""
        invoices = self.issued_invoices
        if buyer_email:
            invoices = self.get_invoices_by_buyer(buyer_email)
        return sum(inv['amount'] for inv in invoices if inv['status'] == 'issued')

    def set_fail(self, message='開立失敗'):
        """設定下次呼叫失敗"""
        self.should_succeed = False
        self.fail_message = message

    def set_success(self):
        """設定下次呼叫成功"""
        self.should_succeed = True
        self.fail_message = None


class WillTestMixin:
    """Will 測試場景的共用方法"""

    WILL_EMAIL = 'will@example.com'
    WILL_NAME = 'Will'
    SUBSCRIPTION_FEE = 298.0  # 每月訂閱費
    TOPUP_AMOUNT = 300.0  # 儲值金額

    def setup_will_scenario(self):
        """設定 Will 測試場景"""
        # 建立 Will 客戶
        self.will_partner = self.env['res.partner'].create({
            'name': self.WILL_NAME,
            'email': self.WILL_EMAIL,
        })

        # 初始化 Mock 物件
        self.mock_payment = MockECPayPayment()
        self.mock_invoice = MockECPayInvoice()

        # 建立 SaaS 訂閱產品
        self.saas_product = self.env['product.product'].create({
            'name': 'SaaS Monthly Service',
            'type': 'service',
            'list_price': self.SUBSCRIPTION_FEE,
        })

    def will_bind_card(self, card_last4='1234', card_expiry='12/28'):
        """Will 綁定信用卡"""
        token = f'WILL_TOKEN_{card_last4}'
        result = self.mock_payment.bind_card(
            partner_id=self.will_partner.id,
            card_token=token,
            card_last4=card_last4,
            card_expiry=card_expiry,
        )

        if result['RtnCode'] == 1:
            self.will_partner.write({
                'ecpay_card_token': token,
                'ecpay_card_last4': card_last4,
                'ecpay_card_expiry': card_expiry,
                'ecpay_card_bound_date': datetime.now(),
            })
        return result

    def will_topup_points(self, amount=None):
        """Will 儲值點數"""
        amount = amount or self.TOPUP_AMOUNT
        result = self.mock_payment.topup_points(
            partner_id=self.will_partner.id,
            amount=amount,
        )

        if result['RtnCode'] == 1:
            # 新增點數到錢包
            self.will_partner.add_wallet_points(amount, f'ECPay 儲值 {amount} 元')

            # 開立儲值發票
            self.mock_invoice.issue_invoice(
                buyer_name=self.WILL_NAME,
                buyer_email=self.WILL_EMAIL,
                amount=amount,
                items=[{'name': 'eWallet 點數儲值', 'qty': 1, 'price': amount}],
            )
        return result

    def create_will_subscription(self):
        """建立 Will 的 SaaS 訂閱"""
        if 'sh.subscription.subscription' in self.env:
            # 找到或建立訂閱方案
            plan = self.env['sh.subscription.plan'].search([], limit=1)
            if not plan:
                plan = self.env['sh.subscription.plan'].create({
                    'name': 'SaaS Monthly Plan',
                    'sh_duration': 1,
                    'sh_unit': 'month',
                    'sh_plan_price': self.SUBSCRIPTION_FEE,
                    'sh_no_of_billing_cycle': 12,
                    'sh_start_immediately': True,
                })

            self.will_subscription = self.env['sh.subscription.subscription'].create({
                'sh_partner_id': self.will_partner.id,
                'sh_subscription_plan_id': plan.id,
                'sh_plan_price': self.SUBSCRIPTION_FEE,
                'sh_recurrency': plan.sh_duration,
                'sh_unit': plan.sh_unit,
                'sh_no_of_billing_cycle': plan.sh_no_of_billing_cycle,
                'product_id': self.saas_product.id,
                'sh_qty': 1,
                'sh_start_date': datetime.now().date(),
                'sh_date_of_next_payment': (datetime.now() + timedelta(days=30)).date(),
            })
            self.will_subscription.state = 'in_progress'
        else:
            # 使用模擬訂閱
            self.will_subscription = MagicMock()
            self.will_subscription.id = 1
            self.will_subscription.name = f'WILL-SaaS-MOCK'
            self.will_subscription.sh_partner_id = self.will_partner
            self.will_subscription.sh_plan_price = self.SUBSCRIPTION_FEE
            self.will_subscription.state = 'in_progress'

        return self.will_subscription

    def process_month_payment(self, month_number):
        """處理第 N 個月的扣款"""
        self.env.flush_all()
        self.env.invalidate_all()
        balance = self.will_partner.wallet_balance
        fee = self.SUBSCRIPTION_FEE

        points_to_deduct = min(balance, fee)
        card_to_charge = fee - points_to_deduct

        # 扣除點數
        if points_to_deduct > 0:
            self.will_partner.deduct_wallet_points(
                points_to_deduct,
                f'第 {month_number} 個月訂閱扣款'
            )

        # 信用卡扣差額
        if card_to_charge > 0:
            result = self.mock_payment.charge_card(
                token=self.will_partner.ecpay_card_token,
                amount=card_to_charge,
                order_id=f'SUB_{self.will_subscription.id}_M{month_number}',
                description=f'訂閱扣款 - 第 {month_number} 個月',
            )

            if result['RtnCode'] == 1:
                # 開立信用卡扣款發票
                self.mock_invoice.issue_invoice(
                    buyer_name=self.WILL_NAME,
                    buyer_email=self.WILL_EMAIL,
                    amount=card_to_charge,
                    items=[{
                        'name': f'SaaS 訂閱服務 - 第 {month_number} 個月（信用卡補差額）',
                        'qty': 1,
                        'price': card_to_charge,
                    }],
                )

        self.env.flush_all()
        self.env.invalidate_all()
        return {
            'month': month_number,
            'balance_before': balance,
            'points_deducted': points_to_deduct,
            'card_charged': card_to_charge,
            'balance_after': self.will_partner.wallet_balance,
        }


@tagged('wallet', '-at_install', 'post_install')
class WalletTestCommon(TransactionCase):
    """錢包模組測試基礎類別"""

    # True if the real payment_ecpay module is installed
    ecpay_module_installed = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # 使用已安裝的 Loyalty Program，或在不存在時建立一個新的
        existing = cls.env.ref('sh_subscription_wallet.loyalty_program_wallet', raise_if_not_found=False)
        if existing:
            cls.wallet_program = existing
        else:
            cls.wallet_program = cls.env['loyalty.program'].create({
                'name': 'Test Wallet Program',
                'program_type': 'ewallet',
                'applies_on': 'both',
                'trigger': 'auto',
                'currency_id': cls.env.company.currency_id.id,
            })
            cls.env['ir.model.data'].create({
                'name': 'loyalty_program_wallet',
                'module': 'sh_subscription_wallet',
                'model': 'loyalty.program',
                'res_id': cls.wallet_program.id,
            })

        # 建立測試客戶 A（既有客戶，有餘額）
        cls.partner_a = cls.env['res.partner'].create({
            'name': 'Test Customer A',
            'email': 'test.customer.a@example.com',
        })

        # 為客戶 A 建立 Loyalty Card 並設定初始點數
        cls.loyalty_card_a = cls.env['loyalty.card'].create({
            'program_id': cls.wallet_program.id,
            'partner_id': cls.partner_a.id,
            'points': 1000.0,
        })

        # 設定客戶 A 的信用卡綁定資訊
        cls.partner_a.write({
            'ecpay_card_token': 'TEST_TOKEN_12345',
            'ecpay_card_last4': '2222',
            'ecpay_card_expiry': '12/28',
            'ecpay_card_bound_date': '2026-02-10 10:00:00',
        })

        # 建立測試客戶 B（新客戶，無餘額、無綁卡）
        cls.partner_b = cls.env['res.partner'].create({
            'name': 'Test Customer B',
            'email': 'wallet.test.new@example.com',
        })

        # 取得測試 ECPay Provider（已存在於 DB，需設為 test 模式）
        cls.ecpay_provider = cls.env['payment.provider'].search([
            ('code', '=', 'ECPay'),
        ], limit=1)

        if cls.ecpay_provider:
            cls.ecpay_provider.state = 'test'
            cls.ecpay_module_installed = True
        else:
            # ECPay 模組未安裝 — 建立一個替代 provider 供測試用
            # 使用 code='none' 因為 'ECPay' 不在 selection 列表中
            cls.ecpay_provider = cls.env['payment.provider'].create({
                'name': 'ECPay Test (Mock)',
                'code': 'none',
                'state': 'test',
                'company_id': cls.env.company.id,
            })
            cls.ecpay_module_installed = False

        # 建立模擬訂閱用的產品
        cls.product = cls.env['product.product'].create({
            'name': 'Test Subscription Product',
            'type': 'service',
            'list_price': 500.0,
        })

    def _mock_ecpay_provider_search(self):
        """Return a context manager that patches ECPay provider search to
        return self.ecpay_provider even when code != 'ECPay'.
        Use this when ECPay module is not installed but tests need to
        bypass the provider lookup in _charge_ecpay_token."""
        from unittest.mock import patch
        original_search = type(self.ecpay_provider).search

        provider = self.ecpay_provider

        def patched_search(model_self, domain, *args, **kwargs):
            # Intercept ECPay provider lookups
            for condition in domain:
                if len(condition) == 3 and condition[0] == 'code' and condition[2] == 'ECPay':
                    return provider
            return original_search(model_self, domain, *args, **kwargs)

        return patch.object(type(self.ecpay_provider), 'search', patched_search)

    def _create_test_subscription(self, partner, amount=500.0):
        """建立測試用訂閱"""
        from datetime import date
        plan = self.env['sh.subscription.plan'].search([], limit=1)
        if not plan:
            plan = self.env['sh.subscription.plan'].create({
                'name': 'Test Plan',
                'sh_duration': 1,
                'sh_unit': 'month',
                'sh_plan_price': amount,
                'sh_no_of_billing_cycle': 12,
                'sh_start_immediately': True,
            })
        subscription = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': partner.id,
            'sh_subscription_plan_id': plan.id,
            'sh_plan_price': amount,
            'sh_recurrency': plan.sh_duration,
            'sh_unit': plan.sh_unit,
            'sh_no_of_billing_cycle': plan.sh_no_of_billing_cycle,
            'product_id': self.product.id,
            'sh_qty': 1,
            'sh_start_date': date.today(),
            'sh_date_of_next_payment': date.today(),
        })
        subscription.state = 'in_progress'
        return subscription
