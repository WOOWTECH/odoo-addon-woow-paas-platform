from __future__ import annotations

import logging
from typing import Any

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
