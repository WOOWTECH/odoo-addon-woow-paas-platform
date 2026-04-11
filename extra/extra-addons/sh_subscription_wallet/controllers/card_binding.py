# -*- coding: utf-8 -*-
from odoo import http, _, fields
from odoo.http import request
import logging
import hashlib
import time
import urllib.parse

_logger = logging.getLogger(__name__)


class CardBinding(http.Controller):

    @http.route(['/my/wallet/bind_card'], type='http', auth='user')
    def bind_card_page(self, **kw):
        """信用卡綁定頁面"""
        partner = request.env.user.partner_id

        # 記住來源訂閱，以便「返回訂閱」能導回正確頁面
        subscription_id = kw.get('subscription_id')
        if subscription_id:
            request.session['bind_card_subscription_id'] = int(subscription_id)
        back_subscription_id = request.session.get('bind_card_subscription_id')

        values = {
            'partner': partner,
            'has_bound_card': partner.has_bound_credit_card(),
            'card_last4': partner.ecpay_card_last4,
            'card_expiry': partner.ecpay_card_expiry,
            'card_bound_date': partner.ecpay_card_bound_date,
            'page_name': 'wallet_bind_card',
            'error': kw.get('error'),
            'success': kw.get('success'),
            'back_subscription_id': back_subscription_id,
        }
        return request.render('sh_subscription_wallet.portal_bind_card', values)

    @http.route(['/my/wallet/bind_card/submit'], type='http', auth='user', methods=['POST'])
    def bind_card_submit(self, **kw):
        """提交信用卡綁定請求"""
        partner = request.env.user.partner_id

        # 取得 ECPay Provider
        ecpay_provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'ECPay'),
            ('state', '!=', 'disabled'),
        ], limit=1)

        if not ecpay_provider:
            return request.redirect('/my/wallet/bind_card?error=payment_unavailable')

        try:
            # 產生 ECPay 定期定額授權請求
            merchant_trade_no = self._generate_trade_no()

            # 儲存到 session
            request.session['card_bind_trade_no'] = merchant_trade_no
            request.session['card_bind_partner_id'] = partner.id

            # 建立 ECPay 定期定額參數
            params = self._prepare_ecpay_period_params(ecpay_provider, partner, merchant_trade_no)

            # 導向 ECPay 授權頁面
            # 注意：這裡需要根據 ECPay 的實際流程來實作
            ecpay_url = self._get_ecpay_period_url(ecpay_provider)

            return request.render('sh_subscription_wallet.redirect_to_ecpay', {
                'ecpay_url': ecpay_url,
                'params': params,
            })

        except Exception as e:
            _logger.exception(f"Card binding failed for partner {partner.id}")
            return request.redirect(f'/my/wallet/bind_card?error={urllib.parse.quote(str(e))}')

    @http.route(['/my/wallet/bind_card/callback'], type='http', auth='public', csrf=False, methods=['POST', 'GET'])
    def bind_card_callback(self, **kw):
        """ECPay 信用卡綁定回調"""
        _logger.info("Card binding callback received")

        merchant_trade_no = kw.get('MerchantTradeNo') or request.session.get('card_bind_trade_no')

        if not merchant_trade_no:
            _logger.error("Card binding callback missing MerchantTradeNo")
            return request.redirect('/my/wallet/bind_card?error=invalid_callback')

        # 驗證 CheckMacValue（防止偽造回調）— 必須存在且正確
        if not kw.get('CheckMacValue') or not self._verify_callback_mac(kw):
            _logger.warning("Card binding callback: CheckMacValue missing or invalid")
            return request.redirect('/my/wallet/bind_card?error=verification_failed')

        # 安全取得 partner：優先使用已登入使用者，session 僅作備援
        partner = None
        if request.env.user and not request.env.user._is_public():
            partner = request.env.user.partner_id
        else:
            partner_id = request.session.get('card_bind_partner_id')
            if partner_id:
                partner = request.env['res.partner'].sudo().browse(partner_id)

        if not partner or not partner.exists():
            _logger.error("Card binding callback: cannot identify partner")
            return request.redirect('/my/wallet/bind_card?error=partner_not_found')

        # 檢查回調狀態
        rtn_code = kw.get('RtnCode')

        if rtn_code == '1':
            # 綁定成功
            card_info = {
                'ecpay_card_token': kw.get('Token') or kw.get('PlatformID') or merchant_trade_no,
                'ecpay_card_last4': kw.get('Card4No', '****'),
                'ecpay_card_expiry': kw.get('CardValidDate', ''),
                'ecpay_card_bound_date': fields.Datetime.now(),
            }
            partner.sudo().write(card_info)

            # 記錄綁定交易
            request.env['wallet.transaction'].sudo().create({
                'partner_id': partner.id,
                'transaction_type': 'card_bind',
                'amount': 0,
                'description': _('綁定信用卡 (末四碼: %s)') % card_info['ecpay_card_last4'],
                'ecpay_merchant_trade_no': merchant_trade_no,
                'state': 'done',
            })

            # 清除 session
            request.session.pop('card_bind_trade_no', None)
            request.session.pop('card_bind_partner_id', None)

            _logger.info(f"Card binding success for partner {partner.id}")
            return request.redirect('/my/wallet/bind_card?success=1')
        else:
            error_msg = kw.get('RtnMsg', _('綁定失敗'))
            _logger.warning(f"Card binding failed for partner {partner.id}: {error_msg}")
            return request.redirect(f'/my/wallet/bind_card?error={urllib.parse.quote(error_msg)}')

    @http.route(['/my/wallet/unbind_card'], type='http', auth='user', methods=['POST'])
    def unbind_card(self, **kw):
        """解除信用卡綁定"""
        partner = request.env.user.partner_id

        if partner.has_bound_credit_card():
            partner.action_unbind_credit_card()
            return request.redirect('/my/wallet/bind_card?success=unbind')

        return request.redirect('/my/wallet/bind_card?error=no_card')

    def _generate_trade_no(self):
        """產生交易編號"""
        timestamp = int(time.time() * 1000)
        return f"BIND{timestamp}"

    def _prepare_ecpay_period_params(self, provider, partner, merchant_trade_no):
        """準備 ECPay 定期定額參數"""
        base_url = request.env['ir.config_parameter'].sudo().get_param('web.base.url')

        params = {
            'MerchantID': provider.MerchantID or '',
            'MerchantTradeNo': merchant_trade_no,
            'MerchantTradeDate': time.strftime('%Y/%m/%d %H:%M:%S'),
            'PaymentType': 'aio',
            'PeriodAmount': 0,  # 綁卡時金額設為 0
            'PeriodType': 'M',  # 月
            'Frequency': 1,
            'ExecTimes': 99,
            'PeriodReturnURL': f'{base_url}/my/wallet/bind_card/callback',
            'TradeDesc': urllib.parse.quote('信用卡綁定授權'),
            'ItemName': '信用卡綁定授權',
        }

        return params

    def _get_ecpay_period_url(self, provider):
        """取得 ECPay 定期定額 URL"""
        if provider.state == 'test':
            return 'https://payment-stage.ecpay.com.tw/SP/CreateTrade'
        return 'https://payment.ecpay.com.tw/SP/CreateTrade'

    def _verify_callback_mac(self, data):
        """驗證 ECPay 回調的 CheckMacValue"""
        ecpay_provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'ECPay'),
            ('state', '!=', 'disabled'),
        ], limit=1)

        if not ecpay_provider:
            return False

        check_mac = data.get('CheckMacValue', '')
        params = {k: v for k, v in data.items() if k != 'CheckMacValue'}

        # 按 key 排序 → 組裝字串 → 加 HashKey/HashIV → SHA256
        sorted_params = sorted(params.items(), key=lambda x: x[0].lower())
        raw = '&'.join(f'{k}={v}' for k, v in sorted_params)
        raw = f"HashKey={ecpay_provider.HashKey}&{raw}&HashIV={ecpay_provider.HashIV}"

        encoded = urllib.parse.quote_plus(raw).lower()
        computed_mac = hashlib.sha256(encoded.encode('utf-8')).hexdigest().upper()

        return computed_mac == check_mac
