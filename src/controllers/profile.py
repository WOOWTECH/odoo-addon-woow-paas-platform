from __future__ import annotations

import logging
import re
from typing import Any

from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

# Error codes — stable contract for frontend
ERR_NAME_REQUIRED = 'name_required'
ERR_INVALID_EMAIL = 'invalid_email'
ERR_INVALID_CARRIER_TYPE = 'invalid_carrier_type'

VALID_CARRIER_TYPES = (
    'email', 'mobile', 'citizen', 'triplicate', 'paper', 'donation',
)

# Accepts typical public emails. Intentionally stricter than RFC 5322:
#   - requires at least one dot after @ (rejects "user@localhost" and
#     bare-intranet addresses — acceptable because this is an end-user
#     billing/notification field, not a deliverability-critical route).
#   - TLD must be ASCII letters only, 2+ chars (rejects IDN TLDs like .中国).
# If future requirements need intranet/IDN support, swap in a library.
# No ReDoS risk: single quantifier per character class, linear backtracking.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class ProfileController(Controller):
    _name = 'woow_paas_platform.profile'

    # ==================== Internal helpers ====================

    def _me_guard(self):
        """Return the current session user's partner (sudoed for write access).

        Security invariant: this is the ONLY way the two endpoints below
        identify the target partner. Callers cannot supply a partner_id.
        """
        return request.env.user.partner_id.sudo()

    def _serialize(self, partner):
        return {
            'id': partner.id,
            'name': partner.name or '',
            'email': partner.email or '',
            'phone': partner.phone or '',
            'avatar_url': f'/web/image/res.partner/{partner.id}/image_128',
        }

    def _serialize_invoice(self, partner):
        return {
            'invoice_carrier_type': partner.woow_invoice_carrier_type or 'email',
            'invoice_carrier': partner.woow_invoice_carrier or '',
            'invoice_tax_id': partner.woow_invoice_tax_id or '',
            'invoice_title': partner.woow_invoice_title or '',
            'invoice_lovecode': partner.woow_invoice_lovecode or '',
        }

    # ==================== Endpoints ====================

    @route("/api/me", auth="user", methods=["POST"], type="json")
    def api_me(self, **kw) -> dict[str, Any]:
        """Return the current session user's partner profile."""
        return {'success': True, 'data': self._serialize(self._me_guard())}

    @route("/api/me/update", auth="user", methods=["POST"], type="json")
    def api_me_update(
        self,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """Update the current user's partner name/email/phone (full replace).

        Contract: this is NOT a patch. All three fields are written on every
        call; callers must re-send existing values they want preserved.

        Per-field semantics:
            name:  required, non-blank after strip(); whitespace-only → rejected
                   with code 'name_required'.
            email: optional. Empty, None, or whitespace-only → cleared (stored
                   as False). Non-blank → validated against EMAIL_RE;
                   malformed → 'invalid_email'.
            phone: optional. Empty, None, or whitespace-only → cleared (stored
                   as False). Any non-blank string is accepted as-is (the
                   ECPay integration sanitizes at send time — see
                   _sanitize_phone_for_ecpay in billing.py).

        Security: all other kwargs (including any `partner_id` a caller might
        supply) are ignored — writes always target
        `request.env.user.partner_id`.
        """
        partner = self._me_guard()

        if name is None or not name.strip():
            return {
                'success': False,
                'error': 'Name is required',
                'code': ERR_NAME_REQUIRED,
            }

        email_clean = (email or '').strip()
        if email_clean and not EMAIL_RE.match(email_clean):
            return {
                'success': False,
                'error': 'Invalid email format',
                'code': ERR_INVALID_EMAIL,
            }

        partner.write({
            'name': name.strip(),
            'email': email_clean or False,
            'phone': (phone or '').strip() or False,
        })
        return {'success': True, 'data': self._serialize(partner)}

    @route("/api/me/invoice-preference", auth="user", methods=["POST"], type="json")
    def api_me_invoice_preference(self, **kw) -> dict[str, Any]:
        """Return the current user's stored invoice carrier preference."""
        return {'success': True, 'data': self._serialize_invoice(self._me_guard())}

    @route(
        "/api/me/invoice-preference/update",
        auth="user", methods=["POST"], type="json",
    )
    def api_me_invoice_preference_update(
        self,
        invoice_carrier_type: str | None = None,
        invoice_carrier: str | None = None,
        invoice_tax_id: str | None = None,
        invoice_title: str | None = None,
        invoice_lovecode: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """Replace this user's invoice carrier preference. Full replace, not
        patch — callers must re-send any fields they want preserved.

        Validation is intentionally minimal: ECPay's validate_ecpay_invoice
        runs at issue time and is the source of truth for carrier-number /
        love-code / tax-id formats. Doing it here too would risk drift.
        """
        partner = self._me_guard()
        ct = (invoice_carrier_type or 'email').strip()
        if ct not in VALID_CARRIER_TYPES:
            return {
                'success': False,
                'error': 'Invalid carrier type',
                'code': ERR_INVALID_CARRIER_TYPE,
            }
        partner.write({
            'woow_invoice_carrier_type': ct,
            'woow_invoice_carrier': (invoice_carrier or '').strip() or False,
            'woow_invoice_tax_id': (invoice_tax_id or '').strip() or False,
            'woow_invoice_title': (invoice_title or '').strip() or False,
            'woow_invoice_lovecode': (invoice_lovecode or '').strip() or False,
        })
        return {'success': True, 'data': self._serialize_invoice(partner)}
