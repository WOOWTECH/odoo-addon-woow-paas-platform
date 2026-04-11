# -*- coding: utf-8 -*-
from odoo import http, _
from odoo.http import request
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager


class WalletPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'wallet_transaction_count' in counters:
            partner = request.env.user.partner_id
            values['wallet_transaction_count'] = request.env['wallet.transaction'].sudo().search_count([
                ('partner_id', '=', partner.id)
            ])
        return values

    @http.route(['/my/wallet'], type='http', auth='user')
    def portal_wallet(self, **kw):
        """錢包首頁 — 已合併至訂閱頁面，重導向"""
        return request.redirect('/my/sh_subscription')

    @http.route(['/my/wallet/transactions', '/my/wallet/transactions/page/<int:page>'],
                type='http', auth='user')
    def portal_wallet_transactions(self, page=1, sortby=None, **kw):
        """錢包交易記錄"""
        partner = request.env.user.partner_id
        WalletTransaction = request.env['wallet.transaction'].sudo()

        domain = [('partner_id', '=', partner.id)]

        # 排序
        searchbar_sortings = {
            'date': {'label': _('日期'), 'order': 'create_date desc'},
            'amount': {'label': _('金額'), 'order': 'amount desc'},
            'type': {'label': _('類型'), 'order': 'transaction_type'},
        }
        if not sortby:
            sortby = 'date'
        order = searchbar_sortings[sortby]['order']

        # 計數和分頁
        transaction_count = WalletTransaction.search_count(domain)
        pager = portal_pager(
            url='/my/wallet/transactions',
            total=transaction_count,
            page=page,
            step=20,
            url_args={'sortby': sortby},
        )

        transactions = WalletTransaction.search(
            domain,
            order=order,
            limit=20,
            offset=pager['offset'],
        )

        values = {
            'transactions': transactions,
            'page_name': 'wallet_transactions',
            'pager': pager,
            'default_url': '/my/wallet/transactions',
            'searchbar_sortings': searchbar_sortings,
            'sortby': sortby,
            'wallet_balance': partner.wallet_balance,
        }
        return request.render('sh_subscription_wallet.portal_wallet_transactions', values)
