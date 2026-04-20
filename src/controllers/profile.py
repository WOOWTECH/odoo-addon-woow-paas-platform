from __future__ import annotations

import logging
import re
from typing import Any

from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

# Error codes — stable contract for frontend
ERR_NAME_REQUIRED = 'name_required'
ERR_INVALID_EMAIL = 'invalid_email'

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
