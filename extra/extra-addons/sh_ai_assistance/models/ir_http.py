# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo import models
from odoo.http import request


class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def color_scheme(self):
        """
        Override color_scheme to respect user's color_scheme preference.

        Returns:
            str: 'light', 'dark', or 'system'

        User preference:
            - 'system': Use browser/OS preference (detected via prefers-color-scheme media query)
            - 'light': Force light mode
            - 'dark': Force dark mode
        """
        # Check if user is logged in
        if not request.session.uid:
            return super().color_scheme()

        try:
            user = self.env['res.users'].browse(request.session.uid)
            color_scheme = user.color_scheme

            if color_scheme == 'system':
                # When system preference is selected, check the cookie set by JavaScript
                # JavaScript detects OS/browser preference and sets this cookie
                cookie_scheme = request.httprequest.cookies.get('color_scheme_detected')
                if cookie_scheme in ('light', 'dark'):
                    return cookie_scheme
                # Default to light if browser hasn't set the cookie yet
                return 'light'
            elif color_scheme == 'dark':
                return 'dark'
            else:  # 'light' or any other value
                return 'light'
        except Exception:
            # Fallback to default if anything goes wrong
            return super().color_scheme()
