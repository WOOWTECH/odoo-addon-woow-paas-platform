from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from odoo import fields
from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))


def _find_ecpg_provider(env):
    """Return the first ECPay provider with enable_ecpg=True, or an empty recordset."""
    return env['payment.provider'].sudo().search(
        [('code', '=', 'ECPay'), ('enable_ecpg', '=', True)], limit=1,
    )


class BillingController(Controller):
    _name = 'woow_paas_platform.billing'

    # ==================== Transaction API ====================

    @route("/api/billing/transactions", auth="user", methods=["POST"], type="json")
    def api_transactions(self, page: int = 1, limit: int = 20, **kw) -> dict[str, Any]:
        """List current user's wallet transactions with pagination.

        Args:
            page: Page number (1-based).
            limit: Items per page (max 50).

        Returns:
            dict with success, data (list), total, page, limit.
        """
        partner = request.env.user.partner_id
        Transaction = request.env['wallet.transaction'].sudo()

        domain = [('partner_id', '=', partner.id)]

        limit = max(1, min(int(limit), 50))
        page = max(1, int(page))
        offset = (page - 1) * limit

        total = Transaction.search_count(domain)
        transactions = Transaction.search(domain, order='create_date desc, id desc', limit=limit, offset=offset)

        data = []
        for txn in transactions:
            data.append({
                'id': txn.id,
                'date': txn.create_date.isoformat() if txn.create_date else None,
                'description': txn.description or txn.name or '',
                'transaction_type': 'credit' if txn.amount >= 0 else 'debit',
                'amount': txn.amount,
                'balance_after': txn.balance_after,
            })

        return {
            'success': True,
            'data': data,
            'total': total,
            'page': page,
            'limit': limit,
        }

    # ==================== Wallet API ====================

    @route("/api/billing/wallet", auth="user", methods=["POST"], type="json")
    def api_wallet(self) -> dict[str, Any]:
        """
        Get current user's wallet balance and credit card info.

        Returns:
            dict: Wallet information including:
                - success (bool): True
                - data (dict):
                    - balance (float): Wallet point balance
                    - card_bound (bool): Whether a credit card is bound
                    - card_last4 (str): Last 4 digits of bound card
                    - card_expiry (str): Card expiry date (MM/YY)
        """
        partner = request.env.user.partner_id.sudo()

        return {
            'success': True,
            'data': {
                'balance': partner.wallet_balance,
                'card_bound': partner.has_bound_credit_card(),
                'card_last4': partner.ecpay_card_last4 or '',
                'card_expiry': partner.ecpay_card_expiry or '',
            },
        }

    # ==================== Payment Method API ====================

    @route("/api/billing/payment-methods", auth="user", methods=["POST"], type="json")
    def api_payment_methods(self, **kw) -> dict[str, Any]:
        """List the current user's ECPG-bound cards (reads payment.token locally)."""
        partner = request.env.user.partner_id
        tokens = request.env['payment.token'].sudo().search([
            ('partner_id', '=', partner.id),
            ('ecpg_bindcard_id', '!=', False),
            ('active', '=', True),
        ])
        default_token_id = False
        if 'property_payment_token_id' in partner._fields:
            default_token = partner.with_company(
                partner.company_id or request.env.company
            ).sudo().property_payment_token_id
            default_token_id = default_token.id if default_token else False

        def _format_expire(raw):
            # ECPay's ecpg_card_expire is MMYY (e.g. '1226' = Dec 2026)
            if not raw or len(raw) != 4:
                return raw or ''
            return f"{raw[:2]}/{raw[2:]}"

        return {
            'success': True,
            'data': [{
                'id': t.id,
                'last4': (t.payment_details or '').replace('*', '').strip(),
                'expire': _format_expire(t.ecpg_card_expire or ''),
                'bank': t.ecpg_issuing_bank or '',
                'type': 'credit_card',
                'is_default': t.id == default_token_id,
            } for t in tokens],
        }

    @route("/api/billing/payment-methods/prepare-bind", auth="user", methods=["POST"], type="json")
    def api_payment_method_prepare_bind(self, **kw) -> dict[str, Any]:
        """Step 1 of ECPG binding flow: obtain a Token for the JS SDK.

        3DS OrderResultURL is set to our OWL Payment Methods page so the user lands
        on `#/settings/payment-methods?bind_status=ok&trade_no=...` after ECPay
        completes the binding.
        """
        from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import ECPGClient, ECPGError

        provider = _find_ecpg_provider(request.env)
        if not provider:
            return {
                'success': False,
                'error': 'No ECPay provider with ECPG enabled',
                'code': 'provider_not_found',
            }

        partner = request.env.user.partner_id
        member_id = provider._ecpg_build_member_id(partner)
        trade_no = request.env['payment.transaction']._ecpg_build_bind_trade_no()

        client = ECPGClient(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )
        # Use web.base.url so ECPay always gets https:// even when behind TLS proxy.
        base = (request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                or request.httprequest.url_root).rstrip('/')
        consumer_info = {
            'MerchantMemberID': member_id,
            'Email': partner.email or '',
            'Phone': partner.phone or '',
            'Name': partner.name or '',
            'CountryCode': '158',
        }
        order_info = {
            'MerchantTradeDate': datetime.now(TAIPEI_TZ).strftime('%Y/%m/%d %H:%M:%S'),
            'MerchantTradeNo': trade_no,
            'TotalAmount': '5',
            'TradeDesc': '綁卡驗證',
            'ItemName': '綁卡',
            # Reuse ecpay addon's existing callback that persists payment.token for us
            'ReturnURL': f'{base}/payment/ecpg/bind_callback',
        }
        try:
            result = client.get_token_by_binding_card(
                consumer_info=consumer_info,
                order_info=order_info,
                order_result_url=(
                    f'{base}/woow#/settings/payment-methods'
                    f'?bind_status=ok&trade_no={trade_no}'
                ),
            )
        except ECPGError as e:
            _logger.exception('ECPG prepare_bind failed: %s', e)
            return {'success': False, 'error': str(e), 'code': 'sdk_error'}

        if result.get('RtnCode') != 1 or not result.get('Token'):
            return {
                'success': False,
                'error': result.get('RtnMsg') or 'Unknown ECPay error',
                'code': 'ecpay_rejected',
            }

        return {
            'success': True,
            'data': {
                'token': result['Token'],
                'expire_date': result.get('TokenExpireDate'),
                'member_id': member_id,
                'trade_no': trade_no,
                'merchant_id': provider.MerchantID,
                'sandbox': provider.state != 'enabled',
            },
        }

    @route("/api/billing/payment-methods/create-bind", auth="user", methods=["POST"], type="json")
    def api_payment_method_create_bind(
        self,
        member_id: str | None = None,
        trade_no: str | None = None,
        bind_card_pay_token: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """Step 2 of ECPG binding flow.

        Consumes the BindCardPayToken issued by the ECPay JS SDK and returns
        ThreeDURL (if 3DS is required) so the frontend can redirect the browser.
        """
        from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import ECPGClient, ECPGError

        if not (member_id and trade_no and bind_card_pay_token):
            return {
                'success': False,
                'error': 'Missing member_id, trade_no, or bind_card_pay_token',
                'code': 'missing_params',
            }

        provider = _find_ecpg_provider(request.env)
        if not provider:
            return {
                'success': False,
                'error': 'No ECPay provider with ECPG enabled',
                'code': 'provider_not_found',
            }

        partner = request.env.user.partner_id
        expected = provider._ecpg_build_member_id(partner)
        if member_id != expected:
            return {
                'success': False,
                'error': 'member_id does not match current session',
                'code': 'member_id_mismatch',
            }

        client = ECPGClient(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )
        try:
            result = client.create_bind_card(
                member_id=member_id,
                bind_card_pay_token=bind_card_pay_token,
            )
        except ECPGError as e:
            _logger.exception('ECPG create_bind failed: %s', e)
            return {'success': False, 'error': str(e), 'code': 'sdk_error'}

        if result.get('RtnCode') != 1:
            return {
                'success': False,
                'error': result.get('RtnMsg') or 'ECPay rejected binding',
                'code': 'ecpay_rejected',
            }

        three_d_info = result.get('ThreeDInfo') or {}
        return {
            'success': True,
            'data': {
                'three_d_url': three_d_info.get('ThreeDURL') or '',
                'bind_card_id': result.get('BindCardID') or '',
            },
        }

    @route("/api/billing/payment-methods/<int:method_id>/delete",
           auth="user", methods=["POST"], type="json")
    def api_payment_method_delete(self, method_id: int, **kw) -> dict[str, Any]:
        """Archive a bound card. Triggers ecpay addon's _handle_archiving override
        which calls DeleteMemberBindCard automatically."""
        token = request.env['payment.token'].sudo().browse(method_id)
        if not token.exists() or not token.ecpg_bindcard_id:
            return {'success': False, 'error': 'Token not found', 'code': 'not_found'}
        if token.partner_id != request.env.user.partner_id:
            return {'success': False, 'error': 'Not owner', 'code': 'not_owner'}
        token.active = False
        return {'success': True}

    @route("/api/billing/payment-methods/<int:method_id>/set-default",
           auth="user", methods=["POST"], type="json")
    def api_payment_method_set_default(self, method_id: int, **kw) -> dict[str, Any]:
        """Set this card as the partner's default payment token."""
        token = request.env['payment.token'].sudo().browse(method_id)
        if not token.exists() or not token.ecpg_bindcard_id:
            return {'success': False, 'error': 'Token not found', 'code': 'not_found'}
        if token.partner_id != request.env.user.partner_id:
            return {'success': False, 'error': 'Not owner', 'code': 'not_owner'}
        partner = token.partner_id
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(
                partner.company_id or request.env.company
            ).sudo().property_payment_token_id = token
        return {'success': True}

    # ==================== Top-up API ====================

    @route("/api/billing/wallet/topup-plans", auth="user", methods=["POST"], type="json")
    def api_topup_plans(self, category: str | None = None, **kw) -> dict[str, Any]:
        """List available credit top-up plans.

        Args:
            category: Optional filter by category ('recommended', 'large_volume').

        Returns:
            dict with success and data (list of plan dicts).
        """
        Plan = request.env['woow_paas_platform.topup_plan'].sudo()

        domain = [('active', '=', True)]
        if category:
            domain.append(('category', '=', category))

        plans = Plan.search(domain, order='sequence, id')

        return {
            'success': True,
            'data': [{
                'id': p.id,
                'name': p.name,
                'amount': p.amount,
                'credits': p.credits,
                'bonus': p.bonus,
                'label': p.label or '',
                'icon': p.icon or 'toll',
                'category': p.category or 'all',
            } for p in plans],
        }

    @route("/api/billing/wallet/topup", auth="user", methods=["POST"], type="json")
    def api_topup(self, plan_id: int | None = None, **kw) -> dict[str, Any]:
        """Top up wallet credits using a selected plan.

        Args:
            plan_id: ID of the top-up plan to purchase.

        Returns:
            dict with credits_added, bonus, and new_balance on success.
        """
        if not plan_id:
            return {'success': False, 'error': 'plan_id is required'}

        Plan = request.env['woow_paas_platform.topup_plan'].sudo()
        plan = Plan.browse(int(plan_id))

        if not plan.exists() or not plan.active:
            return {'success': False, 'error': 'Plan not found or inactive'}

        partner = request.env.user.partner_id.sudo()

        # Verify payment method is configured
        if not partner.has_bound_credit_card():
            return {
                'success': False,
                'error': 'No payment method configured',
                'code': 'no_card',
            }

        # Guard: actual payment processing not yet implemented
        ICP = request.env['ir.config_parameter'].sudo()
        if not ICP.get_param('woow_paas_platform.topup_payment_enabled', False):
            return {
                'success': False,
                'error': 'Credit top-up is not yet available. Payment processing is being configured.',
                'code': 'payment_not_configured',
            }

        try:
            # Add wallet points (skip actual ECPay charge for now)
            total_credits = plan.credits + plan.bonus
            partner.add_wallet_points(total_credits, f"Top-up: {plan.name}")
        except Exception as e:
            _logger.error("Error during wallet top-up: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'Top-up failed. Please try again.'}

        _logger.info(
            "Wallet top-up: plan=%s, credits=%s, bonus=%s, partner=%s",
            plan.name, plan.credits, plan.bonus, partner.id,
        )

        return {
            'success': True,
            'data': {
                'credits_added': plan.credits,
                'bonus': plan.bonus,
                'new_balance': partner.wallet_balance,
            },
        }

    # ==================== Subscription API ====================

    def _serialize_subscription(self, sub):
        """Return a compact dict for subscription list items."""
        # Recurrency label
        unit_labels = {
            'day': 'Daily',
            'week': 'Weekly',
            'month': 'Monthly',
            'year': 'Yearly',
        }
        recurrency_label = unit_labels.get(sub.sh_unit, '')
        if sub.sh_recurrency and sub.sh_recurrency > 1:
            recurrency_label = f"Every {sub.sh_recurrency} {sub.sh_unit}s"

        # Linked cloud service name (if any)
        CloudService = request.env['woow_paas_platform.cloud_service'].sudo()
        linked_service = CloudService.search([('subscription_id', '=', sub.id)], limit=1)
        service_name = linked_service.name if linked_service else None

        return {
            'id': sub.id,
            'name': sub.name or '',
            'state': sub.state,
            'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else '',
            'plan_price': sub.sh_plan_price,
            'start_date': sub.sh_start_date.isoformat() if sub.sh_start_date else None,
            'end_date': sub.sh_end_date.isoformat() if sub.sh_end_date else None,
            'next_payment': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
            'is_trial': sub.sh_trial,
            'trial_end_date': sub.sh_trial_end_date.isoformat() if sub.sh_trial_end_date else None,
            'recurrency_label': recurrency_label,
            'service_name': service_name,
        }

    @route("/api/billing/subscriptions", auth="user", methods=["POST"], type="json")
    def api_subscriptions(self, filter: str = 'active', page: int = 1, limit: int = 10, **kw) -> dict[str, Any]:
        """List current user's subscriptions with pagination and optional filter.

        Args:
            filter: 'active', 'past', or 'all'.
            page: Page number (1-based).
            limit: Items per page (max 50).

        Returns:
            dict with success, data (list), total, page, limit.
        """
        partner = request.env.user.partner_id
        Subscription = request.env['sh.subscription.subscription'].sudo()

        domain = [('sh_partner_id', '=', partner.id)]

        if filter == 'active':
            domain.append(('state', 'in', ('in_progress',)))
        elif filter == 'past':
            domain.append(('state', 'in', ('expire', 'close', 'cancel')))
        # 'all' → no state filter

        limit = max(1, min(int(limit), 50))
        page = max(1, int(page))
        offset = (page - 1) * limit

        total = Subscription.search_count(domain)
        subscriptions = Subscription.search(domain, order='create_date desc', limit=limit, offset=offset)

        return {
            'success': True,
            'data': [self._serialize_subscription(sub) for sub in subscriptions],
            'total': total,
            'page': page,
            'limit': limit,
        }

    @route("/api/billing/subscriptions/<int:subscription_id>", auth="user", methods=["POST"], type="json")
    def api_subscription_detail(self, subscription_id: int, **kw) -> dict[str, Any]:
        """Get full detail of a single subscription and its associated services.

        Returns:
            dict with subscription detail and linked cloud services.
        """
        partner = request.env.user.partner_id
        sub = request.env['sh.subscription.subscription'].sudo().browse(subscription_id)

        if not sub.exists() or sub.sh_partner_id != partner:
            return {'success': False, 'error': 'Subscription not found'}

        # Associated cloud services
        CloudService = request.env['woow_paas_platform.cloud_service'].sudo()
        services = CloudService.search([('subscription_id', '=', sub.id)])

        service_list = []
        for svc in services:
            template_name = ''
            if svc.template_id:
                template_name = svc.template_id.name or ''
            service_list.append({
                'id': svc.id,
                'name': svc.name or '',
                'state': svc.state,
                'subdomain': svc.subdomain or '',
                'template_name': template_name,
            })

        data = self._serialize_subscription(sub)
        data['services'] = service_list

        return {
            'success': True,
            'data': data,
        }

    @route("/api/billing/subscriptions/<int:subscription_id>/cancel", auth="user", methods=["POST"], type="json")
    def api_subscription_cancel(self, subscription_id: int, reason: str | None = None, **kw) -> dict[str, Any]:
        """Cancel a subscription.

        Args:
            subscription_id: Subscription ID.
            reason: Optional cancellation reason.

        Returns:
            dict with success=True on success.
        """
        partner = request.env.user.partner_id
        sub = request.env['sh.subscription.subscription'].sudo().browse(subscription_id)

        if not sub.exists() or sub.sh_partner_id != partner:
            return {'success': False, 'error': 'Subscription not found'}

        if sub.state not in ('in_progress',):
            return {'success': False, 'error': 'Only active subscriptions can be cancelled'}

        try:
            sub.write({'state': 'close'})
        except Exception as e:
            _logger.error("Error cancelling subscription: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'Cancellation failed. Please try again.'}

        _logger.info(
            "Subscription cancelled: id=%s, partner=%s, reason=%s",
            sub.id, partner.id, reason or '(none)',
        )

        return {'success': True}

    @route("/api/billing/upcoming-renewals", auth="user", methods=["POST"], type="json")
    def api_upcoming_renewals(self, **kw) -> dict[str, Any]:
        """Return subscriptions renewing within 3 days with low-balance status.

        Returns:
            dict with:
                - has_low_balance (bool): wallet balance < total needed
                - upcoming (list): each item has
                    subscription_id, plan_name, amount, renewal_date, sufficient
                - shortfall (float): max(0, total_needed - wallet_balance)
        """
        partner = request.env.user.partner_id
        today = fields.Date.today()
        cutoff = today + timedelta(days=3)

        subs = request.env['sh.subscription.subscription'].sudo().search([
            ('sh_partner_id', '=', partner.id),
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '>=', today),
            ('sh_date_of_next_payment', '<=', cutoff),
        ])

        upcoming = []
        total_needed = 0.0
        for sub in subs:
            amount = sub.sh_plan_price or 0.0
            total_needed += amount
            upcoming.append({
                'subscription_id': sub.id,
                'plan_name': sub.sh_subscription_plan_id.name if sub.sh_subscription_plan_id else '',
                'amount': amount,
                'renewal_date': sub.sh_date_of_next_payment.isoformat() if sub.sh_date_of_next_payment else None,
                'sufficient': partner.wallet_balance >= amount,
            })

        return {
            'success': True,
            'data': {
                'has_low_balance': partner.wallet_balance < total_needed,
                'upcoming': upcoming,
                'shortfall': max(0.0, total_needed - partner.wallet_balance),
            },
        }

    # ==================== Invoice API ====================

    def _invoice_status_label(self, invoice):
        """Compute a human-readable status label for an invoice.

        Mapping:
            - draft  → invoice not yet posted
            - paid   → fully paid
            - overdue → posted, not paid, past due date
            - waiting → posted, awaiting payment
        """
        if invoice.state == 'draft':
            return 'draft'
        if invoice.payment_state == 'paid':
            return 'paid'
        if invoice.invoice_date_due and invoice.invoice_date_due < fields.Date.today():
            return 'overdue'
        return 'waiting'

    def _serialize_invoice(self, invoice):
        """Return a compact dict for invoice list items."""
        return {
            'id': invoice.id,
            'name': invoice.name or '',
            'invoice_date': invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            'invoice_date_due': invoice.invoice_date_due.isoformat() if invoice.invoice_date_due else None,
            'amount_total': invoice.amount_total,
            'amount_residual': invoice.amount_residual,
            'state': invoice.state,
            'payment_state': invoice.payment_state or '',
            'status_label': self._invoice_status_label(invoice),
        }

    @route("/api/billing/invoices", auth="user", methods=["POST"], type="json")
    def api_invoices(
        self,
        page: int = 1,
        limit: int = 10,
        sort_by: str = 'invoice_date',
        sort_order: str = 'desc',
        status: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """List current user's invoices with pagination and optional status filter.

        Args:
            page: Page number (1-based).
            limit: Items per page (max 50).
            sort_by: Field to sort by (invoice_date | amount_total).
            sort_order: 'asc' or 'desc'.
            status: Optional filter – 'draft', 'waiting', 'paid', 'overdue'.

        Returns:
            dict with success, data (list), total, page, limit.
        """
        partner = request.env.user.partner_id
        Move = request.env['account.move'].sudo()

        domain = [
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
        ]

        today = fields.Date.today()
        if status == 'draft':
            domain.append(('state', '=', 'draft'))
        elif status == 'waiting':
            domain += [
                ('state', '=', 'posted'),
                ('payment_state', 'in', ('not_paid', 'partial')),
                '|',
                ('invoice_date_due', '=', False),
                ('invoice_date_due', '>=', today),
            ]
        elif status == 'paid':
            domain.append(('payment_state', '=', 'paid'))
        elif status == 'overdue':
            domain += [
                ('state', '=', 'posted'),
                ('payment_state', '!=', 'paid'),
                ('invoice_date_due', '<', today),
            ]

        # Sanitise sort parameters
        allowed_sort_fields = {'invoice_date', 'amount_total'}
        if sort_by not in allowed_sort_fields:
            sort_by = 'invoice_date'
        if sort_order not in ('asc', 'desc'):
            sort_order = 'desc'
        order = f'{sort_by} {sort_order}'

        # Clamp limit
        limit = max(1, min(int(limit), 50))
        page = max(1, int(page))
        offset = (page - 1) * limit

        total = Move.search_count(domain)
        invoices = Move.search(domain, order=order, limit=limit, offset=offset)

        return {
            'success': True,
            'data': [self._serialize_invoice(inv) for inv in invoices],
            'total': total,
            'page': page,
            'limit': limit,
        }

    @route("/api/billing/invoices/<int:invoice_id>", auth="user", methods=["POST"], type="json")
    def api_invoice_detail(self, invoice_id: int, **kw) -> dict[str, Any]:
        """Get full detail of a single invoice.

        Returns invoice header, line items, partner/company info,
        and optional subscription reference.
        """
        partner = request.env.user.partner_id
        invoice = request.env['account.move'].sudo().browse(invoice_id)

        if not invoice.exists() or invoice.partner_id != partner:
            return {'success': False, 'error': 'Invoice not found'}

        # Line items (skip section/note display lines)
        lines = []
        for line in invoice.invoice_line_ids.filtered(lambda l: l.display_type not in ('line_section', 'line_note')):
            lines.append({
                'product_name': line.product_id.display_name if line.product_id else (line.name or ''),
                'quantity': line.quantity,
                'price_unit': line.price_unit,
                'tax_names': ', '.join(line.tax_ids.mapped('name')) if line.tax_ids else '',
                'price_subtotal': line.price_subtotal,
            })

        # Partner address
        p = invoice.partner_id
        partner_address = {
            'name': p.name or '',
            'street': p.street or '',
            'city': p.city or '',
            'zip': p.zip or '',
            'country': p.country_id.name if p.country_id else '',
        }

        # Company address
        c = invoice.company_id.partner_id if invoice.company_id else None
        company_info = {}
        if c:
            company_info = {
                'name': invoice.company_id.name or '',
                'street': c.street or '',
                'city': c.city or '',
                'zip': c.zip or '',
                'country': c.country_id.name if c.country_id else '',
            }

        # Subscription reference (sh_subscription_id added by sh_subscription module)
        subscription_ref = None
        if hasattr(invoice, 'sh_subscription_id') and invoice.sh_subscription_id:
            subscription_ref = invoice.sh_subscription_id.name

        return {
            'success': True,
            'data': {
                'id': invoice.id,
                'name': invoice.name or '',
                'state': invoice.state,
                'payment_state': invoice.payment_state or '',
                'status_label': self._invoice_status_label(invoice),
                'invoice_date': invoice.invoice_date.isoformat() if invoice.invoice_date else None,
                'invoice_date_due': invoice.invoice_date_due.isoformat() if invoice.invoice_date_due else None,
                'amount_untaxed': invoice.amount_untaxed,
                'amount_tax': invoice.amount_tax,
                'amount_total': invoice.amount_total,
                'amount_residual': invoice.amount_residual,
                'partner': partner_address,
                'company': company_info,
                'lines': lines,
                'subscription_ref': subscription_ref,
            },
        }

    @route("/api/billing/invoices/<int:invoice_id>/pay", auth="user", methods=["POST"], type="json")
    def api_invoice_pay(self, invoice_id: int, **kw) -> dict[str, Any]:
        """Pay an invoice using the current user's wallet balance.

        Flow:
            1. Validate invoice belongs to user and is payable.
            2. Check wallet balance >= amount_residual.
            3. Post invoice if still in draft.
            4. Deduct wallet points.
            5. Create payment and reconcile with invoice.

        Returns:
            dict with new_balance and invoice_status on success.
        """
        partner = request.env.user.partner_id.sudo()
        invoice = request.env['account.move'].sudo().browse(invoice_id)

        if not invoice.exists() or invoice.partner_id != partner:
            return {'success': False, 'error': 'Invoice not found'}

        # Must be posted (or draft that we can post) and not yet fully paid
        if invoice.state not in ('draft', 'posted'):
            return {'success': False, 'error': 'Invoice is not payable'}
        if invoice.payment_state == 'paid':
            return {'success': False, 'error': 'Invoice is already paid'}

        amount = invoice.amount_residual
        if invoice.state == 'draft':
            # amount_residual is 0 for draft; use amount_total instead
            amount = invoice.amount_total

        balance = partner.wallet_balance
        if balance < amount:
            return {
                'success': False,
                'error': f'Insufficient credits. Your balance is {balance:.0f} pts but {amount:.0f} pts is required.',
                'code': 'insufficient_balance',
                'data': {
                    'balance': balance,
                    'required': amount,
                },
            }

        try:
            # Deduct wallet FIRST (before posting) to avoid race condition
            # where invoice is posted but wallet deduction fails
            deducted = partner.deduct_wallet_points(amount, f"Invoice payment: {invoice.name}")
            if deducted <= 0:
                return {
                    'success': False,
                    'error': 'Wallet deduction failed',
                }

            # Post the invoice after successful wallet deduction
            if invoice.state == 'draft':
                invoice.action_post()

            # Create payment and reconcile (same pattern as sh_subscription 'paid' mode)
            company = invoice.company_id.sudo()
            journal = company.sh_paid_subscription_journal
            if not journal:
                # Fallback: find first bank/cash journal
                journal = request.env['account.journal'].sudo().search([
                    ('company_id', '=', company.id),
                    ('type', 'in', ('bank', 'cash')),
                ], limit=1)
            if not journal:
                _logger.error("No payment journal found for company %s", company.name)
                return {'success': False, 'error': 'Payment journal not configured'}

            payment_method_line = journal.inbound_payment_method_line_ids[:1]
            payment = request.env['account.payment'].sudo().create({
                'journal_id': journal.id,
                'amount': amount,
                'payment_type': 'inbound',
                'payment_method_line_id': payment_method_line.id if payment_method_line else False,
                'partner_type': 'customer',
                'partner_id': partner.id,
            })
            payment.action_post()

            # Reconcile payment with invoice
            receivable_lines = invoice.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )
            payment_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )
            if receivable_lines and payment_lines:
                (receivable_lines + payment_lines).reconcile()

            # Refresh to get updated state
            invoice.invalidate_recordset(['payment_state', 'amount_residual'])

        except Exception as e:
            _logger.error("Error processing invoice payment: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'Payment processing failed.'}

        _logger.info(
            "Wallet payment for invoice %s: amount=%s, partner=%s",
            invoice.name, amount, partner.id,
        )

        return {
            'success': True,
            'data': {
                'new_balance': partner.wallet_balance,
                'invoice_status': self._invoice_status_label(invoice),
            },
        }
