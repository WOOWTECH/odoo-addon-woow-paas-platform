from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

from odoo import fields
from odoo.http import request, route, Controller
from odoo.tools import str2bool

_logger = logging.getLogger(__name__)

TAIPEI_TZ = timezone(timedelta(hours=8))

# Error codes returned by /api/billing/payment-methods/* endpoints.
# Stable contract for frontend error handling.
ERR_PROVIDER_NOT_FOUND = 'provider_not_found'
ERR_SDK = 'sdk_error'
ERR_ECPAY_REJECTED = 'ecpay_rejected'
ERR_MISSING_PARAMS = 'missing_params'
ERR_MEMBER_MISMATCH = 'member_id_mismatch'
ERR_NOT_FOUND = 'not_found'
ERR_NOT_OWNER = 'not_owner'


def _sanitize_phone_for_ecpay(raw):
    """Strip non-digits from a phone string for ECPay.

    ECPay's ConsumerInfo.Phone rejects '+', '-', spaces, parentheses.
    Returns digits-only string, or '' if the result is too short to be
    a plausible phone number (ECPay's Phone field is optional).
    """
    if not raw:
        return ''
    digits = ''.join(c for c in raw if c.isdigit())
    return digits if len(digits) >= 7 else ''


class BillingController(Controller):
    _name = 'woow_paas_platform.billing'

    # ==================== Internal helpers ====================

    def _find_ecpg_provider(self):
        """Return the first ECPay provider with enable_ecpg=True, or empty recordset."""
        return request.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay'), ('enable_ecpg', '=', True)], limit=1,
        )

    def _get_owned_token(self, method_id):
        """Return (token, None) if current user owns a valid ECPG token, or (None, error_dict)."""
        token = request.env['payment.token'].sudo().browse(method_id)
        if not token.exists() or not token.ecpg_bindcard_id:
            return None, {'success': False, 'error': 'Token not found', 'code': ERR_NOT_FOUND}
        if token.partner_id != request.env.user.partner_id:
            return None, {'success': False, 'error': 'Not owner', 'code': ERR_NOT_OWNER}
        return token, None

    def _get_partner_ecpg_tokens(self, partner):
        """Return active ECPG-bound payment tokens for a partner."""
        return request.env['payment.token'].sudo().search([
            ('partner_id', '=', partner.id),
            ('ecpg_bindcard_id', '!=', False),
            ('active', '=', True),
        ])

    def _get_default_ecpg_token(self, partner, tokens=None):
        """Return the default ECPG token for a partner, or empty recordset.

        Prefers Enterprise's property_payment_token_id; otherwise falls back
        to the most recently created token so auto-topup matches what the
        Payment Methods UI marks as Default.
        """
        Token = request.env['payment.token']
        if tokens is None:
            tokens = self._get_partner_ecpg_tokens(partner)
        if not tokens:
            return Token
        default_token = Token
        if 'property_payment_token_id' in partner._fields:
            candidate = partner.with_company(
                partner.company_id or request.env.company
            ).sudo().property_payment_token_id
            if candidate and candidate in tokens:
                default_token = candidate
        if not default_token:
            default_token = tokens.sorted('id', reverse=True)[0]
        return default_token

    def _format_ecpg_expire(self, raw):
        # ECPay stores ecpg_card_expire as YYMM (e.g. '2812' = Dec 2028).
        if not raw or len(raw) != 4:
            return raw or ''
        return f"{raw[2:]}/{raw[:2]}"

    def _get_primary_card_info(self, partner):
        """Return primary bound-card info for a partner.

        Prefers ECPG payment.token (multi-card system, matches Payment Methods
        page). Falls back to legacy sh_subscription_wallet partner fields so
        old data keeps working.
        """
        default_token = self._get_default_ecpg_token(partner)
        if default_token:
            last4 = (default_token.payment_details or '').replace('*', '').strip()
            return {
                'bound': True,
                'last4': last4,
                'expiry': self._format_ecpg_expire(default_token.ecpg_card_expire or ''),
                'bank': default_token.ecpg_issuing_bank or '',
            }
        if partner.has_bound_credit_card():
            return {
                'bound': True,
                'last4': partner.ecpay_card_last4 or '',
                'expiry': partner.ecpay_card_expiry or '',
                'bank': '',
            }
        return {'bound': False, 'last4': '', 'expiry': '', 'bank': ''}

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
                    - card_bank (str): Issuing bank of bound card
        """
        partner = request.env.user.partner_id.sudo()
        card = self._get_primary_card_info(partner)

        return {
            'success': True,
            'data': {
                'balance': partner.wallet_balance,
                'card_bound': card['bound'],
                'card_last4': card['last4'],
                'card_expiry': card['expiry'],
                'card_bank': card['bank'],
            },
        }

    # ==================== Payment Method API ====================

    @route("/api/billing/payment-methods", auth="user", methods=["POST"], type="json")
    def api_payment_methods(self, **kw) -> dict[str, Any]:
        """List the current user's ECPG-bound cards (reads payment.token locally)."""
        partner = request.env.user.partner_id
        tokens = self._get_partner_ecpg_tokens(partner)
        default_token = self._get_default_ecpg_token(partner, tokens=tokens)
        default_token_id = default_token.id if default_token else False

        return {
            'success': True,
            'data': [{
                'id': t.id,
                'last4': (t.payment_details or '').replace('*', '').strip(),
                'expire': self._format_ecpg_expire(t.ecpg_card_expire or ''),
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

        provider = self._find_ecpg_provider()
        if not provider:
            return {
                'success': False,
                'error': 'No ECPay provider with ECPG enabled',
                'code': ERR_PROVIDER_NOT_FOUND,
            }

        partner = request.env.user.partner_id
        member_id = provider._ecpg_build_member_id(partner)
        trade_no = request.env['payment.transaction']._ecpg_build_bind_trade_no()
        _logger.info(
            "[BIND] prepare_bind partner=%s member=%s trade_no=%s sandbox=%s",
            partner.id, member_id, trade_no, provider.state != 'enabled',
        )

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
            'Phone': _sanitize_phone_for_ecpay(partner.phone),
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
                # ECPay POSTs (not GETs) to OrderResultURL after 3DS, so it must
                # accept POST without CSRF. Our /woow shell doesn't, hence a
                # dedicated bouncer at /api/billing/payment-methods/bind-result/<trade_no>
                # that 303-redirects the browser into the SPA hash route.
                order_result_url=(
                    f'{base}/api/billing/payment-methods/bind-result/{trade_no}'
                ),
            )
        except ECPGError as e:
            _logger.exception('ECPG prepare_bind failed: %s', e)
            return {'success': False, 'error': str(e), 'code': ERR_SDK}

        if result.get('RtnCode') != 1 or not result.get('Token'):
            _logger.warning(
                "[BIND] prepare_bind ECPay rejected partner=%s trade_no=%s rtn=%s msg=%r",
                partner.id, trade_no, result.get('RtnCode'), result.get('RtnMsg'),
            )
            return {
                'success': False,
                'error': result.get('RtnMsg') or 'Unknown ECPay error',
                'code': ERR_ECPAY_REJECTED,
            }

        _logger.info(
            "[BIND] prepare_bind ok partner=%s trade_no=%s token_len=%s",
            partner.id, trade_no, len(result['Token']),
        )
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

    @route(
        "/api/billing/payment-methods/bind-result/<string:trade_no>",
        auth="public",
        type="http",
        methods=["GET", "POST"],
        csrf=False,
        save_session=False,
    )
    def api_payment_method_bind_result(self, trade_no, **kw):
        """Browser-facing OrderResultURL landing for the ECPG bind 3DS flow.

        ECPay redirects the user's browser here via a cross-site form POST after
        3DS. Two things to handle carefully:

        1) Persist the token. The S2S `/payment/ecpg/bind_callback` is a separate
           server-to-server POST from ECPay that may or may not fire reliably on
           sandbox. We re-use the official `_handle_bind_callback` helper here so
           the payment.token row exists by the time the user lands back in the SPA.
           Idempotency in the helper handles double-creation if the S2S fires too.

        2) Don't 303 to `/woow`. The browser's POST to this endpoint is cross-site,
           so SameSite=Lax cookies are stripped. A server 303 chains the cross-site
           context into the GET, so cookies still aren't sent and `/woow` (auth=user)
           bounces to /web/login. Instead, return a tiny same-origin HTML page that
           uses a JS top-level navigation — that GET is treated as same-origin so
           Lax cookies are sent and the SPA loads with the user's session intact.

        3) `save_session=False`. Even with the JS redirect above, Odoo still mints
           an anonymous session for this `auth='public'` endpoint (because the
           incoming POST carries no session_id cookie) and its `_save_session`
           post-dispatch hook sets `Set-Cookie: session_id=<anon_sid>` on the
           response — which overwrites the user's authenticated cookie in the
           browser. `save_session=False` short-circuits `_save_session`, keeping
           the user's session_id intact. Upstream Odoo uses the same flag on
           `payment_mollie.mollie_return_from_checkout` and
           `payment_aps.aps_return_from_checkout` for the identical reason.
        """
        _logger.info(
            "[BIND] bind-result landing trade_no=%s kw_keys=%s",
            trade_no, list(kw.keys()),
        )
        try:
            from odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback import (
                ECPGCallback,
            )
            ECPGCallback()._handle_bind_callback(kw)
            _logger.info(
                "[BIND] bind-result _handle_bind_callback ok trade_no=%s",
                trade_no,
            )
        except Exception:
            _logger.exception('bind-result: error persisting bind for trade_no=%s', trade_no)

        base = (request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                or request.httprequest.url_root).rstrip('/')
        target = f'{base}/woow#/settings/payment-methods?bind_status=ok&trade_no={trade_no}'
        # JS redirect avoids the cross-site POST → 303 → cookie-strip pitfall.
        html = (
            '<!doctype html><html><head><meta charset="utf-8"><title>綁定完成</title>'
            f'<meta http-equiv="refresh" content="0;url={target}">'
            f'<script>window.location.replace({json.dumps(target)});</script>'
            '</head><body>綁定完成，正在返回…</body></html>'
        )
        return request.make_response(
            html, headers=[('Content-Type', 'text/html; charset=utf-8')],
        )

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
                'code': ERR_MISSING_PARAMS,
            }

        provider = self._find_ecpg_provider()
        if not provider:
            return {
                'success': False,
                'error': 'No ECPay provider with ECPG enabled',
                'code': ERR_PROVIDER_NOT_FOUND,
            }

        partner = request.env.user.partner_id
        expected = provider._ecpg_build_member_id(partner)
        if member_id != expected:
            _logger.warning(
                "[BIND] create_bind member_id mismatch partner=%s expected=%s got=%s",
                partner.id, expected, member_id,
            )
            return {
                'success': False,
                'error': 'member_id does not match current session',
                'code': ERR_MEMBER_MISMATCH,
            }

        _logger.info(
            "[BIND] create_bind partner=%s member=%s trade_no=%s",
            partner.id, member_id, trade_no,
        )
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
            return {'success': False, 'error': str(e), 'code': ERR_SDK}

        if result.get('RtnCode') != 1:
            _logger.warning(
                "[BIND] create_bind ECPay rejected partner=%s trade_no=%s rtn=%s msg=%r",
                partner.id, trade_no, result.get('RtnCode'), result.get('RtnMsg'),
            )
            return {
                'success': False,
                'error': result.get('RtnMsg') or 'ECPay rejected binding',
                'code': ERR_ECPAY_REJECTED,
            }

        three_d_info = result.get('ThreeDInfo') or {}
        three_d_url = three_d_info.get('ThreeDURL') or ''
        bind_card_id = result.get('BindCardID') or ''
        _logger.info(
            "[BIND] create_bind ok partner=%s trade_no=%s has_3ds=%s bind_card_id=%s",
            partner.id, trade_no, bool(three_d_url), bind_card_id,
        )
        return {
            'success': True,
            'data': {
                'three_d_url': three_d_url,
                'bind_card_id': bind_card_id,
            },
        }

    @route("/api/billing/payment-methods/<int:method_id>/delete",
           auth="user", methods=["POST"], type="json")
    def api_payment_method_delete(self, method_id: int, **kw) -> dict[str, Any]:
        """Archive a bound card. Triggers ecpay addon's _handle_archiving override
        which calls DeleteMemberBindCard automatically."""
        token, err = self._get_owned_token(method_id)
        if err:
            _logger.info(
                "[BIND] delete_token rejected method=%s err=%s",
                method_id, err.get('code'),
            )
            return err
        _logger.info(
            "[BIND] delete_token method=%s partner=%s provider=%s",
            method_id, token.partner_id.id, token.provider_id.code,
        )
        token.active = False
        return {'success': True}

    @route("/api/billing/payment-methods/<int:method_id>/set-default",
           auth="user", methods=["POST"], type="json")
    def api_payment_method_set_default(self, method_id: int, **kw) -> dict[str, Any]:
        """Set this card as the partner's default payment token."""
        token, err = self._get_owned_token(method_id)
        if err:
            _logger.info(
                "[BIND] set_default rejected method=%s err=%s",
                method_id, err.get('code'),
            )
            return err
        partner = token.partner_id
        _logger.info(
            "[BIND] set_default method=%s partner=%s",
            method_id, partner.id,
        )
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
    def api_topup(self, plan_id: int | None = None,
                  invoice_intent: dict | None = None, **kw) -> dict[str, Any]:
        """Start a top-up charge.

        Returns success payload with either a freshly started flow
        ``{topup_id, redirect_url, state}`` or — if the partner already has a
        pending topup — the existing record ``{topup_id, redirect_url, resumed}``.
        Errors carry a stable ``code`` string.
        """
        ICP = request.env['ir.config_parameter'].sudo()
        topup_enabled = str2bool(
            ICP.get_param('woow_paas_platform.topup_payment_enabled', 'False')
        )
        if not topup_enabled:
            return {
                'success': False,
                'error': 'Credit top-up is not yet available.',
                'code': 'payment_not_configured',
            }

        if not plan_id:
            return {'success': False, 'error': 'plan_id is required',
                    'code': 'missing_params'}

        Plan = request.env['woow_paas_platform.topup_plan'].sudo()
        plan = Plan.browse(int(plan_id))
        if not plan.exists() or not plan.active:
            return {'success': False, 'error': 'Plan not found or inactive',
                    'code': 'plan_not_found'}

        partner = request.env.user.partner_id.sudo()
        _logger.info(
            "[TOPUP] api_topup entry partner=%s plan=%s amount=%s credits=%s bonus=%s",
            partner.id, plan.id, plan.amount, plan.credits, plan.bonus,
        )
        if not self._get_primary_card_info(partner)['bound']:
            _logger.info("[TOPUP] api_topup no_card partner=%s", partner.id)
            return {'success': False, 'error': 'No payment method configured',
                    'code': 'no_card'}

        Topup = request.env['woow.wallet.topup'].sudo()
        existing = Topup.search([
            ('partner_id', '=', partner.id),
            ('state', '=', 'pending'),
        ], limit=1)
        if existing:
            _logger.info(
                "[TOPUP] api_topup resume existing topup=%s partner=%s",
                existing.id, partner.id,
            )
            return {'success': True, 'data': {
                'topup_id': existing.id,
                'redirect_url': self._resume_3ds_url(existing),
                'resumed': True,
                'state': existing.state,
            }}

        token = self._get_default_ecpg_token(partner)
        if not token:
            _logger.info(
                "[TOPUP] api_topup no ECPG token partner=%s", partner.id,
            )
            return {'success': False, 'error': 'No payment method configured',
                    'code': 'no_card'}

        invoice_vals = self._build_invoice_intent_vals(invoice_intent or {})
        topup = Topup.create({
            'partner_id': partner.id,
            'plan_id': plan.id,
            'amount': plan.amount,
            'credits': plan.credits,
            'bonus': plan.bonus,
            'currency_id': request.env.company.currency_id.id,
            'state': 'draft',
            **invoice_vals,
        })
        _logger.info(
            "[TOPUP] draft created topup=%s partner=%s token=%s",
            topup.id, partner.id, token.id,
        )

        try:
            result = token._charge(
                amount=plan.amount,
                reference=f"TOPUP{topup.id}",
                currency_id=topup.currency_id.id,
                purpose_field='woow_topup_id',
                purpose_id=topup.id,
            )
        except Exception as e:
            _logger.exception(
                "[TOPUP] charge exception topup=%s partner=%s",
                topup.id, partner.id,
            )
            topup.write({'state': 'failed', 'failure_code': 'sdk_error',
                         'failure_message': str(e)[:200]})
            return {'success': False, 'error': 'Payment service unavailable',
                    'code': 'sdk_error'}

        tx = result['tx']
        new_state = 'pending' if result['state'] in ('draft', 'pending') else result['state']
        topup.write({
            'payment_transaction_id': tx.id,
            'state': new_state,
        })
        _logger.info(
            "[TOPUP] charge returned topup=%s tx=%s tx_state=%s topup_state=%s has_redirect=%s",
            topup.id, tx.id, result['state'], new_state,
            bool(result.get('redirect_url')),
        )

        return {'success': True, 'data': {
            'topup_id': topup.id,
            'redirect_url': result['redirect_url'],
            'state': topup.state,
        }}

    def _build_invoice_intent_vals(self, intent):
        """Whitelist invoice fields we persist on the topup order."""
        allowed = ('invoice_carrier_type', 'invoice_carrier', 'invoice_tax_id',
                   'invoice_title', 'invoice_lovecode')
        return {k: intent[k] for k in allowed if intent.get(k)}

    def _resume_3ds_url(self, topup):
        """Return the most recent ThreeDURL for a pending topup, if still valid.

        Phase 1 returns None; the frontend surfaces an "awaiting confirmation"
        message. Phase 2 can query ECPay to refresh the 3DS URL if supported.
        """
        return None

    @route("/api/billing/topup/<int:topup_id>/status",
           auth="user", methods=["POST"], type="json")
    def api_topup_status(self, topup_id, **kw) -> dict[str, Any]:
        """Return the current state of a top-up order owned by the caller.

        Used by the frontend after the 3DS redirect returns. Owner-only;
        non-owners get a ``not_found`` code to avoid leaking existence.
        """
        partner = request.env.user.partner_id
        topup = request.env['woow.wallet.topup'].sudo().browse(topup_id)
        if not topup.exists() or topup.partner_id.id != partner.id:
            _logger.info(
                "[TOPUP] status not_found topup=%s requester_partner=%s",
                topup_id, partner.id,
            )
            return {'success': False, 'error': 'Not found', 'code': 'not_found'}
        _logger.info(
            "[TOPUP] status topup=%s partner=%s state=%s tx=%s",
            topup.id, partner.id, topup.state,
            topup.payment_transaction_id.id if topup.payment_transaction_id else None,
        )
        return {'success': True, 'data': {
            'topup_id': topup.id,
            'state': topup.state,
            'amount': topup.amount,
            'credits': topup.credits,
            'bonus': topup.bonus,
            'new_balance': partner.wallet_balance,
            'failure_code': topup.failure_code or '',
            'failure_message': topup.failure_message or '',
            'plan_name': topup.plan_id.name,
        }}

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
        _logger.info(
            "[SUB] cancel request subscription=%s partner=%s reason=%r",
            subscription_id, partner.id, (reason or '')[:100],
        )

        if not sub.exists() or sub.sh_partner_id != partner:
            _logger.info(
                "[SUB] cancel not_found subscription=%s partner=%s",
                subscription_id, partner.id,
            )
            return {'success': False, 'error': 'Subscription not found'}

        if sub.state not in ('in_progress',):
            _logger.info(
                "[SUB] cancel state_reject subscription=%s state=%s",
                sub.id, sub.state,
            )
            return {'success': False, 'error': 'Only active subscriptions can be cancelled'}

        try:
            sub.write({'state': 'close'})
        except Exception as e:
            _logger.error("[SUB] cancel error subscription=%s: %s\n%s",
                          sub.id, str(e), traceback.format_exc())
            return {'success': False, 'error': 'Cancellation failed. Please try again.'}

        _logger.info(
            "[SUB] cancelled subscription=%s partner=%s reason=%s",
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

        Thin wrapper around ``account.move._pay_from_wallet`` — this endpoint
        owns ownership + balance validation and error-code mapping for the
        frontend; the deduct + post + reconcile sequence lives on the model
        so the subscription cron can reuse it (Phase 2c #5).
        """
        from odoo.exceptions import UserError

        partner = request.env.user.partner_id.sudo()
        invoice = request.env['account.move'].sudo().browse(invoice_id)
        _logger.info(
            "[INV] pay_from_wallet request invoice=%s partner=%s",
            invoice_id, partner.id,
        )

        if not invoice.exists() or invoice.partner_id != partner:
            _logger.info(
                "[INV] pay not_found invoice=%s partner=%s",
                invoice_id, partner.id,
            )
            return {'success': False, 'error': 'Invoice not found'}
        if invoice.state not in ('draft', 'posted'):
            _logger.info(
                "[INV] pay state_reject invoice=%s state=%s",
                invoice.id, invoice.state,
            )
            return {'success': False, 'error': 'Invoice is not payable'}
        if invoice.payment_state == 'paid':
            _logger.info(
                "[INV] pay already_paid invoice=%s",
                invoice.id,
            )
            return {'success': False, 'error': 'Invoice is already paid'}

        amount = invoice.amount_total if invoice.state == 'draft' else invoice.amount_residual
        balance = partner.wallet_balance
        _logger.info(
            "[INV] pay amount=%s balance=%s invoice=%s state=%s payment_state=%s",
            amount, balance, invoice.id, invoice.state, invoice.payment_state,
        )
        if balance < amount:
            _logger.info(
                "[INV] pay insufficient_balance invoice=%s needed=%s have=%s",
                invoice.id, amount, balance,
            )
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
            invoice._pay_from_wallet()
        except UserError as e:
            _logger.info(
                "[INV] pay UserError invoice=%s err=%s",
                invoice.id, str(e)[:200],
            )
            return {'success': False, 'error': str(e)}
        except Exception as e:
            _logger.error("[INV] pay error invoice=%s: %s\n%s",
                          invoice.id, str(e), traceback.format_exc())
            return {'success': False, 'error': 'Payment processing failed.'}

        _logger.info(
            "[INV] pay ok invoice=%s new_balance=%s",
            invoice.id, partner.wallet_balance,
        )
        return {
            'success': True,
            'data': {
                'new_balance': partner.wallet_balance,
                'invoice_status': self._invoice_status_label(invoice),
            },
        }
