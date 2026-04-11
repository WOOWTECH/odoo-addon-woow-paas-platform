# -*- coding: utf-8 -*-
import logging
import random
import time

import requests as req_lib

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SubscriptionWalletMixin(models.AbstractModel):
    """訂閱錢包扣款 Mixin - 提供點數優先扣款 + 信用卡補扣邏輯"""
    _name = 'subscription.wallet.mixin'
    _description = '訂閱錢包扣款 Mixin'

    def _process_wallet_payment(self, subscription, amount):
        """
        處理訂閱扣款：點數優先，不足部分刷信用卡

        Args:
            subscription: sh.subscription.subscription 記錄
            amount: 應扣金額

        Returns:
            dict: {
                'success': bool,
                'points_deducted': float,
                'card_charged': float,
                'error': str or None,
                'transaction': wallet.transaction record
            }
        """
        partner = subscription.sh_partner_id
        if not partner:
            return {
                'success': False,
                'points_deducted': 0,
                'card_charged': 0,
                'error': _('訂閱沒有關聯客戶'),
                'transaction': False,
            }

        wallet_balance = partner.wallet_balance
        points_to_deduct = min(wallet_balance, amount)
        card_charge_needed = amount - points_to_deduct

        _logger.info(
            f"Processing wallet payment for subscription {subscription.name}: "
            f"amount={amount}, wallet_balance={wallet_balance}, "
            f"points_to_deduct={points_to_deduct}, card_charge_needed={card_charge_needed}"
        )

        # 建立交易記錄
        transaction_vals = {
            'partner_id': partner.id,
            'transaction_type': 'deduct',
            'amount': -amount,
            'subscription_id': subscription.id,
            'description': _('訂閱扣款: %s') % subscription.name,
            'state': 'draft',
        }

        # 使用 savepoint 確保：若信用卡扣款失敗，已扣的點數能回滾
        savepoint = self.env.cr.savepoint()

        try:
            # 步驟 1: 扣除點數
            if points_to_deduct > 0:
                partner.deduct_wallet_points(
                    points_to_deduct,
                    description=_('訂閱扣款 (點數): %s') % subscription.name,
                    subscription=subscription,
                )

            # 步驟 2: 如果需要信用卡補扣
            if card_charge_needed > 0:
                if not partner.has_bound_credit_card():
                    # 沒有綁定信用卡 → 回滾點數扣除
                    savepoint.rollback()
                    transaction_vals.update({
                        'card_charge_amount': card_charge_needed,
                        'card_charge_status': 'failed',
                        'card_charge_error': _('點數不足且未綁定信用卡'),
                        'state': 'failed',
                    })
                    transaction = self.env['wallet.transaction'].create(transaction_vals)

                    return {
                        'success': False,
                        'points_deducted': 0,
                        'card_charged': 0,
                        'error': _('點數不足 (餘額: %.2f)，且未綁定信用卡') % wallet_balance,
                        'transaction': transaction,
                    }

                # 嘗試信用卡扣款
                charge_result = self._charge_ecpay_token(
                    partner, card_charge_needed, subscription
                )

                if charge_result.get('success'):
                    transaction_vals.update({
                        'card_charge_amount': card_charge_needed,
                        'card_charge_status': 'success',
                        'ecpay_merchant_trade_no': charge_result.get('merchant_trade_no'),
                        'ecpay_trade_no': charge_result.get('trade_no'),
                        'state': 'done',
                        'balance_after': partner.wallet_balance,
                    })
                    # 釋放 savepoint（提交點數扣除）
                    savepoint.close(rollback=False)
                else:
                    # 信用卡扣款失敗 → 回滾點數扣除
                    savepoint.rollback()
                    transaction_vals.update({
                        'card_charge_amount': card_charge_needed,
                        'card_charge_status': 'failed',
                        'card_charge_error': charge_result.get('error'),
                        'state': 'failed',
                    })
                    transaction = self.env['wallet.transaction'].create(transaction_vals)

                    return {
                        'success': False,
                        'points_deducted': 0,
                        'card_charged': 0,
                        'error': charge_result.get('error'),
                        'transaction': transaction,
                    }
            else:
                transaction_vals.update({
                    'state': 'done',
                    'balance_after': partner.wallet_balance,
                })
                savepoint.close(rollback=False)
        except Exception:
            savepoint.rollback()
            raise

        # 建立最終交易記錄
        transaction = self.env['wallet.transaction'].create(transaction_vals)

        return {
            'success': True,
            'points_deducted': points_to_deduct,
            'card_charged': card_charge_needed,
            'error': None,
            'transaction': transaction,
        }

    def _charge_ecpay_token(self, partner, amount, subscription=None):
        """
        使用 ECPay Token 進行信用卡扣款

        Args:
            partner: res.partner 記錄
            amount: 扣款金額
            subscription: 關聯的訂閱（可選）

        Returns:
            dict: {'success': bool, 'error': str, 'merchant_trade_no': str, 'trade_no': str}
        """
        if not partner.ecpay_card_token:
            return {
                'success': False,
                'error': _('客戶未綁定信用卡'),
            }

        # 取得 ECPay Provider
        ecpay_provider = self.env['payment.provider'].sudo().search([
            ('code', '=', 'ECPay'),
            ('state', '!=', 'disabled'),
        ], limit=1)

        if not ecpay_provider:
            return {
                'success': False,
                'error': _('ECPay 付款服務未啟用'),
            }

        try:
            # 產生交易編號
            merchant_trade_no = self._generate_ecpay_trade_no()

            # 呼叫 ECPay 定期定額扣款 API
            # 注意：這裡需要根據 ECPay 的實際 API 來實作
            result = self._call_ecpay_period_charge_api(
                provider=ecpay_provider,
                partner=partner,
                amount=amount,
                merchant_trade_no=merchant_trade_no,
            )

            if result.get('RtnCode') == 1:
                # 扣款成功（交易記錄由 _process_wallet_payment 統一建立）
                return {
                    'success': True,
                    'merchant_trade_no': merchant_trade_no,
                    'trade_no': result.get('TradeNo'),
                }
            else:
                return {
                    'success': False,
                    'error': result.get('RtnMsg', _('ECPay 扣款失敗')),
                }

        except Exception as e:
            _logger.exception(f"ECPay token charge failed for partner {partner.id}")
            return {
                'success': False,
                'error': str(e),
            }

    def _generate_ecpay_trade_no(self):
        """產生 ECPay 交易編號"""
        timestamp = int(time.time() * 1000)
        random_suffix = random.randint(1000, 9999)
        return f"SW{timestamp}{random_suffix}"

    def _call_ecpay_period_charge_api(self, provider, partner, amount, merchant_trade_no):
        """
        呼叫 ECPay 定期定額執行扣款 API

        使用 payment_ecpay 模組的 SDK 透過已授權的 Token 進行扣款。
        測試環境回傳模擬成功結果。
        """
        _logger.info(
            f"Calling ECPay period charge API: partner={partner.id}, "
            f"amount={amount}, trade_no={merchant_trade_no}"
        )

        # 測試環境回傳模擬結果
        if provider.state == 'test':
            return {
                'RtnCode': 1,
                'RtnMsg': 'Success',
                'TradeNo': f"ECPAY{merchant_trade_no}",
            }

        # 正式環境：使用 ECPay SDK 執行扣款
        try:
            from odoo.addons.payment_ecpay.sdk.ecpay_payment_sdk import ECPayPaymentSdk
        except ImportError:
            _logger.error("payment_ecpay SDK not available")
            return {
                'RtnCode': 0,
                'RtnMsg': _('ECPay SDK 未安裝'),
            }

        sdk = ECPayPaymentSdk(
            MerchantID=provider.MerchantID,
            HashKey=provider.HashKey,
            HashIV=provider.HashIV,
        )

        trade_date = time.strftime('%Y/%m/%d %H:%M:%S')

        params = {
            'MerchantTradeNo': merchant_trade_no,
            'MerchantTradeDate': trade_date,
            'TotalAmount': str(int(amount)),
            'TradeDesc': 'subscription_wallet_charge',
            'ItemName': _('訂閱錢包扣款'),
            'PaymentType': 'aio',
            'ChoosePayment': 'Credit',
            'EncryptType': 1,
            'BindingCard': 1,
            'MerchantMemberID': f'member_{partner.id}',
        }

        # 若有 Token，使用 Token 扣款
        if partner.ecpay_card_token:
            api_url = (
                'https://payment.ecpay.com.tw/Cashier/AioCheckOut/V5'
                if provider.state == 'enabled'
                else 'https://payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5'
            )

            try:
                order_params = sdk.create_order(params)
                response = req_lib.post(api_url, data=order_params, timeout=30)
                response.raise_for_status()

                # ECPay 回傳格式解析
                result_data = {}
                for pair in response.text.split('&'):
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        result_data[k] = v

                rtn_code = int(result_data.get('RtnCode', 0))
                return {
                    'RtnCode': rtn_code,
                    'RtnMsg': result_data.get('RtnMsg', ''),
                    'TradeNo': result_data.get('TradeNo', ''),
                }
            except Exception as e:
                _logger.exception(
                    f"ECPay API call failed for partner {partner.id}"
                )
                return {
                    'RtnCode': 0,
                    'RtnMsg': str(e),
                }
        else:
            return {
                'RtnCode': 0,
                'RtnMsg': _('客戶未綁定信用卡 Token'),
            }
