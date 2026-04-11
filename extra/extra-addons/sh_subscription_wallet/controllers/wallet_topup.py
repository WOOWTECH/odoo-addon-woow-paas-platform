# -*- coding: utf-8 -*-
from odoo import http, _
from odoo.http import request
import logging

_logger = logging.getLogger(__name__)


class WalletTopup(http.Controller):

    @http.route(['/my/wallet/topup'], type='http', auth='user')
    def wallet_topup_page(self, **kw):
        """點數儲值頁面"""
        partner = request.env.user.partner_id

        # 預設儲值金額選項
        topup_options = [
            {'amount': 500, 'bonus': 0, 'label': 'NT$ 500'},
            {'amount': 1000, 'bonus': 50, 'label': 'NT$ 1,000 (+50 點)'},
            {'amount': 3000, 'bonus': 200, 'label': 'NT$ 3,000 (+200 點)'},
            {'amount': 5000, 'bonus': 400, 'label': 'NT$ 5,000 (+400 點)'},
            {'amount': 10000, 'bonus': 1000, 'label': 'NT$ 10,000 (+1,000 點)'},
        ]

        # 取得 ECPay Provider
        ecpay_provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'ECPay'),
            ('state', '!=', 'disabled'),
        ], limit=1)

        values = {
            'partner': partner,
            'wallet_balance': partner.wallet_balance,
            'topup_options': topup_options,
            'ecpay_available': bool(ecpay_provider),
            'page_name': 'wallet_topup',
        }
        return request.render('sh_subscription_wallet.portal_wallet_topup', values)

    @http.route(['/my/wallet/topup/submit'], type='http', auth='user', methods=['POST'])
    def wallet_topup_submit(self, amount=None, **kw):
        """提交儲值請求"""
        partner = request.env.user.partner_id

        try:
            amount = float(amount)
            if amount <= 0:
                return request.redirect('/my/wallet/topup?error=invalid_amount')
        except (TypeError, ValueError):
            return request.redirect('/my/wallet/topup?error=invalid_amount')

        # 取得 ECPay Provider
        ecpay_provider = request.env['payment.provider'].sudo().search([
            ('code', '=', 'ECPay'),
            ('state', '!=', 'disabled'),
        ], limit=1)

        if not ecpay_provider:
            return request.redirect('/my/wallet/topup?error=payment_unavailable')

        # 建立付款交易
        payment_tx = request.env['payment.transaction'].sudo().create({
            'provider_id': ecpay_provider.id,
            'amount': amount,
            'currency_id': request.env.company.currency_id.id,
            'partner_id': partner.id,
            'reference': f"WALLET-{partner.id}-{request.env['ir.sequence'].next_by_code('wallet.topup')}",
            'operation': 'online_direct',
        })

        # 儲存交易 ID 到 session，用於回調處理
        request.session['wallet_topup_tx_id'] = payment_tx.id

        # 導向 ECPay 付款頁面
        return request.redirect(f'/payment/ecpay/checkout?tx_id={payment_tx.id}&wallet_topup=1')

    @http.route(['/my/wallet/topup/callback'], type='http', auth='public', csrf=False)
    def wallet_topup_callback(self, **kw):
        """ECPay 付款回調"""
        _logger.info("Wallet topup callback received")

        tx_id = kw.get('tx_id') or request.session.get('wallet_topup_tx_id')
        if not tx_id:
            return request.redirect('/my/sh_subscription?error=tx_not_found')

        try:
            tx_id = int(tx_id)
        except (TypeError, ValueError):
            return request.redirect('/my/sh_subscription?error=tx_not_found')

        payment_tx = request.env['payment.transaction'].sudo().browse(tx_id)
        if not payment_tx.exists():
            return request.redirect('/my/sh_subscription?error=tx_not_found')

        # 防重播：檢查此交易是否已完成點數加值
        existing_topup = request.env['wallet.transaction'].sudo().search([
            ('payment_transaction_id', '=', payment_tx.id),
            ('transaction_type', '=', 'topup'),
            ('state', '=', 'done'),
        ], limit=1)
        if existing_topup:
            _logger.info(f"Wallet topup already credited for tx {payment_tx.id}, skipping")
            # 清除 session
            request.session.pop('wallet_topup_tx_id', None)
            return request.redirect('/my/sh_subscription?success=topup')

        # 檢查付款狀態
        if payment_tx.state == 'done':
            # 付款成功，加點數
            partner = payment_tx.partner_id

            # 所有權驗證：若使用者已登入，確認交易屬於該使用者
            if request.env.user and not request.env.user._is_public():
                if partner.id != request.env.user.partner_id.id:
                    _logger.warning(
                        f"Wallet topup ownership mismatch: tx partner={partner.id}, "
                        f"user partner={request.env.user.partner_id.id}"
                    )
                    return request.redirect('/my/sh_subscription?error=access_denied')

            amount = payment_tx.amount

            # 計算贈送點數
            bonus = self._calculate_bonus(amount)
            total_points = amount + bonus

            # 加點數到錢包
            partner.add_wallet_points(
                total_points,
                description=_('儲值 NT$%.0f (含贈送 %d 點)') % (amount, bonus) if bonus else _('儲值 NT$%.0f') % amount,
                payment_transaction=payment_tx,
            )

            # 清除 session
            request.session.pop('wallet_topup_tx_id', None)

            return request.redirect('/my/sh_subscription?success=topup')
        elif payment_tx.state in ('pending', 'authorized'):
            return request.redirect('/my/sh_subscription?pending=topup')
        else:
            return request.redirect('/my/sh_subscription?error=payment_failed')

    def _calculate_bonus(self, amount):
        """計算贈送點數"""
        bonus_rules = [
            (10000, 1000),
            (5000, 400),
            (3000, 200),
            (1000, 50),
        ]
        for threshold, bonus in bonus_rules:
            if amount >= threshold:
                return bonus
        return 0
