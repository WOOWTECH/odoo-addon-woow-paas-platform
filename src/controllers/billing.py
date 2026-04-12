from __future__ import annotations

import logging
from typing import Any

from odoo import fields
from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)


class BillingController(Controller):
    _name = 'woow_paas_platform.billing'

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
        """
        List current user's bound payment methods.

        Currently supports a single ECPay card binding per partner.
        Returns the bound card as a one-item list, or an empty list.

        Returns:
            dict: { success, data: [{ id, type, last4, expiry, is_default, bound_date }] }
        """
        partner = request.env.user.partner_id.sudo()

        methods = []
        if partner.has_bound_credit_card():
            methods.append({
                'id': 'ecpay_default',
                'type': 'visa',
                'last4': partner.ecpay_card_last4 or '',
                'expiry': partner.ecpay_card_expiry or '',
                'is_default': True,
                'bound_date': partner.ecpay_card_bound_date.isoformat() if partner.ecpay_card_bound_date else None,
            })

        return {
            'success': True,
            'data': methods,
        }

    @route("/api/billing/payment-methods/add", auth="user", methods=["POST"], type="json")
    def api_payment_method_add(
        self,
        card_number: str | None = None,
        expiry: str | None = None,
        cvc: str | None = None,
        cardholder_name: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """
        Add/bind a credit card (placeholder).

        Actual ECPay tokenization API integration will be implemented separately.
        For now, returns an error indicating ECPay portal is required.

        Args:
            card_number: Card number.
            expiry: Expiry date (MM/YY).
            cvc: CVC code.
            cardholder_name: Name on card.

        Returns:
            dict with success=False and instruction to use ECPay portal.
        """
        return {
            'success': False,
            'error': 'Card binding requires ECPay integration. Please contact support.',
            'code': 'ecpay_required',
        }

    @route("/api/billing/payment-methods/<string:method_id>/delete", auth="user", methods=["POST"], type="json")
    def api_payment_method_delete(self, method_id: str, **kw) -> dict[str, Any]:
        """
        Unbind a payment method (credit card).

        Clears the partner's ECPay card fields.

        Args:
            method_id: Payment method identifier (e.g. 'ecpay_default').

        Returns:
            dict: { success: true }
        """
        partner = request.env.user.partner_id.sudo()

        if method_id != 'ecpay_default':
            return {'success': False, 'error': 'Payment method not found'}

        if not partner.has_bound_credit_card():
            return {'success': False, 'error': 'No card is currently bound'}

        partner.write({
            'ecpay_card_token': False,
            'ecpay_card_last4': False,
            'ecpay_card_expiry': False,
            'ecpay_card_bound_date': False,
        })

        _logger.info("Payment method unbound: partner=%s", partner.id)

        return {'success': True}

    @route("/api/billing/payment-methods/<string:method_id>/set-default", auth="user", methods=["POST"], type="json")
    def api_payment_method_set_default(self, method_id: str, **kw) -> dict[str, Any]:
        """
        Set a payment method as default.

        Since only one card is supported, this is a no-op.

        Args:
            method_id: Payment method identifier.

        Returns:
            dict: { success: true }
        """
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

        # Add wallet points (skip actual ECPay charge for now)
        total_credits = plan.credits + plan.bonus
        partner.add_wallet_points(total_credits, f"Top-up: {plan.name}")

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
                'error': 'Insufficient credits',
                'data': {
                    'balance': balance,
                    'required': amount,
                },
            }

        # Post the invoice first if it is a draft
        if invoice.state == 'draft':
            invoice.action_post()

        # Deduct wallet
        deducted = partner.deduct_wallet_points(amount, f"Invoice payment: {invoice.name}")
        if deducted <= 0:
            return {
                'success': False,
                'error': 'Wallet deduction failed',
            }

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
